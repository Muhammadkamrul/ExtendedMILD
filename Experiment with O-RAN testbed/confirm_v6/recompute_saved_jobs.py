#!/usr/bin/env python3
"""Re-evaluate saved test predictions, never train or read saved metric outcomes."""
import argparse
import glob
import hashlib
import json
import os
from pathlib import Path
import numpy as np
import pandas as pd
from mild_repro import core
from confirm_v6 import write_descriptive_report

NAMES = {'mild':'MILD', 'mlp':'MLP', 'lstm':'LSTM', 'mlp_matched':'MLP (MILD settings)',
         'lr':'LR-OvR', 'wkpi':'WKPI-Tuned', 'dist':'Dist-Target'}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def reconstruct(folder, cfg, method, events):
    metrics = []
    provenance = []
    for tsfile in sorted(glob.glob(str(folder / 'folds/fold*_timeseries.csv'))):
        if '_val_' in tsfile:
            continue
        settings_file = tsfile.replace('_timeseries.csv', '_metrics.json')
        settings = json.loads(Path(settings_file).read_text())['params']
        ts = pd.read_csv(tsfile, float_precision='round_trip')
        t = ts.t.to_numpy()
        if not len(t) or np.any(np.diff(t) <= 0):
            raise ValueError(f'Empty or unordered time series: {tsfile}')
        ids = sorted(settings)
        ema = {k: ts['ema_'+k].to_numpy() for k in ids}
        alerts = {k: t[ema[k] >= settings[k]['tau']] for k in ids}
        gate = ts[['gate_'+k for k in ids]].to_numpy() if method == 'mild' else None
        if cfg.get('protocol_v6'):
            ev = {k:[dict(e) for e in es if e['start'] >= t[0] and e['end'] <= t[-1]]
                  for k,es in events.items()}
        else:
            ev = core.filter_events(events, int(t[0]), int(t[-1]))
        scope = cfg.get('fp_scope', 'own' if cfg.get('protocol_v6') else 'any')
        m, _, _ = core.evaluate(alerts, ev, t, ema, gate_probs=gate, fold_t0=t[0], fp_scope=scope)
        metrics.append(m)
        provenance.append({'predictions':tsfile, 'sha256':digest(tsfile),
                           'threshold_file':settings_file, 'threshold_file_sha256':digest(settings_file),
                           'fp_scope':scope})
    if not metrics:
        raise ValueError(f'No saved fold predictions in {folder}')
    agg = core.aggregate_folds(metrics, sorted(metrics[0]['per_intent']))
    return {**agg['macro'], **{'det_'+k:v['detection_mean'] for k,v in agg['per_intent'].items()}}, provenance


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--run', required=True, action='append', help='confirmation folder; repeat to add tuned classics')
    ap.add_argument('--events_json', required=True)
    ap.add_argument('--out', required=True, help='separate output directory, not the source run')
    a = ap.parse_args()
    runs = [Path(r).expanduser().resolve() for r in a.run]
    out = Path(a.out).expanduser().resolve()
    if out in runs:
        raise ValueError('Use a separate --out directory to preserve original results')
    events = json.loads(Path(a.events_json).expanduser().read_text())
    eh = digest(Path(a.events_json).expanduser())
    rows, manifest, seen = [], [], set()
    record = {}
    for run in runs:
        rp = run/'PREREG_RECORD.json'
        if rp.exists():
            rec = json.loads(rp.read_text())
            if rec.get('events_sha256') and rec['events_sha256'] != eh:
                raise ValueError('Event-label hash differs from execution record: '+str(run))
            if record and any(record.get(k) != rec.get(k) for k in ('dataset_sha256','dev_rows','test_rows')):
                raise ValueError('Input runs use different test datasets/partitions')
            record = rec
        cp = run/'classic_selected.json'
        selected = json.loads(cp.read_text()) if cp.exists() else {}
        for specfile in sorted(run.glob('jobs/*/*/spec.json')):
            job = specfile.parent
            if not (job/'DONE').exists():
                continue
            spec = json.loads(specfile.read_text()); cfg = spec['cfg']; method = spec['method']
            targets = set()
            for role in spec.get('roles', []):
                p = role.split(':')
                if p[0] == 'confirm':
                    names = ['lr','wkpi','dist'] if p[1]=='classic' else [p[1]]
                    for name in names:
                        targets.add((name, int(p[2].replace('seed',''))))
                elif p[0] == 'classic_test':
                    for name, choice in selected.items():
                        if int(choice['label_horizon']) == int(p[1][1:]):
                            targets.add((name, int(cfg.get('seed',42))))
            for name, seed in sorted(targets):
                label = NAMES[name]
                key = (label,seed)
                if key in seen:
                    raise ValueError(f'Duplicate method/seed {key}; evaluate confirm_A and confirm_B separately')
                folder = job/name if method=='classic' else job
                vals, files = reconstruct(folder, cfg, method, events)
                seen.add(key); rows.append(dict(method=label,seed=seed,**vals))
                manifest.append(dict(method=label,seed=seed,spec=str(specfile),spec_sha256=digest(specfile),folds=files))
    if not rows:
        raise ValueError('No completed confirmation/classic_test jobs found; extract jobs.zip under the run first')
    out.mkdir(parents=True,exist_ok=True)
    fr = pd.DataFrame(rows).sort_values(['method','seed'])
    fr.to_csv(out/'final_test_runs.csv',index=False)
    (out/'RECOMPUTE_MANIFEST.json').write_text(json.dumps(dict(events_sha256=eh,runs=list(map(str,runs)),jobs=manifest),indent=2))
    write_descriptive_report(fr,str(out),record)
    print('Recomputed from saved predictions:',out)

if __name__=='__main__':
    main()
