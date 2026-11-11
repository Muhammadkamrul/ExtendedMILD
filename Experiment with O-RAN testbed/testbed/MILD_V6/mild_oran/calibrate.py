"""calibrate.py -- self-calibration and self-validation before the long collection.

Step 0  plant      (RAN) offered-load probe: the healthy probe RTT and jitter floor of
                   the radio (ZMQ) at light load, per UE, and the saturated DL throughput of
                   the cell = capacity_mbps (sum of what the N UEs receive; E2SM-KPM cell
                   DRB.UEThpDl and RRU.PrbTotDl recorded next to it). A capacity far below
                   the nominal value means a slice PRB cap is still in force.
Step 0b E2 control (xRC) one E2SM-RC slice-PRB-quota control per UE through the RIC: ACK,
                   FAILURE or shadow mode, and the E2 round-trip time.
Step 1  baseline   no faults for calib_baseline_ticks. Measures the healthy KPI
                   distributions, sets the intent targets, and checks that healthy
                   operation almost never violates them (<= 1 % of ticks per intent)
                   and that the KPIs are measured (<= 5 % ticks with a missing KPI).
Step 2  weights    sweeps the user-plane impairment (tc netem delay/loss) and validates
                   the snet/sri weights: monotone response, healthy scores high, enough
                   range to cross the nonlinear thresholds. If the range is too small the
                   weights are rescaled uniformly (ratios kept) and the sweep repeated once.
Step 3  faults     one test episode per event type; the severity scale of each fault is
                   adjusted until the intent predicate is crossed at 45-90 % of the
                   window (observable, with a learnable build-up). Mimics must NOT cross.

Results: <state>/calibration.json (resumable step by step) and calibration_report.md.
"""
from __future__ import annotations

import math
import os
import time

import numpy as np

from . import util
from .collector import TickRunner, predicate_parts, run_ticks
from .kpi import KPI_NAMES
from .util import mono


class Interrupted(RuntimeError):
    """Raised when the user asked to stop, a process died or the RAN went down during calibration."""


SHOULD_STOP = {'fn': lambda: False}


def _guard(ok, runner=None):
    if not ok:
        why = ''
        if runner is not None and runner.ran_down:
            why = f' (RAN down: {runner.ran_down["why"]})'
        raise Interrupted('calibration interrupted' + why)


def _sleep(s):
    t0 = mono()
    while True:
        if SHOULD_STOP['fn']():
            raise Interrupted('calibration interrupted (stop requested or a MILD component died)')
        rest = s - (mono() - t0)
        if rest <= 0:
            return
        time.sleep(min(0.25, rest))


PLANT_VERSION = 2          # v2.1: the plant step checks that the RAN survived it; v2.0 results are redone


def _ran_alive(stack, cfg, where):
    """The plant and RC steps have no tick watchdog: check the gNB, the RIC and E2 explicitly; a RAN that died during
    a step makes the step's numbers worthless, so the step is redone after an automatic RAN restart."""
    rc = getattr(stack, 'ranctl', None)
    why = []
    if rc is not None:
        try:
            st = rc.status(quick=True)
            if not st.get('gnb'):
                why.append('the gNB process is not running')
            if not st.get('ric'):
                why.append('the nearRT-RIC process is not running')
        except Exception:
            pass
    try:
        age = stack.read_kpis().get('e2_age_s')
        if age is not None and not (isinstance(age, float) and math.isnan(age)) and \
                age > float(cfg['ran'].get('e2_stale_s', 10)):
            why.append(f'no E2SM-KPM indication at xKPM for {age:.0f} s')
    except Exception:
        pass
    if why:
        raise Interrupted(f"calibration interrupted (RAN down during {where}: {'; '.join(why)})")


def step_plant(stack, cfg, cal):
    if cal.get('plant', {}).get('passed') is not None and cal.get('capacity_mbps') and \
            cal['plant'].get('check_version') == PLANT_VERSION:
        stack.set_capacity(cal['capacity_mbps'])
        stack.set_baseline(cal.get('probe_baseline', {}))
        return cal
    util.log('calibration 0/3: plant (per-UE probe floor at light load, saturated DL capacity of the cell)')
    ran = cfg['ran']
    nominal = float(ran.get('capacity_nominal_mbps', 20.0))
    f = float(cfg['tick_s']) / 4.0                  # 1 on the testbed; tiny in the self-test
    smp = float(cfg['sample_s'])
    ues = list(getattr(stack, 'ues', []) or ['ue1'])
    rt = os.path.join(cfg['state_dir'], 'runtime')
    stack.reset()
    # light load: probe floor of every UE
    stack.set_fixed_load(0.08 * nominal)
    _sleep(8 * f)
    rtts, hz = [], []
    per = {u: {'rtt': [], 'jit': []} for u in ues}
    t0 = mono()
    while mono() - t0 < 20 * f:
        k = stack.read_kpis()
        if k.get('ue_rtt_ms') is not None and (k.get('ue_loss_pct') or 0) < 5:
            rtts.append(k['ue_rtt_ms'])
        for u in ues:
            ag = util.load_json(os.path.join(rt, f'ue_agent_{u}.json')) or {}
            r = [x[2] for x in (ag.get('pings') or [])[-40:] if x[2] is not None]
            if len(r) >= 5:
                per[u]['rtt'].append(float(np.median(r)))
                per[u]['jit'].append(sum(abs(b - a) for a, b in zip(r, r[1:])) / (len(r) - 1))
        if k.get('e2_ind_hz'):
            hz.append(k['e2_ind_hz'])
        _sleep(smp)
    base = {'rtt_ms': float(np.median(rtts)) if rtts else 40.0}
    jits = [x for u in ues for x in per[u]['jit']]
    base['jitter_ms'] = float(np.median(jits)) if jits else 5.0
    base['per_ue'] = {u: {'rtt_ms': round(float(np.median(per[u]['rtt'])), 3),
                          'jitter_ms': round(float(np.median(per[u]['jit'])), 3)} for u in ues if per[u]['rtt']}
    _ran_alive(stack, cfg, 'the light-load probe')
    # saturation: capacity of the cell (what the UEs receive; the gNB's KPM view recorded alongside)
    stack.set_fixed_load(float(ran.get('plant_saturation_mult', 1.6)) * nominal)
    _sleep(6 * f)
    sink, kpm, prb = [], [], []
    t0 = mono()
    while mono() - t0 < 12 * f:
        k = stack.read_kpis()
        if k.get('ue_sink_mbps'):
            sink.append(k['ue_sink_mbps'])
        if k.get('ran_dl_mbps') is not None:
            kpm.append(k['ran_dl_mbps'])
        if k.get('ran_prb_dl_pct') is not None:
            prb.append(k['ran_prb_dl_pct'])
        _sleep(smp)
    stack.set_fixed_load(0.0)
    _sleep(10 * f)                              # drain the DL buffers
    stack.set_fixed_load(None)
    _ran_alive(stack, cfg, 'the saturation test')
    top = lambda v: float(np.median(sorted(v)[len(v) // 3:])) if v else None  # noqa: E731
    cap_sink, cap_kpm = top(sink), top(kpm)
    cap = cap_sink if cap_sink and cap_sink > 0.5 else cap_kpm
    issues = []
    if cap is None:
        issues.append(f'no throughput measured during saturation: the nominal {nominal} Mbit/s is used')
        cap = nominal
    ok_cap = cap >= float(ran.get('capacity_min_ok_mbps', 8.0))
    if not ok_cap:
        issues.append(f'saturated DL throughput only {cap:.1f} Mbit/s (expected ~{nominal:.0f}): a slice PRB cap '
                      '(an earlier E2SM-RC control or jbpf slice app) may still be in force -> `mild-oran ran restart`')
    if hz and float(np.median(hz)) < 0.5:
        issues.append(f'only {np.median(hz):.2f} E2 indications/s at xKPM (expected ~1.5-2 with styles 1 and 4)')
    cal['plant'] = {'passed': bool(ok_cap and (sink or kpm)), 'check_version': PLANT_VERSION, 'capacity_sink_mbps': cap_sink,
                    'capacity_kpm_ue_thp_mbps': cap_kpm, 'prb_dl_pct_at_saturation': top(prb),
                    'probe_rtt_samples': len(rtts), 'e2_ind_hz': float(np.median(hz)) if hz else None,
                    'n_ues': len(ues), 'issues': issues,
                    'note': 'E2SM-KPM DRB.UEThpDl is the 3GPP UE throughput (volume / time with data in the '
                            'buffer), so it reads higher than the received rate; capacity uses the received rate.'}
    cal['capacity_mbps'] = round(cap, 3)
    cal['probe_baseline'] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in base.items()}
    stack.set_capacity(cap)
    stack.set_baseline(cal['probe_baseline'])
    util.log(f"plant: capacity {cap:.2f} Mbit/s (UEs receive {cap_sink}, KPM UE throughput {cap_kpm}), probe floor "
             f"{base['rtt_ms']:.1f} ms / jitter {base['jitter_ms']:.1f} ms, per UE {base['per_ue']}"
             + (f'; ISSUES: {issues}' if issues else ''))
    return cal


RC_CHECK_VERSION = 2       # v2.1: strict ACK accounting; results of the v2.0 check are redone


def step_rc(stack, cfg, cal):
    """Three xSLA-style /control requests to xRC: E2SM-RC control per UE of the slice. Passes only when every
    control was answered by a RIC CONTROL ACKNOWLEDGE (or xRC is in shadow mode because the gNB offers no RC)."""
    rc_old = cal.get('rc') or {}
    if (rc_old.get('done') and rc_old.get('check_version') == RC_CHECK_VERSION) or cfg.get('selftest'):
        return cal
    import http.client
    import json as _json
    P = cfg['ports']
    util.log('calibration 0b: E2SM-RC control check (xRC -> FlexRIC -> gNB, slice-level PRB quota)')
    s0 = util.http_get_json('127.0.0.1', P['xrc'] + P['ctl_offset'], '/stats?since=999999999', timeout=2.0) or {}
    um = (getattr(stack, 'ue_map', {}) or {}).get('ue') or {}
    ue = sorted(um)[0] if um else 'ue1'
    res, t0 = [], mono()
    if not s0.get('rc_stuck'):
        for risk in (0.95, 0.65, 0.95):          # boosts 36 / 13 / 36: every request moves the quota by >= 10
            body = _json.dumps({'ue': ue, 'f1ap': (um.get(ue) or {}).get('f1ap'), 'sd': (um.get(ue) or {}).get('sd', 1),
                                'risk': risk, 'reason': 'calibration'}).encode()
            try:
                c = http.client.HTTPConnection('127.0.0.1', P['xrc'], timeout=40)
                c.request('POST', '/control', body=body,
                          headers={'Content-Type': 'application/json', 'X-Send-Mono': f'{mono():.6f}'})
                r = c.getresponse()
                res.append(_json.loads(r.read() or b'{}').get('rc'))
                c.close()
            except Exception as e:
                res.append({'error': str(e)})
            s_mid = util.http_get_json('127.0.0.1', P['xrc'] + P['ctl_offset'], '/stats?since=999999999', timeout=2.0) or {}
            if s_mid.get('rc_stuck'):
                break                                # the gNB stopped answering: no point sending more
            _sleep(float(cfg['ric'].get('rc_min_interval_s', 5.0)) + 1.0)  # one actuation per slice per 5 s
    if not (util.http_get_json('127.0.0.1', P['xrc'] + P['ctl_offset'], '/stats?since=999999999', timeout=2.0)
            or {}).get('rc_stuck'):
        _ran_alive(stack, cfg, 'the E2SM-RC check')
    s1 = util.http_get_json('127.0.0.1', P['xrc'] + P['ctl_offset'], '/stats?since=999999999', timeout=2.0) or {}
    keys = ('rc_sent', 'rc_ack', 'rc_fail', 'rc_timeout', 'rc_shadow', 'rc_skipped_stuck')
    d = {k: int(s1.get(k, 0)) - int(s0.get(k, 0)) for k in keys}
    tot = {k: int(s1.get(k, 0)) for k in keys}
    mode = s1.get('mode')
    stuck = s1.get('rc_stuck')
    if mode == 'shadow':
        ok = True
    else:
        ok = (mode == 'e2rc' and not stuck and d['rc_ack'] >= 1 and d['rc_fail'] == 0 and d['rc_timeout'] == 0
              and tot['rc_timeout'] == 0)
    p50 = s1.get('rc_ms_p50')
    cal['rc'] = {'done': True, 'check_version': RC_CHECK_VERSION, 'mode': mode, 'ok': bool(ok), 'stuck': stuck,
                 'during_check': d, 'since_start': tot, 'responses': res, 'ms': round((mono() - t0) * 1000, 1),
                 'rc_ms_p50': p50, 'rc_ms_p95': s1.get('rc_ms_p95'),
                 'rc_params': ((s1.get('e2') or {}).get('rc_params'))}
    msg = (f"E2SM-RC check: mode {mode}; controls during the check {d}, since start {tot}; round trip p50 "
           f"{p50 if p50 is None else round(p50, 1)} ms; parameter IDs {cal['rc']['rc_params']}")
    if ok:
        util.log(msg + ' -> OK' + (' (shadow mode: the gNB offers no E2SM-RC; no control reaches the gNB)'
                                   if mode == 'shadow' else ': every control was acknowledged by the gNB'))
    else:
        why = (f"the gNB did not answer a control ({stuck})" if stuck else
               'controls were refused (RIC CONTROL FAILURE)' if d['rc_fail'] else
               'no control was acknowledged' if d['rc_ack'] == 0 else 'some controls timed out')
        util.log(msg + f' -> FAILED: {why}', 'WARN')
    return cal


def rc_check_failed(cal):
    """True when the E2SM-RC check ran with real actuation (mode e2rc) and did not pass."""
    rc = cal.get('rc') or {}
    return bool(rc.get('done') and not rc.get('ok') and rc.get('mode') != 'shadow')


def _demand(cfg, n):
    """Scenario v3: the tenant-demand profile also runs during calibration, so that the targets are set on the
    healthy range WITH demand changes and the fault severities are tuned on top of them."""
    if cfg.get('scenario') != 'v3':
        return []
    from .scheduler import build_plan
    plan, _ = build_plan(dict(cfg, events_per_segment=0, mimics_per_10_events=0), cfg['base_seed'] + 90001, max(n, 50))
    return [e for e in plan if e.get('intent') == 'demand']


def _baseline(stack, cfg, targets, scales, n):
    runner = TickRunner(stack, cfg, targets, scales)
    runner.demand = _demand(cfg, n)
    rows = []
    _guard(run_ticks(runner, [], {'api': [], 'analytics': [], 'telemetry': []}, 0, n,
                     on_row=lambda r, t: rows.append(r), should_stop=SHOULD_STOP['fn']), runner)
    return rows


def step_baseline(stack, cfg, cal):
    if cal.get('baseline', {}).get('passed'):
        return cal
    util.log(f"calibration 1/3: baseline ({cfg['calib_baseline_ticks']} ticks without faults, "
             f"~{cfg['calib_baseline_ticks'] * cfg['tick_s'] / 60:.0f} min)")
    t0 = dict(cfg['targets'])
    prov = dict(t0)
    if prov.get('analytics_tput_min') in (None, 'auto'):
        prov['analytics_tput_min'] = -1.0
    if prov.get('telemetry_q_max') in (None, 'auto'):
        prov['telemetry_q_max'] = float('inf')
    rows = _baseline(stack, cfg, prov, cal['scales'], cfg['calib_baseline_ticks'])[10:]
    arr = {k: np.array([r[k] for r in rows if not math.isnan(r[k])]) for k in KPI_NAMES}
    q = {k: {p: float(np.percentile(v, p)) for p in (1, 5, 50, 95, 99)} if len(v) else {} for k, v in arr.items()}
    tg = dict(t0)
    tg['api_p95_ms'] = round(max(float(t0['api_p95_ms']), 3.0 * q['api_latency'].get(99, 0)), 1)
    ric = cfg['ric']
    n_ues = len(cfg['ran'].get('_netns') or ['ue1'])
    if t0.get('analytics_tput_min') in (None, 'auto'):
        # guaranteed floor = scheduled forecast jobs + one row job per UE indication + one cell job per indication
        # (E2SM-KPM every ~1.1 s at 0.9x radio speed)
        floor = float(ric.get('anl_sched_rate', 8.0)) + 0.9 * (n_ues + 1)
        tg['analytics_tput_min'] = round(0.6 * min(floor, q['analytics_tput'].get(1, floor)), 3)
        tg['analytics_floor_jobs_s'] = round(floor, 3)
    if t0.get('telemetry_q_max') in (None, 'auto'):
        tg['telemetry_q_max'] = float(max(float(cfg.get('telemetry_q_min', 25)), 4 * q['telemetry_queue'].get(99, 0) + 10))
    viol = {it: float(np.mean([predicate_parts(it, 'independent', r, tg)[2] for r in rows]))
            for it in ('api', 'analytics', 'telemetry')}
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
        raise RuntimeError(f'baseline validation failed: healthy violation={viol}, missing={missing}. The machine is '
                           'overloaded or a KPI is not measured. See calibration.json and the runbook troubleshooting.')
    return cal


def step_weights(stack, cfg, cal, weights_path):
    if cal.get('weights', {}).get('passed') is not None and cal['weights'].get('final_weights'):
        return cal
    if cfg['network'].get('backend', 'netem') != 'netem' and not cfg.get('mock_ran'):
        cal['weights'] = {'passed': True, 'note': 'network backend is cell_load: weight sweep skipped',
                          'final_weights': util.load_json(weights_path, cfg['snet_sri_weights'])}
        return cal
    util.log('calibration 2/3: snet/sri weight validation (user-plane impairment sweep with tc netem)')
    levels = cfg['weight_sweep_levels']
    tg = cal['targets']
    neutral = {'api': 1, 'analytics': 1, 'telemetry': 1, 'burst': 1, 'cell_load': 1, 'delay_ms': 0, 'loss_pct': 0,
               'telemetry_active': False}

    def sweep():
        res = []
        f = float(cfg['tick_s']) / 4.0
        for d, l in levels:
            stack.apply(dict(neutral, delay_ms=d, loss_pct=l))
            _sleep(4 * f)
            sn, sr, comp = [], [], {'rtt': [], 'loss': [], 'http': []}
            t0 = mono()
            while mono() - t0 < cfg['weight_sweep_hold_s']:
                k = stack.read_kpis()
                if k['snet'] is not None:
                    sn.append(k['snet'])
                if k['sri'] is not None:
                    sr.append(k['sri'])
                for a, b in (('rtt', 'ue_rtt_ms'), ('loss', 'ue_loss_pct'), ('http', 'ue_http_ok_pct')):
                    if k.get(b) is not None:
                        comp[a].append(k[b])
                _sleep(float(cfg['sample_s']))
            res.append({'delay_ms': d, 'loss_pct': l, 'snet': float(np.mean(sn)) if sn else None,
                        'sri': float(np.mean(sr)) if sr else None,
                        **{a: (float(np.mean(v)) if v else None) for a, v in comp.items()}})
            util.log(f"  sweep d={d} l={l}: snet={res[-1]['snet']} sri={res[-1]['sri']} rtt={res[-1]['rtt']} "
                     f"loss={res[-1]['loss']} http_ok={res[-1]['http']}")
        stack.reset()
        _sleep(4 * f)
        return res

    def assess(res):
        from scipy.stats import spearmanr
        sev = np.arange(len(res))
        sn = np.array([r['snet'] if r['snet'] is not None else np.nan for r in res])
        sr = np.array([r['sri'] if r['sri'] is not None else np.nan for r in res])
        rs, rr = spearmanr(sev, sn, nan_policy='omit').correlation, spearmanr(sev, sr, nan_policy='omit').correlation
        ok = {'snet_monotone': bool(rs <= -0.9), 'sri_monotone': bool(rr <= -0.8),
              'snet_healthy_high': bool(sn[0] >= tg['snet_low'] + 5), 'sri_healthy_high': bool(sr[0] >= tg['sri_low'] + 5),
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
        util.log(f'weights rescaled (snet x{f_sn:.2f}, sri network terms x{f_sr:.2f}); repeating the sweep')
        _sleep(2 * float(cfg['tick_s']) / 4.0)
        res = sweep()
        ok, corr = assess(res)
        history.append({'weights': W, 'sweep': res, 'checks': ok, **corr})
    passed = ok['snet_monotone'] and ok['snet_healthy_high'] and ok['sri_healthy_high'] and \
        ok['snet_reaches_threshold'] and ok['sri_reaches_threshold']
    cal['weights'] = {'passed': passed, 'final_weights': W, 'history': history}
    if not passed:
        util.log(f'WARNING: snet/sri weight validation not fully passed: {ok}', 'WARN')
    return cal


def _episode(stack, cfg, cal, typ, D):
    pre, post = 8, cfg['calib_drain_ticks']
    s, e = pre, pre + D
    who = typ.split('_')[1]
    ev = {'intent': who, 'type': typ, 'start': s, 'end': e}
    labels = {'api': [], 'analytics': [], 'telemetry': []}
    if typ.startswith('conflicting'):
        ev.update({'victim_start': s + 5, 'victim_end': e})
        labels['telemetry'].append({'start': s + 5, 'end': e, 'type': 'conflicting_victim'})
    if typ.startswith('mimic'):
        for x in ('api', 'analytics', 'telemetry'):
            labels[x].append({'start': s, 'end': e, 'type': 'mimic'})
        ev['intent'] = 'mimic'
    else:
        labels[who].append({'start': s, 'end': e, 'type': typ})
    if cfg.get('scenario') == 'v3':
        ev['ramp'] = cfg.get('v3_ramp_power', 2.5)
    out = []
    runner = TickRunner(stack, cfg, cal['targets'], cal['scales'])
    runner.demand = _demand(cfg, e + post)
    ok = run_ticks(runner, [ev], labels, 0, e + post, on_event=out.append, should_stop=SHOULD_STOP['fn'])
    stack.reset()
    _guard(ok, runner)
    return out


def _q(cal, kpi, p):
    qq = (cal.get('baseline', {}).get('quantiles') or {}).get(kpi) or {}
    return qq.get(p, qq.get(str(p)))


def step_knee(stack, cfg, cal, save):
    """Scenario v4: capacity-knee scan of each xApp. The CPU-work knob of one xApp rises linearly from 1 to kmax over
    60 ticks while everything else is healthy. Recorded per xApp:
      knee = knob value where its intent KPI first moves half-way out of its healthy band for 3 ticks (above the
             midpoint of healthy median and p99 for api_latency / telemetry_queue, below median-to-p1 for
             analytics_tput);
      fail = knob value where the KPI crosses the intent target for 2 ticks.
    Then scale = 1 + 1.6 x (fail - 1) (the collapse overshoots the failure point) and kneek_<x> = knee: a v4 episode
    climbs slowly to 0.95 x knee (KPI still healthy, only resource signals move) and then collapses."""
    res = cal.setdefault('knee', {})
    sc, B = cal['scales'], cfg['scale_bounds']
    tg = cal['targets']
    N = int(cfg.get('knee_scan_ticks', 60))
    def half(kpi, lo):                      # half-way from the healthy median to the healthy p1 / p99
        m = _q(cal, kpi, 50)
        e = _q(cal, kpi, 1 if lo else 99)
        return None if m is None or e is None else m + 0.5 * (e - m)
    band = {'api': ('api_latency', 1, half('api_latency', False) or 90.0),
            'analytics': ('analytics_tput', -1, half('analytics_tput', True) or 12.0),
            'telemetry': ('telemetry_queue', 1, max(half('telemetry_queue', False) or 1.0, 1.0))}
    target = {'api': tg['api_p95_ms'], 'analytics': tg['analytics_tput_min'], 'telemetry': tg['telemetry_q_max']}
    util.log(f'calibration 2b/3 (scenario {cfg.get("scenario")}): capacity-knee scan of xRC, xSLA and xKPM (60 ticks each)')
    for x in ('api', 'analytics', 'telemetry'):
        if res.get(x, {}).get('done'):
            continue
        kpi, sign, hb = band[x]
        kmax = float(min(B[x][1], max(sc[x] * 1.5, 6.0)))
        knee = fail = None
        hist = []
        for attempt in range(3):
            pre, post = 6, cfg['calib_drain_ticks']
            ev = {'intent': x, 'type': f'kneescan_{x}', 'start': pre, 'end': pre + N, 'kmax': kmax}
            rows = []
            runner = TickRunner(stack, cfg, tg, sc)
            ok = run_ticks(runner, [ev], {'api': [], 'analytics': [], 'telemetry': []}, 0, pre + N + post,
                           on_row=lambda r, tr: rows.append((r, tr)), should_stop=SHOULD_STOP['fn'])
            stack.reset()
            _guard(ok, runner)
            knee = fail = None
            sk = sf = 0
            k_at = f_at = None
            for r, tr in rows:
                v = r.get(kpi)
                kn = tr.get(f'knob_{x}', 1.0)
                if v is None or (isinstance(v, float) and math.isnan(v)) or tr['t'] < pre:
                    continue
                out_band = (v > hb) if sign > 0 else (v < hb)
                crossed = (v >= target[x]) if sign > 0 else (v <= target[x])
                if out_band:
                    k_at = kn if sk == 0 else k_at       # knob at the FIRST tick of the streak
                    sk += 1
                else:
                    sk = 0
                if crossed:
                    f_at = kn if sf == 0 else f_at
                    sf += 1
                else:
                    sf = 0
                if knee is None and sk >= 3:
                    knee = k_at
                if fail is None and sf >= 2:
                    fail = f_at
            hist.append({'kmax': kmax, 'knee': knee, 'fail': fail})
            util.log(f'  knee scan {x}: knob 1 -> {kmax:.2f}: leaves the healthy band at knob {knee}, crosses the '
                     f'target at knob {fail}')
            if fail is not None and knee is not None:
                break
            kmax = float(min(B[x][1], kmax * 1.6))
        if knee is None or fail is None:
            util.log(f'WARNING: knee scan of {x} did not reach the target; keeping scale {sc[x]:.2f} and the default '
                     'knee fraction', 'WARN')
            res[x] = {'done': True, 'ok': False, 'history': hist}
        else:
            # the KPI reacts 1-2 ticks after the knob (4-s rate windows, queues): step back 10 % of the way
            knee = max(1.05, min(1 + 0.9 * (knee - 1), 1 + 0.9 * (fail - 1)))
            sc[x] = float(min(B[x][1], max(B[x][0], 1 + 1.6 * (fail - 1))))
            sc[f'kneek_{x}'] = float(knee)
            fl = (cfg.get('knee_floor') or {}).get(x)
            if fl:                                   # scenario v5: minimum pre-phase depth (see scheduler.V5)
                k_fl = 1 + float(fl) * (sc[x] - 1)
                if k_fl > knee:
                    util.log(f'  knee scan {x}: knee knob {knee:.3f} raised to the v5 floor {k_fl:.3f} '
                             f'({fl:.2f} of the scale)')
                    sc[f'kneek_{x}'] = float(k_fl)
                    knee = k_fl
            if x == 'telemetry':
                sc['victim'] = sc[x]
            res[x] = {'done': True, 'ok': True, 'knee_knob': round(knee, 3), 'fail_knob': round(fail, 3),
                      'scale': round(sc[x], 3), 'knee_frac': round((knee - 1) / (sc[x] - 1), 3), 'history': hist}
        cal['scales'] = sc
        save(cal)
    stack.set_scales(sc)
    return cal


def _episode_v4(stack, cfg, cal, typ):
    """One scenario-v4 calibration episode (hockey profile; victims labelled from the cause's start)."""
    pre = (cfg['pre_min'] + cfg['pre_max']) // 2
    col, hold, lag, lead_in, post = 4, 8, 3, 8, cfg['calib_drain_ticks']
    s = lead_in
    labels = {'api': [], 'analytics': [], 'telemetry': []}
    if typ.startswith('mimic'):
        e = s + pre + 8
        ev = {'intent': 'mimic', 'type': typ, 'start': s, 'end': e, 'pre': pre, 'down': 8,
              'level': cfg['mimic_level'], 'creep_amp': cfg['creep_amp']}
        for x in labels:
            labels[x].append({'start': s, 'end': e, 'type': 'mimic'})
    else:
        who = typ.split('_')[1]
        e = s + pre + col + hold + (lag if typ.startswith('conflicting') else 0)
        ev = {'intent': who, 'type': typ, 'start': s, 'end': e, 'pre': pre, 'col': col, 'profile': 'hockey'}
        labels[who].append({'start': s, 'end': e, 'type': typ})
        if typ.startswith('conflicting'):
            ev.update(victim_start=s + pre + lag, victim_end=e, victim_pre=2, victim_col=2)
            labels['telemetry'].append({'start': s, 'end': e, 'type': 'conflicting_victim'})
    out = []
    runner = TickRunner(stack, cfg, cal['targets'], cal['scales'])
    runner.demand = []
    ok = run_ticks(runner, [ev], labels, 0, e + post, on_event=out.append, should_stop=SHOULD_STOP['fn'])
    stack.reset()
    _guard(ok, runner)
    return out


def step_faults(stack, cfg, cal, save):
    util.log('calibration 3/3: fault-response calibration (one test episode per event type)')
    sc, B = cal['scales'], cfg['scale_bounds']
    D = (cfg['dur_min'] + cfg['dur_max']) // 2
    plan = [('independent_api_latency', 'api'), ('independent_analytics_slow', 'analytics'),
            ('independent_telemetry_slow', 'telemetry'), ('conflicting_analytics_cause', 'victim'),
            ('conflicting_api_cause', 'victim'), ('nonlinear_api_xor', 'nl_api'),
            ('nonlinear_analytics_xor', 'nl_analytics'), ('nonlinear_telemetry_xor', 'nl_telemetry'),
            ('mimic_api_only', 'mimic'), ('mimic_analytics_only', 'mimic'), ('mimic_telemetry_only', 'mimic'),
            ('mimic_network_only', 'mimic')]
    if cfg.get('scenario') in ('v4', 'v5'):
        plan = [x for x in plan if not x[0].startswith('mimic')] + [
            ('mimic_partial_api', 'mimic'), ('mimic_partial_analytics', 'mimic'), ('mimic_partial_telemetry', 'mimic'),
            ('mimic_demand_creep', 'mimic'), ('mimic_network_only', 'mimic')]
    only = cfg.get('calib_fault_types', 'all')
    if only != 'all':
        plan = [x for x in plan if x[0] in only]
    res = cal.setdefault('faults', {})
    for typ, key in plan:
        if res.get(typ, {}).get('done'):
            continue
        hist = []
        c, evs = None, []
        for it in range(cfg['calib_max_iter']):
            stack.set_scales(sc)
            evs = _episode_v4(stack, cfg, cal, typ) if cfg.get('scenario') in ('v4', 'v5') else _episode(stack, cfg, cal, typ, D)
            main = [x for x in evs if x['type'] in (typ, 'mimic')]
            vic = [x for x in evs if x['type'] == 'conflicting_victim']
            target = vic[0] if key == 'victim' and vic else (main[0] if main else None)
            c = target['crossing_frac'] if target and target['failure_observed'] else None
            hist.append({'iter': it, 'scales': {k: round(v, 4) for k, v in sc.items()}, 'events': evs})
            util.log(f'  {typ} iter {it}: crossing={c} parts={target and (target.get("svc_hit"), target.get("net_hit"))}')
            if key == 'mimic':
                if any(x['failure_observed'] for x in evs):
                    if cfg.get('scenario') in ('v4', 'v5'):
                        sc['mimic_v4'] = max(0.2, sc.get('mimic_v4', 1.0) * 0.8)
                    else:
                        sc['mimic'] = max(0.05, sc['mimic'] * 0.75)
                    continue
                break
            if c is not None and 0.45 <= c <= 0.9:
                break
            if key.startswith('nl_'):
                if c is None:
                    sv = bool(target and target.get('svc_hit'))
                    nt = bool(target and target.get('net_hit'))
                    if not sv:
                        sc[key] = min(1.3, sc[key] * 1.15)
                    if not nt:
                        sc['delay_ms'] = min(B['delay_ms'][1], sc['delay_ms'] * 1.25)
                        sc['loss_pct'] = min(B['loss_pct'][1], sc['loss_pct'] * 1.25)
                    if sv and nt:                       # both reached, but not together for `persist` ticks
                        sc[key] = min(1.3, sc[key] * 1.1)
                        sc['delay_ms'] = min(B['delay_ms'][1], sc['delay_ms'] * 1.1)
                        sc['loss_pct'] = min(B['loss_pct'][1], sc['loss_pct'] * 1.1)
                elif c < 0.45:
                    sc[key] = max(0.3, sc[key] / 1.2)
                else:                                   # crossed, but too late (> 0.9): a little stronger
                    sc[key] = min(1.3, sc[key] * 1.1)
                    sc['delay_ms'] = min(B['delay_ms'][1], sc['delay_ms'] * 1.1)
                    sc['loss_pct'] = min(B['loss_pct'][1], sc['loss_pct'] * 1.1)
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
            util.log(f'WARNING: {typ} not calibrated into the target band (last crossing={c}); the collector keeps '
                     'adapting the severity between segments', 'WARN')
    cal['faults_passed'] = all(v['ok'] for v in res.values())
    stack.set_scales(sc)
    return cal


def write_report(cal, path):
    L = ['# Calibration report (MILD on O-RAN)', '']
    p = cal.get('plant', {})
    L += ['## Plant (live cell)', '', f"- saturated DL capacity at the gNB: {cal.get('capacity_mbps')} Mbit/s",
          f"- UEs received {p.get('capacity_sink_mbps')} Mbit/s; E2SM-KPM DRB.UEThpDl {p.get('capacity_kpm_ue_thp_mbps')}"
          f" Mbit/s; RRU.PrbTotDl {p.get('prb_dl_pct_at_saturation')} %",
          f"- probe floor (UE -> UPF over the radio, per UE): {cal.get('probe_baseline')}",
          f"- E2 indications at xKPM: {p.get('e2_ind_hz')} /s", f"- issues: {p.get('issues')}", '',
          '## E2SM-RC control check (xRC)', '', f"- {cal.get('rc')}", '',
          '## Intent targets used for failure labelling', '']
    for k, v in cal.get('targets', {}).items():
        L.append(f'- {k}: {v}')
    b = cal.get('baseline', {})
    L += ['', '## Healthy baseline', '', f"- violation rate per intent: {b.get('healthy_violation_rate')}",
          f"- missing-KPI rate: {b.get('missing_rate')}", f"- issues: {b.get('issues')}", '',
          '| KPI | p1 | p5 | p50 | p95 | p99 |', '|---|---|---|---|---|---|']
    for k, q in b.get('quantiles', {}).items():
        if q:
            L.append(f"| {k} | " + ' | '.join(f'{q[pp]:.2f}' for pp in (1, 5, 50, 95, 99)) + ' |')
    w = cal.get('weights', {})
    L += ['', '## snet/sri weight validation', '', f"- passed: {w.get('passed')}", f"- final weights: {w.get('final_weights')}"]
    for h in w.get('history', []):
        L.append(f"- checks: {h['checks']} (Spearman snet={h['spearman_snet']:.2f}, sri={h['spearman_sri']:.2f})")
        for r in h['sweep']:
            L.append(f"  - delay {r['delay_ms']} ms, loss {r['loss_pct']} %: snet {r['snet']}, sri {r['sri']}, "
                     f"RTT {r.get('rtt')}, loss {r.get('loss')}, HTTP ok {r.get('http')}")
    L += ['', '## Fault-response calibration (crossing fraction of the window)', '']
    for typ, r in cal.get('faults', {}).items():
        L.append(f"- {typ}: ok={r['ok']} final crossing={r['final_crossing']} iterations={len(r['history'])}")
    L += ['', '## Final severity scales', '', f"{cal.get('scales')}"]
    with open(path, 'w') as f:
        f.write('\n'.join(L))
