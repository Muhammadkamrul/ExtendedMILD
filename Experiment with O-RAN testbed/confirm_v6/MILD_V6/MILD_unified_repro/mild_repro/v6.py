"""Opt-in, development-tested neural runner with equal features and OOF calibration.

Legacy runners remain unchanged unless protocol_v6=True. Chronological OOF
models use earlier fit/early-stop blocks only; policy sees OOF scores, never
outer evaluation scores. Alarm loss options are available to all three methods.
"""
import json
import os
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from . import core


def quiet(t, ev, row):
    busy = core.in_any_window(t, [(e['start'], e['end']) for es in ev.values() for e in es])
    for r in range(int(row), len(t)):
        if not busy[r]:
            return r
    raise ValueError('No quiet boundary available')


def contained(ev, t):
    return {k: [dict(e) for e in es if e['start'] >= t[0] and e['end'] <= t[-1]]
            for k, es in ev.items()}


def _dataset(tf, x, y, batch, seed, shuffle):
    # Explicit private pool avoids one large implicit pool per Keras iterator.
    d = tf.data.Dataset.from_tensor_slices((tuple(x) if isinstance(x, list) else x, y))
    if shuffle:
        d = d.shuffle(len(next(iter(y.values()))), seed=seed, reshuffle_each_iteration=True)
    opt = tf.data.Options()
    opt.threading.private_threadpool_size = 1
    opt.threading.max_intra_op_parallelism = 1
    return d.batch(batch).with_options(opt)


def _predict(model, x):
    n = len(x[0]) if isinstance(x, list) else len(x)
    pieces = []
    for i in range(0, n, 512):
        inp = [v[i:i+512] for v in x] if isinstance(x, list) else x[i:i+512]
        out = model(inp, training=False)
        if not isinstance(out, (list, tuple)):
            out = [out]
        pieces.append([np.asarray(v) for v in out])
    return [np.concatenate([p[j] for p in pieces]) for j in range(len(pieces[0]))]


def fit_predict(method, X, df, ev, ids, cfg, stop, end, outdir, label):
    """Train before stop; early stop before stop; predict [stop,end)."""
    import tensorflow as tf
    from .models import (create_mild_moe_with_teacher, create_mlp_baseline,
                         create_lstm_baseline, distillation_focal_with_temperature,
                         gate_kldiv_with_teacher, head_decorrelation)
    tf.keras.backend.clear_session()
    core.set_global_determinism(cfg['seed'])
    t = df.t.to_numpy()
    fit_end = quiet(t[:stop], ev, int((1-cfg.get('val_frac', .4))*stop))
    if fit_end >= stop or fit_end < 500:
        raise ValueError('Insufficient earlier fitting / early-stop data')
    # No event is clipped to manufacture a failure at any boundary.
    assert not any(e['start'] <= t[fit_end] <= e['end'] for es in ev.values() for e in es)
    scaler = StandardScaler().fit(X[:fit_end])
    Z = scaler.transform(X[:end]).astype('float32')
    all_ttf, all_bin, all_cause = core.build_forward_labels(df.iloc[:end], ev, ids, cfg['horizon'])
    # Restrict training labels to failures available before each fitting boundary.
    for lo, hi in [(0,fit_end),(fit_end,stop)]:
        sub = df.iloc[lo:hi]
        yy = core.build_forward_labels(sub, contained(ev, sub.t.to_numpy()), ids, cfg['horizon'])
        all_ttf[lo:hi], all_bin[lo:hi], all_cause[lo:hi] = yy
    ranges = [np.arange(0, fit_end), np.arange(fit_end,stop), np.arange(stop,end)]
    if method == 'lstm':
        L = cfg.get('lookback',40)
        ranges[0] = ranges[0][ranges[0] >= L-1]
        def inp(ix):
            return np.stack([Z[i-L+1:i+1] for i in ix])
    else:
        def inp(ix):
            return Z[ix]
    xs = [inp(ix) for ix in ranges]
    teacher_on = method == 'mild' and cfg.get('use_teacher_gate',True)
    alpha = cfg.get('distill_alpha',1.)
    loss_kind = cfg.get('alarm_loss', 'paper_focal' if method=='mild' else 'bce')
    needs_teacher = teacher_on or (method=='mild' and cfg.get('gate_teacher',0)>0) or (loss_kind=='paper_focal' and alpha<1)
    P = np.zeros((end,len(ids)), dtype='float32')
    if needs_teacher:
        for j in range(len(ids)):
            if len(np.unique(all_bin[:fit_end,j])) == 2:
                lr = LogisticRegression(class_weight='balanced',max_iter=600,random_state=cfg['seed'])
                lr.fit(Z[:fit_end],all_bin[:fit_end,j])
                P[:,j] = lr.predict_proba(Z)[:,1]
    D = core.prob_to_dist(P,T=cfg.get('temp',2.))
    if method=='mild':
        model = create_mild_moe_with_teacher(X.shape[1],len(ids),ids,
                    use_teacher_gate=teacher_on,gate_mode=cfg.get('gate_mode','multiply'),
                    gate_use=cfg.get('gate_use','scale'))
        if teacher_on:
            xs = [[x,D[ix]] for x,ix in zip(xs,ranges)]
    elif method=='mlp':
        model = create_mlp_baseline(X.shape[1],ids)
    else:
        model = create_lstm_baseline((cfg.get('lookback',40),X.shape[1]),ids)
    losses, ys = {}, []
    for ix in ranges[:2]:
        y = {}
        for j,k in enumerate(ids):
            y[k+'_out'] = (all_bin[ix,j:j+1] if loss_kind=='bce' else
                           np.column_stack([all_ttf[ix,j],P[ix,j]]))
        if method=='mild':
            y['gate'] = np.concatenate([all_cause[ix],D[ix]],axis=1)
            y['head_concat'] = np.zeros((len(ix),16*len(ids)),dtype='float32')
        ys.append(y)
    for k in ids:
        losses[k+'_out'] = ('binary_crossentropy' if loss_kind=='bce' else
                  distillation_focal_with_temperature(alpha=alpha,horizon_min=cfg['horizon'],temperature=cfg.get('temp',2.)))
    if method=='mild':
        losses['gate'] = gate_kldiv_with_teacher(cfg.get('gate_supervise',.5),
                               cfg.get('gate_teacher',.5),cfg.get('gate_sparsity',.001))
        losses['head_concat'] = head_decorrelation(cfg.get('lambda_decorr',1e-4))
    model.compile(optimizer=tf.keras.optimizers.Adam(1e-3),loss=losses)
    train = _dataset(tf,xs[0],ys[0],cfg['batch'],cfg['seed'],True)
    val = _dataset(tf,xs[1],ys[1],cfg['batch'],cfg['seed'],False)
    h = model.fit(train,validation_data=val,epochs=cfg['epochs'],verbose=0,
          callbacks=[tf.keras.callbacks.EarlyStopping(monitor='val_loss',patience=cfg.get('patience',8),restore_best_weights=True)])
    preds = _predict(model,xs[2])
    va = _predict(model,xs[1])
    ts = pd.DataFrame({'t':t[ranges[2]],**{f'raw_{k}':preds[j].ravel() for j,k in enumerate(ids)}})
    if method=='mild':
        for j,k in enumerate(ids): ts[f'gate_{k}'] = preds[len(ids)][:,j]
    vs = pd.DataFrame({'t':t[ranges[1]],**{f'raw_{k}':va[j].ravel() for j,k in enumerate(ids)}})
    audit = dict(label=label,fit_rows=[0,fit_end],early_stop_rows=[fit_end,stop],prediction_rows=[stop,end],
                 epochs_run=len(h.history['loss']),best_epoch=int(np.argmin(h.history['val_loss']))+1,
                 features=int(X.shape[1]),parameters=model.count_params(),teacher_trained=bool(needs_teacher))
    os.makedirs(outdir,exist_ok=True)
    with open(os.path.join(outdir,label+'_fit.json'),'w') as f: json.dump(audit,f,indent=2)
    ts.to_csv(os.path.join(outdir,label+'_raw.csv'),index=False)
    return ts,vs


def run_neural(method, df, ev, ids, cfg, outdir):
    from .runners import _save_fold
    if not cfg.get('test_range'):
        raise ValueError('protocol_v6 requires an explicit chronological test_range')
    s,e = map(int,cfg['test_range'])
    if not 1000 < s < e <= len(df): raise ValueError('Invalid evaluation range')
    df=df.iloc[:e].reset_index(drop=True)
    ev=contained(ev,df.t.to_numpy())
    X,feats=core.engineer(df,core.feature_base(df),onset=core.onset_mode(cfg))
    X=X.to_numpy(dtype='float32')
    mode=cfg.get('threshold_mode','crossfit')
    fold_dir=os.path.join(outdir,'calibration')
    blocks=[]
    if mode=='crossfit':
        cuts=[quiet(df.t.to_numpy()[:s],ev,int(.4*s)),quiet(df.t.to_numpy()[:s],ev,int(.7*s)),s]
        for i,(a,b) in enumerate(zip(cuts[:-1],cuts[1:])):
            sc,_=fit_predict(method,X,df,ev,ids,cfg,a,b,fold_dir,f'oof{i+1}')
            blocks.append(sc)
    elif mode!='holdout':
        raise ValueError('threshold_mode must be holdout or crossfit')
    ts,vs=fit_predict(method,X,df,ev,ids,cfg,s,e,fold_dir,'final')
    if mode=='holdout': blocks=[vs]
    params=core.crossfit_params(blocks,ev,ids,cfg.get('fp_budget',1.),cfg.get('thr_rule','expected_lead'),cfg.get('fp_scope','own'))
    times=ts.t.to_numpy()
    scores={k:ts['raw_'+k].to_numpy() for k in ids}
    ema,alerts=core.apply_policy(scores,times,params)
    gate=ts[[f'gate_{k}' for k in ids]].to_numpy() if method=='mild' else None
    m,rec,days=core.evaluate(alerts,contained(ev,times),times,ema,gate_probs=gate,fold_t0=times[0],fp_scope=cfg.get('fp_scope','own'))
    for k in ids: ts['ema_'+k]=ema[k]
    m.update(params=params,fold=1,calibration_mode=mode,feature_columns=feats)
    rec.insert(0,'fold',1);days.insert(0,'fold',1)
    _save_fold(outdir,1,m,rec,days,ts)
    pd.concat([b.assign(block=i) for i,b in enumerate(blocks)]).to_csv(os.path.join(fold_dir,'pooled_oof.csv'),index=False)
    return [m]
