"""
runners.py -- one function per method. Each runs the blocked cross-validation
of the original script and writes, per evaluated fold:

  folds/fold{i}_metrics.json   paper metrics + extended metrics + tuned (W, tau)
  folds/fold{i}_events.csv     one row per test event (used for significance)
  folds/fold{i}_days.csv       false-alert time steps per test day
  folds/fold{i}_timeseries.csv EWMA risk (and gate probabilities for MILD)

The method logic is identical to the original per-dataset scripts:
  run_mild      <- run_hybrid_experiment.py
  run_mlp       <- baseline_NEW_MLP.py
  run_lstm      <- baseline_LSTM.py
  run_classic   <- baselines_multiintent_gem_corrected.py  (WKPI, Dist, LR-OvR)
"""
from __future__ import annotations

import json
import os
import time
from typing import Callable, Dict, List

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from . import core


# ----------------------------------------------------------------------------
# fold bookkeeping (resumable at fold level when allowed)
# ----------------------------------------------------------------------------
def _fold_paths(outdir, i):
    d = os.path.join(outdir, 'folds')
    os.makedirs(d, exist_ok=True)
    return {k: os.path.join(d, f'fold{i}_{k}.{ext}') for k, ext in
            [('metrics', 'json'), ('events', 'csv'), ('days', 'csv'), ('timeseries', 'csv')]}


def _save_fold(outdir, i, metrics, rec, days, ts):
    p = _fold_paths(outdir, i)
    rec.to_csv(p['events'], index=False)
    days.to_csv(p['days'], index=False)
    ts.to_csv(p['timeseries'], index=False)
    with open(p['metrics'] + '.tmp', 'w') as f:
        json.dump(metrics, f, indent=2, default=float)
    os.replace(p['metrics'] + '.tmp', p['metrics'])   # metrics file written last = fold complete


def _load_fold(outdir, i):
    p = _fold_paths(outdir, i)
    if os.path.exists(p['metrics']):
        with open(p['metrics']) as f:
            return json.load(f)
    return None


def _log(msg, logf=None):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)


def _iterate_folds(df, cfg, outdir, min_train, fold_fn: Callable):
    """Shared expanding-window loop. Returns list of fold metrics."""
    N = len(df)
    tests = _tests(N, cfg)
    out = []
    for i, (s, e) in enumerate(tests):
        fold_no = i + 1
        _log(f"Fold {fold_no}/{len(tests)}: test [{s}:{e})")
        if s < min_train:
            _log("  [skip] not enough train history before this test block.")
            continue
        if cfg.get('allow_fold_resume', False):
            done = _load_fold(outdir, fold_no)
            if done is not None:
                _log("  [resume] fold already complete, loaded from disk.")
                out.append(done)
                continue
        m, rec, days, ts = fold_fn(fold_no, s, e)
        m['fold'] = fold_no
        rec.insert(0, 'fold', fold_no)
        days.insert(0, 'fold', fold_no)
        _save_fold(outdir, fold_no, m, rec, days, ts)
        out.append(m)
    return out


def _tests(N, cfg):
    """Blocked expanding-window folds, or a single held-out block when
    cfg['test_range'] = [start_row, end_row] is given (used by autotrain)."""
    if cfg.get('test_range'):
        s, e = cfg['test_range']
        return [(int(s), int(min(e, N)))]
    return core.blocked_folds(N, cfg['folds'])


# ----------------------------------------------------------------------------
# MILD
# ----------------------------------------------------------------------------
def run_mild(df, events_by_intent, intent_ids, cfg, outdir):
    import tensorflow as tf
    from .models import (create_mild_moe_with_teacher, distillation_focal_with_temperature,
                         gate_kldiv_with_teacher, head_decorrelation)
    core.set_global_determinism(cfg['seed'])
    H = cfg['horizon']

    def fold_fn(fold_no, s, e):
        df_test = df.iloc[s:e].reset_index(drop=True)
        df_train_full = df.iloc[:s].reset_index(drop=True)
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

        _log("  Training teacher (Logistic OvR) ...")
        K = len(intent_ids)
        P_tr = np.zeros((len(Xtr), K), dtype='float32')
        P_va = np.zeros((len(Xva), K), dtype='float32')
        P_te = np.zeros((len(Xte), K), dtype='float32')
        Ytr_bin_fold = core.build_binary_labels(df_train, tr_ev, intent_ids, H)
        for j, name in enumerate(intent_ids):
            if np.unique(Ytr_bin_fold[:, j]).size < 2:
                _log(f"  [WARN] No positives for intent '{name}' in this fold.")
                continue
            lr = LogisticRegression(max_iter=cfg.get('teacher_max_iter', 600), random_state=cfg['seed'],
                                    class_weight='balanced')
            lr.fit(Xtr, Ytr_bin_fold[:, j])
            P_tr[:, j] = lr.predict_proba(Xtr)[:, 1]
            P_va[:, j] = lr.predict_proba(Xva)[:, 1]
            P_te[:, j] = lr.predict_proba(Xte)[:, 1]
        D_tr = core.prob_to_dist(P_tr, T=cfg['temp'])
        D_va = core.prob_to_dist(P_va, T=cfg['temp'])
        D_te = core.prob_to_dist(P_te, T=cfg['temp'])

        ytr, yva = {}, {}
        for j, name in enumerate(intent_ids):
            ytr[f'{name}_out'] = np.concatenate([Ytr_ttf[:, j:j + 1], P_tr[:, j:j + 1]], axis=1)
            yva[f'{name}_out'] = np.concatenate([Yva_ttf[:, j:j + 1], P_va[:, j:j + 1]], axis=1)
        ytr['gate'] = np.concatenate([Ytr_cause.astype('float32'), D_tr], axis=1)
        yva['gate'] = np.concatenate([Yva_cause.astype('float32'), D_va], axis=1)
        ytr['head_concat'] = np.zeros((len(Xtr), K * 16), dtype='float32')
        yva['head_concat'] = np.zeros((len(Xva), K * 16), dtype='float32')

        tf.keras.backend.clear_session()
        model = create_mild_moe_with_teacher(num_features=Xtr.shape[1], num_teacher=K, intent_ids=intent_ids,
                                             use_teacher_gate=cfg['use_teacher_gate'])
        losses = {f'{n}_out': distillation_focal_with_temperature(alpha=cfg['distill_alpha'], horizon_min=H,
                                                                  temperature=cfg['temp']) for n in intent_ids}
        losses['gate'] = gate_kldiv_with_teacher(supervise_weight=cfg['gate_supervise'],
                                                 teacher_weight=cfg['gate_teacher'],
                                                 sparsity_weight=cfg['gate_sparsity'])
        losses['head_concat'] = head_decorrelation(cfg.get('lambda_decorr', 1e-4))
        model.compile(optimizer=tf.keras.optimizers.Adam(1e-3), loss=losses)
        cb = [tf.keras.callbacks.EarlyStopping(monitor='val_loss', patience=cfg.get('patience', 8),
                                               restore_best_weights=True)]
        if cfg['use_teacher_gate']:
            tri, vai, tei = [Xtr, D_tr], [Xva, D_va], [Xte, D_te]
        else:
            tri, vai, tei = Xtr, Xva, Xte
        _log("  Training student (MILD) ...")
        model.fit(tri, ytr, epochs=cfg['epochs'], batch_size=cfg['batch'], validation_data=(vai, yva),
                  callbacks=cb, verbose=0)
        preds_va = model.predict(vai, verbose=0)
        preds_te = model.predict(tei, verbose=0)
        gate_te = np.asarray(preds_te[K])

        params = {name: core.grid_search_params(np.asarray(preds_va[j]).reshape(-1), df_val['t'].values,
                                                va_ev, name, fp_budget_per_day=cfg['fp_budget'])
                  for j, name in enumerate(intent_ids)}
        Tte_vals = df_test['t'].values
        scores = {name: np.asarray(preds_te[j]).reshape(-1) for j, name in enumerate(intent_ids)}
        ema, alerts = core.apply_policy(scores, Tte_vals, params)
        m, rec, days = core.evaluate(alerts, te_ev, Tte_vals, ema, gate_probs=gate_te, fold_t0=Tte_vals[0])
        m['params'] = params
        ts = pd.DataFrame({'t': Tte_vals, **{f'ema_{n}': ema[n] for n in intent_ids},
                           **{f'gate_{n}': gate_te[:, j] for j, n in enumerate(intent_ids)},
                           **{f'raw_{n}': scores[n] for n in intent_ids}})
        return m, rec, days, ts

    return _iterate_folds(df, cfg, outdir, 1000, fold_fn)


# ----------------------------------------------------------------------------
# MLP baseline
# ----------------------------------------------------------------------------
def run_mlp(df, events_by_intent, intent_ids, cfg, outdir):
    import tensorflow as tf
    from .models import create_mlp_baseline
    core.set_global_determinism(cfg['seed'])
    feats = core.BASE_FEATURES

    def fold_fn(fold_no, s, e):
        df_test = df.iloc[s:e].reset_index(drop=True)
        df_train_full = df.iloc[:s].reset_index(drop=True)
        split = int(0.8 * len(df_train_full))
        df_train = df_train_full.iloc[:split].reset_index(drop=True)
        df_val = df_train_full.iloc[split:].reset_index(drop=True)
        tr_ev = core.filter_events(events_by_intent, df_train['t'].min(), df_train['t'].max())
        va_ev = core.filter_events(events_by_intent, df_val['t'].min(), df_val['t'].max())
        te_ev = core.filter_events(events_by_intent, df_test['t'].min(), df_test['t'].max())
        Tva, Tte = df_val['t'].values, df_test['t'].values
        scaler = StandardScaler().fit(df_train[feats].values)
        Xtr, Xva, Xte = (scaler.transform(d[feats].values) for d in (df_train, df_val, df_test))
        Ytr = core.build_binary_labels(df_train, tr_ev, intent_ids, cfg['horizon'])
        Yva = core.build_binary_labels(df_val, va_ev, intent_ids, cfg['horizon'])
        tf.keras.backend.clear_session()
        model = create_mlp_baseline(Xtr.shape[1], intent_ids)
        model.compile(optimizer=tf.keras.optimizers.Adam(1e-3), loss='binary_crossentropy')
        cb = [tf.keras.callbacks.EarlyStopping(monitor='val_loss', patience=8, restore_best_weights=True)]
        _log("  Training MLP baseline ...")
        model.fit(Xtr, {f'{n}_out': Ytr[:, j] for j, n in enumerate(intent_ids)}, epochs=cfg['epochs'],
                  batch_size=cfg['batch'], validation_data=(Xva, {f'{n}_out': Yva[:, j] for j, n in enumerate(intent_ids)}),
                  callbacks=cb, verbose=0)
        pva = model.predict(Xva, verbose=0)
        pte = model.predict(Xte, verbose=0)
        params = {n: core.grid_search_params(np.asarray(pva[j]).reshape(-1), Tva, va_ev, n, cfg['fp_budget'])
                  for j, n in enumerate(intent_ids)}
        scores = {n: np.asarray(pte[j]).reshape(-1) for j, n in enumerate(intent_ids)}
        ema, alerts = core.apply_policy(scores, Tte, params)
        m, rec, days = core.evaluate(alerts, te_ev, Tte, ema, fold_t0=Tte[0])
        m['params'] = params
        ts = pd.DataFrame({'t': Tte, **{f'ema_{n}': ema[n] for n in intent_ids}})
        return m, rec, days, ts

    return _iterate_folds(df, cfg, outdir, 1000, fold_fn)


# ----------------------------------------------------------------------------
# LSTM baseline
# ----------------------------------------------------------------------------
def run_lstm(df, events_by_intent, intent_ids, cfg, outdir):
    import tensorflow as tf
    from numpy.lib.stride_tricks import sliding_window_view
    from .models import create_lstm_baseline
    core.set_global_determinism(cfg['seed'])
    feats = core.BASE_FEATURES
    L = cfg['lookback']

    def fold_fn(fold_no, s, e):
        df_test = df.iloc[s:e].reset_index(drop=True)
        df_train_full = df.iloc[:s].reset_index(drop=True)
        split = int(0.8 * len(df_train_full))
        df_train = df_train_full.iloc[:split].reset_index(drop=True)
        df_val = df_train_full.iloc[split:].reset_index(drop=True)
        scaler = StandardScaler().fit(df_train[feats].values)

        def prep(df_part, evs_part):
            X_raw = scaler.transform(df_part[feats].values)
            Y_raw = core.build_binary_labels(df_part, evs_part, intent_ids, cfg['horizon'])
            X_seq = np.swapaxes(sliding_window_view(X_raw, window_shape=L, axis=0), 1, 2)
            X_seq = X_seq[:-1] if len(X_seq) > (len(X_raw) - L) else X_seq
            Y_seq = Y_raw[L - 1:]
            T_seq = df_part['t'].values[L - 1:]
            n = min(len(X_seq), len(Y_seq), len(T_seq))
            return np.ascontiguousarray(X_seq[:n], dtype=np.float32), Y_seq[:n], T_seq[:n]

        tr_ev = core.filter_events(events_by_intent, df_train['t'].min(), df_train['t'].max())
        va_ev = core.filter_events(events_by_intent, df_val['t'].min(), df_val['t'].max())
        te_ev = core.filter_events(events_by_intent, df_test['t'].min(), df_test['t'].max())
        Xtr, Ytr, _ = prep(df_train, tr_ev)
        Xva, Yva, Tva = prep(df_val, va_ev)
        Xte, Yte, Tte = prep(df_test, te_ev)
        tf.keras.backend.clear_session()
        model = create_lstm_baseline(input_shape=(L, len(feats)), intent_ids=intent_ids)
        model.compile(optimizer=tf.keras.optimizers.Adam(1e-3), loss='binary_crossentropy')
        cb = [tf.keras.callbacks.EarlyStopping(monitor='val_loss', patience=6, restore_best_weights=True)]
        _log("  Training LSTM baseline ...")
        model.fit(Xtr, {f'{n}_out': Ytr[:, j] for j, n in enumerate(intent_ids)}, epochs=cfg['epochs'],
                  batch_size=cfg['batch'], validation_data=(Xva, {f'{n}_out': Yva[:, j] for j, n in enumerate(intent_ids)}),
                  callbacks=cb, verbose=0)
        pva = model.predict(Xva, verbose=0)
        pte = model.predict(Xte, verbose=0)
        params = {n: core.grid_search_params(np.asarray(pva[j]).reshape(-1), Tva, va_ev, n, cfg['fp_budget'])
                  for j, n in enumerate(intent_ids)}
        scores = {n: np.asarray(pte[j]).reshape(-1) for j, n in enumerate(intent_ids)}
        ema, alerts = core.apply_policy(scores, Tte, params)
        m, rec, days = core.evaluate(alerts, te_ev, Tte, ema, fold_t0=df_test['t'].values[0])
        m['params'] = params
        ts = pd.DataFrame({'t': Tte, **{f'ema_{n}': ema[n] for n in intent_ids}})
        return m, rec, days, ts

    return _iterate_folds(df, cfg, outdir, L + 200, fold_fn)


# ----------------------------------------------------------------------------
# classical baselines: WKPI-Tuned, Dist-Target, LR-OvR (one job, three methods)
# ----------------------------------------------------------------------------
CLASSIC_METHODS = ['wkpi', 'dist', 'lr']


def run_classic(df, events_by_intent, intent_ids, cfg, outdir_by_method: Dict[str, str]):
    core.set_global_determinism(cfg['seed'])
    feats = core.BASE_FEATURES
    Hlab = cfg.get('label_horizon', 120)   # the original script hard-codes 120 for every dataset
    N = len(df)
    tests = _tests(N, cfg)
    results = {m: [] for m in CLASSIC_METHODS}
    feat_sel = {'telemetry': ['sri', 'snet', 'ram_pct', 'cpu_pct', 'telemetry_queue'],
                'api': ['api_latency', 'cpu_pct', 'sri'],
                'analytics': ['snet', 'analytics_tput', 'cpu_pct']}
    lower_is_bad = {'sri', 'snet', 'analytics_tput'}

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
        scaler = StandardScaler().fit(df_train[feats].values)
        Xtr = scaler.transform(df_train[feats].values)
        Xte = scaler.transform(df_test[feats].values)
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
            if r.max() == r.min():
                return pd.Series(0.0, index=Z.index)
            return (r - r.min()) / (r.max() - r.min() + 1e-9)

        sc_tr = {}
        sc_te = {}
        for k in intent_ids:
            wts = {f: coefs[k][fi] for fi, f in enumerate(feats)}
            sc_tr[k] = risk_weighted(Xtr_df, wts).values
            sc_te[k] = risk_weighted(Xte_df, wts).values
        _classic_eval('wkpi', sc_tr, sc_te, Ttr, Tte, tr_ev, te_ev, intent_ids, cfg, fold_no, outdir_by_method, results)

        # ---- Dist-Target ----
        excl = set()
        for evs in tr_ev.values():
            for e_ in evs:
                a = max(int(df_train['t'].min()), e_['start'] - 30)
                b = min(int(df_train['t'].max()), e_['end'] + 30)
                excl.update(range(a, b + 1))
        golden = df_train[~df_train['t'].isin(excl)].index
        target_unscaled = df_train[feats].mean() if len(golden) < 100 else df_train.loc[golden, feats].mean()
        target_scaled = dict(zip(feats, scaler.transform(target_unscaled.values.reshape(1, -1))[0]))
        sc_tr, sc_te = {}, {}
        for k in intent_ids:
            sub = feat_sel[k]
            tv = np.array([target_scaled[f] for f in sub])
            for S, Z in ((sc_tr, Xtr_df), (sc_te, Xte_df)):
                d = np.sqrt(np.sum((Z[sub].values - tv) ** 2, axis=1))
                lo, hi = d.min(), d.max()
                S[k] = (d - lo) / (hi - lo + 1e-9) if (hi - lo) > 1e-9 else np.zeros_like(d)
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


def _classic_eval(method, sc_tr, sc_te, Ttr, Tte, tr_ev, te_ev, intent_ids, cfg, fold_no, outdirs, results):
    # NOTE: as in the original script, thresholds are tuned on the TRAINING scores
    params = {k: core.grid_search_params(sc_tr[k], Ttr, tr_ev, k, cfg['fp_budget']) for k in intent_ids}
    ema, alerts = core.apply_policy(sc_te, Tte, params)
    m, rec, days = core.evaluate(alerts, te_ev, Tte, ema, fold_t0=Tte[0])
    m['params'] = params
    m['fold'] = fold_no
    rec.insert(0, 'fold', fold_no)
    days.insert(0, 'fold', fold_no)
    ts = pd.DataFrame({'t': Tte, **{f'ema_{n}': ema[n] for n in intent_ids}})
    _save_fold(outdirs[method], fold_no, m, rec, days, ts)
    results[method].append(m)
