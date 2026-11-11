#!/usr/bin/env python3
"""
check_collection.py -- read-only health check of a running (or finished) SDN collection.

  /home/windows/ryu39/bin/python check_collection.py --state ~/mild_sdn2_state_v2

Prints one line per segment and an overall verdict. It never writes into the
state folder, so it is safe to run while pipeline.py is collecting. A copy of the
table is written to --out (default: ./collection_health.csv).

What is checked per segment
  api_nan %     share of ticks where the API latency could not be measured.
                Expected after the fix: about 0 %. Above 5 % means trouble.
  sri<5 %       share of ticks where almost all health probes failed.
                Expected: 0 % outside heavy network faults.
  tick s        mean wall-clock length of a tick (truth.csv, fixed collector only).
                Expected: about 4.0 s. Above 4.5 s means the machine is overloaded.
  observed %    injected episodes whose failure was observed (all types).
  api_events_lost  API events with a latency gap longer than 3 ticks. They are
                removed when the dataset is built. Expected after v2.3: 0.
  watchdog      the segment was stopped early because the testbed stopped responding.
"""
import argparse
import glob
import json
import os
import time

import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--state', default=os.path.expanduser('~/mild_sdn2_state_v2'))
    ap.add_argument('--out', default='collection_health.csv')
    ap.add_argument('--target', type=int, default=20000)
    a = ap.parse_args()
    rows, warn = [], []
    for d in sorted(glob.glob(os.path.join(a.state, 'segments', 'seg_*'))):
        try:
            m = json.load(open(os.path.join(d, 'meta.json')))
            r = pd.read_csv(os.path.join(d, 'rows.csv'))
        except Exception:
            continue
        tr_p = os.path.join(d, 'truth.csv')
        tr = pd.read_csv(tr_p) if os.path.exists(tr_p) and os.path.getsize(tr_p) > 0 else pd.DataFrame()
        ev = [json.loads(x) for x in open(os.path.join(d, 'events.jsonl'))] if os.path.exists(os.path.join(d, 'events.jsonl')) else []
        obs = sum(e['failure_observed'] for e in ev) / len(ev) if ev else float('nan')
        api_obs = [e['failure_observed'] for e in ev if e['intent'] == 'api']
        # API events that will be dropped at merge time (latency gap longer than 3 ticks inside the event)
        nan = r['api_latency'].isna().values if len(r) else []
        long_gap = set()
        run = 0
        for j, x in enumerate(nan):
            run = run + 1 if x else 0
            if run > 3:
                long_gap.update(int(r['t'].iloc[q]) for q in range(j - run + 1, j + 1))
        api_ev = [e for e in ev if e['intent'] == 'api']
        api_gap = sum(1 for e in api_ev if any(e['start'] <= q < e['end'] for q in long_gap))
        rec = {'segment': os.path.basename(d), 'status': m.get('status'), 'version': m.get('code_version', 1),
               'rows': len(r), 'api_nan_pct': round(100 * r['api_latency'].isna().mean(), 1) if len(r) else None,
               'sri_lt5_pct': round(100 * (r['sri'] < 5).mean(), 1) if len(r) else None,
               'tick_s': round(tr['tick_wall_s'].mean(), 2) if 'tick_wall_s' in tr else None,
               'events': len(ev), 'observed_pct': round(100 * obs, 0) if ev else None,
               'api_observed_pct': round(100 * sum(api_obs) / len(api_obs), 0) if api_obs else None,
               'api_events_lost': f'{api_gap}/{len(api_ev)}',
               'api_scale': round(m.get('scales', {}).get('api', float('nan')), 2),
               'watchdog': 'YES' if m.get('watchdog') else ''}
        rows.append(rec)
        tag = rec['segment']
        if api_ev and api_gap / len(api_ev) > 0.1:
            warn.append(f'{tag}: {api_gap} of {len(api_ev)} API events have a latency gap > 3 ticks and will be dropped')
        elif rec['api_nan_pct'] is not None and rec['api_nan_pct'] > 5:
            warn.append(f'{tag}: API latency missing in {rec["api_nan_pct"]} % of ticks')
        if rec['tick_s'] and rec['tick_s'] > 4.5:
            warn.append(f'{tag}: ticks take {rec["tick_s"]} s (expected ~4.0 s); the machine may be overloaded')
        if rec['watchdog']:
            warn.append(f'{tag}: watchdog restarted the testbed (see pipeline.log)')
    if not rows:
        print('no segments yet')
        return
    df = pd.DataFrame(rows)
    pd.set_option('display.width', 200)
    print(df.to_string(index=False))
    df.to_csv(a.out, index=False)
    done = df[df['status'].isin(['complete', 'truncated'])]
    total = int(df['rows'].sum())
    print(f'\nrows collected: {total} (finished segments: {int(done["rows"].sum())})')
    log = os.path.join(a.state, 'pipeline.log')
    if os.path.exists(log):
        mt = os.path.getmtime(log)
        print(f'pipeline.log last written {int(time.time() - mt)} s ago' + ('  <-- collection may be stopped' if time.time() - mt > 180 else ''))
    ts = df['tick_s'].dropna()
    if len(ts):
        rate = 60.0 / ts.iloc[-1]
        left = max(0, a.target - total)
        print(f'rate ~{rate:.1f} rows/min -> {left} rows to {a.target}: ~{left / rate / 60:.1f} h')
    print('\nVERDICT: ' + ('OK, collection looks healthy' if not warn else 'CHECK THESE:'))
    for w in warn:
        print('  - ' + w)


if __name__ == '__main__':
    main()
