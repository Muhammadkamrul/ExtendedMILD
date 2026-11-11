#!/usr/bin/env python3
"""classic_tuned.py -- TUNED classic baselines (LR-OvR, WKPI-Tuned, Dist-Target) under the same V6 calibration as
confirm_v6.py (own-scope false alarms, 1 FP step / 1440 ticks / intent, holdout calibration, onset inputs).

1. search on DEVELOPMENT data only: every candidate label horizon (build-up quantiles + 120) is trained on
   dev[:inner] and scored on the inner validation [inner, dev_end) with autotrain's objective; the best horizon is
   chosen separately for each classic method;
2. one final run of each chosen configuration on the held-out test part [dev_end, N) (same split as confirm_v6.py).
The classic models do not depend on the random seed, so each is run once.

  python classic_tuned.py --data_csv D/dataset.csv --events_json D/events_by_intent.json --out OUT
Outputs in OUT: classic_search.csv, classic_selected.json, classic_test.csv, CLASSIC_REPORT.md. Resumable.
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault('MILD_FP_BIN_TICKS', '240')
import autotrain as A                    # noqa: E402
from mild_repro import core              # noqa: E402
from mild_repro import paper_config as P  # noqa: E402

SUBS = {'lr': 'LR-OvR', 'wkpi': 'WKPI-Tuned', 'dist': 'Dist-Target'}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data_csv', required=True)
    ap.add_argument('--events_json', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--test_frac', type=float, default=0.30)
    ap.add_argument('--fp_budget_total', type=float, default=3.0)
    ap.add_argument('--calibration', default='v6', choices=['v6', 'standard'],
                    help='v6: the V6 holdout calibration of confirm_v6.py; standard: the v2.7 classic runner (as in '
                         'mild-oran train / Z5)')
    ap.add_argument('--fp_scope', default='own', choices=['own', 'any'])
    a = ap.parse_args()
    out = os.path.abspath(os.path.expanduser(a.out))
    os.makedirs(out, exist_ok=True)
    dc, ej = os.path.abspath(os.path.expanduser(a.data_csv)), os.path.abspath(os.path.expanduser(a.events_json))
    df, ev, intents = core.load_dataset(dc, ej)
    N = len(df)
    dev_end = A.quiet_cut(df, ev, int((1 - a.test_frac) * N))
    inner = A.quiet_cut(df, ev, int(0.75 * dev_end))
    t_dev = df.t.iloc[dev_end - 1]
    bu = [e['failure_time'] - e['start'] for v in ev.values() for e in v if e['end'] <= t_dev]
    lt_ref = float(np.median(bu))
    Hc = sorted({int(max(3, round(np.quantile(bu, q)))) for q in (0.3, 0.5, 0.7, 0.9)} | {120})
    print(f'rows {N}; development [0, {dev_end}); search: train [0, {inner}), validate [{inner}, {dev_end}); '
          f'TEST [{dev_end}, {N}); median build-up {lt_ref:.1f}; horizons {Hc}', flush=True)
    if a.calibration == 'v6':
        base = dict(P.CLASSIC['oran'], protocol_v6=True, threshold_mode='holdout', features='onset', fp_budget=1.0,
                    thr_budget_fixed=True, val_frac=0.4, fp_scope=a.fp_scope, seed=42)
    else:
        base = dict(P.CLASSIC['oran'], fp_budget=1.0, thr_budget_fixed=True, val_frac=0.4, fp_scope=a.fp_scope,
                    seed=42)
    base.pop('folds', None)

    rows, best = [], {}
    for h in Hc:
        c = dict(base, label_horizon=h, test_range=[inner, dev_end], row_limit=dev_end)
        res, d = A.run_job(out, dc, ej, 'classic', c, f'classic_search:h{h}')
        if res is None:
            print('FAILED search', h, flush=True)
            continue
        for sub, disp in SUBS.items():
            m = A.macro_of(res, 'classic', sub)
            sc = A.objective(m, a.fp_budget_total, lt_ref)
            rows.append({'method': disp, 'label_horizon': h, 'score': round(sc, 2),
                         **{k: round(m[k], 2) for k in ('detection', 'disamb', 'fp_per_day', 'lead')}})
            print(rows[-1], flush=True)
            if sub not in best or sc > best[sub]['score']:
                best[sub] = {'score': sc, 'label_horizon': h}
    pd.DataFrame(rows).to_csv(os.path.join(out, 'classic_search.csv'), index=False)
    json.dump(best, open(os.path.join(out, 'classic_selected.json'), 'w'), indent=2)

    test = []
    for h in sorted({b['label_horizon'] for b in best.values()}):
        c = dict(base, label_horizon=h, test_range=[dev_end, N])
        res, d = A.run_job(out, dc, ej, 'classic', c, f'classic_test:h{h}')
        if res is None:
            print('FAILED test', h, flush=True)
            continue
        for sub, disp in SUBS.items():
            if best.get(sub, {}).get('label_horizon') != h:
                continue
            m = A.macro_of(res, 'classic', sub)
            test.append({'method': disp, 'label_horizon': h,
                         **{k: round(m[k], 2) for k in ('detection', 'disamb', 'fp_per_day', 'lead')},
                         **{f'det_{k}': round(v, 1) for k, v in m['per_intent_detection'].items()}})
    T = pd.DataFrame(test)
    T.to_csv(os.path.join(out, 'classic_test.csv'), index=False)
    L = ['# Tuned classic baselines (V6 calibration, held-out test)', '',
         f'Data {dc}: {N} rows. Development [0, {dev_end}), TEST [{dev_end}, {N}) -- the same split as confirm_v6.py.',
         'Each method\'s label horizon was chosen on the inner validation of the development part only; thresholds '
         'at 1 FP step / 1440 ticks / intent (own scope), holdout calibration. Deterministic: one run each.', '',
         f'Calibration path: {a.calibration}; false-alarm scope: {a.fp_scope}.', '', '## Search on development data', '', pd.DataFrame(rows).to_string(index=False), '',
         '## Held-out test', '', T.to_string(index=False), '']
    open(os.path.join(out, 'CLASSIC_REPORT.md'), 'w').write('\n'.join(L) + '\n')
    print('\n'.join(L), flush=True)


if __name__ == '__main__':
    main()
