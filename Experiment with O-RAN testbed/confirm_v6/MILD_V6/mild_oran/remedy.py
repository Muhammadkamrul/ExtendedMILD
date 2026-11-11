"""remedy.py -- the collector's self-repair: mini-QA every 5 min, a remedy ladder after every segment,
escalation to the user only when the ladder is exhausted.

Three layers (all automatic; every action is appended to <state>/remedies.jsonl):

1. Mini-QA, every qa.check_every_ticks ticks (75 ticks = 5 min) on the last 75 ticks:
     missing KPIs     one component's KPI missing in > 30 % of the window      -> restart that component in place
                                                                                  (mid-segment; its ticks are NaN)
                      ... still missing at the next check                     -> cut the segment (stack restart)
     E2 stale         indications stale in > 30 % of the window while the
                      RAN processes are alive                                 -> rotate the E2 front ends in place
                      ... still stale at the next check                       -> cut (RAN restart)
     tick overrun     > 20 % of ticks late                                     -> lower the background load now
                                                                                  (tuning; applied at the next start)
     calibration      healthy-time target violations > 10 % of quiet ticks    -> noted for the segment end
     radio            radio speed < ran.radio_speed_min                        -> noted (RAN restart at the end)
2. After every segment (30 min), from the segment QA codes (collector.QA_CODES), the next rung of the
   ladder of every BAD code is executed before the next segment starts:
     missing_kpis   restart stack -> restart RAN + stack -> lower load
     tick_overrun   lower load -> lower load more -> restart RAN + stack
     e2_stale       rotate E2 front ends -> restart RAN + stack -> restart RAN with longer report period guard
     radio_slow     restart RAN + stack -> lower load -> restart RAN + stack
     ue_probes      re-map UEs + restart stack -> restart RAN + stack -> restart RAN + stack
     healthy_slo    re-derive targets from the last healthy ticks (calibration self-correction) -> lower load
                    -> redo the baseline calibration step
     flat_kpis      restart stack -> restart RAN + stack -> escalate
   WARN codes are calibration/tuning issues and are corrected silently, without counting as an attempt:
     probe_floor    refresh the per-UE probe floor from the last healthy ticks
     mimics, low_observed, crosstalk   severity adaptation (collector.adapt_scales)
3. Escalation: a BAD code that is still BAD after qa.max_remedy_attempts rungs -> ESCALATION.md in the
   state folder (what was seen, what was tried, what to do), and the collection stops (unless --keep_going).
Nothing is ever deleted: BAD segments stay on disk and their good windows are salvaged (collector).
"""
from __future__ import annotations

import json
import math
import os
import time

import numpy as np

from . import util

LADDER = {
    'missing_kpis': ['restart_stack', 'restart_ran', 'reduce_load'],
    'tick_overrun': ['reduce_load', 'reduce_load', 'restart_ran'],
    'e2_stale': ['rotate_e2', 'restart_ran', 'restart_ran'],
    'radio_slow': ['restart_ran', 'reduce_load', 'restart_ran'],
    'ue_probes': ['remap_restart', 'restart_ran', 'restart_ran'],
    'healthy_slo': ['retarget', 'reduce_load', 'rebaseline'],
    'flat_kpis': ['restart_stack', 'restart_ran', 'escalate'],
}
KPI_ROLE = {'api_latency': 'xrc', 'analytics_tput': 'xsla', 'telemetry_queue': 'xkpm', 'storage_pct': 'xkpm'}
LOAD_KEYS = (('api_rps', 0.8, 1.0), ('dash_rps', 0.8, 0.25), ('anl_sched_rate', 0.85, 2.0))


def _nan(v):
    return v is None or (isinstance(v, float) and math.isnan(v))


class Doctor:
    def __init__(self, cfg, state_dir, targets_fn, keep_going=False):
        self.cfg = cfg
        self.st = state_dir
        self.targets_fn = targets_fn                 # () -> current targets
        self.keep_going = keep_going
        self.path = os.path.join(state_dir, 'remedy_state.json')
        self.s = util.load_json(self.path, {}) or {}
        self.s.setdefault('rungs', {})               # code -> next rung index
        self.s.setdefault('tuning', {})              # ric.* overrides chosen by reduce_load
        self.s.setdefault('history', [])
        self.stack = None
        self.pending = set()
        self.strikes = {}

    # ------------------------------------------------------------------ bookkeeping
    def _save(self):
        util.save_json(self.path, self.s)

    def log(self, what, **kw):
        rec = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'what': what, **kw}
        with open(os.path.join(self.st, 'remedies.jsonl'), 'a') as f:
            f.write(json.dumps(rec, default=str) + '\n')
        util.log(f'REMEDY: {what} ' + ' '.join(f'{k}={v}' for k, v in kw.items()), 'WARN')

    def tuning(self):
        return dict(self.s['tuning'])

    # ------------------------------------------------------------------ 1. mini-QA (inside a segment)
    def mini_check(self, seg, t, win, plan):
        """win = [(row, truth)] of the last check_every_ticks ticks. Fixes in place; returns a cut dict or None."""
        if not win:
            return None
        cfg, ran = self.cfg, self.cfg['ran']
        rows = [r for r, _ in win]
        tick = float(cfg['tick_s'])
        n = len(rows)
        rec = {'segment': seg, 't': t, 'ticks': n}
        # missing KPIs per component
        miss = {}
        for kpi, role in KPI_ROLE.items():
            frac = sum(1 for r in rows if _nan(r.get(kpi))) / n
            if frac > 0.3:
                miss[role] = round(frac, 2)
        rec['missing'] = miss
        overrun = sum(1 for _, tr in win if tr['tick_wall_s'] > 1.1 * tick) / n
        rec['overrun_frac'] = round(overrun, 3)
        stale = sum(1 for r in rows if not _nan(r.get('e2_age_s')) and r['e2_age_s'] > float(ran.get('e2_stale_s', 10))) / n
        rec['e2_stale_frac'] = round(stale, 3)
        sp = [r['radio_speed'] for r in rows if not _nan(r.get('radio_speed'))]
        rec['radio_speed'] = round(float(np.median(sp)), 3) if sp else None
        rmax = int(cfg.get('recovery_max', 10)) + 2
        tg = self.targets_fn()
        quiet = [r for r in rows if not any(e['start'] <= r['t'] < e['end'] + rmax for e in plan)]
        viol = None
        if len(quiet) >= 20:
            viol = max(np.mean([(not _nan(r['api_latency'])) and r['api_latency'] >= tg['api_p95_ms'] for r in quiet]),
                       np.mean([(not _nan(r['analytics_tput'])) and r['analytics_tput'] <= tg['analytics_tput_min']
                                for r in quiet]),
                       np.mean([(not _nan(r['telemetry_queue'])) and r['telemetry_queue'] >= tg['telemetry_q_max']
                                for r in quiet]))
        rec['healthy_violation'] = None if viol is None else round(float(viol), 3)
        with open(os.path.join(self.st, 'segments', f'seg_{seg:04d}', 'minqa.jsonl'), 'a') as f:
            f.write(json.dumps(rec) + '\n')
        actions = []
        # --- components: restart in place once, cut on the second strike
        for role, frac in miss.items():
            k = ('miss', role)
            self.strikes[k] = self.strikes.get(k, 0) + 1
            if self.strikes[k] >= 2:
                self.log('mini-QA cut: component still not answering', segment=seg, role=role, missing_frac=frac)
                return {'why': f'{role} KPI missing in {100 * frac:.0f} % of the last {n} ticks twice in a row',
                        'code': 'missing_kpis', 'since': t - n + 1}
            if self.stack is not None and hasattr(self.stack, 'restart_role'):
                try:
                    self.stack.restart_role(role)
                    actions.append(f'restarted {role}')
                except Exception as e:
                    return {'why': f'restart of {role} failed: {e}', 'code': 'missing_kpis', 'since': t - n + 1}
        for key in [x for x in self.strikes if isinstance(x, tuple) and x[0] == 'miss' and x[1] not in miss]:
            self.strikes.pop(key, None)
        # --- E2 stale: rotate the E2 front ends once, cut (RAN restart) on the second strike
        if stale > 0.3:
            self.strikes['e2'] = self.strikes.get('e2', 0) + 1
            if self.strikes['e2'] >= 2:
                self.log('mini-QA cut: E2 still stale after rotating the E2 front ends', segment=seg)
                return {'why': f'E2 indications stale in {100 * stale:.0f} % of the last {n} ticks twice in a row',
                        'code': 'e2_stale', 'since': t - n + 1, 'ran': True}
            if self.stack is not None and hasattr(self.stack, 'rotate_e2'):
                self.stack.rotate_e2(wait=False)
                actions.append('rotated E2 front ends')
        else:
            self.strikes.pop('e2', None)
        # --- tuning and calibration: never cut, decided now, applied at the next (re)start / segment end
        if overrun > 0.2 and self.s.get('reduced_seg') != seg:          # at most once per segment
            self._reduce_load(f'{100 * overrun:.0f} % late ticks in the last {n} ticks')
            self.s['reduced_seg'] = seg
            self._save()
            actions.append('background load lowered (next start)')
        if viol is not None and viol > 0.10:
            self.pending.add('retarget')
            actions.append('targets will be re-derived at the segment end')
        if rec['radio_speed'] is not None and rec['radio_speed'] < float(ran.get('radio_speed_min', 0.5)):
            self.pending.add('restart_ran')
            actions.append('RAN restart at the segment end (radio too slow)')
        if actions:
            self.log('mini-QA', segment=seg, t=t, actions=actions, observed=rec)
        return None

    # ------------------------------------------------------------------ 2. after a segment
    def _reduce_load(self, why):
        ric = self.cfg['ric']
        tun = self.s['tuning']
        for key, f, lo in LOAD_KEYS:
            cur = float(tun.get(key, ric.get(key, 0)) or 0)
            if cur > lo:
                tun[key] = round(max(lo, cur * f), 3)
        self._save()
        self.log('reduce_load', why=why, tuning=tun)

    def plan_after_segment(self, seg, qa):
        """Returns the list of actions to run before the next segment (and escalations)."""
        acts, esc = set(self.pending), []
        self.pending = set()
        codes = qa.get('codes') or {}
        bad = [c for c, lv in codes.items() if lv == 'BAD']
        mx = int(self.cfg['qa'].get('max_remedy_attempts', 3))
        for c in bad:
            ladder = LADDER.get(c)
            if not ladder:
                continue
            r = int(self.s['rungs'].get(c, 0))
            if r >= mx or r >= len(ladder) or ladder[r] == 'escalate':
                esc.append(c)
                continue
            acts.add(ladder[r])
            self.s['rungs'][c] = r + 1
        if 'reduce_load' in acts and self.s.get('reduced_seg') == seg:
            acts.discard('reduce_load')                 # the mini-QA already lowered the load in this segment
            acts.add('restart_stack')                   # ... it takes effect when the components restart
        for c in list(self.s['rungs']):                 # a code that is no longer BAD resets its ladder
            if c not in bad:
                self.s['rungs'].pop(c)
        if codes.get('probe_floor') == 'WARN':
            acts.add('refresh_probe_floor')
        self.s['history'].append({'segment': seg, 'verdict': qa.get('verdict'), 'codes': codes,
                                  'actions': sorted(acts), 'escalate': esc})
        self.s['history'] = self.s['history'][-200:]
        self._save()
        if acts or esc:
            self.log('after segment', segment=seg, verdict=qa.get('verdict'), bad=bad, actions=sorted(acts),
                     escalate=esc)
        return sorted(acts), esc

    def reduce_load_now(self, why):
        self._reduce_load(why)

    # ------------------------------------------------------------------ calibration self-correction helpers
    @staticmethod
    def retarget(state_dir, store, cfg, cal, n_seg=2):
        """Re-derive the 'auto' targets from the healthy ticks of the last n_seg segments (same rules as the
        baseline calibration step). Returns the new targets (or None if not enough healthy data)."""
        import pandas as pd
        rmax = int(cfg.get('recovery_max', 10)) + 2
        frames = []
        for i in store.list()[-n_seg:]:
            d = store.path(i)
            try:
                df = pd.read_csv(os.path.join(d, 'rows.csv'))
            except Exception:
                continue
            plan = util.load_json(os.path.join(d, 'plan.json'), {}).get('plan', [])
            q = df[[not any(e['start'] <= t < e['end'] + rmax for e in plan) for t in df['t'].values]]
            frames.append(q.iloc[10:])
        if not frames:
            return None
        q = pd.concat(frames)
        if len(q) < 60:
            return None
        tg = dict(cal['targets'])
        t0 = cfg['targets']
        p99 = lambda c: float(q[c].dropna().quantile(0.99)) if q[c].notna().any() else None  # noqa: E731
        p01 = lambda c: float(q[c].dropna().quantile(0.01)) if q[c].notna().any() else None  # noqa: E731
        if p99('api_latency') is not None:
            tg['api_p95_ms'] = round(max(float(t0['api_p95_ms']), 3.0 * p99('api_latency'), tg['api_p95_ms']), 1)
        if t0.get('analytics_tput_min') in (None, 'auto') and p01('analytics_tput') is not None:
            tg['analytics_tput_min'] = round(min(tg['analytics_tput_min'], 0.6 * p01('analytics_tput')), 3)
        if t0.get('telemetry_q_max') in (None, 'auto') and p99('telemetry_queue') is not None:
            tg['telemetry_q_max'] = float(max(tg['telemetry_q_max'], float(cfg.get('telemetry_q_min', 25)),
                                              4 * p99('telemetry_queue') + 10))
        return tg

    @staticmethod
    def probe_floor(store, n_seg=1):
        """Per-UE healthy probe floor is not stored per tick; the cell-wide floor is re-derived from the healthy
        ticks' median RTT (ue_rtt_ms is the mean over UEs). Returns {'rtt_ms': x} or None."""
        import pandas as pd
        vals = []
        for i in store.list()[-n_seg:]:
            d = store.path(i)
            try:
                df = pd.read_csv(os.path.join(d, 'rows.csv'))
            except Exception:
                continue
            plan = util.load_json(os.path.join(d, 'plan.json'), {}).get('plan', [])
            q = df[[not any(e['start'] <= t < e['end'] + 12 for e in plan) for t in df['t'].values]]
            vals += list(q['ue_rtt_ms'].dropna().values)
        if len(vals) < 50:
            return None
        return {'rtt_ms': round(float(np.percentile(vals, 20)), 3)}

    # ------------------------------------------------------------------ 3. escalation
    def escalate(self, codes, qa, store):
        p = os.path.join(self.st, 'ESCALATION.md')
        from .collector import QA_CODES
        L = ['# MILD collection needs you', '', f"time: {time.strftime('%Y-%m-%d %H:%M:%S')}", '',
             'The collector tried to fix the problems below by itself and could not. The collection has stopped '
             '(nothing collected so far is lost; BAD segments are kept and their good windows are salvaged).', '']
        for c in codes:
            L += [f'## {c}: {QA_CODES.get(c, c)}', '', 'What was tried (remedies.jsonl has every step):', '']
            L += [f"- segment {h['segment']}: {h['actions']}" for h in self.s['history'] if c in (h.get('codes') or {})][-6:]
            L += ['', 'Latest QA reasons:', ''] + [f'- {r}' for r in qa.get('bad', [])] + ['', 'What to do: ' +
                                                                                       ADVICE.get(c, 'see the runbook M12.'), '']
        L += ['When fixed, run the same `mild-oran collect` command again: the ladder starts from the first rung.',
              'If you want to start a clean attempt (keeping what was collected for later use): '
              '`mild-oran collect --new-attempt`.']
        with open(p, 'w') as f:
            f.write('\n'.join(L) + '\n')
        for c in codes:
            self.s['rungs'].pop(c, None)
        self._save()
        self.log('ESCALATION', codes=codes, file=p)
        return p


ADVICE = {
    'missing_kpis': 'look at the newest run folder logs (mild-oran logs xkpm|xsla|xrc|ue_agent_ue1 ...) for a '
                    'Python traceback; check free memory (free -h) and disk (df -h ~).',
    'tick_overrun': 'the laptop is too busy: close other programs, check WSL CPUs (nproc >= 8) and the Windows power '
                    'plan; then `mild-oran collect --set ric.api_rps=3`.',
    'e2_stale': 'check ~/intact/logs/run/ric.out and gnb.out for E2 errors; run `mild-oran ran restart` and '
                '`mild-oran preflight --ran`.',
    'radio_slow': 'the ZMQ broker runs far slower than real time: fewer UEs (MU_N) or more WSL CPUs.',
    'ue_probes': 'a UE keeps detaching: `intact-multiue status`, and the srsUE PRACH fix (testbed runbook E.3b).',
    'healthy_slo': 'healthy operation keeps violating the targets even after re-deriving them: '
                   '`mild-oran collect --recalibrate`.',
    'flat_kpis': 'a KPI never changes: `mild-oran status` shows which; usually a component that hangs or a probe '
                 'that never gets a reply.',
}
