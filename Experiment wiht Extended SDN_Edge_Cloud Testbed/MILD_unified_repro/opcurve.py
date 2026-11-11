#!/usr/bin/env python3
"""
opcurve.py -- compare all methods at the SAME false-alarm rate (post hoc, no retraining).

Why
  Each method tunes its alert thresholds on its own validation period. On the
  test period the methods then end up at different false-alarm rates (for
  example MLP 11 FP/day, MILD 4 FP/day). Detection and lead time grow with the
  number of alarms, so comparing them at different FP rates mixes two effects.
  This script removes that effect. It reuses the test-period risk scores that
  every final job already saved (folds/fold1_timeseries.csv) and re-places the
  thresholds so that every method produces the same number of false-alarm
  steps per day on the test period. It then recomputes detection, lead time
  and disambiguation with the unchanged evaluator (mild_repro.core.evaluate).

How the thresholds are placed (identical rule for every method)
  For each intent, the threshold is the q-quantile of that intent's smoothed
  risk in healthy time (outside every event window). One common q is searched
  (bisection) so that the summed FP steps per day equal the target. The
  smoothing span W of each intent stays the one tuned on validation data.
  This is an operating-point analysis, like a ROC/AMOC curve. It uses the
  test period to place the thresholds, so it is reported in addition to the
  main protocol, never instead of it.

Usage
  python opcurve.py --train_dir ~/mild_sdn2_train_now/training \
                    --events_json ~/mild_sdn2_train_now/dataset/events_by_intent.json \
                    --targets 1,2,3,5,8

Output (in --train_dir/opcurve/)
  opcurve_runs.csv     one row per method x variant x seed x target FP/day
  opcurve_summary.csv  mean and std over seeds
  OPCURVE_REPORT.md    table + short interpretation
"""
from __future__ import annotations

import argparse
import glob
import json
import os

import numpy as np
import pandas as pd

from mild_repro import core

DISPLAY = {'mild': 'MILD', 'mlp': 'MLP', 'lstm': 'LSTM', 'wkpi': 'WKPI-Tuned', 'dist': 'Dist-Target', 'lr': 'LR-OvR'}


def final_jobs(train_dir):
    out = []
    for sp in glob.glob(os.path.join(train_dir, 'jobs', '*', '*', 'spec.json')):
        s = json.load(open(sp))
        d = os.path.dirname(sp)
        if not os.path.exists(os.path.join(d, 'DONE')):
            continue
        for role in s.get('roles', []):
            if not role.startswith('final:'):
                continue
            _, name, variant, seed = role.split(':')
            ts = (os.path.join(d, name, 'folds', 'fold1_timeseries.csv') if s['method'] == 'classic'
                  else os.path.join(d, 'folds', 'fold1_timeseries.csv'))
            if os.path.exists(ts):
                out.append({'name': name, 'variant': variant, 'seed': int(seed.replace('seed', '')), 'ts': ts})
    return out


def evaluate_at(ts, events, target):
    T = ts['t'].values.astype(int)
    intents = sorted(c[4:] for c in ts.columns if c.startswith('ema_'))
    te_ev = core.filter_events(events, int(T.min()), int(T.max()))
    wins = [(e['start'], e['end']) for v in te_ev.values() for e in v]
    healthy = ~core.in_any_window(T, wins)
    days = (T[-1] - T[0]) / core.MINUTES_PER_DAY
    ema = {k: ts[f'ema_{k}'].values for k in intents}
    gate = ts[[f'gate_{k}' for k in intents]].values if all(f'gate_{k}' in ts for k in intents) else None

    def fp_at(q):
        thr = {k: np.quantile(ema[k][healthy], q) if healthy.any() else np.inf for k in intents}
        fp = sum(int(np.count_nonzero((ema[k] > thr[k]) & healthy)) for k in intents)
        return fp / max(days, 1e-9), thr

    lo, hi = 0.5, 1.0
    for _ in range(40):                              # bisection on the common quantile
        mid = (lo + hi) / 2
        f, _ = fp_at(mid)
        if f > target:
            lo = mid
        else:
            hi = mid
    fp, thr = fp_at(hi)
    alerts = {k: T[ema[k] > thr[k]] for k in intents}
    m, rec, dys = core.evaluate(alerts, te_ev, T, ema, gate_probs=gate, fold_t0=T[0])
    rec['fold'], dys['fold'] = 1, 1
    det = np.mean([m['per_intent'][k]['detection_rate'] for k in intents])
    lts = rec.loc[rec['detected'] == 1, 'lead']
    return rec, dys, {'fp_per_day': m['overall']['fp_per_day'], 'detection': 100 * det,
            'lead_per_intent_mean': float(np.mean([m['per_intent'][k]['avg_lead_time'] for k in intents])),
            'lead_all_detected': float(lts.mean()) if len(lts) else 0.0,
            'disamb': 100 * m['overall']['disamb_accuracy'], 'n_events': int(len(rec)),
            **{f'det_{k}': 100 * m['per_intent'][k]['detection_rate'] for k in intents}}


def run(train_dir, events_json, targets='1,2,3,5,8', fp_budget=3.0):
    from mild_repro import stats as S
    events = json.load(open(events_json))
    out_dir = os.path.join(train_dir, 'opcurve')
    os.makedirs(out_dir, exist_ok=True)
    jobs = final_jobs(train_dir)
    if not jobs:
        raise RuntimeError('no finished final jobs found')
    tgs = sorted({float(x) for x in str(targets).split(',') if x} | {float(fp_budget)})
    rows, recs = [], {}
    for j in jobs:
        ts = pd.read_csv(j['ts'])
        for tg in tgs:
            rec, dys, r = evaluate_at(ts, events, tg)
            rows.append({'method': DISPLAY[j['name']], 'variant': j['variant'], 'seed': j['seed'], 'target_fp': tg, **r})
            if tg == float(fp_budget) and j['variant'] == 'tuned':
                recs[(j['name'], j['seed'])] = (rec, dys)
    df = pd.DataFrame(rows).drop_duplicates(['method', 'variant', 'seed', 'target_fp'])
    df.to_csv(os.path.join(out_dir, 'opcurve_runs.csv'), index=False)
    cols = ['fp_per_day', 'detection', 'lead_all_detected', 'disamb']
    summ = df.groupby(['target_fp', 'variant', 'method'])[cols].agg(['mean', 'std']).round(2)
    summ.to_csv(os.path.join(out_dir, 'opcurve_summary.csv'))
    # paired tests at the operating point = FP budget (tuned variants, per seed, then consensus)
    sig_rows = []
    for (name, sd), (rm, dm) in recs.items():
        if name != 'mild':
            continue
        for b in ['mlp', 'lstm', 'wkpi', 'dist', 'lr']:
            if (b, sd) not in recs:
                continue
            rb, db = recs[(b, sd)]
            for r in S.compare(rm, rb, dm, db)[:3]:          # detection, disambiguation, lead (FP is equal by design)
                r.update({'baseline_name': DISPLAY[b], 'seed': sd})
                sig_rows.append(r)
    sig = pd.DataFrame(sig_rows)
    if not sig.empty:
        by_seed = S.holm_and_verdict(sig, ('seed', 'metric'))
        by_seed.to_csv(os.path.join(out_dir, 'significance_equal_fp_by_seed.csv'), index=False)
        sig = S.consensus(by_seed)
        sig.to_csv(os.path.join(out_dir, 'significance_equal_fp.csv'), index=False)
    spread = (df[(df['target_fp'] == float(fp_budget)) & (df['variant'] == 'tuned')]
              .groupby('method')[['detection', 'disamb']].std(ddof=0).round(2))
    spread.to_csv(os.path.join(out_dir, 'seed_spread_equal_fp.csv'))
    L = ['# Comparison at equal false-alarm rate (post hoc operating points)', '',
         'Each method is re-thresholded on the test period so that all methods produce the same number of',
         'false-alarm steps per day. Same rule for every method (common healthy-time quantile, bisection).',
         'The achieved FP rate can be slightly below the target because FP steps are discrete.',
         'Mean over seeds. This analysis complements, and does not replace, the main protocol.', '']
    if not sig.empty:
        L += [f'## Paired tests at the FP budget ({fp_budget:g}/day target), consensus over seeds', '',
              sig[['baseline_name', 'metric', 'n_units', 'mild', 'baseline', 'diff', 'p_holm', 'seeds_mild_better',
                   'seeds_baseline_better', 'verdict']].round(3).to_string(index=False), '']
    L += ['## Seed spread at the FP budget (std over seeds, percentage points)', '', spread.to_string(), '']
    for variant in ('tuned', 'paper'):
        sub = df[df['variant'] == variant]
        if sub.empty:
            continue
        L += [f'## Variant: {variant}', '']
        for tg, g in sub.groupby('target_fp'):
            t = g.groupby('method')[cols].mean().round(2).sort_values('disamb', ascending=False)
            L += [f'### Target {tg:g} FP steps/day', '', t.to_string(), '']
    with open(os.path.join(out_dir, 'OPCURVE_REPORT.md'), 'w') as f:
        f.write('\n'.join(L) + '\n')
    return df, sig, spread


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--train_dir', required=True)
    ap.add_argument('--events_json', required=True)
    ap.add_argument('--targets', default='1,2,3,5,8')
    ap.add_argument('--fp_budget', type=float, default=3.0)
    a = ap.parse_args()
    run(a.train_dir, a.events_json, a.targets, a.fp_budget)
    print(open(os.path.join(a.train_dir, 'opcurve', 'OPCURVE_REPORT.md')).read())


if __name__ == '__main__':
    main()
