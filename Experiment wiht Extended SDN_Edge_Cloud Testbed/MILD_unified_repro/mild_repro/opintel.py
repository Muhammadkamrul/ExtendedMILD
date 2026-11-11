"""
opintel.py -- operational-intelligence experiments of the paper:

  * run_multihorizon_shap(): port of multi_horizon_and_shap_V2.py
      (multi-horizon urgency estimation table + SHAP explanation per horizon)
  * plot_disambiguation(): port of plot_disambiguation.py (Fig. 9 style)

The training routine is kept identical to multi_horizon_and_shap_V2.py,
including its own small differences from the main runner (teacher LR with
max_iter=200, gate targets that fall back to binary labels on rows without a
cause label, and teacher input fed as zeros unless use_teacher_inputs=True).
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from . import core


def _train_one_horizon(df, events_by_intent, intent_ids, H, cfg, outdir):
    import tensorflow as tf
    from .models import (create_mild_moe_with_teacher, distillation_focal_with_temperature,
                         gate_kldiv_with_teacher, head_decorrelation)
    tests = core.blocked_folds(len(df), cfg['folds'])
    s, e = tests[cfg['fold'] - 1]
    df_test = df.iloc[s:e].reset_index(drop=True)
    df_train_full = df.iloc[:s].reset_index(drop=True)
    assert len(df_train_full) >= 1000, "Not enough train history for this fold."
    split = int(0.8 * len(df_train_full))
    df_train = df_train_full.iloc[:split].reset_index(drop=True)
    df_val = df_train_full.iloc[split:].reset_index(drop=True)
    tr_ev = core.filter_events(events_by_intent, df_train['t'].min(), df_train['t'].max())
    va_ev = core.filter_events(events_by_intent, df_val['t'].min(), df_val['t'].max())
    te_ev = core.filter_events(events_by_intent, df_test['t'].min(), df_test['t'].max())

    Xtr, Ytr_ttf, Ytr_bin, Ytr_cause, Ttr, feats = core.make_forward_looking_dataset(df_train, tr_ev, intent_ids, H)
    Xva, Yva_ttf, Yva_bin, Yva_cause, Tva, _ = core.make_forward_looking_dataset(df_val, va_ev, intent_ids, H)
    Xte, Yte_ttf, Yte_bin, Yte_cause, Tte, _ = core.make_forward_looking_dataset(df_test, te_ev, intent_ids, H)
    scaler = StandardScaler().fit(Xtr)
    Xtr, Xva, Xte = scaler.transform(Xtr), scaler.transform(Xva), scaler.transform(Xte)

    K = len(intent_ids)
    P_tr, P_va, P_te = (np.zeros((len(X), K), dtype='float32') for X in (Xtr, Xva, Xte))
    Ytr_bin_fold = core.build_binary_labels(df_train, tr_ev, intent_ids, H)
    for j, name in enumerate(intent_ids):
        if np.unique(Ytr_bin_fold[:, j]).size < 2:
            continue
        lr = LogisticRegression(max_iter=200, random_state=cfg['seed'], class_weight='balanced')
        lr.fit(Xtr, Ytr_bin_fold[:, j])
        P_tr[:, j], P_va[:, j], P_te[:, j] = (lr.predict_proba(X)[:, 1] for X in (Xtr, Xva, Xte))
    D_tr, D_va, D_te = (core.prob_to_dist(P, T=cfg['temp']) for P in (P_tr, P_va, P_te))

    ytr, yva = {}, {}
    for j, name in enumerate(intent_ids):
        ytr[f'{name}_out'] = np.concatenate([Ytr_ttf[:, j:j + 1], P_tr[:, j:j + 1]], axis=1)
        yva[f'{name}_out'] = np.concatenate([Yva_ttf[:, j:j + 1], P_va[:, j:j + 1]], axis=1)
    gate_tr, gate_va = Ytr_cause.astype('float32').copy(), Yva_cause.astype('float32').copy()
    mtr, mva = gate_tr.sum(axis=1) == 0, gate_va.sum(axis=1) == 0
    gate_tr[mtr, :] = Ytr_bin[mtr, :]
    gate_va[mva, :] = Yva_bin[mva, :]
    ytr['gate'] = np.concatenate([gate_tr, D_tr], axis=1)
    yva['gate'] = np.concatenate([gate_va, D_va], axis=1)
    ytr['head_concat'] = np.zeros((len(Xtr), K * 16), dtype='float32')
    yva['head_concat'] = np.zeros((len(Xva), K * 16), dtype='float32')
    use_ti = cfg.get('use_teacher_inputs', False)
    Ttr_in, Tva_in, Tte_in = ((D if use_ti else np.zeros_like(D)) for D in (D_tr, D_va, D_te))

    tf.keras.backend.clear_session()
    model = create_mild_moe_with_teacher(num_features=Xtr.shape[1], num_teacher=K, intent_ids=intent_ids)
    losses = {f'{n}_out': distillation_focal_with_temperature(alpha=cfg['distill_alpha'], horizon_min=H,
                                                              temperature=cfg['temp']) for n in intent_ids}
    losses['gate'] = gate_kldiv_with_teacher(cfg['gate_supervise'], cfg['gate_teacher'], cfg['gate_sparsity'])
    losses['head_concat'] = head_decorrelation(1e-4)
    model.compile(optimizer=tf.keras.optimizers.Adam(1e-3), loss=losses)
    cb = [tf.keras.callbacks.EarlyStopping(monitor='val_loss', patience=8, restore_best_weights=True)]
    model.fit([Xtr, Ttr_in], ytr, epochs=cfg['epochs'], batch_size=cfg['batch'],
              validation_data=([Xva, Tva_in], yva), callbacks=cb, verbose=0)
    pva = model.predict([Xva, Tva_in], verbose=0)
    pte = model.predict([Xte, Tte_in], verbose=0)
    gate_te = np.asarray(pte[K])
    params = {n: core.grid_search_params(np.asarray(pva[j]).reshape(-1), df_val['t'].values, va_ev, n,
                                         cfg['fp_budget']) for j, n in enumerate(intent_ids)}
    Tv = df_test['t'].values
    scores = {n: np.asarray(pte[j]).reshape(-1) for j, n in enumerate(intent_ids)}
    ema, alerts = core.apply_policy(scores, Tv, params)
    m, rec, days = core.evaluate(alerts, te_ev, Tv, ema, gate_probs=gate_te, fold_t0=Tv[0])
    m['params'] = params
    ts_path = os.path.join(outdir, f"fold{cfg['fold']}_timeseries_{H}.csv")
    cols = {'t': Tv}
    for n in intent_ids:
        cols[f'ema_{n}'] = ema[n]
        cols[f'tau_{n}'] = np.full(len(Tv), params[n]['tau'], dtype=float)
    for j, n in enumerate(intent_ids):
        cols[f'gate_{n}'] = gate_te[:, j]
    pd.DataFrame(cols).to_csv(ts_path, index=False)
    return m, ts_path, model, scaler, Xte, Tv, te_ev, feats


def _pick_event_for_intent(te_events, intent):
    evs = te_events.get(intent, [])
    if not evs:
        return None
    for e in evs:
        a1, b1 = e['start'], e['failure_time']
        overlap = False
        for k, L in te_events.items():
            if k == intent:
                continue
            for e2 in L:
                a2, b2 = e2['start'], e2['failure_time']
                inter = max(0, min(b1, b2) - max(a1, a2))
                dur = min(b1 - a1, b2 - a2)
                if dur > 0 and inter >= 0.5 * dur:
                    overlap = True
                    break
            if overlap:
                break
        if not overlap:
            return e
    return evs[0]


def _shap_for_alert(model, scaler, Xte, Tte, ts_path, intent, feats, out_png, out_csv):
    try:
        import shap
        import tensorflow as tf
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except Exception as exc:
        print(f"[WARN] SHAP unavailable ({exc}); skipping explanation.")
        return None
    d = pd.read_csv(ts_path)
    r, tau = d[f'ema_{intent}'].values, float(d[f'tau_{intent}'].values[0])
    idx = np.where(r >= tau)[0]
    if len(idx) == 0:
        print(f"[WARN] no alert for {intent}; skipping SHAP")
        return None
    i0 = int(np.where(Tte == d['t'].values[idx[0]])[0][0])
    head = model.get_layer(f'{intent}_out').output
    m1 = tf.keras.Model(inputs=model.inputs, outputs=head)
    rng = np.random.default_rng(0)
    nbg = min(200, Xte.shape[0])
    bg = Xte[rng.choice(np.arange(Xte.shape[0]), nbg, replace=False)]
    x0 = Xte[i0:i0 + 1]
    tdim = int(model.inputs[1].shape[-1])
    tbg, t0 = np.zeros((nbg, tdim), dtype=bg.dtype), np.zeros((1, tdim), dtype=bg.dtype)
    try:
        disp = scaler.inverse_transform(x0).reshape(-1)
    except Exception:
        disp = x0.reshape(-1)
    try:
        dexp = shap.DeepExplainer(m1, [bg, tbg])
        sv = dexp.shap_values([x0, t0])
        sv = sv[0] if isinstance(sv, list) else sv
        sv = sv[0] if isinstance(sv, list) else sv
        vals = np.asarray(sv).reshape(1, -1)[0][:len(feats)]
        base = float(np.asarray(dexp.expected_value).reshape(-1)[0])
    except Exception:
        def f(X):
            return m1([X, np.zeros((X.shape[0], tdim), dtype=X.dtype)]).numpy()
        kexp = shap.KernelExplainer(f, bg[:50])
        sv = kexp.shap_values(x0, nsamples=200)
        vals = (sv[0] if isinstance(sv, list) else np.asarray(sv)).reshape(-1)[:len(feats)]
        base = float(np.asarray(kexp.expected_value).reshape(-1)[0])
    exp = shap.Explanation(values=vals, base_values=base, data=disp.astype(np.float32), feature_names=feats)
    dfv = pd.DataFrame({'feature': feats, 'shap': vals}).sort_values('shap', key=np.abs, ascending=False)
    dfv.to_csv(out_csv, index=False)
    try:
        plt.figure(figsize=(9.5, 5.8))
        shap.plots.waterfall(exp, max_display=10, show=False)
        plt.tight_layout()
        plt.savefig(out_png, dpi=200, bbox_inches='tight')
        plt.close()
    except Exception as exc:
        print(f"[WARN] SHAP plot failed: {exc}")
    return dfv


def run_multihorizon_shap(df, events_by_intent, intent_ids, cfg, outdir):
    core.set_global_determinism(cfg['seed'])
    os.makedirs(outdir, exist_ok=True)
    intent = cfg['intent']
    ts_paths, te_ev_last, rows = {}, None, []
    for H in cfg['horizons']:
        print(f"=== Training MILD for H={H} ===", flush=True)
        m, ts_path, model, scaler, Xte, Tte, te_ev, feats = _train_one_horizon(df, events_by_intent, intent_ids, H, cfg, outdir)
        with open(os.path.join(outdir, f'metrics_{H}.json'), 'w') as f:
            json.dump(m, f, indent=2, default=float)
        ts_paths[H] = ts_path
        te_ev_last = te_ev
        _shap_for_alert(model, scaler, Xte, Tte, ts_path, intent, feats,
                        os.path.join(outdir, f"shap_{cfg['fold']}_{intent}_{H}.png"),
                        os.path.join(outdir, f"shap_{cfg['fold']}_{intent}_{H}.csv"))
    e = _pick_event_for_intent(te_ev_last, intent)
    if e is not None:
        s0, f0 = e['start'], e['failure_time']
        for H, p in ts_paths.items():
            d = pd.read_csv(p)
            t, r, tau = d['t'].values, d[f'ema_{intent}'].values, float(d[f'tau_{intent}'].values[0])
            k = np.where((t >= s0) & (t <= f0) & (r >= tau))[0]
            ta = t[k[0]] if len(k) else None
            rows.append({'H': H, 'event_start': s0, 'failure_time': f0, 't_alert': ta, 'threshold': tau,
                         'est_TTF_min': (f0 - ta) if ta is not None else np.nan})
    ttf = pd.DataFrame(rows).sort_values('H', ascending=False)
    ttf.to_csv(os.path.join(outdir, f"ttf_table_{cfg['fold']}_{intent}.csv"), index=False)
    return ttf


def plot_disambiguation(run_dir, fold, events_by_intent, out_png, margin=40):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    ts_path = os.path.join(run_dir, 'folds', f'fold{fold}_timeseries.csv')
    mpath = os.path.join(run_dir, 'folds', f'fold{fold}_metrics.json')
    if not os.path.exists(ts_path):
        print(f"[WARN] {ts_path} missing; skip plot")
        return None
    df = pd.read_csv(ts_path)
    th = json.load(open(mpath)).get('params', {}) if os.path.exists(mpath) else {}
    te = core.filter_events(events_by_intent, int(df['t'].min()), int(df['t'].max()))
    cause = victim = e = None
    for root in te:
        for ev in te[root]:
            ty = ev.get('type', '')
            if ty.startswith('conflicting_') and ty.endswith('_cause'):
                for vn, vl in te.items():
                    for v in vl:
                        if v.get('type', '') == 'conflicting_victim' and not (v['end'] < ev['start'] or v['start'] > ev['end']):
                            cause, victim, e = root, vn, ev
                            break
                    if e: break
            if e: break
        if e: break
    if e is None:
        print("[WARN] no co-drift event in this fold")
        return None
    t0, t1 = max(int(df['t'].min()), e['start'] - margin), min(int(df['t'].max()), e['end'] + margin)
    d = df[(df['t'] >= t0) & (df['t'] <= t1)]
    fig, ax1 = plt.subplots(figsize=(8, 9))
    ax1.plot(d['t'], d[f'ema_{cause}'], color='#E67E22', lw=3, label='Cause Risk Score')
    ax1.plot(d['t'], d[f'ema_{victim}'], color='#16A085', lw=3, label='Victim Risk Score')
    for n, c, lab in ((cause, '#E67E22', 'Cause'), (victim, '#16A085', 'Victim')):
        if n in th:
            ax1.hlines(th[n]['tau'], d['t'].iloc[0], d['t'].iloc[-1], colors=c, linestyles=(0, (5, 3)), lw=2,
                       label=f'Alarm Threshold ({lab})')
    ax1.axvspan(e['start'], e['end'], color='#D6DBDF', alpha=0.35, hatch='///', label='Failure Propagation Window')
    ax1.axvline(e['failure_time'], color='k', lw=2.5, ls='-.', label='Failure Time')
    ax1.set_ylim(-0.02, 1.05)
    ax1.set_xlabel('Time (minutes)', fontsize=20)
    ax1.set_ylabel('Smoothed Risk Score', fontsize=20)
    ax2 = ax1.twinx()
    ax2.plot(d['t'], d[f'gate_{cause}'], color='#E67E22', ls='-.', lw=2, label='Gating Probability (Cause)')
    ax2.plot(d['t'], d[f'gate_{victim}'], color='#16A085', ls='-.', lw=2, label='Gating Probability (Victim)')
    ax2.set_ylim(-0.02, 1.05)
    ax2.set_ylabel('Gating Probability', fontsize=20)
    h1, l1 = ax1.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax1.legend(h1 + h2, l1 + l2, loc='upper left', fontsize=12, framealpha=0.3)
    fig.tight_layout()
    fig.savefig(out_png, dpi=300, bbox_inches='tight')
    plt.close(fig)
    win = (d['t'] >= e['start']) & (d['t'] <= e['failure_time'])
    return {'cause': cause, 'victim': victim, 'start': e['start'], 'failure_time': e['failure_time'],
            'gate_cause_at_failure': float(d.loc[win, f'gate_{cause}'].iloc[-1]) if win.any() else np.nan,
            'gate_victim_at_failure': float(d.loc[win, f'gate_{victim}'].iloc[-1]) if win.any() else np.nan}
