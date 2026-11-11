"""
report.py -- build every manuscript table from the finished jobs, compare each
cell with the submitted paper, run the significance tests, and write a
plain-language REPORT.md.

Outputs (in <out>/tables, <out>/significance, <out>/REPORT.md):
  <ds>_mild_per_intent.csv        Tables V / VII / IX
  <ds>_comparison.csv             Tables VI / VIII / X  (+ extended metrics)
  cross_dataset.csv               Table XI
  <ds>_ablation.csv               Tables XII-XIV (notebook configuration)
  <ds>_ablation_consistent.csv    ablation re-run around the main configuration
  <ds>_sensitivity.csv            Tables XV-XVII
  multihorizon.csv                Sec. V-E3 lead-time ladder
  paper_vs_reproduced_ALL.csv     every compared cell with its status
  significance/<ds>_significance.csv
"""
from __future__ import annotations

import glob
import json
import os

import numpy as np
import pandas as pd

from . import paper_config as P
from . import stats as S

METRICS = ['detection', 'lead', 'fp_per_day', 'disamb']
METRIC_LABEL = {'detection': 'Detection (%)', 'lead': 'Lead Time (min)', 'fp_per_day': 'FP/Day',
                'disamb': 'Disamb. Acc. (%)'}
CLASSIC_NAME = {'wkpi': 'WKPI-Tuned', 'dist': 'Dist-Target', 'lr': 'LR-OvR'}


def status(paper, rep, metric):
    if paper is None or rep is None or (isinstance(rep, float) and np.isnan(rep)):
        return 'MISSING'
    d = abs(rep - paper)
    if d <= P.TOLERANCE['identical_abs']:
        return 'IDENTICAL'
    if d <= P.TOLERANCE['close_abs'].get(metric, 1.0) or (paper != 0 and d / abs(paper) <= P.TOLERANCE['close_rel']):
        return 'CLOSE'
    return 'DIFFERENT'


def _load_results(root):
    res = {}
    for p in glob.glob(os.path.join(root, 'runs', '*', '*', '*', 'result.json')):
        with open(p) as f:
            r = json.load(f)
        r['_dir'] = os.path.dirname(p)
        res[r['id']] = r
    return res


def _by_role(jobs, results):
    m = {}
    for j in jobs:
        if j['id'] in results:
            for role in j['roles']:
                m[(j['dataset'], role)] = results[j['id']]
    return m


def _macro(agg):
    return [agg['macro']['detection'], agg['macro']['lead'], agg['macro']['fp_per_day'], agg['macro']['disamb']]


def _fold_summ(folds):
    out = []
    for f in folds:
        pi = f['per_intent']
        out.append({'detection': 100 * np.mean([v['detection_rate'] for v in pi.values()]),
                    'lead': np.mean([v['avg_lead_time'] for v in pi.values()]),
                    'fp_per_day': f['overall']['fp_per_day'],
                    'disamb': 100 * f['overall']['disamb_accuracy']})
    return out


def _events(run_dir):
    ev = [pd.read_csv(p) for p in sorted(glob.glob(os.path.join(run_dir, 'folds', 'fold*_events.csv')))]
    dy = [pd.read_csv(p) for p in sorted(glob.glob(os.path.join(run_dir, 'folds', 'fold*_days.csv')))]
    return (pd.concat(ev, ignore_index=True) if ev else pd.DataFrame(),
            pd.concat(dy, ignore_index=True) if dy else pd.DataFrame())


def method_entries(ds, roles, suffix=''):
    """Map method name -> (aggregate, folds, run_dir) for the main comparison."""
    out = {}
    for name, role in [('MILD', 'main:MILD'), ('MLP', 'main:MLP'), ('LSTM', 'main:LSTM')]:
        r = roles.get((ds, role + suffix))
        if r:
            out[name] = (r['aggregate'], r['folds'], r['_dir'])
    r = roles.get((ds, 'main:CLASSIC' + suffix))
    if r:
        for k, nm in CLASSIC_NAME.items():
            out[nm] = (r['methods'][k]['aggregate'], r['methods'][k]['folds'], os.path.join(r['_dir'], k))
    return out


def build(root, jobs, datasets):
    results = _load_results(root)
    roles = _by_role(jobs, results)
    tdir, sdir = os.path.join(root, 'tables'), os.path.join(root, 'significance')
    os.makedirs(tdir, exist_ok=True)
    os.makedirs(sdir, exist_ok=True)
    allcells, cross, notes, sig_summ = [], [], [], {}

    def cell(table, ds, row, metric, paper, rep):
        stt = status(paper, rep, metric)
        allcells.append({'table': table, 'dataset': ds, 'row': row, 'metric': metric, 'paper': paper,
                         'reproduced': None if rep is None else round(float(rep), 4),
                         'abs_diff': None if (rep is None or paper is None) else round(float(rep - paper), 4),
                         'rel_diff_%': None if (rep is None or not paper) else round(100 * float(rep - paper) / abs(paper), 2),
                         'status': stt})
        return stt

    for ds in datasets:
        ents = method_entries(ds, roles)
        # ---------------- per-intent MILD table ----------------
        if 'MILD' in ents:
            agg = ents['MILD'][0]
            pp = P.PAPER_MILD_PER_INTENT[ds]
            rows = []
            for intent in ['analytics', 'api', 'telemetry']:
                a = agg['per_intent'][intent]
                rows.append({'metric': 'Failure Detection Rate (%)', 'detail': intent,
                             'reproduced': f"{a['detection_mean']:.2f} ± {a['detection_std']:.2f}",
                             'paper': f"{pp[intent][0]:.2f} ± {pp[intent][1]:.2f}",
                             'status': cell('MILD per intent', ds, intent, 'detection', pp[intent][0], a['detection_mean'])})
            for intent in ['analytics', 'api', 'telemetry']:
                a = agg['per_intent'][intent]
                rows.append({'metric': 'Avg. Lead Time (min)', 'detail': intent,
                             'reproduced': f"{a['lead_mean']:.2f} ± {a['lead_std']:.2f}",
                             'paper': f"{pp[intent][2]:.2f} ± {pp[intent][3]:.2f}",
                             'status': cell('MILD per intent', ds, intent, 'lead', pp[intent][2], a['lead_mean'])})
            o = agg['overall']
            rows.append({'metric': 'FP Rate per Day', 'detail': 'Overall',
                         'reproduced': f"{o['fp_per_day_mean']:.2f} ± {o['fp_per_day_std']:.2f}",
                         'paper': f"{pp['fp'][0]:.2f} ± {pp['fp'][1]:.2f}",
                         'status': cell('MILD per intent', ds, 'overall', 'fp_per_day', pp['fp'][0], o['fp_per_day_mean'])})
            rows.append({'metric': 'Disambiguation Accuracy (%)', 'detail': 'Root Cause Acc.',
                         'reproduced': f"{o['disamb_accuracy_mean']:.2f} ± {o['disamb_accuracy_std']:.2f}",
                         'paper': f"{pp['da'][0]:.2f} ± {pp['da'][1]:.2f}",
                         'status': cell('MILD per intent', ds, 'overall', 'disamb', pp['da'][0], o['disamb_accuracy_mean'])})
            pd.DataFrame(rows).to_csv(os.path.join(tdir, f'{ds}_mild_per_intent.csv'), index=False)
            m = _macro(agg)
            cross.append({'dataset': P.DATASETS[ds]['label'], **{METRIC_LABEL[k]: round(v, 2) for k, v in zip(METRICS, m)},
                          'folds_evaluated': agg['n_folds_evaluated']})
            for k, v in zip(METRICS, m):
                cell('Cross-dataset', ds, 'MILD', k, P.PAPER_COMPARISON[ds]['MILD'][METRICS.index(k)], v)

        # ---------------- comparison table ----------------
        rows = []
        for name in P.METHODS:
            if name not in ents:
                continue
            agg = ents[name][0]
            rep = _macro(agg)
            pap = P.PAPER_COMPARISON[ds][name]
            row = {'Model': name}
            for k, r_, p_ in zip(METRICS, rep, pap):
                row[METRIC_LABEL[k]] = round(r_, 2)
                row[f'paper {METRIC_LABEL[k]}'] = p_
                row[f'status {k}'] = cell('Comparison', ds, name, k, p_, r_)
            nb = P.NOTEBOOK_DIFFERENCES.get((ds, name, 'fp_per_day'))
            if nb is not None:
                row['note'] = f'notebook printed FP/day={nb} (manuscript shows {pap[2]})'
            row['FP episodes/day (extended)'] = round(agg['macro']['fp_episodes_per_day'], 2)
            row['Disamb. incl. nonlinear roots (%) (extended)'] = round(agg['macro']['disamb_all_roots'], 2)
            row['missed events (FN, all test blocks)'] = int(sum(f['per_intent'][i]['fn'] for f in ents[name][1]
                                                                for i in f['per_intent']))
            row['folds_evaluated'] = agg['n_folds_evaluated']
            rows.append(row)
        if rows:
            pd.DataFrame(rows).to_csv(os.path.join(tdir, f'{ds}_comparison.csv'), index=False)

        # ---------------- significance ----------------
        if 'MILD' in ents:
            ev_m, dy_m = _events(ents['MILD'][2])
            fm = _fold_summ(ents['MILD'][1])
            srows = []
            for name in P.METHODS[1:]:
                if name not in ents:
                    continue
                ev_b, dy_b = _events(ents[name][2])
                if ev_b.empty:
                    continue
                for r in S.compare(ev_m, ev_b, dy_m, dy_b, fm, _fold_summ(ents[name][1])):
                    r['baseline_name'] = name
                    srows.append(r)
            sdf = pd.DataFrame(srows)
            if not sdf.empty:
                sdf['p_holm'] = np.nan
                for met, g in sdf.groupby('metric'):
                    sdf.loc[g.index, 'p_holm'] = S.holm(g['p'].values)
                better = np.where(sdf['higher_is_better'], sdf['diff'] > 0, sdf['diff'] < 0)
                sdf['MILD_better'] = better
                sdf['significant_0.05'] = sdf['p_holm'] < 0.05
                sdf['verdict'] = np.where(sdf['p_holm'].isna(), 'not testable (too few units)',
                                  np.where(sdf['significant_0.05'] & sdf['MILD_better'], 'MILD significantly better',
                                  np.where(sdf['significant_0.05'] & ~sdf['MILD_better'], 'baseline significantly better',
                                           'no significant difference')))
                cols = ['baseline_name', 'metric', 'n_units', 'mild', 'baseline', 'diff', 'ci_low', 'ci_high', 'test',
                        'p', 'p_holm', 'verdict', 'detail']
                sdf[cols].round(6).to_csv(os.path.join(sdir, f'{ds}_significance.csv'), index=False)
                sig_summ[ds] = sdf

        # ---------------- ablation ----------------
        for grp, fname in [('ablation', 'ablation'), ('ablation_consistent', 'ablation_consistent')]:
            rows = []
            for v in P.ABLATION_VARIANTS:
                r = roles.get((ds, f'{grp}:{v}'))
                if not r:
                    continue
                rep = _macro(r['aggregate'])
                pap = P.PAPER_ABLATION[ds][v]
                row = {'Variant': v}
                for k, r_, p_ in zip(METRICS, rep, pap):
                    row[METRIC_LABEL[k]] = round(r_, 2)
                    row[f'paper {METRIC_LABEL[k]}'] = p_
                    if grp == 'ablation':
                        row[f'status {k}'] = cell('Ablation', ds, v, k, p_, r_)
                rows.append(row)
            if rows:
                pd.DataFrame(rows).to_csv(os.path.join(tdir, f'{ds}_{fname}.csv'), index=False)

        # ---------------- sensitivity ----------------
        rows = []
        for param, vals in P.SENSITIVITY[ds].items():
            for val in vals:
                r = roles.get((ds, f'sensitivity:{param}={val}'))
                if not r:
                    continue
                rep = _macro(r['aggregate'])
                pap = P.PAPER_SENSITIVITY[ds].get((param, val))
                row = {'Param': 'alpha' if param == 'distill_alpha' else 'w_c', 'Value': val}
                for i, (k, r_) in enumerate(zip(METRICS, rep)):
                    row[METRIC_LABEL[k]] = round(r_, 2)
                    if pap:
                        row[f'paper {METRIC_LABEL[k]}'] = pap[i]
                        row[f'status {k}'] = cell('Sensitivity', ds, f'{param}={val}', k, pap[i], r_)
                rows.append(row)
        if rows:
            pd.DataFrame(rows).to_csv(os.path.join(tdir, f'{ds}_sensitivity.csv'), index=False)

        # ---------------- seed robustness (optional group) ----------------
        seed_rows = []
        for (d2, role), r in roles.items():
            if d2 == ds and role.startswith('main:') and '@seed' in role and 'aggregate' in r:
                nm, sd = role[5:].split('@seed')
                seed_rows.append({'method': nm, 'seed': int(sd), **dict(zip(METRICS, _macro(r['aggregate'])))})
        for name, role in [('MILD', 'main:MILD'), ('MLP', 'main:MLP'), ('LSTM', 'main:LSTM')]:
            r = roles.get((ds, role))
            if r and seed_rows:
                seed_rows.append({'method': name, 'seed': 42, **dict(zip(METRICS, _macro(r['aggregate'])))})
        if seed_rows:
            sr = pd.DataFrame(seed_rows).sort_values(['method', 'seed'])
            sr.to_csv(os.path.join(tdir, f'{ds}_seed_runs.csv'), index=False)
            sr.groupby('method')[METRICS].agg(['mean', 'std']).round(2).to_csv(os.path.join(tdir, f'{ds}_seed_summary.csv'))

    # ---------------- cross dataset & multi-horizon ----------------
    if cross:
        pd.DataFrame(cross).to_csv(os.path.join(tdir, 'cross_dataset.csv'), index=False)
    mh = []
    for ds in datasets:
        r = roles.get((ds, 'opintel:multihorizon'))
        if r:
            for row in r.get('ttf', []):
                paper = P.PAPER_MULTIHORIZON.get(ds, {}).get(int(row['H']))
                row = dict(row, dataset=ds, paper_first_alert_before_failure_min=paper)
                if paper is not None and row.get('est_TTF_min') == row.get('est_TTF_min'):
                    row['status'] = cell('Multi-horizon', ds, f"H={row['H']}", 'lead', paper, float(row['est_TTF_min']))
                mh.append(row)
    if mh:
        pd.DataFrame(mh).to_csv(os.path.join(tdir, 'multihorizon.csv'), index=False)

    cells = pd.DataFrame(allcells)
    if not cells.empty:
        cells.to_csv(os.path.join(tdir, 'paper_vs_reproduced_ALL.csv'), index=False)
    _write_report(root, cells, sig_summ, datasets, roles)
    return cells


def _write_report(root, cells, sig, datasets, roles):
    L = ['# MILD reproduction report', '',
         'This file is generated automatically by `run_all.py`. It compares every reproduced number '
         'with the submitted manuscript and summarises the paired significance tests.', '',
         'Status labels: **IDENTICAL** = equal at two decimals; **CLOSE** = within the tolerance in '
         '`paper_config.TOLERANCE` (2 pp / 2 min / 1 FP per day, or 5 %); **DIFFERENT** = outside it.', '',
         'Exact equality is not expected across TensorFlow versions or between CPU and GPU, because '
         'floating-point reduction order changes the trained weights. The classical baselines '
         '(WKPI-Tuned, Dist-Target, LR-OvR) do not use TensorFlow and should be IDENTICAL.', '']
    if not cells.empty:
        L += ['## 1. Paper vs reproduced', '']
        summ = cells.groupby(['dataset', 'table', 'status']).size().unstack(fill_value=0)
        L += [summ.to_markdown() if hasattr(summ, 'to_markdown') else summ.to_string(), '']
        worst = cells[cells['status'] == 'DIFFERENT'].copy()
        if not worst.empty:
            worst['absd'] = worst['abs_diff'].abs()
            L += ['Largest differences (up to 15):', '']
            L += [worst.sort_values('absd', ascending=False).head(15)[
                ['dataset', 'table', 'row', 'metric', 'paper', 'reproduced', 'abs_diff']].to_string(index=False), '']
        L += ['Interpretation: differences confined to the neural models (MILD, MLP, LSTM) and of a few points '
              'are run-to-run numerical variation; check `tables/<dataset>_seed_summary.csv` (group `seeds`) '
              'to see the natural spread. A difference in the classical baselines would indicate a data '
              'mismatch, because those methods are deterministic.', '']
    L += ['## 2. Is MILD significantly better than each baseline?', '',
          'Event-level paired tests pooled over all evaluated test blocks; Holm-corrected over the five baselines.', '']
    for ds, sdf in sig.items():
        L += [f'### {P.DATASETS[ds]["label"]}', '']
        core_m = sdf[~sdf['metric'].str.startswith('fold_')]
        piv = core_m.pivot_table(index='baseline_name', columns='metric', values='verdict', aggfunc='first')
        L += [piv.to_string(), '']
        for met in ['fp_steps_per_day', 'disambiguation_%', 'lead_time_min (common detections)', 'detection_%']:
            g = core_m[core_m['metric'] == met]
            if g.empty:
                continue
            wins = (g['verdict'] == 'MILD significantly better').sum()
            loss = (g['verdict'] == 'baseline significantly better').sum()
            L.append(f'- {met}: MILD significantly better than {wins}/{len(g)} baselines; '
                     f'significantly worse than {loss}/{len(g)}.')
        L.append('')
    L += ['## 3. Notes that matter for the response letter', '',
          '- Emulation datasets: "3-fold" blocked CV evaluates **two** test blocks; the first block has no '
          'training history and is skipped (same as the original scripts).',
          '- FP/day counts alert *time steps* outside every annotated window. `FP episodes/day` (extended '
          'column) counts contiguous false-alert runs instead.',
          '- The paper disambiguation accuracy covers `independent*` and `*_cause` events. The extended column '
          '`Disamb. incl. nonlinear roots` also includes the `nonlinear_*` root events of the emulation datasets.',
          '- The classical baselines train on labels with H=120 for every dataset and tune thresholds on '
          'training scores, exactly as the original script does.', '']
    with open(os.path.join(root, 'REPORT.md'), 'w') as f:
        f.write('\n'.join(L))
