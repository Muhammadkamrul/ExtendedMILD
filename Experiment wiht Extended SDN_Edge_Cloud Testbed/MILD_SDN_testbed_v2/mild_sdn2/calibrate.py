"""
calibrate.py -- self-calibration and self-validation before the long collection.

Step 1  baseline     no faults. Measures the healthy KPI distributions, sets the
                     intent targets, and checks that healthy operation almost
                     never violates them (at most 1 % of ticks per intent).
Step 2  weights      (real testbed only) sweeps the WAN impairment and validates
                     the SNET/SRI scoring weights: monotone response, healthy
                     score high, enough dynamic range to cross the nonlinear
                     thresholds, and no single component dominating. If the
                     range is insufficient, the weights are rescaled uniformly
                     (their ratios are kept) and the sweep is repeated once.
Step 3  faults       one test episode per event type. The severity scale of each
                     fault is adjusted until the intent predicate is crossed
                     at 45-90 % of the window, so failures are observable and
                     leave a learnable build-up. Benign mimics are checked NOT
                     to cross.

Results go to <state>/calibration.json, which is resumable step by step, and
to a human-readable calibration_report.md. Collection starts only when every
step has passed or has been explicitly accepted with a warning.
"""
from __future__ import annotations

import math
import os
import time

import numpy as np

from . import util
from .collector import TickRunner, predicate_parts, run_ticks
from .kpi import KPI_NAMES


class Interrupted(RuntimeError):
    """Raised when the user asked to stop or a testbed process died during calibration."""


SHOULD_STOP = {'fn': lambda: False}


def _guard(ok):
    if not ok:
        raise Interrupted('calibration interrupted (stop requested or a testbed process died); re-run to resume')


def _baseline(stack, cfg, targets, scales, n):
    runner = TickRunner(stack, cfg, targets, scales)
    rows = []
    _guard(run_ticks(runner, [], {'api': [], 'analytics': [], 'telemetry': []}, 0, n, on_row=lambda r, t: rows.append(r),
                     should_stop=SHOULD_STOP['fn']))
    return rows


def step_baseline(stack, cfg, cal):
    if cal.get('baseline', {}).get('passed'):
        return cal
    util.log(f"calibration 1/3: baseline ({cfg['calib_baseline_ticks']} ticks without faults)")
    t0 = dict(cfg['targets'])
    prov = dict(t0)                       # provisional numbers while the baseline is measured
    if prov.get('analytics_tput_min') in (None, 'auto'):
        prov['analytics_tput_min'] = -1.0
    if prov.get('telemetry_q_max') in (None, 'auto'):
        prov['telemetry_q_max'] = float('inf')
    rows = _baseline(stack, cfg, prov, cal['scales'], cfg['calib_baseline_ticks'])[10:]
    arr = {k: np.array([r[k] for r in rows if not math.isnan(r[k])]) for k in KPI_NAMES}
    q = {k: {p: float(np.percentile(v, p)) for p in (1, 5, 50, 95, 99)} if len(v) else {} for k, v in arr.items()}
    tg = dict(t0)
    tg['api_p95_ms'] = max(t0['api_p95_ms'], 3.0 * q['api_latency'].get(99, 0))
    if t0.get('analytics_tput_min') in (None, 'auto'):
        # healthy floor = scheduled inference + telemetry-forwarded jobs (independent of the user load)
        sites = 1 if cfg['mock'] else 2
        floor = cfg['analytics_sched_rate'] + sites * cfg['devices_per_site'] / cfg['device_period_s'] / 20.0
        tg['analytics_tput_min'] = round(0.6 * min(floor, q['analytics_tput'].get(1, floor)), 3)
        tg['analytics_floor_jobs_s'] = round(floor, 3)
    if t0.get('telemetry_q_max') in (None, 'auto'):
        tg['telemetry_q_max'] = float(max(400.0, 4 * q['telemetry_queue'].get(99, 0) + 200))
    viol = {}
    for intent in ('api', 'analytics', 'telemetry'):
        viol[intent] = float(np.mean([predicate_parts(intent, 'independent', r, tg)[2] for r in rows]))
    missing = float(np.mean([r['n_missing'] > 0 for r in rows]))
    passed = all(v <= 0.01 for v in viol.values()) and missing <= 0.05
    issues = []
    if q['snet'].get(5, 100) < tg['snet_low'] + 3:
        issues.append(f"healthy snet p5={q['snet'].get(5):.1f} too close to snet_low={tg['snet_low']}")
    if q['sri'].get(5, 100) < tg['sri_low'] + 3:
        issues.append(f"healthy sri p5={q['sri'].get(5):.1f} too close to sri_low={tg['sri_low']}")
    cal['baseline'] = {'passed': passed, 'quantiles': q, 'healthy_violation_rate': viol, 'missing_rate': missing,
                       'issues': issues}
    cal['targets'] = tg
    util.log(f'baseline: targets={tg} healthy violation rate={viol} missing={missing:.3f} issues={issues}')
    if not passed:
        raise RuntimeError(f'baseline validation failed: healthy violation={viol}, missing={missing}. '
                           'The testbed is overloaded or a KPI is not measured. See calibration.json.')
    return cal


def step_weights(stack, cfg, cal, weights_path):
    if cal.get('weights', {}).get('passed') or cfg['mock']:
        cal.setdefault('weights', {'passed': True, 'note': 'skipped in mock mode' if cfg['mock'] else ''})
        return cal
    util.log('calibration 2/3: SNET/SRI weight validation (WAN impairment sweep)')
    levels = cfg['weight_sweep_levels']          # list of [delay_ms, loss_pct]
    tg = cal['targets']

    def sweep():
        res = []
        for d, l in levels:
            stack.apply({'api': 1, 'analytics': 1, 'telemetry': 1, 'burst': 1, 'cross': 1, 'delay_ms': d, 'loss_pct': l})
            time.sleep(3)
            sn, sr = [], []
            _guard(not SHOULD_STOP['fn']())
            t0 = time.time()
            while time.time() - t0 < cfg['weight_sweep_hold_s']:
                k = stack.read_kpis()
                if k['snet'] is not None:
                    sn.append(k['snet'])
                if k['sri'] is not None:
                    sr.append(k['sri'])
                time.sleep(0.5)
            comp = {}
            kq = getattr(stack, 'kpi', None)
            if kq is not None and hasattr(kq, 'query'):
                for name, e in (('loss', 'max(probe_loss_pct)'), ('rtt', 'max(probe_rtt_ms)'),
                                ('jitter', 'max(probe_jitter_ms)'), ('http', 'min(probe_http_success_pct)')):
                    comp[name] = kq.query(e)
            res.append({'delay_ms': d, 'loss_pct': l, 'snet': float(np.mean(sn)) if sn else None,
                        'sri': float(np.mean(sr)) if sr else None, **comp})
            util.log(f'  sweep d={d} l={l}: snet={res[-1]["snet"]} sri={res[-1]["sri"]} comp={comp}')
        stack.reset()
        time.sleep(3)
        return res

    def assess(res):
        sev = np.arange(len(res))
        sn = np.array([r['snet'] if r['snet'] is not None else np.nan for r in res])
        sr = np.array([r['sri'] if r['sri'] is not None else np.nan for r in res])
        from scipy.stats import spearmanr
        rs, rr = spearmanr(sev, sn).correlation, spearmanr(sev, sr).correlation
        ok = {'snet_monotone': bool(rs <= -0.9), 'sri_monotone': bool(rr <= -0.8),
              'snet_healthy_high': bool(sn[0] >= tg['snet_low'] + 5),
              'sri_healthy_high': bool(sr[0] >= tg['sri_low'] + 5),
              'snet_reaches_threshold': bool(np.nanmin(sn) <= tg['snet_low'] - 5),
              'sri_reaches_threshold': bool(np.nanmin(sr) <= tg['sri_low'] - 5),
              'snet_not_saturated_mid': bool(sn[len(sn) // 2] > 10), 'sri_not_saturated_mid': bool(sr[len(sr) // 2] > 10)}
        return ok, {'spearman_snet': float(rs), 'spearman_sri': float(rr)}

    W = util.load_json(weights_path, cfg['snet_sri_weights'])
    res = sweep()
    ok, corr = assess(res)
    history = [{'weights': W, 'sweep': res, 'checks': ok, **corr}]
    if not (ok['snet_reaches_threshold'] and ok['sri_reaches_threshold']):
        mx = res[-1]
        f_sn = (100 - cfg['weight_target_snet_at_max']) / max(1e-6, 100 - (mx['snet'] or 100))
        f_sr = (100 - cfg['weight_target_sri_at_max']) / max(1e-6, 100 - (mx['sri'] or 100))
        f_sn, f_sr = float(np.clip(f_sn, 1.0, 4.0)), float(np.clip(f_sr, 1.0, 4.0))
        W = {'snet': {k: v * f_sn for k, v in W['snet'].items()},
             'sri': {k: (v * f_sr if k != 'http_fail' else v) for k, v in W['sri'].items()}}
        util.save_json(weights_path, W)
        util.log(f'weights rescaled (snet x{f_sn:.2f}, sri network terms x{f_sr:.2f}); repeating sweep')
        time.sleep(3)
        res = sweep()
        ok, corr = assess(res)
        history.append({'weights': W, 'sweep': res, 'checks': ok, **corr})
    passed = ok['snet_monotone'] and ok['snet_healthy_high'] and ok['sri_healthy_high'] and \
        ok['snet_reaches_threshold'] and ok['sri_reaches_threshold']
    cal['weights'] = {'passed': passed, 'final_weights': W, 'history': history}
    if not passed:
        util.log(f'WARNING: SNET/SRI weight validation not fully passed: {ok}', 'WARN')
    return cal


def _episode(stack, cfg, cal, typ, D):
    intent = {'api': 'api', 'analytics': 'analytics', 'telemetry': 'telemetry'}
    pre, post = 8, cfg['calib_drain_ticks']
    s, e = pre, pre + D
    who = typ.split('_')[1] if not typ.startswith('mimic') else typ.split('_')[1]
    ev = {'intent': who, 'type': typ, 'start': s, 'end': e}
    labels = {'api': [], 'analytics': [], 'telemetry': []}
    if typ.startswith('conflicting'):
        ev.update({'victim_start': s + 5, 'victim_end': e})
        labels['telemetry'].append({'start': s + 5, 'end': e, 'type': 'conflicting_victim'})
    if who == 'network':
        for x in ('api', 'analytics', 'telemetry'):
            labels[x].append({'start': s, 'end': e, 'type': 'mimic'})
    else:
        labels[intent[who]].append({'start': s, 'end': e, 'type': typ})
    out = []
    runner = TickRunner(stack, cfg, cal['targets'], cal['scales'])
    ok = run_ticks(runner, [ev], labels, 0, e + post, on_event=out.append, should_stop=SHOULD_STOP['fn'])
    stack.reset()
    _guard(ok)
    return out


def step_faults(stack, cfg, cal, save):
    util.log('calibration 3/3: fault-response calibration')
    sc, B = cal['scales'], cfg['scale_bounds']
    D = (cfg['dur_min'] + cfg['dur_max']) // 2
    plan = [('independent_api_latency', 'api'), ('independent_analytics_slow', 'analytics'),
            ('independent_telemetry_slow', 'telemetry'), ('conflicting_analytics_cause', 'victim'),
            ('conflicting_api_cause', 'victim'), ('nonlinear_api_xor', 'nl_api'),
            ('nonlinear_analytics_xor', 'nl_analytics'), ('nonlinear_telemetry_xor', 'nl_telemetry'),
            ('mimic_api_only', 'mimic'), ('mimic_analytics_only', 'mimic'), ('mimic_telemetry_only', 'mimic'),
            ('mimic_network_only', 'mimic')]
    res = cal.setdefault('faults', {})
    for typ, key in plan:
        if res.get(typ, {}).get('done'):
            continue
        hist = []
        for it in range(cfg['calib_max_iter']):
            evs = _episode(stack, cfg, cal, typ, D)
            main = [x for x in evs if x['type'] == typ or x['type'] == 'mimic']
            vic = [x for x in evs if x['type'] == 'conflicting_victim']
            target = vic[0] if key == 'victim' and vic else (main[0] if main else None)
            c = target['crossing_frac'] if target and target['failure_observed'] else None
            hist.append({'iter': it, 'scales': {k: round(v, 4) for k, v in sc.items()}, 'events': evs})
            util.log(f'  {typ} iter {it}: crossing={c} parts={target and (target.get("svc_hit"), target.get("net_hit"))}')
            if key == 'mimic':
                crossed = any(x['failure_observed'] for x in evs)
                if crossed:
                    sc['mimic'] = max(0.05, sc['mimic'] * 0.75)
                    continue
                break
            if c is not None and 0.45 <= c <= 0.9:
                break
            if key.startswith('nl_'):
                if c is None:
                    if not target.get('svc_hit'):
                        sc[key] = min(1.3, sc[key] * 1.15)
                    if not target.get('net_hit'):
                        sc['delay_ms'] = min(B['delay_ms'][1], sc['delay_ms'] * 1.25)
                        sc['loss_pct'] = min(B['loss_pct'][1], sc['loss_pct'] * 1.25)
                elif c < 0.45:
                    sc[key] = max(0.3, sc[key] / 1.2)
                continue
            if c is None:
                sc[key] = min(B[key][1], sc[key] * 1.5)
                if key == 'telemetry':
                    sc['burst'] = min(B['burst'][1], sc['burst'] * 1.2)
            elif c < 0.45:
                sc[key] = max(B[key][0], sc[key] / 1.25)
            else:
                sc[key] = min(B[key][1], sc[key] * 1.15)
        final_ok = (c is not None and 0.3 <= c <= 0.95) if key != 'mimic' else not any(x['failure_observed'] for x in evs)
        res[typ] = {'done': True, 'ok': bool(final_ok), 'final_crossing': c, 'history': hist}
        cal['scales'] = sc
        save(cal)
        if not final_ok:
            util.log(f'WARNING: {typ} not calibrated into the target band (last crossing={c}); '
                     'the collector will keep adapting the severity between segments', 'WARN')
    cal['faults_passed'] = all(v['ok'] for v in res.values())
    return cal


def write_report(cal, path):
    L = ['# Calibration report', '', '## Intent targets used for failure labelling', '']
    for k, v in cal['targets'].items():
        L.append(f'- {k}: {v}')
    b = cal.get('baseline', {})
    L += ['', '## Healthy baseline', '', f"- violation rate per intent: {b.get('healthy_violation_rate')}",
          f"- missing-KPI rate: {b.get('missing_rate')}", f"- issues: {b.get('issues')}", '',
          '| KPI | p1 | p5 | p50 | p95 | p99 |', '|---|---|---|---|---|---|']
    for k, q in b.get('quantiles', {}).items():
        if q:
            L.append(f"| {k} | " + ' | '.join(f'{q[p]:.2f}' for p in (1, 5, 50, 95, 99)) + ' |')
    w = cal.get('weights', {})
    L += ['', '## SNET/SRI weight validation', '', f"- passed: {w.get('passed')}",
          f"- final weights: {w.get('final_weights')}"]
    for h in w.get('history', []):
        L.append(f"- checks: {h['checks']} (Spearman snet={h['spearman_snet']:.2f}, sri={h['spearman_sri']:.2f})")
    L += ['', '## Fault-response calibration (crossing fraction of the window)', '']
    for typ, r in cal.get('faults', {}).items():
        L.append(f"- {typ}: ok={r['ok']} final crossing={r['final_crossing']} iterations={len(r['history'])}")
    L += ['', '## Final severity scales', '', f"{cal['scales']}"]
    with open(path, 'w') as f:
        f.write('\n'.join(L))
