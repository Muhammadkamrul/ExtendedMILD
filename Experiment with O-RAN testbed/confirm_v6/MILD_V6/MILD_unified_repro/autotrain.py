#!/usr/bin/env python3
"""
autotrain.py -- automatic, resumable, fair training and comparison of MILD and
the five baselines on a NEWLY COLLECTED dataset (used by the Task 3 SDN testbed
and the Task 4 microservices testbed pipelines).

Protocol
  1. Audit the dataset (events per intent and type, build-up length, observed
     failures) and write audit.json.
  2. Chronological split: development set = first (1 - test_frac) of the rows,
     held-out test set = the rest. Each cut is moved to a quiet point, where no
     event window is active, so no event is split across sets.
  3. Hyper-parameter search on the development set only. The last 25 % of the
     development set is the inner validation block. Every method gets the same
     number of trials and the same objective. Each search always includes the
     paper configuration of the method.
  4. Final evaluation on the held-out test set with the best configuration of
     each method, for several seeds.
  5. Paired significance tests (event level, day level) on the test set.
  6. AUTOTRAIN_REPORT.md: tables, an honest verdict (does MILD beat the best
     baseline on FP/day and disambiguation first, then on lead time and
     detection?), diagnostics, and suggested fixes when it does not.

Every trial is an isolated, resumable job (folder with DONE). Re-running the
same command continues where it stopped.

Usage:
  python autotrain.py --data_csv merged/dataset.csv --events_json merged/events_by_intent.json \
                      --out autotrain_out --paper_profile sdn --trials 16
"""
import argparse
import json
import os
import subprocess
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mild_repro import core  # noqa: E402
from mild_repro import paper_config as P  # noqa: E402
from mild_repro import stats as S  # noqa: E402
from mild_repro.jobs import cfg_hash  # noqa: E402

NEURAL = ['mild', 'mlp', 'lstm']
CLASSIC_NAMES = {'wkpi': 'WKPI-Tuned', 'dist': 'Dist-Target', 'lr': 'LR-OvR'}
DISPLAY = {'mild': 'MILD', 'mlp': 'MLP', 'lstm': 'LSTM', **CLASSIC_NAMES}


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ----------------------------------------------------------------------------
# dataset audit and splits
# ----------------------------------------------------------------------------
def audit(df, ev):
    rows = []
    for k, L in ev.items():
        for e in L:
            rows.append({'intent': k, 'type': e['type'], 'buildup': e['failure_time'] - e['start'],
                         'dur': e['end'] - e['start'], 'observed': e.get('failure_observed', True)})
    a = pd.DataFrame(rows)
    out = {'rows': int(len(df)), 'events_total': int(len(a)),
           'events_by_intent': a.groupby('intent').size().to_dict(),
           'events_by_type': a.groupby('type').size().to_dict(),
           'observed_fraction_by_intent': a.groupby('intent')['observed'].mean().round(3).to_dict(),
           'buildup_quantiles': {str(q): float(a['buildup'].quantile(q)) for q in (0.1, 0.25, 0.5, 0.75, 0.9)},
           'missing_values': int(df.isna().sum().sum())}
    return out, a


def quiet_cut(df, ev, target_row):
    """Nearest row >= target where no event window [start, end] is active."""
    t = df['t'].values
    busy = core.in_any_window(t, [(e['start'], e['end']) for L in ev.values() for e in L])
    for r in range(int(target_row), len(t)):
        if not busy[r]:
            return r
    return int(target_row)


# ----------------------------------------------------------------------------
# search spaces (same budget and objective for every method)
# ----------------------------------------------------------------------------
def h_candidates(a_df, paper_h):
    qs = [a_df['buildup'].quantile(q) for q in (0.3, 0.5, 0.7, 0.9)]
    c = sorted({int(max(3, round(x))) for x in qs} | {int(paper_h)})
    return c


def sample_cfgs(method, base, rng, n, Hc):
    if base.get('protocol_v6') and method != 'classic':
        from mild_repro.v6search import sample_cfgs_v6
        return sample_cfgs_v6(method, base, rng, n, Hc)
    # the FP budget used inside threshold tuning is searched for EVERY method (same grid);
    # the reported FP/day is always judged against the same operational budget
    FPB = sorted({1.0, 2.0, float(base.get('fp_budget', 3.0))})
    if base.get('thr_budget_fixed'):            # v2.5 --thr_budget: one per-intent threshold budget, not searched
        FPB = [float(base['fp_budget'])]
    space = {
        'mild': {'fp_budget': FPB, 'horizon': Hc, 'distill_alpha': [0.5, 0.6, 0.7, 0.8, 0.9, 1.0], 'gate_supervise': [0.3, 0.5, 0.6, 0.7, 0.9],
                 'gate_teacher': [0.1, 0.3, 0.5, 0.7], 'gate_sparsity': [0.001, 0.002, 0.005],
                 'epochs': [20, 30], 'batch': [128, 256]},
        'mlp': {'fp_budget': FPB, 'horizon': Hc, 'epochs': [20, 30], 'batch': [128, 256]},
        'lstm': {'fp_budget': FPB, 'horizon': Hc, 'lookback': [20, 40, 60], 'epochs': [20, 30], 'batch': [128, 256]},
        'classic': {'fp_budget': FPB, 'label_horizon': Hc + [120]},
    }[method]
    out, seen = [dict(base)], {json.dumps(base, sort_keys=True)}
    tries = 0
    while len(out) < n and tries < 50 * n:
        tries += 1
        c = dict(base)
        for k, vals in space.items():
            c[k] = vals[int(rng.integers(len(vals)))]
        key = json.dumps(c, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def objective(m, fp_budget, lt_ref):
    """Higher is better. FP/day and disambiguation dominate, then lead and detection."""
    fp, da, det, lt = m['fp_per_day'], m['disamb'], m['detection'], m['lead']
    return (da + 0.5 * det + 25.0 * min(lt / max(lt_ref, 1e-9), 1.5)
            - 4.0 * fp - 20.0 * max(0.0, fp - fp_budget))


# ----------------------------------------------------------------------------
# job execution (each trial = one subprocess, resumable)
# ----------------------------------------------------------------------------
def data_fingerprint(*paths):
    import hashlib
    h = hashlib.sha1()
    for p in paths:
        st = os.stat(p)
        h.update(f'{os.path.abspath(p)}|{st.st_size}|{int(st.st_mtime)}'.encode())
    return h.hexdigest()[:10]


def run_job(out_root, data_csv, events_json, method, cfg, tag):
    # V6 cache keys include source bytes so revised code never reuses stale scores.
    if cfg.get('protocol_v6'):
        import hashlib
        hh=hashlib.sha256()
        for fn in ['mild_repro/v6.py','mild_repro/core.py','mild_repro/models.py','mild_repro/runners.py']:
            with open(os.path.join(HERE,fn),'rb') as ff: hh.update(ff.read())
        if method=='classic':
            with open(os.path.join(HERE,'mild_repro/classic_v6.py'),'rb') as ff: hh.update(ff.read())
        cfg=dict(cfg,implementation_sha256=hh.hexdigest())
    # the dataset fingerprint is part of the key: new data never reuses old results
    h = cfg_hash(method, 'custom|' + data_fingerprint(data_csv, events_json), cfg)
    d = os.path.join(out_root, 'jobs', method, h)
    os.makedirs(d, exist_ok=True)
    if not os.path.exists(os.path.join(d, 'DONE')):
        spec = {'id': f'custom/{method}/{h}', 'dataset': 'custom', 'method': method, 'cfg': cfg, 'outdir': d,
                'roles': [tag], 'data_root': HERE, 'data_csv': data_csv, 'events_json': events_json}
        with open(os.path.join(d, 'spec.json'), 'w') as f:
            json.dump(spec, f, indent=2)
        log(f'  run {method} {tag} -> {d}')
        with open(os.path.join(d, 'job.log'), 'a') as jl:
            rc = subprocess.call([sys.executable, '-u', '-m', 'mild_repro.jobs', '--spec', os.path.join(d, 'spec.json')],
                                 cwd=HERE, stdout=jl, stderr=subprocess.STDOUT,
                                 env=dict(os.environ, TF_CPP_MIN_LOG_LEVEL='3'))
        if rc != 0 or not os.path.exists(os.path.join(d, 'DONE')):
            log(f'  FAILED {method} {tag} (see {d}/job.log)')
            return None, d
    with open(os.path.join(d, 'result.json')) as f:
        return json.load(f), d


def macro_of(res, method, sub=None):
    agg = res['methods'][sub]['aggregate'] if method == 'classic' else res['aggregate']
    m = agg['macro']
    return {'detection': m['detection'], 'lead': m['lead'], 'fp_per_day': m['fp_per_day'], 'disamb': m['disamb'],
            'fp_episodes_per_day': m['fp_episodes_per_day'], 'disamb_all_roots': m['disamb_all_roots'],
            'per_intent_detection': {k: v['detection_mean'] for k, v in agg['per_intent'].items()}}


def events_dir(res_dir, method, sub=None):
    return os.path.join(res_dir, sub) if method == 'classic' else res_dir


def load_events(d):
    import glob
    ev = pd.concat([pd.read_csv(p) for p in glob.glob(os.path.join(d, 'folds', 'fold*_events.csv'))], ignore_index=True)
    dy = pd.concat([pd.read_csv(p) for p in glob.glob(os.path.join(d, 'folds', 'fold*_days.csv'))], ignore_index=True)
    return ev, dy


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data_csv', required=True)
    ap.add_argument('--events_json', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--paper_profile', default='sdn', choices=['statistical', 'microservices', 'sdn', 'oran'],
                    help='paper configuration used as the starting point of every search')
    ap.add_argument('--test_frac', type=float, default=0.30)
    ap.add_argument('--trials', type=int, default=20, help='trials per method (same for every method)')
    ap.add_argument('--fp_budget', type=float, default=3.0, help='operational FP/day budget (same for all)')
    ap.add_argument('--seeds', default='42,43,44')
    ap.add_argument('--search_seed', type=int, default=7)
    ap.add_argument('--rerank_top', type=int, default=3,
                    help='re-rank the K best search trials of every neural method with extra seeds (0 = off)')
    ap.add_argument('--rerank_seeds', default='43,44', help='extra seeds used for re-ranking')
    ap.add_argument('--opcurve_targets', default='1,2,3,5,8',
                    help='target FP/day values for the equal-FP comparison (empty = skip)')
    ap.add_argument('--thr_rule', default='expected_lead', choices=['expected_lead', 'lead'],
                    help='alert-threshold rule of the TUNED variant of every method (the paper variant always uses '
                         'the paper rule "lead"): expected_lead = detection x mean lead (a missed event counts 0); '
                         'lead = mean lead over detected events only (can choose thresholds that miss many events)')
    ap.add_argument('--mild_arch', default='v23', choices=['v23', 'v26', 'paper'],
                    help='v23 (default): MILD with the residual gate and the onset features (MILD v2.3); '
                         'v26: the published gate (expert x g) with the onset features; '
                         'paper: the published MILD (multiplicative gate, rolling features only)')
    ap.add_argument('--baseline_features', default='base', choices=['base', 'onset'],
                    help='base (paper protocol): MLP/LSTM use the ten base features; onset (v2.6): the MLP AND the LSTM get '
                         'MILD v2.3\'s input features (fairness ablation: separates the gain of the features from '
                         'the gain of the architecture)')
    ap.add_argument('--fp_scope', default='any', choices=['any', 'own'],
                    help="v2.7: what counts as a false alarm of intent k. any (paper): an alert outside the windows of "
                         "EVERY intent; own: an alert of intent k outside intent k's own event windows (an alarm on the "
                         "wrong tenant is false). Used for threshold calibration, evaluation and the equal-FP report")
    ap.add_argument('--variants', default='tuned,paper',
                    help='final-test variants to run: tuned,paper (default) or tuned')
    ap.add_argument('--val_frac', type=float, default=0.2,
                    help='v2.5: fraction of each training period held out for early stopping AND threshold calibration '
                         'of MILD, MLP and LSTM (paper: 0.2). 0.4 gives a calibration period with ~2x more look-alikes')
    ap.add_argument('--thr_budget', type=float, default=0.0,
                    help='v2.5: FP steps/day PER INTENT used to place every method\'s thresholds, in every variant, not '
                         'searched (0 = legacy: searched over 1, 2 and --fp_budget). 1.0 with 3 intents = 3/day in total')
    ap.add_argument('--fp_block', type=int, default=0,
                    help='ticks per block for the paired FP test (default one day = 1440). Use 240 for short test sets')
    ap.add_argument('--protocol_v6', action='store_true', help='Opt-in equal features, shared alarm-loss options and chronological OOF calibration')
    ap.add_argument('--threshold_mode', default='crossfit', choices=['holdout','crossfit'])
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    data_csv, events_json = os.path.abspath(a.data_csv), os.path.abspath(a.events_json)

    df, ev, intents = core.load_dataset(data_csv, events_json)
    aud, adf = audit(df, ev)
    N = len(df)
    dev_end = quiet_cut(df, ev, int((1 - a.test_frac) * N))
    inner = quiet_cut(df, ev, int(0.75 * dev_end))
    if a.protocol_v6:
        _, adf = audit(df.iloc[:dev_end], {k:[e for e in es if e['end'] < df.t.iloc[dev_end]] for k,es in ev.items()})
    lt_ref = float(adf['buildup'].median())
    aud.update({'dev_rows': dev_end, 'test_rows': N - dev_end, 'inner_validation_start': inner, 'lt_ref': lt_ref})
    with open(os.path.join(a.out, 'audit.json'), 'w') as f:
        json.dump(aud, f, indent=2, default=str)
    log(f'Audit: {N} rows, events {aud["events_by_intent"]}, dev={dev_end}, test={N - dev_end}, LT_ref={lt_ref:.1f}')
    if N - dev_end < 1000 or inner < 2000:
        log('ERROR: dataset too small for a development/test protocol. Collect more data.')
        sys.exit(2)

    prof = a.paper_profile
    base = {'mild': dict(P.MILD[prof]), 'mlp': dict(P.MLP[prof]), 'lstm': dict(P.LSTM[prof]),
            'classic': dict(P.CLASSIC[prof])}
    for m in base:
        base[m]['fp_budget'] = a.fp_budget
        base[m].pop('folds', None)
        if a.fp_scope != 'any':                             # v2.7 (part of the job key)
            base[m]['fp_scope'] = a.fp_scope
        if a.thr_budget > 0:                                # v2.5 (part of the job key)
            base[m]['fp_budget'] = a.thr_budget
            base[m]['thr_budget_fixed'] = True
        if abs(a.val_frac - 0.2) > 1e-9 and m in ('mild', 'mlp', 'lstm'):
            base[m]['val_frac'] = a.val_frac                # v2.5 (part of the job key)
    if a.thr_budget > 0 or abs(a.val_frac - 0.2) > 1e-9:
        log(f'v2.5 threshold calibration: per-intent threshold budget '
            f'{a.thr_budget if a.thr_budget > 0 else "searched"}, validation fraction {a.val_frac} (same for every method)')
        if a.fp_block > 0:
            base[m]['fp_block_ticks'] = a.fp_block          # part of the job key, so cached jobs are not mixed
    if a.mild_arch == 'v23':
        base['mild'].update(gate_mode='residual', features='onset', distill_alpha=1.0)
    elif a.mild_arch == 'v26':
        base['mild'].update(gate_mode='multiply', features='onset', distill_alpha=1.0)
    if a.baseline_features == 'onset':
        base['mlp']['features'] = 'onset'
        base['lstm']['features'] = 'onset'          # v2.6: both neural baselines get exactly MILD's inputs
    if a.fp_block > 0:
        os.environ['MILD_FP_BIN_TICKS'] = str(a.fp_block)
    if a.protocol_v6:
        if a.thr_budget not in (0.,1.): raise ValueError('V6 uses 1 FP/1440 ticks/intent')
        for m in NEURAL+['classic']:
            base[m].update(protocol_v6=True, threshold_mode=a.threshold_mode, features='onset',
                           fp_budget=1., thr_budget_fixed=True, val_frac=a.val_frac,
                           distill_alpha=1., temp=2.)
    h_note = ''
    if lt_ref >= 2.0 * base['mild']['horizon']:
        # v2.4: the paper's horizon (10 ticks for O-RAN v2) cannot see a precursor that starts ~37 ticks before the
        # failure (scenario v4). Start every neural method from 0.8 x the median build-up instead (the search still
        # tries the build-up quantiles); the classic baselines keep their label horizon grid.
        hb = int(round(0.8 * lt_ref))
        for m in ('mild', 'mlp', 'lstm'):
            base[m]['horizon'] = hb
        h_note = (f'* Starting horizon of MILD, MLP and LSTM set to 0.8 x the median build-up = {hb} ticks (the '
                  f'paper horizon is less than half the median build-up of {lt_ref:.0f} ticks).')
        log(h_note[2:])
    aud['h_note'] = h_note
    Hc = h_candidates(adf, base['mild']['horizon'])
    rng = np.random.default_rng(a.search_seed)

    # ------------------------------ 1. search ------------------------------
    leader, rerank_rows = [], []
    best = {}
    for method in ['mild', 'mlp', 'lstm', 'classic']:
        n = a.trials if method != 'classic' or a.protocol_v6 else max(8, 2 * (len(Hc) + 1))
        cfgs = sample_cfgs(method, base[method], rng, n, Hc)
        if a.thr_rule != 'lead':                       # tuned candidates only; base (= paper variant) is unchanged
            cfgs = [dict(c, thr_rule=a.thr_rule) for c in cfgs]
        log(f'Search {method}: {len(cfgs)} trials on development data')
        for i, c in enumerate(cfgs):
            cc = dict(c, test_range=[inner, dev_end], row_limit=dev_end, seed=42)
            res, d = run_job(a.out, data_csv, events_json, method, cc, f'search#{i}')
            if res is None:
                continue
            subs = list(CLASSIC_NAMES) if method == 'classic' else [None]
            for sub in subs:
                m = macro_of(res, method, sub)
                sc = objective(m, a.fp_budget, lt_ref)
                name = sub or method
                leader.append({'method': DISPLAY[name], 'trial': i, 'score': sc, **{k: v for k, v in m.items()
                               if k != 'per_intent_detection'}, 'cfg': json.dumps(c, sort_keys=True)})
                if name not in best or sc > best[name]['score']:
                    best[name] = {'score': sc, 'cfg': c, 'parent': method}
        if a.rerank_top > 1 and method != 'classic':
            # Winner's-curse guard: one seed decides little when seed noise is large. The K best
            # trials are re-run with extra seeds on the same inner validation period and the
            # configuration with the best MEAN score is kept. Same rule for every neural method.
            mine = sorted([r for r in leader if r['method'] == DISPLAY[method]], key=lambda r: -r['score'])
            seen_cfg, cands = set(), []
            for r in mine:
                if r['cfg'] not in seen_cfg:
                    seen_cfg.add(r['cfg'])
                    cands.append(r)
                if len(cands) >= a.rerank_top:
                    break
            rr_best = None
            for ci, r in enumerate(cands):
                c = json.loads(r['cfg'])
                scs = [r['score']]
                for sd in [int(x) for x in a.rerank_seeds.split(',') if x]:
                    cc = dict(c, test_range=[inner, dev_end], row_limit=dev_end, seed=sd)
                    res, d = run_job(a.out, data_csv, events_json, method, cc, f'rerank#{ci}:seed{sd}')
                    if res is not None:
                        scs.append(objective(macro_of(res, method), a.fp_budget, lt_ref))
                rerank_rows.append({'method': DISPLAY[method], 'candidate': ci, 'seed42_score': r['score'],
                                    'mean_score': float(np.mean(scs)), 'std_score': float(np.std(scs)),
                                    'n_seeds': len(scs), 'cfg': r['cfg']})
                if rr_best is None or np.mean(scs) > rr_best[0]:
                    rr_best = (float(np.mean(scs)), c)
            if rr_best is not None:
                best[method] = {'score': rr_best[0], 'cfg': rr_best[1], 'parent': method, 'reranked': True}
            pd.DataFrame(rerank_rows).to_csv(os.path.join(a.out, 'search_rerank.csv'), index=False)
        pd.DataFrame(leader).sort_values(['method', 'score'], ascending=[True, False]).to_csv(
            os.path.join(a.out, 'search_leaderboard.csv'), index=False)

    # ------------------------------ 2. held-out test ------------------------------
    seeds = [int(s) for s in a.seeds.split(',')]
    final_rows, final_dirs, cur_final = [], {}, []
    for name, b in best.items():
        parent = b['parent']
        for variant, cfg0 in [x for x in [('tuned', b['cfg']), ('paper', base[parent])] if x[0] in a.variants.split(',')]:
            for sd in seeds:
                cc = dict(cfg0, test_range=[dev_end, N], seed=sd)
                res, d = run_job(a.out, data_csv, events_json, parent, cc, f'final:{name}:{variant}:seed{sd}')
                if res is None:
                    continue
                m = macro_of(res, parent, name if parent == 'classic' else None)
                final_rows.append({'method': DISPLAY[name], 'variant': variant, 'seed': sd, **{k: v for k, v in m.items()
                                   if k != 'per_intent_detection'},
                                   **{f'det_{k}': v for k, v in m['per_intent_detection'].items()},
                                   'cfg': json.dumps(cfg0, sort_keys=True)})
                final_dirs[(name, variant, sd)] = events_dir(d, parent, name if parent == 'classic' else None)
                cur_final.append({'name': name, 'variant': variant, 'seed': sd, 'method': parent, 'dir': d})
    # v2.2.1: the final jobs of THIS run (this dataset). The jobs folder also holds cached jobs of earlier datasets;
    # opcurve.py and train_now.py read only the jobs listed here.
    with open(os.path.join(a.out, 'final_jobs.json'), 'w') as f:
        json.dump({'data_fingerprint': data_fingerprint(data_csv, events_json), 'dev_rows': dev_end, 'rows': N,
                   'jobs': cur_final}, f, indent=1)
    fr = pd.DataFrame(final_rows)
    fr.to_csv(os.path.join(a.out, 'final_test_runs.csv'), index=False)
    cols = ['detection', 'lead', 'fp_per_day', 'disamb', 'fp_episodes_per_day', 'disamb_all_roots']
    summ = fr.groupby(['method', 'variant'])[cols].agg(['mean', 'std']).round(2)
    summ.to_csv(os.path.join(a.out, 'final_test_summary.csv'))

    # ------------------------------ 3. significance ------------------------------
    # Paired tests for EVERY seed (MILD seed s vs baseline seed s), Holm within seed and metric,
    # then a consensus verdict over seeds (see stats.consensus).
    sig_rows = []
    for sd in seeds:
        if ('mild', 'tuned', sd) not in final_dirs:
            continue
        evm, dym = load_events(final_dirs[('mild', 'tuned', sd)])
        for name in ['mlp', 'lstm', 'wkpi', 'dist', 'lr']:
            if (name, 'tuned', sd) not in final_dirs:
                continue
            evb, dyb = load_events(final_dirs[(name, 'tuned', sd)])
            for r in S.compare(evm, evb, dym, dyb):
                r['baseline_name'] = DISPLAY[name]
                r['seed'] = sd
                sig_rows.append(r)
    sig = pd.DataFrame(sig_rows)
    if not sig.empty:
        by_seed = S.holm_and_verdict(sig, ('seed', 'metric'))
        by_seed.to_csv(os.path.join(a.out, 'significance_by_seed.csv'), index=False)
        sig = S.consensus(by_seed)
        sig.to_csv(os.path.join(a.out, 'significance_test_set.csv'), index=False)

    if a.opcurve_targets:
        try:
            import opcurve
            opcurve.run(a.out, events_json, a.opcurve_targets, a.fp_budget, fp_scope=a.fp_scope)
            log(f'equal-FP comparison written to {os.path.join(a.out, "opcurve")}')
        except Exception as exc:                     # the analysis is optional; never lose the main results
            log(f'equal-FP comparison failed: {exc}')

    write_report(a, aud, fr, summ, sig, best)
    log(f'Done. Read {os.path.join(a.out, "AUTOTRAIN_REPORT.md")}')


def write_report(a, aud, fr, summ, sig, best):
    L = ['# Automatic training and comparison report', '',
         f'Dataset: `{a.data_csv}`', '', '## 1. Dataset audit', '', '```', json.dumps(aud, indent=2, default=str), '```', '',
         '## 2. Protocol', '',
         f'* Chronological split. Development set: first {aud["dev_rows"]} rows. Held-out test set: last {aud["test_rows"]} rows. '
         'The cut points are placed where no event is active.',
         f'* Every method was tuned with the same trial budget ({a.trials}) and the same objective on the development set only. '
         'The objective is: DA + 0.5·Det + 25·min(LT/LT_ref, 1.5) − 4·FP/day − 20·max(0, FP/day − budget).',
         f'* All methods tune their alert thresholds under the same FP budget of {a.fp_budget}/day.',
         ('* MILD architecture: v2.3 (residual gate expert x (1 + g), onset features d1/d3/z60 in addition to the '
          'rolling statistics; distillation weight searched in 0.5-1.0, start 1.0 = teacher used by the gate only).'
          if a.mild_arch == 'v23' else
          ('* MILD architecture: published gate (expert x g) with the onset features d1/d3/z60 in addition to the '
           'rolling statistics (v2.6).' if a.mild_arch == 'v26' else '* MILD architecture: as published (paper).')),
         aud.get('h_note', ''),
         ('* Baseline inputs: the ten base features (paper protocol).' if a.baseline_features == 'base' else
          '* Baseline inputs: the MLP and the LSTM use exactly the same input features as MILD '
          '(base KPIs, rolling statistics and change features).'),
         ('* False alarm (calibration, evaluation, equal-FP report): an alert of intent k outside the event windows of '
          'intent k itself (fp_scope=own, v2.7: an alarm raised for the wrong tenant is false).' if a.fp_scope == 'own'
          else '* False alarm: an alert outside the event windows of every intent (fp_scope=any, paper).'),
         (f'* Threshold rule of the tuned variant (every method): {a.thr_rule}'
          + (' (maximise detection rate x mean lead; a missed event counts 0). The paper variant keeps the paper rule '
             '(maximise the mean lead over detected events only).' if a.thr_rule == 'expected_lead' else ' (paper rule).')),
         f'* The {a.rerank_top} best trials of each neural method were re-run with seeds {a.rerank_seeds} on the same '
         'development validation period; the configuration with the best mean score was kept (search_rerank.csv).'
         if a.rerank_top > 1 else '* No re-ranking.',
         '* The held-out test set was used once, for the final evaluation, with seeds ' + a.seeds + '.', '',
         '## 3. Held-out test results (mean and std over seeds)', '', summ.to_string(), '']
    tuned = fr[fr['variant'] == 'tuned'].groupby('method')[['detection', 'lead', 'fp_per_day', 'disamb']].mean()
    verdict_lines, problems = [], []
    if 'MILD' in tuned.index and len(tuned) > 1:
        mild = tuned.loc['MILD']
        others = tuned.drop(index='MILD')
        checks = [('fp_per_day', False, 'FP/day'), ('disamb', True, 'Disambiguation accuracy'),
                  ('lead', True, 'Lead time'), ('detection', True, 'Detection rate')]
        L += ['## 4. Verdict (priority order: FP/day, disambiguation, lead time, detection)', '']
        for col, hib, lab in checks:
            bb = others[col].max() if hib else others[col].min()
            who = others[col].idxmax() if hib else others[col].idxmin()
            win = mild[col] >= bb if hib else mild[col] <= bb
            sigtxt = ''
            if not sig.empty:
                metric_map = {'fp_per_day': 'fp_steps_per_day', 'disamb': 'disambiguation_%',
                              'lead': 'lead_time_min (common detections)', 'detection': 'detection_%'}
                g = sig[(sig['metric'] == metric_map[col]) & (sig['baseline_name'] == who)]
                if not g.empty:
                    pv = g['p_holm'].iloc[0]
                    sigtxt = f" (paired test vs {who}: {g['verdict'].iloc[0]}" + (f", Holm p={pv:.3g})" if pv == pv else ")")
            if col == 'lead':
                sigtxt += ' [means are over each model\'s own detections; the paired test uses events detected by both]'
            L.append(f'- **{lab}**: MILD {mild[col]:.2f} vs best baseline {who} {bb:.2f} → '
                     f'{"MILD better or equal" if win else "MILD WORSE"}{sigtxt}')
            if not win:
                problems.append((col, who, mild[col], bb))
        L.append('')
        if not problems:
            L += ['**MILD beats the best baseline on all four metrics on the held-out test set.**', '']
        else:
            L += ['**MILD does not beat the best baseline on every metric.** Below are the likely reasons and fixes, '
                  'based on the dataset audit and the per-intent results:', '']
            obs = aud.get('observed_fraction_by_intent', {})
            for col, who, mv, bv in problems:
                if col == 'fp_per_day':
                    L.append('- FP/day is higher. Check whether the validation FP budget was met but not the test one. '
                             'That points to a shift between the development and test periods, so collect more '
                             'segments or use more seeds. A lower `--fp_budget` or a longer EWMA span grid can help.')
                if col == 'disamb':
                    L.append('- Disambiguation is lower. The gate learns root causes only from `*_cause`, `independent*` '
                             'and `nonlinear*` labels. Check that co-drift episodes are frequent enough, and look at '
                             '`audit.json` → events_by_type. A larger `gate_supervise` or more co-drift episodes '
                             '(`--p-codrift`) is the usual fix.')
                if col == 'lead':
                    L.append('- Lead time is shorter. If most build-ups are short (see buildup_quantiles), the horizon '
                             'grid is capped by the data. Increase the event durations or the ramp power in the collector.')
                if col == 'detection':
                    low = {k: v for k, v in obs.items() if v < 0.8}
                    L.append('- Detection is lower. Intents with few observed failures: ' + (str(low) if low else 'none') +
                             '. Re-run the collector calibration for those intents, or collect more segments.')
            L.append('')
    L += ['## 5. Paired significance tests on the held-out test set (consensus over seeds ' + a.seeds + ')', '',
          'Each seed is tested separately (MILD seed s vs baseline seed s, Holm-corrected). A verdict needs at least two '
          'thirds of the seeds and no seed with a significant opposite result. Means are over seeds; p_holm is the median. '
          'Per-seed results: significance_by_seed.csv.', '',
          sig[['baseline_name', 'metric', 'n_units', 'mild', 'baseline', 'diff', 'ci_low', 'ci_high', 'p_holm',
               'seeds_mild_better', 'seeds_baseline_better', 'verdict']].round(4).to_string(index=False)
          if not sig.empty else '(not available)', '',
          '## 6. Selected configurations', '']
    for k, b in best.items():
        L.append(f'- {DISPLAY[k]}: `{json.dumps(b["cfg"], sort_keys=True)}` (search score {b["score"]:.2f})')
    with open(os.path.join(a.out, 'AUTOTRAIN_REPORT.md'), 'w') as f:
        f.write('\n'.join(L))


if __name__ == '__main__':
    main()
