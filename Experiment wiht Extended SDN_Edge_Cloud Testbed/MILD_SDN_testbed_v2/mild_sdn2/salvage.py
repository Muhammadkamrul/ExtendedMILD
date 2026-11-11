"""
salvage.py -- find and exclude the "API jam" intervals in segments collected
with the first version of the collector (code_version 1).

What went wrong in version 1
  The fault knob of each service was set through the service's own HTTP worker
  pool. When a strong API fault saturated that pool, the reset request queued
  behind slow requests and timed out, so the fault stayed active after the
  episode. The API latency histogram then received no completed requests
  (api_latency = NaN) and the health probes failed (sri close to 0). Because a
  missing KPI cannot satisfy the failure predicate, the adaptive severity loop
  read these episodes as "failure not observed" and raised the API severity
  further, which made the jam permanent.

What this module does
  A tick is "bad" when api_latency is missing or sri < 5. Runs of bad ticks
  (gaps of up to 2 good ticks are bridged) are classified as a jam when they
  cannot be explained by an injected fault, i.e. when they reach into quiet
  time (no injection window or recovery tail open) for at least 3 ticks or
  last at least 40 ticks. Each jam run is widened by 10 ticks on both sides
  and written to seg_XXXX/exclude.json. quality.merge() removes these rows and
  every labelled event that overlaps them. A segment with fewer than
  min_segment_ticks clean ticks left is marked 'excluded' and is skipped.

Nothing is deleted. The original rows stay on disk, the original status is kept
in meta.json ('status_before_salvage'), and the decision is reproducible.

Usage (also called automatically by pipeline.py and train_now.py):
  python -m mild_sdn2.salvage --state ~/mild_sdn2_state            # write exclude.json files
  python -m mild_sdn2.salvage --state ~/mild_sdn2_state --dry_run  # only report
"""
from __future__ import annotations

import argparse
import json
import os

import numpy as np
import pandas as pd

from . import util

BRIDGE = 2
QUIET_MIN = 3
LONG_RUN = 40
DILATE = 10


def detect(seg_dir, recovery_max=10):
    rows = pd.read_csv(os.path.join(seg_dir, 'rows.csv'))
    meta = util.load_json(os.path.join(seg_dir, 'meta.json'), {})
    n = int(meta.get('ticks', len(rows)))
    rows = rows[rows['t'] < n]
    plan = util.load_json(os.path.join(seg_dir, 'plan.json'), {}).get('plan', [])
    t = rows['t'].astype(int).values
    bad = (rows['api_latency'].isna() | (rows['sri'].fillna(100) < 5)).values
    rmax = int(meta.get('recovery_max', recovery_max)) + 2
    quiet = np.array([not any(e['start'] <= x < e['end'] + rmax for e in plan) for x in t], dtype=bool)
    # bridge short good gaps inside bad runs
    b = bad.copy()
    i = 0
    while i < len(b):
        if not b[i]:
            j = i
            while j < len(b) and not b[j]:
                j += 1
            if 0 < i and j < len(b) and (j - i) <= BRIDGE:
                b[i:j] = True
            i = j
        else:
            i += 1
    runs, i = [], 0
    while i < len(b):
        if b[i]:
            j = i
            while j < len(b) and b[j]:
                j += 1
            runs.append((i, j))
            i = j
        else:
            i += 1
    jam = np.zeros(len(b), dtype=bool)
    jam_runs = []
    for s, e in runs:
        nq = int(quiet[s:e].sum())
        if nq >= QUIET_MIN or (e - s) >= LONG_RUN:
            jam_runs.append({'from': int(t[s]), 'to': int(t[e - 1]) + 1, 'len': e - s, 'quiet_ticks': nq})
            jam[max(0, s - DILATE):min(len(b), e + DILATE)] = True
    intervals, i = [], 0
    while i < len(jam):
        if jam[i]:
            j = i
            while j < len(jam) and jam[j]:
                j += 1
            intervals.append([int(t[i]), int(t[j - 1]) + 1])
            i = j
        else:
            i += 1
    return {'ticks': n, 'bad_ticks': int(bad.sum()), 'jam_runs': jam_runs, 'intervals': intervals,
            'excluded_ticks': int(jam.sum()), 'clean_ticks': int(n - jam.sum()),
            'api_nan_frac': float(rows['api_latency'].isna().mean()) if len(rows) else 0.0}


def salvage_state(state_dir, min_segment_ticks=200, dry_run=False, force=False):
    """Apply detect() to every finished version-1 segment. Returns a summary list."""
    from .collector import SegmentStore
    store = SegmentStore(state_dir)
    out = []
    for i in store.list():
        d = store.path(i)
        m = store.meta(i)
        status = m.get('status_before_salvage', m.get('status'))
        if status not in ('complete', 'truncated'):
            continue
        if m.get('code_version', 1) >= 2 and not force:
            continue                     # collected with the fixed collector (watchdog active)
        if m.get('salvage') and not force:
            out.append({'segment': i, **m['salvage'], 'status': m['status']})
            continue
        r = detect(d)
        new_status = 'excluded' if r['clean_ticks'] < min_segment_ticks else status
        rec = {'segment': i, 'status': new_status, **{k: v for k, v in r.items() if k != 'jam_runs'},
               'n_jam_runs': len(r['jam_runs'])}
        out.append(rec)
        if dry_run:
            continue
        util.save_json(os.path.join(d, 'exclude.json'), {'method': 'salvage v1 (see mild_sdn2/salvage.py)',
                                                         'intervals': r['intervals'], 'jam_runs': r['jam_runs']})
        m['status_before_salvage'] = status
        m['status'] = new_status
        m['excluded_ticks'] = 0 if new_status == 'excluded' else r['excluded_ticks']
        m['salvage'] = {k: rec[k] for k in ('excluded_ticks', 'clean_ticks', 'bad_ticks', 'n_jam_runs', 'api_nan_frac')}
        util.save_json(os.path.join(d, 'meta.json'), m)
    return out


def write_report(summary, path):
    df = pd.DataFrame(summary)
    L = ['# Salvage report (API-jam intervals of collector version 1)', '']
    if df.empty:
        L.append('No version-1 segments found. Nothing to salvage.')
    else:
        kept = df[df['status'] != 'excluded']
        L += [f"version-1 segments: {len(df)}; kept: {len(kept)}; excluded completely: {int((df['status'] == 'excluded').sum())}",
              f"usable ticks kept: {int(kept['clean_ticks'].sum())} of {int(df['ticks'].sum())}", '',
              '| segment | status | ticks | API NaN % | jam runs | excluded ticks | clean ticks |', '|---|---|---|---|---|---|---|']
        for _, r in df.iterrows():
            L.append(f"| {int(r['segment'])} | {r['status']} | {int(r['ticks'])} | {100 * r['api_nan_frac']:.0f} | "
                     f"{int(r['n_jam_runs'])} | {int(r['excluded_ticks'])} | {int(r['clean_ticks'])} |")
    with open(path, 'w') as f:
        f.write('\n'.join(L) + '\n')
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state', required=True)
    ap.add_argument('--min_segment_ticks', type=int, default=200)
    ap.add_argument('--dry_run', action='store_true')
    a = ap.parse_args()
    s = salvage_state(a.state, a.min_segment_ticks, a.dry_run)
    p = write_report(s, os.path.join(a.state, 'SALVAGE_REPORT.md') if not a.dry_run else '/dev/stdout')
    if not a.dry_run:
        print(open(p).read())


if __name__ == '__main__':
    main()
