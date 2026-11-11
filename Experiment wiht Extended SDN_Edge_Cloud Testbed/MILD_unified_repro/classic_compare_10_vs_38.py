#!/usr/bin/env python3
"""Paired 10-versus-38 feature ablation, classic models ONLY.
Place beside run_all.py in the October mild_repro.zip package.

Example:
 python classic_compare_10_vs_38.py --datasets statistical,microservices,sdn \
   --data_root . --seeds 42,43,44 --out classic_feature_comparison

Both branches use ONE runner. Only the selected feature columns change:
10=core.BASE_FEATURES; 38=core.engineer columns (10 base+28 rolling).
All folds, event labels, horizon, scaler-fitting policy, LR settings, WKPI
sign convention, training-fixed score normalization, and calibration rule
are common. Rolling features reset at block boundaries, as in original MILD.
There is no model/hyperparameter search and no neural model is trained.
Default H=120/folds/FP budgets come from P.CLASSIC per dataset. --horizon mild
uses P.MILD horizons for BOTH branches, and needs a separate output folder.

This is a controlled feature sensitivity, NOT an exact reproduction of old
10-feature paper rows: the old test-extrema normalization is fixed in BOTH
branches. Distance uses ALL selected coordinates in BOTH branches rather
than old intent-specific subsets. Its raw score is shared across intent heads;
DA is limited by that construction and intent-specific calibrated policies.
WKPI uses the same family rule: lower sri/snet/analytics_tput (and their
rolling means) is worse; other coordinates, including stds, use positive sign.
As in the old classic runner, thresholds are selected on TRAINING scores,
not held-out calibration. This is not a full protocol match to neural models.

Resume with exactly the same command: completed feature/seed jobs are reused;
a partly completed job restarts all its folds. Atomic result writes prevent
partial jobs being mistaken for complete. Do not run two copies in one output.
No seed-bootstrap significance is inferred from deterministic repeats.
"""
import argparse, hashlib, json, sys, platform, os
from pathlib import Path
import numpy as np
import pandas as pd
import sklearn
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from typing import Dict
from mild_repro import core, paper_config as P
from mild_repro.runners import _tests, _classic_eval, _log
CLASSIC_METHODS=['wkpi','dist','lr']
METRICS=['detection','lead','fp_per_day','disamb']
def feature_names(df,mode):
    _,engineered=core.engineer(df.iloc[:1])
    features=list(core.BASE_FEATURES) if mode=='10' else engineered
    assert len(features)==int(mode),(mode,features)
    return features
def feature_matrix(df,features):
    full,_=core.engineer(df)
    return full.loc[:,features]
def normalize_pair(tr,te):
    lo,hi=float(np.min(tr)),float(np.max(tr))
    if hi-lo<=1e-9:return np.zeros_like(tr),np.zeros_like(te)
    return (tr-lo)/(hi-lo+1e-9),(te-lo)/(hi-lo+1e-9)
def run_classic(df, events_by_intent, intent_ids, cfg, outdir_by_method: Dict[str, str]):
    core.set_global_determinism(cfg['seed'])
    feats = feature_names(df,cfg['feature_mode'])
    Hlab = cfg.get('label_horizon', 120)   # the original script hard-codes 120 for every dataset
    N = len(df)
    tests = _tests(N, cfg)
    results = {m: [] for m in CLASSIC_METHODS}
    feat_sel = {'telemetry': ['sri', 'snet', 'ram_pct', 'cpu_pct', 'telemetry_queue'],
                'api': ['api_latency', 'cpu_pct', 'sri'],
                'analytics': ['snet', 'analytics_tput', 'cpu_pct']}
    lower_is_bad = {'sri', 'snet', 'analytics_tput'}
    lower_is_bad |= {f for f in feats if any(f.startswith(k+'_mean_') for k in ['sri','snet','analytics_tput'])}
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
        train_features = feature_matrix(df_train,feats)
        test_features = feature_matrix(df_test,feats)
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


def file_sha(p):
    h=hashlib.sha256()
    with open(p,'rb') as stream:
        for block in iter(lambda:stream.read(1048576),b''):h.update(block)
    return h.hexdigest()
def write_json(p,value):
    tmp=p.with_name(p.name+'.tmp')
    tmp.write_text(json.dumps(value,indent=2,default=float)+'\n');os.replace(tmp,p)
def write_csv(p,frame):
    tmp=p.with_name(p.name+'.tmp');frame.to_csv(tmp,index=False);os.replace(tmp,p)
def main():
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--datasets',default='statistical,microservices,sdn')
    ap.add_argument('--data_root',default='.')
    ap.add_argument('--out',default='classic_feature_comparison')
    ap.add_argument('--seeds',default='42,43,44')
    ap.add_argument('--horizon',choices=['classic','mild'],default='classic')
    a=ap.parse_args();out=Path(a.out).expanduser().resolve();out.mkdir(parents=True,exist_ok=True)
    seeds=[int(x.strip()) for x in a.seeds.split(',')];datasets=[x.strip() for x in a.datasets.split(',')]
    if len(seeds)!=len(set(seeds)) or len(datasets)!=len(set(datasets)):raise SystemExit('Duplicate seeds/datasets')
    if not seeds or not datasets:raise SystemExit('Empty seeds/datasets')
    plan=[]
    for ds in datasets:
        if ds not in P.DATASETS:raise SystemExit('Unknown dataset: '+ds)
        csv=Path(a.data_root).expanduser()/P.DATASETS[ds]['csv'];evj=Path(a.data_root).expanduser()/P.DATASETS[ds]['events']
        cfg=dict(P.CLASSIC[ds]);cfg.pop('seed',None)
        if a.horizon=='mild':cfg['label_horizon']=P.MILD[ds]['horizon']
        plan.append(dict(dataset=ds,csv=str(csv.resolve()),events=str(evj.resolve()),csv_sha=file_sha(csv),events_sha=file_sha(evj),cfg=cfg))
    record=dict(script_sha=file_sha(__file__),seeds=seeds,modes=['10','38'],plan=plan,
      source_sha={n:file_sha(Path(core.__file__).parent/n) for n in ['core.py','runners.py','paper_config.py']},
      environment=dict(python=platform.python_version(),numpy=np.__version__,pandas=pd.__version__,sklearn=sklearn.__version__),
      normalization='training bounds in both branches',distance='all selected coordinates in both branches',
      calibration='training-score threshold selection, legacy policy; no hyperparameter search')
    rp=out/'PLAN.json'
    if rp.exists() and json.loads(rp.read_text())!=record:raise SystemExit('Data/source/settings/environment differ: choose a new --out.')
    write_json(rp,record)
    expected_jobs=len(plan)*len(seeds)*2;done=0;rows=[]
    def status():write_json(out/'RUN_STATUS.json',dict(expected_jobs=expected_jobs,completed_jobs=done,expected_metric_rows=expected_jobs*3,completed_metric_rows=len(rows),complete=done==expected_jobs))
    status()
    for item in plan:
        ds=item['dataset'];df,events,ids=core.load_dataset(item['csv'],item['events'])
        for seed in seeds:
            for mode in ['10','38']:
                cfg=dict(item['cfg'],seed=seed,feature_mode=mode)
                d=out/ds/('seed'+str(seed))/('features'+mode);d.mkdir(parents=True,exist_ok=True)
                features=feature_names(df,mode)
                _log(f'Dataset={ds} seed={seed} features={mode}')
                if (d/'result.json').exists():
                    job=json.loads((d/'result.json').read_text())
                    if job['cfg']!=cfg or job['features']!=features:raise SystemExit('Cached job mismatch: '+str(d))
                    result=job['methods'];_log('  completed job reused')
                else:
                    write_json(d/'features.json',features)
                    res=run_classic(df,events,ids,cfg,{m:str(d/m) for m in CLASSIC_METHODS})
                    if any(not fs for fs in res.values()):raise SystemExit('No evaluated folds: '+str(d))
                    result={m:core.aggregate_folds(fs,ids) for m,fs in res.items()}
                    write_json(d/'result.json',dict(cfg=cfg,features=features,methods=result,folds_evaluated={m:len(fs) for m,fs in res.items()}))
                if set(result)!=set(CLASSIC_METHODS):raise SystemExit('Incomplete method set: '+str(d))
                for method,agg in result.items():rows.append(dict(dataset=ds,seed=seed,features=int(mode),method=method,**agg['macro']))
                done+=1;write_csv(out/'classic_runs.csv',pd.DataFrame(rows));status()
    table=pd.DataFrame(rows)
    index=['dataset','seed','method']
    paired=table[table.features==10].merge(table[table.features==38],on=index,suffixes=('_10','_38'),validate='one_to_one')
    for metric in METRICS:paired[metric+'_delta_38_minus_10']=paired[metric+'_38']-paired[metric+'_10']
    keep=index+[c for metric in METRICS for c in [metric+'_10',metric+'_38',metric+'_delta_38_minus_10']]
    paired=paired[keep];write_csv(out/'paired_differences.csv',paired)
    compare=[]
    for (ds,method),g in paired.groupby(['dataset','method'],sort=True):
        row=dict(dataset=ds,method=method,seeds=len(g))
        for metric in METRICS:
            for tag in ['10','38','delta_38_minus_10']:
                col=metric+'_'+tag;row[col+'_mean']=g[col].mean();row[col+'_std']=g[col].std()
        compare.append(row)
    summary=pd.DataFrame(compare);write_csv(out/'comparison_summary.csv',summary)
    table.groupby(['dataset','features','method'])[METRICS].agg(['mean','std']).to_csv(out/'classic_summary.csv')
    print(summary[['dataset','method']+[m+'_delta_38_minus_10_mean' for m in METRICS]].to_string(index=False))
    print('COMPLETE:',done,'/',expected_jobs,'jobs;',len(table),'metric rows. Saved:',out)
if __name__=='__main__':main()
