#!/usr/bin/env python3
"""diagnostics_confirm.py -- confusion counts, ROC/AUC, detection-FP trade-off curves, MILD-only events and alarm
trustworthiness for FINISHED runs, from their saved test predictions. NO retraining.

  python diagnostics_confirm.py --run ~/intact/mild/compare_mlp_frozen_31k
  python diagnostics_confirm.py --run ~/intact/mild/compare_mlp_frozen_31k --run ~/intact/mild/classic_tuned_v5
  python diagnostics_confirm.py --run ~/intact/mild/train_v5/training           (mild-oran train / autotrain folder)

--run can be given several times (e.g. the confirm folder + a classic_tuned.py folder; tuned classics get the
suffix "(tuned)"). Output folder: --out (default <first run>/diagnostics).

What is read (per finished job): folds/fold1_timeseries.csv (per-tick risk ema_<intent>, gate_<intent>),
folds/fold1_metrics.json (the thresholds tau the main run used), folds/fold1_events.csv (per-event outcome).
An alarm of intent k at tick t is  ema_k(t) >= tau_k  -- exactly the rule of core.apply_policy. The script
re-derives detection and false-alarm steps from these and checks them against the saved metrics (section 0 of
the report); a mismatch is flagged, never silently ignored.

Definitions (also written into the report)
  positive ticks of intent k : inside the build-up [start, failure_time] of one of k's own events
  negative ticks of intent k : outside every own window [start, end] of k   (fp_scope own; 'any' if the run used it)
  ignored ticks              : (failure_time, end] of k's own events (after the failure; neither credited nor false)
  event-level TP / FN        : event detected / missed (own alarm in [start, failure_time]) -- the paper's metric
  event-level FP             : false-alarm episodes (maximal runs of alarm ticks in negative time)
  event-level TN             : negative time cut into blocks of L ticks (L = median build-up length of the test
                               events, so a negative unit has the size of a positive unit); a block with no alarm is
                               a TN, a block with >= 1 alarm a false block. Specificity = TN / (TN + false blocks).
  alarm precision            : alarm episodes (runs of alarm ticks of intent k) that overlap a build-up window of k,
                               divided by all alarm episodes of k -- "when it fires, how often is a failure coming"
"""
import argparse
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

try:
    from scipy.stats import wilcoxon
except Exception:                       # scipy is in the training venv; keep the numbers even without it
    wilcoxon = None

TICKS_PER_DAY = 1440.0
TRAPZ = getattr(np, 'trapezoid', None) or getattr(np, 'trapz')
NAMES = {'mild': 'MILD', 'mlp': 'MLP', 'lstm': 'LSTM', 'mlp_matched': 'MLP (MILD settings)',
         'lr': 'LR-OvR', 'wkpi': 'WKPI-Tuned', 'dist': 'Dist-Target'}
ORDER = ['MILD', 'MLP', 'MLP (MILD settings)', 'LSTM', 'LR-OvR', 'WKPI-Tuned', 'Dist-Target']
COLORS = {'MILD': '#c0392b', 'MLP': '#2471a3', 'MLP (MILD settings)': '#5dade2', 'LSTM': '#229954',
          'LR-OvR': '#7d3c98', 'WKPI-Tuned': '#b9770e', 'Dist-Target': '#707b7c'}


def color(m):
    return COLORS.get(m.replace(' (tuned)', ''), '#444444')


def morder(ms):
    return sorted(ms, key=lambda m: (ORDER.index(m.replace(' (tuned)', '')) if m.replace(' (tuned)', '') in ORDER
                                     else 99, m))


# ------------------------------------------------------------------------------------------------ job discovery
def discover(run):
    """[(method display name, seed, job folder that holds folds/)] for every finished final/confirm job in run."""
    run = os.path.abspath(os.path.expanduser(run))
    out = []
    fj = os.path.join(run, 'final_jobs.json')
    if os.path.exists(fj):                                   # autotrain / mild-oran train
        for j in json.load(open(fj))['jobs']:
            if j.get('variant', 'tuned') != 'tuned':
                continue
            d = j['dir']
            if not os.path.isdir(d):
                d = os.path.join(run, 'jobs', j['method'], os.path.basename(d))
            if j['method'] == 'classic':
                d = os.path.join(d, j['name'])
            out.append((NAMES.get(j['name'], j['name']), int(j['seed']), d))
    else:
        sel = os.path.join(run, 'classic_selected.json')
        selected = json.load(open(sel)) if os.path.exists(sel) else {}
        for sp in sorted(glob.glob(os.path.join(run, 'jobs', '*', '*', 'spec.json'))):
            d = os.path.dirname(sp)
            if not os.path.exists(os.path.join(d, 'DONE')):
                continue
            s = json.load(open(sp))
            seed = int(s['cfg'].get('seed', 42))
            for role in s.get('roles', []):
                p = role.split(':')
                if p[0] == 'confirm':                        # confirm_v6.py: confirm:<name>:seed<s>
                    if p[1] == 'classic':
                        for sub in ('lr', 'wkpi', 'dist'):
                            out.append((NAMES[sub], seed, os.path.join(d, sub)))
                    else:
                        out.append((NAMES.get(p[1], p[1]), seed, d))
                elif p[0] == 'classic_test':                 # classic_tuned.py: classic_test:h<H>
                    h = int(p[1][1:])
                    for sub, b in selected.items():
                        if int(b['label_horizon']) == h:
                            out.append((NAMES[sub] + ' (tuned)', seed, os.path.join(d, sub)))
                elif p[0] == 'final' and len(p) == 4 and p[2] == 'tuned':   # autotrain without final_jobs.json
                    dd = os.path.join(d, p[1]) if s['method'] == 'classic' else d
                    out.append((NAMES.get(p[1], p[1]), int(p[3].replace('seed', '')), dd))
    out = [o for o in out if all(os.path.exists(os.path.join(o[2], 'folds', f)) for f in
                                 ('fold1_timeseries.csv', 'fold1_metrics.json', 'fold1_events.csv'))]
    return sorted(set(out))


# ------------------------------------------------------------------------------------------------ helpers
def runs_of(mask):
    """(start_idx, end_idx_inclusive) of maximal True runs."""
    if not mask.any():
        return np.zeros((0, 2), dtype=int)
    d = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return np.stack([np.flatnonzero(d == 1), np.flatnonzero(d == -1) - 1], axis=1)


def window_mask(T, wins):
    m = np.zeros(len(T), dtype=bool)
    for s, e in wins:
        lo, hi = np.searchsorted(T, s, 'left'), np.searchsorted(T, e, 'right')
        m[lo:hi] = True
    return m


def roc_auc(score, y):
    """ROC curve (fpr, tpr, thresholds) and AUC (Mann-Whitney, ties = 1/2)."""
    pos, neg = y.sum(), (~y).sum()
    if pos == 0 or neg == 0:
        return None
    o = np.argsort(-score, kind='mergesort')
    s, yy = score[o], y[o]
    last = np.r_[np.flatnonzero(np.diff(s)), len(s) - 1]       # last index of each distinct threshold
    tp, fp = np.cumsum(yy)[last], np.cumsum(~yy)[last]
    tpr, fpr = np.r_[0, tp / pos], np.r_[0, fp / neg]
    auc = float(TRAPZ(tpr, fpr))
    return fpr, tpr, np.r_[np.inf, s[last]], auc


def pauc(fpr, tpr, maxfpr):
    """McClish-standardised partial AUC on FPR in [0, maxfpr] (0.5 = chance, 1 = perfect)."""
    g = np.linspace(0, maxfpr, 400)
    a = TRAPZ(np.interp(g, fpr, tpr), g)
    mn, mx = maxfpr ** 2 / 2, maxfpr
    return float(0.5 * (1 + (a - mn) / (mx - mn)))


def avg_precision(score, y):
    o = np.argsort(-score, kind='mergesort')
    yy = y[o]
    tp = np.cumsum(yy)
    prec = tp / np.arange(1, len(yy) + 1)
    return float((prec * yy).sum() / max(yy.sum(), 1))


def best_det_at(fp_curve, det_curve, grid):
    """Step curve: the highest detection reachable with FP <= x, for each x in grid."""
    o = np.argsort(fp_curve)
    f, d = fp_curve[o], np.maximum.accumulate(det_curve[o])
    idx = np.searchsorted(f, grid, side='right') - 1
    return np.where(idx >= 0, d[np.clip(idx, 0, None)], 0.0)


# ------------------------------------------------------------------------------------------------ one job
def analyse_job(name, seed, d, fp_grid, fpr_grid, tn_block):
    f = os.path.join(d, 'folds')
    ts = pd.read_csv(os.path.join(f, 'fold1_timeseries.csv'))
    met = json.load(open(os.path.join(f, 'fold1_metrics.json')))
    ev = pd.read_csv(os.path.join(f, 'fold1_events.csv'))
    intents = [c[4:] for c in ts.columns if c.startswith('ema_')]
    T = ts['t'].to_numpy().astype(np.int64)
    days = float(met.get('overall', {}).get('test_days') or (T[-1] - T[0]) / TICKS_PER_DAY)
    tau = {k: float(met['params'][k]['tau']) for k in intents}
    ema = {k: ts['ema_' + k].to_numpy() for k in intents}
    alarm = {k: ema[k] >= tau[k] for k in intents}
    own = {k: window_mask(T, ev.loc[ev.intent == k, ['start', 'end']].to_numpy()) for k in intents}
    anyw = window_mask(T, ev[['start', 'end']].to_numpy())
    build = {k: window_mask(T, ev.loc[ev.intent == k, ['start', 'failure_time']].to_numpy()) for k in intents}

    # ---- which FP scope did the run use? (re-derive FP steps both ways, keep the one matching the saved metric)
    saved_fp = met.get('overall', {}).get('fp_steps_total')
    fp_own = sum(int((alarm[k] & ~own[k]).sum()) for k in intents)
    fp_any = sum(int((alarm[k] & ~anyw).sum()) for k in intents)
    scope = 'any' if (saved_fp is not None and fp_any == saved_fp and fp_own != saved_fp) else 'own'
    neg = {k: (~own[k] if scope == 'own' else ~anyw) for k in intents}
    # ---- reconstruction check against the saved per-event outcome
    det_re = []
    for _, e in ev.iterrows():
        k = e.intent
        lo, hi = np.searchsorted(T, e.start, 'left'), np.searchsorted(T, e.failure_time, 'right')
        det_re.append(int(alarm[k][lo:hi].any()) if k in alarm else 0)
    ev['detected_re'] = det_re
    check = {'method': name, 'seed': seed, 'fp_scope_detected': scope,
             'fp_steps_saved': saved_fp, 'fp_steps_rebuilt': fp_own if scope == 'own' else fp_any,
             'events': len(ev), 'event_outcome_mismatches': int((ev.detected_re != ev.detected).sum())}

    rows_int, roc_rows, curves_int, trust = [], [], {}, []
    L = tn_block
    for k in intents:
        a, p, n = alarm[k], build[k], neg[k]
        e_k = ev[ev.intent == k]
        # tick level
        tick = dict(TP=int((a & p).sum()), FN=int((~a & p).sum()), FP=int((a & n).sum()), TN=int((~a & n).sum()))
        # event level
        fp_eps = runs_of(a & n)
        tn = fb = 0
        for s0, s1 in runs_of(n):
            nb = (s1 - s0 + 1) // L
            for b in range(nb):
                if a[s0 + b * L: s0 + (b + 1) * L].any():
                    fb += 1
                else:
                    tn += 1
        TPe, FNe = int(e_k.detected.sum()), int((1 - e_k.detected).sum())
        # alarm episodes and their usefulness
        eps = runs_of(a)
        useful = sum(bool(p[s0:s1 + 1].any()) for s0, s1 in eps)
        late = sum((not p[s0:s1 + 1].any()) and bool(own[k][s0:s1 + 1].any()) for s0, s1 in eps)
        frag = []
        for _, e in e_k[e_k.detected == 1].iterrows():
            lo, hi = np.searchsorted(T, e.start, 'left'), np.searchsorted(T, e.end, 'right')
            frag.append(len(runs_of(a[lo:hi])))
        rows_int.append({'method': name, 'seed': seed, 'intent': k, 'tau': tau[k],
                         'tick_TP': tick['TP'], 'tick_FN': tick['FN'], 'tick_FP': tick['FP'], 'tick_TN': tick['TN'],
                         'event_TP': TPe, 'event_FN': FNe, 'event_FP_episodes': len(fp_eps),
                         'event_TN_blocks': tn, 'event_false_blocks': fb,
                         'alarm_episodes': len(eps), 'alarm_episodes_useful': useful, 'alarm_episodes_late': late,
                         'fp_steps_per_day': tick['FP'] / days, 'fp_episodes_per_day': len(fp_eps) / days,
                         'detection_%': 100 * TPe / max(TPe + FNe, 1),
                         'mean_false_episode_ticks': float(np.mean(fp_eps[:, 1] - fp_eps[:, 0] + 1)) if len(fp_eps) else 0.0,
                         'ticks_between_false_episodes': float(n.sum() / len(fp_eps)) if len(fp_eps) else np.nan,
                         'alarm_episodes_per_detected_event': float(np.mean(frag)) if frag else np.nan})
        # ROC on ticks (positive = build-up, negative = healthy for k)
        sel = p | n
        r = roc_auc(ema[k][sel], p[sel])
        if r is not None:
            fpr, tpr, thr, auc = r
            roc_rows.append({'method': name, 'seed': seed, 'intent': k, 'auc': auc,
                             'pauc_fpr<=0.01': pauc(fpr, tpr, 0.01), 'pauc_fpr<=0.05': pauc(fpr, tpr, 0.05),
                             'avg_precision': avg_precision(ema[k][sel], p[sel]),
                             'op_fpr': tick['FP'] / max(n.sum(), 1), 'op_tpr': tick['TP'] / max(p.sum(), 1)})
            curves_int.setdefault(('roc', k), np.interp(fpr_grid, fpr, tpr))
        # per-intent event-level trade-off: detection_k vs FP steps/day of intent k, sweeping tau_k
        mx = np.array([ema[k][np.searchsorted(T, s, 'left'):np.searchsorted(T, fl, 'right')].max(initial=-np.inf)
                       for s, fl in e_k[['start', 'failure_time']].to_numpy()])
        negs = np.sort(ema[k][n])
        cand = np.unique(np.r_[mx[np.isfinite(mx)], np.quantile(negs, 1 - np.logspace(-6, 0, 300)) if len(negs) else []])
        cand = np.r_[cand, np.inf]
        fpc = (len(negs) - np.searchsorted(negs, cand, 'left')) / days
        detc = np.array([100 * np.mean(mx >= c) if len(mx) else 0.0 for c in cand])
        curves_int[('tradeoff', k)] = best_det_at(fpc, detc, fp_grid)
        curves_int[('maxbuild', k)] = (mx, negs)
    # ---- macro trade-off: one common healthy-time quantile for every intent (same rule as opcurve.py)
    qs = np.r_[1 - np.logspace(-6, np.log10(0.5), 400), 1.0]
    fp_m, det_m = [], []
    for q in qs:
        f_, d_ = 0.0, []
        for k in intents:
            mx, negs = curves_int[('maxbuild', k)]
            th = np.nextafter(np.quantile(negs, q) if len(negs) else -np.inf, np.inf)   # q = 1: zero false alarms
            f_ += (len(negs) - np.searchsorted(negs, th, 'left')) / days
            if len(mx):
                d_.append(100 * np.mean(mx >= th))
        fp_m.append(f_)
        det_m.append(np.mean(d_) if d_ else 0.0)
    curves_int['macro_tradeoff'] = best_det_at(np.array(fp_m), np.array(det_m), fp_grid)
    for k in intents:
        curves_int.pop(('maxbuild', k))
    ev['method'], ev['seed'] = name, seed
    return {'intents': intents, 'rows': rows_int, 'roc': roc_rows, 'curves': curves_int, 'check': check,
            'events': ev, 'op': {'fp': sum(r['fp_steps_per_day'] for r in rows_int),
                                  'det': float(np.mean([r['detection_%'] for r in rows_int]))}}


# ------------------------------------------------------------------------------------------------ main
def paired_test(a, b):
    """MILD seed values a vs baseline values b (paired by seed, or b constant for a deterministic baseline)."""
    d = np.asarray(a) - np.asarray(b)
    if wilcoxon is None or len(d) < 5 or np.allclose(d, 0):
        return np.nan
    try:
        return float(wilcoxon(d, zero_method='pratt').pvalue)
    except Exception:
        return np.nan


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--run', action='append', required=True, help='run folder (repeatable)')
    ap.add_argument('--out', default='')
    ap.add_argument('--fp_max', type=float, default=15.0, help='x-range of the trade-off curves (FP steps/1440 ticks)')
    ap.add_argument('--budget', type=float, default=3.0, help='total FP budget line (per-intent line = budget/#intents)')
    ap.add_argument('--early', type=int, default=10, help='lead (ticks) for an actionable alert')
    ap.add_argument('--majority', type=float, default=0.5,
                    help='an event counts as "detected by a method" if detected in more than this fraction of seeds')
    ap.add_argument('--tn_block', type=int, default=0, help='TN block length in ticks (0 = median build-up length)')
    a = ap.parse_args()
    jobs = []
    for r in a.run:
        jobs += discover(r)
    if not jobs:
        sys.exit('no finished jobs with saved test predictions found in ' + ', '.join(a.run))
    out = os.path.abspath(os.path.expanduser(a.out or os.path.join(a.run[0], 'diagnostics')))
    os.makedirs(out, exist_ok=True)
    print(f'{len(jobs)} finished jobs:', {m: sum(j[0] == m for j in jobs) for m in morder({j[0] for j in jobs})},
          flush=True)

    if a.tn_block <= 0:
        e0 = pd.read_csv(os.path.join(jobs[0][2], 'folds', 'fold1_events.csv'))
        a.tn_block = int(max(3, round(float(np.median(e0.failure_time - e0.start)))))
    fp_grid = np.linspace(0, a.fp_max, 301)
    fpr_grid = np.r_[0, np.logspace(-5, 0, 400)]

    R = {}
    for name, seed, d in jobs:
        R[(name, seed)] = analyse_job(name, seed, d, fp_grid, fpr_grid, a.tn_block)
        print('  done', name, seed, flush=True)
    intents = R[next(iter(R))]['intents']
    methods = morder({k[0] for k in R})
    INT = pd.DataFrame([r for v in R.values() for r in v['rows']])
    ROC = pd.DataFrame([r for v in R.values() for r in v['roc']])
    CHK = pd.DataFrame([v['check'] for v in R.values()])
    EV = pd.concat([v['events'] for v in R.values()], ignore_index=True)
    INT.to_csv(os.path.join(out, 'confusion_per_intent_runs.csv'), index=False)
    ROC.to_csv(os.path.join(out, 'roc_auc_runs.csv'), index=False)
    CHK.to_csv(os.path.join(out, 'reconstruction_check.csv'), index=False)

    # ---------------- 1. confusion counts (sum over intents per run, then mean over seeds)
    s = INT.groupby(['method', 'seed']).sum(numeric_only=True).reset_index()
    s['event_recall_%'] = 100 * s.event_TP / (s.event_TP + s.event_FN)
    s['event_precision_%'] = 100 * s.event_TP / (s.event_TP + s.event_FP_episodes).clip(lower=1)
    s['event_specificity_%'] = 100 * s.event_TN_blocks / (s.event_TN_blocks + s.event_false_blocks).clip(lower=1)
    s['event_F1_%'] = 2 * s['event_precision_%'] * s['event_recall_%'] / (s['event_precision_%'] + s['event_recall_%']).clip(lower=1e-9)
    s['tick_recall_%'] = 100 * s.tick_TP / (s.tick_TP + s.tick_FN)
    s['tick_precision_%'] = 100 * s.tick_TP / (s.tick_TP + s.tick_FP).clip(lower=1)
    s['tick_FPR_%'] = 100 * s.tick_FP / (s.tick_FP + s.tick_TN)
    s['alarm_precision_%'] = 100 * s.alarm_episodes_useful / s.alarm_episodes.clip(lower=1)
    s.to_csv(os.path.join(out, 'confusion_runs.csv'), index=False)
    ccols = ['event_TP', 'event_FN', 'event_FP_episodes', 'event_TN_blocks', 'event_false_blocks',
             'event_recall_%', 'event_precision_%', 'event_specificity_%', 'event_F1_%']
    tcols = ['tick_TP', 'tick_FN', 'tick_FP', 'tick_TN', 'tick_recall_%', 'tick_precision_%', 'tick_FPR_%']
    CE = s.groupby('method')[ccols].mean().reindex(methods).round(1)
    CT = s.groupby('method')[tcols].mean().reindex(methods).round(2)
    CI = INT.groupby(['intent', 'method'])[['event_TP', 'event_FN', 'event_FP_episodes', 'event_TN_blocks',
                                             'event_false_blocks', 'detection_%', 'fp_steps_per_day']].mean().round(1)
    CE.to_csv(os.path.join(out, 'confusion_event_summary.csv'))
    CT.to_csv(os.path.join(out, 'confusion_tick_summary.csv'))
    CI.to_csv(os.path.join(out, 'confusion_per_intent_summary.csv'))

    # ---------------- 2. ROC / AUC (tick level) + area under the event-level trade-off curve
    rm = ROC.groupby(['method', 'seed'])[['auc', 'pauc_fpr<=0.01', 'pauc_fpr<=0.05', 'avg_precision']].mean().reset_index()
    for (m, sd), v in R.items():
        c = v['curves']['macro_tradeoff']
        lim = fp_grid <= a.budget * 2
        rm.loc[(rm.method == m) & (rm.seed == sd), f'mean_det_FP<={a.budget * 2:g}'] = float(TRAPZ(c[lim], fp_grid[lim]) / fp_grid[lim][-1])
        rm.loc[(rm.method == m) & (rm.seed == sd), f'det_at_FP={a.budget:g}'] = float(np.interp(a.budget, fp_grid, c))
    rm.to_csv(os.path.join(out, 'auc_macro_runs.csv'), index=False)
    acols = ['auc', 'pauc_fpr<=0.01', 'pauc_fpr<=0.05', 'avg_precision', f'mean_det_FP<={a.budget * 2:g}', f'det_at_FP={a.budget:g}']
    AU = rm.groupby('method')[acols].agg(['mean', 'std']).reindex(methods).round(3)
    AUI = ROC.groupby(['intent', 'method'])[['auc', 'pauc_fpr<=0.01', 'avg_precision']].mean().round(3)
    AU.to_csv(os.path.join(out, 'auc_summary.csv'))
    AUI.to_csv(os.path.join(out, 'auc_per_intent_summary.csv'))
    pr = []
    mm = rm[rm.method == 'MILD'].set_index('seed')
    for b in methods:
        if b == 'MILD' or mm.empty:
            continue
        o = rm[rm.method == b].set_index('seed')
        for col in acols:
            if len(o) == 1:                                # deterministic baseline: compare every MILD seed with it
                av, bv = mm[col].to_numpy(), np.full(len(mm), o[col].iloc[0])
            else:
                sd = mm.index.intersection(o.index)
                av, bv = mm.loc[sd, col].to_numpy(), o.loc[sd, col].to_numpy()
            if len(av) == 0:
                continue
            pr.append({'baseline': b, 'metric': col, 'MILD': round(av.mean(), 3), 'baseline_value': round(bv.mean(), 3),
                       'seeds_MILD_higher': int((av > bv).sum()), 'seeds_baseline_higher': int((av < bv).sum()),
                       'seeds': len(av), 'wilcoxon_p': paired_test(av, bv)})
    PRS = pd.DataFrame(pr)
    PRS.to_csv(os.path.join(out, 'auc_paired.csv'), index=False)

    # ---------------- 3. which events does MILD detect that others do not
    key = ['intent', 'type', 'start', 'failure_time', 'end']
    fr = EV.groupby(key + ['method']).detected.mean().unstack('method').reindex(columns=methods)
    ld = EV[EV.detected == 1].groupby(key + ['method']).lead.median().unstack('method').reindex(columns=methods)
    ev_tab = fr.round(2).add_prefix('det_frac_').join(ld.round(1).add_prefix('median_lead_')).reset_index()
    ev_tab.to_csv(os.path.join(out, 'events_detection_by_method.csv'), index=False)
    maj = fr > a.majority
    uniq, lists = [], []
    if 'MILD' in maj:
        for b in methods:
            if b == 'MILD':
                continue
            mo, bo = maj['MILD'] & ~maj[b], ~maj['MILD'] & maj[b]
            both, neither = maj['MILD'] & maj[b], ~maj['MILD'] & ~maj[b]
            for (k, t), g in fr.groupby(level=['intent', 'type']):
                idx = g.index
                uniq.append({'baseline': b, 'intent': k, 'type': t, 'events': len(idx),
                             'both': int(both[idx].sum()), 'MILD_only': int(mo[idx].sum()),
                             'baseline_only': int(bo[idx].sum()), 'neither': int(neither[idx].sum())})
            x = ev_tab[mo.to_numpy()].assign(category='MILD only', baseline=b)
            y = ev_tab[bo.to_numpy()].assign(category='baseline only', baseline=b)
            lists += [x, y]
        # per-seed discordant pairs (what McNemar uses)
        disc = []
        for b in methods:
            if b == 'MILD':
                continue
            for sd in sorted(EV[EV.method == 'MILD'].seed.unique()):
                em = EV[(EV.method == 'MILD') & (EV.seed == sd)].set_index(key).detected
                bs = EV[EV.method == b]
                bsd = sd if sd in set(bs.seed) else (bs.seed.iloc[0] if len(bs) else None)
                if bsd is None:
                    continue
                eb = bs[bs.seed == bsd].set_index(key).detected
                j = pd.concat([em, eb], axis=1, keys=['m', 'b']).dropna()
                disc.append({'baseline': b, 'seed': sd, 'MILD_only': int(((j.m == 1) & (j.b == 0)).sum()),
                             'baseline_only': int(((j.m == 0) & (j.b == 1)).sum())})
        DISC = pd.DataFrame(disc)
        DISC.to_csv(os.path.join(out, 'discordant_events_by_seed.csv'), index=False)
    UQ = pd.DataFrame(uniq)
    UQ.to_csv(os.path.join(out, 'unique_events_by_type.csv'), index=False)
    if lists:
        pd.concat(lists).to_csv(os.path.join(out, 'unique_events_list.csv'), index=False)

    # ---------------- 4/5. operational trustworthiness
    tr = []
    for (m, sd), v in R.items():
        e = v['events']
        rows = pd.DataFrame(v['rows'])
        det = e[e.detected == 1]
        root = e[e.root_paper == 1]
        neg_ticks = sum(r['tick_FP'] + r['tick_TN'] for r in v['rows'])
        n_fe = rows.event_FP_episodes.sum()
        tr.append({'method': m, 'seed': sd,
                   'alarm_precision_%': 100 * rows.alarm_episodes_useful.sum() / max(rows.alarm_episodes.sum(), 1),
                   'late_alarm_episodes_%': 100 * rows.alarm_episodes_late.sum() / max(rows.alarm_episodes.sum(), 1),
                   'false_episodes_per_day': n_fe / max(neg_ticks / len(v['rows']) / TICKS_PER_DAY, 1e-9) if len(v['rows']) else np.nan,
                   'ticks_between_false_episodes': neg_ticks / len(v['rows']) / n_fe if n_fe else np.nan,
                   'mean_false_episode_ticks': float(np.average(rows.mean_false_episode_ticks, weights=rows.event_FP_episodes.clip(lower=1e-9))) if n_fe else 0.0,
                   'alarm_episodes_per_detected_event': float(np.nanmean(rows.alarm_episodes_per_detected_event)),
                   'detection_%': 100 * e.detected.mean(),
                   'median_lead': float(det.lead.median()) if len(det) else np.nan,
                   'p10_lead': float(det.lead.quantile(0.1)) if len(det) else np.nan,
                   f'actionable_%(lead>={a.early})': 100 * ((e.detected == 1) & (e.lead >= a.early)).mean(),
                   'correct_cause_%_root': 100 * root.cause_correct.mean() if len(root) else np.nan,
                   'wrong_cause_when_alarmed_%_root': 100 * ((root.pred_cause.notna()) & (root.cause_correct == 0)).sum()
                   / max(root.pred_cause.notna().sum(), 1) if len(root) else np.nan})
    TR = pd.DataFrame(tr)
    TR.to_csv(os.path.join(out, 'trust_runs.csv'), index=False)
    TRS = TR.groupby('method').mean(numeric_only=True).drop(columns='seed').reindex(methods).round(2)
    TRS.to_csv(os.path.join(out, 'trust_summary.csv'))
    # seed agreement: how reproducible is each detection across seeds
    agree = []
    for m in methods:
        if fr[m].notna().sum() == 0 or EV[EV.method == m].seed.nunique() < 2:
            continue
        f = fr[m].dropna()
        agree.append({'method': m, 'events_always_detected_%': round(100 * (f == 1).mean(), 1),
                      'events_never_detected_%': round(100 * (f == 0).mean(), 1),
                      'events_seed_dependent_%': round(100 * ((f > 0) & (f < 1)).mean(), 1)})
    AG = pd.DataFrame(agree)
    AG.to_csv(os.path.join(out, 'seed_agreement.csv'), index=False)
    # cause confusion (root events, pooled over seeds)
    conf = (EV[EV.root_paper == 1].assign(pred=lambda x: x.pred_cause.fillna('no alarm'))
            .groupby(['method', 'intent', 'pred']).size().rename('n').reset_index())
    conf.to_csv(os.path.join(out, 'cause_confusion.csv'), index=False)

    # ---------------- plots
    figs = make_plots(out, R, methods, intents, fp_grid, fpr_grid, a, EV, fr, maj, UQ, TR, conf)

    # ---------------- report
    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', 40)
    bad = CHK[(CHK.event_outcome_mismatches > 0) | (CHK.fp_steps_saved.notna() & (CHK.fp_steps_saved != CHK.fp_steps_rebuilt))]
    L = ['# Diagnostics from saved test predictions (no retraining)', '',
         'Runs: ' + ', '.join(os.path.abspath(os.path.expanduser(r)) for r in a.run),
         f'Jobs: {len(R)}; methods: {", ".join(methods)}; intents: {", ".join(intents)}.', '',
         '## 0. Reconstruction check', '',
         'Alarms rebuilt as ema >= tau (the main run\'s thresholds). Each run\'s re-derived FP steps and per-event '
         'detections are compared with the saved metrics.', '',
         ('ALL RUNS MATCH (FP steps and every event outcome).' if bad.empty else
          'MISMATCH in the runs below -- do not use their rebuilt numbers before checking:\n\n' + bad.to_string(index=False)),
         f'FP scope used by the runs: {", ".join(sorted(CHK.fp_scope_detected.unique()))}.', '',
         '## 1. Confusion counts at the main (development-calibrated) thresholds', '',
         '### Event level (sum over intents, mean over seeds)', '',
         f'TP/FN = failure episodes detected/missed (own alarm in [start, failure_time]). FP = false-alarm episodes. '
         f'TN/false blocks: healthy time cut into blocks of {a.tn_block} ticks (median build-up length); a block '
         'without alarm is a TN. Precision = TP/(TP+FP episodes); specificity = TN/(TN+false blocks).', '',
         CE.to_string(), '',
         '### Tick level (sum over intents, mean over seeds)', '',
         'Positive tick = inside a build-up [start, failure_time] of the intent\'s own event; negative = healthy for '
         'that intent; ticks after the failure are ignored. Tick FP = the paper\'s FP steps.', '',
         CT.to_string(), '', '### Per intent (mean over seeds)', '', CI.to_string(), '',
         '## 2. ROC / AUC', '',
         'Tick-level ROC of each intent\'s smoothed risk (positive = build-up ticks, negative = healthy ticks), '
         'macro-averaged over intents. pAUC = McClish-standardised partial AUC on FPR <= 1 % / 5 % (0.5 = chance); '
         'the low-FPR region is the one that matters at the FP budget. avg_precision = area under the '
         f'precision-recall curve. mean_det_FP<={a.budget * 2:g} = mean macro detection over FP 0..{a.budget * 2:g} '
         f'steps/1440 ticks on the event-level trade-off curve; det_at_FP={a.budget:g} = detection at the budget '
         '(thresholds re-placed on the test part with one common healthy-time quantile: post-hoc, score quality only).', '',
         AU.to_string(), '', '### Per intent (mean over seeds)', '', AUI.to_string(), '',
         '### MILD vs each method (per seed; Wilcoxon signed-rank over seeds)', '',
         PRS.to_string(index=False) if not PRS.empty else '(no MILD runs)', '',
         '## 3. Events MILD detects that the other method does not', '',
         f'An event counts as detected by a method when detected in more than {100 * a.majority:g} % of its seeds '
         '(deterministic classics: their single run). Full list: unique_events_list.csv; every event with each '
         'method\'s detection fraction and median lead: events_detection_by_method.csv.', '',
         (UQ.groupby('baseline')[['events', 'both', 'MILD_only', 'baseline_only', 'neither']].sum()
          .reindex([m for m in methods if m != 'MILD']).to_string() if not UQ.empty else '(no MILD runs)'), '',
         '### By intent and event type', '',
         UQ[UQ.baseline.isin([m for m in methods if not m.startswith(('LR', 'WKPI', 'Dist'))])].to_string(index=False)
         if not UQ.empty else '', '',
         '### Per seed (the discordant pairs McNemar uses), mean over seeds', '',
         (pd.read_csv(os.path.join(out, 'discordant_events_by_seed.csv')).groupby('baseline')[['MILD_only', 'baseline_only']]
          .agg(['mean', 'min', 'max']).round(1).to_string() if os.path.exists(os.path.join(out, 'discordant_events_by_seed.csv')) else ''), '',
         '## 4. Operational trustworthiness (main thresholds, mean over seeds)', '',
         'alarm_precision_% = alarm episodes that overlap a real build-up of the same intent (when it fires, a failure '
         'is coming); late_alarm_episodes_% = episodes only after the failure; ticks_between_false_episodes = mean '
         'healthy time between false episodes; alarm_episodes_per_detected_event = alarm fragmentation (1 = one clean '
         f'alarm); actionable = detected >= {a.early} ticks before the failure; wrong_cause_when_alarmed = root '
         'episodes with an alarm whose named cause is wrong.', '',
         TRS.T.to_string(), '', '### Seed agreement (how reproducible each detection is)', '',
         AG.to_string(index=False) if not AG.empty else '(single seed)', '',
         '## Figures', ''] + [f'- {f}' for f in figs] + ['']
    open(os.path.join(out, 'DIAGNOSTICS_REPORT.md'), 'w').write('\n'.join(L) + '\n')
    print('\n'.join(L), flush=True)
    print('\nOutput folder:', out)


# ------------------------------------------------------------------------------------------------ plots
def make_plots(out, R, methods, intents, fp_grid, fpr_grid, a, EV, fr, maj, UQ, TR, conf):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except Exception:
        print('matplotlib not installed: numbers written, no figures. Install: '
              '~/intact/mild/venv/bin/pip install matplotlib', flush=True)
        return []
    plt.rcParams.update({'font.size': 9, 'axes.grid': True, 'grid.alpha': 0.3, 'savefig.dpi': 200})
    figs = []

    def band(ax, x, ys, m, label=True, log=False):
        Y = np.array(ys)
        mu = Y.mean(0)
        ax.plot(x, mu, color=color(m), lw=1.8 if m == 'MILD' else 1.2, label=m if label else None)
        if len(Y) > 1:
            ax.fill_between(x, Y.min(0), Y.max(0), color=color(m), alpha=0.12, lw=0)

    def save(fig, name):
        fig.tight_layout()
        fig.savefig(os.path.join(out, name))
        plt.close(fig)
        figs.append(name)

    # fig1 macro trade-off
    fig, ax = plt.subplots(figsize=(6.4, 4.2))
    for m in methods:
        ys = [v['curves']['macro_tradeoff'] for (mm, _), v in R.items() if mm == m]
        band(ax, fp_grid, ys, m)
        ops = [v['op'] for (mm, _), v in R.items() if mm == m]
        ax.scatter([o['fp'] for o in ops], [o['det'] for o in ops], color=color(m), s=12, marker='x', alpha=0.7)
    ax.axvline(a.budget, color='k', ls='--', lw=0.8)
    ax.text(a.budget, 2, f' budget {a.budget:g}', fontsize=8)
    ax.set(xlabel='false-alarm steps per 1440 ticks (all intents, own scope)', ylabel='macro detection (%)',
           xlim=(0, a.fp_max), ylim=(0, 100),
           title='Detection vs false alarms (test part, common healthy-time quantile)\nline = seed mean, band = seed range, x = main run')
    ax.legend(fontsize=7, loc='lower right')
    save(fig, 'fig1_tradeoff_macro.png')

    # fig2 per-intent trade-off + operating points
    fig, axs = plt.subplots(1, len(intents), figsize=(4.2 * len(intents), 3.8), squeeze=False)
    for i, k in enumerate(intents):
        ax = axs[0, i]
        for m in methods:
            ys = [v['curves'][('tradeoff', k)] for (mm, _), v in R.items() if mm == m]
            band(ax, fp_grid, ys, m, label=(i == 0))
            rows = [r for (mm, _), v in R.items() if mm == m for r in v['rows'] if r['intent'] == k]
            ax.scatter([r['fp_steps_per_day'] for r in rows], [r['detection_%'] for r in rows], color=color(m), s=12,
                       marker='x', alpha=0.7)
        ax.axvline(a.budget / len(intents), color='k', ls='--', lw=0.8)
        ax.set(title=f'intent: {k}', xlabel='FP steps / 1440 ticks (this intent)', xlim=(0, a.fp_max / 2), ylim=(0, 102))
        if i == 0:
            ax.set_ylabel('detection (%)')
    axs[0, 0].legend(fontsize=7, loc='lower right')
    fig.suptitle('Per-intent detection vs false alarms (curve = threshold sweep on test; x = main operating point per seed)')
    save(fig, 'fig2_tradeoff_per_intent.png')

    # fig3 ROC per intent (log FPR)
    fig, axs = plt.subplots(1, len(intents), figsize=(4.2 * len(intents), 3.8), squeeze=False)
    for i, k in enumerate(intents):
        ax = axs[0, i]
        for m in methods:
            ys = [v['curves'][('roc', k)] for (mm, _), v in R.items() if mm == m and ('roc', k) in v['curves']]
            if not ys:
                continue
            aucs = [r['auc'] for (mm, _), v in R.items() if mm == m for r in v['roc'] if r['intent'] == k]
            Y = np.array(ys)
            ax.plot(fpr_grid[1:], Y.mean(0)[1:], color=color(m), lw=1.8 if m == 'MILD' else 1.1,
                    label=f'{m} (AUC {np.mean(aucs):.3f})')
            ops = [r for (mm, _), v in R.items() if mm == m for r in v['roc'] if r['intent'] == k]
            ax.scatter([max(o['op_fpr'], 1e-5) for o in ops], [o['op_tpr'] for o in ops], color=color(m), s=10, marker='x')
        ax.plot([1e-5, 1], [1e-5, 1], color='grey', lw=0.6, ls=':')
        ax.set(xscale='log', xlim=(1e-4, 1), ylim=(0, 1.01), title=f'intent: {k}', xlabel='false positive rate (healthy ticks, log)')
        if i == 0:
            ax.set_ylabel('true positive rate (build-up ticks)')
        ax.legend(fontsize=6.5, loc='upper left')
    fig.suptitle('Tick-level ROC of the smoothed risk (mean over seeds; x = main operating point)')
    save(fig, 'fig3_roc_per_intent.png')

    # fig4 unique events per baseline
    if not UQ.empty:
        bl = [m for m in methods if m != 'MILD']
        g = UQ.groupby(['baseline', 'intent'])[['MILD_only', 'baseline_only']].sum()
        fig, ax = plt.subplots(figsize=(max(7, 0.75 * len(bl) * len(intents)), 4.4))
        x, labels = 0, []
        for b in bl:
            for k in intents:
                mo = g.loc[(b, k), 'MILD_only'] if (b, k) in g.index else 0
                bo = g.loc[(b, k), 'baseline_only'] if (b, k) in g.index else 0
                ax.bar(x - 0.2, mo, 0.4, color=color('MILD'))
                ax.bar(x + 0.2, bo, 0.4, color=color(b))
                ax.text(x - 0.2, mo, str(int(mo)), ha='center', va='bottom', fontsize=7)
                ax.text(x + 0.2, bo, str(int(bo)), ha='center', va='bottom', fontsize=7)
                labels.append(f'{b}\n{k}')
                x += 1
            x += 0.5
        ax.set_xticks([i + 0.5 * (i // len(intents)) for i in range(len(labels))])
        ax.set_xticklabels(labels, fontsize=6.5, rotation=45, ha='right')
        ax.set_ylabel('events')
        ax.set_title('Events detected only by MILD (red) vs only by the other method (right bar), majority of seeds')
        save(fig, 'fig4_unique_events.png')

    # fig5 per-event detection heatmap
    H = fr.reindex(columns=methods)
    fig, ax = plt.subplots(figsize=(1.0 + 0.9 * len(methods), max(4, 0.06 * len(H) + 1.5)))
    im = ax.imshow(H.to_numpy(dtype=float), aspect='auto', cmap='viridis', vmin=0, vmax=1, interpolation='nearest')
    ax.set_xticks(range(len(methods)))
    ax.set_xticklabels(methods, rotation=40, ha='right', fontsize=7)
    ints = H.index.get_level_values('intent')
    bounds = [i for i in range(1, len(ints)) if ints[i] != ints[i - 1]]
    for b in bounds:
        ax.axhline(b - 0.5, color='w', lw=1)
    starts = [0] + bounds
    ax.set_yticks([(s + e) / 2 for s, e in zip(starts, bounds + [len(ints)])])
    ax.set_yticklabels([ints[s] for s in starts])
    ax.grid(False)
    fig.colorbar(im, ax=ax, label='fraction of seeds that detect the event')
    ax.set_title('Every test failure episode (rows, by intent) x method')
    save(fig, 'fig5_event_heatmap.png')

    # fig6 trust: alarm precision vs detection, lead distribution
    fig, axs = plt.subplots(1, 2, figsize=(10, 3.9))
    for m in methods:
        t = TR[TR.method == m]
        axs[0].scatter(t['detection_%'], t['alarm_precision_%'], color=color(m), s=16, label=m, alpha=0.8)
    axs[0].set(xlabel='event detection (%)', ylabel='alarm precision (%)\n(alarm episodes followed by a real failure)',
               title='When it fires, is a failure coming? (one dot = one seed)', xlim=(0, 100), ylim=(0, 100))
    axs[0].legend(fontsize=7)
    data = [EV[(EV.method == m) & (EV.detected == 1)].lead.to_numpy() for m in methods]
    keep = [i for i, d in enumerate(data) if len(d)]
    bp = axs[1].boxplot([data[i] for i in keep], patch_artist=True, showfliers=False)
    for p, i in zip(bp['boxes'], keep):
        p.set_facecolor(color(methods[i]))
        p.set_alpha(0.5)
    axs[1].set_xticks(range(1, len(keep) + 1))
    axs[1].set_xticklabels([methods[i] for i in keep], rotation=30, ha='right', fontsize=7)
    axs[1].axhline(a.early, color='k', ls='--', lw=0.8)
    axs[1].set(ylabel='lead time (ticks before failure)', title=f'Lead of detected episodes (dashed = {a.early} ticks)')
    save(fig, 'fig6_trust.png')

    # fig7 cause confusion (row-normalised) for the neural methods
    nm = [m for m in methods if not m.startswith(('LR', 'WKPI', 'Dist'))]
    if nm and not conf.empty:
        cols = intents + ['no alarm']
        fig, axs = plt.subplots(1, len(nm), figsize=(3.3 * len(nm), 3.2), squeeze=False)
        for i, m in enumerate(nm):
            c = conf[conf.method == m].pivot_table(index='intent', columns='pred', values='n', aggfunc='sum')
            c = c.reindex(index=intents, columns=cols).fillna(0)
            cn = c.div(c.sum(axis=1).replace(0, 1), axis=0)
            ax = axs[0, i]
            ax.imshow(cn.to_numpy(), cmap='Blues', vmin=0, vmax=1)
            for r in range(cn.shape[0]):
                for q in range(cn.shape[1]):
                    ax.text(q, r, f'{cn.iloc[r, q]:.2f}', ha='center', va='center', fontsize=7,
                            color='w' if cn.iloc[r, q] > 0.6 else 'k')
            ax.set_xticks(range(len(cols)))
            ax.set_xticklabels(cols, rotation=35, ha='right', fontsize=7)
            ax.set_yticks(range(len(intents)))
            ax.set_yticklabels(intents if i == 0 else [], fontsize=7)
            ax.set_title(m, fontsize=8)
            ax.grid(False)
        fig.suptitle('Named root cause on root episodes (rows = true cause, row-normalised, pooled seeds)')
        save(fig, 'fig7_cause_confusion.png')
    return figs


if __name__ == '__main__':
    main()
