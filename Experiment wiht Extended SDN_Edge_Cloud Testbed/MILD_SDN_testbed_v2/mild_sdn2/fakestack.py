"""
fakestack.py -- a deterministic, process-free stand-in for the testbed, used by
`pipeline.py --selftest`.

It answers apply() and read_kpis() with a simple physical model of the knobs
(queueing latency, capacity-limited throughput, a fluid telemetry queue,
probe scores that fall with delay and loss, diurnal load). The model is NOT
used to make data for the paper. It only lets the calibration, collection,
quality gate and training stages be exercised end to end in a few minutes,
including the paths where failures are observed and accepted.
"""
from __future__ import annotations

import math
import random
import time

from .kpi import KPI_NAMES


class FakeStack:
    def __init__(self, cfg, run_dir):
        self.cfg, self.run_dir = cfg, run_dir
        self.k = {'api': 1, 'analytics': 1, 'telemetry': 1, 'burst': 1, 'cross': 1, 'delay_ms': 0, 'loss_pct': 0}
        self.q = 0.0
        self.t_last = time.time()
        self.t0 = time.time()
        self.rng = random.Random(3)
        self.weights_path = run_dir + '/snet_sri_weights.json'
        self.info = {'mode': 'FakeStack (self-test only)'}
        self.procs = []

    def start(self):
        return self

    def apply(self, k):
        self.k = dict(k)

    def reset(self):
        self.apply({'api': 1, 'analytics': 1, 'telemetry': 1, 'burst': 1, 'cross': 1, 'delay_ms': 0, 'loss_pct': 0})

    def read_kpis(self):
        k, r = self.k, self.rng
        now = time.time()
        dt = (now - self.t_last) / max(self.cfg['tick_s'], 1e-6)       # in ticks
        self.t_last = now
        load = 1 + 0.4 * math.sin(2 * math.pi * (now - self.t0) / (1440 * self.cfg['tick_s'] / 20))
        rho = min(0.995, 0.22 * load * k['api'])
        api = 20 / (1 - rho) * (1 + 0.1 * r.random()) + 2 * k['delay_ms']
        cap_an = 33.0 / k['analytics']
        dem_an = 7 + 6 * load
        tput = min(cap_an, dem_an) * (1 + 0.05 * r.gauss(0, 1))
        inflow = 60 * k['burst'] * (1 + 0.05 * r.gauss(0, 1))
        cap_te = 250.0 / k['telemetry']
        self.q = max(0.0, self.q + 3.0 * (inflow - cap_te) * dt)
        snet = 100 - 0.35 * k['delay_ms'] - 1.5 * k['loss_pct'] - 0.6 * 0.2 * k['delay_ms'] - abs(r.gauss(0, 0.3))
        sri = 100 - 0.15 * k['delay_ms'] - 0.5 * k['loss_pct'] - 100 * min(1, k['loss_pct'] / 40) - abs(r.gauss(0, 0.2))
        cpu = min(100, 25 * load + 8 * (k['api'] + k['analytics'] + k['telemetry'] - 3) + r.gauss(0, 1))
        return {'cpu_pct': max(0, cpu), 'ram_pct': 30 + self.q / 200 + r.gauss(0, 0.3),
                'storage_pct': 5 + self.q / 1000 + r.gauss(0, 0.05), 'snet': max(0, snet), 'sri': max(0, sri),
                'api_latency': api, 'analytics_tput': max(0, tput), 'telemetry_queue': self.q}

    def check_alive(self):
        return []

    def stop(self):
        pass

    def manifest(self):
        return self.info

    def set_net(self, d, l):
        pass


assert set(KPI_NAMES) == {'cpu_pct', 'ram_pct', 'storage_pct', 'snet', 'sri', 'api_latency', 'analytics_tput', 'telemetry_queue'}
