"""collector.py -- tick loop, failure-time detection, resumable 30-min segments, QA every 5 min and per segment.

One tick (= one time step of the dataset) lasts tick_s seconds (monotonic clock).
During a tick the driver applies the fault knobs of all active events, reads
all KPIs every sample_s seconds, and stores their mean. A KPI that could not be
measured in any sample of the tick is stored as NaN and counted in n_missing;
it is never replaced by a healthy value.

Failure time of a labelled event (intent compliance predicate, Table I rule):
  API        api_latency     >= api_p95_ms                 (nonlinear: AND sri  <= sri_low)
  Analytics  analytics_tput  <= analytics_tput_min         (nonlinear: AND snet <= snet_low)
  Telemetry  telemetry_queue >= telemetry_q_max            (nonlinear: AND sri  <= sri_low)
The predicate must hold for `persist` consecutive ticks inside the event window;
the failure time is the tick at which this persistence is reached. If it never
holds, the event is kept with failure_observed = false (strict policy: not
labelled, see quality.py). An event window lasts from the fault onset until the
intent has recovered for 2 ticks (at most recovery_max ticks after the injection).

Watchdogs, every tick (a segment is cut at its last quiet tick; nothing before the cut is lost):
  * RIC watchdog   a long run of ticks with missing KPIs that also covers quiet time cannot be explained
                   by a fault -> the MILD components are restarted.
  * RAN watchdog   no E2 indication at xKPM for > e2_stale_s, a UE without an address, or one UE without
                   any probe reply for probe_dead_s while no network fault is active -> RAN down: the
                   collector restarts the RAN automatically (ranctl: down, up, back-off).
Mini-QA, every qa.check_every_ticks ticks (5 min): see remedy.Doctor (fixes in place, or cuts the segment).
Segment QA, at the end of every segment (30 min): segment_qa() below; BAD segments are KEPT on disk and
their good windows (salvage) stay usable.

Each segment is written incrementally (flushed every tick):
  segments/seg_XXXX/rows.csv       one row per tick: t, 8 KPIs, deltas, n_missing, RAN/E2 context
  segments/seg_XXXX/truth.csv      knob values, active events, tick wall length (audit only, never a feature)
  segments/seg_XXXX/events.jsonl   one line per closed labelled event
  segments/seg_XXXX/plan.json, meta.json, qa.json, minqa.jsonl
"""
from __future__ import annotations

import csv
import json
import math
import os
import time
from typing import Dict, List

import numpy as np

from . import util
from .kpi import CTX_NAMES, KPI_NAMES
from .scheduler import build_plan, knobs_for_tick
from .util import mono

ROW_COLS = ['t'] + KPI_NAMES + ['cpu_delta', 'sri_delta', 'n_missing'] + CTX_NAMES
INTENT_KPI = {'api': 'api_latency', 'analytics': 'analytics_tput', 'telemetry': 'telemetry_queue'}


def _ok(v):
    return v is not None and not (isinstance(v, float) and math.isnan(v))


def predicate_parts(intent, typ, k, tg):
    """Return (service_part, network_part, holds) for one labelled event."""
    if intent == 'api':
        svc = _ok(k['api_latency']) and k['api_latency'] >= tg['api_p95_ms']
        net = _ok(k['sri']) and k['sri'] <= tg['sri_low']
    elif intent == 'analytics':
        svc = _ok(k['analytics_tput']) and k['analytics_tput'] <= tg['analytics_tput_min']
        net = _ok(k['snet']) and k['snet'] <= tg['snet_low']
    else:
        svc = _ok(k['telemetry_queue']) and k['telemetry_queue'] >= tg['telemetry_q_max']
        net = _ok(k['sri']) and k['sri'] <= tg['sri_low']
    holds = (svc and net) if typ.startswith('nonlinear') else svc
    return svc, net, holds


def recovered(intent, typ, k, tg):
    """True when the intent is clearly healthy again (used to close an episode window)."""
    if intent == 'api':
        svc = _ok(k['api_latency']) and k['api_latency'] < 0.5 * tg['api_p95_ms']
        net = _ok(k['sri']) and k['sri'] > tg['sri_low'] + 2
    elif intent == 'analytics':
        svc = _ok(k['analytics_tput']) and k['analytics_tput'] > 1.25 * tg['analytics_tput_min']
        net = _ok(k['snet']) and k['snet'] > tg['snet_low'] + 2
    else:
        svc = _ok(k['telemetry_queue']) and k['telemetry_queue'] < 0.5 * tg['telemetry_q_max']
        net = _ok(k['sri']) and k['sri'] > tg['sri_low'] + 2
    return svc and (net or not typ.startswith('nonlinear'))


class TickRunner:
    def __init__(self, stack, cfg, targets, scales):
        self.stack, self.cfg, self.tg, self.sc = stack, cfg, targets, scales
        self.prev = {'cpu_pct': None, 'sri': None, 'telemetry_queue': None}
        self.bad_run, self.bad_quiet, self.bad_start = 0, 0, None
        self.ran_bad, self.ran_bad_start, self.probe_dead = 0, None, 0
        self.unhealthy = None
        self.ran_down = None
        self.cut = None
        self.next_t = None
        self.miss_run = {}
        self.on_repair = None                      # callback(role, ticks, ok) for the remedy log

    REPAIR_KPI = {'api_latency': 'xrc', 'analytics_tput': 'xsla', 'telemetry_queue': 'xkpm'}

    def repair(self, row):
        """Early in-place repair: a component whose KPI is missing for qa.repair_after_ticks ticks in a row
        (8 ticks = 32 s) is restarted at once, without cutting the segment (its ticks stay NaN; events that
        overlap the gap are dropped at merge). The RIC watchdog (40 ticks) remains the last resort."""
        n = int(self.cfg.get('qa', {}).get('repair_after_ticks', 8))
        for kpi, role in self.REPAIR_KPI.items():
            v = row.get(kpi)
            miss = v is None or (isinstance(v, float) and math.isnan(v))
            self.miss_run[role] = self.miss_run.get(role, 0) + 1 if miss else 0
            if self.miss_run[role] == n and hasattr(self.stack, 'restart_role'):
                util.log(f'REMEDY: {kpi} missing for {n} ticks: restarting {role} in place', 'WARN')
                try:
                    self.stack.restart_role(role)
                    ok = True
                except Exception as e:
                    ok = False
                    util.log(f'restart of {role} failed: {e}', 'WARN')
                if self.on_repair:
                    self.on_repair(role, n, ok)

    # ---------------------------------------------------------------- watchdogs
    def watch(self, t, row, plan, k):
        sri = row.get('sri')
        bad = row['n_missing'] > 0 or (sri is not None and not math.isnan(sri) and sri < 5)
        rmax = self.cfg.get('recovery_max', 10) + 2
        quiet = not any(e['start'] <= t < e['end'] + rmax for e in plan)
        if bad:
            if self.bad_run == 0:
                self.bad_start = t
            self.bad_run += 1
            self.bad_quiet += int(quiet)
        else:
            self.bad_run, self.bad_quiet = 0, 0
        if self.bad_run >= self.cfg.get('watchdog_bad_ticks', 40) and self.bad_quiet >= 3:
            self.unhealthy = {'detected_at': t, 'bad_since': self.bad_start, 'bad_ticks': self.bad_run,
                              'bad_quiet_ticks': self.bad_quiet}
        # RAN watchdog
        ran = self.cfg['ran']
        age = row.get('e2_age_s')
        # unknown age (xKPM did not answer) is a RIC-side problem (RIC watchdog), not a RAN outage
        e2_bad = age is not None and not (isinstance(age, float) and math.isnan(age)) and \
            age > float(ran.get('e2_stale_s', 10))
        ips = row.get('_ue_ips') or []
        no_ip = [f'ue{j + 1}' for j, ip in enumerate(ips) if not ip]
        net_fault = k['delay_ms'] > 0 or k['loss_pct'] > 0 or k['cell_load'] > 1.2
        dead_ues = [f'ue{j + 1}' for j, l in enumerate(row.get('_ue_loss') or [])
                    if l is not None and l >= 99.9] if not net_fault else []
        self.probe_dead = self.probe_dead + 1 if dead_ues else 0
        if e2_bad or no_ip:
            if self.ran_bad == 0:
                self.ran_bad_start = t
            self.ran_bad += 1
        else:
            self.ran_bad = 0
        tick = float(self.cfg['tick_s'])
        lim_ran = max(2, int(math.ceil(float(ran.get('ue_ip_missing_s', 12)) / tick)))
        lim_probe = max(3, int(math.ceil(float(ran.get('probe_dead_s', 20)) / tick)))
        if row.get('_rc_stuck') and not self.ran_down:
            st_ = row['_rc_stuck']
            self.ran_down = {'detected_at': t, 'since': t,
                             'why': f"the gNB did not answer an E2SM-RC {st_.get('what', 'control')} "
                                    f"(after {st_.get('ms')} ms): its E2 agent is wedged"}
            return self.unhealthy or self.ran_down
        if self.ran_bad >= lim_ran or self.probe_dead >= lim_probe:
            why = []
            if e2_bad:
                why.append(f'no E2SM-KPM indication at xKPM for {age:.0f} s')
            if no_ip:
                why.append(f'no IP address in UE namespace(s) {no_ip}')
            if self.probe_dead >= lim_probe:
                why.append(f'100 % probe loss of {dead_ues} without any network fault')
            start = self.ran_bad_start if self.ran_bad >= lim_ran else t - self.probe_dead + 1
            self.ran_down = {'detected_at': t, 'since': start, 'why': '; '.join(why)}
        return self.unhealthy or self.ran_down

    # ---------------------------------------------------------------- one tick
    def tick(self, t, plan):
        tick_s = float(self.cfg['tick_s'])
        now = mono()
        if self.next_t is None or now - self.next_t > tick_s:
            self.next_t = now                      # first tick, or resynchronise after a long stall
        t0 = self.next_t
        self.next_t = t0 + tick_s
        if t % 25 == 0 and hasattr(self.stack, 'sent'):
            self.stack.sent = {}                   # periodically re-send every knob (guards a lost update)
        k = knobs_for_tick(t, list(plan) + list(getattr(self, 'demand', []) or []), self.sc, self.cfg['ramp_power'])
        tq = self.prev.get('telemetry_queue')
        drain = (not k['telemetry_active']) and tq is not None and tq > 0.5 * self.tg['telemetry_q_max']
        if drain:                                  # operator remediation between episodes (scale-out)
            k['telemetry'] = min(k['telemetry'], self.cfg['drain_mult'])
        self.stack.apply(k)
        samples = {n: [] for n in KPI_NAMES + CTX_NAMES}
        hidden = {'_ue_ips': None, '_ue_loss': None, '_e2_connected': None, '_rc_stuck': None}
        sample_s = float(self.cfg['sample_s'])
        nxt = mono()
        while True:
            r = self.stack.read_kpis()
            for n in samples:
                v = r.get(n)
                if v is not None and not (isinstance(v, float) and math.isnan(v)):
                    samples[n].append(float(v))
            for h in hidden:
                if r.get(h) is not None:
                    hidden[h] = r[h]
            nxt += sample_s
            if nxt >= self.next_t - 0.05:
                break
            time.sleep(max(0.0, nxt - mono()))
        rest = self.next_t - mono()
        if rest > 0:
            time.sleep(rest)
        row = {'t': t}
        for n in samples:
            row[n] = float(np.mean(samples[n])) if samples[n] else float('nan')
        for n in ('cpu_pct', 'ram_pct', 'storage_pct', 'snet', 'sri'):
            if not math.isnan(row[n]):
                row[n] = float(np.clip(row[n], 0, 100))
        row['n_missing'] = sum(1 for n in KPI_NAMES if math.isnan(row[n]))
        for n, d in (('cpu_pct', 'cpu_delta'), ('sri', 'sri_delta')):
            p = self.prev.get(n)
            row[d] = (row[n] - p) if (p is not None and not math.isnan(row[n])) else 0.0
            if not math.isnan(row[n]):
                self.prev[n] = row[n]
        if not math.isnan(row['telemetry_queue']):
            self.prev['telemetry_queue'] = row['telemetry_queue']
        row.update(hidden)
        wall = round(mono() - t0, 3)
        truth = {'t': t, 'tick_wall_s': wall, **{f'knob_{x}': round(float(k[x]), 4) for x in
                                                   ('api', 'analytics', 'telemetry', 'burst', 'cell_load',
                                                    'delay_ms', 'loss_pct')},
                 'drain': int(drain), 'active': ';'.join(e['type'] for e in plan if e['start'] <= t < e['end'])}
        return row, truth, k


def _parts_out(p):
    p = p or {'svc_hit': False, 'net_hit': False, '_n': 0, '_miss': 0}
    return {'svc_hit': p['svc_hit'], 'net_hit': p['net_hit'], 'kpi_missing_frac': round(p['_miss'] / max(p['_n'], 1), 3)}


def run_ticks(runner, plan, labels, t_from, t_to, on_row=None, on_event=None, should_stop=None, check=None):
    """Run ticks [t_from, t_to). Labelled events are evaluated and closed here.
    check(t) is the mini-QA hook (called after every tick; a non-empty answer cuts the run: runner.cut).
    Returns True when all ticks ran, False when stopped (user, watchdog, RAN down or mini-QA cut)."""
    streak, done, parts = {}, {}, {}
    lab = [(intent, e) for intent, L in labels.items() for e in L]
    closed, pending = set(), {}
    rmax = runner.cfg.get('recovery_max', 10)

    def emit(key, rec, end_tick):
        rec['end'] = int(end_tick)
        if on_event:
            on_event(rec)
        closed.add(key)

    t = t_from
    for t in range(t_from, t_to):
        if should_stop and should_stop():
            return False
        row, truth, k = runner.tick(t, plan)
        for intent, e in lab:
            key = (intent, e['type'], e['start'])
            if key in closed:
                continue
            if e['start'] <= t < e['end'] and key not in done:
                svc, net, holds = predicate_parts(intent, e['type'], row, runner.tg)
                p = parts.setdefault(key, {'svc_hit': False, 'net_hit': False, '_n': 0, '_miss': 0})
                p['svc_hit'] |= svc
                p['net_hit'] |= net
                p['_n'] += 1
                v = row.get(INTENT_KPI[intent])
                p['_miss'] += int(v is None or (isinstance(v, float) and math.isnan(v)))
                streak[key] = streak.get(key, 0) + 1 if holds else 0
                if streak[key] >= runner.cfg['persist']:
                    done[key] = t
            if t == e['end'] - 1 and key not in pending:
                ft = done.get(key)
                obs = ft is not None
                ft = ft if obs else e['end'] - 1
                ft = min(max(int(ft), e['start'] + 1), e['end'] - 1)
                pending[key] = [{'intent': intent, 'type': e['type'], 'start': e['start'], 'end': e['end'],
                                 'inject_end': e['end'], 'failure_time': ft, 'failure_observed': obs,
                                 'crossing_frac': round((ft - e['start']) / max(1, e['end'] - e['start']), 3),
                                 **_parts_out(parts.get(key))}, 0]
            elif key in pending and t >= e['end']:
                rec, good = pending[key]
                good = good + 1 if recovered(intent, e['type'], row, runner.tg) else 0
                pending[key][1] = good
                if good >= 2 or t >= e['end'] - 1 + rmax:
                    emit(key, rec, (t - 1) if good >= 2 else (t + 1))
                    pending.pop(key)
        if on_row:
            on_row(row, truth)
        w = runner.watch(t, row, plan, k)
        if not w:
            runner.repair(row)
        if not w and check is not None:
            c = check(t)
            if c:
                runner.cut = c
                util.log(f'MINI-QA: {c}; cutting this segment here', 'WARN')
                return False
        if w:
            if runner.ran_down:
                util.log(f'RAN WATCHDOG: {runner.ran_down}; stopping this segment', 'WARN')
            else:
                util.log(f'RIC WATCHDOG: components unresponsive {runner.unhealthy}; stopping this segment', 'WARN')
            return False
    for key, (rec, good) in list(pending.items()):
        emit(key, rec, t_to)
    return True


# =============================================================================
USABLE = ('complete', 'truncated')          # whole segment usable
# 'bad'       QA verdict BAD: kept on disk; meta['good_windows'] lists the salvaged tick ranges
# 'discarded' too short after a cut (still kept on disk, never merged)


class SegmentStore:
    def __init__(self, root):
        self.root = os.path.join(root, 'segments')
        os.makedirs(self.root, exist_ok=True)

    def path(self, i):
        return os.path.join(self.root, f'seg_{i:04d}')

    def list(self):
        return sorted(int(d[4:]) for d in os.listdir(self.root) if d.startswith('seg_') and d[4:].isdigit())

    def meta(self, i):
        return util.load_json(os.path.join(self.path(i), 'meta.json'), {})

    def set_meta(self, i, **kw):
        m = self.meta(i)
        m.update(kw)
        util.save_json(os.path.join(self.path(i), 'meta.json'), m)
        return m

    def finished(self):
        """Segments with a QA-able result (whole or salvaged)."""
        return [i for i in self.list() if self.meta(i).get('status') in USABLE + ('bad',)]

    def pieces(self):
        """(segment, a, b): tick ranges that go into the dataset. Whole usable segments, plus the good windows
        of BAD segments (salvage)."""
        out = []
        for i in self.list():
            m = self.meta(i)
            if m.get('status') in USABLE:
                out.append((i, 0, int(m.get('ticks', 0))))
            elif m.get('status') == 'bad':
                for a, b in m.get('good_windows') or []:
                    out.append((i, int(a), int(b)))
        return out

    def total_ticks(self):
        return sum(b - a for _, a, b in self.pieces())

    def repair(self, i, min_ticks, upto=None):
        """Cut an interrupted segment at the last quiet tick (at or before `upto` if given)."""
        d = self.path(i)
        m = self.meta(i)
        plan = util.load_json(os.path.join(d, 'plan.json'), {}).get('plan', [])
        rows_p = os.path.join(d, 'rows.csv')
        if not os.path.exists(rows_p):
            m['status'] = 'discarded'
            util.save_json(os.path.join(d, 'meta.json'), m)
            return
        with open(rows_p) as f:
            rows = list(csv.DictReader(f))
        n = len(rows)
        cut = n if upto is None else max(0, min(n, int(upto)))
        rmax = int(m.get('recovery_max', 10)) + 2
        while cut > 0 and any(e['start'] < cut < e['end'] + rmax for e in plan):
            cut -= 1
        if cut < min_ticks:
            util.log(f'segment {i}: only {cut} usable ticks after the cut; segment kept on disk as discarded')
            m['status'] = 'discarded'
            m['discarded_ticks'] = n
            util.save_json(os.path.join(d, 'meta.json'), m)
            return
        for name in ('rows.csv', 'truth.csv'):
            p = os.path.join(d, name)
            if os.path.exists(p):
                with open(p) as f:
                    r = list(csv.reader(f))
                if len(r) > cut + 1:                      # the cut-off part is kept beside it, never deleted
                    with open(p + '.after_cut', 'w', newline='') as f:
                        csv.writer(f).writerows(r[:1] + r[cut + 1:])
                with open(p, 'w', newline='') as f:
                    csv.writer(f).writerows(r[:cut + 1])
        evp = os.path.join(d, 'events.jsonl')
        ev = [json.loads(x) for x in open(evp)] if os.path.exists(evp) else []
        ev = [e for e in ev if e['end'] <= cut]
        with open(evp, 'w') as f:
            for e in ev:
                f.write(json.dumps(e) + '\n')
        m.update({'status': 'truncated', 'ticks': cut, 'truncated_from': n})
        util.save_json(os.path.join(d, 'meta.json'), m)
        util.log(f'segment {i}: stopped at tick {n}, kept {cut} ticks and {len(ev)} complete events')


def run_segment(stack, cfg, targets, scales, store: SegmentStore, i, should_stop, doctor=None):
    """One segment. Returns 'ok', 'ran_down', 'unhealthy', 'cut' (mini-QA) or 'stopped'."""
    d = store.path(i)
    os.makedirs(d, exist_ok=True)
    seed = cfg['base_seed'] + i
    ticks = cfg['segment_ticks']
    plan, labels = build_plan(cfg, seed, ticks)
    # v3: the tenant-demand profile is not an event (it spans the whole segment); it is kept apart so that every
    # quiet-time / cut / QA rule keeps working on the fault events only
    demand = [e for e in plan if e.get('intent') == 'demand']
    plan = [e for e in plan if e.get('intent') != 'demand']
    util.save_json(os.path.join(d, 'plan.json'), {'seed': seed, 'plan': plan, 'labels': labels, 'demand': demand})
    meta = {'segment': i, 'seed': seed, 'status': 'running', 'code_version': 2,
            'started': time.strftime('%Y-%m-%d %H:%M:%S'), 'recovery_max': cfg.get('recovery_max', 10),
            'tick_s': cfg['tick_s'], 'targets': targets, 'scales': scales, 'stack': stack.manifest(),
            'attempt': cfg.get('_attempt', 1)}
    util.save_json(os.path.join(d, 'meta.json'), meta)
    fr = open(os.path.join(d, 'rows.csv'), 'w', newline='')
    ft = open(os.path.join(d, 'truth.csv'), 'w', newline='')
    fe = open(os.path.join(d, 'events.jsonl'), 'w')
    wr = csv.DictWriter(fr, fieldnames=ROW_COLS, extrasaction='ignore')
    wt = None
    wr.writeheader()
    st = {'last_print': mono()}
    recent = []

    def on_row(row, truth):
        nonlocal wt
        wr.writerow({c: row.get(c) for c in ROW_COLS})
        fr.flush()
        if wt is None:
            wt = csv.DictWriter(ft, fieldnames=list(truth.keys()))
            wt.writeheader()
        wt.writerow(truth)
        ft.flush()
        recent.append((row, truth))
        if mono() - st['last_print'] > 60:
            st['last_print'] = mono()
            f = lambda x, p=1: 'nan' if x is None or (isinstance(x, float) and math.isnan(x)) else f'{x:.{p}f}'  # noqa
            util.log(f"seg {i} t={row['t']}/{ticks} api={f(row['api_latency'], 0)}ms tput={f(row['analytics_tput'], 2)} "
                     f"tq={f(row['telemetry_queue'], 0)} cpu={f(row['cpu_pct'])} ram={f(row['ram_pct'])} "
                     f"disk={f(row['storage_pct'])} snet={f(row['snet'])} sri={f(row['sri'])} | "
                     f"cell dl={f(row['ran_dl_mbps'])}Mb/s prb={f(row['ran_prb_dl_pct'], 0)}% "
                     f"E2 {f(row['e2_ind_hz'], 2)}/s +{f(row['e2_extra_subs'], 0)} subs rtt={f(row['ue_rtt_ms'], 0)}ms "
                     f"tick={truth['tick_wall_s']}s [{truth['active']}]")

    def on_event(rec):
        fe.write(json.dumps(rec) + '\n')
        fe.flush()

    every = int(cfg.get('qa', {}).get('check_every_ticks', 75))

    def check(t):
        if doctor is None or (t + 1) % every != 0:
            return None
        win = recent[-every:]
        del recent[:-every]
        return doctor.mini_check(i, t, win, plan)

    runner = TickRunner(stack, cfg, targets, scales)
    runner.demand = demand
    if doctor is not None:
        runner.on_repair = lambda role, n, ok: doctor.log('in-place restart', segment=i, role=role,
                                                          missing_ticks=n, ok=ok)
    stack.reset()
    ok = run_ticks(runner, plan, labels, 0, ticks, on_row, on_event, should_stop, check=check)
    for f in (fr, ft, fe):
        f.close()
    comp = stack.component_stats() if hasattr(stack, 'component_stats') else {}
    if ok:
        meta.update({'status': 'complete', 'ticks': ticks, 'finished': time.strftime('%Y-%m-%d %H:%M:%S'),
                     'knob_failures': stack.manifest().get('knob_failures', {}), 'component_stats': comp})
        util.save_json(os.path.join(d, 'meta.json'), meta)
        return 'ok'
    m = store.set_meta(i, component_stats=comp)
    if runner.ran_down:
        store.set_meta(i, ran_down=runner.ran_down)
        store.repair(i, cfg['min_segment_ticks'], upto=max(0, runner.ran_down['since'] - 2))
        return 'ran_down'
    if runner.unhealthy:
        store.set_meta(i, watchdog=runner.unhealthy)
        store.repair(i, cfg['min_segment_ticks'], upto=max(0, runner.unhealthy['bad_since'] - 5))
        return 'unhealthy'
    if runner.cut:
        store.set_meta(i, minqa_cut=runner.cut)
        upto = runner.cut.get('since') if isinstance(runner.cut, dict) else None
        store.repair(i, cfg['min_segment_ticks'], upto=upto)
        return 'cut'
    store.repair(i, cfg['min_segment_ticks'])
    return 'stopped'


# =============================================================================
# per-segment QA: events (observed failures) + data health + salvage windows (runs after every segment)
# =============================================================================
def _crossed(rows, intent, typ, tg, a, b, persist):
    s = 0
    for r in rows[a:b]:
        s = s + 1 if predicate_parts(intent, typ, r, tg)[2] else 0
        if s >= persist:
            return True
    return False


def tick_good(rows_df, tr, cfg, n_ues):
    """Per-tick health used for salvage: all KPIs measured, tick on time, E2 fresh, every UE probed,
    radio not too slow. Returns a boolean array aligned with rows_df."""
    tick = float(cfg['tick_s'])
    ran = cfg['ran']
    g = np.array((rows_df['n_missing'].fillna(9) == 0).to_numpy(), dtype=bool, copy=True)
    if 'tick_wall_s' in tr and len(tr) == len(rows_df):
        late = np.array(tr['tick_wall_s'].to_numpy() > 1.1 * tick, dtype=bool)
        prev = np.concatenate([[False], late[:-1]])
        nxt = np.concatenate([late[1:], [False]])
        g &= ~(late & (prev | nxt))                 # an isolated late tick is harmless; a run of them is not
    if 'e2_age_s' in rows_df:
        a = rows_df['e2_age_s'].values
        g &= ~(a > float(ran.get('e2_stale_s', 10)))              # NaN (no E2 view) counts as good here
    if 'ue_probe_n' in rows_df and n_ues:
        g &= ~(rows_df['ue_probe_n'].values < n_ues - 0.5)
    if 'radio_speed' in rows_df:
        r = rows_df['radio_speed'].values
        g &= ~(r < float(ran.get('radio_speed_min', 0.5)))
    return g


def salvage_windows(rows_df, good, plan, labels_ok_fn, cfg):
    """Maximal runs of good ticks, shrunk so that no event window (+ recovery) is cut, at least
    qa.salvage_min_ticks long, and with healthy-time target violations <= 5 % (labels_ok_fn)."""
    rmax = int(cfg.get('recovery_max', 10)) + 2
    tv = rows_df['t'].values.astype(int)
    out, j, n = [], 0, len(tv)
    min_len = int(cfg.get('qa', {}).get('salvage_min_ticks', 100))
    while j < n:
        if not good[j]:
            j += 1
            continue
        k = j
        while k < n and good[k]:
            k += 1
        a, b = int(tv[j]), int(tv[k - 1]) + 1
        while any(e['start'] < a < e['end'] + rmax for e in plan) and a < b:
            a += 1
        while any(e['start'] < b < e['end'] + rmax for e in plan) and b > a:
            b -= 1
        if b - a >= min_len and labels_ok_fn(a, b):
            out.append([a, b])
        j = k
    return out


QA_CODES = {
    'missing_kpis': 'KPIs missing in many ticks (a component or a probe not answering)',
    'tick_overrun': 'ticks longer than 1.1 x tick_s (machine overloaded)',
    'e2_stale': 'E2 indications stale or too slow at xKPM',
    'radio_slow': 'ZMQ radio much slower than real time',
    'ue_probes': 'not every UE probed (UE detached or agent dead)',
    'healthy_slo': 'targets violated in healthy (quiet) time: calibration drift',
    'flat_kpis': 'a KPI without any variation',
    'probe_floor': 'healthy snet/sri close to the nonlinear thresholds',
    'mimics': 'benign mimics crossed a target',
    'crosstalk': 'unlabelled crossings of other intents inside event windows',
    'low_observed': 'event types with few observed failures',
}


def segment_qa(store: SegmentStore, i, cfg, targets):
    import pandas as pd
    d = store.path(i)
    ev = [json.loads(x) for x in open(os.path.join(d, 'events.jsonl'))] if os.path.exists(os.path.join(d, 'events.jsonl')) else []
    out = {}
    for e in ev:
        g = out.setdefault(e['type'], {'n': 0, 'observed': 0, 'cross': [], 'svc_hit': 0, 'net_hit': 0, 'missing': 0.0})
        g['n'] += 1
        g['missing'] += float(e.get('kpi_missing_frac', 0.0))
        g['observed'] += int(e['failure_observed'])
        g['svc_hit'] += int(e.get('svc_hit', False))
        g['net_hit'] += int(e.get('net_hit', False))
        if e['failure_observed']:
            g['cross'].append(e['crossing_frac'])
    for g in out.values():
        g['observed_frac'] = g['observed'] / max(g['n'], 1)
        g['median_cross'] = float(np.median(g['cross'])) if g['cross'] else None
        g['missing_frac'] = g['missing'] / max(g['n'], 1)
        del g['cross']
    # ---------------- data health
    health = {}
    try:
        rows_df = pd.read_csv(os.path.join(d, 'rows.csv'))
        tr = pd.read_csv(os.path.join(d, 'truth.csv')) if os.path.getsize(os.path.join(d, 'truth.csv')) else pd.DataFrame()
    except Exception:
        rows_df, tr = pd.DataFrame(), pd.DataFrame()
    m = store.meta(i)
    n = int(m.get('ticks', len(rows_df)))
    rows_df = rows_df[rows_df['t'] < n] if len(rows_df) else rows_df
    tr = tr[tr['t'] < n] if len(tr) and 't' in tr else tr
    n_ues = int(((m.get('stack') or {}).get('n_ues')) or 0)
    plan = util.load_json(os.path.join(d, 'plan.json'), {}).get('plan', [])
    rmax = int(cfg.get('recovery_max', 10)) + 2
    good = np.array([], dtype=bool)
    if len(rows_df):
        tv = rows_df['t'].values
        quiet = np.array([not any(e['start'] <= t < e['end'] + rmax for e in plan) for t in tv], dtype=bool)
        q = rows_df[quiet]
        tick = float(cfg['tick_s'])
        recs = rows_df.to_dict('records')

        def slo_of(df):
            return {'api': float((df['api_latency'] >= targets['api_p95_ms']).mean()) if len(df) else None,
                    'analytics': float((df['analytics_tput'] <= targets['analytics_tput_min']).mean()) if len(df) else None,
                    'telemetry': float((df['telemetry_queue'] >= targets['telemetry_q_max']).mean()) if len(df) else None}
        slo = slo_of(q)
        mimic_cross, crosstalk, n_mimic = 0, 0, 0
        idx = {int(t): j for j, t in enumerate(tv)}
        per = int(cfg['persist'])
        for e in plan:
            a, b = idx.get(e['start']), idx.get(min(e['end'] + rmax, int(tv[-1])))
            if a is None or b is None:
                continue
            if e['intent'] == 'mimic':
                n_mimic += 1
                mimic_cross += int(any(_crossed(recs, it, 'independent', targets, a, b, per)
                                       for it in ('api', 'analytics', 'telemetry')))
            else:
                own = {e['intent']} | ({'telemetry'} if e.get('victim_start') is not None else set())
                for it in {'api', 'analytics', 'telemetry'} - own:
                    crosstalk += int(_crossed(recs, it, 'independent', targets, a, b, per))
        med = lambda c, df=rows_df: float(df[c].median()) if c in df and df[c].notna().any() else None  # noqa: E731
        good = tick_good(rows_df, tr, cfg, n_ues)
        health = {
            'rows': int(len(rows_df)),
            'missing_row_frac': round(float((rows_df['n_missing'] > 0).mean()), 4),
            'tick_mean_s': round(float(tr['tick_wall_s'].mean()), 3) if 'tick_wall_s' in tr else None,
            'tick_overrun_frac': round(float((tr['tick_wall_s'] > 1.1 * tick).mean()), 4) if 'tick_wall_s' in tr else None,
            'e2_hz_median': med('e2_ind_hz'),
            'e2_stale_frac': round(float((rows_df['e2_age_s'] > float(cfg['ran'].get('e2_stale_s', 10))).mean()), 4)
            if 'e2_age_s' in rows_df else None,
            'radio_speed_median': med('radio_speed'),
            'ue_probe_short_frac': round(float((rows_df['ue_probe_n'] < n_ues - 0.5).mean()), 4)
            if 'ue_probe_n' in rows_df and n_ues else None,
            'n_ue_e2_median': med('ran_n_ue'),
            'good_tick_frac': round(float(good.mean()), 4),
            'healthy_snet_p10': float(q['snet'].quantile(0.10)) if len(q) and q['snet'].notna().any() else None,
            'healthy_sri_p10': float(q['sri'].quantile(0.10)) if len(q) and q['sri'].notna().any() else None,
            'healthy_rtt_ms_median': med('ue_rtt_ms', q) if len(q) else None,
            'healthy_dl_mbps_median': med('ran_dl_mbps', q) if len(q) else None,
            'healthy_slo_exceedance': slo,
            'mimics': n_mimic, 'mimic_crossings': mimic_cross, 'crosstalk_crossings': crosstalk,
            'kpi_std': {c: float(rows_df[c].std()) for c in KPI_NAMES if c in rows_df},
        }
    # ---------------- verdict (every reason carries a code the remedy ladder understands)
    bad, warn, codes = [], [], {}

    def flag(level, code, msg):
        (bad if level == 'BAD' else warn).append(msg)
        if codes.get(code) != 'BAD':
            codes[code] = level
    if health:
        mx = float(cfg.get('max_missing_rate_per_segment', 0.2))
        if health['missing_row_frac'] > mx:
            flag('BAD', 'missing_kpis', f"KPIs missing in {100 * health['missing_row_frac']:.0f} % of the ticks")
        elif health['missing_row_frac'] > 0.05:
            flag('WARN', 'missing_kpis', f"KPIs missing in {100 * health['missing_row_frac']:.1f} % of the ticks")
        ov = health['tick_overrun_frac']
        if ov is not None and ov > float(cfg.get('max_tick_overrun_frac', 0.1)):
            flag('BAD', 'tick_overrun', f"{100 * ov:.0f} % of ticks longer than 1.1 x tick_s (machine overloaded)")
        elif ov is not None and ov > 0.03:
            flag('WARN', 'tick_overrun', f"{100 * ov:.1f} % of ticks longer than 1.1 x tick_s")
        es = health.get('e2_stale_frac')
        if es is not None and es > 0.1:
            flag('BAD', 'e2_stale', f'E2 indications stale in {100 * es:.0f} % of the ticks')
        hz = health.get('e2_hz_median')
        if hz is not None and hz < 0.5:
            flag('WARN', 'e2_stale', f'only {hz:.2f} E2 indications/s at xKPM (expected ~1.5-2 with styles 1 + 4)')
        rs = health.get('radio_speed_median')
        if rs is not None and rs < float(cfg['ran'].get('radio_speed_min', 0.5)):
            flag('BAD', 'radio_slow', f'ZMQ radio at {rs:.2f} x real time (minimum {cfg["ran"].get("radio_speed_min")})')
        us = health.get('ue_probe_short_frac')
        if us is not None and us > 0.1:
            flag('BAD', 'ue_probes', f'not every UE probed in {100 * us:.0f} % of the ticks')
        nue = health.get('n_ue_e2_median')
        if nue is not None and n_ues and nue < n_ues - 0.5:
            flag('WARN', 'ue_probes', f'E2SM-KPM reports {nue:.0f} UEs, {n_ues} expected')
        s = health['healthy_slo_exceedance']
        worst = max([v for v in s.values() if v is not None] or [0.0])
        if worst > 0.05:
            flag('BAD', 'healthy_slo', f'targets violated in {100 * worst:.1f} % of healthy ticks {s}')
        elif worst > 0.02:
            flag('WARN', 'healthy_slo', f'targets violated in {100 * worst:.1f} % of healthy ticks {s}')
        for key, low in (('healthy_snet_p10', targets['snet_low']), ('healthy_sri_p10', targets['sri_low'])):
            v = health.get(key)
            if v is not None and v < low + 2:
                flag('WARN', 'probe_floor', f'{key} = {v:.1f} is close to its nonlinear threshold {low}')
        if health['mimics'] and health['mimic_crossings'] > max(1, 0.3 * health['mimics']):
            flag('WARN', 'mimics', f"{health['mimic_crossings']} of {health['mimics']} benign mimics crossed a target")
        if health['crosstalk_crossings'] > 3:
            flag('WARN', 'crosstalk', f"{health['crosstalk_crossings']} unlabelled crossings of other intents inside "
                                      'event windows')
        flat = [c for c, v in health['kpi_std'].items() if not v or v < 1e-6]
        if flat:
            flag('BAD', 'flat_kpis', f'KPIs without any variation: {flat}')
    types_low = [k for k, g in out.items() if g['n'] >= 2 and g['observed_frac'] < 0.5]
    if types_low:
        flag('WARN', 'low_observed', f'event types with < 50 % observed failures: {types_low} (severity adapts '
                                     'automatically)')
    verdict = 'BAD' if bad else ('WARN' if warn else 'OK')
    res = {'events': out, 'health': health, 'verdict': verdict, 'bad': bad, 'warn': warn, 'codes': codes}
    # ---------------- salvage: good windows of a BAD segment stay usable
    if verdict == 'BAD' and len(rows_df) and 'flat_kpis' not in [c for c, lv in codes.items() if lv == 'BAD']:
        tv = rows_df['t'].values

        def labels_ok(a, b):
            w = rows_df[(tv >= a) & (tv < b)]
            qq = w[[not any(e['start'] <= t < e['end'] + rmax for e in plan) for t in w['t'].values]]
            s2 = slo_of(qq)
            return max([v for v in s2.values() if v is not None] or [0.0]) <= 0.05
        res['good_windows'] = salvage_windows(rows_df, good, plan, labels_ok, cfg)
        res['salvaged_ticks'] = int(sum(b - a for a, b in res['good_windows']))
    return res


SCALE_FOR_TYPE = {'independent_api_latency': 'api', 'conflicting_api_cause': 'api',
                  'independent_analytics_slow': 'analytics', 'conflicting_analytics_cause': 'analytics',
                  'independent_telemetry_slow': 'telemetry', 'conflicting_victim': 'victim'}
NL_FRAC = {'nonlinear_api_xor': 'nl_api', 'nonlinear_analytics_xor': 'nl_analytics',
           'nonlinear_telemetry_xor': 'nl_telemetry'}


def adapt_scales(scales, qa_list, bounds, log_path):
    """Closed-loop severity adaptation between segments (failures observable, not instantaneous)."""
    agg = {}
    mim_n = mim_x = 0
    for qa in qa_list:
        for ty, g in (qa.get('events') or {}).items():
            a = agg.setdefault(ty, {'n': 0, 'observed': 0, 'svc_hit': 0, 'net_hit': 0, 'cross': [], 'missing': 0.0})
            for k in ('n', 'observed', 'svc_hit', 'net_hit'):
                a[k] += g[k]
            a['missing'] += float(g.get('missing', 0.0))
            if g['median_cross'] is not None:
                a['cross'].append(g['median_cross'])
        h = qa.get('health') or {}
        mim_n += h.get('mimics', 0)
        mim_x += h.get('mimic_crossings', 0)
    new = dict(scales)
    for ty, a in agg.items():
        if a['n'] < 3:
            continue
        if a['missing'] / a['n'] > 0.2:
            util.log(f'adaptive severity: {ty} skipped (KPI missing in {100 * a["missing"] / a["n"]:.0f} % of its ticks)',
                     'WARN')
            continue
        of = a['observed'] / a['n']
        mc = float(np.median(a['cross'])) if a['cross'] else None
        if ty in SCALE_FOR_TYPE:
            key = SCALE_FOR_TYPE[ty]
            if of < 0.8:
                new[key] = min(bounds[key][1], new[key] * 1.15)
            elif mc is not None and mc < 0.35:
                new[key] = max(bounds[key][0], new[key] * 0.9)
        elif ty in NL_FRAC:
            if of < 0.8:
                sv, nt = a['svc_hit'] / a['n'] >= 0.8, a['net_hit'] / a['n'] >= 0.8
                if not sv or (sv and nt):           # both parts reached but rarely together: strengthen both
                    new[NL_FRAC[ty]] = min(1.3, new[NL_FRAC[ty]] * 1.1)
                if not nt or (sv and nt):
                    new['delay_ms'] = min(bounds['delay_ms'][1], new['delay_ms'] * 1.1)
                    new['loss_pct'] = min(bounds['loss_pct'][1], new['loss_pct'] * 1.1)
    if mim_n >= 4 and mim_x / mim_n > 0.3:
        new['mimic'] = max(0.1, new['mimic'] * 0.85)
    changes = {k: (round(scales[k], 4), round(new[k], 4)) for k in new if abs(new[k] - scales.get(k, new[k])) > 1e-9}
    if changes:
        util.log(f'adaptive severity update: {changes}')
        hist = util.load_json(log_path, [])
        hist.append({'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'changes': changes})
        util.save_json(log_path, hist)
    return new
