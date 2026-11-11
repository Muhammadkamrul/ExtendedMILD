#!/usr/bin/env python3
"""xsla.py -- Slice-SLA Analytics xApp (Analytics intent): the ANALYTICS-AND-DECISION function of the loop.

Realises the ANALYTICS intent (I_anl): "SLA analytics keep up: inference jobs are completed at >= the
target rate (jobs/s)" (KPI: analytics_tput).

Inputs
  * per-UE and per-cell feature rows from xKPM (internal RIC stream, TCP, newline-delimited JSON);
  * its OWN E2 session (mild_e2gw, role kpm, style 1 only): the cell-level KPM of the gNB (PRB use, cell
    throughput) for the slice demand forecaster; a real xApp-level E2 subscription;
  * dashboard queries of operators and tenants (GET /query?jobs=n, from the load generator).

Jobs (one inference worker, FIFO, bounded queue sla_in_q_max)
  row job     one per UE feature row: the SLA-risk model of that UE. Features: RLC SDU delay over the
              slice's delay budget, cell PRB utilisation, throughput z-score (sudden drop), drop rate.
              risk = logistic(w . x + b) (weights fixed below). Example: UE ue2 (slice SD 2), RLC delay
              36 ms (budget 30 ms), cell PRB use 88 % (threshold 85 %), throughput z = -1.4, no drops:
              -3.2 + 2.2*1.2 + 1.6*1.035 - 0.9*(-1.4) = 2.36  ->  risk 0.91.
  cell job    one per cell indication from its own E2 subscription: appends PRB use and cell throughput to
              the cell history (60-s window).
  forecast    scheduled at anl_sched_rate per second (a floor, not a backlog: skipped when 4 are waiting):
              per slice, a least-squares trend over the last 60 s of the slice's throughput and of the cell
              PRB use, projected 30 s ahead -> projected PRB headroom of the cell.
  dashboard   per job: 95th percentile of every UE's RLC delay and throughput over the last 30 rows.

Backpressure: when the job queue is full, the row reader blocks, TCP stops draining and xKPM's delivery
queue grows: the real Analytics -> Telemetry propagation path.

Closed loop (decisions): when a UE's risk > 0.6, at most once per sla_ctrl_period_s per UE, xSLA
  (i) asks the control xApp xRC (POST /control) to protect that UE's slice: raise the slice's minimum PRB
      ratio, lower the other slice's maximum -> xRC actuates over E2SM-RC;
  (ii) asks xKPM (POST /subscription) for closer monitoring of that UE -> one more E2 KPM subscription.
RAN congestion therefore raises the load of the API and Telemetry functions: an emergent coupling.

KPI exported for MILD: jobs completed per second (all types). Fault knob: work = CPU-time multiplier per job.
"""
from __future__ import annotations

import argparse
import json
import math
import queue
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from mild_oran.xapps.common import (CtlServer, E2Link, Knobs, LineServer, RateMeter, burn, common_args, keep_alive,
                                    load_cfg, log, mono, proc_rss_kb, stop_on_signal)

NAME = 'xsla'
W = {'delay': 2.2, 'prb': 1.6, 'z': -0.9, 'drop': 0.4, 'b': -3.2}     # SLA-risk model (logistic)


class XSLA:
    def __init__(self, a, cfg):
        self.cfg = cfg
        self.a = a
        self.knobs = Knobs(work=1.0)
        self.q = queue.Queue(maxsize=int(cfg.get('sla_in_q_max', 40)))
        self.sched_pending = 0
        self.plock = threading.Lock()
        self.cnt = {'row': 0, 'cell': 0, 'sched': 0, 'dash': 0, 'sched_missed': 0, 'ctrl_sent': 0, 'ctrl_fail': 0,
                    'sub_req': 0, 'dash_timeouts': 0, 'gw_exits': 0}
        self.clock = threading.Lock()
        self.done_rate = RateMeter(10.0)
        self.ue_hist = {}                 # ue key -> deque of rows
        self.cell_hist = deque(maxlen=120)
        self.risk = {}                    # ue key -> (risk, t, sd)
        self.forecast = {}
        self.last_ctrl = {}
        self.ctrl_q = queue.Queue(maxsize=200)
        self.e2 = E2Link(NAME, 'kpm', cfg, a.runtime, self.on_e2, styles='1')

    # ------------------------------------------------------------------ inputs
    def on_row(self, line: bytes):
        try:
            row = json.loads(line)
        except ValueError:
            return
        if row.get('kind') == 'ue':
            self.q.put(('row', row, None))      # blocks when full -> TCP backpressure to xKPM

    def on_e2(self, ev):
        if ev.get('ev') == 'ind' and ev.get('fmt') == 1:
            try:
                self.q.put_nowait(('cell', ev.get('m') or {}, None))
            except queue.Full:
                pass
        elif ev.get('ev') == 'gw_exit':
            with self.clock:
                self.cnt['gw_exits'] += 1

    def _scheduler(self):
        rate = float(self.cfg.get('anl_sched_rate', 8.0))
        nxt = mono()
        k = 0
        while True:
            nxt += 1.0 / rate
            time.sleep(max(0.0, nxt - mono()))
            with self.plock:
                if self.sched_pending >= 4:
                    with self.clock:
                        self.cnt['sched_missed'] += 1
                    continue
                self.sched_pending += 1
            try:
                self.q.put_nowait(('sched', k, None))
                k += 1
            except queue.Full:
                with self.plock:
                    self.sched_pending -= 1
                with self.clock:
                    self.cnt['sched_missed'] += 1

    # ------------------------------------------------------------------ jobs
    def _row_job(self, row):
        key = row.get('ue') or f"f1ap{row.get('f1ap')}"
        h = self.ue_hist.setdefault(key, deque(maxlen=120))
        h.append(row)
        budget = float(self.cfg.get('sla_rlc_delay_ms', 30.0))
        delay = row.get('rlc_delay_ms')
        delay = float(delay) if delay is not None else 0.0
        prb = self.cell_hist[-1]['prb'] if self.cell_hist else (row.get('prb_dl_pct') or 0.0)
        x = {'delay': min(4.0, delay / budget), 'prb': (prb or 0.0) / float(self.cfg.get('sla_prb_util_hi', 85.0)),
             'z': max(-4.0, min(4.0, float(row.get('dl_z') or 0.0))), 'drop': min(5.0, float(row.get('drop_pct') or 0.0))}
        zsum = W['b'] + sum(W[k] * v for k, v in x.items())
        risk = 1.0 / (1.0 + math.exp(-zsum))
        self.risk[key] = (risk, mono(), row.get('sd'), row.get('f1ap'))
        if risk > 0.6:
            self._react(key, risk, row)
        burn(float(self.cfg.get('anl_row_ms', 15.0)) * self.knobs.get('work'))

    def _cell_job(self, m):
        self.cell_hist.append({'t': mono(), 'prb': m.get('RRU.PrbTotDl'),
                               'dl': (m.get('DRB.UEThpDl') or 0.0) / 1000.0})
        burn(float(self.cfg.get('anl_cell_ms', 8.0)) * self.knobs.get('work'))

    @staticmethod
    def _trend(ys, ahead):
        n = len(ys)
        xs = list(range(n))
        mx, my = sum(xs) / n, sum(ys) / n
        den = sum((x - mx) ** 2 for x in xs) or 1.0
        b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
        return my + b * (n - 1 + ahead - mx)

    def _sched_job(self, k):
        sds = sorted({r.get('sd') for h in self.ue_hist.values() for r in list(h)[-1:]} - {None}) or [0]
        sd = sds[k % len(sds)]
        rows = [r for h in self.ue_hist.values() for r in list(h)[-60:] if r.get('sd') == sd or sd == 0]
        if len(rows) >= 3:
            dl = self._trend([r['dl_mbps'] for r in rows[-60:]], 30)
            prbs = [c['prb'] for c in list(self.cell_hist)[-60:] if c['prb'] is not None]
            prb = self._trend(prbs, 30) if len(prbs) >= 3 else None
            self.forecast[sd] = {'dl_mbps_30s': round(dl, 3), 'prb_pct_30s': None if prb is None else round(prb, 1)}
        burn(float(self.cfg.get('anl_sched_ms', 25.0)) * self.knobs.get('work'))

    def _dash_job(self):
        out = {}
        for key, h in self.ue_hist.items():
            d = sorted((r.get('rlc_delay_ms') or 0.0) for r in list(h)[-30:])
            if d:
                out[key] = d[int(0.95 * (len(d) - 1))]
        burn(float(self.cfg.get('anl_dash_ms', 25.0)) * self.knobs.get('work'))
        return out

    def _worker(self):
        while True:
            kind, obj, ev = self.q.get()
            try:
                if kind == 'row':
                    self._row_job(obj)
                elif kind == 'cell':
                    self._cell_job(obj)
                elif kind == 'sched':
                    with self.plock:
                        self.sched_pending -= 1
                    self._sched_job(obj)
                else:
                    self._dash_job()
            except Exception as e:
                log(NAME, f'job error: {e}')
            with self.clock:
                self.cnt[kind] += 1
            self.done_rate.hit()
            if ev is not None:
                ev.set()

    # ------------------------------------------------------------------ closed loop
    def _react(self, key, risk, row):
        now = mono()
        if now - self.last_ctrl.get(key, -1e9) < float(self.cfg.get('sla_ctrl_period_s', 5.0)):
            return
        self.last_ctrl[key] = now
        body = {'ue': row.get('ue'), 'f1ap': row.get('f1ap'), 'sd': row.get('sd'), 'risk': round(risk, 3),
                'reason': 'sla_risk', 'evidence': {k: row.get(k) for k in ('rlc_delay_ms', 'dl_mbps', 'dl_z', 'prb_dl_pct')}}
        for item in (('ctrl', body), ('sub', {'ue': key, 'ttl_s': float(self.cfg.get('sla_sub_ttl_s', 10.0))})):
            try:
                self.ctrl_q.put_nowait(item)
            except queue.Full:
                pass

    def _ctrl_sender(self):
        import http.client
        conn = None
        while True:
            kind, body = self.ctrl_q.get()
            b = json.dumps(body).encode()
            if kind == 'sub':
                try:
                    c = http.client.HTTPConnection('127.0.0.1', self.a.xkpm_ctl, timeout=1.5)
                    c.request('POST', '/subscription', body=b)
                    c.getresponse().read()
                    c.close()
                    with self.clock:
                        self.cnt['sub_req'] += 1
                except Exception:
                    pass
                continue
            try:
                if conn is None:
                    conn = http.client.HTTPConnection('127.0.0.1', self.a.xrc_port, timeout=15)
                conn.request('POST', '/control', body=b, headers={'Content-Type': 'application/json',
                                                                   'X-Send-Mono': f'{mono():.6f}'})
                conn.getresponse().read()
                with self.clock:
                    self.cnt['ctrl_sent'] += 1
            except Exception:
                with self.clock:
                    self.cnt['ctrl_fail'] += 1
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None

    # ------------------------------------------------------------------ dashboard API (data port)
    def _http(self, port):
        srv = self

        class H(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urlparse(self.path)
                if u.path != '/query':
                    self.send_response(404)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                n = max(1, min(50, int(parse_qs(u.query).get('jobs', ['1'])[0])))
                evs = []
                for _ in range(n):
                    ev = threading.Event()
                    try:
                        srv.q.put(('dash', None, ev), timeout=5)
                        evs.append(ev)
                    except queue.Full:
                        break
                ok = all(ev.wait(10) for ev in evs)
                if not ok:
                    with srv.clock:
                        srv.cnt['dash_timeouts'] += 1
                b = json.dumps({'jobs': len(evs), 'ok': ok}).encode()
                self.send_response(200 if ok else 504)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        h = ThreadingHTTPServer(('127.0.0.1', port), H)
        h.daemon_threads = True
        threading.Thread(target=h.serve_forever, daemon=True).start()

    def stats(self, q):
        with self.clock:
            c = dict(self.cnt)
        now = mono()
        risk = {k: round(v[0], 3) for k, v in self.risk.items() if now - v[1] < 10}
        return {'jobs_done': c['row'] + c['cell'] + c['sched'] + c['dash'], **c, 'queue': self.q.qsize(),
                'done_rate': self.done_rate.rate(), 'risk': risk, 'forecast': self.forecast,
                'rss_kb': proc_rss_kb(), 'e2': self.e2.status()}

    def rotate(self, q=None, body=None):
        threading.Thread(target=self.e2.rotate, daemon=True).start()
        return {'ok': 1}

    def start(self, a):
        CtlServer(NAME, a.ctl_port or a.port + 1000, self.knobs, self.stats, routes={('POST', '/rotate'): self.rotate})
        LineServer('xsla-ingest', a.ingest_port, self.on_row)
        self._http(a.port)
        for fn in (self._worker, self._scheduler, self._ctrl_sender):
            threading.Thread(target=fn, daemon=True).start()
        self.e2.start()
        stop_on_signal(self.e2)
        log(NAME, f'up: dashboard :{a.port}, ingest :{a.ingest_port}, ctl :{a.ctl_port or a.port + 1000}')


def main():
    ap = common_args(argparse.ArgumentParser(description=__doc__))
    ap.add_argument('--ingest_port', type=int, required=True)
    ap.add_argument('--xrc_port', type=int, required=True)
    ap.add_argument('--xkpm_ctl', type=int, required=True)
    a = ap.parse_args()
    x = XSLA(a, load_cfg(a.cfg))
    x.start(a)
    keep_alive()


if __name__ == '__main__':
    main()
