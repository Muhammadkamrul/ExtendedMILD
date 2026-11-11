#!/usr/bin/env python3
"""ablation.py -- v2.6: equal-input and gate ablation on the held-out test part, with the TUNED configurations of a
finished autotrain run (no new search, so it is fast and every variant differs in exactly one thing).

Variants (all with the same calibration, seeds and test part as the paper run):
  mild_v26                 published gate (expert x g), inputs = base KPIs + rolling stats + change features (d1, d3, z60)
  mild_gate_1plusg         same inputs, gate expert x (1+g) (MILD v2.3-v2.5)          -> does (1+g) help?
  mild_no_change_features  published gate, base + rolling only (= the published MILD) -> do the change features help?
  mlp_same_inputs          MLP with exactly MILD's inputs
  lstm_same_inputs         LSTM with exactly MILD's inputs
  mlp_base_inputs, lstm_base_inputs   MLP / LSTM with the base KPIs only (paper protocol up to v2.5)

  python ablation.py --run_dir ~/intact/mild/train_v25/training --out ~/intact/mild/ablation_v26 \
      --seeds 42,43,44,45,46 [--variants mild_v23,mild_multiply,...]
Resumable (finished jobs are reused). Writes ABLATION_REPORT.md and ablation_runs.csv.
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import autotrain as A   # noqa: E402

VARIANTS = {   # explicit settings, so the result does not depend on which architecture the run_dir was tuned with
    'mild_v26': ('mild', 'MILD', {'gate_mode': 'multiply', 'features': 'onset'}),
    'mild_gate_1plusg': ('mild', 'MILD', {'gate_mode': 'residual', 'features': 'onset'}),
    'mild_no_change_features': ('mild', 'MILD', {'gate_mode': 'multiply', 'features': 'paper'}),
    'mlp_same_inputs': ('mlp', 'MLP', {'features': 'onset'}),
    'lstm_same_inputs': ('lstm', 'LSTM', {'features': 'onset'}),
    'mlp_base_inputs': ('mlp', 'MLP', {'features': 'base'}),
    'lstm_base_inputs': ('lstm', 'LSTM', {'features': 'base'}),
}
METRICS = ['detection', 'disamb', 'fp_per_day', 'lead']
PAIRS = [('mild_v26', 'mild_gate_1plusg', 'published gate g vs (1+g), same inputs'),
         ('mild_v26', 'mild_no_change_features', 'with vs without change features, same gate'),
         ('mild_v26', 'mlp_same_inputs', 'MILD vs MLP, identical inputs'),
         ('mild_v26', 'lstm_same_inputs', 'MILD vs LSTM, identical inputs'),
         ('mlp_same_inputs', 'mlp_base_inputs', 'MLP: MILD inputs vs base inputs'),
         ('lstm_same_inputs', 'lstm_base_inputs', 'LSTM: MILD inputs vs base inputs')]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run_dir', required=True, help='the training/ folder of a finished autotrain run')
    ap.add_argument('--out', required=True)
    ap.add_argument('--seeds', default='42,43,44,45,46')
    ap.add_argument('--variants', default=','.join(VARIANTS))
    ap.add_argument('--data_csv', default='')
    ap.add_argument('--events_json', default='')
    a = ap.parse_args()
    run = os.path.abspath(os.path.expanduser(a.run_dir))
    out = os.path.abspath(os.path.expanduser(a.out))
    os.makedirs(out, exist_ok=True)
    dc = a.data_csv or os.path.join(os.path.dirname(run), 'dataset', 'dataset.csv')
    ej = a.events_json or os.path.join(os.path.dirname(run), 'dataset', 'events_by_intent.json')
    aud = json.load(open(os.path.join(run, 'audit.json')))
    N = aud['dev_rows'] + aud['test_rows']
    test_range = [aud['dev_rows'], N]
    f = pd.read_csv(os.path.join(run, 'final_test_runs.csv'))
    tuned = {m: json.loads(f[(f.method == m) & (f.variant == 'tuned')].cfg.iloc[0]) for m in ('MILD', 'MLP', 'LSTM')}
    fpb = os.environ.get('MILD_FP_BIN_TICKS')
    if not fpb and 'fp_block_ticks' in tuned['MILD']:
        os.environ['MILD_FP_BIN_TICKS'] = str(tuned['MILD']['fp_block_ticks'])
    print(f'data {dc}\ntest part rows [{test_range[0]}, {test_range[1]})', flush=True)
    rows = []
    for v in a.variants.split(','):
        method, disp, change = VARIANTS[v]
        for sd in [int(x) for x in a.seeds.split(',')]:
            c = dict(tuned[disp]); c.update(change); c.update(test_range=test_range, seed=sd)
            res, d = A.run_job(out, dc, ej, method, c, f'ablation:{v}:seed{sd}')
            if res is None:
                print('FAILED', v, sd, flush=True)
                continue
            m = A.macro_of(res, method)
            rows.append({'variant': v, 'seed': sd, **{k: m[k] for k in METRICS}})
            print({k: (round(x, 2) if isinstance(x, float) else x) for k, x in rows[-1].items()}, flush=True)
            pd.DataFrame(rows).to_csv(os.path.join(out, 'ablation_runs.csv'), index=False)
    R = pd.DataFrame(rows)
    L = ['# Ablation on the held-out test part (tuned configurations of ' + run + ')', '',
         f'Seeds {a.seeds}. Every variant uses the same data, split, calibration and seeds; it differs from its '
         'partner in exactly one thing. FP = false-alarm steps per 1,440 steps; lead in steps.', '',
         '## Mean (std) over seeds', '', '| variant | detection % | DA % | FP | lead |', '|---|---|---|---|---|']
    g = R.groupby('variant')[METRICS]
    for v in [x for x in VARIANTS if x in set(R.variant)]:
        L.append(f'| {v} | ' + ' | '.join(f'{g.mean().loc[v, k]:.2f} ({g.std().loc[v, k]:.2f})' for k in METRICS) + ' |')
    L += ['', '## Paired differences (first minus second; same seeds)', '',
          '| comparison | metric | mean diff | seeds first better | seeds second better |', '|---|---|---|---|---|']
    P = R.pivot(index='seed', columns='variant', values=METRICS)
    for x, y, lab in PAIRS:
        if x not in set(R.variant) or y not in set(R.variant):
            continue
        for k in METRICS:
            d = (P[(k, x)] - P[(k, y)]).dropna()
            if k == 'fp_per_day':
                better, worse = int((d < -0.25).sum()), int((d > 0.25).sum())
            else:
                better, worse = int((d > 0.25).sum()), int((d < -0.25).sum())
            L.append(f'| {lab} | {k} | {d.mean():+.2f} | {better} | {worse} |')
    open(os.path.join(out, 'ABLATION_REPORT.md'), 'w').write('\n'.join(L) + '\n')
    print('\n'.join(L), flush=True)


if __name__ == '__main__':
    main()
