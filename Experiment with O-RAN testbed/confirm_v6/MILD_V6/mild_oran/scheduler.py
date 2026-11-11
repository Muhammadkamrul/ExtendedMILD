"""scheduler.py -- fault plan of one collection segment and the mapping from events to knobs.

Event taxonomy (identical names to the paper's datasets, so MILD's code and
evaluator are used unchanged). O-RAN meaning on the v2 testbed (FlexRIC, real E2, N UEs):

  independent_api_latency        xRC overload (policy compilation / conflict checking per request)
  independent_analytics_slow     xSLA slowdown (heavier model, CPU-starved inference)
  independent_telemetry_slow     xKPM decoder slowdown + KPM storm (extra REAL E2SM-KPM style-4
                                 subscriptions: the gNB sends one more indication per period each)
  nonlinear_api_xor              xRC overload AND user-plane impairment (fails only if sri <= sri_low too)
  nonlinear_analytics_xor        xSLA slowdown AND user-plane impairment (fails only if snet <= snet_low too)
  nonlinear_telemetry_xor        xKPM slowdown + storm AND impairment (fails only if sri <= sri_low too)
  conflicting_api_cause          co-drift root: xRC overload with radio congestion (traffic surge of all UEs);
   + conflicting_victim            after a lag the Telemetry victim (re-subscription storm + slower decoding)
  conflicting_analytics_cause    co-drift root: xSLA slowdown (real backpressure into xKPM's delivery queue);
   + conflicting_victim            after a lag the Telemetry victim
  mimic_{api,analytics,telemetry,network}_only   unlabelled look-alikes at `mimic` x the fault scale

Knobs produced per tick (maximum over the active events):
  api, analytics, telemetry   CPU-time multipliers of xRC, xSLA, xKPM (HTTP POST /knob on their control port)
  burst                       KPM storm: round(burst - 1) extra E2SM-KPM style-4 subscriptions of xKPM
                              (at most ric.storm_max_subs; 'replay' fallback if the gNB refuses them)
  cell_load                   multiplier of the DL offered load of every UE (radio congestion)
  delay_ms, loss_pct          added delay and loss on every UE's user plane (tc netem: ogstun DL, tun_srsue UL)
Severity of an event grows as ((t - start + 1) / (end - start)) ** ramp_power.
Every segment uses its own seed (base_seed + segment index).
"""
from __future__ import annotations

import random
from typing import Dict, List, Tuple


def _window(rng, tmin, tmax, lo, hi):
    dur = rng.randint(lo, hi)
    start = rng.randint(tmin, max(tmin + 1, tmax - dur))
    return start, start + dur


# Scenario v3 ("coupled multi-tenant", v2.3): settings used by build_plan when cfg['scenario'] == 'v3'.
# Why: on the v2 scenario a fixed threshold on the failing intent's OWN KPI detected 100 % of the failures with
# the same lead as every neural model (detection and lead time saturated). v3 adds what makes intent assurance
# hard in a shared RIC: (1) time-varying tenant demand and short surges that move every KPI's healthy level,
# (2) convex onsets (little visible change early), (3) longer co-drift lags, so the victim's own KPI reacts late
# and the early evidence lies in the CAUSE's KPIs, (4) rarer failures and as many look-alikes as failures,
# including network-only and partial co-drift mimics, (5) all three intents fail on their own (balanced).
V3 = {'events_per_segment': 6, 'balanced_intents': True, 'mimics_per_10_events': 10, 'p_nonlinear': 0.34,
      'p_codrift': 0.34, 'dur_min': 16, 'dur_max': 30, 'lag_min': 6, 'lag_max': 12, 'victim_min_dur': 6,
      'mimic_dur_min': 8, 'mimic_dur_max': 16, 'min_gap': 14, 'v3_ramp_power': 2.5,
      'demand_amp': 0.5, 'surge_amp': 0.6, 'demand_period_min': 180, 'demand_period_max': 360,
      'surge_dur_min': 2, 'surge_dur_max': 6, 'surge_gap_mean': 30}


# Scenario v4 ("capacity knee", v2.4): settings used when cfg['scenario'] == 'v4'.
# Why (from the v3 pilot): with gradual faults the failing intent's OWN KPI degrades early and steadily, so one
# fixed threshold on it detects most failures with a good lead; learned models only helped where the KPI stays
# flat until the service runs out of capacity (API latency). v4 makes every intent behave like that, which is
# how capacity-bounded services really fail:
#   * a fault grows LINEARLY for a long time (pre phase, 26-38 ticks of 2 s) while the xApp still keeps up: its
#     intent KPI stays at its healthy level, only resource signals move (CPU of the xApp tree, queue/latency
#     a little, the cause's KPIs for a victim, the network for nonlinear episodes);
#   * when the xApp reaches its capacity knee (measured per xApp by the v4 knee scan in calibration) the KPI
#     collapses within a few ticks (collapse phase) and the intent fails;
#   * look-alikes: partial ramps that stop below the knee and recede (same start as a fault), tenant-demand creeps
#     that raise CPU together with the cell load (load_pct is a model input in v4), network-only dips, surges;
#   * co-drift victims fail a few ticks after their cause; their episode starts with the cause, so a model that
#     reads the cause's precursor can warn early, while the victim's own KPI warns only at the end.
# Ticks are 2 s (twice the data per hour); the KPI rate windows are 4 s.
V4 = {'tick_s': 2.0, 'rate_window_s': 4, 'segment_ticks': 720, 'events_per_segment': 8, 'balanced_intents': True,
      'mimics_per_10_events': 5, 'p_nonlinear': 0.25, 'p_codrift': 0.375, 'pre_min': 26, 'pre_max': 38,
      'col_min': 3, 'col_max': 5, 'hold_min': 6, 'hold_max': 9, 'victim_lag_min': 2, 'victim_lag_max': 5,
      'mimic_level': 0.6, 'min_gap': 10, 'warmup_ticks': 30, 'tail_ticks': 12, 'persist': 3,
      'recovery_max': 12, 'demand_amp': 0.35, 'surge_amp': 0.5, 'demand_period_min': 300,
      'demand_period_max': 600, 'surge_dur_min': 2, 'surge_dur_max': 5, 'surge_gap_mean': 60,
      'creep_amp': 0.9, 'v4_knee_default': {'api': 0.30, 'analytics': 0.22, 'telemetry': 0.25},
      'calib_baseline_ticks': 200, 'calib_drain_ticks': 20, 'watchdog_bad_ticks': 60, 'min_total_ticks': 30000,
      'target_ticks': 30000, 'min_events_per_intent': 120, 'min_codrift_pairs': 60}

# Scenario v5 ("multi-tenant contention", v2.7) = v4 with two changes, both fixed BEFORE the v5 collection:
#   * API precursor: on the testbed the xRC knee scan stops the v4 pre phase where xRC still uses almost no CPU
#     (its p95 latency leaves its narrow healthy band first), so API faults showed no early signal at all and every
#     method detected only ~60 % of them. v5 lets the API pre phase climb to at least 45 % of the API scale (about
#     2/3 of the failing work), so the API latency rises gradually while staying below its target (knee_floor).
#   * more cross-tenant coupling: half of the episodes are co-drift (cause in xRC or xSLA, victim xKPM), the
#     situation multi-tenant intent assurance is about; independent episodes become 2 of 8.
# Everything else (ticks, look-alikes, demand, calibration) is unchanged from v4.
V5 = dict(V4, p_codrift=0.5, p_nonlinear=0.25, knee_floor={'api': 0.45})

SCENARIOS = {'v3': V3, 'v4': V4, 'v5': V5}
V4_FAMILY = ('v4', 'v5')


def knee_frac(sc, x, cfg=None):
    """Scenario v4: severity (0..1 of the calibrated scale) at which xApp x reaches its capacity knee.
    The knee scan stores the knee in knob units (sc['kneek_<x>']); the fraction follows the current scale."""
    kk = sc.get(f'kneek_{x}')
    s = sc.get(x, 2.0)
    if kk is not None and s > 1.0:
        return float(min(0.85, max(0.05, (kk - 1.0) / (s - 1.0))))
    return float(((cfg or {}).get('v4_knee_default') or V4['v4_knee_default']).get(x, 0.3))


def hockey(t, e, knee):
    """Scenario v4 severity: linear to 0.95 x knee over the pre phase, then to 1 within the collapse phase."""
    tau = t - e['start'] + 1
    pre, col = e['pre'], e['col']
    k0 = 0.95 * knee
    if tau <= pre:
        return k0 * tau / pre
    if tau <= pre + col:
        return k0 + (1.0 - k0) * (tau - pre) / col
    return 1.0


def build_plan_v4(cfg: dict, seed: int, ticks: int):
    rng = random.Random(seed)
    E = cfg['events_per_segment']
    n_non = int(round(E * cfg['p_nonlinear']))
    n_co = int(round(E * cfg['p_codrift']))
    n_ind = max(0, E - n_non - n_co)
    order = ['api', 'analytics', 'telemetry']
    k = seed % 3
    order = order[k:] + order[:k]
    specs = ([('independent', x) for x in (order * E)[:n_ind]]
             + [('nonlinear', x) for x in (order[1:] + order[:1]) * E][:n_non]
             + [('codrift', x) for x in (['analytics', 'api'] if seed % 2 else ['api', 'analytics']) * E][:n_co])
    rng.shuffle(specs)
    occupied = []
    tmin, tmax = cfg['warmup_ticks'], ticks - cfg['tail_ticks']

    def free(s, e, gap):
        return not any(not (e + gap <= a or b + gap <= s) for a, b in occupied)

    plan, labels = [], {'api': [], 'analytics': [], 'telemetry': []}
    for kind, which in specs:
        for _ in range(600):
            pre = rng.randint(cfg['pre_min'], cfg['pre_max'])
            col = rng.randint(cfg['col_min'], cfg['col_max'])
            hold = rng.randint(cfg['hold_min'], cfg['hold_max'])
            lag = rng.randint(cfg['victim_lag_min'], cfg['victim_lag_max']) if kind == 'codrift' else 0
            dur = pre + col + hold + lag
            s = rng.randint(tmin, max(tmin + 1, tmax - dur))
            e = s + dur
            if not free(s, e, cfg['min_gap']):
                continue
            ev = {'start': s, 'end': e, 'pre': pre, 'col': col, 'profile': 'hockey'}
            if kind == 'independent':
                typ = {'api': 'independent_api_latency', 'analytics': 'independent_analytics_slow',
                       'telemetry': 'independent_telemetry_slow'}[which]
                ev.update(intent=which, type=typ)
                labels[which].append({'start': s, 'end': e, 'type': typ})
            elif kind == 'nonlinear':
                typ = f'nonlinear_{which}_xor'
                ev.update(intent=which, type=typ)
                labels[which].append({'start': s, 'end': e, 'type': typ})
            else:
                typ = f'conflicting_{which}_cause'
                # the victim's own ramp starts when the cause reaches its knee and is short; its episode is
                # labelled from the cause's start: the cause's precursor is the victim's early warning
                vs = s + pre + lag
                ev.update(intent=which, type=typ, victim_start=vs, victim_end=e, victim_pre=2,
                          victim_col=2)
                labels[which].append({'start': s, 'end': e, 'type': typ})
                labels['telemetry'].append({'start': s, 'end': e, 'type': 'conflicting_victim'})
            plan.append(ev)
            occupied.append((s, e))
            break
    n_mimic = int(round(E * cfg['mimics_per_10_events'] / 10.0))
    kinds = ['mimic_partial_api', 'mimic_demand_creep', 'mimic_partial_analytics', 'mimic_network_only',
             'mimic_partial_telemetry', 'mimic_demand_creep']
    k0 = seed % len(kinds)
    kinds = kinds[k0:] + kinds[:k0]
    for i in range(n_mimic):
        for _ in range(600):
            pre = rng.randint(cfg['pre_min'], cfg['pre_max'])
            down = rng.randint(6, 10)
            s = rng.randint(tmin, max(tmin + 1, tmax - pre - down))
            e = s + pre + down
            if free(s, e, max(4, cfg['min_gap'] // 2)):
                plan.append({'intent': 'mimic', 'type': kinds[i % len(kinds)], 'start': s, 'end': e, 'pre': pre,
                             'down': down, 'level': round(rng.uniform(0.6, 1.0) * cfg['mimic_level'], 3),
                             'creep_amp': round(rng.uniform(0.7, 1.0) * cfg['creep_amp'], 3)})
                occupied.append((s, e))
                break
    P = rng.randint(cfg['demand_period_min'], cfg['demand_period_max'])
    bursts = []
    tt = rng.randint(5, 40)
    while tt < ticks - 5:
        d = rng.randint(cfg['surge_dur_min'], cfg['surge_dur_max'])
        bursts.append([tt, tt + d, round(rng.uniform(0.4, 1.0), 3)])
        tt += d + int(rng.expovariate(1.0 / cfg['surge_gap_mean'])) + 3
    plan.append({'intent': 'demand', 'type': 'demand_profile', 'start': 0, 'end': ticks, 'amp': cfg['demand_amp'],
                 'period': P, 'phase': round(rng.uniform(0, 6.2832), 4), 'surge_amp': cfg['surge_amp'],
                 'bursts': bursts, 'creep_amp': cfg['creep_amp']})
    plan.sort(key=lambda x: x['start'])
    for k in labels:
        labels[k].sort(key=lambda x: x['start'])
    return plan, labels


def build_plan(cfg: dict, seed: int, ticks: int) -> Tuple[List[Dict], Dict[str, List[Dict]]]:
    if cfg.get('scenario') in V4_FAMILY:
        sv = SCENARIOS[cfg['scenario']]
        cfg = dict(cfg, **{k: v for k, v in sv.items() if k not in (cfg.get('_v3_user_keys') or [])})
        return build_plan_v4(cfg, seed, ticks)
    rng = random.Random(seed)
    if cfg.get('scenario') == 'v3':
        # scenario v3 (coupled multi-tenant, see V3 below): its own episode mix; everything else is unchanged
        cfg = dict(cfg, **{k: v for k, v in V3.items() if k not in (cfg.get('_v3_user_keys') or [])})
    E = cfg['events_per_segment']
    n_non = int(round(E * cfg['p_nonlinear']))
    n_co = int(round(E * cfg['p_codrift']))
    n_ind = max(0, E - n_non - n_co)
    # With the default 10 events (round(3.5) = 4 nonlinear, 4 co-drift, 2 independent) the fixed intent order
    # gives every segment independent API + Analytics episodes and two nonlinear API episodes, and never an
    # independent Telemetry episode: Telemetry fails only as a co-drift victim or a nonlinear episode. The
    # first O-RAN dataset was collected that way, so it stays the default (data stay comparable); with
    # balanced_intents = true the starting intent rotates with the segment seed and all three take turns.
    order = ['api', 'analytics', 'telemetry']
    if cfg.get('balanced_intents'):
        k = seed % 3
        order = order[k:] + order[:k]
    specs = ([('independent', x) for x in (order * E)[:n_ind]]
             + [('nonlinear', x) for x in (order * E)[:n_non]]
             + [('codrift', x) for x in (['api', 'analytics'] * E)[:n_co]])
    rng.shuffle(specs)
    occupied = []
    tmin, tmax = cfg['warmup_ticks'], ticks - cfg['tail_ticks']

    def free(s, e, gap):
        return not any(not (e + gap <= a or b + gap <= s) for a, b in occupied)

    plan, labels = [], {'api': [], 'analytics': [], 'telemetry': []}
    for kind, which in specs:
        for _ in range(400):
            s, e = _window(rng, tmin, tmax, cfg['dur_min'], cfg['dur_max'])
            if not free(s, e, cfg['min_gap']):
                continue
            if kind == 'independent':
                typ = {'api': 'independent_api_latency', 'analytics': 'independent_analytics_slow',
                       'telemetry': 'independent_telemetry_slow'}[which]
                ev = {'intent': which, 'type': typ, 'start': s, 'end': e}
                labels[which].append({'start': s, 'end': e, 'type': typ})
            elif kind == 'nonlinear':
                typ = f'nonlinear_{which}_xor'
                ev = {'intent': which, 'type': typ, 'start': s, 'end': e}
                labels[which].append({'start': s, 'end': e, 'type': typ})
            else:
                lag = rng.randint(cfg['lag_min'], cfg['lag_max'])
                vs = max(s + 2, min(e - cfg['victim_min_dur'], s + lag))
                typ = f'conflicting_{which}_cause'
                ev = {'intent': which, 'type': typ, 'start': s, 'end': e, 'victim_start': vs, 'victim_end': e}
                labels[which].append({'start': s, 'end': e, 'type': typ})
                labels['telemetry'].append({'start': vs, 'end': e, 'type': 'conflicting_victim'})
            plan.append(ev)
            occupied.append((s, e))
            break
    v3 = cfg.get('scenario') == 'v3'
    n_mimic = int(round(E * cfg['mimics_per_10_events'] / 10.0))
    kinds = ['mimic_api_only', 'mimic_analytics_only', 'mimic_telemetry_only', 'mimic_network_only']
    if v3:
        # v3: every look-alike family, incl. network-only and partial co-drift (a cause ramp that stops below its
        # target and drags the Telemetry queue a little, with no failure), in an order that rotates per segment
        kinds = ['mimic_codrift_api', 'mimic_analytics_only', 'mimic_network_only', 'mimic_codrift_analytics',
                 'mimic_api_only', 'mimic_telemetry_only']
        k0 = seed % len(kinds)
        kinds = kinds[k0:] + kinds[:k0]
    for i in range(n_mimic):
        for _ in range(400):
            s, e = _window(rng, tmin, tmax, cfg['mimic_dur_min'], cfg['mimic_dur_max'])
            if free(s, e, max(4, cfg['min_gap'] // 2)):
                plan.append({'intent': 'mimic', 'type': kinds[i % len(kinds)], 'start': s, 'end': e})
                occupied.append((s, e))
                break
    if v3:
        # v3: time-varying tenant demand (slow wave + random short surges) on top of which faults happen. It moves
        # the healthy level of every KPI, so a fixed per-KPI threshold no longer separates faults from load.
        P = rng.randint(cfg.get('demand_period_min', 180), cfg.get('demand_period_max', 360))
        bursts = []
        tt = rng.randint(5, 40)
        while tt < ticks - 5:
            d = rng.randint(cfg.get('surge_dur_min', 2), cfg.get('surge_dur_max', 6))
            bursts.append([tt, tt + d, round(rng.uniform(0.4, 1.0), 3)])
            tt += d + int(rng.expovariate(1.0 / cfg.get('surge_gap_mean', 30))) + 3
        plan.append({'intent': 'demand', 'type': 'demand_profile', 'start': 0, 'end': ticks,
                     'amp': cfg.get('demand_amp', 0.5), 'period': P, 'phase': round(rng.uniform(0, 6.2832), 4),
                     'surge_amp': cfg.get('surge_amp', 0.6), 'bursts': bursts})
    if v3:
        for ev in plan:
            if ev['intent'] not in ('mimic', 'demand'):
                ev['ramp'] = cfg['v3_ramp_power']        # convex onset: little visible change early
    plan.sort(key=lambda x: x['start'])
    for k in labels:
        labels[k].sort(key=lambda x: x['start'])
    return plan, labels


def _frac(t, s, e):
    return (t - s + 1) / max(1, e - s)


def knobs_for_tick(t: int, plan: List[Dict], sc: dict, ramp: float) -> Dict[str, float]:
    """Combine all active events into knob values (maximum over events).

    sc = calibrated scales: api, analytics, telemetry (max CPU-time multipliers), victim
    (telemetry multiplier of co-drift victims), burst (max E2 report-rate multiplier),
    delay_ms / loss_pct (max added RTT / loss), cell_load (max DL load multiplier),
    mimic (fraction of the scale used by benign mimics), nl_* (service share in nonlinear events).
    """
    k = {'api': 1.0, 'analytics': 1.0, 'telemetry': 1.0, 'burst': 1.0, 'cell_load': 1.0, 'delay_ms': 0.0,
         'loss_pct': 0.0, 'telemetry_active': False}

    def up(name, v):
        k[name] = max(k[name], v)

    creeps = []                                   # v4: demand creeps act through the demand profile
    for e in plan:
        if e.get('type') == 'mimic_demand_creep' and e['start'] <= t < e['end']:
            tau = t - e['start'] + 1
            lvl = e.get('creep_amp', 0.9) * sc.get('mimic_v4', 1.0) * (min(1.0, tau / e['pre']) if tau <= e['pre']
                                             else max(0.0, 1.0 - (tau - e['pre']) / e['down']))
            creeps.append(lvl)
    if creeps and not any(e.get('type') == 'demand_profile' for e in plan):
        dem = max(creeps)
        up('cell_load', 1 + dem)
        for svc, f in (('api', 0.35), ('analytics', 0.3), ('telemetry', 0.25)):
            up(svc, 1 + f * dem)
        k['demand'] = round(dem, 4)
    for e in plan:
        if not (e['start'] <= t < e['end']):
            continue
        if e.get('type') == 'demand_profile':
            import math
            w = 0.5 + 0.5 * math.sin(2 * math.pi * t / e['period'] + e['phase'])
            dem = e['amp'] * w
            for a_, b_, mag in e.get('bursts', []):
                if a_ <= t < b_:
                    dem = max(dem, e['surge_amp'] * mag)
            for c in creeps:                                  # v4 benign demand creeps (mimic_demand_creep)
                dem = max(dem, c)
            up('cell_load', 1 + dem)
            for svc, f in (('api', 0.35), ('analytics', 0.3), ('telemetry', 0.25)):
                up(svc, 1 + f * dem)
            k['demand'] = round(dem, 4)
            continue
        if e.get('type', '').startswith('kneescan_'):     # v4 calibration: slow linear knob scan
            svc = e['type'].split('_', 1)[1]
            if svc == 'telemetry':
                k['telemetry_active'] = True
            up(svc, 1 + (e['kmax'] - 1) * min(1.0, (t - e['start'] + 1) / max(1, e['end'] - e['start'])))
            continue
        if e.get('profile') == 'hockey':
            _hockey_knobs(t, e, sc, k, up)
            continue
        if e.get('type') == 'mimic_demand_creep':
            continue
        if e.get('type', '').startswith('mimic_partial_') or (e.get('pre') and e.get('intent') == 'mimic'):
            _v4_mimic_knobs(t, e, sc, k, up)
            continue
        sev = _frac(t, e['start'], e['end']) ** e.get('ramp', ramp)
        ty = e['type']
        if ty in ('independent_api_latency', 'conflicting_api_cause'):
            up('api', 1 + (sc['api'] - 1) * sev)
        if ty == 'conflicting_api_cause':
            up('cell_load', 1 + (sc['cell_load'] - 1) * 0.6 * sev)
            up('delay_ms', 0.3 * sc['delay_ms'] * sev)
        if ty == 'nonlinear_api_xor':
            up('api', 1 + (sc['api'] - 1) * sc.get('nl_api', 0.8) * sev)
            up('delay_ms', sc['delay_ms'] * sev)
            up('loss_pct', 0.6 * sc['loss_pct'] * sev)
        if ty in ('independent_analytics_slow', 'conflicting_analytics_cause'):
            up('analytics', 1 + (sc['analytics'] - 1) * sev)
        if ty == 'nonlinear_analytics_xor':
            up('analytics', 1 + (sc['analytics'] - 1) * sc.get('nl_analytics', 0.8) * sev)
            up('delay_ms', 0.8 * sc['delay_ms'] * sev)
            up('loss_pct', 0.45 * sc['loss_pct'] * sev)
        if ty == 'independent_telemetry_slow':
            k['telemetry_active'] = True
            up('telemetry', 1 + (sc['telemetry'] - 1) * sev)
            up('burst', 1 + (sc['burst'] - 1) * 0.6 * sev)
        if ty == 'nonlinear_telemetry_xor':
            k['telemetry_active'] = True
            up('telemetry', 1 + (sc['telemetry'] - 1) * sc.get('nl_telemetry', 0.8) * sev)
            up('burst', 1 + (sc['burst'] - 1) * sev)
            up('delay_ms', 0.9 * sc['delay_ms'] * sev)
            up('loss_pct', 0.7 * sc['loss_pct'] * sev)
        if e.get('victim_start') is not None and e['victim_start'] <= t < e['victim_end']:
            k['telemetry_active'] = True
            sv = _frac(t, e['victim_start'], e['victim_end']) ** ramp
            up('telemetry', 1 + (sc['victim'] - 1) * sv)
            up('burst', 1 + (sc['burst'] - 1) * sv)
        m = sc['mimic']
        if ty == 'mimic_api_only':
            up('api', 1 + (sc['api'] - 1) * m * sev)
        if ty == 'mimic_analytics_only':
            up('analytics', 1 + (sc['analytics'] - 1) * m * sev)
        if ty == 'mimic_telemetry_only':
            k['telemetry_active'] = True
            up('telemetry', 1 + (sc['telemetry'] - 1) * m * sev)
        if ty in ('mimic_codrift_api', 'mimic_codrift_analytics'):
            svc = ty.rsplit('_', 1)[1]
            up(svc, 1 + (sc[svc] - 1) * m * sev)
            k['telemetry_active'] = True
            up('telemetry', 1 + (sc['victim'] - 1) * 0.5 * m * sev)
        if ty == 'mimic_network_only':
            up('delay_ms', sc['delay_ms'] * m * sev)
            up('loss_pct', sc['loss_pct'] * m * sev)
            up('cell_load', 1 + (sc['cell_load'] - 1) * m * sev)
    return k


def _hockey_knobs(t, e, sc, k, up):
    """Scenario v4 fault knobs (see V4). The service part follows hockey(); the network part of nonlinear and
    API-cause episodes ramps linearly over pre + collapse (a visible network precursor)."""
    ty = e['type']
    tau = t - e['start'] + 1
    lin = min(1.0, tau / float(e['pre'] + e['col']))
    if ty in ('independent_api_latency', 'conflicting_api_cause'):
        up('api', 1 + (sc['api'] - 1) * hockey(t, e, knee_frac(sc, 'api')))
    if ty == 'conflicting_api_cause':
        up('cell_load', 1 + (sc['cell_load'] - 1) * 0.5 * lin)
        up('delay_ms', 0.25 * sc['delay_ms'] * lin)
    if ty in ('independent_analytics_slow', 'conflicting_analytics_cause'):
        up('analytics', 1 + (sc['analytics'] - 1) * hockey(t, e, knee_frac(sc, 'analytics')))
    if ty == 'independent_telemetry_slow':
        k['telemetry_active'] = True
        up('telemetry', 1 + (sc['telemetry'] - 1) * hockey(t, e, knee_frac(sc, 'telemetry')))
    if ty.startswith('nonlinear_'):
        svc = ty.split('_')[1]
        if svc == 'telemetry':
            k['telemetry_active'] = True
        # XOR: the service alone stays below its knee for the whole episode; the network alone stays mild;
        # only both together break the intent (shares calibrated as nl_*)
        share = sc.get(f'nl_{svc}', 0.8)
        up(svc, 1 + (sc[svc] - 1) * share * hockey(t, e, knee_frac(sc, svc)))
        up('delay_ms', {'api': 1.0, 'analytics': 0.8, 'telemetry': 0.9}[svc] * sc['delay_ms'] * lin)
        up('loss_pct', {'api': 0.6, 'analytics': 0.45, 'telemetry': 0.7}[svc] * sc['loss_pct'] * lin)
    if e.get('victim_start') is not None and e['victim_start'] <= t < e['victim_end']:
        k['telemetry_active'] = True
        ve = {'start': e['victim_start'], 'pre': e.get('victim_pre', 3), 'col': e.get('victim_col', 2)}
        kv = knee_frac({'kneek_victim': sc.get('kneek_telemetry'), 'victim': sc['victim']}, 'victim')
        up('telemetry', 1 + (sc['victim'] - 1) * hockey(t, ve, kv))


def _v4_mimic_knobs(t, e, sc, k, up):
    """Scenario v4 look-alikes: the same linear start as a fault, stopping at `level` x knee, then receding."""
    tau = t - e['start'] + 1
    ramp = min(1.0, tau / e['pre']) if tau <= e['pre'] else max(0.0, 1.0 - (tau - e['pre']) / e['down'])
    ty = e['type']
    if ty.startswith('mimic_partial_'):
        svc = ty.rsplit('_', 1)[1]
        if svc == 'telemetry':
            k['telemetry_active'] = True
        up(svc, 1 + (sc[svc] - 1) * e['level'] * sc.get('mimic_v4', 1.0) * knee_frac(sc, svc) * ramp)
    if ty == 'mimic_network_only':
        up('delay_ms', 0.6 * sc['delay_ms'] * ramp)
        up('loss_pct', 0.5 * sc['loss_pct'] * ramp)
