#!/usr/bin/env python3
"""xkpm.py -- KPM-Monitor xApp (Telemetry intent): the MONITORING function of the near-RT RIC loop.

Realises the TELEMETRY intent (I_tel): "every KPM measurement received over E2 is processed and delivered
to the analytics xApp without a growing backlog" (KPI: telemetry_queue, records; target Q_max).

E2 side (real): its own FlexRIC xApp session (mild_e2gw, role kpm) subscribes to E2SM-KPM v3 on the srsRAN
gNB DU through the FlexRIC nearRT-RIC:
  * style 1 (action format 1)  E2 node level: cell PRB use, cell RLC throughput and volumes, delays;
  * style 4 (action format 4)  UE level, one block per UE (gNB-CU UE F1AP ID): per-UE RLC throughput,
                                PRBs, RLC SDU delay, drop rate, volumes.
  Report period 1000 ms (srsRAN minimum); at 0.9x radio speed an indication arrives every ~1.1 s.

What it does with every indication (one ingest record per cell block and per UE block):
  1. write-ahead log: the raw record is appended to a WAL segment on disk before processing; a WAL
     segment is deleted when all its records are processed (durable ingestion, as in a RIC data layer);
  2. decode and validate every measurement (unit and range per metric, "no value" records counted);
  3. derive per-UE features: DL/UL throughput (Mbit/s), share of the cell throughput, RLC SDU delay (ms),
     drop rate, PRB utilisation, EWMA and z-score of the throughput, and the UE's slice;
  4. store the processed record in the KPM time-series store (one file per minute, kept store_retention_s);
  5. deliver one feature row per UE record (and per cell record) to the analytics xApp xSLA over a
     persistent TCP stream with backpressure (bounded delivery queue);
  6. subscription manager: xSLA asks for closer monitoring of a congested UE (POST /subscription) and gets
     an extra style-4 subscription for sla_sub_ttl_s; the fault knob 'burst' (KPM storm) adds extra
     style-4 subscriptions. Every extra subscription is a real E2 subscription: the gNB sends one more
     indication per period, over the real E2 path. If the gNB refuses extra subscriptions 3 times in a row,
     xKPM falls back to 'replay' mode (each received UE indication is ingested once more per missing
     subscription) and reports it in /stats (storm_mode).
  7. UE identity map: the F1AP UE IDs reported by the gNB are in attach order; calibration finds which
     F1AP ID belongs to which srsUE (ue1..ueN) and writes ue_map.json, which xKPM reads (slice per UE).

KPI exported for MILD: backlog = records waiting + in processing + rows waiting for delivery
-> telemetry_queue. Fault knob: work = multiplier of the per-record CPU time (decoder slowdown /
CPU-starved xApp; < 1 is the operator's scale-out that drains a backlog between episodes).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import threading
import time
from collections import deque

from mild_oran.xapps.common import (CtlServer, E2Link, Knobs, LineClient, RateMeter, burn, common_args, keep_alive,
                                    load_cfg, log, mono, proc_rss_kb, stop_on_signal)

NAME = 'xkpm'

# unit sanity per metric (srsRAN 25.10 E2SM-KPM, DU provider): (min, max)
RANGES = {'RRU.PrbUsedDl': (0, 275), 'RRU.PrbUsedUl': (0, 275), 'RRU.PrbAvailDl': (0, 275),
          'RRU.PrbAvailUl': (0, 275), 'RRU.PrbTotDl': (0, 100), 'RRU.PrbTotUl': (0, 100),
          'DRB.UEThpDl': (0, 1e7), 'DRB.UEThpUl': (0, 1e7), 'DRB.RlcSduTransmittedVolumeDL': (0, 1e8),
          'DRB.RlcSduTransmittedVolumeUL': (0, 1e8), 'DRB.RlcPacketDropRateDl': (0, 100),
          'DRB.RlcSduDelayDl': (0, 1e6), 'DRB.AirIfDelayUl': (0, 1e6), 'DRB.RlcDelayUl': (0, 1e6),
          'RACH.PreambleDedCell': (0, 1e6), 'CQI': (0, 15)}


class XKPM:
    def __init__(self, a, cfg):
        self.cfg = cfg
        self.a = a
        self.knobs = Knobs(work=1.0, burst=1.0)
        self.data = os.path.join(a.runtime, 'xkpm_data')
        shutil.rmtree(self.data, ignore_errors=True)
        self.wal_dir = os.path.join(self.data, 'wal')
        self.store_dir = os.path.join(self.data, 'store')
        os.makedirs(self.wal_dir)
        os.makedirs(self.store_dir)
        self.cv = threading.Condition()
        self.ingest_q = deque()
        self.in_proc = 0
        self.dq = deque()
        self.dcv = threading.Condition()
        self.cnt = {'rx_cell': 0, 'rx_ue': 0, 'rx_replay': 0, 'processed': 0, 'delivered': 0, 'rows_dropped': 0,
                    'val_errors': 0, 'no_value': 0, 'sub_ok': 0, 'sub_fail': 0, 'unsub': 0, 'gw_exits': 0}
        self.clock = threading.Lock()
        self.rx_rate = RateMeter(10.0)
        self.ind_rate = RateMeter(10.0)
        self.proc_rate = RateMeter(10.0)
        self.wlock = threading.Lock()
        self.wal_seg, self.wal_n, self.wal_f = 0, 0, None
        self.wal_pending = {}
        self.wal_closed = set()
        self._wal_open()
        self.slock = threading.Lock()
        self.store_f, self.store_t = None, 0.0
        # live view of the RAN (from the newest indication, before any processing: an overloaded xKPM
        # still shows a fresh RAN)
        self.live = {'cell': {}, 'ues': {}, 't': None}
        self.ind_times = deque(maxlen=64)
        self.ue_state = {}                  # f1ap -> {'ewma':.., 'var':.., 'n':..}
        self.ue_map = {}                    # f1ap(str) -> {'ue': 'ue1', 'sd': 1}
        self.ue_map_mtime = 0.0
        self.sub_req = {}                   # requester key -> expires (mono)
        self.extra_subs = 0
        self.sub_fail_run = 0
        self.storm_mode = 'e2sub'
        self.cmd_pending = None
        self.out = LineClient('127.0.0.1', a.sla_port, 'xkpm->xsla')
        self.e2 = E2Link(NAME, 'kpm', cfg, a.runtime, self.on_e2, styles='1,4')

    # ------------------------------------------------------------------ WAL
    def _wal_open(self):
        self.wal_seg += 1
        self.wal_n = 0
        self.wal_f = open(os.path.join(self.wal_dir, f'wal_{self.wal_seg:08d}.ndjson'), 'ab')
        self.wal_pending[self.wal_seg] = 0

    def _wal_append(self, line: bytes):
        with self.wlock:
            self.wal_f.write(line if line.endswith(b'\n') else line + b'\n')
            self.wal_f.flush()
            seg = self.wal_seg
            self.wal_pending[seg] += 1
            self.wal_n += 1
            if self.wal_n >= self.cfg.get('wal_segment_records', 200):
                self.wal_f.close()
                self.wal_closed.add(seg)
                self._wal_open()
            return seg

    def _wal_done(self, seg):
        with self.wlock:
            self.wal_pending[seg] -= 1
            if seg in self.wal_closed and self.wal_pending[seg] <= 0:
                self.wal_closed.discard(seg)
                self.wal_pending.pop(seg, None)
                try:
                    os.remove(os.path.join(self.wal_dir, f'wal_{seg:08d}.ndjson'))
                except OSError:
                    pass

    # ------------------------------------------------------------------ store
    def _store(self, rec: dict):
        now = mono()
        with self.slock:
            if self.store_f is None or now - self.store_t > 60:
                if self.store_f:
                    self.store_f.close()
                self.store_t = now
                self.store_f = open(os.path.join(self.store_dir, f'kpm_{int(now * 1000):014d}.ndjson'), 'a')
            self.store_f.write(json.dumps(rec, separators=(',', ':')) + '\n')

    def _store_janitor(self):
        keep = float(self.cfg.get('store_retention_s', 600))
        while True:
            time.sleep(10)
            now = mono() * 1000
            for fn in os.listdir(self.store_dir):
                try:
                    t = int(fn[4:18])
                    if now - t > (keep + 60) * 1000:
                        os.remove(os.path.join(self.store_dir, fn))
                except Exception:
                    pass
            with self.slock:
                if self.store_f:
                    self.store_f.flush()

    # ------------------------------------------------------------------ E2 ingest (gateway reader thread)
    def on_e2(self, ev):
        e = ev.get('ev')
        if e == 'ind':
            now = mono()
            self.ind_times.append(now)
            self.ind_rate.hit()
            if ev.get('fmt') == 1:
                self.live['cell'] = ev.get('m') or {}
                self.live['t'] = now
                self._ingest({'kind': 'cell', 't': now, 'm': ev.get('m') or {}}, 'rx_cell')
            elif ev.get('fmt') == 3:
                ues = ev.get('ues') or []
                self.live['ues'] = {str(u.get('f1ap')): u.get('m') or {} for u in ues}
                self.live['t'] = now
                for u in ues:
                    self._ingest({'kind': 'ue', 't': now, 'f1ap': u.get('f1ap'), 'm': u.get('m') or {}}, 'rx_ue')
                    if self.storm_mode == 'replay':                 # fallback: missing subscriptions replayed
                        for _ in range(max(0, self.want_extra() - self.extra_subs)):
                            self._ingest({'kind': 'ue', 't': now, 'f1ap': u.get('f1ap'), 'm': u.get('m') or {},
                                          'replay': 1}, 'rx_replay')
        elif e in ('sub', 'unsub'):
            self.cmd_pending = None                          # the gateway answered the last sub/unsub command
            if e == 'unsub':
                return
            with self.clock:
                self.cnt['sub_ok' if ev.get('ok') else 'sub_fail'] += 1
            if ev.get('ok'):
                self.sub_fail_run = 0
            else:
                self.sub_fail_run += 1
                if self.sub_fail_run >= 3 and self.storm_mode != 'replay':
                    self.storm_mode = 'replay'
                    log(NAME, 'the gNB refused 3 extra KPM subscriptions in a row: storm mode = replay')
        elif e == 'ready':
            log(NAME, f"E2 ready: node {ev.get('node')} (KPM {ev.get('kpm')})")
            self.extra_subs = 0
            self.cmd_pending = None
        elif e == 'gw_exit':
            self.extra_subs = 0
            self.cmd_pending = None
            with self.clock:
                self.cnt['gw_exits'] += 1

    def _ingest(self, rec, counter):
        line = json.dumps(rec, separators=(',', ':')).encode()
        seg = self._wal_append(line)
        with self.cv:
            self.ingest_q.append((seg, line))
            self.cv.notify()
        with self.clock:
            self.cnt[counter] += 1
        self.rx_rate.hit()

    # ------------------------------------------------------------------ subscription manager
    def want_extra(self):
        now = mono()
        storm = int(round(max(0.0, self.knobs.get('burst') - 1.0)))
        req = sum(1 for v in self.sub_req.values() if v > now)
        return min(int(self.cfg.get('storm_max_subs', 4)) + int(self.cfg.get('sub_req_max', 2)),
                   min(storm, int(self.cfg.get('storm_max_subs', 4))) + min(req, int(self.cfg.get('sub_req_max', 2))))

    def _sub_manager(self):
        while True:
            time.sleep(1.0)
            if not self.e2.st['connected'] or self.storm_mode == 'replay':
                continue
            # one command at a time: the gateway handles stdin sequentially, and a command sent while the previous
            # one is still waiting for the E2 node would be executed late, against a stale subscription count
            if self.cmd_pending is not None and mono() - self.cmd_pending < 15.0:
                continue
            base = 2                                   # style 1 + style 4 made at start
            have = max(0, self.e2.st['n_subs'] - base)
            self.extra_subs = have
            want = self.want_extra()
            if want > have:
                if self.e2.send('sub 4'):              # one per second at most: a burst, not a flood
                    self.cmd_pending = mono()
            elif want < have:
                if self.e2.send('unsub'):
                    self.cmd_pending = mono()
                    with self.clock:
                        self.cnt['unsub'] += 1

    def subs_request(self, q, body):
        d = json.loads(body or b'{}')
        key = str(d.get('ue', d.get('who', 'any')))
        self.sub_req[key] = mono() + float(d.get('ttl_s', self.cfg.get('sla_sub_ttl_s', 10.0)))
        return {'ok': 1, 'active_requests': sum(1 for v in self.sub_req.values() if v > mono())}

    # ------------------------------------------------------------------ processing (worker threads)
    def _ue_map(self):
        p = os.path.join(self.a.runtime, 'ue_map.json')
        try:
            m = os.path.getmtime(p)
            if m != self.ue_map_mtime:
                with open(p) as f:
                    self.ue_map = json.load(f).get('f1ap', {})
                self.ue_map_mtime = m
        except Exception:
            pass
        return self.ue_map

    def _process(self, line):
        rec = json.loads(line)
        m = rec.get('m') or {}
        bad = nv = 0
        clean = {}
        for k, v in m.items():
            if v is None:
                nv += 1
                continue
            lo, hi = RANGES.get(k, (-1e12, 1e12))
            if not (isinstance(v, (int, float)) and lo <= v <= hi and not math.isnan(v)):
                bad += 1
                continue
            clean[k] = float(v)
        out = {'kind': rec['kind'], 't': round(rec['t'], 3), 'n_val': len(clean), 'n_novalue': nv}
        out['dl_mbps'] = round(clean.get('DRB.UEThpDl', 0.0) / 1000.0, 4)
        out['ul_mbps'] = round(clean.get('DRB.UEThpUl', 0.0) / 1000.0, 4)
        out['prb_dl_pct'] = clean.get('RRU.PrbTotDl')
        out['rlc_delay_ms'] = round(clean['DRB.RlcSduDelayDl'] / 10.0, 3) if 'DRB.RlcSduDelayDl' in clean else None
        out['drop_pct'] = clean.get('DRB.RlcPacketDropRateDl')
        if rec['kind'] == 'ue':
            f = str(rec.get('f1ap'))
            info = self._ue_map().get(f, {})
            out.update(f1ap=rec.get('f1ap'), ue=info.get('ue'), sd=info.get('sd'))
            cell_dl = (self.live.get('cell') or {}).get('DRB.UEThpDl')
            out['share'] = round(clean.get('DRB.UEThpDl', 0.0) / cell_dl, 4) if cell_dl else None
            s = self.ue_state.setdefault(f, {'ewma': out['dl_mbps'], 'var': 0.0, 'n': 0})
            d = out['dl_mbps'] - s['ewma']
            s['ewma'] += 0.1 * d
            s['var'] = 0.9 * (s['var'] + 0.1 * d * d)
            s['n'] += 1
            out['dl_z'] = round(d / math.sqrt(s['var']), 3) if s['var'] > 1e-6 else 0.0
        with self.clock:
            self.cnt['val_errors'] += bad
            self.cnt['no_value'] += nv
        n_vals = max(1, len(m))
        burn(float(self.cfg.get('tel_record_ms', 60.0)) * (0.5 + n_vals / 28.0) * self.knobs.get('work'))
        self._store(out)
        return out

    def _worker(self):
        qmax = int(self.cfg.get('delivery_q_max', 60))
        while True:
            with self.cv:
                while not self.ingest_q:
                    self.cv.wait()
                seg, line = self.ingest_q.popleft()
                self.in_proc += 1
            row = None
            try:
                row = self._process(line)
            except Exception as e:
                log(NAME, f'processing error: {e}')
            finally:
                with self.cv:
                    self.in_proc -= 1
                self._wal_done(seg)
                with self.clock:
                    self.cnt['processed'] += 1
                self.proc_rate.hit()
            if row is not None:
                with self.dcv:
                    if len(self.dq) >= qmax:
                        with self.clock:
                            self.cnt['rows_dropped'] += 1
                    else:
                        self.dq.append(json.dumps(row, separators=(',', ':')).encode())
                        self.dcv.notify()

    def _delivery(self):
        while True:
            with self.dcv:
                while not self.dq:
                    self.dcv.wait()
                line = self.dq[0]
            if self.out.send(line):          # blocks while xSLA applies backpressure
                with self.dcv:
                    if self.dq and self.dq[0] is line:
                        self.dq.popleft()
                with self.clock:
                    self.cnt['delivered'] += 1
            else:
                time.sleep(1.0)

    # ------------------------------------------------------------------ control-port views
    def live_view(self, q=None, body=None):
        now = mono()
        lt = list(self.ind_times)
        recent = [t for t in lt if now - t <= 15.0]
        rate = (len(recent) - 1) / (recent[-1] - recent[0]) if len(recent) >= 3 and recent[-1] > recent[0] else 0.0
        c = self.live.get('cell') or {}
        ues = self.live.get('ues') or {}
        umap = self._ue_map()
        per_ue = {}
        for f, m in ues.items():
            name = (umap.get(f) or {}).get('ue') or f'f1ap{f}'
            per_ue[name] = round((m.get('DRB.UEThpDl') or 0.0) / 1000.0, 4)
        e2 = self.e2.status()
        return {'age_s': (now - self.live['t']) if self.live.get('t') else None, 'ind_hz': rate,
                'connected': e2['connected'], 'node': e2['node'], 'n_ue': len(ues),
                'dl_mbps': (c.get('DRB.UEThpDl') or 0.0) / 1000.0 if c else None,
                'ul_mbps': (c.get('DRB.UEThpUl') or 0.0) / 1000.0 if c else None,
                'prb_dl_pct': c.get('RRU.PrbTotDl') if c else None,
                'rlc_delay_ms': (c['DRB.RlcSduDelayDl'] / 10.0) if c.get('DRB.RlcSduDelayDl') is not None else None,
                'ue_dl_mbps': per_ue, 'ue_raw': {f: m.get('DRB.UEThpDl') for f, m in ues.items()},
                'extra_subs': self.extra_subs, 'storm_mode': self.storm_mode}

    def stats(self, q):
        with self.cv:
            iq, ip = len(self.ingest_q), self.in_proc
        with self.dcv:
            dq = len(self.dq)
        with self.clock:
            c = dict(self.cnt)
        return {'backlog': iq + ip + dq, 'ingest_q': iq, 'in_proc': ip, 'delivery_q': dq, **c,
                'rx_rate': self.rx_rate.rate(), 'ind_rate': self.ind_rate.rate(), 'proc_rate': self.proc_rate.rate(),
                'rss_kb': proc_rss_kb(), 'wal_open_segments': len(self.wal_pending), 'live': self.live_view(),
                'e2': self.e2.status(), 'extra_subs': self.extra_subs, 'want_extra': self.want_extra(),
                'storm_mode': self.storm_mode}

    def rotate(self, q=None, body=None):
        threading.Thread(target=self.e2.rotate, daemon=True).start()
        return {'ok': 1}

    def start(self, a):
        CtlServer(NAME, a.ctl_port or a.port + 1000, self.knobs, self.stats,
                  routes={('GET', '/live'): self.live_view, ('POST', '/subscription'): self.subs_request,
                          ('POST', '/rotate'): self.rotate})
        for i in range(int(self.cfg.get('tel_workers', 1))):
            threading.Thread(target=self._worker, daemon=True, name=f'w{i}').start()
        for fn in (self._delivery, self._store_janitor, self._sub_manager):
            threading.Thread(target=fn, daemon=True).start()
        self.e2.start()
        stop_on_signal(self.e2)
        log(NAME, f'up: ctl :{a.ctl_port or a.port + 1000}, E2 gateway {" ".join(self.e2.cmd[:1]) or "(none)"}')


def main():
    ap = common_args(argparse.ArgumentParser(description=__doc__))
    ap.add_argument('--sla_port', type=int, required=True, help='xSLA ingest port (delivery target)')
    a = ap.parse_args()
    x = XKPM(a, load_cfg(a.cfg))
    x.start(a)
    keep_alive()


if __name__ == '__main__':
    main()
