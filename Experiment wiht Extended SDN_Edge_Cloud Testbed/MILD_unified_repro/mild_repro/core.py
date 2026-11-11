"""
core.py -- shared, dataset-independent logic used by MILD and every baseline.

All functions here are faithful ports of the helpers that were duplicated in the
original per-dataset scripts (run_hybrid_experiment.py, baseline_NEW_MLP.py,
baseline_LSTM.py, baselines_multiintent_gem_corrected.py,
make_forward_looking_dataset.py). The logic is unchanged. Two things differ:

1. grid_search_params() and evaluate() are vectorised with NumPy. They return
   exactly the same values as the original Python loops (same W/tau grid, same
   strict ">" tie rule, same window semantics), only much faster.
2. evaluate() additionally returns per-event and per-day records, which the
   report stage uses for statistical significance tests and extended metrics.
   The paper metrics themselves are computed exactly as before.
"""
from __future__ import annotations

import json
import os
import random
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

BASE_FEATURES = ['cpu_pct', 'ram_pct', 'storage_pct', 'snet', 'sri',
                 'cpu_delta', 'sri_delta', 'api_latency', 'analytics_tput',
                 'telemetry_queue']
ROLL_KPIS = ['cpu_pct', 'ram_pct', 'sri', 'snet', 'api_latency',
             'analytics_tput', 'telemetry_queue']
EWMA_SPANS = [3, 5, 8, 13, 21, 34]
MINUTES_PER_DAY = 1440.0


# ----------------------------------------------------------------------------
# reproducibility
# ----------------------------------------------------------------------------
def set_global_determinism(seed: int = 42) -> None:
    """Same as the original determinism.py."""
    os.environ["PYTHONHASHSEED"] = str(seed)
    os.environ["TF_DETERMINISTIC_OPS"] = "1"
    os.environ["TF_CUDNN_DETERMINISTIC"] = "1"
    os.environ["TF_ENABLE_ONEDNN_OPTS"] = "0"
    random.seed(seed)
    np.random.seed(seed)
    try:
        import tensorflow as tf
        tf.random.set_seed(seed)
        tf.keras.utils.set_random_seed(seed)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# data / folds / events
# ----------------------------------------------------------------------------
def read_json(p):
    with open(p, 'r') as f:
        return json.load(f)


def load_dataset(data_csv: str, events_json: str | None = None):
    df = pd.read_csv(data_csv)
    if events_json is None:
        events_json = os.path.join(os.path.dirname(data_csv), 'events_by_intent.json')
    events = read_json(events_json)
    intent_ids = sorted(events.keys())
    return df, events, intent_ids


def blocked_folds(N: int, folds: int) -> List[Tuple[int, int]]:
    """Expanding-window blocked CV exactly as in the original scripts."""
    fold_sizes = [N // folds] * (folds - 1) + [N - (folds - 1) * (N // folds)]
    starts = [sum(fold_sizes[:i]) for i in range(folds)]
    return [(s, s + fold_sizes[i]) for i, s in enumerate(starts)]


def filter_events(events_by_intent, t_min, t_max):
    subset = {k: [] for k in events_by_intent}
    for k, evs in events_by_intent.items():
        for e in evs:
            if (e['start'] <= t_max) and (e['end'] >= t_min):
                e2 = dict(e)
                e2['start'] = max(e['start'], int(t_min))
                e2['end'] = min(e['end'], int(t_max))
                e2['failure_time'] = min(max(e['failure_time'], e2['start'] + 1), e2['end'])
                subset[k].append(e2)
    return subset


# ----------------------------------------------------------------------------
# features and labels (make_forward_looking_dataset.py)
# ----------------------------------------------------------------------------
def engineer(df: pd.DataFrame, base_features: List[str] = BASE_FEATURES):
    df_e = df.copy()
    for k in ROLL_KPIS:
        for w in [5, 15]:
            df_e[f'{k}_mean_{w}'] = df_e[k].rolling(window=w, min_periods=1).mean()
            df_e[f'{k}_std_{w}'] = df_e[k].rolling(window=w, min_periods=1).std()
    df_e.fillna(0, inplace=True)
    eng_cols = [c for c in df_e.columns if c.endswith(('_mean_5', '_std_5', '_mean_15', '_std_15'))]
    feats = list(base_features) + eng_cols
    return df_e[feats], feats


def build_forward_labels(df, events_by_intent, intent_ids, horizon_min):
    N, K = len(df), len(intent_ids)
    y_ttf = np.zeros((N, K), dtype='float32')
    y_bin = np.zeros((N, K), dtype='float32')
    y_cause = np.zeros((N, K), dtype='float32')
    tvals = df['t'].values
    intent_to_idx = {name: i for i, name in enumerate(intent_ids)}

    def add_pos(j, failure_time, start):
        start = max(start, int(tvals[0]))
        end = min(failure_time, int(tvals[-1]) + 1)
        if end <= start:
            return
        mask = (tvals >= start) & (tvals < end)
        y_ttf[mask, j] = (failure_time - tvals[mask]).astype('float32')
        y_bin[mask, j] = 1.0

    for intent, evs in events_by_intent.items():
        j = intent_to_idx[intent]
        for e in evs:
            failure = e['failure_time']
            start = failure - horizon_min
            add_pos(j, failure, start)
            typ = e.get('type', '')
            if typ.startswith('conflicting') and typ.endswith('cause'):
                y_cause[(tvals >= start) & (tvals < failure), j] = 1.0
            elif typ.startswith('independent') or typ.startswith('nonlinear') or typ == 'correlated_hw':
                y_cause[(tvals >= start) & (tvals < failure), j] = 1.0
    return y_ttf, y_bin, y_cause


def make_forward_looking_dataset(df, events_by_intent, intent_ids, horizon_min,
                                 base_features=BASE_FEATURES):
    Xdf, feats = engineer(df, base_features)
    Y_ttf, Y_bin, Y_cause = build_forward_labels(df, events_by_intent, intent_ids, horizon_min)
    T = df['t'].values.astype('int64')
    X = Xdf.values.astype('float32')
    return X, Y_ttf, Y_bin, Y_cause, T, feats


def build_binary_labels(df_slice, evs, intent_ids, horizon):
    """Binary labels used by teachers and baselines (failure_time - H <= t < failure_time)."""
    Y = np.zeros((len(df_slice), len(intent_ids)), dtype=np.float32)
    t = df_slice['t'].values
    for j, intent in enumerate(intent_ids):
        for ev in evs.get(intent, []):
            start = max(ev['failure_time'] - horizon, int(t[0]))
            end = min(ev['failure_time'], int(t[-1]) + 1)
            if end > start:
                Y[(t >= start) & (t < end), j] = 1.0
    return Y


def prob_to_dist(P, T=2.0, eps=1e-6):
    logits = np.log(np.clip(P, eps, 1 - eps)) - np.log(np.clip(1 - P, eps, 1 - eps))
    logits = logits / T
    m = np.max(logits, axis=1, keepdims=True)
    e = np.exp(logits - m)
    Z = np.sum(e, axis=1, keepdims=True) + eps
    return e / Z


# ----------------------------------------------------------------------------
# window helpers (vectorised)
# ----------------------------------------------------------------------------
def in_any_window(tvals: np.ndarray, windows) -> np.ndarray:
    """Boolean mask: tvals[i] lies in some closed window [s, e]."""
    mask = np.zeros(len(tvals), dtype=bool)
    if len(tvals) == 0:
        return mask
    for (s, e) in windows:
        lo = np.searchsorted(tvals, s, side='left')
        hi = np.searchsorted(tvals, e, side='right')
        if hi > lo:
            mask[lo:hi] = True
    return mask


def first_alert_in(alert_times: np.ndarray, lo, hi):
    """First element of sorted alert_times with lo <= a <= hi, or None."""
    k = np.searchsorted(alert_times, lo, side='left')
    if k < len(alert_times) and alert_times[k] <= hi:
        return alert_times[k]
    return None


def ewma(scores, tvals, W):
    return pd.Series(np.asarray(scores).reshape(-1), index=tvals).ewm(span=W, adjust=False).mean().values


# ----------------------------------------------------------------------------
# alert-policy tuning (identical results to original grid_search_params)
# ----------------------------------------------------------------------------
def grid_search_params(scores, tvals, evs_by_intent, intent, fp_budget_per_day=1.0):
    best = {'W': 5, 'tau': 0.5, 'lead': -1.0}
    evs = evs_by_intent.get(intent, [])
    if not evs:
        return best
    tvals = np.asarray(tvals)
    all_windows = [(e['start'], e['end']) for v in evs_by_intent.values() for e in v]
    inwin = in_any_window(tvals, all_windows)
    days = (tvals[-1] - tvals[0]) / MINUTES_PER_DAY if len(tvals) > 1 else 1.0
    ev_bounds = [(e['start'], e['failure_time']) for e in evs]
    for W in EWMA_SPANS:
        ema = ewma(scores, tvals, W)
        smin, smax = float(np.min(ema)), float(np.max(ema))
        if smax == smin:
            continue
        for tau in np.linspace(smin, smax, 60):
            amask = ema >= tau
            fp = int(np.count_nonzero(amask & ~inwin))
            fp_rate = fp / max(days, 1e-9)
            if fp_rate > fp_budget_per_day:
                continue
            alerts = tvals[amask]
            lt = []
            for (s, f) in ev_bounds:
                a0 = first_alert_in(alerts, s, f)
                if a0 is not None:
                    lt.append(f - a0)
            avg_lead = float(np.mean(lt)) if lt else 0.0
            if avg_lead > best['lead']:
                best = {'W': W, 'tau': float(tau), 'lead': avg_lead}
    return best


def apply_policy(scores_by_intent: Dict[str, np.ndarray], tvals, params):
    ema_by_intent, alerts = {}, {}
    for k, sc in scores_by_intent.items():
        W, tau = params[k]['W'], params[k]['tau']
        ema = ewma(sc, tvals, W)
        ema_by_intent[k] = ema
        alerts[k] = np.asarray(tvals)[ema >= tau]
    return ema_by_intent, alerts


# ----------------------------------------------------------------------------
# evaluation
# ----------------------------------------------------------------------------
def is_root_event_paper(event_type: str) -> bool:
    """Root-cause rule used by the paper's evaluator."""
    return event_type.endswith('_cause') or event_type.startswith('independent')


def is_root_event_all(event_type: str) -> bool:
    """Extended rule: every non-victim event is a root (adds nonlinear_*)."""
    return event_type != 'conflicting_victim'


def evaluate(per_intent_alerts, te_events, Tte, ema_by_intent, gate_probs=None,
             fold_t0=None):
    """
    Returns (paper_metrics_dict, event_records, day_records).

    paper metrics are computed exactly as in the original scripts:
      * detection: an alert of the event's own intent in [start, failure_time]
      * lead time: failure_time - first such alert
      * FP: number of alert time steps (summed over intents) outside every
        annotated window of every intent, divided by test days
      * disambiguation: over 'paper' root events (independent* or *_cause);
        earliest alert of ANY intent inside [start, failure_time]; predicted
        cause = gate argmax (MILD) or earliest-alerting intent with EMA
        tie-break (baselines); events with no alert count as wrong.
    """
    Tte = np.asarray(Tte)
    intent_ids = list(ema_by_intent.keys())
    time_to_idx = {int(t): idx for idx, t in enumerate(Tte)}
    all_windows = [(e_['start'], e_['end']) for v in te_events.values() for e_ in v]
    inwin = in_any_window(Tte, all_windows)

    tp = {k: 0 for k in te_events}
    fn = {k: 0 for k in te_events}
    lt = {k: [] for k in te_events}
    fp_total = 0
    fp_episodes = 0
    t_first = int(Tte[0]) if fold_t0 is None else int(fold_t0)
    # FP bins for the paired day-level test. One bin is one emulated day by default.
    # Short test sets (a few days) can use smaller blocks (MILD_FP_BIN_TICKS, set by
    # autotrain --fp_block); counts are then rescaled to FP steps per day.
    bin_w = float(os.environ.get('MILD_FP_BIN_TICKS', MINUTES_PER_DAY) or MINUTES_PER_DAY)
    n_day_bins = int((int(Tte[-1]) - t_first) // bin_w) + 1 if len(Tte) else 1
    fp_per_day_bins = np.zeros(n_day_bins, dtype=np.int64)

    for name, alerts in per_intent_alerts.items():
        alerts = np.asarray(alerts)
        for eobj in te_events.get(name, []):
            a0 = first_alert_in(alerts, eobj['start'], eobj['failure_time'])
            if a0 is not None:
                tp[name] += 1
                lt[name].append(int(eobj['failure_time'] - a0))
            else:
                fn[name] += 1
        if name in ema_by_intent:
            amask = np.zeros(len(Tte), dtype=bool)
            idx = np.searchsorted(Tte, alerts)
            amask[idx[(idx < len(Tte))]] = True
            fpm = amask & ~inwin
            fp_total += int(np.count_nonzero(fpm))
            # episodes = maximal runs of consecutive false-alert steps
            if fpm.any():
                d = np.diff(np.concatenate([[0], fpm.astype(np.int8)]))
                fp_episodes += int(np.count_nonzero(d == 1))
                day_idx = ((Tte[fpm] - t_first) // bin_w).astype(int)
                day_idx = np.clip(day_idx, 0, n_day_bins - 1)
                fp_per_day_bins += np.bincount(day_idx, minlength=n_day_bins)

    # ------------------------- per-event records -------------------------
    records = []
    correct_paper, total_paper, conf = 0, 0, {}
    for true_name, evs in te_events.items():
        for eobj in evs:
            et = eobj.get('type', '')
            own = per_intent_alerts.get(true_name, np.array([]))
            a_own = first_alert_in(np.asarray(own), eobj['start'], eobj['failure_time'])
            # earliest alert across intents (with tie set)
            earliest, tie = None, set()
            for name, arr in per_intent_alerts.items():
                a = first_alert_in(np.asarray(arr), eobj['start'], eobj['failure_time'])
                if a is not None:
                    if earliest is None or a < earliest:
                        earliest, tie = a, {name}
                    elif a == earliest:
                        tie.add(name)
            pred = None
            if earliest is not None:
                ia = time_to_idx.get(int(earliest), None)
                if ia is not None:
                    if gate_probs is not None:
                        pred = intent_ids[int(np.argmax(gate_probs[ia]))]
                    elif len(tie) == 1:
                        pred = list(tie)[0]
                    else:
                        best_score, pred = -np.inf, list(tie)[0]
                        for nm in tie:
                            sc = ema_by_intent[nm][ia]
                            if sc > best_score:
                                best_score, pred = sc, nm
            root_paper = is_root_event_paper(et)
            if root_paper:
                total_paper += 1
                if pred is not None and pred == true_name:
                    correct_paper += 1
                if pred is not None:
                    conf[f'{true_name}->{pred}'] = conf.get(f'{true_name}->{pred}', 0) + 1
            records.append({
                'intent': true_name, 'type': et, 'start': int(eobj['start']),
                'failure_time': int(eobj['failure_time']), 'end': int(eobj['end']),
                'detected': int(a_own is not None),
                'lead': (int(eobj['failure_time'] - a_own) if a_own is not None else np.nan),
                'root_paper': int(root_paper), 'root_all': int(is_root_event_all(et)),
                'earliest_alert': (int(earliest) if earliest is not None else np.nan),
                'pred_cause': pred if pred is not None else '',
                'cause_correct': int(pred is not None and pred == true_name),
            })

    days = (Tte[-1] - Tte[0]) / MINUTES_PER_DAY if len(Tte) > 1 else 1.0
    rec_df = pd.DataFrame(records)
    roots_all = rec_df[rec_df['root_all'] == 1] if len(rec_df) else rec_df
    metrics = {
        'per_intent': {
            name: {
                'detection_rate': tp[name] / max(tp[name] + fn[name], 1e-9),
                'avg_lead_time': float(np.mean(lt[name])) if lt[name] else 0.0,
                'tp': int(tp[name]), 'fn': int(fn[name]),
            } for name in te_events
        },
        'overall': {
            'fp_per_day': fp_total / max(days, 1e-9),
            'disamb_accuracy': (correct_paper / max(total_paper, 1)) if total_paper > 0 else 0.0,
            'num_cause_events': int(total_paper),
            # ---- extended (not in the paper) ----
            'fp_steps_total': int(fp_total),
            'fp_episodes_per_day': fp_episodes / max(days, 1e-9),
            'disamb_accuracy_all_roots': float(roots_all['cause_correct'].mean()) if len(roots_all) else 0.0,
            'num_roots_all': int(len(roots_all)),
            'test_days': float(days),
        },
        'confusion': conf,
    }
    day_df = pd.DataFrame({'day': np.arange(n_day_bins),
                           'fp_steps': fp_per_day_bins * (MINUTES_PER_DAY / bin_w) if bin_w != MINUTES_PER_DAY
                           else fp_per_day_bins, 'bin_ticks': bin_w})
    return metrics, rec_df, day_df


def aggregate_folds(fold_metrics: List[dict], intent_ids: List[str]) -> dict:
    """Mean/std over evaluated folds, as printed by the original scripts."""
    out = {'per_intent': {}, 'overall': {}}
    for name in intent_ids:
        dr = [f['per_intent'][name]['detection_rate'] for f in fold_metrics]
        lt = [f['per_intent'][name]['avg_lead_time'] for f in fold_metrics]
        out['per_intent'][name] = {
            'detection_mean': 100 * float(np.mean(dr)), 'detection_std': 100 * float(np.std(dr)),
            'lead_mean': float(np.mean(lt)), 'lead_std': float(np.std(lt)),
        }
    for key, scale in [('fp_per_day', 1), ('disamb_accuracy', 100), ('fp_episodes_per_day', 1),
                       ('disamb_accuracy_all_roots', 100)]:
        vals = [f['overall'].get(key, np.nan) for f in fold_metrics]
        out['overall'][key + '_mean'] = scale * float(np.nanmean(vals))
        out['overall'][key + '_std'] = scale * float(np.nanstd(vals))
    # The paper computed the macro averages from the per-intent values printed
    # with two decimals; the same rounding is applied here.
    out['macro'] = {
        'detection': float(np.mean([round(out['per_intent'][k]['detection_mean'], 2) for k in intent_ids])),
        'lead': float(np.mean([round(out['per_intent'][k]['lead_mean'], 2) for k in intent_ids])),
        'fp_per_day': out['overall']['fp_per_day_mean'],
        'disamb': out['overall']['disamb_accuracy_mean'],
        'fp_episodes_per_day': out['overall']['fp_episodes_per_day_mean'],
        'disamb_all_roots': out['overall']['disamb_accuracy_all_roots_mean'],
    }
    out['n_folds_evaluated'] = len(fold_metrics)
    return out
