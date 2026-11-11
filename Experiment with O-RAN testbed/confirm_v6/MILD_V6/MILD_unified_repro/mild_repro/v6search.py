"""Predeclared, equal-count candidate schedules (no adaptive extra trials)."""
import numpy as np


def sample_cfgs_v6(method, base, rng, n, Hc):
    if method not in ('mild','mlp','lstm'):
        raise ValueError('V6 search currently supports neural methods')
    shared_rng=np.random.default_rng(7)  # identical generic candidate draws for all methods
    out=[]
    # Six matched anchors: the first four isolate gate/teacher changes;
    # last two isolate calibration. Generic alarm loss options follow.
    for i in range(n):
        c=dict(base)
        if i<6:
            c.update(threshold_mode='holdout' if i>=4 else 'crossfit')
            c['alarm_loss']='paper_focal' if method=='mild' else 'bce'
        else:
            c.update(horizon=int(shared_rng.choice(Hc)),epochs=int(shared_rng.choice([20,30])),
                     batch=int(shared_rng.choice([128,256])),
                     alarm_loss=str(shared_rng.choice(['bce','paper_focal'])),
                     threshold_mode=str(shared_rng.choice(['holdout','crossfit'])),
                     distill_alpha=float(shared_rng.choice([.5,1.])))
            if method=='lstm': c['lookback']=int(rng.choice([20,40,60]))
        if method=='mild':
            if i<6:
                gm,gu,on=[('multiply','scale',True),('residual','scale',True),
                          ('multiply','attr_only',True),('multiply','attr_only',False),
                          ('multiply','scale',True),('multiply','attr_only',False)][i]
            else:
                gm=str(rng.choice(['multiply','residual']))
                gu=str(rng.choice(['scale','attr_only']))
                on=bool(rng.choice([False,True]))
            c.update(gate_mode=gm,gate_use=gu,use_teacher_gate=on,gate_teacher=.5 if on else 0.)
            if not on: c['distill_alpha']=1.
        if method!='mild' and i<6:
            # Six distinct baseline candidates: horizons/loss, plus matched calibration controls.
            if i==1: c['horizon']=int(Hc[-1])
            if i==2: c['alarm_loss']='paper_focal';c['distill_alpha']=1.
            if i==3: c['alarm_loss']='paper_focal';c['distill_alpha']=.5
            if i==5: c['alarm_loss']='paper_focal';c['distill_alpha']=1.
        out.append(c)
    return out
