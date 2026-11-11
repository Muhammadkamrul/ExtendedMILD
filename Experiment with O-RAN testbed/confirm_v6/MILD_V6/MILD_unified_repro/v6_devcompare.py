#!/usr/bin/env python3
"""Development-only V6 search. Requires an already restricted data snapshot.
Never invokes autotrain.main or a held-out-test phase. Candidate JSON is saved
BEFORE any training. One method can run per process with identical budgets.
"""
import argparse,hashlib,json,os,sys
from pathlib import Path
import numpy as np
import pandas as pd
import autotrain as A
from mild_repro import core,paper_config as P


def metrics(res,method,d):
    m=A.macro_of(res,method)
    row={k:v for k,v in m.items() if k!='per_intent_detection'}
    row.update({'det_'+k:v for k,v in m['per_intent_detection'].items()})
    rec=pd.read_csv(Path(d)/'folds/fold1_events.csv')
    roots=rec[rec.root_paper==1]
    # Correct root named by earliest cross-intent alarm, >=10 ticks before root failure.
    row['legacy_actionable']=100*float(((roots.cause_correct==1)&(roots.detected==1)).mean())
    row['own_early_correct_cause']=100*float(((roots.cause_correct==1)&(roots.detected==1)&(roots.lead>=10)).mean())
    row['correct_cause_actionable']=100*float(((roots.cause_correct==1)&((roots.failure_time-roots.earliest_alert)>=10)).mean())
    victims=rec[rec.type=='conflicting_victim']
    row['victim_detection']=100*float(victims.detected.mean())
    row['n_events']=len(rec);row['n_roots']=len(roots);row['n_victims']=len(victims)
    return row


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--data_csv',required=True);ap.add_argument('--events_json',required=True)
    ap.add_argument('--out',required=True);ap.add_argument('--method',required=True,choices=A.NEURAL)
    ap.add_argument('--dev_end',type=int,default=10080);ap.add_argument('--inner',type=int,default=7594)
    ap.add_argument('--trials',type=int,default=20);ap.add_argument('--rerank_top',type=int,default=3)
    ap.add_argument('--smoke',action='store_true')
    a=ap.parse_args();out=Path(a.out).resolve();out.mkdir(parents=True,exist_ok=True)
    dc,ej=str(Path(a.data_csv).resolve()),str(Path(a.events_json).resolve())
    df,ev,ids=core.load_dataset(dc,ej)
    if len(df)!=a.dev_end: raise ValueError('Physical data must already end at the development boundary')
    for r in (a.inner,):
        if A.quiet_cut(df,ev,r)!=r: raise ValueError('Evaluation boundary crosses event')
    _,adf=A.audit(df,ev);lt_ref=float(adf.buildup.median())
    base=dict({'mild':P.MILD,'mlp':P.MLP,'lstm':P.LSTM}[a.method]['oran'])
    base.pop('folds',None)
    base.update(protocol_v6=True,features='onset',horizon=int(round(.8*lt_ref)),
                fp_budget=1.,thr_budget_fixed=True,fp_scope='own',val_frac=.4,
                thr_rule='expected_lead',threshold_mode='crossfit',distill_alpha=1.,
                temp=2.,epochs=20,batch=128,patience=8)
    if a.method=='mild':base.update(gate_mode='multiply',gate_use='scale')
    Hc=A.h_candidates(adf,base['horizon'])
    cfgs=A.sample_cfgs(a.method,base,np.random.default_rng(7),a.trials,Hc)
    if a.smoke:
        cfgs=[dict(cfgs[0],epochs=1)]
    plan={'protocol':'V6-development-1','method':a.method,'rows':len(df),'inner':a.inner,
          'lt_ref':lt_ref,'horizons':Hc,'candidates':cfgs,'seeds':[42,43,44],
          'data_sha256':hashlib.sha256(Path(dc).read_bytes()).hexdigest(),
          'events_sha256':hashlib.sha256(Path(ej).read_bytes()).hexdigest()}
    plan_path=out/f'{a.method}_candidate_plan.json'
    if plan_path.exists() and json.loads(plan_path.read_text())!=plan:
        raise ValueError('Existing immutable candidate plan differs')
    plan_path.write_text(json.dumps(plan,indent=2))
    rows=[]
    def run(c,trial,sd,role):
        cc=dict(c,seed=sd,test_range=[a.inner,a.dev_end],row_limit=a.dev_end,fp_block_ticks=240)
        res,d=A.run_job(str(out),dc,ej,a.method,cc,f'v6:{role}:trial{trial}:seed{sd}')
        if res is None: raise RuntimeError(f'Failed job: {d}; do not select around failed jobs')
        mm=metrics(res,a.method,d)
        r=dict(method=a.method,trial=trial,seed=sd,role=role,score=A.objective(mm,3.,lt_ref),
               **mm,cfg=json.dumps(c,sort_keys=True),job_dir=d)
        rows.append(r)
        pd.DataFrame(rows).to_csv(out/f'{a.method}_all_runs.csv',index=False)
        print({k:r[k] for k in ['method','trial','seed','role','detection','disamb','fp_per_day','score']},flush=True)
        return r
    first=[run(c,i,42,'search') for i,c in enumerate(cfgs)]
    if a.smoke:return
    top=sorted(first,key=lambda r:-r['score'])[:a.rerank_top]
    rerank=[]
    for r in top:
        c=cfgs[r['trial']]
        rr=[r]+[run(c,r['trial'],sd,'rerank') for sd in [43,44]]
        rerank.append(dict(trial=r['trial'],mean_score=float(np.mean([x['score'] for x in rr])),cfg=c))
    win=max(rerank,key=lambda r:r['mean_score'])
    (out/f'{a.method}_selected.json').write_text(json.dumps(win,indent=2))
    # Matched, fixed original-paper-input anchor; never eligible for selection.
    anchor=dict(base,features='paper',threshold_mode='holdout',alarm_loss='paper_focal' if a.method=='mild' else 'bce')
    if a.method=='mild':anchor.update(gate_mode='multiply',gate_use='scale',distill_alpha=.8)
    for sd in [42,43,44]:run(anchor,-1,sd,'paper_input_anchor')
    pd.DataFrame([dict(trial=r['trial'],mean_score=r['mean_score'],cfg=json.dumps(r['cfg'],sort_keys=True)) for r in rerank]).to_csv(out/f'{a.method}_rerank.csv',index=False)
    print('SELECTED',a.method,win,flush=True)

if __name__=='__main__':main()
