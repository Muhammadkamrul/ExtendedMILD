#!/usr/bin/env python3
"""
IoT / device telemetry producer for one edge site.

* --devices devices, each reporting one record every --period seconds.
  A record has 16 metrics (temperature, load, link counters, ...) with
  per-device baselines, daily seasonality and noise. It is about 350 bytes of JSON.
* Records are batched every second and POSTed to the telemetry service
  across the WAN.
* Control endpoint (HTTP, --ctl-port): POST /burst?mult=x multiplies the
  reporting rate. The experiment driver uses it to emulate event storms,
  for example a fault that makes devices report more often.
* Control file (--ctl-file, used by the Mininet testbed): the driver writes
  the burst multiplier into a local file, which is read every 0.5 s. This
  out-of-band path does not cross the emulated WAN, so a lossy or congested
  WAN cannot delay or block the fault control.
* A summary is appended to --log every 10 s (records/s, bytes/s, success).
"""
import argparse
import json
import math
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests

F = 16


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--site', required=True)
    ap.add_argument('--url', required=True)
    ap.add_argument('--devices', type=int, default=300)
    ap.add_argument('--period', type=float, default=10.0)
    ap.add_argument('--day-s', type=float, default=1440 * 4.0)
    ap.add_argument('--ctl-port', type=int, default=9200)
    ap.add_argument('--ctl-file', default='', help='read the burst multiplier from this file (out-of-band control)')
    ap.add_argument('--seed', type=int, default=2)
    ap.add_argument('--log', required=True)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    base = [[rng.uniform(20, 80) for _ in range(F)] for _ in range(a.devices)]
    ctl = {'mult': 1.0}
    st = {'rec': 0, 'bytes': 0, 'ok': 0, 'posts': 0}
    lock = threading.Lock()
    t0 = time.time()

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            q = parse_qs(urlparse(self.path).query)
            if self.path.startswith('/burst'):
                ctl['mult'] = max(0.1, min(50.0, float(q.get('mult', ['1'])[0])))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(ctl).encode())

        def log_message(self, *args):
            pass

    threading.Thread(target=ThreadingHTTPServer(('0.0.0.0', a.ctl_port), H).serve_forever, daemon=True).start()

    def ctl_file_loop():
        while True:
            try:
                with open(a.ctl_file) as f:
                    ctl['mult'] = max(0.1, min(50.0, float(f.read().strip() or 1.0)))
            except Exception:
                pass
            time.sleep(0.5)

    if a.ctl_file:
        threading.Thread(target=ctl_file_loop, daemon=True).start()

    def logger():
        while True:
            time.sleep(10)
            with lock:
                rec = {'ts': time.time(), 'site': a.site, 'records_per_s': st['rec'] / 10, 'bytes_per_s': st['bytes'] / 10,
                       'post_success': st['ok'] / max(st['posts'], 1), 'burst_mult': ctl['mult']}
                for k in st:
                    st[k] = 0
            with open(a.log, 'a') as f:
                f.write(json.dumps(rec) + '\n')

    threading.Thread(target=logger, daemon=True).start()
    sess = requests.Session()
    carry = 0.0
    print(f'[iot {a.site}] {a.devices} devices, period {a.period}s', flush=True)
    while True:
        tick = time.time()
        carry += a.devices / a.period * ctl['mult']
        n = int(carry)
        carry -= n
        season = math.sin(2 * math.pi * (tick - t0) / a.day_s)
        recs = []
        for _ in range(n):
            d = rng.randrange(a.devices)
            recs.append({'dev': f'{a.site}-{d}', 'ts': round(tick, 3),
                         'm': [round(b * (1 + 0.1 * season) + rng.gauss(0, 1.5), 3) for b in base[d]]})
        if recs:
            body = json.dumps({'site': a.site, 'records': recs})
            ok = False
            try:
                ok = sess.post(a.url, data=body, headers={'Content-Type': 'application/json'}, timeout=3).status_code == 200
            except Exception:
                pass
            with lock:
                st['rec'] += len(recs)
                st['bytes'] += len(body)
                st['ok'] += int(ok)
                st['posts'] += 1
        time.sleep(max(0.0, 1.0 - (time.time() - tick)))


if __name__ == '__main__':
    main()
