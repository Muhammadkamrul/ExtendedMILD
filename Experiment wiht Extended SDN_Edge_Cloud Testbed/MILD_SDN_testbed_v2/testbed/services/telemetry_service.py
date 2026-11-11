#!/usr/bin/env python3
"""
Telemetry (monitoring) service of the SDN testbed.

Realises the Telemetry intent. IoT/telemetry producers at the two edge sites
POST batches of device records to /ingest across the WAN. The service:
  1. appends every received batch to an on-disk spool on the storage volume
     (this is real disk usage);
  2. validates, normalises and aggregates every record with real computation
     (running statistics per device, CRC);
  3. forwards aggregated feature rows to the analytics service. When analytics
     answers 429 (backpressure), the rows stay in the outbound buffer.

telemetry_queue = records waiting to be processed + records waiting to be
delivered to analytics. It grows when telemetry processing is slow, when
bursts arrive, or when analytics pushes back. A spool segment is deleted only
after all of its records have been delivered, so disk usage follows the
undelivered backlog.

Fault knob (/set_slowdown?mult=): multiplies the per-record validation work.
"""
import argparse
import collections
import json
import os
import queue
import random
import threading
import time

import numpy as np
import requests
from flask import Flask, Response, jsonify, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

import common

common.limit_threads()
app = Flask(__name__)
IN = Counter('telemetry_items_in_total', 'Records ingested')
OUT = Counter('telemetry_items_out_total', 'Records processed')
FWD = Counter('telemetry_items_forwarded_total', 'Records delivered to analytics')
BYTES = Counter('telemetry_bytes_in_total', 'Payload bytes ingested')
QDEPTH = Gauge('telemetry_queue', 'Records waiting for processing or delivery')
PENDING_IN = Gauge('telemetry_ingest_backlog', 'Records waiting for processing')
PENDING_OUT = Gauge('telemetry_outbound_backlog', 'Records waiting for delivery to analytics')
BP = Counter('telemetry_backpressure_total', 'Forward attempts rejected by analytics')
SPOOL = Gauge('telemetry_spool_bytes', 'Bytes in the on-disk spool')

F = 16
AGG = 20                      # records aggregated into one analytics row
CFG = {'units': 1, 'analytics': '', 'spool': ''}
KNOB = common.Knob('telemetry')
inq = queue.Queue()
out_rows = collections.deque()
out_lock = threading.Lock()
stats = {}
_seq_lock = threading.Lock()
_seq_in = 0
_seq_fwd = 0
_spool_files = collections.deque()      # (path, max_seq)


def _update_gauges():
    with out_lock:
        pend_out = sum(n for _, n, _ in out_rows)
    PENDING_IN.set(inq.qsize())
    PENDING_OUT.set(pend_out)
    QDEPTH.set(inq.qsize() + pend_out)


def _spool_write(raw: bytes, max_seq: int):
    if not CFG['spool']:
        return
    try:
        seg = os.path.join(CFG['spool'], f'seg_{int(time.time() // 30)}.jsonl')
        with open(seg, 'ab') as f:
            f.write(raw + b'\n')
        if not _spool_files or _spool_files[-1][0] != seg:
            _spool_files.append([seg, max_seq])
        else:
            _spool_files[-1][1] = max_seq
    except Exception:
        pass


def _spool_gc():
    while True:
        time.sleep(2.0)
        try:
            cur = os.path.join(CFG['spool'], f'seg_{int(time.time() // 30)}.jsonl')
            while _spool_files and _spool_files[0][0] != cur and _spool_files[0][1] <= _seq_fwd:
                p, _ = _spool_files.popleft()
                if os.path.exists(p):
                    os.remove(p)
            tot = sum(os.path.getsize(os.path.join(CFG['spool'], f)) for f in os.listdir(CFG['spool']))
            SPOOL.set(tot)
        except Exception:
            pass


def processor():
    buf, last_seq = [], 0
    while True:
        seq, rec = inq.get()
        n = max(1, int(round(CFG['units'] * KNOB.get())))
        for _ in range(n):
            common.telemetry_unit(rec, stats)
        OUT.inc()
        m = rec.get('m', [])
        buf.append((list(m) + [0.0] * F)[:F])
        last_seq = seq
        if len(buf) >= AGG:
            row = np.mean(np.asarray(buf, dtype=float), axis=0).round(4).tolist()
            with out_lock:
                out_rows.append((row, len(buf), last_seq))
            buf = []
        _update_gauges()


def forwarder():
    global _seq_fwd
    s = requests.Session()
    while True:
        with out_lock:
            batch = [out_rows[i] for i in range(min(50, len(out_rows)))]
        if not batch or not CFG['analytics']:
            if batch and not CFG['analytics']:
                with out_lock:
                    for _ in batch:
                        _, n, sq = out_rows.popleft()
                        _seq_fwd = max(_seq_fwd, sq)
                        FWD.inc(n)
            time.sleep(0.25)
            _update_gauges()
            continue
        body = {'batch': [r for r, _, _ in batch], 'jobs': len(batch)}
        try:
            r = s.post(f"{CFG['analytics']}/submit", json=body, timeout=2.0)
            ok = r.status_code == 200
        except Exception:
            ok = False
        if ok:
            with out_lock:
                for _ in batch:
                    _, n, sq = out_rows.popleft()
                    _seq_fwd = max(_seq_fwd, sq)
                    FWD.inc(n)
        else:
            BP.inc()
            time.sleep(0.5)
        _update_gauges()
        time.sleep(0.05)


def _enqueue(records, raw_len):
    global _seq_in
    with _seq_lock:
        start = _seq_in
        _seq_in += len(records)
    for i, r in enumerate(records):
        inq.put((start + i + 1, r))
    IN.inc(len(records))
    BYTES.inc(raw_len)
    return start + len(records)


@app.post('/ingest')
def ingest():
    raw = request.get_data() or b''
    body = None
    if raw:
        try:
            body = json.loads(raw)
        except Exception:
            body = None
    if body and 'records' in body:
        records = body['records']
    else:   # legacy: ?n=k synthetic records
        n = max(1, int(request.args.get('n', '10')))
        records = [{'dev': f'legacy-{random.randint(0, 99)}', 'ts': time.time(),
                    'm': [random.gauss(50, 5) for _ in range(F)]} for _ in range(n)]
        raw = json.dumps({'records': records}).encode()
    max_seq = _enqueue(records, len(raw))
    _spool_write(raw, max_seq)
    _update_gauges()
    return jsonify({'ok': True, 'ingested': len(records), 'queue': inq.qsize()})


@app.post('/set_slowdown')
def set_slowdown():
    return jsonify({'slowdown_mult': KNOB.set(request.args.get('mult', '1'))})


@app.get('/healthz')
def healthz():
    return jsonify({'ok': True, 'service': 'telemetry'})


@app.get('/')
def root():
    return jsonify({'ok': True, 'service': 'telemetry'})


@app.get('/metrics')
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8002)
    ap.add_argument('--workers', type=int, default=2)
    ap.add_argument('--base-ms', type=float, default=4.0, help='target healthy processing time per record')
    ap.add_argument('--analytics', default='', help='analytics base URL for forwarding')
    ap.add_argument('--run-dir', default='/tmp/mild_run')
    ap.add_argument('--vol', default='')
    ap.add_argument('--ctl-port', type=int, default=0, help='out-of-band knob port (default: port + 1000)')
    a = ap.parse_args()
    sample = {'dev': 'd1', 'ts': 0.0, 'm': [float(i) for i in range(F)]}
    CFG['units'], per_ms = common.calibrate_unit(lambda r: common.telemetry_unit(r, {}), lambda: sample, a.base_ms)
    CFG['analytics'] = a.analytics
    if a.vol:
        CFG['spool'] = os.path.join(a.vol, 'telemetry_spool')
        os.makedirs(CFG['spool'], exist_ok=True)
        for f in os.listdir(CFG['spool']):
            os.remove(os.path.join(CFG['spool'], f))
    common.UNIT_MS.labels('telemetry').set(per_ms)
    common.write_pid('telemetry', a.run_dir)
    common.start_control_server(a.ctl_port or a.port + 1000, KNOB)
    common.start_proc_metrics('telemetry')
    for _ in range(max(1, a.workers)):
        threading.Thread(target=processor, daemon=True).start()
    threading.Thread(target=forwarder, daemon=True).start()
    threading.Thread(target=_spool_gc, daemon=True).start()
    print(f"[telemetry] units/record={CFG['units']} (unit={per_ms:.4f} ms), workers={a.workers}", flush=True)
    from waitress import serve
    serve(app, host='0.0.0.0', port=a.port, threads=8, connection_limit=256, channel_timeout=15, ident='mild-telemetry')
