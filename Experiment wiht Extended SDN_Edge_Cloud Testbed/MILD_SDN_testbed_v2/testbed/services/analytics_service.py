#!/usr/bin/env python3
"""
Analytics (inference and control) service of the SDN testbed.

Realises the Analytics intent. Jobs come from two sources:
  * the telemetry service, which forwards aggregated telemetry batches (POST /submit with a JSON body);
  * operator dashboards at the edge sites (POST /submit?n=k from the load generator).

Each job is real computation over a sliding window of telemetry features:
rolling statistics, z-scores, percentiles, a correlation matrix and an EWMA.
High anomaly scores trigger an actuation request to the API service, so
Analytics -> API is a real data path. When the backlog exceeds --max-backlog
the service answers 429. The telemetry forwarder then has to keep its batches,
so an Analytics fault propagates to the telemetry queue through real
backpressure.

Fault knob (/set_slowdown?mult=): multiplies the analysis window length, so
each job does proportionally more real work.
"""
import argparse
import collections
import json
import os
import queue
import threading
import time

import numpy as np
import requests
from flask import Flask, Response, jsonify, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

import common

common.limit_threads()
app = Flask(__name__)
JOBS_IN = Counter('analytics_jobs_in_total', 'Jobs accepted')
JOBS_OUT = Counter('analytics_jobs_out_total', 'Jobs processed')
JOBS_REJ = Counter('analytics_jobs_rejected_total', 'Jobs rejected because of backpressure')
BACKLOG = Gauge('analytics_backlog', 'Jobs waiting')
TPUT = Gauge('analytics_tput', 'Throughput over the last 5 s (jobs/s)')
JOB_LAT = Histogram('analytics_job_seconds', 'Processing time per job (s)',
                    buckets=(0.005, 0.01, 0.02, 0.03, 0.05, 0.08, 0.13, 0.21, 0.34, 0.55, 0.89, 1.44, 2.33))
ACT_REQ = Counter('analytics_actuation_requests_total', 'Actuation requests sent to the API', ['result'])

F = 16
CFG = {'rows': 64, 'max_backlog': 3000, 'api': '', 'score_thr': 6.0, 'store': ''}
KNOB = common.Knob('analytics')
q = queue.Queue()
hist = collections.deque(maxlen=50000)
hist_lock = threading.Lock()
act_q = queue.Queue(maxsize=50)
_done = 0
_done_lock = threading.Lock()
_rng = np.random.default_rng()


def _window(rows):
    with hist_lock:
        n = len(hist)
        if n >= rows:
            arr = np.asarray(list(hist)[-rows:], dtype=np.float64)
        elif n > 0:
            base = np.asarray(hist, dtype=np.float64)
            reps = int(np.ceil(rows / n))
            arr = np.tile(base, (reps, 1))[:rows] + _rng.normal(0, 0.01, (rows, F))
        else:
            arr = _rng.normal(0, 1, (rows, F))
    return arr


def worker():
    global _done
    while True:
        job = q.get()
        BACKLOG.set(q.qsize())
        rows = max(8, int(CFG['rows'] * KNOB.get()))
        t0 = time.perf_counter()
        score, ew = common.analytics_unit(_window(rows))
        JOB_LAT.observe(time.perf_counter() - t0)
        JOBS_OUT.inc()
        with _done_lock:
            _done += 1
        if score > CFG['score_thr'] and CFG['api']:
            try:
                act_q.put_nowait({'action': 'meter', 'target': '10.0.0.251', 'score': round(score, 3)})
            except queue.Full:
                ACT_REQ.labels('dropped').inc()
        if CFG['store'] and (_done % 20 == 0):
            try:
                with open(CFG['store'], 'a') as f:
                    f.write(json.dumps({'ts': time.time(), 'score': score, 'ewma': np.round(ew, 3).tolist()}) + '\n')
                if os.path.getsize(CFG['store']) > 16 * 1024 * 1024:
                    os.replace(CFG['store'], CFG['store'] + '.1')
            except Exception:
                pass
        q.task_done()


def actuator():
    s = requests.Session()
    while True:
        d = act_q.get()
        try:
            r = s.post(f"{CFG['api']}/actuate", json=d, timeout=2.0)
            ACT_REQ.labels('ok' if r.status_code == 200 else f'http{r.status_code}').inc()
        except Exception:
            ACT_REQ.labels('error').inc()


def scheduler(rate):
    """Continuous inference loop: periodic analysis jobs at a constant mean rate (Poisson).
    This gives the healthy throughput a known floor that does not follow the user load."""
    rng = np.random.default_rng(7)
    while True:
        time.sleep(float(rng.exponential(1.0 / rate)))
        if q.qsize() < CFG['max_backlog']:
            q.put(1)
            JOBS_IN.inc()
        else:
            JOBS_REJ.inc()


def tput_loop():
    global _done
    while True:
        time.sleep(5.0)
        with _done_lock:
            n, _done_local = _done, 0
            _done = 0
        TPUT.set(n / 5.0)
        BACKLOG.set(q.qsize())


@app.post('/submit')
def submit():
    body = request.get_json(silent=True)
    if body and 'batch' in body:
        rows = body['batch']                     # telemetry aggregates: list of F-dim vectors
        with hist_lock:
            for r in rows:
                v = (list(r) + [0.0] * F)[:F]
                hist.append(v)
        n = max(1, int(body.get('jobs', len(rows))))
    else:
        n = max(1, int(request.args.get('n', '1')))
    if q.qsize() + n > CFG['max_backlog']:
        JOBS_REJ.inc(n)
        return jsonify({'ok': False, 'error': 'backpressure', 'backlog': q.qsize()}), 429
    for _ in range(n):
        q.put(1)
        JOBS_IN.inc()
    BACKLOG.set(q.qsize())
    return jsonify({'ok': True, 'queued': n, 'backlog': q.qsize()})


@app.post('/set_slowdown')
def set_slowdown():
    return jsonify({'slowdown_mult': KNOB.set(request.args.get('mult', '1'))})


@app.get('/healthz')
def healthz():
    return jsonify({'ok': True, 'service': 'analytics'})


@app.get('/')
def root():
    return jsonify({'ok': True, 'service': 'analytics'})


@app.get('/metrics')
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8001)
    ap.add_argument('--workers', type=int, default=1)
    ap.add_argument('--base-ms', type=float, default=30.0, help='target healthy processing time per job')
    ap.add_argument('--max-backlog', type=int, default=3000)
    ap.add_argument('--api', default='', help='API base URL for actuation requests')
    ap.add_argument('--sched-rate', type=float, default=4.0, help='scheduled inference jobs per second')
    ap.add_argument('--run-dir', default='/tmp/mild_run')
    ap.add_argument('--vol', default='')
    ap.add_argument('--ctl-port', type=int, default=0, help='out-of-band knob port (default: port + 1000)')
    a = ap.parse_args()
    # calibrate rows so that one job takes about base_ms
    probe = 512
    reps, per_ms = common.calibrate_unit(common.analytics_unit, lambda: np.random.normal(0, 1, (probe, F)), a.base_ms)
    CFG['rows'] = int(max(16, min(20000, probe * a.base_ms / max(per_ms, 1e-6))))
    CFG.update({'max_backlog': a.max_backlog, 'api': a.api,
                'store': os.path.join(a.vol, 'analytics_results.jsonl') if a.vol else ''})
    common.UNIT_MS.labels('analytics').set(per_ms * CFG['rows'] / probe)
    common.write_pid('analytics', a.run_dir)
    common.start_control_server(a.ctl_port or a.port + 1000, KNOB)
    common.start_proc_metrics('analytics')
    for _ in range(max(1, a.workers)):
        threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=tput_loop, daemon=True).start()
    if a.sched_rate > 0:
        threading.Thread(target=scheduler, args=(a.sched_rate,), daemon=True).start()
    threading.Thread(target=actuator, daemon=True).start()
    print(f"[analytics] window rows per job={CFG['rows']} (~{a.base_ms} ms), workers={a.workers}", flush=True)
    from waitress import serve
    serve(app, host='0.0.0.0', port=a.port, threads=6, connection_limit=256, channel_timeout=15, ident='mild-analytics')
