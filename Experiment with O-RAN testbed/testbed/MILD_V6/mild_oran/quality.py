"""quality.py -- merge segments into one MILD dataset and check that it is valid and sufficient.

What is merged ("pieces", in time order):
  * every whole segment with status complete / truncated (current attempt);
  * the good windows of BAD segments (salvage, collector.salvage_windows);
  * the same from archived attempts (attempts/attempt_NN/segments) when qa.include_archived is true and the
    attempt was collected with the same tick length and code version (labels use each segment's own targets).

Output (in <state>/dataset/):
  dataset.csv                 t, 8 KPIs, cpu_delta, sri_delta, n_missing, segment, RAN context columns
  events_by_intent.json       labelled events (MILD format, plus failure_observed etc.)
  mimics.json                 benign mimic windows (not labels; train_now uses them to explain false alarms)
  events_unobserved.json      injected episodes whose failure was not observed (strict policy)
  quality_report.json / .md   validity and sufficiency checks
  traffic_summary.json        measured traffic and telemetry volumes (for the paper's traffic table)

Missing KPI values: gaps of at most interp_max_gap ticks are interpolated between real
neighbouring measurements; rows inside longer gaps are removed together with every
event that overlaps them (and the rest of that event's window). No value is ever
replaced by an assumed healthy constant.
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

from . import util
from .collector import SegmentStore
from .kpi import CTX_NAMES, KPI_NAMES

OUT_COLS = ['t', 'cpu_pct', 'ram_pct', 'storage_pct', 'snet', 'sri', 'api_latency', 'analytics_tput', 'telemetry_queue',
            'cpu_delta', 'sri_delta', 'n_missing', 'segment'] + CTX_NAMES + ['ram_raw_pct']


def _sources(state_dir, cfg):
    """[(label, SegmentStore)] for the current attempt and, if enabled, the archived ones."""
    out = []
    root = os.path.join(state_dir, 'attempts')
    if cfg.get('qa', {}).get('include_archived', True) and os.path.isdir(root):
        for d in sorted(os.listdir(root)):
            if d.startswith('attempt_') and os.path.isdir(os.path.join(root, d, 'segments')):
                out.append((d.replace('attempt_', 'a'), SegmentStore(os.path.join(root, d))))
    out.append(('', SegmentStore(state_dir)))
    return out


def merge(state_dir, cfg, out_dir=None):
    pieces = []
    for label, store in _sources(state_dir, cfg):
        for i, a, b in store.pieces():
            m = store.meta(i)
            if label and (int(m.get('code_version', 1)) != 2 or float(m.get('tick_s', cfg['tick_s'])) != float(cfg['tick_s'])):
                continue
            pieces.append((label, store, i, a, b))
    if not pieces:
        raise RuntimeError('no finished segments to merge')
    out_dir = out_dir or os.path.join(state_dir, 'dataset')
    os.makedirs(out_dir, exist_ok=True)
    frames, events, unobs, off = [], {'api': [], 'analytics': [], 'telemetry': []}, [], 0
    mimics = []          # v2.2: benign mimic windows (not labels; used only to explain false alarms)
    cur = SegmentStore(state_dir)
    stats = {'interpolated_values': 0, 'dropped_rows': 0, 'dropped_events_gap': 0, 'pieces': len(pieces),
             'pieces_salvaged': sum(1 for p in pieces if p[1].meta(p[2]).get('status') == 'bad'),
             'pieces_archived': sum(1 for p in pieces if p[0]),
             'segments_discarded': sum(1 for i in cur.list() if cur.meta(i).get('status') == 'discarded'),
             'segments_ran_down': sum(1 for i in cur.list() if cur.meta(i).get('ran_down')),
             'segments_watchdog': sum(1 for i in cur.list() if cur.meta(i).get('watchdog'))}
    for label, store, i, a, b in pieces:
        d = store.path(i)
        df = pd.read_csv(os.path.join(d, 'rows.csv'))
        df = df[(df['t'] >= a) & (df['t'] < b)].copy()
        if not len(df):
            continue
        for c in CTX_NAMES:
            if c not in df:
                df[c] = np.nan
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
        tv = df['t'].values
        evp = os.path.join(d, 'events.jsonl')
        seg_ev = [json.loads(x) for x in open(evp)] if os.path.exists(evp) else []
        seg_ev = [e for e in seg_ev if e['start'] >= a and e['end'] <= b]
        removed = long_gap.copy()
        keep = [True] * len(seg_ev)
        changed = True
        while changed:
            changed = False
            for j, e in enumerate(seg_ev):
                if not keep[j]:
                    continue
                w = (tv >= e['start']) & (tv < e['end'])
                if (w & removed).any():
                    keep[j] = False
                    removed |= w
                    changed = True
        stats['dropped_events_gap'] += sum(1 for k in keep if not k)
        stats['dropped_rows_event_windows'] = stats.get('dropped_rows_event_windows', 0) + int((removed & ~long_gap).sum())
        stats['interpolated_values'] -= int(isna[removed].sum().sum()) if removed.any() else 0
        df = df[~removed]
        stats['dropped_rows'] += int(long_gap.sum())
        name = (f'{label}_' if label else '') + f'seg_{i:04d}' + \
            (f'_w{a}' if store.meta(i).get('status') == 'bad' else '')            # a salvaged window
        df['segment'] = name
        df['t'] = df['t'] - a + off
        frames.append(df)
        for j, e in enumerate(seg_ev):
            if not keep[j]:
                continue
            for k in ('start', 'end', 'failure_time', 'inject_end'):
                if k in e:
                    e[k] += off - a
            e['segment'] = name
            if e['failure_observed'] or cfg['label_policy'] == 'lenient':
                events[e['intent']].append({k: e[k] for k in ('start', 'failure_time', 'end', 'type', 'failure_observed',
                                                              'crossing_frac', 'segment')})
            else:
                unobs.append(e)
        try:
            plan = util.load_json(os.path.join(d, 'plan.json'), {}).get('plan', []) or []
        except Exception:
            plan = []
        for p in plan:
            if p.get('intent') == 'mimic' and p['start'] >= a and p['end'] <= b:
                mimics.append({'type': p['type'], 'start': int(p['start'] + off - a), 'end': int(p['end'] + off - a),
                               'segment': name})
        off += b - a
    data = pd.concat(frames, ignore_index=True)
    data = data.dropna(subset=KPI_NAMES)
    # v2.2: ram_pct without the memory history of the processes (see kpi_norm.py); raw value kept as context
    from .kpi_norm import ram_excess_series
    data['ram_raw_pct'] = data['ram_pct']
    data['ram_pct'] = ram_excess_series(data['ram_raw_pct'].values, data['segment'].values)
    for k in events:
        events[k].sort(key=lambda x: x['start'])
    cols = list(OUT_COLS)
    if cfg.get('scenario') in ('v4', 'v5'):
        # v2.4 scenario v4: the cell load (E2SM-KPM RRU.PrbTotDl, %) is a model input of EVERY method, because the
        # tenant demand moves the xApps' CPU too (load_pct; core.feature_base adds it when the column exists)
        data['load_pct'] = data['ran_prb_dl_pct'].ffill().bfill().fillna(0.0)
        cols.append('load_pct')
    data[cols].to_csv(os.path.join(out_dir, 'dataset.csv'), index=False)
    util.save_json(os.path.join(out_dir, 'events_by_intent.json'), events)
    util.save_json(os.path.join(out_dir, 'events_unobserved.json'), unobs)
    util.save_json(os.path.join(out_dir, 'mimics.json'), sorted(mimics, key=lambda x: x['start']))
    return data, events, unobs, stats, out_dir


def traffic_summary(state_dir, data):
    """Measured volumes for the paper's traffic table (means over all clean rows and finished segments)."""
    out = {}
    for c, unit in (('ran_dl_mbps', 'Mbit/s (E2SM-KPM DRB.UEThpDl, cell)'), ('ran_ul_mbps', 'Mbit/s'),
                    ('ran_prb_dl_pct', '% (RRU.PrbTotDl)'), ('ue_sink_mbps', 'Mbit/s received by all UEs'),
                    ('e2_ind_hz', 'E2 indications/s at xKPM'), ('xkpm_rx_s', 'KPM records/s'),
                    ('e2_extra_subs', 'extra KPM subscriptions'), ('rc_ctrl_s', 'E2SM-RC controls/s'),
                    ('rc_ack_ms', 'ms (RC control round trip)'), ('e2_db_mb', 'MB (FlexRIC SQLite)'),
                    ('radio_speed', 'x real time'), ('ue_rtt_ms', 'ms'), ('ue_http_ok_pct', '%')):
        if c in data and data[c].notna().any():
            s = data[c].dropna()
            out[c] = {'unit': unit, 'mean': float(s.mean()), 'p5': float(s.quantile(.05)), 'p95': float(s.quantile(.95))}
    store = SegmentStore(state_dir)
    tot = {}
    secs = 0.0
    for i in store.finished():
        m = store.meta(i)
        cs = m.get('component_stats') or {}
        if not cs:
            continue
        secs += m.get('ticks', 0) * float(m.get('tick_s', 4.0))
        for role, st in cs.items():
            for k, v in (st or {}).items():
                if isinstance(v, (int, float)) and k in ('policy', 'control', 'shed', 'rules', 'conflicts', 'api_sent',
                                                         'dash_sent', 'sent', 'row', 'cell', 'sched', 'dash', 'ctrl_sent',
                                                         'rx_cell', 'rx_ue', 'rx_replay', 'delivered', 'rows_dropped',
                                                         'rc_sent', 'rc_ack', 'rc_fail', 'rc_timeout', 'rc_shadow',
                                                         'sub_ok', 'sub_fail', 'unsub', 'gw_exits'):
                    tot[f'{role}.{k}'] = tot.get(f'{role}.{k}', 0) + v
    if secs:
        out['per_second'] = {k: round(v / secs, 4) for k, v in sorted(tot.items())}
        out['seconds_counted'] = secs
    return out


def assess(state_dir, cfg, targets, out_dir=None):
    data, events, unobs, mstats, out_dir = merge(state_dir, cfg, out_dir)
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
    qa = [util.load_json(os.path.join(st.path(i), 'qa.json'), {}) for _, st in _sources(state_dir, cfg)
          for i in st.finished()]
    mim = sum((q.get('health') or {}).get('mimics', 0) for q in qa)
    mimx = sum((q.get('health') or {}).get('mimic_crossings', 0) for q in qa)
    xtalk = sum((q.get('health') or {}).get('crosstalk_crossings', 0) for q in qa)
    checks = {
        'enough_ticks': len(data) >= cfg['min_total_ticks'],
        'enough_events_per_intent': all(n_obs.get(k, 0) >= cfg['min_events_per_intent'] for k in ('api', 'analytics', 'telemetry')),
        'enough_codrift_pairs': n_codrift >= cfg['min_codrift_pairs'],
        'observed_fraction_ok': bool(inj_by_type) and all(v[1] / max(v[0], 1) >= 0.7 for v in inj_by_type.values()),
        'healthy_slo_exceedance_low': all(v <= 0.02 for v in slo_out.values()),
        'buildup_ok': bool(len(ev) and ev['buildup'].median() >= 4),
        'kpis_vary': bool(all(data[c].std() > 1e-6 for c in KPI_NAMES)),
        'mimics_mostly_benign': (mimx <= 0.2 * mim) if mim else True,
    }
    rep = {'rows': int(len(data)), 'segments': int(data['segment'].nunique()), 'merge': mstats,
           'events_per_intent': n_obs, 'codrift_pairs': n_codrift,
           'events_by_type_injected_observed': inj_by_type, 'unobserved_events': len(unobs),
           'buildup_quantiles': {str(k): v for k, v in ev['buildup'].quantile([.1, .5, .9]).to_dict().items()} if len(ev) else {},
           'slo_exceedance_outside_windows': slo_out, 'mimics': mim, 'mimic_crossings': mimx,
           'crosstalk_crossings': xtalk,
           'kpi_summary': data[KPI_NAMES].describe().T.round(3).to_dict(orient='index'),
           'checks': checks, 'sufficient': bool(checks['enough_ticks'] and checks['enough_events_per_intent']
                                               and checks['enough_codrift_pairs']),
           'valid': bool(checks['observed_fraction_ok'] and checks['healthy_slo_exceedance_low']
                         and checks['buildup_ok'] and checks['kpis_vary']),
           'targets': targets, 'traffic': traffic_summary(state_dir, data)}
    util.save_json(os.path.join(out_dir, 'quality_report.json'), rep)
    util.save_json(os.path.join(out_dir, 'traffic_summary.json'), rep['traffic'])
    L = ['# Dataset quality report (MILD on O-RAN)', '', f"rows: {rep['rows']}  pieces: {rep['segments']} "
         f"(salvaged windows: {mstats.get('pieces_salvaged')}, from archived attempts: {mstats.get('pieces_archived')})",
         f"events per intent (labelled, observed): {n_obs}", f"co-drift pairs: {n_codrift}",
         f"unobserved injected episodes (not labelled): {len(unobs)}",
         f"benign mimics: {mim}, of which crossed a target: {mimx}; unlabelled cross-intent crossings: {xtalk}",
         '', '## Checks', '']
    L += [f'- {k}: {"PASS" if v else "FAIL"}' for k, v in checks.items()]
    L += ['', f"**valid = {rep['valid']}, sufficient = {rep['sufficient']}**", '',
          '## Injected vs observed failures by type', '']
    L += [f'- {k}: injected {v[0]}, observed {v[1]} ({100 * v[1] / max(v[0], 1):.1f} %)' for k, v in sorted(inj_by_type.items())]
    L += ['', f"SLO exceedance outside event windows: {slo_out}", f"build-up quantiles (ticks): {rep['buildup_quantiles']}",
          f"merge statistics: {mstats}", '', '## Measured traffic and telemetry volumes', '',
          '```', json.dumps(rep['traffic'], indent=1), '```']
    with open(os.path.join(out_dir, 'quality_report.md'), 'w') as f:
        f.write('\n'.join(L))
    return rep, out_dir
