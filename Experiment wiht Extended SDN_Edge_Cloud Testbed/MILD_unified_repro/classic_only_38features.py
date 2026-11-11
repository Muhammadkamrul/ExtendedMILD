#!/usr/bin/env python3
"""Classic-only 38-feature sensitivity for the October mild_repro.zip.
Place beside run_all.py. Fixed paper classic folds/budgets/H120 are preserved;
--horizon mild optionally aligns the label horizon too. No neural model training.
Uses core.engineer independently on train/test blocks like MILD's feature builder.
WKPI/distance normalization uses TRAINING bounds, fixing legacy test-wide rescaling.
Threshold calibration remains on training scores as in the old classic runner;
this does NOT make its full training/calibration protocol identical to MILD's.
Distance defaults to all 38 coordinates (same feature vector), changing the old
intent-specific distance definition. Its raw distance is then identical across
intent heads; this is a generic-distance sensitivity, not an attribution model.
Use --distance_features original for the original per-intent raw-coordinate subsets.
No hyperparameter search is performed. Preserve original results separately.
"""
import argparse, hashlib, json, sys
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from typing import Dict
from mild_repro import core, paper_config as P
from mild_repro.runners import _tests, _classic_eval, _log
CLASSIC_METHODS=['wkpi','dist','lr']
def normalize_pair(tr,te):
    lo,hi=float(np.min(tr)),float(np.max(tr))
    if hi-lo<=1e-9:return np.zeros_like(tr),np.zeros_like(te)
    return (tr-lo)/(hi-lo+1e-9),(te-lo)/(hi-lo+1e-9)
def run_classic(df, events_by_intent, intent_ids, cfg, outdir_by_method: Dict[str, str]):
    core.set_global_determinism(cfg['seed'])
    _, feats = core.engineer(df.iloc[:1])
    Hlab = cfg.get('label_horizon', 120)   # the original script hard-codes 120 for every dataset
    N = len(df)
    tests = _tests(N, cfg)
    results = {m: [] for m in CLASSIC_METHODS}
    feat_sel = {'telemetry': ['sri', 'snet', 'ram_pct', 'cpu_pct', 'telemetry_queue'],
                'api': ['api_latency', 'cpu_pct', 'sri'],
                'analytics': ['snet', 'analytics_tput', 'cpu_pct']}
    lower_is_bad = {'sri', 'snet', 'analytics_tput'}
    lower_is_bad |= {f for f in feats if any(f.startswith(k+'_mean_') for k in ['sri','snet','analytics_tput'])}
    if cfg.get('distance_features','all') == 'all':
        feat_sel = {k:list(feats) for k in intent_ids}

    for i, (s, e) in enumerate(tests):
        fold_no = i + 1
        _log(f"Fold {fold_no}/{len(tests)}: test [{s}:{e})")
        df_test = df.iloc[s:e].reset_index(drop=True)
        df_train = df.iloc[:s].reset_index(drop=True)
        if len(df_train) < 1000:
            _log("  [skip] not enough train history before this test block.")
            continue
        tr_ev = core.filter_events(events_by_intent, df_train['t'].min(), df_train['t'].max())
        te_ev = core.filter_events(events_by_intent, df_test['t'].min(), df_test['t'].max())
        Ttr, Tte = df_train['t'].values, df_test['t'].values
        train_features, _ = core.engineer(df_train)
        test_features, _ = core.engineer(df_test)
        scaler = StandardScaler().fit(train_features.values)
        Xtr = scaler.transform(train_features.values)
        Xte = scaler.transform(test_features.values)
        Xtr_df, Xte_df = pd.DataFrame(Xtr, columns=feats), pd.DataFrame(Xte, columns=feats)
        Ytr = core.build_binary_labels(df_train, tr_ev, intent_ids, Hlab)

        # ---- WKPI-Tuned ----
        coefs = {}
        for j, k in enumerate(intent_ids):
            if 0 < np.sum(Ytr[:, j]) < len(Ytr):
                lr = LogisticRegression(max_iter=200, random_state=cfg['seed'], class_weight='balanced')
                lr.fit(Xtr, Ytr[:, j])
                coefs[k] = np.abs(lr.coef_[0])
            else:
                coefs[k] = np.zeros(len(feats))

        def risk_weighted(Z, weights):
            tw = sum(weights.values()) if sum(weights.values()) > 1e-9 else 1.0
            r = 0
            for kk, w in weights.items():
                sser = Z[kk]
                if kk in lower_is_bad:
                    sser = -sser
                r += (w / tw) * sser
            return r

        sc_tr = {}
        sc_te = {}
        for k in intent_ids:
            wts = {f: coefs[k][fi] for fi, f in enumerate(feats)}
            tr = risk_weighted(Xtr_df, wts).values
            te = risk_weighted(Xte_df, wts).values
            sc_tr[k], sc_te[k] = normalize_pair(tr,te)
        _classic_eval('wkpi', sc_tr, sc_te, Ttr, Tte, tr_ev, te_ev, intent_ids, cfg, fold_no, outdir_by_method, results)

        # ---- Dist-Target ----
        excl = set()
        for evs in tr_ev.values():
            for e_ in evs:
                a = max(int(df_train['t'].min()), e_['start'] - 30)
                b = min(int(df_train['t'].max()), e_['end'] + 30)
                excl.update(range(a, b + 1))
        golden = df_train[~df_train['t'].isin(excl)].index
        target_unscaled = train_features.mean() if len(golden) < 100 else train_features.loc[golden].mean()
        target_scaled = dict(zip(feats, scaler.transform(target_unscaled.values.reshape(1, -1))[0]))
        sc_tr, sc_te = {}, {}
        for k in intent_ids:
            sub = feat_sel[k]
            tv = np.array([target_scaled[f] for f in sub])
            tr = np.sqrt(np.sum((Xtr_df[sub].values-tv)**2,axis=1))
            te = np.sqrt(np.sum((Xte_df[sub].values-tv)**2,axis=1))
            sc_tr[k],sc_te[k] = normalize_pair(tr,te)
        _classic_eval('dist', sc_tr, sc_te, Ttr, Tte, tr_ev, te_ev, intent_ids, cfg, fold_no, outdir_by_method, results)

        # ---- LR-OvR ----
        sc_tr, sc_te = {}, {}
        for j, k in enumerate(intent_ids):
            if 0 < np.sum(Ytr[:, j]) < len(Ytr):
                lr = LogisticRegression(max_iter=200, random_state=cfg['seed'], class_weight='balanced')
                lr.fit(Xtr, Ytr[:, j])
                sc_tr[k] = lr.predict_proba(Xtr)[:, 1]
                sc_te[k] = lr.predict_proba(Xte)[:, 1]
            else:
                sc_tr[k] = np.zeros(len(Xtr))
                sc_te[k] = np.zeros(len(Xte))
        _classic_eval('lr', sc_tr, sc_te, Ttr, Tte, tr_ev, te_ev, intent_ids, cfg, fold_no, outdir_by_method, results)
    return results


def file_sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def main():
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--datasets',default='statistical,microservices,sdn')
    ap.add_argument('--data_root',default='.')
    ap.add_argument('--out',default='classic_38_results')
    ap.add_argument('--seeds',default='42',help='run_all main uses 42; main,seeds defaults to 42,43,44,45,46')
    ap.add_argument('--horizon',choices=['classic','mild'],default='classic')
    ap.add_argument('--distance_features',choices=['all','original'],default='all')
    a=ap.parse_args();out=Path(a.out).resolve();out.mkdir(parents=True,exist_ok=True)
    seeds=[int(x) for x in a.seeds.split(',')];datasets=a.datasets.split(',');rows=[];plan=[]
    for ds in datasets:
        if ds not in P.DATASETS:raise SystemExit('Unknown dataset: '+ds)
        csv=Path(a.data_root)/P.DATASETS[ds]['csv'];evj=Path(a.data_root)/P.DATASETS[ds]['events']
        for seed in seeds:
            cfg=dict(P.CLASSIC[ds],seed=seed,distance_features=a.distance_features)
            if a.horizon=='mild':cfg['label_horizon']=P.MILD[ds]['horizon']
            plan.append(dict(dataset=ds,csv=str(csv.resolve()),events=str(evj.resolve()),csv_sha=file_sha(csv),events_sha=file_sha(evj),cfg=cfg))
    record=dict(script_sha=file_sha(__file__),source_sha={n:file_sha(Path(core.__file__).parent/n) for n in ['core.py','runners.py','paper_config.py']},plan=plan)
    rp=out/'PLAN.json'
    if rp.exists() and json.loads(rp.read_text())!=record:raise SystemExit('Output plan changed: choose a new --out.')
    rp.write_text(json.dumps(record,indent=2)+'\n')
    for item in plan:
        ds=item['dataset'];cfg=item['cfg'];seed=cfg['seed'];d=out/ds/('seed'+str(seed));d.mkdir(parents=True,exist_ok=True)
        if (d/'result.json').exists():result=json.loads((d/'result.json').read_text())
        else:
            df,events,ids=core.load_dataset(item['csv'],item['events'])
            _,features=core.engineer(df.iloc[:1]);assert len(features)==38,features
            (d/'features.json').write_text(json.dumps(features,indent=2))
            dirs={m:str(d/m) for m in CLASSIC_METHODS}
            folds=run_classic(df,events,ids,cfg,dirs)
            result={m:core.aggregate_folds(fs,ids) for m,fs in folds.items()}
            (d/'result.json').write_text(json.dumps(result,indent=2,default=float))
        for method,agg in result.items():rows.append(dict(dataset=ds,seed=seed,method=method,**agg['macro']))
        pd.DataFrame(rows).to_csv(out/'classic_runs.csv',index=False)
    table=pd.DataFrame(rows)
    cols=[c for c in ['detection','lead','fp_per_day','disamb'] if c in table]
    summary=table.groupby(['dataset','method'])[cols].agg(['mean','std'])
    summary.to_csv(out/'classic_summary.csv');print(summary.to_string());print('Saved:',out)
if __name__=='__main__':main()
