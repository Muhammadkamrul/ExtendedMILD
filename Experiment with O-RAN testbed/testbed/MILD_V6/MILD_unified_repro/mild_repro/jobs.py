"""
jobs.py -- build the list of experiments and execute ONE experiment.

Each experiment ("job") is identified by its dataset, method and full
configuration. Its output folder is runs/<dataset>/<method>/<hash-of-config>/,
so identical configurations requested by different tables (for example the
main MILD run and the 'full' ablation row) are trained only once.

A job is complete when its folder contains DONE. The orchestrator runs every
job in a separate Python process (python -m mild_repro.jobs --spec ...), so a
crash or a kill never corrupts other results, and a re-run skips finished jobs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import traceback

from . import paper_config as P


def cfg_hash(method, dataset, cfg):
    blob = json.dumps({'m': method, 'd': dataset, 'c': cfg}, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:10]


def make_job(root, dataset, method, cfg, role):
    h = cfg_hash(method, dataset, cfg)
    return {'id': f'{dataset}/{method}/{h}', 'dataset': dataset, 'method': method, 'cfg': cfg,
            'outdir': os.path.join(root, 'runs', dataset, method, h), 'roles': [role]}


def build_jobs(root, datasets, groups, seeds=(42,), fold_resume=False):
    jobs = {}

    def add(dataset, method, cfg, role):
        cfg = dict(cfg)
        if fold_resume:
            cfg['allow_fold_resume'] = True
        j = make_job(root, dataset, method, cfg, role)
        if j['id'] in jobs:
            jobs[j['id']]['roles'].append(role)
        else:
            jobs[j['id']] = j

    for ds in datasets:
        for seed in seeds:
            tag = '' if seed == 42 else f'@seed{seed}'
            if 'main' in groups or (seed != 42 and 'seeds' in groups):
                add(ds, 'mild', dict(P.MILD[ds], seed=seed), f'main:MILD{tag}')
                add(ds, 'mlp', dict(P.MLP[ds], seed=seed), f'main:MLP{tag}')
                add(ds, 'lstm', dict(P.LSTM[ds], seed=seed), f'main:LSTM{tag}')
                add(ds, 'classic', dict(P.CLASSIC[ds], seed=seed), f'main:CLASSIC{tag}')
        if 'ablation' in groups:
            for v, ch in P.ABLATION_VARIANTS.items():
                cfg = dict(P.ABLATION_BASE_AS_RUN[ds], **ch)
                if v == 'full':
                    cfg = dict(P.MILD[ds])     # the paper's 'full' row is the main result
                add(ds, 'mild', cfg, f'ablation:{v}')
        if 'ablation_consistent' in groups:
            for v, ch in P.ABLATION_VARIANTS.items():
                add(ds, 'mild', dict(P.MILD[ds], **ch), f'ablation_consistent:{v}')
        if 'sensitivity' in groups:
            for param, values in P.SENSITIVITY[ds].items():
                for val in values:
                    base = P.MILD[ds] if param == 'distill_alpha' else P.ABLATION_BASE_AS_RUN[ds]
                    cfg = dict(base, **{param: val})
                    if val == P.MILD[ds][param]:
                        cfg = dict(P.MILD[ds])  # bold row = main configuration
                    add(ds, 'mild', cfg, f'sensitivity:{param}={val}')
        if 'opintel' in groups:
            add(ds, 'multihorizon', dict(P.MULTIHORIZON[ds]), 'opintel:multihorizon')
    return list(jobs.values())


# ----------------------------------------------------------------------------
# execute a single job (called in a subprocess)
# ----------------------------------------------------------------------------
def _configure_tf():
    os.environ.setdefault('TF_CPP_MIN_LOG_LEVEL', '3')
    try:
        import tensorflow as tf
        for g in tf.config.list_physical_devices('GPU'):
            tf.config.experimental.set_memory_growth(g, True)
        return [g.name for g in tf.config.list_physical_devices('GPU')], tf.__version__
    except Exception as exc:
        return [], f'unavailable ({exc})'


def execute(spec):
    from . import core, runners, opintel
    t0 = time.time()
    os.makedirs(spec['outdir'], exist_ok=True)
    gpus, tfv = _configure_tf()
    ds = spec['dataset']
    data_root = spec['data_root']
    if spec.get('data_csv'):            # explicit dataset (autotrain / new testbeds)
        csv, evj = spec['data_csv'], spec['events_json']
    else:
        csv = os.path.join(data_root, P.DATASETS[ds]['csv'])
        evj = os.path.join(data_root, P.DATASETS[ds]['events'])
    df, events, intent_ids = core.load_dataset(csv, evj)
    cfg, out, method = spec['cfg'], spec['outdir'], spec['method']
    if cfg.get('row_limit'):            # development-only view of the data (autotrain inner search)
        df = df.iloc[:int(cfg['row_limit'])].reset_index(drop=True)
    print(f"[job] {spec['id']} method={method} rows={len(df)} intents={intent_ids} gpus={gpus} tf={tfv}", flush=True)
    result = {'id': spec['id'], 'dataset': ds, 'method': method, 'cfg': cfg, 'tf_version': tfv, 'gpus': gpus}

    if method in ('mild', 'mlp', 'lstm'):
        fn = {'mild': runners.run_mild, 'mlp': runners.run_mlp, 'lstm': runners.run_lstm}[method]
        folds = fn(df, events, intent_ids, cfg, out)
        result['folds'] = folds
        result['aggregate'] = core.aggregate_folds(folds, intent_ids)
        if method == 'mild' and not spec.get('data_csv'):
            fold = P.DISAMB_PLOT_FOLD.get(ds)
            if fold and os.path.exists(os.path.join(out, 'folds', f'fold{fold}_timeseries.csv')):
                try:
                    result['disamb_plot'] = opintel.plot_disambiguation(out, fold, events,
                                                                         os.path.join(out, f'fold{fold}_disamb.png'))
                except Exception as exc:
                    result['disamb_plot_error'] = str(exc)
    elif method == 'classic':
        dirs = {m: os.path.join(out, m) for m in runners.CLASSIC_METHODS}
        if cfg.get('protocol_v6'):
            from .classic_v6 import run_classic_v6
            res = run_classic_v6(df, events, intent_ids, cfg, dirs)
        else:
            res = runners.run_classic(df, events, intent_ids, cfg, dirs)
        result['methods'] = {}
        for m, folds in res.items():
            agg = core.aggregate_folds(folds, intent_ids)
            result['methods'][m] = {'folds': folds, 'aggregate': agg}
    elif method == 'multihorizon':
        ttf = opintel.run_multihorizon_shap(df, events, intent_ids, cfg, out)
        result['ttf'] = ttf.to_dict(orient='records')
    else:
        raise ValueError(method)

    result['elapsed_s'] = time.time() - t0
    with open(os.path.join(out, 'result.json'), 'w') as f:
        json.dump(result, f, indent=2, default=lambda o: o.item() if hasattr(o, 'item') else str(o))
    with open(os.path.join(out, 'DONE'), 'w') as f:
        f.write(time.strftime('%Y-%m-%d %H:%M:%S'))
    print(f"[job] done in {result['elapsed_s']/60:.1f} min", flush=True)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--spec', required=True)
    a = ap.parse_args()
    with open(a.spec) as f:
        spec = json.load(f)
    try:
        execute(spec)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
