#!/usr/bin/env python3
"""devcompare.py -- model development on the DEVELOPMENT part only (never touches the test part).

Trains model variants on dev[:inner] and scores them on the inner validation period dev[inner:dev_end] (the same
split autotrain uses for its search), at the main operating point and at equal false-alarm rate.

  python devcompare.py --data_csv D/dataset.csv --events_json D/events_by_intent.json --out OUT \
      --variants mild,mild_residual,mlp,lstm --seeds 42,43,44
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
import autotrain as A          # noqa: E402
import opcurve as O            # noqa: E402
from mild_repro import core    # noqa: E402
from mild_repro import paper_config as P  # noqa: E402

HZ = [10]     # --horizon sets the horizon of the *_h variants

VARIANTS = {
    'mild': ('mild', lambda: dict(P.MILD['oran'])),
    'mild_residual': ('mild', lambda: dict(P.MILD['oran'], gate_mode='residual')),
    'mild_res_onset': ('mild', lambda: dict(P.MILD['oran'], gate_mode='residual', features='onset')),
    'mild_res_onset_nodist': ('mild', lambda: dict(P.MILD['oran'], gate_mode='residual', features='onset',
                                                   distill_alpha=1.0)),
    'mild_res_nodist': ('mild', lambda: dict(P.MILD['oran'], gate_mode='residual', distill_alpha=1.0)),
    'mild_res_onset_nodist_h14': ('mild', lambda: dict(P.MILD['oran'], gate_mode='residual', features='onset',
                                                       distill_alpha=1.0, horizon=14)),
    'mild_onset': ('mild', lambda: dict(P.MILD['oran'], features='onset')),
    'mlp': ('mlp', lambda: dict(P.MLP['oran'])),
    'mild_v24': ('mild', lambda: dict(P.MILD['oran'], gate_mode='residual', features='trend', distill_alpha=1.0)),
    'mild_v24_h': ('mild', lambda: dict(P.MILD['oran'], gate_mode='residual', features='trend', distill_alpha=1.0,
                                        horizon=HZ[0])),
    'mild_v23_h': ('mild', lambda: dict(P.MILD['oran'], gate_mode='residual', features='onset', distill_alpha=1.0,
                                        horizon=HZ[0])),
    'mlp_h': ('mlp', lambda: dict(P.MLP['oran'], horizon=HZ[0])),
    'lstm_h': ('lstm', lambda: dict(P.LSTM['oran'], horizon=HZ[0])),
    'mlp_trend_h': ('mlp', lambda: dict(P.MLP['oran'], features='trend', horizon=HZ[0])),
    'mlp_onset': ('mlp', lambda: dict(P.MLP['oran'], features='onset')),
    'lstm': ('lstm', lambda: dict(P.LSTM['oran'])),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data_csv', required=True)
    ap.add_argument('--events_json', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--variants', default='mild,mild_residual,mlp,lstm')
    ap.add_argument('--seeds', default='42,43,44')
    ap.add_argument('--test_frac', type=float, default=0.30)
    ap.add_argument('--targets', default='1,2,3')
    ap.add_argument('--horizon', type=int, default=10)
    a = ap.parse_args()
    HZ[0] = a.horizon
    dc, ej = os.path.abspath(a.data_csv), os.path.abspath(a.events_json)
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    df, ev, _ = core.load_dataset(dc, ej)
    N = len(df)
    dev_end = A.quiet_cut(df, ev, int((1 - a.test_frac) * N))
    inner = A.quiet_cut(df, ev, int(0.75 * dev_end))
    print(f'rows {N}, dev_end {dev_end}, inner validation [{inner}, {dev_end}) -- the test part is not used', flush=True)
    rows, oc = [], []
    for v in a.variants.split(','):
        method, mk = VARIANTS[v]
        for sd in [int(x) for x in a.seeds.split(',')]:
            c = mk()
            c.pop('folds', None)
            c.update(fp_budget=3.0, fp_block_ticks=240, thr_rule='expected_lead', test_range=[inner, dev_end],
                     row_limit=dev_end, seed=sd)
            res, d = A.run_job(out, dc, ej, method, c, f'dev:{v}:seed{sd}')
            if res is None:
                print('FAILED', v, sd, flush=True)
                continue
            m = A.macro_of(res, method)
            rows.append({'variant': v, 'seed': sd, **{k: m[k] for k in ('detection', 'lead', 'fp_per_day', 'disamb')}})
            ts = pd.read_csv(os.path.join(d, 'folds', 'fold1_timeseries.csv'))
            for tg in [float(x) for x in a.targets.split(',')]:
                _, _, r = O.evaluate_at(ts, ev, tg)
                oc.append({'variant': v, 'seed': sd, 'target_fp': tg, **r})
            print(rows[-1], flush=True)
    R = pd.DataFrame(rows)
    C = pd.DataFrame(oc)
    R.to_csv(os.path.join(out, 'dev_main.csv'), index=False)
    C.to_csv(os.path.join(out, 'dev_equal_fp.csv'), index=False)
    pd.set_option('display.width', 200)
    print('\n== main operating point (dev inner validation), mean over seeds')
    print(R.groupby('variant')[['detection', 'lead', 'fp_per_day', 'disamb']].mean().round(2).to_string())
    print('\n== equal FP (dev inner validation), mean over seeds')
    print(C.groupby(['target_fp', 'variant'])[['fp_per_day', 'detection', 'lead_all_detected', 'disamb']]
          .mean().round(2).to_string())


if __name__ == '__main__':
    if '--protocol_v6' in sys.argv:
        sys.argv.remove('--protocol_v6')
        from v6_devcompare import main as v6_main
        v6_main()
    else:
        main()
