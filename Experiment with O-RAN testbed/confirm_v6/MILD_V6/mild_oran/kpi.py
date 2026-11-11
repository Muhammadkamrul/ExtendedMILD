"""kpi.py -- the 8 base KPIs of the MILD dataset, measured on the O-RAN testbed (v2: FlexRIC, real E2, N UEs).

Same names and roles as Table II of the paper; the O-RAN source of each one:

  cpu_pct          mean CPU use of the three xApps (xKPM, xSLA, xRC). Each xApp is a process TREE: the Python
                   xApp plus its FlexRIC E2 front end (mild_e2gw, a C process that does the E42/E2AP and
                   ASN.1 work). Each tree is normalised to the one core it may use (psutil CPU time).
  ram_pct          resident memory of the three xApp trees over their budget (3 x mem_budget_mb)
  storage_pct      xKPM data volume (write-ahead log + KPM time-series store) over storage_quota_mb.
                   FlexRIC's own SQLite copy of every indication is NOT counted (it is reset each segment
                   when the E2 front ends are rotated); it is reported as the context column e2_db_mb.
  snet             mean over the N UEs of 100 - (w_l*loss% + w_r*dRTT + w_j*djitter): ICMP probes UE -> UPF
                   over the 5G user plane (one ue_agent in every UE namespace ue1..ueN), 8-s window
  sri              mean over the N UEs of 100 - (100*(1 - r_HTTP) + w_r*dRTT + w_l*loss%): HTTP probes
                   UE -> edge app 10.45.0.1:18080 (success ratio of the last 20 probes), same window
  api_latency      p95 latency (ms) of xRC policy/control requests completed in the last 8 s (client send
                   -> response; includes the E2SM-RC control round trip to the gNB for control requests)
  analytics_tput   xSLA analytics jobs completed per second over the last 8 s
  telemetry_queue  xKPM ingestion backlog (E2 records waiting + in processing + rows waiting for xSLA)

dRTT and djitter are the excess over the healthy floor of THAT UE measured at calibration (the radio over
ZMQ has a non-zero RTT and jitter floor, slightly different per UE).

Context columns (measured, stored next to the KPIs, NOT MILD features):
  ran_*            cell level from E2SM-KPM style 1 (DRB.UEThpDl/Ul, RRU.PrbTotDl, DRB.RlcSduDelayDl)
  ran_n_ue         UEs in the last E2SM-KPM style-4 report; ueK_dl_mbps per UE (style 4, mapped to ueK)
  e2_*             E2 indication rate and age at xKPM, extra KPM subscriptions (storm), FlexRIC DB size
  rc_*             E2SM-RC control rate and acknowledgement time at xRC
  ue_*             probe RTT / loss / HTTP success (mean over UEs), DL rate received by the UEs (sum)
  radio_speed      simulated radio time / wall time of the ZMQ broker; gnb/ue/ric CPU
A KPI that cannot be measured in a sample is None (never a healthy default).
"""
from __future__ import annotations

import math
import os
import statistics as st
from collections import deque

from . import util
from .util import mono

KPI_NAMES = ['cpu_pct', 'ram_pct', 'storage_pct', 'snet', 'sri', 'api_latency', 'analytics_tput', 'telemetry_queue']
MAX_UES = 5
CTX_NAMES = (['ran_dl_mbps', 'ran_ul_mbps', 'ran_prb_dl_pct', 'ran_rlc_delay_ms', 'ran_n_ue']
             + [f'ue{k}_dl_mbps' for k in range(1, MAX_UES + 1)]
             + ['e2_ind_hz', 'e2_age_s', 'e2_extra_subs', 'e2_db_mb', 'rc_ctrl_s', 'rc_ack_ms', 'xkpm_rx_s',
                'ue_rtt_ms', 'ue_loss_pct', 'ue_http_ok_pct', 'ue_sink_mbps', 'ue_probe_n',
                'radio_speed', 'gnb_cpu_pct', 'ue_cpu_pct', 'ric_cpu_pct'])


def probe_scores(agent_state, now, window_s, timeout_s, base, weights):
    """snet, sri and their components from ONE UE agent's probe log."""
    if not agent_state:
        return None
    pings = agent_state.get('pings') or []
    rep, lost = [], 0
    for seq, t, rtt in pings:
        if now - window_s - timeout_s <= t <= now:
            if rtt is not None and rtt <= timeout_s * 1000.0:
                rep.append((seq, rtt))
            elif now - t > timeout_s:
                lost += 1
    n = len(rep) + lost
    http = [h for h in (agent_state.get('http') or []) if now - h[0] <= 60.0][-20:]
    if n == 0 and not http:
        return None
    loss = 100.0 * lost / n if n else 100.0
    rtts = [r for _, r in rep]
    rtt = st.median(rtts) if rtts else timeout_s * 1000.0
    jit = (sum(abs(b - a) for a, b in zip(rtts, rtts[1:])) / (len(rtts) - 1)) if len(rtts) >= 2 else 0.0
    d_rtt = max(0.0, rtt - base.get('rtt_ms', 0.0))
    d_jit = max(0.0, jit - base.get('jitter_ms', 0.0))
    r_http = (sum(h[1] for h in http) / len(http)) if http else 0.0
    ws, wr = weights['snet'], weights['sri']
    snet = 100.0 - (ws['loss'] * loss + ws['excess_rtt'] * d_rtt + ws['jitter'] * d_jit)
    sri = 100.0 - (wr['http_fail'] * (1.0 - r_http) + wr['excess_rtt'] * d_rtt + wr['loss'] * loss)
    return {'snet': max(0.0, min(100.0, snet)), 'sri': max(0.0, min(100.0, sri)), 'loss': loss, 'rtt': rtt,
            'jitter': jit, 'http_ok': 100.0 * r_http, 'n_ping': n}


def ue_base(baseline, ue):
    """Healthy probe floor of one UE (per-UE values from calibration, else the cell-wide one)."""
    b = (baseline.get('per_ue') or {}).get(ue)
    return b if b else baseline


class TreeCpu:
    """CPU (% of one core) and RSS of a process and all its children (an xApp + its E2 front end).
    Per-pid CPU-time deltas, so a child that exits (gateway rotation) never makes the sum go backwards."""

    def __init__(self):
        self.root, self.procs, self.scan, self.t, self.last = None, [], 0.0, None, {}

    def sample(self, pid):
        import psutil
        if not pid:
            self.root = None
            return None, None
        now = mono()
        if pid != self.root or now - self.scan > 10.0:
            try:
                r = psutil.Process(pid)
                self.procs = [r] + r.children(recursive=True)
            except Exception:
                self.procs = []
                return None, None
            if pid != self.root:
                self.t, self.last = None, {}
            self.root, self.scan = pid, now
        busy, rss, alive = 0.0, 0, False
        for p in list(self.procs):
            try:
                ct = p.cpu_times()
                c = ct.user + ct.system
                lc = self.last.get(p.pid)
                if lc is not None:
                    busy += max(0.0, c - lc)
                self.last[p.pid] = c
                rss += p.memory_info().rss
                alive = alive or p.pid == pid
            except Exception:
                self.scan = 0.0                          # rescan next time
        if not alive:
            return None, None
        prev, self.t = self.t, now
        if prev is None or now - prev <= 0:
            return None, rss
        return min(100.0, 100.0 * busy / (now - prev)), rss


class KpiReader:
    """Samples every source once per call (the driver calls it every sample_s seconds)."""

    def __init__(self, cfg, ports, runtime, pid_fn, weights_path, baseline, ues, radio_fn=None):
        self.cfg = cfg
        self.p = ports
        self.rt = runtime
        self.pid_fn = pid_fn                     # role -> pid of the xApp's Python process
        self.weights_path = weights_path
        self.baseline = baseline                 # {'rtt_ms', 'jitter_ms', 'per_ue': {'ue1': {...}}}
        self.ues = list(ues)                     # ['ue1', 'ue2', 'ue3']
        self.radio_fn = radio_fn                 # () -> radio speed (read every 30 s)
        self.W = float(cfg.get('rate_window_s', 8))
        self.lat = deque()
        self.lat_seq = 0
        self.jobs = deque()
        self.rc = deque()
        self.trees = {r: TreeCpu() for r in ('xkpm', 'xsla', 'xrc')}
        self.ran_proc = {}
        self.ran_scan = 0.0
        self.radio = (0.0, None)
        self.db = (0.0, None)
        self.weights = util.load_json(weights_path, cfg['snet_sri_weights'])
        self.weights_mtime = 0.0
        ric = cfg['ric']
        self.mem_budget = float(ric.get('mem_budget_mb', 384)) * 1024 * 1024
        self.quota = float(ric.get('storage_quota_mb', 256)) * 1024 * 1024
        self.ctl = lambda name: self.p[name] + self.p['ctl_offset']

    def _weights(self):
        try:
            m = os.path.getmtime(self.weights_path)
            if m != self.weights_mtime:
                self.weights = util.load_json(self.weights_path, self.weights)
                self.weights_mtime = m
        except OSError:
            pass
        return self.weights

    def _ran_cpu(self):
        """CPU of the RAN processes (context only, % of one core; srsue summed over all UEs)."""
        import psutil
        now = mono()
        if now - self.ran_scan > 30:
            self.ran_scan = now
            seen = set()
            for p in psutil.process_iter(['name']):
                n = p.info['name']
                if n in ('gnb', 'srsue', 'nearRT-RIC'):
                    seen.add(p.pid)
                    if p.pid not in self.ran_proc:
                        self.ran_proc[p.pid] = [p, n, None]
            for pid in list(self.ran_proc):
                if pid not in seen:
                    self.ran_proc.pop(pid, None)
        out = {}
        for pid, item in list(self.ran_proc.items()):
            try:
                ct = item[0].cpu_times()
                c = ct.user + ct.system
                if item[2] is not None and now - item[2][0] > 0:
                    out[item[1]] = out.get(item[1], 0.0) + 100.0 * (c - item[2][1]) / (now - item[2][0])
                item[2] = (now, c)
            except Exception:
                self.ran_proc.pop(pid, None)
        return out.get('gnb'), out.get('srsue'), out.get('nearRT-RIC')

    def sample(self):
        now = mono()
        k = {n: None for n in KPI_NAMES + CTX_NAMES}
        # ---------------- API latency + RC context (xRC)
        s = util.http_get_json('127.0.0.1', self.ctl('xrc'), f'/stats?since={self.lat_seq}', timeout=1.5)
        if s is not None:
            if s.get('seq', 0) < self.lat_seq:          # xRC restarted
                self.lat.clear()
            for seq, t_done, lat, _shed in s.get('samples', []):
                self.lat.append((t_done, lat))
            self.lat_seq = s.get('seq', self.lat_seq)
            sent = int(s.get('rc_sent', 0)) + int(s.get('rc_shadow', 0))
            if self.rc and sent < self.rc[-1][1]:
                self.rc.clear()
            self.rc.append((now, sent))
            while len(self.rc) > 2 and self.rc[1][0] <= now - 30.0:
                self.rc.popleft()
            if now - self.rc[0][0] >= 4.0:
                k['rc_ctrl_s'] = (sent - self.rc[0][1]) / (now - self.rc[0][0])
            k['rc_ack_ms'] = s.get('rc_ms_p50')
            if s.get('rc_stuck'):
                k['_rc_stuck'] = s['rc_stuck']             # the gNB did not answer an E2SM-RC control (watchdog)
        while self.lat and self.lat[0][0] < now - self.W:
            self.lat.popleft()
        if self.lat and s is not None:
            v = sorted(x[1] for x in self.lat)
            k['api_latency'] = v[min(len(v) - 1, int(math.ceil(0.95 * len(v))) - 1)]
        # ---------------- analytics throughput (xSLA)
        s = util.http_get_json('127.0.0.1', self.ctl('xsla'), '/stats', timeout=1.5)
        if s is not None:
            jd = s.get('jobs_done', 0)
            if self.jobs and jd < self.jobs[-1][1]:
                self.jobs.clear()
            self.jobs.append((now, jd))
            while len(self.jobs) > 2 and self.jobs[1][0] <= now - self.W:
                self.jobs.popleft()
            t0, j0 = self.jobs[0]
            if now - t0 >= 0.5 * self.W:
                k['analytics_tput'] = (jd - j0) / (now - t0)
        # ---------------- telemetry backlog + live RAN context over E2 (xKPM)
        s = util.http_get_json('127.0.0.1', self.ctl('xkpm'), '/stats', timeout=1.5)
        if s is not None:
            k['telemetry_queue'] = float(s.get('backlog', 0))
            k['xkpm_rx_s'] = s.get('rx_rate')
            k['e2_extra_subs'] = s.get('extra_subs')
            lv = s.get('live') or {}
            k['_e2_connected'] = bool(lv.get('connected'))
            if lv.get('age_s') is not None:
                k['e2_age_s'] = lv['age_s']
                k['e2_ind_hz'] = lv.get('ind_hz')
                if lv['age_s'] < float(self.cfg['ran'].get('e2_stale_s', 10.0)):
                    k['ran_dl_mbps'] = lv.get('dl_mbps')
                    k['ran_ul_mbps'] = lv.get('ul_mbps')
                    k['ran_prb_dl_pct'] = lv.get('prb_dl_pct')
                    k['ran_rlc_delay_ms'] = lv.get('rlc_delay_ms')
                    k['ran_n_ue'] = lv.get('n_ue')
                    for name, v in (lv.get('ue_dl_mbps') or {}).items():
                        if f'{name}_dl_mbps' in k:
                            k[f'{name}_dl_mbps'] = v
        if now - self.db[0] > 30:
            d = os.path.join(self.rt, 'flexric_db')
            self.db = (now, util.dir_size_bytes(d) / 1e6 if os.path.isdir(d) else None)
        k['e2_db_mb'] = self.db[1]
        # ---------------- resources of the three xApps (process trees)
        cpus, rss, ok_rss = [], 0, True
        for role in ('xkpm', 'xsla', 'xrc'):
            c, r = self.trees[role].sample(self.pid_fn(role))
            if c is not None:
                cpus.append(c)
            if r is None:
                ok_rss = False
            else:
                rss += r
        if len(cpus) == 3:
            k['cpu_pct'] = sum(cpus) / 3.0
        if ok_rss:
            k['ram_pct'] = 100.0 * rss / (3 * self.mem_budget)
        data = os.path.join(self.rt, 'xkpm_data')
        if os.path.isdir(data):
            k['storage_pct'] = 100.0 * util.dir_size_bytes(data) / self.quota
        # ---------------- probes over the RAN user plane, every UE
        ran = self.cfg['ran']
        W = self._weights()
        sc, ips, losses = [], [], []
        sink = 0.0
        any_sink = False
        for ue in self.ues:
            ag = util.load_json(os.path.join(self.rt, f'ue_agent_{ue}.json'))
            fresh = bool(ag) and now - float(ag.get('t_mono', 0)) < 5.0
            ips.append(ag.get('ue_ip') if fresh else None)
            ps = probe_scores(ag, now, self.W, float(ran.get('probe_timeout_s', 2.0)), ue_base(self.baseline, ue), W) \
                if fresh else None
            losses.append(ps['loss'] if ps else None)
            if ps:
                sc.append(ps)
            if fresh and ag.get('sink_mbps') is not None:
                sink += float(ag['sink_mbps'])
                any_sink = True
        if sc:
            k['snet'] = sum(x['snet'] for x in sc) / len(sc)
            k['sri'] = sum(x['sri'] for x in sc) / len(sc)
            k['ue_rtt_ms'] = sum(x['rtt'] for x in sc) / len(sc)
            k['ue_loss_pct'] = sum(x['loss'] for x in sc) / len(sc)
            k['ue_http_ok_pct'] = sum(x['http_ok'] for x in sc) / len(sc)
        k['ue_probe_n'] = len(sc)
        k['ue_sink_mbps'] = sink if any_sink else None
        k['_ue_ips'] = ips
        k['_ue_loss'] = losses
        k['gnb_cpu_pct'], k['ue_cpu_pct'], k['ric_cpu_pct'] = self._ran_cpu()
        if self.radio_fn and now - self.radio[0] > 30:
            try:
                self.radio = (now, self.radio_fn())
            except Exception:
                self.radio = (now, None)
        k['radio_speed'] = self.radio[1]
        return k
