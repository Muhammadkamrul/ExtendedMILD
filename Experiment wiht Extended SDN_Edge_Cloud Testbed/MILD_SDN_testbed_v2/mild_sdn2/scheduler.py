"""
scheduler.py -- event plan of one collection segment and the mapping from
active events to fault knobs.

The event taxonomy is the same as in the paper's SDN dataset:
  independent_{api_latency, analytics_slow, telemetry_slow}   single-intent drift
  nonlinear_{api, analytics, telemetry}_xor                  failure only when the service KPI AND
                                                              a network KPI degrade together
  conflicting_{api, analytics}_cause + conflicting_victim    co-drift (root cause + lagged telemetry victim)
  mimic_{api, analytics, telemetry, network}_only            unlabelled benign look-alikes

Every segment uses its own seed (base_seed + segment index), so segments
have different timetables. The three runs of the original dataset reused one seed.
"""
from __future__ import annotations

import random
from typing import Dict, List, Tuple


def _window(rng, tmin, tmax, lo, hi):
    dur = rng.randint(lo, hi)
    start = rng.randint(tmin, max(tmin + 1, tmax - dur))
    return start, start + dur


def build_plan(cfg: dict, seed: int, ticks: int) -> Tuple[List[Dict], Dict[str, List[Dict]]]:
    rng = random.Random(seed)
    E = cfg['events_per_segment']
    n_non = int(round(E * cfg['p_nonlinear']))
    n_co = int(round(E * cfg['p_codrift']))
    n_ind = max(0, E - n_non - n_co)
    specs = ([('independent', x) for x in (['api', 'analytics', 'telemetry'] * E)[:n_ind]]
             + [('nonlinear', x) for x in (['api', 'analytics', 'telemetry'] * E)[:n_non]]
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
    n_mimic = int(round(E * cfg['mimics_per_10_events'] / 10.0))
    kinds = ['mimic_api_only', 'mimic_analytics_only', 'mimic_telemetry_only', 'mimic_network_only']
    for i in range(n_mimic):
        for _ in range(400):
            s, e = _window(rng, tmin, tmax, cfg['mimic_dur_min'], cfg['mimic_dur_max'])
            if free(s, e, max(4, cfg['min_gap'] // 2)):
                plan.append({'intent': 'mimic', 'type': kinds[i % len(kinds)], 'start': s, 'end': e})
                occupied.append((s, e))
                break
    plan.sort(key=lambda x: x['start'])
    for k in labels:
        labels[k].sort(key=lambda x: x['start'])
    return plan, labels


def _frac(t, s, e):
    return (t - s + 1) / max(1, e - s)


def knobs_for_tick(t: int, plan: List[Dict], sc: dict, ramp: float) -> Dict[str, float]:
    """Combine all active events into knob values (maximum over events).

    sc holds the calibrated scales:
      api, analytics, telemetry : maximum work multiplier of each service
      victim                    : maximum telemetry multiplier for co-drift victims
      burst                     : maximum device reporting-rate multiplier
      delay_ms, loss_pct        : maximum extra WAN delay / loss
      cross                     : maximum cross-traffic multiplier (WAN congestion)
      mimic                     : fraction of the scale used by benign mimics
    """
    k = {'api': 1.0, 'analytics': 1.0, 'telemetry': 1.0, 'burst': 1.0, 'delay_ms': 0.0, 'loss_pct': 0.0,
         'cross': 1.0, 'telemetry_active': False}

    def up(name, v):
        k[name] = max(k[name], v)

    for e in plan:
        if not (e['start'] <= t < e['end']):
            continue
        sev = _frac(t, e['start'], e['end']) ** ramp
        ty = e['type']
        if ty in ('independent_api_latency', 'conflicting_api_cause'):
            up('api', 1 + (sc['api'] - 1) * sev)
        if ty == 'conflicting_api_cause':
            up('cross', 1 + (sc['cross'] - 1) * 0.6 * sev)
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
        if ty == 'mimic_network_only':
            up('delay_ms', sc['delay_ms'] * m * sev)
            up('loss_pct', sc['loss_pct'] * m * sev)
    return k
