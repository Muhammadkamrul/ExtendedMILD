#!/usr/bin/env python3
"""confirm_v6.py -- ONE confirmatory held-out run of the FROZEN V6 configurations on the full v5 dataset.

No search, no re-ranking, no tuning: every configuration is read from the frozen JSON files given on the command
line. Before any model is trained, the script writes PREREG_RECORD.json (dataset hashes, row counts, the test
boundary, the configurations and the code hash) and refuses to run if the test part would include any pilot row.

  python confirm_v6.py --data_csv D/dataset.csv --events_json D/events_by_intent.json \
      --selected selected_configurations.json --mlp_matched mlp_trial10.json --out OUT --seeds 42,...,51

Outputs in OUT: PREREG_RECORD.json, final_test_runs.csv, final_test_summary.csv, per_intent_summary.csv,
seeds_used.csv, CONFIRM_REPORT.md. Resumable (finished jobs are reused).
"""
import argparse
import hashlib
import json
import os
import sys
import time

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault('MILD_FP_BIN_TICKS', '240')
import autotrain as A                    # noqa: E402
from mild_repro import core              # noqa: E402
from mild_repro import paper_config as P  # noqa: E402

CLASSIC_SUBS = {'lr': 'LR-OvR', 'wkpi': 'WKPI-Tuned', 'dist': 'Dist-Target'}


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def code_hash():
    h = hashlib.sha256()
    for fn in ['autotrain.py', 'confirm_v6.py', 'mild_repro/v6.py', 'mild_repro/core.py', 'mild_repro/models.py',
               'mild_repro/runners.py', 'mild_repro/classic_v6.py', 'mild_repro/stats.py']:
        p = os.path.join(HERE, fn)
        if os.path.exists(p):
            with open(p, 'rb') as f:
                h.update(f.read())
    return h.hexdigest()



def write_descriptive_report(fr, out, record):
    if fr.duplicated(['method', 'seed']).any():
        raise ValueError('Duplicate method/seed rows in final_test_runs.csv')
    seed_rows = []
    for method, g in fr.groupby('method'):
        seeds = sorted(set(int(x) for x in g['seed']))
        seed_rows.append(dict(method=method, n_seeds=len(seeds), seeds=','.join(map(str,seeds))))
    seed_summary = pd.DataFrame(seed_rows)
    seed_summary.to_csv(os.path.join(out, 'seeds_used.csv'), index=False)
    cols = ['detection', 'disamb', 'fp_per_day', 'lead', 'fp_episodes_per_day']
    summ = fr.groupby('method')[cols].agg(['mean', 'std']).round(2)
    summ.to_csv(os.path.join(out, 'final_test_summary.csv'))
    pic = [c for c in fr if c.startswith('det_')]
    per_intent = fr.groupby('method')[pic].mean().round(1)
    per_intent.to_csv(os.path.join(out, 'per_intent_summary.csv'))
    neural = fr[fr.method.isin(['MILD', 'MLP', 'LSTM', 'MLP (MILD settings)'])]
    budget = neural.groupby('method')['fp_per_day'].agg(
        seeds_over_budget_3=lambda x: int((x > 3).sum()), max_fp='max').round(2)
    L = ['# V6 held-out evaluation (frozen configurations)', '',
         f'Dataset: {record.get("dataset_csv", "see execution record")}; '
         f'rows: {record.get("rows", "unknown")}; development rows: {record.get("dev_rows", "unknown")}.', '',
         'Execution record: PREREG_RECORD.json. Existing historical records are preserved.', '',
         '## Means (std) over seeds', '', summ.to_string(), '',
         '## Per-intent detection (mean)', '', per_intent.to_string(), '',
         '## FP budget (total 3 steps / 1440 ticks)', '', budget.to_string(), '',
         '## Seeds used (successful saved runs)', '', seed_summary.to_string(index=False), '',
         'Seed counts are per method. A deterministic classical baseline may have only one saved run. '
         'Means and standard deviations use the available runs listed above. '
         'No significance tests or win/loss/tie comparisons are computed.', '',
         '']
    text = '\n'.join(L)
    Path = __import__('pathlib').Path
    Path(out, 'CONFIRM_REPORT.md').write_text(text)
    print(text, flush=True)


def main():
    if '--from_jobs' in sys.argv:
        sys.argv.remove('--from_jobs')
        from recompute_saved_jobs import main as recompute
        recompute()
        return
    if '--report_only' in sys.argv:
        rp = argparse.ArgumentParser(description='Regenerate descriptive reports without training')
        rp.add_argument('--report_only', action='store_true')
        rp.add_argument('--out', required=True)
        ra = rp.parse_args()
        out = os.path.abspath(os.path.expanduser(ra.out))
        rec = os.path.join(out, 'PREREG_RECORD.json')
        record = json.load(open(rec)) if os.path.exists(rec) else {}
        write_descriptive_report(pd.read_csv(os.path.join(out, 'final_test_runs.csv')), out, record)
        return
    ap = argparse.ArgumentParser()
    ap.add_argument('--data_csv', required=True)
    ap.add_argument('--events_json', required=True)
    ap.add_argument('--selected', required=True, help='selected_configurations.json (frozen MILD, MLP, LSTM)')
    ap.add_argument('--mlp_matched', default='', help='frozen MLP with MILD\'s generic settings (secondary)')
    ap.add_argument('--out', required=True)
    ap.add_argument('--seeds', default='42,43,44,45,46,47,48,49,50,51')
    ap.add_argument('--test_frac', type=float, default=0.30)
    ap.add_argument('--pilot_rows', type=int, default=14400,
                    help='rows already examined (pilot); the test part must start after them')
    ap.add_argument('--no_classic', action='store_true')
    a = ap.parse_args()
    out = os.path.abspath(os.path.expanduser(a.out))
    os.makedirs(out, exist_ok=True)
    dc, ej = os.path.abspath(os.path.expanduser(a.data_csv)), os.path.abspath(os.path.expanduser(a.events_json))
    df, ev, intents = core.load_dataset(dc, ej)
    N = len(df)
    dev_end = A.quiet_cut(df, ev, int((1 - a.test_frac) * N))
    if dev_end <= a.pilot_rows:
        sys.exit(f'ABORT: the test part would start at row {dev_end}, inside the {a.pilot_rows} pilot rows that were '
                 f'already examined. Collect more data first.')
    sel = json.load(open(a.selected))
    frozen = {'mild': ('mild', sel['mild']['cfg']), 'mlp': ('mlp', sel['mlp']['cfg']),
              'lstm': ('lstm', sel['lstm']['cfg'])}
    if a.mlp_matched:
        mm = json.load(open(a.mlp_matched))
        frozen['mlp_matched'] = ('mlp', mm.get('cfg', mm))
    if not a.no_classic:   # untuned reference: paper classic profile, same V6 calibration path
        cc = dict(P.CLASSIC['oran'], protocol_v6=True, threshold_mode='holdout', features='onset', fp_budget=1.0,
                  thr_budget_fixed=True, val_frac=0.4, fp_scope='own')
        cc.pop('folds', None)
        frozen['classic'] = ('classic', cc)
    rec_path = os.path.join(out, 'PREREG_RECORD.json')
    record = {'written': time.strftime('%Y-%m-%d %H:%M:%S'), 'dataset_csv': dc, 'dataset_sha256': sha(dc),
              'events_json': ej, 'events_sha256': sha(ej), 'rows': N, 'dev_rows': dev_end, 'test_rows': N - dev_end,
              'test_t_start': int(df.t.iloc[dev_end]), 'pilot_rows_examined': a.pilot_rows,
              'events_per_intent': {k: len(v) for k, v in ev.items()}, 'seeds': a.seeds,
              'code_sha256': code_hash(), 'frozen_configurations': {k: v[1] for k, v in frozen.items()},
              'primary_endpoint': 'macro per-intent detection, own-scope alarms, thresholds calibrated on '
                                  'development data at 1 FP step/1440 ticks/intent; MILD vs MLP (PREREG trial) '
                                  'and MILD vs LSTM; descriptive metric summaries',
              'secondary': ['DA', 'macro lead over each model’s detections', 'FP steps/1440 ticks (constraint: total <= 3)',
                            'telemetry victim detection', 'MILD vs MLP with MILD\'s generic settings']}
    if os.path.exists(rec_path):
        old = json.load(open(rec_path))
        for k in ('dataset_sha256', 'events_sha256', 'dev_rows', 'frozen_configurations'):
            if old.get(k) != record[k]:
                sys.exit(f'ABORT: {k} differs from the existing PREREG_RECORD.json. Use a new --out folder; '
                         'never change the data or the configurations of a confirmatory run.')
    else:
        with open(rec_path, 'w') as f:
            json.dump(record, f, indent=2)
    print(f'rows {N}, development [0, {dev_end}), TEST [{dev_end}, {N}) -- record: {rec_path}', flush=True)

    seeds = [int(s) for s in a.seeds.split(',')]
    rows, dirs = [], {}
    for name, (method, cfg0) in frozen.items():
        for sd in seeds:
            cfg = dict(cfg0, test_range=[dev_end, N], seed=sd)
            res, d = A.run_job(out, dc, ej, method, cfg, f'confirm:{name}:seed{sd}')
            if res is None:
                print('FAILED', name, sd, flush=True)
                continue
            subs = CLASSIC_SUBS if method == 'classic' else {None: None}
            for sub in subs:
                disp = CLASSIC_SUBS[sub] if sub else {'mild': 'MILD', 'mlp': 'MLP', 'lstm': 'LSTM',
                                                      'mlp_matched': 'MLP (MILD settings)'}[name]
                m = A.macro_of(res, method, sub)
                rows.append({'method': disp, 'seed': sd, **{k: v for k, v in m.items() if k != 'per_intent_detection'},
                             **{f'det_{k}': v for k, v in m['per_intent_detection'].items()}})
                dirs[(disp, sd)] = A.events_dir(d, method, sub)
            print(rows[-1]['method'], sd, {k: round(rows[-1][k], 2) for k in ('detection', 'disamb', 'fp_per_day', 'lead')},
                  flush=True)
            if method == 'classic':
                break                       # the classic models do not depend on the seed
    fr = pd.DataFrame(rows)
    fr.to_csv(os.path.join(out, 'final_test_runs.csv'), index=False)
    cols = ['detection', 'disamb', 'fp_per_day', 'lead', 'fp_episodes_per_day']
    summ = fr.groupby('method')[cols].agg(['mean', 'std']).round(2)
    summ.to_csv(os.path.join(out, 'final_test_summary.csv'))
    pic = [c for c in fr.columns if c.startswith('det_')]
    fr.groupby('method')[pic].mean().round(1).to_csv(os.path.join(out, 'per_intent_summary.csv'))

    write_descriptive_report(fr, out, record)


if __name__ == '__main__':
    main()
