#!/usr/bin/env python3
"""loadgen.py -- operator / tenant clients of the RIC northbound interfaces.

Two open-loop Poisson sources with a daily profile (1 + amp*sin(2*pi*t/P), times an
Ornstein-Uhlenbeck noise term), sharing the time origin of the experiment:

  policy clients   POST xRC /policy: A1-like slice policies of tenant A (slice SD 1), tenant B
                   (SD 2) and the operator: PRB range of the slice, priority, validity, and a rule
                   set whose size is log-normal (median api_rules_median, sigma 0.6).
                   Nominal rate api_rps requests/s.
  dashboards       GET xSLA /query?jobs=n with n ~ geometric(mean dash_jobs_mean).
                   Nominal rate dash_rps requests/s.

Open loop: a request is issued at its Poisson time whatever the state of the
server (up to max_inflight requests in flight; beyond that it is counted as an
overflow), so an overloaded server shows growing latency instead of a
self-throttled client.  Every request carries X-Send-Mono (monotonic send time).
"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from mild_oran.xapps.common import (CtlServer, Diurnal, Knobs, RateMeter, common_args, keep_alive, load_cfg, log, mono,
                                    proc_rss_kb)

NAME = 'loadgen'
_tl = threading.local()


def _conn(port, timeout):
    c = getattr(_tl, f'c{port}', None)
    if c is None:
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
        setattr(_tl, f'c{port}', c)
    return c


def _drop(port):
    c = getattr(_tl, f'c{port}', None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
    setattr(_tl, f'c{port}', None)


class LoadGen:
    def __init__(self, a, cfg):
        self.cfg, self.a = cfg, a
        self.knobs = Knobs(api_rate=1.0, dash_rate=1.0)
        P = float(cfg.get('day_s', 5760.0))
        self.d_api = Diurnal(a.t0, P, cfg.get('load_amp', 0.5), seed=21)
        self.d_dash = Diurnal(a.t0, P, cfg.get('load_amp', 0.5), seed=22)
        self.cnt = {'api_sent': 0, 'api_ok': 0, 'api_err': 0, 'api_overflow': 0,
                    'dash_sent': 0, 'dash_ok': 0, 'dash_err': 0, 'dash_overflow': 0}
        self.lock = threading.Lock()
        self.inflight = {'api': 0, 'dash': 0}
        mi = int(cfg.get('max_inflight', 64))
        self.pool = {'api': ThreadPoolExecutor(mi), 'dash': ThreadPoolExecutor(max(8, mi // 4))}
        self.max_inflight = {'api': mi, 'dash': max(8, mi // 4)}
        self.rng = random.Random(5)
        self.api_rate = RateMeter(10.0)

    def _policy_body(self, r):
        """One A1-like slice policy of tenant A (slice SD 1) or tenant B (SD 2), or of the operator (either).
        Healthy policies ask for PRB ranges inside the safety bands, so the effective quota rarely moves."""
        n = max(1, int(round(r.lognormvariate(math.log(float(self.cfg.get('api_rules_median', 25))), 0.6))))
        n = min(n, 400)
        tenant = r.choice(('A', 'B', 'operator'))
        sd = 1 if tenant == 'A' else (2 if tenant == 'B' else r.choice((1, 2)))
        ues = self.cfg.get('_slice_ues', {}).get(str(sd)) or ['ue1']
        rules = []
        for i in range(n):
            p = r.choice(('max_prb', 'min_prb', 'prio', 'gbr_mbps'))
            v = {'max_prb': r.choice((60, 70, 80, 90, 100)), 'min_prb': r.choice((0, 5, 10, 15, 20)),
                 'prio': r.randint(1, 3), 'gbr_mbps': r.choice((1, 2, 3, 5))}[p]
            rules.append({'scope': r.choice(ues + [f'sd{sd}']), 'param': p, 'value': v})
        return json.dumps({'tenant': tenant, 'sd': sd, 'min_prb': r.choice((0, 5, 10, 15, 20)),
                           'max_prb': r.choice((70, 80, 90, 100)), 'prio': r.randint(1, 3),
                           'ttl_s': r.choice((20, 40, 60)), 'rules': rules}).encode()

    def _api_one(self, body):
        try:
            c = _conn(self.a.xrc_port, 6)
            c.request('POST', '/policy', body=body, headers={'Content-Type': 'application/json',
                                                            'X-Send-Mono': f'{mono():.6f}'})
            r = c.getresponse()
            r.read()
            with self.lock:
                self.cnt['api_ok' if r.status in (200, 504) else 'api_err'] += 1
        except Exception:
            _drop(self.a.xrc_port)
            with self.lock:
                self.cnt['api_err'] += 1
        finally:
            with self.lock:
                self.inflight['api'] -= 1

    def _dash_one(self, n):
        try:
            c = _conn(self.a.xsla_port, 15)
            c.request('GET', f'/query?jobs={n}')
            r = c.getresponse()
            r.read()
            with self.lock:
                self.cnt['dash_ok' if r.status == 200 else 'dash_err'] += 1
        except Exception:
            _drop(self.a.xsla_port)
            with self.lock:
                self.cnt['dash_err'] += 1
        finally:
            with self.lock:
                self.inflight['dash'] -= 1

    def _source(self, kind):
        r = random.Random(31 if kind == 'api' else 32)
        base = float(self.cfg.get('api_rps' if kind == 'api' else 'dash_rps', 6.0 if kind == 'api' else 1.0))
        d = self.d_api if kind == 'api' else self.d_dash
        while True:
            rate = max(0.05, base * d.factor() * self.knobs.get(f'{kind}_rate'))
            time.sleep(r.expovariate(rate))
            with self.lock:
                if self.inflight[kind] >= self.max_inflight[kind]:
                    self.cnt[f'{kind}_overflow'] += 1
                    continue
                self.inflight[kind] += 1
                self.cnt[f'{kind}_sent'] += 1
            if kind == 'api':
                self.api_rate.hit()
                self.pool['api'].submit(self._api_one, self._policy_body(r))
            else:
                m = float(self.cfg.get('dash_jobs_mean', 3.0))
                n = 1
                while r.random() > 1.0 / m and n < 30:
                    n += 1
                self.pool['dash'].submit(self._dash_one, n)

    def stats(self, q):
        with self.lock:
            return {**self.cnt, 'inflight': dict(self.inflight), 'api_rate': self.api_rate.rate(), 'rss_kb': proc_rss_kb()}

    def start(self, a):
        CtlServer(NAME, a.ctl_port or a.port + 1000, self.knobs, self.stats)
        for k in ('api', 'dash'):
            threading.Thread(target=self._source, args=(k,), daemon=True).start()
        log(NAME, f'up: policy clients -> xRC :{a.xrc_port}, dashboards -> xSLA :{a.xsla_port}')


def main():
    ap = common_args(argparse.ArgumentParser(description=__doc__))
    ap.add_argument('--xrc_port', type=int, required=True)
    ap.add_argument('--xsla_port', type=int, required=True)
    ap.add_argument('--t0', type=float, default=0.0, help='monotonic time origin of the daily profile')
    a = ap.parse_args()
    if not a.t0:
        a.t0 = mono()
    x = LoadGen(a, load_cfg(a.cfg))
    x.start(a)
    keep_alive()


if __name__ == '__main__':
    main()
