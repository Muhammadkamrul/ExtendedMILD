#!/usr/bin/env python3
"""diagnostics_extra.py -- supplementary analyses on top of diagnostics_confirm.py. NO retraining.

  python diagnostics_extra.py --run ~/intact/mild/compare_mlp_frozen_31k
      (needs diagnostics_confirm.py next to it; reads the same saved test predictions)

Output: <run>/diagnostics/extra/  (SUPPLEMENT_EXTRA.md, CSVs, figures)

  A. common seeds      every table uses only the seeds that ALL neural methods finished (MILD, MLP, LSTM, ...),
                       so means are paired. Seeds dropped are listed.
  B. allocation test   each baseline is re-thresholded per intent so that it spends EXACTLY MILD's per-intent false
                       alarms of the same seed (and MILD is re-thresholded to each baseline's allocation). Answers:
                       is MILD's detection gain due to WHERE it spends its alarms (calibration/allocation) or to
                       better scores? Post hoc (thresholds placed on the test part).
  C. per-intent matched FP  detection of each intent at the same per-intent FP rate (0.5..6 steps / 1440 ticks),
                       paired over seeds (Wilcoxon) -- e.g. is MILD better on telemetry at equal telemetry alarms?
  D. paired bootstrap 95 % CIs of MILD minus baseline (resampling seeds) for TP, FP episodes, F1, alarm precision,
                       FP steps/day, and the marginal precision of MILD's extra alarms dTP / (dTP + dFP episodes).
  E. curve data        CSVs of the trade-off curves (mean / min / max over seeds) to redraw figures in paper style.
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import diagnostics_confirm as DC   # noqa: E402

try:
    from scipy.stats import wilcoxon
except Exception:
    wilcoxon = None

CLASSIC = ('LR-OvR', 'WKPI-Tuned', 'Dist-Target')
LEVELS = [0.5, 1, 2, 3, 4, 6]


def intent_curves(d):
    """Per intent: exact (FP steps/day, detection %) for every threshold, plus the main-run operating point."""
    f = os.path.join(d, 'folds')
    ts = pd.read_csv(os.path.join(f, 'fold1_timeseries.csv'))
    met = json.load(open(os.path.join(f, 'fold1_metrics.json')))
    ev = pd.read_csv(os.path.join(f, 'fold1_events.csv'))
    T = ts['t'].to_numpy().astype(np.int64)
    days = float(met.get('overall', {}).get('test_days') or (T[-1] - T[0]) / DC.TICKS_PER_DAY)
    out = {}
    ks = [c[4:] for c in ts.columns if c.startswith('ema_')]
    anyw = DC.window_mask(T, ev[['start', 'end']].to_numpy())
    ownw = {k: DC.window_mask(T, ev.loc[ev.intent == k, ['start', 'end']].to_numpy()) for k in ks}
    al = {k: ts['ema_' + k].to_numpy() >= float(met['params'][k]['tau']) for k in ks}
    saved = met.get('overall', {}).get('fp_steps_total')
    f_own = sum(int((al[k] & ~ownw[k]).sum()) for k in ks)
    f_any = sum(int((al[k] & ~anyw).sum()) for k in ks)
    scope_any = saved is not None and f_any == saved and f_own != saved     # same rule as diagnostics_confirm.py
    for k in ks:
        ema = ts['ema_' + k].to_numpy()
        own = anyw if scope_any else ownw[k]
        negs = np.sort(ema[~own])
        e_k = ev[ev.intent == k]
        mx = np.array([ema[np.searchsorted(T, s, 'left'):np.searchsorted(T, fl, 'right')].max(initial=-np.inf)
                       for s, fl in e_k[['start', 'failure_time']].to_numpy()])
        cand = np.unique(np.r_[mx[np.isfinite(mx)], negs, np.inf])
        fp = (len(negs) - np.searchsorted(negs, cand, 'left')) / days
        det = np.array([100 * np.mean(mx >= c) if len(mx) else 0.0 for c in cand])
        tau = float(met['params'][k]['tau'])
        out[k] = {'fp': fp, 'det': det, 'n_events': len(mx),
                  'op_fp': (len(negs) - np.searchsorted(negs, tau, 'left')) / days,
                  'op_det': 100 * float(np.mean(mx >= tau)) if len(mx) else 0.0}
    return out


def det_at(c, f):
    """Highest detection reachable with FP <= f (exact step curve)."""
    return float(DC.best_det_at(c['fp'], c['det'], np.array([f + 1e-9]))[0])


def wil(d):
    d = np.asarray(d, float)
    if wilcoxon is None or len(d) < 5 or np.allclose(d, 0):
        return np.nan
    try:
        return float(wilcoxon(d, zero_method='pratt').pvalue)
    except Exception:
        return np.nan


def boot_ci(x, n=10000, seed=0):
    x = np.asarray(x, float)
    rng = np.random.default_rng(seed)
    m = rng.choice(x, size=(n, len(x)), replace=True).mean(1)
    return float(x.mean()), float(np.quantile(m, 0.025)), float(np.quantile(m, 0.975))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--run', action='append', required=True)
    ap.add_argument('--diag', default='', help='diagnostics_confirm.py output folder (default <first run>/diagnostics)')
    ap.add_argument('--out', default='')
    a = ap.parse_args()
    diag = os.path.abspath(os.path.expanduser(a.diag or os.path.join(a.run[0], 'diagnostics')))
    out = os.path.abspath(os.path.expanduser(a.out or os.path.join(diag, 'extra')))
    os.makedirs(out, exist_ok=True)
    jobs = []
    for r in a.run:
        jobs += DC.discover(r)
    neural = DC.morder({m for m, _, _ in jobs if not m.replace(' (tuned)', '') in CLASSIC})
    if 'MILD' not in neural:
        sys.exit('no MILD jobs found')
    seeds_by = {m: {s for mm, s, _ in jobs if mm == m} for m in neural}
    common = sorted(set.intersection(*seeds_by.values()))
    dropped = {m: sorted(seeds_by[m] - set(common)) for m in neural if seeds_by[m] - set(common)}
    print('common seeds:', len(common), common, '| dropped:', dropped, flush=True)
    jobs = [j for j in jobs if j[0] not in neural or j[1] in common]
    methods = DC.morder({j[0] for j in jobs})
    C = {}
    for m, s, d in jobs:
        C[(m, s)] = intent_curves(d)
        print('  curves', m, s, flush=True)
    intents = list(C[('MILD', common[0])].keys())

    # ---------------- B. allocation test
    al = []
    for b in methods:
        if b == 'MILD':
            continue
        for s in common:
            cm, cb = C[('MILD', s)], C[(b, s) if (b, s) in C else next(k for k in C if k[0] == b)]
            row = {'baseline': b, 'seed': s}
            row['MILD_main'] = np.mean([cm[k]['op_det'] for k in intents])
            row['baseline_main'] = np.mean([cb[k]['op_det'] for k in intents])
            row['baseline_at_MILD_allocation'] = np.mean([det_at(cb[k], cm[k]['op_fp']) for k in intents])
            row['MILD_at_baseline_allocation'] = np.mean([det_at(cm[k], cb[k]['op_fp']) for k in intents])
            row['MILD_fp'] = sum(cm[k]['op_fp'] for k in intents)
            row['baseline_fp'] = sum(cb[k]['op_fp'] for k in intents)
            for k in intents:
                row[f'{k}:baseline_at_MILD_alloc'] = det_at(cb[k], cm[k]['op_fp'])
                row[f'{k}:MILD_main'] = cm[k]['op_det']
            al.append(row)
    AL = pd.DataFrame(al)
    AL.to_csv(os.path.join(out, 'allocation_test_runs.csv'), index=False)
    alt = []
    for b, g in AL.groupby('baseline', sort=False):
        alt.append({'baseline': b, 'seeds': len(g),
                    'MILD (main thresholds)': g.MILD_main.mean(), 'baseline (main thresholds)': g.baseline_main.mean(),
                    'baseline at MILD allocation': g.baseline_at_MILD_allocation.mean(),
                    'MILD minus baseline at MILD allocation': (g.MILD_main - g.baseline_at_MILD_allocation).mean(),
                    'seeds MILD higher': int((g.MILD_main > g.baseline_at_MILD_allocation + 1e-9).sum()),
                    'seeds baseline higher': int((g.MILD_main < g.baseline_at_MILD_allocation - 1e-9).sum()),
                    'wilcoxon_p': wil(g.MILD_main - g.baseline_at_MILD_allocation),
                    'MILD at baseline allocation': g.MILD_at_baseline_allocation.mean(),
                    'MILD minus baseline at baseline allocation': (g.MILD_at_baseline_allocation - g.baseline_main).mean(),
                    'wilcoxon_p (baseline allocation)': wil(g.MILD_at_baseline_allocation - g.baseline_main)})
    ALT = pd.DataFrame(alt).round(3)
    ALT.to_csv(os.path.join(out, 'allocation_test_summary.csv'), index=False)
    ALI = AL.groupby('baseline', sort=False)[[c for c in AL.columns if ':' in c]].mean().round(1)

    # ---------------- C. per-intent matched FP
    mf = []
    for (m, s), c in C.items():
        for k in intents:
            for f in LEVELS:
                mf.append({'method': m, 'seed': s, 'intent': k, 'fp_level': f, 'detection': det_at(c[k], f)})
    MF = pd.DataFrame(mf)
    MF.to_csv(os.path.join(out, 'matched_fp_per_intent_runs.csv'), index=False)
    MFS = MF.groupby(['intent', 'fp_level', 'method']).detection.mean().unstack('method').reindex(columns=methods).round(1)
    pp = []
    for b in methods:
        if b == 'MILD':
            continue
        for k in intents:
            for f in LEVELS:
                x = MF[(MF.method == 'MILD') & (MF.intent == k) & (MF.fp_level == f)].set_index('seed').detection
                y = MF[(MF.method == b) & (MF.intent == k) & (MF.fp_level == f)].set_index('seed').detection
                y = y.reindex(x.index) if len(y) > 1 else pd.Series(y.iloc[0], index=x.index)
                dd = (x - y).dropna()
                pp.append({'baseline': b, 'intent': k, 'fp_level': f, 'MILD': x.mean(), 'baseline_value': y.mean(),
                           'diff': dd.mean(), 'seeds_MILD_higher': int((dd > 0).sum()),
                           'seeds_baseline_higher': int((dd < 0).sum()), 'wilcoxon_p': wil(dd)})
    MFP = pd.DataFrame(pp).round(3)
    MFP.to_csv(os.path.join(out, 'matched_fp_per_intent_paired.csv'), index=False)

    # ---------------- D. paired bootstrap CIs from the diagnostics_confirm.py run tables
    bs = []
    cr, tr = os.path.join(diag, 'confusion_runs.csv'), os.path.join(diag, 'trust_runs.csv')
    if os.path.exists(cr) and os.path.exists(tr):
        R = pd.read_csv(cr).merge(pd.read_csv(tr)[['method', 'seed', 'false_episodes_per_day', 'median_lead',
                                                    'actionable_%(lead>=10)', 'wrong_cause_when_alarmed_%_root']],
                                  on=['method', 'seed'])
        R = R[~R.method.isin(neural) | R.seed.isin(common)]
        R.to_csv(os.path.join(out, 'run_table_common_seeds.csv'), index=False)
        cols = ['event_TP', 'event_FP_episodes', 'event_F1_%', 'event_precision_%', 'alarm_precision_%',
                'fp_steps_per_day', 'false_episodes_per_day', 'median_lead', 'actionable_%(lead>=10)']
        M = R[R.method == 'MILD'].set_index('seed')
        for b in [m for m in methods if m != 'MILD']:
            B = R[R.method == b].set_index('seed')
            B = B.reindex(M.index) if len(B) > 1 else pd.DataFrame([B.iloc[0]] * len(M), index=M.index)
            for c in cols:
                mu, lo, hi = boot_ci((M[c] - B[c]).astype(float))
                bs.append({'baseline': b, 'metric': c, 'MILD': M[c].mean(), 'baseline_value': B[c].astype(float).mean(),
                           'diff': mu, 'ci95_low': lo, 'ci95_high': hi})
            dtp = (M.event_TP - B.event_TP.astype(float)).to_numpy()
            dfp = (M.event_FP_episodes - B.event_FP_episodes.astype(float)).to_numpy()
            rng = np.random.default_rng(1)
            idx = rng.integers(0, len(dtp), size=(10000, len(dtp)))
            num, den = dtp[idx].mean(1), (dtp[idx].mean(1) + dfp[idx].mean(1))
            mp = np.where(den > 0, 100 * num / np.where(den > 0, den, 1), np.nan)
            point = 100 * dtp.mean() / (dtp.mean() + dfp.mean()) if dtp.mean() + dfp.mean() > 0 else np.nan
            bs.append({'baseline': b, 'metric': 'marginal_precision_% = dTP/(dTP+dFP_episodes)', 'MILD': np.nan,
                       'baseline_value': np.nan, 'diff': point, 'ci95_low': float(np.nanquantile(mp, 0.025)),
                       'ci95_high': float(np.nanquantile(mp, 0.975))})
            bs.append({'baseline': b, 'metric': 'extra detections per extra false episode = dTP/dFP', 'MILD': np.nan,
                       'baseline_value': np.nan, 'diff': dtp.mean() / dfp.mean() if dfp.mean() > 0 else np.nan,
                       'ci95_low': np.nan, 'ci95_high': np.nan})
    BS = pd.DataFrame(bs).round(3)
    BS.to_csv(os.path.join(out, 'bootstrap_differences.csv'), index=False)

    # ---------------- E. curve data
    grid = np.round(np.arange(0, 8.0001, 0.05), 3)
    cd = []
    for (m, s), c in C.items():
        for k in intents:
            dd = DC.best_det_at(c[k]['fp'], c[k]['det'], grid + 1e-9)
            cd += [{'method': m, 'seed': s, 'intent': k, 'fp_per_1440': g, 'detection': v} for g, v in zip(grid, dd)]
    CD = pd.DataFrame(cd)
    CD.groupby(['intent', 'method', 'fp_per_1440']).detection.agg(['mean', 'min', 'max']).round(2).to_csv(
        os.path.join(out, 'curve_per_intent.csv'))
    op = pd.DataFrame([{'method': m, 'seed': s, 'intent': k, 'fp_per_1440': c[k]['op_fp'], 'detection': c[k]['op_det']}
                       for (m, s), c in C.items() for k in intents])
    op.to_csv(os.path.join(out, 'operating_points_per_intent.csv'), index=False)

    figs = plots(out, AL, ALT, MF, methods, intents, op)
    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', 40)
    L = ['# Supplementary analyses (no retraining)', '',
         f'Common seeds used for every neural method ({len(common)}): {common}. Dropped (not finished by every neural '
         f'method): {dropped if dropped else "none"}. Classic baselines are deterministic (one run, compared with every seed).', '',
         '## B. Allocation test: each method at MILD\'s per-intent false-alarm allocation', '',
         'For every seed, each baseline is re-thresholded per intent so that it raises at most as many false-alarm steps '
         'on each intent as MILD does on that intent in the same seed. If MILD still detects more, the gain comes from '
         'its scores; if the baseline catches up, the gain comes from where MILD spends its alarms (its calibration). '
         'The reverse (MILD at the baseline\'s allocation) is also given. Post hoc: thresholds placed on the test part.', '',
         ALT.to_string(index=False), '', '### Per intent (mean over seeds)', '', ALI.T.to_string(), '',
         '## C. Per-intent detection at matched per-intent false alarms (steps / 1440 ticks)', '', MFS.to_string(), '',
         '### MILD vs each method (paired over seeds)', '',
         MFP[~MFP.baseline.isin(CLASSIC)].to_string(index=False), '',
         '## D. MILD minus each method, paired seed-bootstrap 95 % CI (10,000 resamples)', '',
         'marginal_precision = of MILD\'s additional alarm outcomes (extra detections + extra false episodes), the share '
         'that are detections.', '',
         BS.to_string(index=False) if not BS.empty else '(run diagnostics_confirm.py first)', '',
         '## E. Curve data for redrawing', '', '- curve_per_intent.csv (mean/min/max over seeds), operating_points_per_intent.csv', '',
         '## Figures', ''] + [f'- {f}' for f in figs]
    open(os.path.join(out, 'SUPPLEMENT_EXTRA.md'), 'w').write('\n'.join(L) + '\n')
    print('\n'.join(L), flush=True)
    print('\nOutput folder:', out)


def plots(out, AL, ALT, MF, methods, intents, op):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except Exception:
        return []
    plt.rcParams.update({'font.size': 9, 'axes.grid': True, 'grid.alpha': 0.3, 'savefig.dpi': 200})
    figs = []
    bl = [b for b in ALT.baseline if b not in CLASSIC]
    if bl:
        fig, ax = plt.subplots(figsize=(1.9 * len(bl) + 2, 3.6))
        for i, b in enumerate(bl):
            g = AL[AL.baseline == b]
            vals = [g.baseline_main, g.baseline_at_MILD_allocation, g.MILD_main]
            cols = [DC.color(b), DC.color(b), DC.color('MILD')]
            for j, (v, c) in enumerate(zip(vals, cols)):
                ax.bar(i * 4 + j, v.mean(), 0.8, color=c, alpha=0.45 if j == 1 else 0.9,
                       hatch='//' if j == 1 else None, edgecolor='k', lw=0.4)
                ax.errorbar(i * 4 + j, v.mean(), yerr=v.std(ddof=0), color='k', capsize=3, lw=0.8)
                ax.text(i * 4 + j, v.mean() + v.std(ddof=0) + 1, f'{v.mean():.1f}', ha='center', fontsize=7)
        ax.set_xticks([i * 4 + j for i in range(len(bl)) for j in range(3)])
        ax.set_xticklabels([t for b in bl for t in (f'{b}\nown', f'{b}\nMILD alloc.', 'MILD')], fontsize=7)
        ax.set(ylabel='macro detection (%)', ylim=(0, 110),
               title='Own thresholds vs MILD\'s per-intent false-alarm allocation')
        fig.tight_layout()
        fig.savefig(os.path.join(out, 'figS_allocation.png'))
        plt.close(fig)
        figs.append('figS_allocation.png')
    fig, axs = plt.subplots(1, len(intents), figsize=(4 * len(intents), 3.5), squeeze=False)
    for i, k in enumerate(intents):
        ax = axs[0, i]
        for m in methods:
            g = MF[(MF.method == m) & (MF.intent == k)].groupby('fp_level').detection
            mu, sd = g.mean(), g.std(ddof=0).fillna(0)
            ax.errorbar(mu.index, mu.values, yerr=sd.values, color=DC.color(m), marker='o', ms=3, capsize=2,
                        lw=1.6 if m == 'MILD' else 1, label=m if i == 0 else None)
        ax.set(title=f'intent: {k}', xlabel='FP steps / 1440 ticks (this intent)', ylim=(0, 102))
        if i == 0:
            ax.set_ylabel('detection (%) at matched FP')
    axs[0, 0].legend(fontsize=7)
    fig.suptitle('Per-intent detection at matched per-intent false alarms (mean +/- sd over seeds; post hoc)')
    fig.tight_layout()
    fig.savefig(os.path.join(out, 'figS_matched_fp_per_intent.png'))
    plt.close(fig)
    figs.append('figS_matched_fp_per_intent.png')
    # allocation picture: where each method spends its false alarms
    g = op[~op.method.isin(CLASSIC)].groupby(['method', 'intent']).fp_per_1440.mean().unstack('intent').reindex(
        [m for m in methods if m not in CLASSIC])
    fig, ax = plt.subplots(figsize=(5.5, 3.3))
    left = np.zeros(len(g))
    shades = ['#4d4d4d', '#9e9e9e', '#d9d9d9', '#f0f0f0']
    for j, k in enumerate(intents):
        ax.barh(range(len(g)), g[k], left=left, color=shades[j % 4], edgecolor='k', lw=0.4, label=k)
        for r, v in enumerate(g[k]):
            if v > 0.4:
                ax.text(left[r] + v / 2, r, f'{v:.1f}', ha='center', va='center', fontsize=7,
                        color='w' if j == 0 else 'k')
        left += g[k].to_numpy()
    ax.set_yticks(range(len(g)))
    ax.set_yticklabels(g.index)
    ax.invert_yaxis()
    ax.set(xlabel='false-alarm steps / 1440 ticks (main thresholds, mean over seeds)',
           title='Where each method spends its false alarms')
    ax.legend(fontsize=7, loc='lower right')
    fig.tight_layout()
    fig.savefig(os.path.join(out, 'figS_fp_allocation.png'))
    plt.close(fig)
    figs.append('figS_fp_allocation.png')
    return figs


if __name__ == '__main__':
    main()
