"""
collector.py -- tick loop, failure-time detection, resumable segments, segment QA.

One tick (= one time step of the dataset) lasts tick_s seconds of wall clock.
During a tick the driver applies the fault knobs of all active events, reads
the 8 KPIs every sample_s seconds, and stores their mean. KPIs that could not
be measured are stored as NaN and counted in n_missing. They are never
replaced by healthy values.

Failure time of a labelled event (intent compliance predicate):
  API        p95 latency >= api_p95_ms                       (nonlinear: AND sri <= sri_low)
  Analytics  throughput  <= analytics_tput_min               (nonlinear: AND snet <= snet_low)
  Telemetry  queue       >= telemetry_q_max                  (nonlinear: AND sri <= sri_low)
The predicate must hold for `persist` consecutive ticks inside the event
window. The failure time is the tick at which this persistence is reached,
which is the same rule as the original collector. If the predicate never
holds, the event is kept with failure_observed = false and
failure_time = end - 1. The export step then decides how to use it (see
quality.py).

Each segment is written incrementally:
  segments/seg_XXXX/rows.csv       one row per tick, flushed every tick
  segments/seg_XXXX/truth.csv      knob values and active events per tick (audit only, never a feature)
  segments/seg_XXXX/events.jsonl   one line per closed labelled event
  segments/seg_XXXX/plan.json, meta.json
After an interruption, the unfinished segment is cut at the last quiet tick
(no event window open), so every kept event is complete.
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
from .kpi import KPI_NAMES
from .scheduler import build_plan, knobs_for_tick

ROW_COLS = ['t'] + KPI_NAMES + ['cpu_delta', 'sri_delta', 'n_missing']


def predicate_parts(intent, typ, k, tg):
    """Return (service_part, network_part, holds) for one labelled event."""
    def ok(v):
        return v is not None and not (isinstance(v, float) and math.isnan(v))
    if intent == 'api':
        svc = ok(k['api_latency']) and k['api_latency'] >= tg['api_p95_ms']
        net = ok(k['sri']) and k['sri'] <= tg['sri_low']
    elif intent == 'analytics':
        svc = ok(k['analytics_tput']) and k['analytics_tput'] <= tg['analytics_tput_min']
        net = ok(k['snet']) and k['snet'] <= tg['snet_low']
    else:
        svc = ok(k['telemetry_queue']) and k['telemetry_queue'] >= tg['telemetry_q_max']
        net = ok(k['sri']) and k['sri'] <= tg['sri_low']
    holds = (svc and net) if typ.startswith('nonlinear') else svc
    return svc, net, holds


class TickRunner:
    def __init__(self, stack, cfg, targets, scales):
        self.stack, self.cfg, self.tg, self.sc = stack, cfg, targets, scales
        self.prev = {'cpu_pct': None, 'sri': None, 'telemetry_queue': None}
        # responsiveness watchdog (see watch())
        self.bad_run, self.bad_quiet, self.bad_start = 0, 0, None
        self.unhealthy = None

    def watch(self, t, row, plan):
        """Detect a testbed that stopped responding (the 'API jam' of the first version).

        A tick is bad when a KPI could not be measured or the reachability index
        collapsed (sri < 5). A long run of bad ticks that also covers quiet time
        (no injection window or recovery tail open) cannot be explained by an
        injected fault. The segment is then stopped, cut before the run, and the
        testbed is restarted.
        """
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
        return self.unhealthy

    def tick(self, t, plan):
        t0 = time.time()                     # the tick period includes the knob update (constant tick length)
        if t % 25 == 0 and hasattr(self.stack, 'sent'):
            self.stack.sent = {}             # periodically re-send every knob (guards against a lost update)
        k = knobs_for_tick(t, plan, self.sc, self.cfg['ramp_power'])
        tq = self.prev.get('telemetry_queue')
        drain = (not k['telemetry_active']) and tq is not None and tq > 0.5 * self.tg['telemetry_q_max']
        if drain:                            # operator remediation between episodes (scale-out)
            k['telemetry'] = min(k['telemetry'], self.cfg['drain_mult'])
        self.stack.apply(k)
        samples = {n: [] for n in KPI_NAMES}
        while time.time() - t0 < self.cfg['tick_s']:
            r = self.stack.read_kpis()
            for n in KPI_NAMES:
                v = r.get(n)
                if v is not None and not math.isnan(v):
                    samples[n].append(v)
            time.sleep(max(0.0, min(self.cfg['sample_s'] - 0.05, self.cfg['tick_s'] - (time.time() - t0))))
        row = {'t': t}
        for n in KPI_NAMES:
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
        wall = round(time.time() - t0, 3)
        truth = {'t': t, 'tick_wall_s': wall, **{f'knob_{x}': round(float(k[x]), 4) for x in
                            ('api', 'analytics', 'telemetry', 'burst', 'cross', 'delay_ms', 'loss_pct')},
                 'drain': int(drain),
                 'active': ';'.join(e['type'] for e in plan if e['start'] <= t < e['end'])}
        return row, truth


def recovered(intent, typ, k, tg):
    """True when the intent is clearly healthy again (used to close an episode window)."""
    def ok(v):
        return v is not None and not (isinstance(v, float) and math.isnan(v))
    if intent == 'api':
        svc = ok(k['api_latency']) and k['api_latency'] < 0.5 * tg['api_p95_ms']
        net = ok(k['sri']) and k['sri'] > tg['sri_low'] + 2
    elif intent == 'analytics':
        svc = ok(k['analytics_tput']) and k['analytics_tput'] > 1.25 * tg['analytics_tput_min']
        net = ok(k['snet']) and k['snet'] > tg['snet_low'] + 2
    else:
        svc = ok(k['telemetry_queue']) and k['telemetry_queue'] < 0.5 * tg['telemetry_q_max']
        net = ok(k['sri']) and k['sri'] > tg['sri_low'] + 2
    return svc and (net or not typ.startswith('nonlinear'))


INTENT_KPI = {'api': 'api_latency', 'analytics': 'analytics_tput', 'telemetry': 'telemetry_queue'}


def _parts_out(p):
    p = p or {'svc_hit': False, 'net_hit': False, '_n': 0, '_miss': 0}
    return {'svc_hit': p['svc_hit'], 'net_hit': p['net_hit'],
            'kpi_missing_frac': round(p['_miss'] / max(p['_n'], 1), 3)}


def run_ticks(runner, plan, labels, t_from, t_to, on_row=None, on_event=None, should_stop=None):
    """Run ticks [t_from, t_to). Labelled events are evaluated and closed here.

    An episode window runs from the fault onset until the intent has recovered
    (2 consecutive healthy ticks after the injection ended), capped at
    recovery_max ticks after the injection end. The injection end is kept as
    'inject_end'. Without this, the drain of a queue after an episode would
    look like unexplained failures in "healthy" time.
    """
    streak, done, parts = {}, {}, {}
    lab = [(intent, e) for intent, L in labels.items() for e in L]
    closed, pending = set(), {}
    rmax = runner.cfg.get('recovery_max', 10)

    def emit(key, rec, end_tick):
        rec['end'] = int(end_tick)
        if on_event:
            on_event(rec)
        closed.add(key)

    for t in range(t_from, t_to):
        if should_stop and should_stop():
            return False
        row, truth = runner.tick(t, plan)
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
                    emit(key, rec, (t - 1) if good >= 2 else (t + 1))   # exclusive end: first healthy tick, or the cap
                    pending.pop(key)
        if on_row:
            on_row(row, truth)
        if runner.watch(t, row, plan):
            util.log(f'WATCHDOG: testbed unresponsive {runner.unhealthy}; stopping this segment', 'WARN')
            return False
    for key, (rec, good) in list(pending.items()):
        emit(key, rec, t_to)
    return True


# =============================================================================
class SegmentStore:
    def __init__(self, root):
        self.root = os.path.join(root, 'segments')
        os.makedirs(self.root, exist_ok=True)

    def path(self, i):
        return os.path.join(self.root, f'seg_{i:04d}')

    def list(self):
        return sorted(int(d[4:]) for d in os.listdir(self.root) if d.startswith('seg_'))

    def meta(self, i):
        return util.load_json(os.path.join(self.path(i), 'meta.json'), {})

    def finished(self):
        return [i for i in self.list() if self.meta(i).get('status') in ('complete', 'truncated')]

    def total_ticks(self):
        """Usable ticks: finished segments minus the intervals excluded by salvage.py."""
        return sum(self.meta(i).get('ticks', 0) - self.meta(i).get('excluded_ticks', 0) for i in self.finished())

    def repair(self, i, min_ticks, upto=None):
        """Cut an interrupted segment at the last quiet tick (at or before `upto` if given)."""
        d = self.path(i)
        m = self.meta(i)
        plan = util.load_json(os.path.join(d, 'plan.json'), {}).get('plan', [])
        rows_p = os.path.join(d, 'rows.csv')
        if not os.path.exists(rows_p):
            util.log(f'segment {i}: no rows, removing')
            m['status'] = 'discarded'
            util.save_json(os.path.join(d, 'meta.json'), m)
            return
        with open(rows_p) as f:
            rows = list(csv.DictReader(f))
        n = len(rows)
        cut = n if upto is None else max(0, min(n, int(upto)))
        rmax = int(m.get('recovery_max', 10)) + 2       # also exclude recovery tails that were still open
        while cut > 0 and any(e['start'] < cut < e['end'] + rmax for e in plan):
            cut -= 1
        if cut < min_ticks:
            util.log(f'segment {i}: only {cut} usable ticks after cut, discarding')
            m['status'] = 'discarded'
            util.save_json(os.path.join(d, 'meta.json'), m)
            return
        for name in ('rows.csv', 'truth.csv'):
            p = os.path.join(d, name)
            if os.path.exists(p):
                with open(p) as f:
                    r = list(csv.reader(f))
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
        util.log(f'segment {i}: interrupted at tick {n}, kept {cut} ticks and {len(ev)} complete events')


def run_segment(stack, cfg, targets, scales, store: SegmentStore, i, should_stop):
    d = store.path(i)
    os.makedirs(d, exist_ok=True)
    seed = cfg['base_seed'] + i
    ticks = cfg['segment_ticks']
    plan, labels = build_plan(cfg, seed, ticks)
    util.save_json(os.path.join(d, 'plan.json'), {'seed': seed, 'plan': plan, 'labels': labels})
    if hasattr(stack, 'rotate_logs'):
        stack.rotate_logs()
    meta = {'segment': i, 'seed': seed, 'status': 'running', 'code_version': 3, 'started': time.strftime('%Y-%m-%d %H:%M:%S'),
            'recovery_max': cfg.get('recovery_max', 10),
            'tick_s': cfg['tick_s'], 'targets': targets, 'scales': scales, 'stack': stack.manifest()}
    util.save_json(os.path.join(d, 'meta.json'), meta)
    fr = open(os.path.join(d, 'rows.csv'), 'w', newline='')
    ft = open(os.path.join(d, 'truth.csv'), 'w', newline='')
    fe = open(os.path.join(d, 'events.jsonl'), 'w')
    wr = csv.DictWriter(fr, fieldnames=ROW_COLS)
    wt = None
    wr.writeheader()
    state = {'n': 0, 'last_print': time.time()}

    def on_row(row, truth):
        nonlocal wt
        wr.writerow({c: row.get(c) for c in ROW_COLS})
        fr.flush()
        if wt is None:
            wt = csv.DictWriter(ft, fieldnames=list(truth.keys()))
            wt.writeheader()
        wt.writerow(truth)
        ft.flush()
        state['n'] += 1
        if time.time() - state['last_print'] > 60:
            state['last_print'] = time.time()
            util.log(f"seg {i} t={row['t']}/{ticks} api={row['api_latency']:.0f}ms tput={row['analytics_tput']:.2f} "
                     f"tq={row['telemetry_queue']:.0f} cpu={row['cpu_pct']:.1f} ram={row['ram_pct']:.1f} "
                     f"disk={row['storage_pct']:.1f} snet={row['snet']:.1f} sri={row['sri']:.1f} [{truth['active']}]")

    def on_event(rec):
        fe.write(json.dumps(rec) + '\n')
        fe.flush()

    runner = TickRunner(stack, cfg, targets, scales)
    stack.reset()
    ok = run_ticks(runner, plan, labels, 0, ticks, on_row, on_event, should_stop)
    for f in (fr, ft, fe):
        f.close()
    if ok:
        meta.update({'status': 'complete', 'ticks': ticks, 'finished': time.strftime('%Y-%m-%d %H:%M:%S'),
                     'knob_send_failures': dict(getattr(stack, 'knob_failures', {}))})
        util.save_json(os.path.join(d, 'meta.json'), meta)
    elif runner.unhealthy:
        m = store.meta(i)
        m['watchdog'] = runner.unhealthy
        util.save_json(os.path.join(d, 'meta.json'), m)
        store.repair(i, cfg['min_segment_ticks'], upto=max(0, runner.unhealthy['bad_since'] - 5))
    else:
        store.repair(i, cfg['min_segment_ticks'])
    return 'unhealthy' if runner.unhealthy else ok


def segment_qa(store: SegmentStore, i):
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
    return out


SCALE_FOR_TYPE = {'independent_api_latency': 'api', 'conflicting_api_cause': 'api',
                  'independent_analytics_slow': 'analytics', 'conflicting_analytics_cause': 'analytics',
                  'independent_telemetry_slow': 'telemetry', 'conflicting_victim': 'victim'}
NL_FRAC = {'nonlinear_api_xor': 'nl_api', 'nonlinear_analytics_xor': 'nl_analytics',
           'nonlinear_telemetry_xor': 'nl_telemetry'}


def adapt_scales(scales, qa_list, bounds, log_path):
    """Closed-loop severity adaptation between segments (keeps failures observable but not instantaneous)."""
    agg = {}
    for qa in qa_list:
        for ty, g in qa.items():
            a = agg.setdefault(ty, {'n': 0, 'observed': 0, 'svc_hit': 0, 'net_hit': 0, 'cross': [], 'missing': 0.0})
            for k in ('n', 'observed', 'svc_hit', 'net_hit'):
                a[k] += g[k]
            a['missing'] += float(g.get('missing', 0.0))
            if g['median_cross'] is not None:
                a['cross'].append(g['median_cross'])
    changes = {}
    new = dict(scales)
    for ty, a in agg.items():
        if a['n'] < 3:
            continue
        if a['missing'] / a['n'] > 0.2:
            # The KPI could not be measured during these episodes. A missed failure is then a
            # measurement problem, not a too-weak fault. Raising the severity here caused the
            # run-away of the API scale in the first version.
            util.log(f'adaptive severity: {ty} skipped (KPI missing in {100 * a["missing"] / a["n"]:.0f} % of its ticks)', 'WARN')
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
                if a['svc_hit'] / a['n'] < 0.8:
                    new[NL_FRAC[ty]] = min(1.2, new[NL_FRAC[ty]] * 1.1)
                if a['net_hit'] / a['n'] < 0.8:
                    new['delay_ms'] = min(bounds['delay_ms'][1], new['delay_ms'] * 1.1)
                    new['loss_pct'] = min(bounds['loss_pct'][1], new['loss_pct'] * 1.1)
    for k in new:
        if abs(new[k] - scales.get(k, new[k])) > 1e-9:
            changes[k] = (round(scales[k], 4), round(new[k], 4))
    if changes:
        util.log(f'adaptive severity update: {changes}')
        hist = util.load_json(log_path, [])
        hist.append({'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'changes': changes})
        util.save_json(log_path, hist)
    return new
