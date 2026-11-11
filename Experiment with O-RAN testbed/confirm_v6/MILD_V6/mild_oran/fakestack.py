"""fakestack.py -- deterministic, process-free stand-in for the O-RAN testbed (`mild-oran selftest`).

It answers apply() and read_kpis() with a simple physical model of the knobs:
a queueing control API, a capacity-limited inference worker, a fluid KPM backlog,
a one-cell radio queue that turns DL overload into RTT and loss, and probe scores
that fall with delay and loss. The model is NOT used to make data for the paper.
It exercises calibration, collection, the QA gates, the learnability check and the
training hand-off end to end in a few minutes, without root, RAN or xApp processes.
"""
from __future__ import annotations

import math
import random
import time

from .kpi import probe_scores


class FakeStack:
    def __init__(self, cfg, run_dir):
        self.cfg, self.run_dir = cfg, run_dir
        self.k = {'api': 1, 'analytics': 1, 'telemetry': 1, 'burst': 1, 'cell_load': 1, 'delay_ms': 0, 'loss_pct': 0}
        self.q = 0.0
        self.radio_q = 0.0
        self.t_last = time.monotonic()
        self.t0 = time.monotonic()
        self.rng = random.Random(3)
        self.capacity = 26.0
        self.fixed = None
        self.baseline = {'rtt_ms': 35.0, 'jitter_ms': 3.0}
        self.scales = dict(cfg.get('initial_scales', {}))
        self.weights = dict(cfg['snet_sri_weights'])
        self.info = {'mode': 'FakeStack (self-test only, not for the paper)', 'n_ues': 3}
        self.procs = []
        self.sent = {}
        self.pings = []
        self.http = []
        self.seq = 0
        self.ues = ['ue1', 'ue2', 'ue3']
        self.ue_map = {'ue': {'ue1': {'f1ap': 0, 'sd': 1}, 'ue2': {'f1ap': 1, 'sd': 2}, 'ue3': {'f1ap': 2, 'sd': 1}}}
        self.fail = {}                                  # self-test fault injection of the collector itself

    def start(self):
        return self

    def apply(self, k):
        self.k = {x: k[x] for x in self.k}
        self.applies = getattr(self, 'applies', 0) + 1

    def inject(self, what):
        """Self-test only: make the collector's self-repair work. 'hang' = xSLA stops answering until restarted;
        'outage' = the RAN goes down (E2 stale, UEs lose their address); 'slow' = ticks 200-350 overrun."""
        if what == 'hang':
            self.fail['xsla'] = 'analytics_tput'
        elif what == 'outage':
            self.outage = True
        elif what == 'slow':
            self.slow_from = getattr(self, 'applies', 0) + 200

    def reset(self):
        self.apply({'api': 1, 'analytics': 1, 'telemetry': 1, 'burst': 1, 'cell_load': 1, 'delay_ms': 0, 'loss_pct': 0})

    def set_net(self, d, l):
        self.k['delay_ms'], self.k['loss_pct'] = d, l

    def set_capacity(self, c):
        self.capacity = float(c)

    def set_baseline(self, b):
        self.baseline = dict(b)

    def set_scales(self, sc):
        self.scales = dict(sc)

    def set_fixed_load(self, mbps, only_ue=None):
        self.fixed = mbps

    def map_ues(self):
        return self.ue_map

    def rotate_e2(self, wait=True):
        return True

    def restart_role(self, role):
        self.fail.pop(role, None)

    def apply_tuning(self, tun):
        pass

    def read_kpis(self):
        k, r = self.k, self.rng
        now = time.monotonic()
        # model time runs as if one tick lasted 4 s, whatever tick_s the self-test uses
        dts = (now - self.t_last) * 4.0 / max(self.cfg['tick_s'], 1e-6)
        self.t_last = now
        tm = (now - self.t0) * 4.0 / max(self.cfg['tick_s'], 1e-6)
        load = 1 + 0.4 * math.sin(2 * math.pi * tm / 5760.0)
        # radio: one FIFO with the cell capacity, 6-MB buffer
        offered = self.fixed if self.fixed is not None else self.capacity * 0.3 * load * k['cell_load']
        self.radio_q = max(0.0, min(6.0, self.radio_q + (offered - self.capacity) / 8.0 * dts))
        served = min(offered, self.capacity) if self.radio_q <= 0 else self.capacity
        qd_ms = self.radio_q * 8.0 / self.capacity * 1000.0
        rtt_true = 32 + qd_ms + k['delay_ms']
        loss_p = min(0.95, k['loss_pct'] / 100.0 + (0.3 if self.radio_q >= 5.9 else 0.0))
        # the probe window of the last 8 s at steady state (40 pings, 20 HTTP probes)
        pings, http = [], []
        for j in range(40):
            self.seq += 1
            pings.append([self.seq, now - 2.5, None if r.random() < loss_p else rtt_true + abs(r.gauss(0, 2.0))])
        for j in range(20):
            http.append([now - 0.5, 0 if (r.random() < loss_p or rtt_true > 1900) else 1, rtt_true])
        ps = probe_scores({'pings': pings, 'http': http}, now, 8.0, 2.0, self.baseline, self.weights) or {}
        # control API: radio congestion makes the analytics xApp issue extra control requests
        extra = 0.08 if qd_ms > 150 else 0.0
        rho = min(0.995, (0.13 + extra) * load * k['api'])
        api = 22 / (1 - rho) * (1 + 0.1 * r.random()) + 0.2 * k['delay_ms']
        api = min(api, 2000.0)
        cap_an = 48.0 / k['analytics']
        dem_an = 13 + 3 * load
        tput = min(cap_an, dem_an) * (1 + 0.05 * r.gauss(0, 1))
        inflow = 50 * k['burst'] * (1 + 0.05 * r.gauss(0, 1))
        cap_te = 220.0 / k['telemetry']
        bp = 4.0 if cap_an < 8 else 0.0                     # backpressure of a stalled analytics consumer
        self.q = max(0.0, self.q + (inflow - cap_te + bp) * dts)
        cpu = min(100, 18 * load + 6 * (k['api'] + k['analytics'] + k['telemetry'] - 3) + r.gauss(0, 1))
        out = {'cpu_pct': max(0, cpu), 'ram_pct': 12 + self.q / 300 + r.gauss(0, 0.3),
               'storage_pct': 4 + self.q / 900 + r.gauss(0, 0.05), 'snet': ps.get('snet'), 'sri': ps.get('sri'),
               'api_latency': api, 'analytics_tput': max(0, tput), 'telemetry_queue': self.q,
               'ran_dl_mbps': served, 'ran_ul_mbps': 0.04 * served, 'ran_prb_dl_pct': min(100.0, 100 * served / self.capacity),
               'ran_rlc_delay_ms': 2.0 + qd_ms, 'ran_n_ue': 3,
               'ue1_dl_mbps': 0.40 * served, 'ue2_dl_mbps': 0.35 * served, 'ue3_dl_mbps': 0.25 * served,
               'e2_ind_hz': 1.8, 'e2_age_s': 0.3, 'e2_extra_subs': round(k['burst'] - 1), 'e2_db_mb': 3.0,
               'rc_ctrl_s': 0.05, 'rc_ack_ms': 12.0, 'xkpm_rx_s': 3.6 * k['burst'],
               'ue_rtt_ms': ps.get('rtt'), 'ue_loss_pct': ps.get('loss'), 'ue_http_ok_pct': ps.get('http_ok'),
               'ue_sink_mbps': served, 'ue_probe_n': 3, 'radio_speed': 0.9, 'gnb_cpu_pct': 120.0,
               'ue_cpu_pct': 270.0, 'ric_cpu_pct': 3.0,
               '_ue_ips': ['10.45.0.2', '10.45.0.3', '10.45.0.4'], '_ue_loss': [ps.get('loss')] * 3,
               '_e2_connected': True}
        for kpi in self.fail.values():
            out[kpi] = None
        if getattr(self, 'outage', False):
            out.update(e2_age_s=99.0, _ue_ips=[None, None, None], snet=None, sri=None)
        sf = getattr(self, 'slow_from', None)
        if sf is not None and sf <= getattr(self, 'applies', 0) < sf + 150:
            time.sleep(1.5 * self.cfg['tick_s'])
        return out

    def ran_status(self):
        return {'e2_ok': True, 'e2_age_s': 0.3, 'e2_hz': 1.8, 'n_ue_e2': 3, 'ue_ok': True,
                'ue_ips': {'ue1': '10.45.0.2', 'ue2': '10.45.0.3', 'ue3': '10.45.0.4'},
                'probe_ok': True, 'last_reply_age_s': 0.2, 'agents_fresh': True}

    def check_alive(self):
        return []

    def component_stats(self):
        return {'xrc': {'policy': 100, 'control': 5}, 'loadgen': {'api_sent': 100, 'dash_sent': 20}}

    def stop(self):
        pass

    def manifest(self):
        return self.info


class FakeRanCtl:
    """Self-test stand-in for ranctl (no processes): always comes up."""

    def __init__(self, cfg, state_dir):
        self.cfg, self.st = cfg, state_dir
        self.restarts = 0

    def n_ues(self):
        return 3

    def netns(self):
        return ['ue1', 'ue2', 'ue3']

    def ue_slices(self):
        return {'ue1': 1, 'ue2': 2, 'ue3': 1}

    def gnb_cfg(self):
        return 'selftest'

    def status(self, quick=False):
        return {'gnb': True, 'srsue': 3, 'ric': True, 'broker': True, 'n_ues_cfg': 3}

    def healthy(self):
        return True, self.status()

    def radio_speed(self):
        return 0.9

    def rc_offered(self):
        return True

    def disable_rc(self, why):
        return None

    def down(self):
        return True, []

    def up(self):
        return True, 'selftest RAN up', {'n_ues': 3, 'steps': {}}

    def restart(self, reason, should_stop=lambda: False, t_limit=None):
        self.restarts += 1
        from . import util
        util.log(f'RAN restart (self-test stand-in) because: {reason}')
        return True, {'n_ues': 3}

    def ensure_up(self, should_stop=lambda: False):
        return True, {'reused': True}


class FakeStackV4(FakeStack):
    """Self-test / design stand-in for scenario v4 (capacity knee). The parameters are read off the real v3 traces
    of the user's testbed (healthy levels, noise, CPU plateaus, how throughput, latency and the KPM backlog react):
      * each xApp tree uses u0 x knob of one core, up to its plateau umax (xRC 0.78, xSLA 0.83, xKPM 0.90);
        cpu_pct = mean of the three trees (healthy ~23 %, sd ~2);
      * xSLA: one FIFO worker; completed jobs/s = demand x min(1, capacity ratio)  (flat, then 1/knob);
      * xRC: p95 latency ~ 45 ms / (1 - rho), rho = u0 x knob / umax, log-normal noise (healthy p50 ~60, p99 ~110);
      * xKPM: fixed-period records; backlog grows when the tree is at its plateau or xSLA stops reading (backpressure);
      * cell: PRB % = 28 x cell_load; probes see the netem delay/loss and radio queueing above ~86 % PRB.
    Not used to make data for the paper."""
    U0 = {'api': 0.20, 'analytics': 0.28, 'telemetry': 0.20}
    UMAX = {'api': 0.78, 'analytics': 0.83, 'telemetry': 0.90}

    def __init__(self, cfg, run_dir):
        super().__init__(cfg, run_dir)
        self.model_tick = 2.0

    def read_kpis(self):
        k, r = self.k, self.rng
        now = time.monotonic()
        dts = (now - self.t_last) * self.model_tick / max(self.cfg['tick_s'], 1e-6)
        self.t_last = now
        u = {x: self.U0[x] * max(1.0, k[x]) for x in self.U0}
        util = {x: min(self.UMAX[x], u[x]) * (1 + 0.05 * r.gauss(0, 1)) for x in u}
        cpu = 100.0 * sum(util.values()) / 3.0 + r.gauss(0, 1.4)
        c_an = self.UMAX['analytics'] / u['analytics']
        tput = 14.0 * min(1.0, c_an) * (1 + 0.07 * r.gauss(0, 1))
        rho = min(0.985, u['api'] / self.UMAX['api'])
        prb = min(100.0, 28.0 * k['cell_load'] * (1 + 0.06 * r.gauss(0, 1)))
        cong = max(0.0, prb - 86.0) * 6.0
        rtt_add = k['delay_ms'] + cong
        api = 45.0 / (1 - rho) * math.exp(0.25 * r.gauss(0, 1)) + 0.3 * rtt_add
        c_te = self.UMAX['telemetry'] / (u['telemetry'] * max(1.0, 0.5 + 0.5 * k['burst']))
        lam = 8.0
        grow = lam * max(0.0, 1.0 - c_te) + 3.0 * max(0.0, 1.0 - c_an)
        drain = lam * max(0.0, c_te - 1.0) if grow == 0 else 0.0
        self.q = max(0.0, self.q + (grow - drain) * dts)
        tq = self.q + abs(r.gauss(0.5, 0.3))
        loss = k['loss_pct'] + (0.0 if prb < 95 else 3.0)
        d_rtt = max(0.0, rtt_add + abs(r.gauss(0, 1.5)))
        jit = abs(r.gauss(0, 1.0)) + 0.3 * rtt_add * abs(r.gauss(0, 0.3))
        http_ok = max(0.0, 1.0 - min(0.9, (loss / 100.0) * 2.0) - (0.4 if rtt_add > 1500 else 0.0))
        ws, wr = self.weights['snet'], self.weights['sri']
        snet = 100 - (ws['loss'] * loss + ws['excess_rtt'] * d_rtt + ws['jitter'] * jit) + r.gauss(0, 0.15)
        sri = 100 - (wr['http_fail'] * (1 - http_ok) + wr['excess_rtt'] * d_rtt + wr['loss'] * loss) + r.gauss(0, 0.1)
        served = prb / 100.0 * 21.6
        out = {'cpu_pct': max(0, min(100, cpu)), 'ram_pct': 7.9 + 0.03 * r.gauss(0, 1) + self.q / 400.0,
               'storage_pct': 0.11 + self.q / 600.0 + 0.02 * abs(r.gauss(0, 1)),
               'snet': max(0.0, min(100.0, snet)), 'sri': max(0.0, min(100.0, sri)),
               'api_latency': min(api, 4000.0), 'analytics_tput': max(0.0, tput), 'telemetry_queue': tq,
               'ran_dl_mbps': served, 'ran_ul_mbps': 0.03, 'ran_prb_dl_pct': prb, 'ran_rlc_delay_ms': 1.0 + cong / 10,
               'ran_n_ue': 3, 'ue1_dl_mbps': 0.4 * served, 'ue2_dl_mbps': 0.35 * served, 'ue3_dl_mbps': 0.25 * served,
               'e2_ind_hz': 1.7 * max(1.0, k['burst']), 'e2_age_s': 0.3, 'e2_extra_subs': round(k['burst'] - 1),
               'e2_db_mb': 3.0, 'rc_ctrl_s': 0.0, 'rc_ack_ms': 12.0, 'xkpm_rx_s': 3.6 * k['burst'],
               'ue_rtt_ms': 19.0 + d_rtt, 'ue_loss_pct': loss, 'ue_http_ok_pct': 100 * http_ok, 'ue_sink_mbps': served,
               'ue_probe_n': 3, 'radio_speed': 0.9, 'gnb_cpu_pct': 120.0, 'ue_cpu_pct': 270.0, 'ric_cpu_pct': 3.0,
               '_ue_ips': ['10.45.0.2', '10.45.0.3', '10.45.0.4'], '_ue_loss': [loss] * 3, '_e2_connected': True}
        for kpi in self.fail.values():
            out[kpi] = None
        if getattr(self, 'outage', False):
            out.update(e2_age_s=99.0, _ue_ips=[None, None, None], snet=None, sri=None)
        return out
