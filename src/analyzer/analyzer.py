import logging
from Queue import Empty
from redis import StrictRedis
from time import time, sleep
from threading import Thread
from collections import defaultdict
from multiprocessing import Process, Manager, Queue
from msgpack import Unpacker, unpackb, packb
from os import path, kill, getpid, system
from math import ceil
import traceback
import operator
import socket
import settings
import re

from alerters import trigger_alert
from algorithms import run_selected_algorithm
from algorithm_exceptions import *

logger = logging.getLogger("AnalyzerLog")

try:
  SERVER_METRIC_PATH = settings.SERVER_METRICS_NAME + '.'
except:
  SERVER_METRIC_PATH = ''

class Analyzer(Thread):
    def __init__(self, parent_pid):
        """
        Initialize the Analyzer
        """
        super(Analyzer, self).__init__()
        self.redis_conn = StrictRedis(**settings.REDIS_OPTS)
        self.daemon = True
        self.parent_pid = parent_pid
        self.current_pid = getpid()
        self.anomalous_metrics = Manager().list()
        self.exceptions_q = Queue()
        self.anomaly_breakdown_q = Queue()

    def check_if_parent_is_alive(self):
        """
        Self explanatory
        """
        try:
            kill(self.current_pid, 0)
            kill(self.parent_pid, 0)
        except:
            exit(0)

    def send_graphite_metric(self, name, value):
        if settings.GRAPHITE_HOST != '':
            sock = socket.socket()
            sock.connect((settings.GRAPHITE_HOST, settings.CARBON_PORT))
            sock.sendall('%s %s %i\n' % (name, value, time()))
            sock.close()
            return True

        return False

    def spin_process(self, i, unique_metrics):
        """
        Assign a bunch of metrics for a process to analyze.
        """
        # Discover assigned metrics
        keys_per_processor = int(ceil(float(len(unique_metrics)) / float(settings.ANALYZER_PROCESSES)))
        if i == settings.ANALYZER_PROCESSES:
            assigned_max = len(unique_metrics)
        else:
            assigned_max = min(len(unique_metrics), i * keys_per_processor)
        assigned_min = (i - 1) * keys_per_processor
        assigned_keys = range(assigned_min, assigned_max)

        # Compile assigned metrics
        assigned_metrics = [unique_metrics[index] for index in assigned_keys]

        # Check if this process is unnecessary
        if len(assigned_metrics) == 0:
            return

        # Multi get series
        raw_assigned = self.redis_conn.mget(assigned_metrics)

        # Make process-specific dicts
        exceptions = defaultdict(int)
        anomaly_breakdown = defaultdict(int)

        # Distill timeseries strings into lists
        for i, metric_name in enumerate(assigned_metrics):
            self.check_if_parent_is_alive()

            try:
                raw_series = raw_assigned[i]
                unpacker = Unpacker(use_list = False)
                unpacker.feed(raw_series)
                timeseries = list(unpacker)

                anomalous, ensemble, datapoint = run_selected_algorithm(timeseries, metric_name)

                # If it's anomalous, add it to list
                if anomalous:
                    base_name = metric_name.replace(settings.FULL_NAMESPACE, '', 1)
                    metric = [datapoint, base_name]
                    self.anomalous_metrics.append(metric)

                    # Get the anomaly breakdown - who returned True?
                    for index, value in enumerate(ensemble):
                        if value:
                            algorithm = settings.ALGORITHMS[index]
                            anomaly_breakdown[algorithm] += 1

            # It could have been deleted by the Roomba
            except TypeError:
                exceptions['DeletedByRoomba'] += 1
            except TooShort:
                exceptions['TooShort'] += 1
            except Stale:
                exceptions['Stale'] += 1
            except Boring:
                exceptions['Boring'] += 1
            except:
                exceptions['Other'] += 1
                logger.info(traceback.format_exc())

        # Add values to the queue so the parent process can collate
        for key, value in anomaly_breakdown.items():
            self.anomaly_breakdown_q.put((key, value))

        for key, value in exceptions.items():
            self.exceptions_q.put((key, value))

    def run(self):
        """
        Called when the process intializes.
        """
        while 1:
            now = time()

            # Make sure Redis is up
            try:
                self.redis_conn.ping()
            except:
                logger.error('skyline can\'t connect to redis')
                sleep(10)
                self.redis_conn = StrictRedis(**settings.REDIS_OPTS)
                continue

            # Discover unique metrics
            unique_metrics = list(self.redis_conn.smembers(settings.FULL_NAMESPACE + 'unique_metrics'))

            if len(unique_metrics) == 0:
                logger.info('no metrics in redis. try adding some - see README')
                sleep(10)
                continue

            # Spawn processes
            pids = []
            for i in range(1, settings.ANALYZER_PROCESSES + 1):
                if i > len(unique_metrics):
                    logger.info('WARNING: skyline is set for more cores than needed.')
                    break

                p = Process(target=self.spin_process, args=(i, unique_metrics))
                pids.append(p)
                p.start()

            # Send wait signal to zombie processes
            for p in pids:
                p.join()

            # Grab data from the queue and populate dictionaries
            exceptions = dict()
            anomaly_breakdown = dict()
            while 1:
                try:
                    key, value = self.anomaly_breakdown_q.get_nowait()
                    if key not in anomaly_breakdown.keys():
                        anomaly_breakdown[key] = value
                    else:
                        anomaly_breakdown[key] += value
                except Empty:
                    break

            while 1:
                try:
                    key, value = self.exceptions_q.get_nowait()
                    if key not in exceptions.keys():
                        exceptions[key] = value
                    else:
                        exceptions[key] += value
                except Empty:
                    break

            # Send alerts
            if settings.ENABLE_ALERTS:
                for alert in settings.ALERTS:
                    for metric in self.anomalous_metrics:
                        ALERT_MATCH_PATTERN = alert[0]
                        METRIC_PATTERN = metric[1]
                        alert_match_pattern = re.compile(ALERT_MATCH_PATTERN)
                        pattern_match = alert_match_pattern.match(METRIC_PATTERN)
                        if pattern_match:
                            cache_key = 'last_alert.%s.%s' % (alert[1], metric[1])
                            try:
                                last_alert = self.redis_conn.get(cache_key)
                                if not last_alert:
                                    self.redis_conn.setex(cache_key, alert[2], packb(metric[0]))
                                    trigger_alert(alert, metric)
                            except Exception as e:
                                logger.error("couldn't send alert: %s" % e)

            # Write anomalous_metrics to static webapp directory
            filename = path.abspath(path.join(path.dirname(__file__), '..', settings.ANOMALY_DUMP))
            with open(filename, 'w') as fh:
                # Make it JSONP with a handle_data() function
                anomalous_metrics = list(self.anomalous_metrics)
                anomalous_metrics.sort(key=operator.itemgetter(1))
                fh.write('handle_data(%s)' % anomalous_metrics)

            # Log progress
            logger.info('seconds to run    :: %.2f' % (time() - now))
            logger.info('total metrics     :: %d' % len(unique_metrics))
            logger.info('total analyzed    :: %d' % (len(unique_metrics) - sum(exceptions.values())))
            logger.info('total anomalies   :: %d' % len(self.anomalous_metrics))
            logger.info('exception stats   :: %s' % exceptions)
            logger.info('anomaly breakdown :: %s' % anomaly_breakdown)

            # Log to Graphite
            self.send_graphite_metric('skyline.analyzer.' + SERVER_METRIC_PATH + 'run_time', '%.2f' % (time() - now))
            self.send_graphite_metric('skyline.analyzer.' + SERVER_METRIC_PATH + 'total_analyzed', len(unique_metrics) - sum(exceptions.values()))
            self.send_graphite_metric('skyline.analyzer.' + SERVER_METRIC_PATH + 'total_anomalies', len(self.anomalous_metrics))
            self.send_graphite_metric('skyline.analyzer.' + SERVER_METRIC_PATH + 'total_metrics', len(unique_metrics))
            for key, value in exceptions.items():
                self.send_graphite_metric('skyline.analyzer.' + SERVER_METRIC_PATH + 'exceptions.' + key, value)
            for key, value in anomaly_breakdown.items():
                self.send_graphite_metric('skyline.analyzer.' + SERVER_METRIC_PATH + 'anomaly_breakdown.' + key, value)

            # Check canary metric
            raw_series = self.redis_conn.get(settings.FULL_NAMESPACE + settings.CANARY_METRIC)
            if raw_series is not None:
                unpacker = Unpacker(use_list = False)
                unpacker.feed(raw_series)
                timeseries = list(unpacker)
                time_human = (timeseries[-1][0] - timeseries[0][0]) / 3600
                projected = 24 * (time() - now) / time_human

                logger.info('canary duration   :: %.2f' % time_human)
                self.send_graphite_metric('skyline.analyzer.' + SERVER_METRIC_PATH + 'duration', '%.2f' % time_human)
                self.send_graphite_metric('skyline.analyzer.' + SERVER_METRIC_PATH + 'projected', '%.2f' % projected)

            # Reset counters
            self.anomalous_metrics[:] = []

            # Sleep if it went too fast
            if time() - now < 5:
                logger.info('sleeping due to low run time...')
                sleep(10)
