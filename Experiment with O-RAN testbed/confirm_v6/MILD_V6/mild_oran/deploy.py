"""deploy.py -- export a deployable MILD model and run it online (Algorithm 1 of the paper).

  mild-oran export   (train_now output -> <out>/deploy/)   fits MILD once with the configuration
                     selected by autotrain (method MILD, variant tuned) on the DEVELOPMENT part
                     of the dataset, exactly as runners.run_mild does for a test block: 80/20
                     train/validation split, class-balanced logistic teachers, student MoE, alert
                     thresholds (EWMA span W, tau) tuned on the validation block under the FP
                     budget. Saves the network, the scaler, the teachers and the thresholds.

  MildOnline         Algorithm 1: for every new KPI row, engineer the features from the last 15
                     rows, compute the teacher scores and the gate input, run MILD, smooth the
                     risks with the EWMA, alert when the smoothed risk exceeds tau, name the
                     root-cause intent by the gate arg-max, and list the KPIs that drive the
                     root-cause risk (gradient x input attribution; SHAP stays an offline tool).
"""
from __future__ import annotations

import json
import os
import sys
from collections import deque

import numpy as np
import pandas as pd

from . import util

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _repro():
    p = os.path.join(ROOT, 'MILD_unified_repro')
    if p not in sys.path:
        sys.path.insert(0, p)


def selected_config(train_out):
    fr = pd.read_csv(os.path.join(train_out, 'training', 'final_test_runs.csv'))
    r = fr[(fr['method'] == 'MILD') & (fr['variant'] == 'tuned')]
    if r.empty:
        raise SystemExit('no tuned MILD run in final_test_runs.csv: run `mild-oran train` first')
    cfg = json.loads(r.iloc[0]['cfg'])
    for k in ('test_range', 'row_limit', 'fp_block_ticks'):
        cfg.pop(k, None)
    return cfg


def export(train_out, seed=42):
    _repro()
    import tensorflow as tf
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    from mild_repro import core
    from mild_repro.models import (create_mild_moe_with_teacher, distillation_focal_with_temperature,
                                   gate_kldiv_with_teacher, head_decorrelation)
    cfg = selected_config(train_out)
    cfg['seed'] = seed
    ds = os.path.join(train_out, 'dataset')
    df, events, intents = core.load_dataset(os.path.join(ds, 'dataset.csv'), os.path.join(ds, 'events_by_intent.json'))
    aud = util.load_json(os.path.join(train_out, 'training', 'audit.json'), {}) or {}
    dev_end = int(aud.get('dev_rows', int(0.7 * len(df))))
    core.set_global_determinism(seed)
    H = int(cfg['horizon'])
    full = df.iloc[:dev_end].reset_index(drop=True)
    split = int(0.8 * len(full))
    dtr, dva = full.iloc[:split].reset_index(drop=True), full.iloc[split:].reset_index(drop=True)
    tr_ev = core.filter_events(events, dtr['t'].min(), dtr['t'].max())
    va_ev = core.filter_events(events, dva['t'].min(), dva['t'].max())
    Xtr, Ytr_ttf, _, Ytr_cause, _, feats = core.make_forward_looking_dataset(dtr, tr_ev, intents, H, onset=core.onset_mode(cfg))
    Xva, Yva_ttf, _, Yva_cause, _, _ = core.make_forward_looking_dataset(dva, va_ev, intents, H, onset=core.onset_mode(cfg))
    sc = StandardScaler().fit(Xtr)
    Xtr, Xva = sc.transform(Xtr), sc.transform(Xva)
    K = len(intents)
    Ybin = core.build_binary_labels(dtr, tr_ev, intents, H)
    teachers = {}
    P_tr, P_va = np.zeros((len(Xtr), K), 'float32'), np.zeros((len(Xva), K), 'float32')
    for j, n in enumerate(intents):
        if np.unique(Ybin[:, j]).size < 2:
            continue
        lr = LogisticRegression(max_iter=cfg.get('teacher_max_iter', 600), random_state=seed, class_weight='balanced')
        lr.fit(Xtr, Ybin[:, j])
        teachers[n] = {'coef': lr.coef_[0].tolist(), 'intercept': float(lr.intercept_[0])}
        P_tr[:, j], P_va[:, j] = lr.predict_proba(Xtr)[:, 1], lr.predict_proba(Xva)[:, 1]
    D_tr, D_va = core.prob_to_dist(P_tr, T=cfg['temp']), core.prob_to_dist(P_va, T=cfg['temp'])
    ytr, yva = {}, {}
    for j, n in enumerate(intents):
        ytr[f'{n}_out'] = np.concatenate([Ytr_ttf[:, j:j + 1], P_tr[:, j:j + 1]], axis=1)
        yva[f'{n}_out'] = np.concatenate([Yva_ttf[:, j:j + 1], P_va[:, j:j + 1]], axis=1)
    ytr['gate'] = np.concatenate([Ytr_cause.astype('float32'), D_tr], axis=1)
    yva['gate'] = np.concatenate([Yva_cause.astype('float32'), D_va], axis=1)
    ytr['head_concat'] = np.zeros((len(Xtr), K * 16), 'float32')
    yva['head_concat'] = np.zeros((len(Xva), K * 16), 'float32')
    tf.keras.backend.clear_session()
    model = create_mild_moe_with_teacher(Xtr.shape[1], K, intents, use_teacher_gate=cfg['use_teacher_gate'],
                                         gate_mode=cfg.get('gate_mode', 'multiply'))
    losses = {f'{n}_out': distillation_focal_with_temperature(alpha=cfg['distill_alpha'], horizon_min=H,
                                                              temperature=cfg['temp']) for n in intents}
    losses['gate'] = gate_kldiv_with_teacher(cfg['gate_supervise'], cfg['gate_teacher'], cfg['gate_sparsity'])
    losses['head_concat'] = head_decorrelation(cfg.get('lambda_decorr', 1e-4))
    model.compile(optimizer=tf.keras.optimizers.Adam(1e-3), loss=losses)
    tri = [Xtr, D_tr] if cfg['use_teacher_gate'] else Xtr
    vai = [Xva, D_va] if cfg['use_teacher_gate'] else Xva
    model.fit(tri, ytr, epochs=cfg['epochs'], batch_size=cfg['batch'], validation_data=(vai, yva), verbose=0,
              callbacks=[tf.keras.callbacks.EarlyStopping(monitor='val_loss', patience=cfg.get('patience', 8),
                                                          restore_best_weights=True)])
    pv = model.predict(vai, verbose=0)
    params = {n: core.grid_search_params(np.asarray(pv[j]).reshape(-1), dva['t'].values, va_ev, n, cfg['fp_budget'])
              for j, n in enumerate(intents)}
    out = os.path.join(train_out, 'deploy')
    os.makedirs(out, exist_ok=True)
    model.save_weights(os.path.join(out, 'mild.weights.h5'))
    util.save_json(os.path.join(out, 'mild_deploy.json'), {
        'config': cfg, 'intents': intents, 'features': feats, 'scaler_mean': sc.mean_.tolist(),
        'scaler_scale': sc.scale_.tolist(), 'teachers': teachers, 'thresholds': params,
        'n_features': int(Xtr.shape[1]), 'dev_rows': dev_end})
    return out, params


class MildOnline:
    """Algorithm 1 on a stream of KPI rows (dicts with t, the 8 KPIs, cpu_delta, sri_delta)."""

    def __init__(self, deploy_dir):
        _repro()
        import tensorflow as tf
        from mild_repro import core
        from mild_repro.models import create_mild_moe_with_teacher
        self.tf, self.core = tf, core
        d = util.load_json(os.path.join(deploy_dir, 'mild_deploy.json'))
        self.d = d
        self.intents = d['intents']
        self.feats = d['features']
        self.mu = np.array(d['scaler_mean'], 'float32')
        self.sd = np.array(d['scaler_scale'], 'float32')
        self.cfg = d['config']
        self.model = create_mild_moe_with_teacher(d['n_features'], len(self.intents), self.intents,
                                                  use_teacher_gate=self.cfg['use_teacher_gate'],
                                                  gate_mode=self.cfg.get('gate_mode', 'multiply'))
        self.model.load_weights(os.path.join(deploy_dir, 'mild.weights.h5'))
        self.hist = deque(maxlen=60 if self.cfg.get('features') in ('onset', 'trend') else 15)
        self.extra = [f for f in getattr(core, 'EXTRA_FEATURES', []) if f in self.feats]
        self.ema = {n: None for n in self.intents}
        from .kpi_norm import RamExcess
        self.ram = RamExcess()                     # the dataset's ram_pct is the excess over the recent floor

    def step(self, row):
        core = self.core
        row = dict(row)
        row['ram_pct'] = self.ram.update(row.get('ram_pct'))
        if 'load_pct' not in row and row.get('ran_prb_dl_pct') is not None:
            row['load_pct'] = row['ran_prb_dl_pct']          # scenario v4: the cell load is a model input
        self.hist.append({k: row.get(k) for k in ['t'] + core.BASE_FEATURES + self.extra})
        df = pd.DataFrame(list(self.hist)).ffill().fillna(0.0)
        X, feats = core.engineer(df, onset=core.onset_mode(self.cfg))
        x = ((X.values[-1:].astype('float32') - self.mu) / self.sd).astype('float32')
        P = np.zeros((1, len(self.intents)), 'float32')
        for j, n in enumerate(self.intents):
            t = self.d['teachers'].get(n)
            if t:
                z = float(np.dot(x[0], np.array(t['coef'], 'float32')) + t['intercept'])
                P[0, j] = 1.0 / (1.0 + np.exp(-z))
        D = core.prob_to_dist(P, T=self.cfg['temp']).astype('float32')
        inp = [x, D] if self.cfg['use_teacher_gate'] else x
        out = self.model(inp, training=False)
        p = {n: float(np.asarray(out[j]).reshape(-1)[0]) for j, n in enumerate(self.intents)}
        g = np.asarray(out[len(self.intents)])[0]
        alerts = []
        for n in self.intents:
            W, tau = self.d['thresholds'][n]['W'], self.d['thresholds'][n]['tau']
            self.ema[n] = p[n] if self.ema[n] is None else self.ema[n] + 2.0 / (W + 1) * (p[n] - self.ema[n])
            if self.ema[n] >= tau:
                alerts.append(n)
        root = self.intents[int(np.argmax(g))]
        top = []
        if alerts:
            xt = self.tf.convert_to_tensor(x)
            with self.tf.GradientTape() as tape:
                tape.watch(xt)
                o = self.model([xt, self.tf.convert_to_tensor(D)] if self.cfg['use_teacher_gate'] else xt, training=False)
                y = o[self.intents.index(root)]
            gr = tape.gradient(y, xt).numpy()[0]
            contrib = gr * x[0]
            top = [(feats[k], round(float(contrib[k]), 4)) for k in np.argsort(-contrib)[:3]]
        return {'p': p, 'ema': dict(self.ema), 'gate': {n: float(g[j]) for j, n in enumerate(self.intents)},
                'alerts': alerts, 'root': root if alerts else None, 'top_kpis': top}


def main():
    import argparse
    ap = argparse.ArgumentParser(description='export a deployable MILD model from a `mild-oran train` folder')
    ap.add_argument('--train_out', default=os.path.expanduser('~/intact/mild/train'))
    ap.add_argument('--seed', type=int, default=42)
    a = ap.parse_args()
    out, params = export(os.path.abspath(os.path.expanduser(a.train_out)), a.seed)
    print(f'EXPORT DONE: {out}')
    print('alert thresholds (EWMA span W, tau) tuned on the validation block:')
    for k, v in params.items():
        print(f"  {k:10s} W={v['W']:3d} tau={v['tau']:.4f} (validation mean lead {v['lead']:.1f} ticks)")


if __name__ == '__main__':
    main()
