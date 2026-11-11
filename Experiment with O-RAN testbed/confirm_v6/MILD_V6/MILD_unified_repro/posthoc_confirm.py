#!/usr/bin/env python3
"""posthoc_confirm.py -- equal-false-alarm analysis and actionable-alert table for a FINISHED confirm_v6.py run,
from its saved test predictions. No training.

  python posthoc_confirm.py --run ~/intact/mild/compare_mlp_frozen --events_json D/events_by_intent.json
  python posthoc_confirm.py --run ~/intact/mild/train_v5/training --events_json D/events_by_intent.json   (all methods incl. classic)

Writes into --run: EQUAL_FP_REPORT.md, equal_fp_runs.csv, equal_fp_paired.csv, fp_for_detection.csv,
actionable_runs.csv, actionable_summary.csv.

Equal-FP: thresholds are re-placed ON THE TEST PART with the same common-quantile rule for every model so that
all reach the same false-alarm rate (own-intent false alarms). This is a post-hoc operating-curve comparison of
score quality, not a deployable calibration -- report it as such, next to the main (development-calibrated) result.
Actionable (from the main run's alarms): an episode counts when the intent's own alarm fires >= --early ticks before
the failure; 'correct-cause actionable' additionally needs the correct root cause, on root episodes.
"""
import argparse
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import opcurve as O   # noqa: E402

IGNORE = {'seed', 'test_range', 'row_limit', 'implementation_sha256'}
DISPLAY = {'mild': 'MILD', 'mlp': 'MLP', 'lstm': 'LSTM', 'mlp_matched': 'MLP (MILD settings)'}


def strip(c):
    return {k: v for k, v in c.items() if k not in IGNORE}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', required=True)
    ap.add_argument('--events_json', required=True)
    ap.add_argument('--targets', default='1,2,3,5,8')
    ap.add_argument('--det_targets', default='70,80,90')
    ap.add_argument('--early', type=int, default=10, help='ticks before failure for an actionable alert (10 = 20 s)')
    a = ap.parse_args()
    run = os.path.abspath(os.path.expanduser(a.run))
    ev = json.load(open(os.path.expanduser(a.events_json)))
    O.FP_SCOPE[0] = 'own'
    jobs = []
    fj = os.path.join(run, 'final_jobs.json')
    if os.path.exists(fj):    # an autotrain / mild-oran train folder (e.g. train_v5/training): all methods incl. classic
        names = {'mild': 'MILD', 'mlp': 'MLP', 'lstm': 'LSTM', 'lr': 'LR-OvR', 'wkpi': 'WKPI-Tuned',
                 'dist': 'Dist-Target'}
        for j in json.load(open(fj))['jobs']:
            if j.get('variant', 'tuned') != 'tuned':
                continue
            d = j['dir']
            if not os.path.isdir(d):          # folder moved: rebuild the path under --run
                d = os.path.join(run, 'jobs', j['method'], os.path.basename(d))
            if j['method'] == 'classic':
                d = os.path.join(d, j['name'])
            jobs.append((names.get(j['name'], j['name']), int(j['seed']), d))
    else:                     # a confirm_v6.py folder
        rec = json.load(open(os.path.join(run, 'PREREG_RECORD.json')))
        frozen = {k: strip(v) for k, v in rec['frozen_configurations'].items() if k != 'classic'}
        for sp in glob.glob(os.path.join(run, 'jobs', '*', '*', 'spec.json')):
            d = os.path.dirname(sp)
            if not os.path.exists(os.path.join(d, 'DONE')):
                continue
            cfg = json.load(open(sp))['cfg']
            name = next((n for n, f in frozen.items() if strip(cfg) == f), None)
            if name is not None:
                jobs.append((DISPLAY.get(name, name), int(cfg['seed']), d))
    jobs = [j for j in jobs if os.path.exists(os.path.join(j[2], 'folds', 'fold1_timeseries.csv'))]
    if not jobs:
        sys.exit('no finished jobs with saved test predictions found in ' + run)
    print(f'{len(jobs)} finished jobs:', sorted({j[0] for j in jobs}), flush=True)

    eq, fpd, act = [], [], []
    tg = [float(x) for x in a.targets.split(',')]
    dt = [float(x) for x in a.det_targets.split(',')]
    for name, seed, d in sorted(jobs):
        ts = pd.read_csv(os.path.join(d, 'folds', 'fold1_timeseries.csv'))
        for t in tg:
            _, _, r = O.evaluate_at(ts, ev, t)
            eq.append({'method': name, 'seed': seed, 'target_fp': t, **r})
        for x in dt:
            fpd.append({'method': name, 'seed': seed, 'det_target': x, 'fp_needed': O.fp_for_detection(ts, ev, x)})
        e = pd.read_csv(os.path.join(d, 'folds', 'fold1_events.csv'))
        early = (e.detected == 1) & (e.lead >= a.early)
        root = e[e.root_paper == 1]
        r_early = (root.detected == 1) & (root.lead >= a.early)
        act.append({'method': name, 'seed': seed, 'events': len(e), 'root_events': len(root),
                    'detected_%': 100 * e.detected.mean(),
                    'early_detected_%': 100 * early.mean(),
                    'correct_cause_%_root': 100 * root.cause_correct.mean(),
                    'correct_cause_actionable_%_root': 100 * (r_early & (root.cause_correct == 1)).mean(),
                    **{f'early_{k}_%': 100 * early[e.intent == k].mean() for k in sorted(e.intent.unique())}})
    EQ, FD, AC = pd.DataFrame(eq), pd.DataFrame(fpd), pd.DataFrame(act)
    EQ.to_csv(os.path.join(run, 'equal_fp_runs.csv'), index=False)
    FD.to_csv(os.path.join(run, 'fp_for_detection.csv'), index=False)
    AC.to_csv(os.path.join(run, 'actionable_runs.csv'), index=False)

    pd.set_option('display.width', 220)
    cols = ['fp_per_day', 'detection', 'disamb', 'lead_all_detected'] + [c for c in EQ.columns if c.startswith('det_')]
    eqs = EQ.groupby(['target_fp', 'method'])[cols].mean().round(2)
    # paired over seeds: MILD minus each other method at the same target
    pr = []
    for t in tg:
        m = EQ[(EQ.target_fp == t) & (EQ.method == 'MILD')].set_index('seed')
        for b in sorted(set(EQ.method) - {'MILD'}):
            o = EQ[(EQ.target_fp == t) & (EQ.method == b)].set_index('seed')
            s = m.index.intersection(o.index)
            for k in ('detection', 'disamb', 'lead_all_detected'):
                dlt = (m.loc[s, k] - o.loc[s, k])
                pr.append({'target_fp': t, 'baseline': b, 'metric': k, 'mild': round(m.loc[s, k].mean(), 2),
                           'baseline_value': round(o.loc[s, k].mean(), 2), 'mean_diff': round(dlt.mean(), 2),
                           'seeds_mild_better': int((dlt > 0.5 if k != 'lead_all_detected' else dlt > 0.2).sum()),
                           'seeds_baseline_better': int((dlt < -0.5 if k != 'lead_all_detected' else dlt < -0.2).sum()),
                           'seeds': len(s)})
    PR = pd.DataFrame(pr)
    PR.to_csv(os.path.join(run, 'equal_fp_paired.csv'), index=False)
    fds = FD.groupby(['det_target', 'method']).fp_needed.agg(
        mean='mean', median='median', seeds_reaching='count').round(2)
    acs = AC.groupby('method').mean(numeric_only=True).drop(columns='seed').round(1)
    acs.to_csv(os.path.join(run, 'actionable_summary.csv'))
    L = ['# Post-hoc analyses of ' + run, '',
         'From the saved test predictions; no model was retrained. Own-intent false alarms (per 1,440 ticks).', '',
         '## Equal false-alarm rate (thresholds re-placed on the test part, same rule for every model)', '',
         'Post-hoc operating-curve comparison of score quality; not a deployable calibration.', '',
         eqs.to_string(), '', '### MILD minus each method at equal FP (paired by seed)', '', PR.to_string(index=False), '',
         '## False alarms needed to reach a detection rate (mean over seeds; seeds_reaching = seeds that reach it)', '',
         fds.to_string(), '',
         f'## Actionable alerts (main run, development-calibrated thresholds; early = own alarm >= {a.early} ticks '
         f'= {2 * a.early} s before the failure)', '', acs.to_string(), '',
         'correct_cause_actionable_%_root: on root episodes (independent and co-drift causes), detected early AND '
         'the correct root cause named.']
    open(os.path.join(run, 'EQUAL_FP_REPORT.md'), 'w').write('\n'.join(L) + '\n')
    print('\n'.join(L), flush=True)


if __name__ == '__main__':
    main()
