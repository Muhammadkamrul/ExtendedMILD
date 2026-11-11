"""
common.py -- shared helpers for the three cloud services of the SDN testbed.

* Real computation instead of time.sleep(): each service performs a small but
  meaningful workload (JSON validation, hashing, statistics, aggregation,
  compression). The size of one "work unit" is calibrated at start-up so that
  the healthy service time is similar on any machine. Fault knobs scale the
  amount of real work, so CPU, latency and queues react physically.
* A production WSGI server (waitress) with a bounded thread pool.
* Process metrics (CPU seconds, RSS) exported for every service.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
import zlib

import numpy as np
import psutil
from prometheus_client import Gauge

PROC_CPU_SECONDS = Gauge('service_proc_cpu_seconds_total', 'Cumulative CPU seconds of the service process', ['service'])
PROC_RSS = Gauge('service_proc_rss_bytes', 'Resident memory of the service process', ['service'])
UNIT_MS = Gauge('service_work_unit_ms', 'Calibrated duration of one work unit (ms)', ['service'])
KNOB = Gauge('service_fault_knob', 'Current fault knob (work multiplier)', ['service'])


def limit_threads():
    """Keep numpy single-threaded so that CPU usage maps to the service quota."""
    for v in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
        os.environ.setdefault(v, '1')


def write_pid(service, run_dir):
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, f'{service}.pid'), 'w') as f:
        f.write(str(os.getpid()))


def proc_metrics_loop(service):
    p = psutil.Process(os.getpid())
    while True:
        try:
            t = p.cpu_times()
            PROC_CPU_SECONDS.labels(service).set(t.user + t.system)
            PROC_RSS.labels(service).set(p.memory_info().rss)
        except Exception:
            pass
        time.sleep(1.0)


def start_proc_metrics(service):
    threading.Thread(target=proc_metrics_loop, args=(service,), daemon=True).start()


# ----------------------------------------------------------------------------
# computational kernels
# ----------------------------------------------------------------------------
def compile_rule(rule: dict) -> str:
    """Validate and canonicalise one policy rule, return its digest."""
    r = {'prio': int(rule.get('prio', 0)), 'src': str(rule.get('src', '0.0.0.0/0')),
         'dst': str(rule.get('dst', '0.0.0.0/0')), 'dport': int(rule.get('dport', 0)) % 65536,
         'action': str(rule.get('action', 'forward'))[:16], 'rate': float(rule.get('rate', 0.0))}
    blob = json.dumps(r, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()


def api_unit(rules):
    """One API work unit: compile a rule set and check pairwise priority conflicts."""
    digests = [compile_rule(r) for r in rules]
    conflicts = 0
    for i in range(len(rules)):
        ri = rules[i]
        for j in range(i + 1, len(rules)):
            rj = rules[j]
            if ri.get('dst') == rj.get('dst') and ri.get('prio') == rj.get('prio'):
                conflicts += 1
    return hashlib.sha256(''.join(digests).encode()).hexdigest(), conflicts


def analytics_unit(window: np.ndarray):
    """One analytics work unit: rolling statistics, z-scores and a correlation-based anomaly score."""
    mu = window.mean(axis=0)
    sd = window.std(axis=0) + 1e-9
    z = (window[-1] - mu) / sd
    q = np.percentile(window, [5, 50, 95], axis=0)
    c = np.corrcoef(window, rowvar=False)
    ew = window[0].copy()
    for row in window[1:]:
        ew = 0.9 * ew + 0.1 * row
    score = float(np.nanmax(np.abs(z)) + 0.1 * np.nanmean(np.abs(c)) + 0.01 * float(np.mean(q[2] - q[0])))
    return score, ew


def telemetry_unit(record: dict, stats: dict):
    """Validate, normalise and aggregate one telemetry record (Welford running stats)."""
    dev = str(record.get('dev', 'x'))
    vals = record.get('m', [])
    s = stats.setdefault(dev, {'n': 0, 'mean': 0.0, 'm2': 0.0})
    for v in vals:
        v = float(v)
        s['n'] += 1
        d = v - s['mean']
        s['mean'] += d / s['n']
        s['m2'] += d * (v - s['mean'])
    return zlib.crc32(json.dumps(record, sort_keys=True).encode())


def calibrate_unit(fn, make_arg, target_ms, max_iter=2000):
    """Return how many calls of fn(make_arg()) take about target_ms (>=1)."""
    arg = make_arg()
    n, t0 = 0, time.perf_counter()
    while time.perf_counter() - t0 < 0.3 and n < max_iter:
        fn(arg)
        n += 1
    per_call_ms = 1000 * (time.perf_counter() - t0) / max(n, 1)
    return max(1, int(round(target_ms / max(per_call_ms, 1e-6)))), per_call_ms


def random_rules(n, rng=random):
    return [{'prio': rng.randint(1, 8), 'src': f'10.{rng.randint(0, 3)}.{rng.randint(0, 255)}.0/24',
             'dst': f'10.0.0.{rng.randint(100, 110)}', 'dport': rng.choice([80, 443, 8000, 8001, 8002]),
             'action': rng.choice(['forward', 'meter', 'mirror']), 'rate': rng.random() * 100} for _ in range(n)]


class Knob:
    """Thread-safe fault multiplier (>= 0.1)."""

    def __init__(self, service, value=1.0):
        self._v = value
        self._lock = threading.Lock()
        self._service = service
        KNOB.labels(service).set(value)

    def get(self):
        with self._lock:
            return self._v

    def set(self, v):
        with self._lock:
            self._v = min(200.0, max(0.1, float(v)))
            KNOB.labels(self._service).set(self._v)
            return self._v


# ----------------------------------------------------------------------------
# out-of-band control channel
# ----------------------------------------------------------------------------
def start_control_server(port, knob):
    """Serve the fault knob and the metrics on their own port and thread (POST /knob?mult=x, GET /knob, GET /metrics).

    The experiment driver must be able to change or reset a knob even when the
    service itself is saturated. In the first version the knob shared the
    service's worker pool, so a reset could queue behind slow requests, time
    out, and leave the fault active after the episode ended.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    class H(BaseHTTPRequestHandler):
        def _reply(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_POST(self):
            u = urlparse(self.path)
            if u.path == '/knob':
                v = parse_qs(u.query).get('mult', ['1'])[0]
                self._reply({'mult': knob.set(v)})
            else:
                self._reply({'error': 'unknown'}, 404)

        def do_GET(self):
            if self.path.startswith('/metrics'):
                # Metrics are served here too, so Prometheus can scrape a saturated service.
                # On the main port the scrape waited behind slow requests, timed out, and the
                # API latency became NaN exactly at the peak of API faults.
                from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
                b = generate_latest()
                self.send_response(200)
                self.send_header('Content-Type', CONTENT_TYPE_LATEST)
                self.send_header('Content-Length', str(len(b)))
                self.end_headers()
                self.wfile.write(b)
                return
            self._reply({'mult': knob.get()})

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(('0.0.0.0', port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
