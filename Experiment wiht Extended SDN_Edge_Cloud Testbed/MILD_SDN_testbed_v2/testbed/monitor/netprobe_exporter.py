#!/usr/bin/env python3
"""
Active network probe exporter, run inside each edge-site client host.

For every cloud service it measures, from the edge site across the WAN:
  * ICMP RTT (avg), jitter (mdev) and packet loss with `ping`;
  * HTTP reachability (GET /healthz) over a rolling window of 20 probes.
It then computes the composite scores with the same formulas as the
original testbed:

  snet = 100 - (w_loss * loss% + w_rtt * excess_rtt_ms + w_jit * jitter_ms)
  sri  = 100 - (w_http * (1 - http_success) * 100/100 + w_rtt2 * excess_rtt_ms + w_loss2 * loss%)

The weights are read from a JSON file at every probe round, so the
calibration step can validate and, if needed, rescale them without a restart.
excess_rtt is measured against the lowest RTT observed so far (the empty-path
baseline).
"""
import argparse
import collections
import json
import math
import os
import re
import subprocess
import threading
import time

import requests
from prometheus_client import Gauge, start_http_server

DEFAULT_W = {'snet': {'loss': 1.5, 'excess_rtt': 0.35, 'jitter': 0.60},
             'sri': {'http_fail': 100.0, 'excess_rtt': 0.15, 'loss': 0.50}}
SNET_BY = Gauge('snet_by_service', 'Network health score (0-100)', ['site', 'service'])
SRI_BY = Gauge('sri_by_service', 'Service reachability score (0-100)', ['site', 'service'])
SNET_SITE = Gauge('snet_site', 'Worst-case network health score of a site', ['site'])
SRI_SITE = Gauge('sri_site', 'Worst-case service reachability score of a site', ['site'])
RTT = Gauge('probe_rtt_ms', 'Ping average RTT (ms)', ['site', 'service'])
JIT = Gauge('probe_jitter_ms', 'Ping mdev (ms)', ['site', 'service'])
LOSS = Gauge('probe_loss_pct', 'Ping loss (%)', ['site', 'service'])
HTTP = Gauge('probe_http_success_pct', 'Rolling HTTP success (%)', ['site', 'service'])
LOSS_RE = re.compile(r'(?P<loss>[0-9.]+)% packet loss')
RTT_RE = re.compile(r'=\s*(?P<min>[0-9.]+)/(?P<avg>[0-9.]+)/(?P<max>[0-9.]+)/(?P<mdev>[0-9.]+)\s*ms')


def clamp(x):
    return max(0.0, min(100.0, x))


def ping(target, count, interval):
    try:
        p = subprocess.run(['ping', '-n', '-q', '-c', str(count), '-i', str(interval), '-W', '1', target],
                           capture_output=True, text=True, timeout=count * interval + 3)
        out = p.stdout + p.stderr
    except Exception:
        return None, None, 100.0
    lm, rm = LOSS_RE.search(out), RTT_RE.search(out)
    loss = float(lm.group('loss')) if lm else 100.0
    return (float(rm.group('avg')) if rm else None), (float(rm.group('mdev')) if rm else None), loss


def load_weights(path):
    try:
        with open(path) as f:
            w = json.load(f)
        return {'snet': {**DEFAULT_W['snet'], **w.get('snet', {})}, 'sri': {**DEFAULT_W['sri'], **w.get('sri', {})}}
    except Exception:
        return DEFAULT_W


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--site', required=True)
    ap.add_argument('--listen-port', type=int, default=9101)
    ap.add_argument('--probe', action='append', required=True, help='service,ip,url')
    ap.add_argument('--weights', default='')
    ap.add_argument('--interval', type=float, default=1.0)
    ap.add_argument('--ping-count', type=int, default=5)
    ap.add_argument('--ping-interval', type=float, default=0.2)
    a = ap.parse_args()
    probes = [dict(zip(['service', 'ip', 'url'], s.split(',', 2))) for s in a.probe]
    hist = {p['service']: collections.deque(maxlen=20) for p in probes}
    base = {p['service']: None for p in probes}
    start_http_server(a.listen_port)
    sess = requests.Session()

    def one(p, res):
        avg, mdev, loss = ping(p['ip'], a.ping_count, a.ping_interval)
        try:
            ok = 200 <= sess.get(p['url'], timeout=1.5).status_code < 400
        except Exception:
            ok = False
        res[p['service']] = (avg, mdev, loss, ok)

    while True:
        t0 = time.time()
        W = load_weights(a.weights) if a.weights else DEFAULT_W
        res = {}
        th = [threading.Thread(target=one, args=(p, res)) for p in probes]
        [t.start() for t in th]
        [t.join() for t in th]
        sn, sr = [], []
        for p in probes:
            s = p['service']
            avg, mdev, loss, ok = res.get(s, (None, None, 100.0, False))
            hist[s].append(1 if ok else 0)
            if avg is not None:
                base[s] = avg if base[s] is None else min(base[s], avg)
            b = base[s] if base[s] is not None else 1.0
            ex = max(0.0, (avg if avg is not None else b) - b)
            jit = mdev or 0.0
            succ = sum(hist[s]) / len(hist[s])
            snet = clamp(100 - (W['snet']['loss'] * loss + W['snet']['excess_rtt'] * ex + W['snet']['jitter'] * jit))
            sri = clamp(100 - (W['sri']['http_fail'] * (1 - succ) + W['sri']['excess_rtt'] * ex + W['sri']['loss'] * loss))
            SNET_BY.labels(a.site, s).set(snet)
            SRI_BY.labels(a.site, s).set(sri)
            RTT.labels(a.site, s).set(avg if avg is not None else math.nan)
            JIT.labels(a.site, s).set(jit)
            LOSS.labels(a.site, s).set(loss)
            HTTP.labels(a.site, s).set(100 * succ)
            sn.append(snet)
            sr.append(sri)
        SNET_SITE.labels(a.site).set(min(sn))
        SRI_SITE.labels(a.site).set(min(sr))
        time.sleep(max(0.05, a.interval - (time.time() - t0)))


if __name__ == '__main__':
    main()
