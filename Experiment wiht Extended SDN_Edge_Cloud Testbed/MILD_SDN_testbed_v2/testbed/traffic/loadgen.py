#!/usr/bin/env python3
"""
Open-loop user/operator load generator for one edge site.

* API policy requests: Poisson arrivals, POST /work with a JSON rule set whose
  size is log-normal (median ~25 rules, ~3 KB).
* Analytics dashboard queries: Poisson arrivals, POST /submit?n=k with a
  geometric number of jobs k (mean 5).
* Rate profile: lambda(t) = base * (1 + A sin(2 pi t / P)) * exp(OU noise).
  P is one emulated day (day_steps * step_s seconds) and A = 0.5.
* Open loop: requests are dispatched by a thread pool at their scheduled
  times, so a slow service does not slow down the arrivals (as with real users).

Every API request carries an X-Sent-At header (client send time). The API
service uses it to shed requests whose deadline has passed and to record the
full waiting time as latency (see api_service.py).

A summary line is appended to --log every 10 s (requests sent, mean payload
bytes, success ratio, client-side p50/p95 latency). The pipeline uses these
logs to report measured traffic characteristics.
"""
import argparse
import concurrent.futures as cf
import json
import math
import os
import random
import threading
import time

import requests


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--site', required=True)
    ap.add_argument('--api-url', required=True)
    ap.add_argument('--analytics-url', required=True)
    ap.add_argument('--api-rps', type=float, default=6.0)
    ap.add_argument('--dash-rps', type=float, default=1.0)
    ap.add_argument('--dash-jobs-mean', type=float, default=5.0)
    ap.add_argument('--amp', type=float, default=0.5)
    ap.add_argument('--day-s', type=float, default=1440 * 4.0, help='seconds of one emulated day')
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--log', required=True)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    t_start = time.time()
    ou = {'x': 0.0}
    stats_lock = threading.Lock()
    st = {'api_n': 0, 'api_bytes': 0, 'api_ok': 0, 'api_lat': [], 'dash_n': 0, 'dash_jobs': 0, 'dash_ok': 0}
    pool = cf.ThreadPoolExecutor(max_workers=96)
    sess = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=96)
    sess.mount('http://', adapter)

    def factor():
        t = time.time() - t_start
        return max(0.05, (1 + a.amp * math.sin(2 * math.pi * t / a.day_s)) * math.exp(ou['x']))

    def ou_loop():
        while True:
            ou['x'] += -0.05 * ou['x'] + 0.04 * rng.gauss(0, 1)
            time.sleep(1.0)

    def api_call():
        n = max(2, int(rng.lognormvariate(math.log(25), 0.5)))
        rules = [{'prio': rng.randint(1, 8), 'src': f'10.{rng.randint(0, 3)}.{rng.randint(0, 255)}.0/24',
                  'dst': f'10.0.0.{rng.randint(100, 110)}', 'dport': rng.choice([80, 443, 8000]),
                  'action': rng.choice(['forward', 'meter', 'mirror']), 'rate': round(rng.random() * 100, 2)}
                 for _ in range(n)]
        body = json.dumps({'site': a.site, 'rules': rules})
        t0 = time.time()
        ok = False
        try:
            r = sess.post(a.api_url, data=body, timeout=5,
                          headers={'Content-Type': 'application/json', 'X-Sent-At': f'{t0:.6f}'})
            ok = r.status_code == 200
        except Exception:
            pass
        with stats_lock:
            st['api_n'] += 1
            st['api_bytes'] += len(body)
            st['api_ok'] += int(ok)
            st['api_lat'].append(time.time() - t0)

    def dash_call():
        k = 1 + int(rng.expovariate(1.0 / max(a.dash_jobs_mean - 1, 0.1)))
        ok = False
        try:
            r = sess.post(a.analytics_url.format(k), timeout=5)
            ok = r.status_code == 200
        except Exception:
            pass
        with stats_lock:
            st['dash_n'] += 1
            st['dash_jobs'] += k
            st['dash_ok'] += int(ok)

    def arrivals(rate_base, fn):
        while True:
            lam = rate_base * factor()
            time.sleep(rng.expovariate(lam))
            pool.submit(fn)

    def logger():
        while True:
            time.sleep(10.0)
            with stats_lock:
                lat = sorted(st['api_lat'])
                rec = {'ts': time.time(), 'site': a.site, 'window_s': 10, 'load_factor': round(factor(), 3),
                       'api_req_per_s': st['api_n'] / 10, 'api_mean_payload_B': st['api_bytes'] / max(st['api_n'], 1),
                       'api_success': st['api_ok'] / max(st['api_n'], 1),
                       'api_p50_ms': 1000 * lat[len(lat) // 2] if lat else None,
                       'api_p95_ms': 1000 * lat[int(0.95 * (len(lat) - 1))] if lat else None,
                       'dash_req_per_s': st['dash_n'] / 10, 'dash_jobs_per_s': st['dash_jobs'] / 10,
                       'dash_success': st['dash_ok'] / max(st['dash_n'], 1)}
                for k in st:
                    st[k] = [] if k == 'api_lat' else 0
            with open(a.log, 'a') as f:
                f.write(json.dumps(rec) + '\n')

    for fn, args in [(ou_loop, ()), (arrivals, (a.api_rps, api_call)), (arrivals, (a.dash_rps, dash_call)), (logger, ())]:
        threading.Thread(target=fn, args=args, daemon=True).start()
    print(f'[loadgen {a.site}] running', flush=True)
    while True:
        time.sleep(60)


if __name__ == '__main__':
    main()
