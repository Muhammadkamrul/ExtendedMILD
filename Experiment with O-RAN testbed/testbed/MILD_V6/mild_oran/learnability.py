"""learnability.py -- fast, periodic check that the data collected so far can be learned.

Runs automatically every qa.learnability_every_segments finished segments (and on
demand with `mild-oran learncheck`). It answers one question early: "if we keep
collecting like this, will MILD have something to learn?" -- in about a minute,
without TensorFlow.

Procedure
  1. Merge the finished segments into a SEPARATE folder (<state>/learncheck/dataset);
     the real dataset is not touched.
  2. Chronological split at a quiet point: first 70 % = train, last 30 % = test.
  3. Features: exactly MILD's feature engineering (mild_repro.core.engineer: the base
     KPIs plus 5- and 15-step rolling means and standard deviations).
  4. Failure-prediction probe, per intent: class-balanced logistic regression on the
     binary label y_bin with horizon H (default 10 ticks):
       - PR-AUC on the test part, and its lift over the base rate;
       - event recall: share of test failures with a score above the 99th percentile
         of healthy-time test scores somewhere in [start, failure_time].
  5. Root-cause probe: multinomial logistic regression trained on the rows of the
     root-cause events (independent and co-drift causes) to name the intent; on test
     events the majority vote over the event's pre-failure rows is the prediction.
     Accuracy overall and on co-drift events, against the majority-class baseline.
  6. Verdict PASS / WARN / FAIL / INSUFFICIENT, with the top features per intent
     (which KPIs carry the signal) and concrete advice. History in learnability.json.

A linear probe is deliberately weaker than MILD: when even it finds the precursors,
the data is learnable; when it finds nothing, days of further collection would be wasted.
"""
from __future__ import annotations

import json
import os
import sys
import time

import numpy as np
import pandas as pd

from . import util

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _core():
    p = os.path.join(ROOT, 'MILD_unified_repro')
    if p not in sys.path:
        sys.path.insert(0, p)
    from mild_repro import core  # noqa
    return core


def _grade(v, good, ok):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return 'FAIL'
    return 'PASS' if v >= good else ('WARN' if v >= ok else 'FAIL')


def check(state_dir, cfg, targets, horizon=None, subdir='learncheck'):
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import average_precision_score
    from sklearn.preprocessing import StandardScaler
    from .quality import assess
    core = _core()
    H = int(horizon or cfg.get('qa', {}).get('learnability_horizon', 10))
    out_root = os.path.join(state_dir, subdir)
    os.makedirs(out_root, exist_ok=True)
    rep_q, ds = assess(state_dir, cfg, targets, out_dir=os.path.join(out_root, 'dataset'))
    df, ev, intents = core.load_dataset(os.path.join(ds, 'dataset.csv'), os.path.join(ds, 'events_by_intent.json'))
    res = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'rows': int(len(df)), 'segments': rep_q['segments'],
           'horizon': H, 'events_per_intent': {k: len(v) for k, v in ev.items()}, 'intents': {}, 'root': {},
           'dataset_checks': rep_q['checks']}
    t = df['t'].values
    win = [(e['start'], e['end']) for L in ev.values() for e in L]
    busy = core.in_any_window(t, win)
    cut = int(0.7 * len(df))
    while cut < len(df) and busy[cut]:
        cut += 1
    t_cut = t[cut] if cut < len(df) else t[-1]
    tr_ev = core.filter_events(ev, t[0], t_cut - 1)
    te_ev = core.filter_events(ev, t_cut, t[-1])
    n_te = {k: len(v) for k, v in te_ev.items()}
    if len(df) < 2000 or min(n_te.values() or [0]) < 5 or min(len(v) for v in tr_ev.values()) < 12:
        res['verdict'] = 'INSUFFICIENT'
        res['advice'] = ['Not enough data yet for a meaningful check (needs >= 2000 rows, >= 12 training and >= 5 test '
                         'events per intent; about 8 segments of 30 min). Keep collecting; the check runs again '
                         'automatically every qa.learnability_every_segments segments.']
        _save(out_root, res)
        return res
    X, feats = core.engineer(df)
    X = X.values.astype('float32')
    Y = core.build_binary_labels(df, ev, intents, H)
    sc = StandardScaler().fit(X[:cut])
    Xtr, Xte = sc.transform(X[:cut]), sc.transform(X[cut:])
    healthy_te = ~busy[cut:]
    grades = []
    for j, it in enumerate(intents):
        ytr, yte = Y[:cut, j], Y[cut:, j]
        if ytr.sum() < 5 or yte.sum() < 3:
            res['intents'][it] = {'note': 'too few positives'}
            grades.append('WARN')
            continue
        lr = LogisticRegression(max_iter=600, class_weight='balanced')
        lr.fit(Xtr, ytr)
        p = lr.predict_proba(Xte)[:, 1]
        ap = float(average_precision_score(yte, p))
        base = float(yte.mean())
        thr = float(np.quantile(p[healthy_te], 0.99)) if healthy_te.any() else 0.5
        tt = t[cut:]
        hit = 0
        for e in te_ev[it]:
            m = (tt >= e['start']) & (tt <= e['failure_time'])
            hit += int(m.any() and (p[m] > thr).any())
        rec = hit / max(1, len(te_ev[it]))
        coef = lr.coef_[0]
        top = [feats[k] for k in np.argsort(-np.abs(coef))[:5]]
        g1 = _grade(ap / max(base, 1e-9), 3.0, 2.0) if ap >= 0.15 else ('WARN' if ap / max(base, 1e-9) >= 2 else 'FAIL')
        g2 = _grade(rec, 0.5, 0.25)
        g = 'FAIL' if 'FAIL' in (g1, g2) else ('WARN' if 'WARN' in (g1, g2) else 'PASS')
        grades.append(g)
        res['intents'][it] = {'pr_auc': round(ap, 3), 'base_rate': round(base, 4), 'lift': round(ap / max(base, 1e-9), 2),
                              'event_recall_at_1pct_healthy_fpr': round(rec, 3), 'test_events': len(te_ev[it]),
                              'top_features': top, 'grade': g}
    # ---------------- root-cause probe
    def root_rows(evs, lo, hi):
        idx, lab, eid = [], [], []
        for it, L in evs.items():
            for k, e in enumerate(L):
                ty = e.get('type', '')
                if ty == 'conflicting_victim':
                    continue
                m = np.where((t >= max(e['start'], lo)) & (t < min(e['failure_time'], hi)))[0]
                idx += list(m)
                lab += [it] * len(m)
                eid += [(it, k, ty)] * len(m)
        return np.array(idx, dtype=int), np.array(lab), eid

    i_tr, l_tr, _ = root_rows(tr_ev, t[0], t_cut)
    i_te, l_te, e_te = root_rows(te_ev, t_cut, t[-1] + 1)
    if len(set(l_tr)) >= 2 and len(i_te):
        clf = LogisticRegression(max_iter=800, class_weight='balanced')
        clf.fit(sc.transform(X[i_tr]), l_tr)
        pred = clf.predict(sc.transform(X[i_te]))
        votes = {}
        for e, pr in zip(e_te, pred):
            votes.setdefault(e, []).append(pr)
        corr = [(max(set(v), key=v.count) == e[0], e[2]) for e, v in votes.items()]
        acc = float(np.mean([c for c, _ in corr]))
        cod = [c for c, ty in corr if ty.endswith('_cause')]
        maj = max(np.mean([e[0] == it for e in votes]) for it in intents)
        acc_cod = float(np.mean(cod)) if len(cod) >= 5 else None
        g = 'PASS' if acc >= max(0.6, maj + 0.2) else ('WARN' if acc >= maj + 0.1 else 'FAIL')
        if acc_cod is not None and acc_cod < 0.5 and g == 'PASS':
            g = 'WARN'
        grades.append(g)
        res['root'] = {'accuracy': round(acc, 3), 'majority_baseline': round(float(maj), 3),
                       'codrift_accuracy': None if acc_cod is None else round(acc_cod, 3),
                       'test_root_events': len(corr), 'grade': g}
    else:
        res['root'] = {'note': 'not enough root-cause events'}
        grades.append('WARN')
    res['verdict'] = 'FAIL' if 'FAIL' in grades else ('WARN' if 'WARN' in grades else 'PASS')
    adv = []
    for it, r in res['intents'].items():
        if r.get('grade') == 'FAIL':
            adv.append(f'{it}: the precursors are not separable from healthy time (PR-AUC {r.get("pr_auc")}, '
                       f'event recall {r.get("event_recall_at_1pct_healthy_fpr")}). Check that its faults ramp up '
                       f'(calibration crossing fractions 0.45-0.9) and that its KPI is measured in every tick.')
        elif r.get('grade') == 'WARN':
            adv.append(f'{it}: weak signal; more data usually helps. Top features: {r.get("top_features")}')
    if res['root'].get('grade') in ('FAIL', 'WARN'):
        adv.append('root cause: the cause intents are hard to tell apart from the first ticks. Check the co-drift '
                   'lag (lag_min/lag_max) and that each cause has its own KPI signature (see top features).')
    if not res['dataset_checks'].get('healthy_slo_exceedance_low', True):
        adv.append('targets are violated in healthy time: unlabelled failures act as false alarms for every model.')
    res['advice'] = adv or ['Data looks learnable. Keep collecting.']
    _save(out_root, res)
    return res


def _save(out_root, res):
    hist = util.load_json(os.path.join(out_root, 'learnability.json'), [])
    hist.append(res)
    util.save_json(os.path.join(out_root, 'learnability.json'), hist)
    L = [f"# Learnability check ({res['time']})", '', f"**Verdict: {res['verdict']}**  "
         f"(rows {res['rows']}, segments {res.get('segments')}, horizon H = {res['horizon']} ticks)", '',
         'PASS: keep collecting. WARN: keep collecting, the signal is weak for the items below. '
         'FAIL: stop and fix the items below before collecting more. INSUFFICIENT: too early to judge.', '',
         '| intent | PR-AUC | base rate | lift | event recall @1% healthy FPR | test events | grade | top features |',
         '|---|---|---|---|---|---|---|---|']
    for it, r in res.get('intents', {}).items():
        L.append(f"| {it} | {r.get('pr_auc')} | {r.get('base_rate')} | {r.get('lift')} | "
                 f"{r.get('event_recall_at_1pct_healthy_fpr')} | {r.get('test_events')} | {r.get('grade', r.get('note'))} | "
                 f"{', '.join(r.get('top_features', []))} |")
    r = res.get('root', {})
    L += ['', f"Root-cause probe: accuracy {r.get('accuracy')} (majority baseline {r.get('majority_baseline')}), "
          f"co-drift accuracy {r.get('codrift_accuracy')}, test root events {r.get('test_root_events')}, "
          f"grade {r.get('grade', r.get('note'))}", '', '## Advice', ''] + [f'- {a}' for a in res.get('advice', [])]
    L += ['', f"Dataset checks of the merged data: {res.get('dataset_checks')}"]
    with open(os.path.join(out_root, 'LEARNABILITY_REPORT.md'), 'w') as f:
        f.write('\n'.join(L) + '\n')
