"""
stats.py -- paired statistical tests between MILD and every baseline.

All models are evaluated on exactly the same test events and the same test
days, so every comparison is paired. The unit of analysis is the event (or the
day for false alarms), pooled over all evaluated test blocks. Fold-level tests
are also reported when at least 5 folds were evaluated (only the statistical
benchmark), because 2 folds cannot support a fold-level test.

  metric          unit                         test
  -------------   --------------------------   -------------------------------------
  detection       each labelled event          exact McNemar (binomial on discordant pairs)
  disambiguation  each paper root event        exact McNemar
  lead time       events detected by both      Wilcoxon signed-rank
  FP per day      each test day (1440 steps)   Wilcoxon signed-rank on daily FP counts

p-values are Holm-corrected over the five baselines within each dataset and
metric. 95% CIs of the paired difference come from 2000 bootstrap resamples.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats as st

RNG = np.random.default_rng(12345)


def mcnemar_exact(a, b):
    a, b = np.asarray(a, int), np.asarray(b, int)
    n01 = int(np.sum((a == 0) & (b == 1)))
    n10 = int(np.sum((a == 1) & (b == 0)))
    n = n01 + n10
    if n == 0:
        return 1.0, n10, n01
    return float(st.binomtest(n10, n, 0.5).pvalue), n10, n01


def wilcoxon_safe(x, y):
    d = np.asarray(x, float) - np.asarray(y, float)
    d = d[~np.isnan(d)]
    if len(d) < 6 or np.all(d == 0):
        return np.nan
    try:
        return float(st.wilcoxon(d, zero_method='pratt').pvalue)
    except Exception:
        return np.nan


def boot_ci(diff_fn, n, B=2000):
    if n == 0:
        return (np.nan, np.nan)
    vals = []
    for _ in range(B):
        idx = RNG.integers(0, n, n)
        v = diff_fn(idx)
        if not np.isnan(v):
            vals.append(v)
    if not vals:
        return (np.nan, np.nan)
    return (float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5)))


def holm(pvals):
    p = np.asarray(pvals, float)
    out = np.full_like(p, np.nan)
    ok = ~np.isnan(p)
    idx = np.argsort(p[ok])
    m = ok.sum()
    adj = np.empty(m)
    run = 0.0
    pv = p[ok][idx]
    for r, val in enumerate(pv):
        run = max(run, (m - r) * val)
        adj[r] = min(1.0, run)
    tmp = np.empty(m)
    tmp[idx] = adj
    out[ok] = tmp
    return out


def _key(df):
    return df['fold'].astype(str) + '|' + df['intent'] + '|' + df['start'].astype(str) + '|' + df['type']


def _fp_detail(dd):
    w = 1440.0
    for c in ('bin_ticks_m', 'bin_ticks'):
        if c in dd and len(dd):
            w = float(dd[c].iloc[0])
            break
    unit = 'per-day counts' if w == 1440.0 else f'per-{int(w)}-tick block counts scaled to per day'
    return unit + '; partial last bin included'


def compare(ev_m, ev_b, day_m, day_b, fold_m=None, fold_b=None):
    """Return a list of dicts (one per metric) comparing MILD (m) with a baseline (b)."""
    em, eb = ev_m.copy(), ev_b.copy()
    em['k'], eb['k'] = _key(em), _key(eb)
    j = em.merge(eb, on='k', suffixes=('_m', '_b'))
    rows = []

    # detection
    p, n10, n01 = mcnemar_exact(j['detected_m'], j['detected_b'])
    dm, db = j['detected_m'].values, j['detected_b'].values
    ci = boot_ci(lambda i: 100 * (dm[i].mean() - db[i].mean()), len(j))
    rows.append({'metric': 'detection_%', 'n_units': len(j), 'mild': 100 * dm.mean(), 'baseline': 100 * db.mean(),
                 'diff': 100 * (dm.mean() - db.mean()), 'ci_low': ci[0], 'ci_high': ci[1], 'test': 'McNemar exact',
                 'p': p, 'detail': f'MILD-only={n10}, baseline-only={n01}', 'higher_is_better': True})

    # disambiguation (paper root events)
    r = j[j['root_paper_m'] == 1]
    p, n10, n01 = mcnemar_exact(r['cause_correct_m'], r['cause_correct_b'])
    cm, cb = r['cause_correct_m'].values, r['cause_correct_b'].values
    ci = boot_ci(lambda i: 100 * (cm[i].mean() - cb[i].mean()), len(r))
    rows.append({'metric': 'disambiguation_%', 'n_units': len(r), 'mild': 100 * cm.mean() if len(r) else np.nan,
                 'baseline': 100 * cb.mean() if len(r) else np.nan, 'diff': 100 * (cm.mean() - cb.mean()) if len(r) else np.nan,
                 'ci_low': ci[0], 'ci_high': ci[1], 'test': 'McNemar exact', 'p': p,
                 'detail': f'MILD-only={n10}, baseline-only={n01}', 'higher_is_better': True})

    # lead time (events detected by both)
    b = j[(j['detected_m'] == 1) & (j['detected_b'] == 1)]
    lm, lb = b['lead_m'].values.astype(float), b['lead_b'].values.astype(float)
    ci = boot_ci(lambda i: lm[i].mean() - lb[i].mean(), len(b))
    rows.append({'metric': 'lead_time_min (common detections)', 'n_units': len(b),
                 'mild': lm.mean() if len(b) else np.nan, 'baseline': lb.mean() if len(b) else np.nan,
                 'diff': (lm.mean() - lb.mean()) if len(b) else np.nan, 'ci_low': ci[0], 'ci_high': ci[1],
                 'test': 'Wilcoxon signed-rank', 'p': wilcoxon_safe(lm, lb), 'detail': '', 'higher_is_better': True})

    # FP per day
    dm_, db_ = day_m.copy(), day_b.copy()
    dd = dm_.merge(db_, on=['fold', 'day'], suffixes=('_m', '_b'))
    fm, fb = dd['fp_steps_m'].values.astype(float), dd['fp_steps_b'].values.astype(float)
    ci = boot_ci(lambda i: fm[i].mean() - fb[i].mean(), len(dd))
    rows.append({'metric': 'fp_steps_per_day', 'n_units': len(dd), 'mild': fm.mean() if len(dd) else np.nan,
                 'baseline': fb.mean() if len(dd) else np.nan, 'diff': (fm.mean() - fb.mean()) if len(dd) else np.nan,
                 'ci_low': ci[0], 'ci_high': ci[1], 'test': 'Wilcoxon signed-rank (days)',
                 'p': wilcoxon_safe(fm, fb), 'detail': _fp_detail(dd),
                 'higher_is_better': False})

    # fold-level (only meaningful with >=5 folds)
    if fold_m is not None and fold_b is not None and len(fold_m) >= 5 and len(fold_m) == len(fold_b):
        for name in ['detection', 'lead', 'fp_per_day', 'disamb']:
            a, c = np.array([f[name] for f in fold_m]), np.array([f[name] for f in fold_b])
            rows.append({'metric': f'fold_{name}', 'n_units': len(a), 'mild': a.mean(), 'baseline': c.mean(),
                         'diff': a.mean() - c.mean(), 'ci_low': np.nan, 'ci_high': np.nan,
                         'test': 'Wilcoxon signed-rank (folds)', 'p': wilcoxon_safe(a, c), 'detail': '',
                         'higher_is_better': name != 'fp_per_day'})
    return rows


# ----------------------------------------------------------------------------
# multi-seed consensus
# ----------------------------------------------------------------------------
def holm_and_verdict(sig, group_cols=('metric',)):
    """Holm-correct p within each group and add a per-row verdict."""
    sig = sig.copy()
    sig['p_holm'] = np.nan
    for _, g in sig.groupby(list(group_cols)):
        sig.loc[g.index, 'p_holm'] = holm(g['p'].values)
    better = np.where(sig['higher_is_better'], sig['diff'] > 0, sig['diff'] < 0)
    sig['verdict'] = np.where(sig['p_holm'].isna(), 'not testable (too few units)',
                     np.where((sig['p_holm'] < 0.05) & better, 'MILD significantly better',
                     np.where((sig['p_holm'] < 0.05) & ~better, 'baseline significantly better',
                              'no significant difference')))
    return sig


def consensus(by_seed):
    """Combine per-seed paired tests into one verdict per (baseline, metric).

    A claim is accepted only when it holds in at least two thirds of the seeds and
    no seed shows a significant difference in the opposite direction. This avoids
    reporting a result that depends on one lucky (or unlucky) training seed.
    """
    rows = []
    n_seeds = by_seed['seed'].nunique()
    need = int(np.ceil(2 * n_seeds / 3))
    for (bn, metric), g in by_seed.groupby(['baseline_name', 'metric'], sort=False):
        nb = int((g['verdict'] == 'MILD significantly better').sum())
        nw = int((g['verdict'] == 'baseline significantly better').sum())
        nt = int((g['verdict'] == 'not testable (too few units)').sum())
        if nt == len(g):
            v = 'not testable (too few units)'
        elif nb >= need and nw == 0:
            v = 'MILD significantly better'
        elif nw >= need and nb == 0:
            v = 'baseline significantly better'
        elif nb and nw:
            v = 'inconsistent across seeds'
        else:
            v = 'no significant difference'
        rows.append({'baseline_name': bn, 'metric': metric, 'n_units': int(g['n_units'].median()),
                     'mild': g['mild'].mean(), 'baseline': g['baseline'].mean(), 'diff': g['diff'].mean(),
                     'ci_low': g['ci_low'].mean(), 'ci_high': g['ci_high'].mean(),
                     'p_holm': float(g['p_holm'].median()) if g['p_holm'].notna().any() else np.nan,
                     'seeds_mild_better': nb, 'seeds_baseline_better': nw, 'n_seeds': len(g),
                     'higher_is_better': bool(g['higher_is_better'].iloc[0]), 'verdict': v})
    return pd.DataFrame(rows)
