#!/usr/bin/env python3
"""
API (actuation-interface) service of the SDN testbed.

Realises the API intent: it is the northbound interface through which policy
requests (POST /work, from users at the edge sites) and automatically derived
actions (POST /actuate, from the analytics service) are compiled and delivered
to the network. /actuate really installs a short-lived OpenFlow rule through
the Ryu northbound REST API (ofctl_rest), so the actuation path crosses the
SDN control plane.

All processing is real computation (rule compilation, hashing and conflict
checks). The fault knob (/set_load?mult=) scales the amount of compilation
work per request ("configuration bloat"). A larger knob therefore consumes
real CPU and creates real queueing delay.
"""
import argparse
import json
import os
import random
import threading
import time

import requests
from flask import Flask, Response, jsonify, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

import common

common.limit_threads()
app = Flask(__name__)
BUCKETS = (0.005, 0.01, 0.02, 0.03, 0.05, 0.08, 0.13, 0.21, 0.3, 0.34, 0.45, 0.55, 0.89, 1.44, 2.33, 3.77, 6.0)
LAT = Histogram('api_request_latency_seconds', 'API request latency (s)', ['path'], buckets=BUCKETS)
REQS = Counter('api_requests_total', 'API requests', ['path', 'code'])
INFLIGHT = Gauge('api_inflight', 'Requests currently in progress')
ACT = Counter('api_actuations_total', 'Actuations sent to the SDN controller', ['result'])
ACT_LAT = Histogram('api_actuation_latency_seconds', 'Controller round-trip of an actuation (s)', buckets=BUCKETS)
BYTES_IN = Counter('api_request_bytes_total', 'Request payload bytes received')

CFG = {'units': 1, 'controller': '', 'dpid': 4, 'audit': '', 'max_inflight': 64, 'deadline_s': 2.0}
SHED = Counter('api_deadline_shed_total', 'Requests answered 504 because their deadline had passed')
KNOB = common.Knob('api')
_sem = None
_rules_cache = common.random_rules(40)


def _work(rules, deadline=None):
    m = KNOB.get()
    n = max(1, int(round(CFG['units'] * m)))
    out = None
    for i in range(n):
        out = common.api_unit(rules)
        if deadline is not None and (i & 7) == 7 and time.time() > deadline:
            raise TimeoutError('deadline exceeded during processing')
    return out


def _guarded(path, fn):
    if not _sem.acquire(blocking=False):
        REQS.labels(path, '503').inc()
        return jsonify({'ok': False, 'error': 'overloaded'}), 503
    INFLIGHT.inc()
    t0 = time.perf_counter()
    try:
        body, code = fn()
    except Exception as exc:
        body, code = {'ok': False, 'error': str(exc)[:200]}, 500
    finally:
        INFLIGHT.dec()
        _sem.release()
    dt = time.perf_counter() - t0
    if path == '/work':
        s0 = _sent_at()
        dt = max(dt, time.time() - s0) if s0 else dt     # include server queueing time when known
    LAT.labels(path).observe(dt)
    REQS.labels(path, str(code)).inc()
    body['latency_ms'] = round(1000 * dt, 2)
    return jsonify(body), code


def _sent_at():
    try:
        return float(request.headers.get('X-Sent-At', ''))
    except ValueError:
        return None


@app.route('/work', methods=['GET', 'POST'])
def work():
    # Deadline-based load shedding: a request that already waited longer than its
    # deadline in the server queue is answered 504 at once (almost no CPU), and
    # its full waiting time is recorded in the latency histogram. Under overload
    # the p95 latency therefore rises (a visible failure) instead of becoming
    # unmeasurable, and the queue drains quickly once the fault ends.
    sent = _sent_at()
    if sent is not None and time.time() - sent > CFG['deadline_s']:
        LAT.labels('/work').observe(time.time() - sent)
        REQS.labels('/work', '504').inc()
        SHED.inc()
        return jsonify({'ok': False, 'error': 'deadline'}), 504

    def run():
        payload = request.get_data() or b''
        BYTES_IN.inc(len(payload))
        try:
            rules = json.loads(payload).get('rules') if payload else None
        except Exception:
            rules = None
        try:
            digest, conflicts = _work(rules or _rules_cache, (sent + 2 * CFG['deadline_s']) if sent else None)
        except TimeoutError:
            SHED.inc()
            return {'ok': False, 'error': 'deadline'}, 504
        return {'ok': True, 'digest': digest[:16], 'conflicts': conflicts}, 200
    return _guarded('/work', run)


@app.post('/actuate')
def actuate():
    def run():
        d = request.get_json(silent=True) or {}
        rules = [{'prio': 9, 'src': '10.0.0.250/32', 'dst': str(d.get('target', '10.0.0.251')),
                  'dport': 9, 'action': str(d.get('action', 'meter')), 'rate': float(d.get('score', 0))}]
        digest, _ = _work(rules * 8)
        res = 'skipped'
        if CFG['controller']:
            flow = {'dpid': CFG['dpid'], 'cookie': 0x4D494C44, 'priority': 9, 'idle_timeout': 20, 'hard_timeout': 40,
                    'match': {'eth_type': 2048, 'ipv4_src': '10.0.0.250', 'ipv4_dst': '10.0.0.251'}, 'actions': []}
            t0 = time.perf_counter()
            try:
                r = requests.post(f"{CFG['controller']}/stats/flowentry/add", data=json.dumps(flow), timeout=1.5)
                res = 'ok' if r.status_code == 200 else f'http{r.status_code}'
            except Exception:
                res = 'error'
            ACT_LAT.observe(time.perf_counter() - t0)
        ACT.labels(res).inc()
        if CFG['audit']:
            try:
                with open(CFG['audit'], 'a') as f:
                    f.write(json.dumps({'ts': time.time(), 'digest': digest[:16], 'result': res, **d}) + '\n')
                if os.path.getsize(CFG['audit']) > 8 * 1024 * 1024:
                    os.replace(CFG['audit'], CFG['audit'] + '.1')
            except Exception:
                pass
        return {'ok': True, 'controller': res}, 200
    return _guarded('/actuate', run)


@app.get('/healthz')
def healthz():
    return jsonify({'ok': True, 'service': 'api'})


@app.get('/')
def root():
    return jsonify({'ok': True, 'service': 'api'})


@app.post('/set_load')
def set_load():
    return jsonify({'mult': KNOB.set(request.args.get('mult', '1'))})


@app.post('/set_extra_latency')          # backward compatible knob (ms -> work multiplier)
def set_extra_latency():
    ms = float(request.args.get('ms', '0'))
    return jsonify({'mult': KNOB.set(1.0 + ms / max(1e-6, CFG['base_ms']))})


@app.get('/config')
def config():
    return jsonify({**CFG, 'mult': KNOB.get()})


@app.get('/metrics')
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--base-ms', type=float, default=20.0, help='target healthy service time per request')
    ap.add_argument('--threads', type=int, default=8)
    ap.add_argument('--controller', default='', help='Ryu REST base URL, e.g. http://10.0.0.254:8080')
    ap.add_argument('--dpid', type=int, default=4)
    ap.add_argument('--run-dir', default='/tmp/mild_run')
    ap.add_argument('--vol', default='', help='storage volume for the actuation audit log')
    ap.add_argument('--ctl-port', type=int, default=0, help='out-of-band knob port (default: port + 1000)')
    ap.add_argument('--deadline-s', type=float, default=2.0)
    a = ap.parse_args()
    random.seed(os.getpid())
    CFG['units'], per_ms = common.calibrate_unit(common.api_unit, lambda: common.random_rules(40), a.base_ms)
    CFG.update({'controller': a.controller, 'dpid': a.dpid, 'base_ms': a.base_ms,
                'audit': os.path.join(a.vol, 'api_audit.jsonl') if a.vol else ''})
    common.UNIT_MS.labels('api').set(per_ms)
    _sem = threading.BoundedSemaphore(CFG['max_inflight'])
    CFG['deadline_s'] = a.deadline_s
    common.start_control_server(a.ctl_port or a.port + 1000, KNOB)
    common.write_pid('api', a.run_dir)
    common.start_proc_metrics('api')
    print(f"[api] units/request={CFG['units']} (unit={per_ms:.3f} ms) controller={a.controller or 'none'}", flush=True)
    from waitress import serve
    serve(app, host='0.0.0.0', port=a.port, threads=a.threads, connection_limit=256, channel_timeout=15,
          ident='mild-api')
