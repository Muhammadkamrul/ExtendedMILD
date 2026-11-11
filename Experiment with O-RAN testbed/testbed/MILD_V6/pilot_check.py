#!/usr/bin/env python3
"""pilot_check.py -- go / no-go check of a (pilot) dataset BEFORE a long collection (MILD on O-RAN, v2.3).

Two questions, both answered on the data alone, never on a later test set:

  1. HEADROOM. Is the dataset hard enough to separate methods at all?
     The simplest possible detector -- one fixed threshold on the failing intent's OWN KPI, placed on the
     development part so that it raises at most --fp_budget false-alarm steps per day per intent -- is
     scored on the evaluation part. If it already detects (almost) every failure with (almost) the
     largest possible lead, detection and lead time are SATURATED: every competent model reaches the same
     ceiling and no method can be shown to be better (this is what happened on the 36k v2 dataset).
     v2.4: judged on ACTIONABLE detection (alert >= 20 s before the failure):
       PASS  : < 50 %      WARN : 50-70 %      FAIL : >= 70 %

  2. PILOT COMPARISON. With the paper configuration of every neural model (no search, same threshold
     rule), seeds --seeds, on a 70/30 chronological split of the pilot: does MILD beat MLP and LSTM?
     Reported at the operating point of the main protocol and at equal false-alarm rate. This is a
     go/no-go SIGNAL for the long collection, not a paper result: the pilot segments end up in the
     development part of the full dataset, whose test part is collected later.

Usage
  mild-oran pilotcheck                     (= python pilot_check.py --state ~/intact/mild/state --out ~/intact/mild/pilot)
  python pilot_check.py --dataset <dir with dataset.csv + events_by_intent.json> --out <dir> [--headroom_only]

Output: <out>/PILOT_REPORT.md, <out>/pilot_verdict.json, <out>/headroom.csv, <out>/training/ (autotrain).
Verdict: GO (both pass), GO WITH CAUTION (headroom WARN or a tie), NO-GO (saturated or MILD clearly behind).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
MIN_COMPARE_ROWS = 12000     # v2.4: below this the pilot comparison is information only
sys.path.insert(0, HERE)

OWN = {'api': ('api_latency', 1), 'analytics': ('analytics_tput', -1), 'telemetry': ('telemetry_queue', 1)}
TICKS_PER_DAY = 1440.0


def windows(ev):
    return [(e['start'], e['end']) for L in ev.values() for e in L]


def in_any(t, wins):
    m = np.zeros(len(t), dtype=bool)
    for a, b in wins:
        m |= (t >= a) & (t <= b)
    return m


def headroom(data, ev, test_frac=0.30, fp_budget=1.0, act_ticks=5):
    """One-KPI-threshold detector per intent; threshold from the development part (FP budget per intent).
    v2.4: judged on ACTIONABLE detection (alert at least act_ticks = 20 s before the failure), because a threshold
    that fires one tick before the failure detects but does not give time to act."""
    data = data.sort_values('t').reset_index(drop=True)
    cut = int((1 - test_frac) * len(data))
    t = data['t'].values
    dev, tst = data.iloc[:cut], data.iloc[cut:]
    W = windows(ev)
    rows = []
    for intent, (col, sign) in OWN.items():
        if col not in data:
            continue
        hd = ~in_any(dev['t'].values, W)
        x = dev[col].values * sign
        days = max(len(dev) / TICKS_PER_DAY, 1e-9)
        allowed = int(fp_budget * days)
        hx = np.sort(x[hd])[::-1]
        thr = hx[min(allowed, len(hx) - 1)] if len(hx) else np.inf      # at most `allowed` healthy steps above
        xt = tst[col].values * sign
        tt = tst['t'].values
        for e in ev.get(intent, []):
            if e['start'] < tt[0] or e['failure_time'] > tt[-1]:
                continue
            m = (tt >= e['start']) & (tt <= e['failure_time']) & (xt > thr)
            first = tt[m][0] if m.any() else None
            rows.append({'intent': intent, 'type': e.get('type', ''), 'buildup': e['failure_time'] - e['start'],
                         'detected': int(first is not None),
                         'lead': (e['failure_time'] - first) if first is not None else np.nan})
        ht = ~in_any(tt, W)
        rows_fp = int(((xt > thr) & ht).sum())
        rows.append({'intent': intent, 'type': '_fp_steps_per_day_test', 'buildup': np.nan, 'detected': np.nan,
                     'lead': rows_fp / max(len(tst) / TICKS_PER_DAY, 1e-9)})
    df = pd.DataFrame(rows)
    evs = df[df['type'] != '_fp_steps_per_day_test']
    det = float(evs.groupby('intent')['detected'].mean().mean() * 100) if len(evs) else float('nan')
    lead_med = float(evs['lead'].median()) if evs['detected'].sum() else 0.0
    bu_med = float(evs['buildup'].median()) if len(evs) else float('nan')
    frac = lead_med / bu_med if bu_med else float('nan')
    evs = evs.assign(actionable=(evs['lead'].fillna(-1) >= act_ticks).astype(float))
    act = float(evs.groupby('intent')['actionable'].mean().mean() * 100) if len(evs) else float('nan')
    if act < 50:
        st = 'PASS'
    elif act < 70:
        st = 'WARN'
    else:
        st = 'FAIL'
    return st, {'one_kpi_actionable_%': round(act, 1), 'act_ticks': act_ticks,
                'one_kpi_detection_%': round(det, 1), 'one_kpi_median_lead': round(lead_med, 2),
                'median_buildup': round(bu_med, 2), 'lead_fraction_of_buildup': round(frac, 3),
                'test_fp_per_day': df[df['type'] == '_fp_steps_per_day_test'].set_index('intent')['lead'].round(2).to_dict(),
                'per_type': evs.groupby(['intent', 'type']).agg(n=('detected', 'size'), detected=('detected', 'mean'),
                                                                actionable=('actionable', 'mean'),
                                                                lead=('lead', 'mean')).round(2).reset_index().to_dict('records')}, df


def compare(out, ds, seeds, py):
    tdir = os.path.join(out, 'training')
    cmd = [py, os.path.join(HERE, 'MILD_unified_repro', 'autotrain.py'), '--data_csv', os.path.join(ds, 'dataset.csv'),
           '--events_json', os.path.join(ds, 'events_by_intent.json'), '--out', tdir, '--paper_profile', 'oran',
           '--trials', '1', '--rerank_top', '0', '--seeds', seeds, '--fp_budget', '3.0', '--fp_block', '240',
           '--test_frac', '0.30', '--opcurve_targets', '1,2,3',
           # v2.7: the paper protocol (same as `mild-oran train` defaults)
           '--mild_arch', 'v26', '--baseline_features', 'onset', '--val_frac', '0.4', '--thr_budget', '1',
           '--fp_scope', 'own', '--variants', 'tuned']
    print('[pilot] training (paper configurations, no search):', ' '.join(cmd), flush=True)
    env = dict(os.environ, TF_CPP_MIN_LOG_LEVEL='3', TF_NUM_INTRAOP_THREADS='4', TF_NUM_INTEROP_THREADS='2',
               OMP_NUM_THREADS='4')
    cmd = ['nice', '-n', '10'] + cmd       # v2.3.1: low priority, so it can run next to a live collection
    with open(os.path.join(out, 'training.log'), 'a') as lf:
        rc = subprocess.call(cmd, cwd=os.path.join(HERE, 'MILD_unified_repro'), stdout=lf, stderr=subprocess.STDOUT, env=env)
    if rc != 0:
        return None
    fr = pd.read_csv(os.path.join(tdir, 'final_test_runs.csv'))
    fr = fr[fr['variant'] == 'tuned']
    main = fr.groupby('method')[['detection', 'lead', 'fp_per_day', 'disamb']].mean().round(2)
    oc = pd.read_csv(os.path.join(tdir, 'opcurve', 'opcurve_runs.csv'))
    oc = oc[oc['variant'] == 'tuned'].groupby(['target_fp', 'method'])[['detection', 'lead_all_detected', 'disamb']].mean().round(2)
    return main, oc


def judge(main, oc):
    """MILD vs the better of MLP / LSTM, per metric, at the main operating point and at equal FP 1/2/3."""
    res, wins, losses = [], 0, 0
    for label, tab, cols in [('main protocol', main, {'detection': 'detection', 'lead': 'lead', 'disamb': 'disamb'})]:
        for m, c in cols.items():
            if 'MILD' not in tab.index:
                continue
            best = max(tab.loc[b, c] for b in ('MLP', 'LSTM') if b in tab.index)
            d = tab.loc['MILD', c] - best
            res.append({'where': label, 'metric': m, 'mild': tab.loc['MILD', c], 'best_baseline': best, 'diff': round(d, 2)})
        if 'MILD' in tab.index and 'fp_per_day' in tab.columns:      # v2.4: shown (lower is better), not counted
            low = min(tab.loc[b, 'fp_per_day'] for b in ('MLP', 'LSTM') if b in tab.index)
            res.append({'where': label, 'metric': 'fp_per_day (lower = better)', 'mild': tab.loc['MILD', 'fp_per_day'],
                        'best_baseline': low, 'diff': round(tab.loc['MILD', 'fp_per_day'] - low, 2)})
    for fp in sorted({i[0] for i in oc.index}):
        sub = oc.loc[fp]
        for c, tol in (('detection', 1.0), ('lead_all_detected', 0.2), ('disamb', 2.0)):
            best = max(sub.loc[b, c] for b in ('MLP', 'LSTM') if b in sub.index)
            d = sub.loc['MILD', c] - best
            res.append({'where': f'equal FP {fp:g}/day', 'metric': c, 'mild': sub.loc['MILD', c], 'best_baseline': best,
                        'diff': round(d, 2)})
            wins += int(d > tol)
            losses += int(d < -tol)
    return pd.DataFrame(res), wins, losses


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state', default=os.path.expanduser('~/intact/mild/state'))
    ap.add_argument('--dataset', default='', help='use this dataset folder instead of building one from --state')
    ap.add_argument('--out', default=os.path.expanduser('~/intact/mild/pilot'))
    ap.add_argument('--seeds', default='42,43,44')
    ap.add_argument('--headroom_only', action='store_true')
    ap.add_argument('--tick_s', type=float, default=0.0, help='tick length in s (default: from the state, else 4)')
    ap.add_argument('--train_python', default=sys.executable)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    ds = a.dataset
    if not ds:
        import train_now as T
        from mild_oran import util
        util.set_log(os.path.join(a.out, 'pilot_check.log'))
        cfg = T.load_cfg(a.state)
        snap = T.snapshot(a.state, a.out, refresh=True)
        _, ds, _ = T.build_dataset(snap, a.out, cfg, 0)
    data = pd.read_csv(os.path.join(ds, 'dataset.csv'))
    ev = json.load(open(os.path.join(ds, 'events_by_intent.json')))
    tick_s = a.tick_s
    if not tick_s:
        try:
            tick_s = float(json.load(open(os.path.join(a.state, 'config_used.json'))).get('tick_s', 4.0)) \
                if not a.dataset else 4.0
        except Exception:
            tick_s = 4.0
    act = max(1, int(round(20.0 / tick_s)))
    st, H, hdf = headroom(data, ev, act_ticks=act)
    hdf.to_csv(os.path.join(a.out, 'headroom.csv'), index=False)
    L = ['# Pilot check (MILD on O-RAN)', '', f'Dataset: {ds} ({len(data)} rows, '
         f'{sum(len(v) for v in ev.values())} labelled events)', '',
         f'## 1. Headroom: {st}', '',
         'One fixed threshold on the failing intent\'s own KPI (placed on the first 70 %, at most 1 false-alarm step '
         'per day per intent), scored on the last 30 %:', '',
         f"- ACTIONABLE detection (alert >= {H['act_ticks']} ticks = 20 s before the failure): "
         f"{H['one_kpi_actionable_%']} %",
         f"- any detection {H['one_kpi_detection_%']} %, median lead {H['one_kpi_median_lead']} ticks, median build-up "
         f"{H['median_buildup']} ticks (lead = {100 * H['lead_fraction_of_buildup']:.0f} % of the build-up)",
         f"- its false-alarm steps per day on the last 30 %: {H['test_fp_per_day']}", '',
         'PASS: actionable detection < 50 % (the own KPI gives no early warning, learned models can). WARN: 50-70 %. '
         'FAIL: >= 70 % (one threshold already warns early: methods cannot be separated on lead time).', '',
         '| intent | type | events | detected | actionable | mean lead |', '|---|---|---|---|---|---|']
    L += [f"| {r['intent']} | {r['type']} | {r['n']} | {r['detected']} | {r['actionable']} | {r['lead']} |"
          for r in H['per_type']]
    verdict, comp = None, None
    if not a.headroom_only:
        r = compare(a.out, ds, a.seeds, a.train_python)
        if r is None:
            L += ['', '## 2. Pilot comparison: training failed (see training.log; dataset too small?)']
            verdict = 'NO-GO'
        else:
            main_t, oc = r
            comp, wins, losses = judge(main_t, oc)
            comp.to_csv(os.path.join(a.out, 'pilot_comparison.csv'), index=False)
            L += ['', '## 2. Pilot comparison (paper configurations, seeds ' + a.seeds + ')', '',
                  'Main protocol (thresholds set on development data), means:', '', main_t.to_string(), '',
                  'Equal false-alarm rate (thresholds placed on the evaluation part, same rule for every model):', '',
                  oc.to_string(), '', 'MILD minus the better of MLP and LSTM:', '', comp.to_string(index=False), '',
                  f'At equal FP: MILD clearly better in {wins} metric/operating-point cells, clearly worse in {losses} '
                  '(tolerance 1 pp detection, 0.2 ticks lead, 2 pp DA).']
            if st == 'FAIL' or losses > wins:
                verdict = 'NO-GO'
            elif st == 'PASS' and wins >= 6 and losses == 0:
                verdict = 'GO'
            else:
                verdict = 'GO WITH CAUTION'
            if len(data) < MIN_COMPARE_ROWS and st != 'FAIL':
                # v2.4: on the v4 simulator the SAME data-generating process gives NO-GO on its first 7,200 ticks
                # and GO WITH CAUTION on its first 14,400 ticks, while the full 26,600 ticks show MILD clearly
                # ahead: below ~12,000 ticks the test part (< 40 failures) is too small and MILD (the largest
                # model) is data-starved. Only the headroom decides then; the comparison is shown for information.
                L += ['', f'Only {len(data)} rows (< {MIN_COMPARE_ROWS}): the comparison above is INFORMATION ONLY '
                      '(too few test failures, MILD is data-starved). The verdict uses the headroom alone; repeat '
                      f'this check at >= {MIN_COMPARE_ROWS} rows.']
                verdict = {'PASS': 'GO (headroom; comparison too small)',
                           'WARN': 'GO WITH CAUTION (headroom; comparison too small)'}[st]
    else:
        verdict = {'PASS': 'GO (headroom only)', 'WARN': 'GO WITH CAUTION (headroom only)', 'FAIL': 'NO-GO'}[st]
    L += ['', f'## Verdict: {verdict}', '',
          '- GO: continue the collection to the target with the SAME settings.',
          '- GO WITH CAUTION: continue, but expect parity on some metrics; send PILOT_REPORT.md for review.',
          '- NO-GO: do not spend 40 h on this scenario; send PILOT_REPORT.md and pilot_comparison.csv.',
          '', 'This is a design check on the pilot only. The paper results come from the full dataset\'s held-out test '
          'part, which the pilot never sees.']
    open(os.path.join(a.out, 'PILOT_REPORT.md'), 'w').write('\n'.join(L) + '\n')
    json.dump({'verdict': verdict, 'headroom': st, 'headroom_detail': {k: v for k, v in H.items() if k != 'per_type'}},
              open(os.path.join(a.out, 'pilot_verdict.json'), 'w'), indent=1, default=str)
    print('\n'.join(L))
    print(f'\nPILOT VERDICT: {verdict}   (report: {os.path.join(a.out, "PILOT_REPORT.md")})')


if __name__ == '__main__':
    main()
