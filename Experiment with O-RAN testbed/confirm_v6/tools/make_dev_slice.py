#!/usr/bin/env python3
"""Cut the V6 development snapshot from a full dataset folder: rows [0, dev_rows) and only the events (and
look-alikes) that end inside them. Usage: make_dev_slice.py FULL_DATASET_DIR OUT_DIR [dev_rows=10080]"""
import json, os, sys
import pandas as pd
src, out = sys.argv[1], sys.argv[2]
n = int(sys.argv[3]) if len(sys.argv) > 3 else 10080
os.makedirs(out, exist_ok=True)
d = pd.read_csv(os.path.join(src, 'dataset.csv'))
if len(d) < n:
    sys.exit(f'only {len(d)} rows, need {n}')
d = d.iloc[:n]
d.to_csv(os.path.join(out, 'dataset.csv'), index=False)
t_last = d.t.iloc[-1]
e = json.load(open(os.path.join(src, 'events_by_intent.json')))
e = {k: [x for x in v if x['end'] <= t_last] for k, v in e.items()}
json.dump(e, open(os.path.join(out, 'events_by_intent.json'), 'w'), indent=1)
mp = os.path.join(src, 'mimics.json')
if os.path.exists(mp):
    m = [x for x in json.load(open(mp)) if x.get('end', 0) <= t_last]
    json.dump(m, open(os.path.join(out, 'mimics.json'), 'w'), indent=1)
print(f'development slice: {n} rows (t {d.t.iloc[0]}..{t_last}), events', {k: len(v) for k, v in e.items()})
