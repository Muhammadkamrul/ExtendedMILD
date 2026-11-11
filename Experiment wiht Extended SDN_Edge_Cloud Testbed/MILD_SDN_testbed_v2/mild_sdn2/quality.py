"""
quality.py -- merge segments into one MILD dataset and check that it is valid
and sufficient for learning.

Output (in <state>/dataset/):
  dataset.csv                 t, 8 KPIs, cpu_delta, sri_delta, n_missing, segment
  events_by_intent.json       labelled events (MILD format, plus failure_observed etc.)
  events_unobserved.json      injected episodes whose failure was not observed (strict policy)
  quality_report.json / .md   validity and sufficiency checks
  traffic_summary.json        measured traffic rates and payload sizes (for the paper)

Missing KPI values:
  * gaps of at most `interp_max_gap` ticks are linearly interpolated between
    the real neighbouring measurements (counted and reported);
  * rows inside longer gaps are removed, together with any event that
    overlaps them. No value is ever replaced by an assumed healthy constant.
"""
from __future__ import annotations

import glob
import json
import os

import numpy as np
import pandas as pd

from . import util
from .collector import SegmentStore
from .kpi import KPI_NAMES


def merge(state_dir, cfg):
    store = SegmentStore(state_dir)
    segs = store.finished()
    if not segs:
        raise RuntimeError('no finished segments to merge')
    out_dir = os.path.join(state_dir, 'dataset')
    os.makedirs(out_dir, exist_ok=True)
    frames, events, unobs, off = [], {'api': [], 'analytics': [], 'telemetry': []}, [], 0
    stats = {'interpolated_values': 0, 'dropped_rows': 0, 'dropped_events_gap': 0,
             'excluded_rows_jam': 0, 'dropped_events_jam': 0, 'segments_excluded': 0}
    stats['segments_excluded'] = sum(1 for i in store.list() if store.meta(i).get('status') == 'excluded')
    for i in segs:
        d = store.path(i)
        df = pd.read_csv(os.path.join(d, 'rows.csv'))
        n_ticks = store.meta(i).get('ticks', len(df))
        df = df[df['t'] < n_ticks].copy()
        isna = df[KPI_NAMES].isna()
        stats['interpolated_values'] += int(isna.sum().sum())
        long_gap = np.zeros(len(df), dtype=bool)
        for c in KPI_NAMES:
            m = isna[c].values
            run = 0
            for j in range(len(m)):
                run = run + 1 if m[j] else 0
                if run > cfg['interp_max_gap']:
                    long_gap[j - run + 1:j + 1] = True
        df[KPI_NAMES] = df[KPI_NAMES].interpolate(limit=cfg['interp_max_gap'], limit_direction='both')
        # intervals excluded by salvage.py (testbed jam in collector version 1)
        exc = util.load_json(os.path.join(d, 'exclude.json'), {}).get('intervals', [])
        jam = np.zeros(len(df), dtype=bool)
        tv = df['t'].values
        for a0, b0 in exc:
            jam |= (tv >= a0) & (tv < b0)
        stats['excluded_rows_jam'] += int((jam & ~long_gap).sum())
        long_gap = long_gap | jam
        evp = os.path.join(d, 'events.jsonl')
        seg_ev = [json.loads(x) for x in open(evp)] if os.path.exists(evp) else []
        # An event that overlaps removed rows is dropped, and so are the remaining rows of
        # its window. Otherwise its failure ticks would stay in the data without a label
        # and count as unexplained (healthy-time) failures. Repeat until stable, because
        # removing one window can cut into an overlapping event (cause and victim).
        removed = long_gap.copy()
        keep = [True] * len(seg_ev)
        why = [None] * len(seg_ev)
        changed = True
        while changed:
            changed = False
            for j, e in enumerate(seg_ev):
                if not keep[j]:
                    continue
                w = (tv >= e['start']) & (tv < e['end'])
                if (w & removed).any():
                    keep[j] = False
                    why[j] = 'jam' if (w & jam).any() else 'gap'
                    removed |= w
                    changed = True
        stats['dropped_events_jam'] += sum(1 for x in why if x == 'jam')
        stats['dropped_events_gap'] += sum(1 for x in why if x == 'gap')
        stats['dropped_rows_event_windows'] = stats.get('dropped_rows_event_windows', 0) + int((removed & ~long_gap).sum())
        df = df[~removed]
        stats['dropped_rows'] += int(long_gap.sum())
        stats['interpolated_values'] -= int(isna[removed].sum().sum()) if removed.any() else 0
        df['segment'] = f'seg_{i:04d}'
        df['t'] = df['t'] + off
        frames.append(df)
        for j, e in enumerate(seg_ev):
            if not keep[j]:
                continue
            for k in ('start', 'end', 'failure_time', 'inject_end'):
                if k in e:
                    e[k] += off
            e['segment'] = i
            if e['failure_observed'] or cfg['label_policy'] == 'lenient':
                events[e['intent']].append({k: e[k] for k in ('start', 'failure_time', 'end', 'type', 'failure_observed',
                                                              'crossing_frac', 'segment')})
            else:
                unobs.append(e)
        off += n_ticks
    data = pd.concat(frames, ignore_index=True)
    data = data.dropna(subset=KPI_NAMES)
    for k in events:
        events[k].sort(key=lambda x: x['start'])
    cols = ['t', 'cpu_pct', 'ram_pct', 'storage_pct', 'snet', 'sri', 'api_latency', 'analytics_tput', 'telemetry_queue',
            'cpu_delta', 'sri_delta', 'n_missing', 'segment']
    data[cols].to_csv(os.path.join(out_dir, 'dataset.csv'), index=False)
    util.save_json(os.path.join(out_dir, 'events_by_intent.json'), events)
    util.save_json(os.path.join(out_dir, 'events_unobserved.json'), unobs)
    return data, events, unobs, stats, out_dir


def traffic_summary(state_dir):
    recs = {}
    for p in glob.glob(os.path.join(state_dir, 'runs', '*', 'traffic', '*.jsonl')):
        name = os.path.basename(p).replace('.jsonl', '')
        rows = [json.loads(x) for x in open(p) if x.strip()]
        if not rows:
            continue
        df = pd.DataFrame(rows)
        recs.setdefault(name, []).append(df)
    out = {}
    for name, dfs in recs.items():
        df = pd.concat(dfs, ignore_index=True)
        num = df.select_dtypes('number').drop(columns=['ts', 'window_s'], errors='ignore')
        out[name] = {c: {'mean': float(num[c].mean()), 'p5': float(num[c].quantile(.05)),
                         'p95': float(num[c].quantile(.95))} for c in num.columns}
    return out


def assess(state_dir, cfg, targets):
    data, events, unobs, mstats, out_dir = merge(state_dir, cfg)
    rows = []
    for k, L in events.items():
        for e in L:
            rows.append({'intent': k, **e, 'buildup': e['failure_time'] - e['start']})
    ev = pd.DataFrame(rows)
    allwin = [(e['start'], e['end']) for L in events.values() for e in L] + [(e['start'], e['end']) for e in unobs]
    t = data['t'].values
    inwin = np.zeros(len(t), dtype=bool)
    for s, e in allwin:
        inwin |= (t >= s) & (t < e)
    out = data[~inwin]
    slo_out = {'api': float((out['api_latency'] >= targets['api_p95_ms']).mean()),
               'analytics': float((out['analytics_tput'] <= targets['analytics_tput_min']).mean()),
               'telemetry': float((out['telemetry_queue'] >= targets['telemetry_q_max']).mean())}
    n_obs = ev.groupby('intent').size().to_dict() if len(ev) else {}
    n_codrift = int((ev['type'] == 'conflicting_victim').sum()) if len(ev) else 0
    inj_by_type = {}
    for k, L in events.items():
        for e in L:
            inj_by_type.setdefault(e['type'], [0, 0])
            inj_by_type[e['type']][0] += 1
            inj_by_type[e['type']][1] += int(e.get('failure_observed', True))
    for e in unobs:
        inj_by_type.setdefault(e['type'], [0, 0])
        inj_by_type[e['type']][0] += 1
    checks = {
        'enough_ticks': len(data) >= cfg['min_total_ticks'],
        'enough_events_per_intent': all(n_obs.get(k, 0) >= cfg['min_events_per_intent'] for k in ('api', 'analytics', 'telemetry')),
        'enough_codrift_pairs': n_codrift >= cfg['min_codrift_pairs'],
        'observed_fraction_ok': all(v[1] / max(v[0], 1) >= 0.7 for v in inj_by_type.values()),
        'healthy_slo_exceedance_low': all(v <= 0.02 for v in slo_out.values()),
        'buildup_ok': bool(len(ev) and ev['buildup'].median() >= 4),
        'kpis_vary': bool(all(data[c].std() > 1e-6 for c in KPI_NAMES)),
    }
    rep = {'rows': int(len(data)), 'segments': int(data['segment'].nunique()), 'merge': mstats,
           'events_per_intent': n_obs, 'codrift_pairs': n_codrift,
           'events_by_type_injected_observed': inj_by_type, 'unobserved_events': len(unobs),
           'buildup_quantiles': ev['buildup'].quantile([.1, .5, .9]).to_dict() if len(ev) else {},
           'slo_exceedance_outside_windows': slo_out,
           'kpi_summary': data[KPI_NAMES].describe().T.round(3).to_dict(orient='index'),
           'checks': checks, 'sufficient': bool(checks['enough_ticks'] and checks['enough_events_per_intent']
                                               and checks['enough_codrift_pairs']),
           'valid': bool(checks['observed_fraction_ok'] and checks['healthy_slo_exceedance_low']
                         and checks['buildup_ok'] and checks['kpis_vary']),
           'targets': targets, 'traffic': traffic_summary(state_dir)}
    util.save_json(os.path.join(out_dir, 'quality_report.json'), rep)
    util.save_json(os.path.join(out_dir, 'traffic_summary.json'), rep['traffic'])
    L = ['# Dataset quality report', '', f"rows: {rep['rows']}  segments: {rep['segments']}",
         f"events per intent (labelled, observed): {n_obs}", f"co-drift pairs: {n_codrift}",
         f"unobserved injected episodes (not labelled): {len(unobs)}", '', '## Checks', '']
    L += [f'- {k}: {"PASS" if v else "FAIL"}' for k, v in checks.items()]
    L += ['', f"**valid = {rep['valid']}, sufficient = {rep['sufficient']}**", '',
          '## Injected vs observed failures by type', '']
    L += [f'- {k}: injected {v[0]}, observed {v[1]} ({100 * v[1] / max(v[0], 1):.1f} %)' for k, v in sorted(inj_by_type.items())]
    L += ['', f"SLO exceedance outside event windows: {slo_out}", f"build-up quantiles (ticks): {rep['buildup_quantiles']}",
          f"merge statistics: {mstats}"]
    with open(os.path.join(out_dir, 'quality_report.md'), 'w') as f:
        f.write('\n'.join(L))
    return rep, out_dir
