#!/usr/bin/env python3
"""Freeze the configurations selected by v6_devcompare into the two files confirm_v6.py reads.
Usage: freeze_configs.py SEARCH_DIR OUT_DIR
  -> OUT_DIR/selected_configurations.json  (MILD, MLP, LSTM: each method's own selected configuration)
  -> OUT_DIR/mlp_matched.json              (the MLP with MILD's selected generic training settings)"""
import json, os, sys
src, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)
sel = {m: json.load(open(os.path.join(src, f'{m}_selected.json'))) for m in ('mild', 'mlp', 'lstm')}
json.dump(sel, open(os.path.join(out, 'selected_configurations.json'), 'w'), indent=2)
GENERIC = ['alarm_loss', 'batch', 'distill_alpha', 'epochs', 'features', 'fp_budget', 'fp_scope', 'horizon',
           'patience', 'protocol_v6', 'temp', 'thr_budget_fixed', 'thr_rule', 'threshold_mode', 'val_frac']
mlp = dict(sel['mlp']['cfg'])
for k in GENERIC:
    if k in sel['mild']['cfg']:
        mlp[k] = sel['mild']['cfg'][k]
json.dump({'trial': 'MILD generic settings', 'cfg': mlp}, open(os.path.join(out, 'mlp_matched.json'), 'w'), indent=2)
for m in sel:
    print(m, 'trial', sel[m]['trial'], {k: sel[m]['cfg'].get(k) for k in ('alarm_loss', 'horizon', 'batch', 'epochs', 'threshold_mode', 'gate_mode')})
print('matched MLP:', {k: mlp.get(k) for k in GENERIC})
