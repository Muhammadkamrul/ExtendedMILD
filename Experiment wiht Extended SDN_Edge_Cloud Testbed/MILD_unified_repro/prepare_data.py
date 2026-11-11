#!/usr/bin/env python3
"""
prepare_data.py -- put the three datasets where run_all.py expects them and
verify that they are the datasets used in the paper.

  data/statistical/   generated here with the paper's seed and settings
  data/microservices/ copied from your dataset_and_events_Microservice.zip (or folder)
  data/sdn/           copied from your dataset_and_events_RYU.zip (or folder)

Usage (from the package folder):
  python prepare_data.py --microservices /path/dataset_and_events_Microservice.zip \
                         --sdn /path/dataset_and_events_RYU.zip

Every step is skipped if its output already exists and verifies correctly.
A JSON summary is written to data/data_manifest.json.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mild_repro import paper_config as P  # noqa: E402


def sha256(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def find_pair(folder):
    for r, _, files in os.walk(folder):
        if 'dataset.csv' in files and 'events_by_intent.json' in files and '__MACOSX' not in r:
            return os.path.join(r, 'dataset.csv'), os.path.join(r, 'events_by_intent.json')
    return None, None


def stage_emulation(name, src, data_root):
    dst = os.path.join(data_root, os.path.dirname(P.DATASETS[name]['csv']))
    os.makedirs(dst, exist_ok=True)
    out_csv, out_ev = os.path.join(data_root, P.DATASETS[name]['csv']), os.path.join(data_root, P.DATASETS[name]['events'])
    if not (os.path.exists(out_csv) and os.path.exists(out_ev)):
        if not src:
            return {'status': 'MISSING', 'hint': f'pass --{name} <zip or folder>'}
        tmp = None
        if src.endswith('.zip'):
            tmp = tempfile.mkdtemp()
            with zipfile.ZipFile(src) as z:
                z.extractall(tmp)
            folder = tmp
        else:
            folder = src
        c, e = find_pair(folder)
        if c is None:
            return {'status': 'ERROR', 'hint': f'no dataset.csv + events_by_intent.json inside {src}'}
        shutil.copy(c, out_csv)
        shutil.copy(e, out_ev)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    hc, he = sha256(out_csv), sha256(out_ev)
    ok = hc == P.DATASETS[name]['sha256_csv'] and he == P.DATASETS[name]['sha256_events']
    return {'status': 'OK (identical to the paper dataset)' if ok else 'WARNING: hash differs from the paper dataset',
            'sha256_csv': hc, 'sha256_events': he}


def stage_statistical(data_root):
    import numpy as np
    import pandas as pd
    from mild_repro import generate_stat_benchmark as G
    g = P.DATASETS['statistical']['generator']
    out_csv = os.path.join(data_root, P.DATASETS['statistical']['csv'])
    out_ev = os.path.join(data_root, P.DATASETS['statistical']['events'])
    os.makedirs(os.path.dirname(out_csv), exist_ok=True)
    if not (os.path.exists(out_csv) and os.path.exists(out_ev)):
        df, ev = G.generate(g['minutes'], g['seed'], p_nonlinear=g['p_nonlinear'], p_codrift=g['p_codrift'],
                            p_independent=g['p_independent'])
        df.to_csv(out_csv, index=False)
        with open(out_ev, 'w') as f:
            json.dump(ev, f, indent=2)
    df = pd.read_csv(out_csv)
    ev = json.load(open(out_ev))
    counts = {k: len(v) for k, v in ev.items()}
    roots = [x['type'] for v in ev.values() for x in v if x['type'] != 'conflicting_victim']
    nonlin = sum(1 for t in roots if t in ('independent_telem_nonlinear', 'independent_api_xor', 'independent_analytics_xor'))
    cod = sum(1 for t in roots if t.endswith('_cause'))
    expected = {'telemetry': 56, 'api': 53, 'analytics': 51}
    ok = len(df) == 200000 and counts == expected and abs(df['cpu_pct'].iloc[0] - 50.48754732760709) < 1e-9
    return {'status': 'OK (matches the paper benchmark: 200000 rows, 160 labelled windows)' if ok
            else 'WARNING: generated benchmark differs from the paper benchmark (numpy version?)',
            'rows': len(df), 'events_per_intent': counts, 'root_events': len(roots),
            'realised_mix': {'nonlinear': nonlin, 'codrift': cod, 'independent': len(roots) - nonlin - cod},
            'sha256_csv': sha256(out_csv)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data_root', default=HERE)
    ap.add_argument('--microservices', default=None, help='zip or folder with dataset.csv + events_by_intent.json')
    ap.add_argument('--sdn', default=None, help='zip or folder with dataset.csv + events_by_intent.json')
    a = ap.parse_args()
    man = {'statistical': stage_statistical(a.data_root),
           'microservices': stage_emulation('microservices', a.microservices, a.data_root),
           'sdn': stage_emulation('sdn', a.sdn, a.data_root)}
    os.makedirs(os.path.join(a.data_root, 'data'), exist_ok=True)
    with open(os.path.join(a.data_root, 'data', 'data_manifest.json'), 'w') as f:
        json.dump(man, f, indent=2)
    for k, v in man.items():
        print(f'[{k}] {v["status"]}')
    print('Manifest written to data/data_manifest.json')


if __name__ == '__main__':
    main()
