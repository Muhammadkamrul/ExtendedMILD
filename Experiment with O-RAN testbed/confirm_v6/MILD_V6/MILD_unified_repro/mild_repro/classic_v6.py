"""Prospective classical scores with the same optional OOF calibration policy.
Not part of the 20-trial-per-neural-method V6 architecture selection study.
"""
import json
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from . import core
from .v6 import quiet,contained


def run_classic_v6(df, ev, ids, cfg, outdirs):
    from .runners import _save_fold
    s,e=map(int,cfg['test_range']);df=df.iloc[:e].reset_index(drop=True)
    ev=contained(ev,df.t.to_numpy())
    X,features=core.engineer(df,core.feature_base(df),onset=core.onset_mode(cfg))
    X=X.to_numpy(dtype='float32');times=df.t.to_numpy()
    feat_sel={'telemetry':['sri','snet','ram_pct','cpu_pct','telemetry_queue'],
              'api':['api_latency','cpu_pct','sri'],
              'analytics':['snet','analytics_tput','cpu_pct']}
    lower={'sri','snet','analytics_tput'}
    mode=cfg.get('threshold_mode','crossfit')
    blocks={m:[] for m in outdirs}
    audits=[]
    def predict(stop,end,label,prediction_start=None):
        fit_end=quiet(times[:stop],ev,int((1-cfg.get('val_frac',.4))*stop))
        sc=StandardScaler().fit(X[:fit_end]);Z=sc.transform(X[:end])
        tr_ev=contained(ev,times[:fit_end])
        Y=core.build_binary_labels(df.iloc[:fit_end],tr_ev,ids,cfg.get('label_horizon',120))
        prediction_start=stop if prediction_start is None else prediction_start
        fit=Z[:fit_end];pr=Z[prediction_start:end]
        risks={m:{} for m in outdirs}
        excluded=core.in_any_window(times[:fit_end],[(a['start']-30,a['end']+30) for es in tr_ev.values() for a in es])
        healthy=fit[~excluded];target=(healthy if len(healthy)>=100 else fit).mean(axis=0)
        def normalized(tr,te):
            lo,hi=tr.min(),tr.max()
            return (te-lo)/(hi-lo+1e-9) if hi-lo>1e-9 else np.zeros(len(te))
        for j,k in enumerate(ids):
            w=np.zeros(len(features))
            if len(np.unique(Y[:,j]))==2:
                lr=LogisticRegression(class_weight='balanced',max_iter=600,random_state=cfg['seed']).fit(fit,Y[:,j])
                risks['lr'][k]=lr.predict_proba(pr)[:,1];w=np.abs(lr.coef_[0])
            else:risks['lr'][k]=np.zeros(len(pr))
            sign=np.array([-1 if f in lower else 1 for f in features])
            weights=w/max(w.sum(),1e-9)*sign
            risks['wkpi'][k]=normalized(fit@weights,pr@weights)
            ix=[features.index(f) for f in feat_sel[k]]
            tr=np.sqrt(np.square(fit[:,ix]-target[ix]).sum(axis=1))
            te=np.sqrt(np.square(pr[:,ix]-target[ix]).sum(axis=1))
            risks['dist'][k]=normalized(tr,te)
        audits.append(dict(label=label,fit_rows=[0,fit_end],reserved_validation_rows=[fit_end,stop],prediction_rows=[prediction_start,end],features=features))
        return {m:pd.DataFrame({'t':times[prediction_start:end],**{'raw_'+k:v for k,v in rs.items()}}) for m,rs in risks.items()}
    if mode=='crossfit':
        cuts=[quiet(times[:s],ev,int(.4*s)),quiet(times[:s],ev,int(.7*s)),s]
        for i,(a,b) in enumerate(zip(cuts[:-1],cuts[1:])):
            rr=predict(a,b,f'oof{i+1}')
            for m in blocks:blocks[m].append(rr[m])
    elif mode!='holdout':
        raise ValueError('threshold_mode must be crossfit or holdout')
    cut=quiet(times[:s],ev,int((1-cfg.get('val_frac',.4))*s))
    final=predict(s,e,'final',prediction_start=cut if mode=='holdout' else None)
    if mode=='holdout':
        for m in final:
            blocks[m]=[final[m][final[m].t<times[s]].reset_index(drop=True)]
            final[m]=final[m][final[m].t>=times[s]].reset_index(drop=True)
    results={m:[] for m in outdirs}
    for m in outdirs:
        p=core.crossfit_params(blocks[m],ev,ids,cfg.get('fp_budget',1.),cfg.get('thr_rule','expected_lead'),cfg.get('fp_scope','own'))
        # Classical scores can exceed 1 because their normalizers are frozen on training.
        # Preserve a truly inactive fallback on later finite scores as well.
        for k in ids:
            largest=max(float(b['raw_'+k].max()) for b in blocks[m])
            if p[k]['tau']>max(largest,1.):p[k]['tau']=float(np.finfo(float).max)
        ts=final[m];t=ts.t.to_numpy();ema,alerts=core.apply_policy({k:ts['raw_'+k].to_numpy() for k in ids},t,p)
        met,rec,days=core.evaluate(alerts,contained(ev,t),t,ema,fold_t0=t[0],fp_scope=cfg.get('fp_scope','own'))
        met.update(params=p,fold=1,calibration_mode=mode,feature_columns=features)
        rec.insert(0,'fold',1);days.insert(0,'fold',1)
        for k in ids:ts['ema_'+k]=ema[k]
        _save_fold(outdirs[m],1,met,rec,days,ts);results[m].append(met)
        d=Path(outdirs[m])/'calibration';d.mkdir(exist_ok=True,parents=True)
        pd.concat([b.assign(block=i) for i,b in enumerate(blocks[m])]).to_csv(d/'pooled_oof.csv',index=False)
        (d/'fit_audit.json').write_text(json.dumps(audits,indent=2))
    return results
