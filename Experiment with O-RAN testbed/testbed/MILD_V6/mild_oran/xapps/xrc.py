#!/usr/bin/env python3
"""xrc.py -- RAN-Control / policy API xApp (API intent): the WRITING-RULES-AND-ACTIONS function of the loop.

Realises the API intent (I_api): "the 95th-percentile latency of policy and control requests stays below
the target (ms)" (KPI: api_latency).

Northbound HTTP API (data port)
  POST /policy    A1-like slice policies from the operator and the two tenants (load generator):
                  {"tenant": "A", "sd": 1, "min_prb": 10, "max_prb": 80, "prio": 2, "ttl_s": 40,
                   "rules": [{"scope": "ue1", "param": "max_prb", "value": 70}, ...]}   (median 25 rules)
  POST /control   re-optimisation requests from the analytics xApp xSLA, e.g.
                  {"ue": "ue2", "f1ap": 1, "sd": 2, "risk": 0.91, "reason": "sla_risk"}
Processing of every request, in a pool of api_workers workers (deadline api_deadline_s):
  1. parse and validate every rule (scope known, parameter known, value in range);
  2. conflict check against the active policy table (two policies conflict when their PRB ranges for the
     same slice do not overlap, or when the guaranteed minima of the two slices add up to more than 100 %)
     and resolution by priority, then validity;
  3. commit: the table holds, per slice, the active policies (they expire after ttl_s); the effective
     quota of a slice is min = max(active minima) (capped so that the minima of both slices sum <= 80 %),
     max = min(active maxima) (>= min + 10), clipped to the safety bands rc_min_band / rc_max_band
     (the RAN can never be starved by a policy);
  4. actuation over E2 (real): when a slice's effective quota moved by >= rc_min_change_pct and the slice
     was not actuated in the last rc_min_interval_s, xRC sends one E2SM-RC CONTROL per UE of that slice
     through its own FlexRIC session (mild_e2gw role rc): RIC control style 2 "Radio Resource Allocation
     Control", action 6 "Slice-level PRB quota", UE = gNB-DU UE ID (F1AP), RRM policy member = PLMN +
     S-NSSAI of the slice, Min/Max/Dedicated PRB Policy Ratio. The gNB answers RIC CONTROL ACKNOWLEDGE or
     FAILURE. A /control request (from xSLA) is answered only after the ACK, so its latency includes the
     E2 round trip; a /policy request is answered after the commit (actuation continues asynchronously).
     Example: xSLA reports ue2 (slice SD 2) at risk 0.91 -> boost = 10 + 30 * (0.91 - 0.6) / 0.4 = 33 ->
     xRC commits SD 2: min 33 %, and SD 1: max 67 % (15-s validity) -> the effective quotas move by >= 10
     -> three E2SM-RC controls: one for ue2 (SD 2: min 33, max 100) and one each for ue1 and ue3 (SD 1:
     max 67). Each one is a RIC CONTROL REQUEST answered by RIC CONTROL ACKNOWLEDGE (or FAILURE).
  Actuation mode: 'e2rc' when the gNB offers E2SM-RC control style 2 action 6 (preflight checks it),
  otherwise 'shadow': steps 1-3 run and the control that WOULD be sent is logged, nothing reaches the RAN.

Latency = response time - client send time (header X-Send-Mono, same monotonic clock), so queueing in
front of the server counts; requests that waited longer than the deadline are answered 504 and their full
waiting time is recorded. KPI: p95 over 8 s (computed by the driver from the samples on the control port).
Fault knob: work = multiplier of the per-request CPU time (policy compilation / conflict-check overload).
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mild_oran.xapps.common import (CtlServer, E2Link, Knobs, LatRing, RateMeter, burn, common_args, keep_alive,
                                    load_cfg, log, mono, proc_rss_kb, stop_on_signal)

NAME = 'xrc'
PARAMS = {'min_prb': (0, 100), 'max_prb': (0, 100), 'prio': (1, 5), 'ded_prb': (0, 100), 'gbr_mbps': (0, 1000)}


class XRC:
    def __init__(self, a, cfg):
        self.cfg = cfg
        self.a = a
        self.knobs = Knobs(work=1.0)
        self.sem = threading.Semaphore(int(cfg.get('api_workers', 2)))
        self.lat = LatRing()
        self.cnt = {'policy': 0, 'control': 0, 'shed': 0, 'bad': 0, 'conflicts': 0, 'rules': 0, 'rc_sent': 0,
                    'rc_ack': 0, 'rc_fail': 0, 'rc_timeout': 0, 'rc_shadow': 0, 'gw_exits': 0, 'rc_skipped_stuck': 0}
        self.clock = threading.Lock()
        self.tlock = threading.Lock()
        self.table = defaultdict(list)      # sd -> [(min, max, prio, expires, origin)]
        self.applied = {}                   # sd -> (min, max, t)
        self.inflight = 0
        self.rate = RateMeter(10.0)
        self.rc_lat = deque(maxlen=200)
        self.act_q = queue.Queue(maxsize=200)
        self.waiters = {}
        self.wlock = threading.Lock()
        self.seq = 0
        self.ue_map = {}
        self.ue_map_mtime = 0.0
        self.mode = 'starting'
        # set when the E2 node did not answer a control: no more controls from this process (each one would wait 6 s
        # and pile up); the collector sees rc_stuck in /stats and restarts the RAN (a fresh xRC starts unstuck)
        self.rc_stuck = None
        self.e2 = E2Link(NAME, 'rc', cfg, a.runtime, self.on_e2, styles='')

    # ------------------------------------------------------------------ E2 events
    def on_e2(self, ev):
        e = ev.get('ev')
        if e == 'ready':
            want = str(self.cfg.get('actuation', 'auto'))
            if want == 'shadow':
                self.mode = 'shadow'
            elif ev.get('rc'):
                self.mode = 'e2rc'
            else:
                self.mode = 'shadow'
                log(NAME, 'the E2 node offers no E2SM-RC slice-level PRB quota: actuation mode = shadow')
            log(NAME, f"E2 ready: node {ev.get('node')}, RC {ev.get('rc')}, actuation {self.mode}")
        elif e == 'rc_action':
            log(NAME, f"E2SM-RC control offered: style {ev.get('style')} action {ev.get('action')} {ev.get('name')}")
        elif e == 'rc_params':
            log(NAME, f"E2SM-RC slice-level PRB quota parameter IDs ({ev.get('source')}): {ev.get('ids')}")
        elif e == 'e2_timeout':
            if self.mode == 'e2rc' and self.rc_stuck is None:
                self.rc_stuck = {'what': ev.get('what'), 'ms': ev.get('ms'), 'time': time.strftime('%H:%M:%S')}
                log(NAME, f"the gNB did not answer an E2SM-RC {ev.get('what')} within {ev.get('ms')} ms: no more controls "
                          'from this xRC (the collector restarts the RAN)')
        elif e == 'ctrl':
            with self.wlock:
                w = self.waiters.pop(int(ev.get('id', -1)), None)
            if w is not None:
                w[1].update(ev)
                w[0].set()
        elif e == 'gw_exit':
            with self.clock:
                self.cnt['gw_exits'] += 1
            with self.wlock:                       # a control in flight dies with the gateway
                ws, self.waiters = self.waiters, {}
            for ev_, res in ws.values():
                res.update(ok=0, ack='timeout' if ws and self.rc_stuck else 'fail', why='gateway exited')
                ev_.set()

    def _ue_map(self):
        p = os.path.join(self.a.runtime, 'ue_map.json')
        try:
            m = os.path.getmtime(p)
            if m != self.ue_map_mtime:
                with open(p) as f:
                    self.ue_map = json.load(f)
                self.ue_map_mtime = m
        except Exception:
            pass
        return self.ue_map

    def _sd_of(self, f1ap):
        if f1ap is None:
            return None
        v = self._ue_map().get('f1ap', {}).get(str(f1ap))
        return int(v['sd']) if v and v.get('sd') is not None else None

    def _ues_of_slice(self, sd):
        m = self._ue_map().get('f1ap', {})
        return sorted(int(f) for f, v in m.items() if int(v.get('sd', -1)) == int(sd))

    # ------------------------------------------------------------------ actuation (one thread, serialised)
    def _rc(self, f1ap, sd, mn, mx, timeout):
        with self.wlock:
            self.seq += 1
            sid = self.seq
            ev, res = threading.Event(), {}
            self.waiters[sid] = (ev, res)
        t0 = mono()
        sst = int(self.cfg.get('_slice_sst', 1))
        if not self.e2.send(f'ctrl {sid} {int(f1ap)} {sst} {int(sd)} {int(mn)} {int(mx)} 0'):
            with self.wlock:
                self.waiters.pop(sid, None)
            return False, 'gateway down', 0.0
        if not ev.wait(timeout):
            with self.wlock:
                self.waiters.pop(sid, None)
            with self.clock:
                self.cnt['rc_timeout'] += 1
            return False, 'timeout', (mono() - t0) * 1000
        ms = (mono() - t0) * 1000
        ack = res.get('ack')
        ok = bool(res.get('ok')) and ack == 'ack'            # only a real RIC CONTROL ACKNOWLEDGE counts
        with self.clock:
            self.cnt['rc_ack' if ok else ('rc_timeout' if ack == 'timeout' else 'rc_fail')] += 1
        self.rc_lat.append(ms)
        return ok, res.get('ack'), ms

    def _actuator(self):
        while True:
            sd, mn, mx, done = self.act_q.get()
            ues = self._ues_of_slice(sd)
            results = []
            for f in ues:
                with self.clock:
                    self.cnt['rc_sent'] += 1
                if self.mode == 'e2rc' and self.rc_stuck:
                    with self.clock:
                        self.cnt['rc_skipped_stuck'] += 1
                    results.append((False, 'stuck', 0.0))
                elif self.mode == 'e2rc':
                    results.append(self._rc(f, sd, mn, mx, float(self.cfg.get('rc_timeout_s', 8.0))))
                else:
                    with self.clock:
                        self.cnt['rc_shadow'] += 1
                    results.append((True, 'shadow', 0.0))
            if done is not None:
                done[1]['rc'] = [{'ok': r[0], 'ack': r[1], 'ms': round(r[2], 1)} for r in results]
                done[0].set()

    # ------------------------------------------------------------------ policy engine
    def _validate(self, d):
        rules, bad = [], 0
        for r in d.get('rules', []) or []:
            p = r.get('param')
            lo_hi = PARAMS.get(p)
            try:
                v = float(r.get('value'))
                if lo_hi and lo_hi[0] <= v <= lo_hi[1] and str(r.get('scope', '')).startswith(('ue', 'sd', 'cell')):
                    rules.append((r['scope'], p, v))
                else:
                    bad += 1
            except Exception:
                bad += 1
        return rules, bad

    def _effective(self, now):
        eff = {}
        for sd, L in self.table.items():
            act = [x for x in L if x[3] > now]
            self.table[sd] = act
            if act:
                mn = max(x[0] for x in act)
                mx = min(x[1] for x in act)
                eff[sd] = [mn, mx]
        if len(eff) >= 2:                              # guaranteed minima of both slices <= 80 %
            tot = sum(v[0] for v in eff.values())
            if tot > 80:
                for v in eff.values():
                    v[0] = int(v[0] * 80 / tot)
        lo_b, hi_b = self.cfg.get('rc_min_band', [0, 40]), self.cfg.get('rc_max_band', [60, 100])
        for v in eff.values():
            v[0] = int(min(max(v[0], lo_b[0]), lo_b[1]))
            v[1] = int(min(max(v[1], hi_b[0], v[0] + 10), hi_b[1]))
        return eff

    def _commit(self, sd, mn, mx, prio, ttl, origin):
        now = mono()
        n_conf = 0
        with self.tlock:
            for (mn2, mx2, pr2, exp2, _o) in self.table[sd]:
                if exp2 > now and (mn > mx2 or mn2 > mx):
                    n_conf += 1
            for sd2, L in self.table.items():
                if sd2 != sd and any(x[3] > now and x[0] + mn > 100 for x in L):
                    n_conf += 1
            self.table[sd].append((mn, mx, prio, now + ttl, origin))
            self.table[sd] = self.table[sd][-64:]
            eff = self._effective(now)
            todo = []
            for s, (emn, emx) in eff.items():
                last = self.applied.get(s)
                ch = float(self.cfg.get('rc_min_change_pct', 10))
                if last is None or abs(last[0] - emn) >= ch or abs(last[1] - emx) >= ch:
                    if last is None or now - last[2] >= float(self.cfg.get('rc_min_interval_s', 5.0)):
                        self.applied[s] = (emn, emx, now)
                        todo.append((s, emn, emx))
        return n_conf, todo

    def handle(self, kind, body, send_mono):
        deadline = float(self.cfg.get('api_deadline_s', 2.0))
        waited = mono() - send_mono
        got = self.sem.acquire(timeout=max(0.0, deadline - waited))
        if not got or mono() - send_mono > deadline:
            if got:
                self.sem.release()
            lat = (mono() - send_mono) * 1000.0
            self.lat.add(mono(), lat, shed=True)
            with self.clock:
                self.cnt['shed'] += 1
            return 504, {'error': 'deadline exceeded', 'latency_ms': round(lat, 1)}
        done = None
        n_conf, rules, bad = 0, [], 0
        try:
            try:
                d = json.loads(body)
            except Exception:
                d = {}
                with self.clock:
                    self.cnt['bad'] += 1
            rules, bad = self._validate(d)
            if kind == 'policy':
                sd = int(d.get('sd', 1))
                mn, mx = int(d.get('min_prb', 0)), int(d.get('max_prb', 100))
                n_conf, todo = self._commit(sd, mn, mx, int(d.get('prio', 1)), float(d.get('ttl_s', 30)), d.get('tenant'))
            else:                                       # /control from xSLA: protect the UE's slice
                sd = int(d.get('sd') or self._sd_of(d.get('f1ap')) or 1)
                risk = float(d.get('risk', 0.7))
                boost = int(10 + 30 * max(0.0, min(1.0, (risk - 0.6) / 0.4)))
                n_conf, todo = self._commit(sd, boost, 100, 5, 15.0, 'xsla')
                for sd2 in list(self.table.keys()):
                    if sd2 != sd:
                        c2, t2 = self._commit(sd2, 0, 100 - boost, 5, 15.0, 'xsla')
                        n_conf += c2
                        todo += t2
            size = max(0.3, (len(rules) + 1) / float(self.cfg.get('api_rules_median', 25)))
            burn(float(self.cfg.get('api_base_ms', 20.0)) * size ** 0.5 * self.knobs.get('work'))
            if todo:
                done = (threading.Event(), {}) if kind == 'control' else None
                for i, (s, emn, emx) in enumerate(todo):
                    try:
                        self.act_q.put_nowait((s, emn, emx, done if i == len(todo) - 1 else None))
                    except queue.Full:
                        done = None
        finally:
            self.sem.release()
        rc = None
        if done is not None:                          # /control: answer after the E2 ACK (or the timeout)
            done[0].wait(float(self.cfg.get('rc_timeout_s', 8.0)) * 3)
            rc = done[1].get('rc')
        lat = (mono() - send_mono) * 1000.0
        self.lat.add(mono(), lat)
        self.rate.hit()
        with self.clock:
            self.cnt[kind] += 1
            self.cnt['rules'] += len(rules)
            self.cnt['conflicts'] += n_conf
            self.cnt['bad'] += bad if kind == 'policy' else 0
        return 200, {'accepted': len(rules), 'conflicts': n_conf, 'latency_ms': round(lat, 1), 'rc': rc}

    # ------------------------------------------------------------------ HTTP
    def _http(self, port):
        srv = self

        class H(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *a):
                pass

            def do_POST(self):
                t_arr = mono()
                path = self.path.split('?')[0]
                n = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(n) if n else b''
                try:
                    send = float(self.headers.get('X-Send-Mono') or t_arr)
                    if not (t_arr - 60 < send <= t_arr + 0.01):
                        send = t_arr
                except ValueError:
                    send = t_arr
                if path not in ('/policy', '/control'):
                    code, obj = 404, {'error': 'not found'}
                else:
                    with srv.clock:
                        srv.inflight += 1
                    try:
                        code, obj = srv.handle(path[1:], body, send)
                    finally:
                        with srv.clock:
                            srv.inflight -= 1
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        h = ThreadingHTTPServer(('127.0.0.1', port), H)
        h.daemon_threads = True
        h.request_queue_size = 256
        threading.Thread(target=h.serve_forever, daemon=True).start()

    def stats(self, q):
        since = int(q.get('since', 0))
        samples, last = self.lat.since(since)
        with self.clock:
            c = dict(self.cnt)
            infl = self.inflight
        rl = sorted(self.rc_lat)
        with self.tlock:
            eff = self._effective(mono())
        return {**c, 'inflight': infl, 'rate': self.rate.rate(), 'seq': last, 'samples': samples,
                'rss_kb': proc_rss_kb(), 'mode': self.mode, 'rc_stuck': self.rc_stuck, 'effective_quota': {str(k): v for k, v in eff.items()},
                'rc_ms_p50': rl[len(rl) // 2] if rl else None, 'rc_ms_p95': rl[int(0.95 * (len(rl) - 1))] if rl else None,
                'e2': self.e2.status()}

    def rotate(self, q=None, body=None):
        threading.Thread(target=self.e2.rotate, daemon=True).start()
        return {'ok': 1}

    def start(self, a):
        CtlServer(NAME, a.ctl_port or a.port + 1000, self.knobs, self.stats, routes={('POST', '/rotate'): self.rotate})
        self._http(a.port)
        threading.Thread(target=self._actuator, daemon=True).start()
        self.e2.start()
        stop_on_signal(self.e2)
        log(NAME, f'up: API :{a.port}, ctl :{a.ctl_port or a.port + 1000}')


def main():
    ap = common_args(argparse.ArgumentParser(description=__doc__))
    a = ap.parse_args()
    x = XRC(a, load_cfg(a.cfg))
    x.start(a)
    keep_alive()


if __name__ == '__main__':
    main()
