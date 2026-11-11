#!/usr/bin/env python3
"""
train_now.py -- train MILD and the five baselines on the O-RAN data collected so far, without
touching a running collection, and say how far the results can be trusted (`mild-oran train`).

  <venv>/bin/python train_now.py --state ~/intact/mild/state --out ~/intact/mild/train

Steps (every step is recorded in --out; re-running continues where it stopped):
  1. snapshot     copies the FINISHED segments of --state (read-only access) into <out>/snapshot.
                  The segment that is still being collected is skipped. A snapshot is reused
                  until new segments are finished (or --refresh is given).
  2. dataset      merges the snapshot into dataset.csv + events_by_intent.json and writes the
                  quality report. dataset.csv is only rewritten when its content changed, so
                  finished training jobs are reused when you run the script again.
  3. suitability  12 checks (size, events, observed failures, label sanity, missing values,
                  drift, ...) -> SUITABLE / MARGINAL / UNSUITABLE. Training runs in every case
                  (unless --check_only); the verdict is carried into the trust report.
  4. train        MILD_unified_repro/autotrain.py: chronological development/test split at quiet
                  points, the same trial budget for every method, held-out test with several
                  seeds, paired significance tests with Holm correction, equal-FP comparison.
                  Every trial is a cached job, so an interrupted run continues where it stopped.
  5. trust        TRUST_REPORT.md: 10 criteria deciding whether the numbers can go into the paper,
                  and which comparative claims the tests support.
                  PAPER-READY / USABLE WITH CAVEATS / NOT TRUSTWORTHY.

Options:
  --quick            fast first look: 6 trials, 1 seed, no re-ranking (about 20-40 min on a CPU)
  --max_rows N       use only the first N clean rows (in collection order)
  --check_only       stop after step 3
  --refresh          rebuild the snapshot even if no new segment is finished
  --trials/--seeds/--fp_budget/--fp_block/--test_frac   training settings
  --nice N           run training with this nice level (default 10, protects a running collection)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mild_oran import util  # noqa: E402
from mild_oran.collector import SegmentStore  # noqa: E402
from mild_oran.kpi import KPI_NAMES  # noqa: E402
from mild_oran.quality import assess  # noqa: E402

LOG = {'f': None}


def log(msg):
    util.log(msg)


def sha(path):
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def load_cfg(state):
    used = util.load_json(os.path.join(state, 'config_used.json'))
    return used or json.load(open(os.path.join(HERE, 'config', 'oran.json')))


# =============================================================================
# 1-2. snapshot and dataset
# =============================================================================
def snapshot(state, out, refresh):
    src = SegmentStore(state)
    segs = src.finished()
    if not segs:
        raise SystemExit('No finished segment in the state folder yet.')
    sig = [[i, src.meta(i).get('ticks'), src.meta(i).get('status')] for i in segs]
    att = os.path.join(state, 'attempts')
    archived = sorted(os.listdir(att)) if os.path.isdir(att) else []
    sig.append(['archived_attempts', archived])
    snap = os.path.join(out, 'snapshot')
    man_p = os.path.join(snap, 'snapshot_manifest.json')
    man = util.load_json(man_p, {}) or {}
    if not refresh and man.get('segments') == sig:
        log(f'snapshot: reusing ({len(sig)} finished segments, nothing new)')
        return snap
    if os.path.exists(snap):
        shutil.rmtree(snap)
    os.makedirs(os.path.join(snap, 'segments'))
    for i in segs:
        shutil.copytree(src.path(i), os.path.join(snap, 'segments', f'seg_{i:04d}'))
    if archived:                                       # earlier attempts: their good data is merged too
        shutil.copytree(att, os.path.join(snap, 'attempts'), ignore=shutil.ignore_patterns('*.after_cut', 'learncheck'))
    for f in ('calibration.json', 'calibration_report.md', 'current_scales.json', 'scale_history.json',
              'config_used.json', 'segment_verdicts.csv'):
        p = os.path.join(state, f)
        if os.path.exists(p):
            shutil.copy(p, snap)
    util.save_json(man_p, {'created': time.strftime('%Y-%m-%d %H:%M:%S'), 'source': os.path.abspath(state),
                           'segments': sig})
    log(f'snapshot: copied {len(segs)} finished segments')
    return snap


def limit_rows(snap, max_rows):
    store = SegmentStore(snap)
    total, cut = 0, []
    for i in store.finished():
        m = store.meta(i)
        if total >= max_rows:
            m['status_before_limit'] = m['status']
            m['status'] = 'skipped_max_rows'
            util.save_json(os.path.join(store.path(i), 'meta.json'), m)
            cut.append(i)
        else:
            total += m.get('ticks', 0)
    if cut:
        log(f'--max_rows {max_rows}: using {total} ticks, segments {cut} left out')


def build_dataset(snap, out, cfg, max_rows):
    if max_rows:
        limit_rows(snap, max_rows)
    cal = util.load_json(os.path.join(snap, 'calibration.json'), {}) or {}
    if not cal.get('targets'):
        raise SystemExit('calibration.json with targets not found in the state folder.')
    rep, ds_dir = assess(snap, cfg, cal['targets'])
    final = os.path.join(out, 'dataset')
    os.makedirs(final, exist_ok=True)
    changed = False
    for f in ('dataset.csv', 'events_by_intent.json', 'events_unobserved.json', 'quality_report.json',
              'quality_report.md', 'traffic_summary.json', 'mimics.json'):
        a, b = os.path.join(ds_dir, f), os.path.join(final, f)
        if not os.path.exists(a):
            continue
        if os.path.exists(b) and sha(a) == sha(b):
            continue                                   # keep the old file and its mtime (training cache)
        shutil.copy2(a, b)
        changed = changed or f in ('dataset.csv', 'events_by_intent.json')
    log(f"dataset: {rep['rows']} rows, events {rep['events_per_intent']}, "
        f"{'changed' if changed else 'unchanged'} since the last run")
    return rep, final, cal['targets']


# =============================================================================
# 3. suitability
# =============================================================================
def grade(v, good, ok, higher=True):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return 'FAIL'
    if higher:
        return 'PASS' if v >= good else ('WARN' if v >= ok else 'FAIL')
    return 'PASS' if v <= good else ('WARN' if v <= ok else 'FAIL')


def suitability(rep, ds, snap, test_frac):
    data = pd.read_csv(os.path.join(ds, 'dataset.csv'))
    ev = json.load(open(os.path.join(ds, 'events_by_intent.json')))
    store = SegmentStore(snap)
    C = []

    def add(name, value, status, rule, meaning):
        C.append({'check': name, 'value': value, 'status': status, 'rule': rule, 'meaning': meaning})

    n = len(data)
    add('clean rows', n, grade(n, 20000, 12000), 'PASS >= 20000, WARN >= 12000',
        'More rows give more test events and tighter confidence intervals.')
    td = test_frac * n / 1440.0
    add('test days (emulated, 1440 ticks/day)', round(td, 2), grade(td, 4, 2), 'PASS >= 4, WARN >= 2',
        'FP/day is estimated on the test days. FP tests use 240-tick blocks when the test set is short.')
    per = {k: len(v) for k, v in ev.items()}
    mt = min(per.values()) * test_frac if per else 0
    add('expected test events per intent (min)', round(mt, 1), grade(mt, 40, 20), 'PASS >= 40, WARN >= 20',
        'Detection and disambiguation are proportions over test events. With 40 events the 95 % CI is about +-12 pp.')
    cod = sum(1 for L in ev.values() for e in L if e['type'] == 'conflicting_victim')
    add('co-drift pairs (cause + victim)', cod, grade(cod, 60, 30), 'PASS >= 60, WARN >= 30',
        'Co-drift pairs make disambiguation hard. Too few make the DA comparison weak.')
    inj = rep.get('events_by_type_injected_observed', {})
    fr = {k: (v[1] / v[0] if v[0] else 0.0) for k, v in inj.items()}
    worst = min(fr.values()) if fr else 0.0
    low = {k: round(v, 2) for k, v in fr.items() if v < 0.7}
    add('observed failure fraction per type (min)', round(worst, 2), grade(worst, 0.7, 0.5), 'PASS >= 0.7, WARN >= 0.5',
        f'Types whose injected faults rarely become failures are under-represented (strict policy). Low types: {low or "none"}.')
    slo = rep.get('slo_exceedance_outside_windows', {})
    ws = max(slo.values()) if slo else float('nan')
    add('SLO exceedance outside event windows (max)', round(ws, 4), grade(ws, 0.02, 0.05, higher=False),
        'PASS <= 2 %, WARN <= 5 %', 'Violations in healthy time are failures without a label; they count as false alarms.')
    ms = rep.get('merge', {})
    mf = ms.get('interpolated_values', 0) / max(1, n * len(KPI_NAMES))
    add('interpolated KPI values', f"{ms.get('interpolated_values', 0)} ({100 * mf:.2f} %)",
        grade(mf, 0.01, 0.03, higher=False), 'PASS <= 1 %, WARN <= 3 %',
        'Short gaps (<= 3 ticks) are interpolated between real measurements. Longer gaps are removed.')
    bq = rep.get('buildup_quantiles', {})
    bm = bq.get('0.5', bq.get(0.5))
    add('median build-up (failure_time - start), ticks', bm, grade(bm, 4, 2), 'PASS >= 4, WARN >= 2',
        'Lead time can only be positive when failures build up over several ticks.')
    allwin = [(e['start'], e['end']) for L in ev.values() for e in L]
    t = data['t'].values
    inwin = np.zeros(len(t), dtype=bool)
    for s, e in allwin:
        inwin |= (t >= s) & (t < e)
    h = data[~inwin]
    cut = int((1 - test_frac) * len(data))
    tcut = data['t'].iloc[cut] if len(data) else 0
    hd, ht = h[h['t'] < tcut], h[h['t'] >= tcut]
    drift = {}
    for c in KPI_NAMES:
        iqr = float(h[c].quantile(0.75) - h[c].quantile(0.25)) or float(h[c].std() or 1.0)
        drift[c] = round(abs(float(ht[c].median() - hd[c].median())) / max(iqr, 1e-9), 2) if len(hd) and len(ht) else None
    dmax = max((v for v in drift.values() if v is not None), default=float('nan'))
    add('healthy-KPI shift dev -> test (max, in IQR units)', dmax, grade(dmax, 1.0, 2.0, higher=False),
        'PASS <= 1, WARN <= 2', f'A large shift means the test period behaves differently from training. Per KPI: {drift}')
    v = {}
    for i in store.finished():
        q = util.load_json(os.path.join(store.path(i), 'qa.json'), {}) or {}
        v[q.get('verdict', '?')] = v.get(q.get('verdict', '?'), 0) + 1
    nb = v.get('BAD', 0)
    add('segments by QA verdict', json.dumps(v), 'PASS' if nb == 0 else ('WARN' if nb <= 0.1 * sum(v.values()) else 'FAIL'),
        'PASS: no BAD segment; WARN <= 10 %', 'BAD segments had missing KPIs, overloaded ticks or violated targets in healthy time.')
    mim, mimx = rep.get('mimics', 0), rep.get('mimic_crossings', 0)
    add('benign mimics that crossed a target', f'{mimx} of {mim}', 'PASS' if mimx <= 0.2 * max(mim, 1) else 'WARN',
        'PASS <= 20 %', 'Mimics must look like the onset of a failure without becoming one (false-positive test).')
    tev = {}
    for k, L in ev.items():
        for e in L:
            if e['start'] >= tcut:
                tev[e['type']] = tev.get(e['type'], 0) + 1
    types = sorted({e['type'] for L in ev.values() for e in L})
    miss = [ty for ty in types if tev.get(ty, 0) < 5]
    add('every event type present in the test part (>= 5 each)', f'missing or rare: {miss or "none"}',
        'PASS' if not miss else 'WARN', 'PASS if none missing', 'Each scenario type should be tested, not only trained.')
    stat = [c['status'] for c in C]
    verdict = 'UNSUITABLE' if 'FAIL' in stat else ('MARGINAL' if 'WARN' in stat else 'SUITABLE')
    return C, verdict, {'test_events_by_type': tev, 'test_start_t': int(tcut) if len(data) else None}


def write_suitability(out, C, verdict):
    L = ['# Data suitability report (MILD on O-RAN)', '', f'**Verdict: {verdict}**', '',
         'SUITABLE: train and report. MARGINAL: train; results need the caveats listed in TRUST_REPORT.md. '
         'UNSUITABLE: training still runs, but the results are exploratory only.', '',
         '| check | value | status | rule |', '|---|---|---|---|']
    for c in C:
        L.append(f"| {c['check']} | {c['value']} | {c['status']} | {c['rule']} |")
    L += ['', '## What each check means', ''] + [f"- **{c['check']}**: {c['meaning']}" for c in C]
    with open(os.path.join(out, 'SUITABILITY_REPORT.md'), 'w') as f:
        f.write('\n'.join(L) + '\n')
    pd.DataFrame(C).to_csv(os.path.join(out, 'suitability_checks.csv'), index=False)


# =============================================================================
# 4. training
# =============================================================================
def train(a, ds, out, cfg):
    repo = os.path.join(HERE, 'MILD_unified_repro')
    script = os.path.join(repo, 'autotrain.py')
    tdir = os.path.join(out, 'training')
    tr = cfg.get('train', {})
    cmd = [a.train_python or sys.executable, script, '--data_csv', os.path.join(ds, 'dataset.csv'),
           '--events_json', os.path.join(ds, 'events_by_intent.json'), '--out', tdir,
           '--paper_profile', tr.get('paper_profile', 'oran'), '--trials', str(a.trials), '--fp_budget', str(a.fp_budget),
           '--test_frac', f'{a.test_frac:.6f}', '--seeds', a.seeds, '--fp_block', str(a.fp_block),
           '--rerank_top', str(a.rerank_top), '--opcurve_targets', '1,2,3,5,8',
           '--mild_arch', a.mild_arch, '--baseline_features', a.baseline_features]
    if a.val_frac and abs(a.val_frac - 0.2) > 1e-9:
        cmd += ['--val_frac', str(a.val_frac)]
    if a.thr_budget > 0:
        cmd += ['--thr_budget', str(a.thr_budget)]
    if a.variants != 'tuned,paper':
        cmd += ['--variants', a.variants]
    cmd += ['--fp_scope', a.fp_scope]
    if a.nice and shutil.which('nice'):
        cmd = ['nice', '-n', str(a.nice)] + cmd
    log('training: ' + ' '.join(cmd))
    log(f'training log: {os.path.join(out, "training.log")} (jobs are cached; re-running continues)')
    t0 = time.time()
    env = dict(os.environ, TF_CPP_MIN_LOG_LEVEL='3')
    if a.threads:
        env.update(TF_NUM_INTRAOP_THREADS=str(a.threads), TF_NUM_INTEROP_THREADS='2', OMP_NUM_THREADS=str(a.threads))
    with open(os.path.join(out, 'training.log'), 'a') as lf:
        p = subprocess.Popen(cmd, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True)
        for line in p.stdout:
            lf.write(line)
            lf.flush()
            if line.startswith('[20'):           # autotrain progress lines
                print('  ' + line.rstrip(), flush=True)
        rc = p.wait()
    log(f'training finished rc={rc} after {(time.time() - t0) / 60:.1f} min')
    return rc, tdir


# =============================================================================
# 5. trust
# =============================================================================
def wilson_half(p, n):
    if not n:
        return float('nan')
    z = 1.96
    den = 1 + z * z / n
    return 100 * z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den


def pct(x):
    return x * 100 if x is not None and abs(x) <= 1.0 + 1e-9 else x


def fp_sources(tdir, ds, test_start, tail=12):
    """v2.2: where the false-alarm steps of the final MILD (tuned) runs fall: during or right after a benign mimic,
    within `tail` ticks after a labelled window (recovery tail), or in plain healthy time. Informational only."""
    mim = util.load_json(os.path.join(ds, 'mimics.json'), None)
    ev = util.load_json(os.path.join(ds, 'events_by_intent.json'), {}) or {}
    fr_p = os.path.join(tdir, 'final_test_runs.csv')
    if mim is None or not os.path.exists(fr_p):
        return None
    jobs = []
    lst = util.load_json(os.path.join(tdir, 'final_jobs.json'), None)
    if lst:                                     # v2.2.1: only this run's final jobs (not cached jobs of old datasets)
        for j in lst.get('jobs', []):
            if j['name'] == 'mild' and j['variant'] == 'tuned':
                ts_p = os.path.join(j['dir'], 'folds', 'fold1_timeseries.csv')
                if os.path.exists(ts_p):
                    jobs.append((ts_p, os.path.join(j['dir'], 'folds', 'fold1_metrics.json')))
    root = os.path.join(tdir, 'jobs', 'mild')
    for j in ([] if lst else (os.listdir(root) if os.path.isdir(root) else [])):
        sp = util.load_json(os.path.join(root, j, 'spec.json'), {}) or {}
        c = dict(sp.get('cfg') or {})
        tr = c.pop('test_range', None)
        ts_p = os.path.join(root, j, 'folds', 'fold1_timeseries.csv')
        mt_p = os.path.join(root, j, 'folds', 'fold1_metrics.json')
        final = any(str(x).startswith('final:mild:tuned:') for x in (sp.get('roles') or []))
        if final and tr and int(tr[0]) == int(test_start) and os.path.exists(ts_p):
            jobs.append((ts_p, mt_p))
    if not jobs:
        return None
    W = [(e['start'], e['end']) for L in ev.values() for e in L]
    tot = {'mimic': 0, 'recovery_tail': 0, 'healthy': 0}
    per_mimic, n_runs, calib = {}, 0, []

    def fp_and_cross(ts, par):
        t = ts['t'].values
        inwin = np.zeros(len(t), dtype=bool)
        for a_, b_ in W:
            inwin |= (t >= a_) & (t <= b_)
        days = max((t[-1] - t[0]) / 1440.0, 1e-9) if len(t) > 1 else 1.0
        fp = sum(int(((ts[f'ema_{k}'].values >= p['tau']) & ~inwin).sum()) for k, p in par.items() if f'ema_{k}' in ts)
        ms = [m for m in mim if len(t) and m['start'] >= t[0] and m['end'] <= t[-1]]
        cross = 0
        for m in ms:
            w = (t >= m['start']) & (t <= m['end'] + tail)
            cross += int(any((ts[f'ema_{k}'].values[w] >= p['tau']).any() for k, p in par.items() if f'ema_{k}' in ts))
        return fp / days, len(ms), cross, days

    for ts_p, mt_p in jobs:
        ts = pd.read_csv(ts_p)
        par = (util.load_json(mt_p, {}) or {}).get('params', {})
        va_p = ts_p.replace('fold1_timeseries.csv', 'fold1_val_timeseries.csv')
        if os.path.exists(va_p):
            v = fp_and_cross(pd.read_csv(va_p), par)
            te = fp_and_cross(ts, par)
            calib.append({'val_days': round(v[3], 2), 'val_fp_per_day': round(v[0], 2), 'val_mimics': v[1],
                          'val_mimics_alerted': v[2], 'test_days': round(te[3], 2), 'test_fp_per_day': round(te[0], 2),
                          'test_mimics': te[1], 'test_mimics_alerted': te[2]})
        t = ts['t'].values
        inwin = np.zeros(len(t), dtype=bool)
        tailw = np.zeros(len(t), dtype=bool)
        for a_, b_ in W:
            inwin |= (t >= a_) & (t <= b_)
            tailw |= (t > b_) & (t <= b_ + tail)
        mw = {}
        for m in mim:
            mw.setdefault(m['type'], np.zeros(len(t), dtype=bool))
            mw[m['type']] |= (t >= m['start']) & (t <= m['end'] + tail)
        anym = np.zeros(len(t), dtype=bool)
        for v in mw.values():
            anym |= v
        for k, p in par.items():
            if f'ema_{k}' not in ts:
                continue
            fp = (ts[f'ema_{k}'].values >= p['tau']) & ~inwin
            tot['mimic'] += int((fp & anym).sum())
            tot['recovery_tail'] += int((fp & ~anym & tailw).sum())
            tot['healthy'] += int((fp & ~anym & ~tailw).sum())
            for ty, v in mw.items():
                per_mimic[f'{k} during {ty}'] = per_mimic.get(f'{k} during {ty}', 0) + int((fp & v).sum())
        n_runs += 1
    n = sum(tot.values())
    return {'runs': n_runs, 'fp_steps': n, 'calibration_vs_test': calib, 'share': {k: (round(100.0 * v / n, 1) if n else 0.0) for k, v in tot.items()},
            'by_intent_and_mimic': {k: v for k, v in sorted(per_mimic.items(), key=lambda x: -x[1]) if v}}


def trust(out, tdir, suit_verdict, suit_extra, fp_budget, ds=None):
    T = []

    def add(name, value, status, rule, why):
        T.append({'criterion': name, 'value': value, 'status': status, 'rule': rule, 'why': why})

    fr_p = os.path.join(tdir, 'final_test_runs.csv')
    if not os.path.exists(fr_p):
        L = ['# Trust report', '', '**Verdict: NOT TRUSTWORTHY (training did not produce test results)**', '',
             f'See {os.path.join(out, "training.log")}. If it says "dataset too small", collect more segments.']
        open(os.path.join(out, 'TRUST_REPORT.md'), 'w').write('\n'.join(L) + '\n')
        return 'NOT TRUSTWORTHY'
    fr = pd.read_csv(fr_p)
    tuned = fr[fr['variant'] == 'tuned'].copy()
    for c in ['detection', 'disamb'] + [c for c in tuned.columns if c.startswith('det_')]:
        tuned[c] = tuned[c].map(pct)
    aud = util.load_json(os.path.join(tdir, 'audit.json'), {}) or {}
    sig = pd.read_csv(os.path.join(tdir, 'significance_test_set.csv')) if os.path.exists(
        os.path.join(tdir, 'significance_test_set.csv')) else pd.DataFrame()
    lb = pd.read_csv(os.path.join(tdir, 'search_leaderboard.csv')) if os.path.exists(
        os.path.join(tdir, 'search_leaderboard.csv')) else pd.DataFrame()
    mild = tuned[tuned['method'] == 'MILD']
    add('T1 data suitability', suit_verdict, {'SUITABLE': 'PASS', 'MARGINAL': 'WARN'}.get(suit_verdict, 'FAIL'),
        'SUITABLE = PASS, MARGINAL = WARN', 'Results cannot be better than the data (see SUITABILITY_REPORT.md).')
    n_det = n_da = None
    if not sig.empty:
        g = sig[sig['metric'] == 'detection_%']
        n_det = int(g['n_units'].iloc[0]) if len(g) else None
        g = sig[sig['metric'] == 'disambiguation_%']
        n_da = int(g['n_units'].iloc[0]) if len(g) else None
    test_days = aud.get('test_rows', 0) / 1440.0
    per_int = {}
    for ty, k in (suit_extra.get('test_events_by_type') or {}).items():
        it = 'api' if 'api' in ty else ('analytics' if 'analytics' in ty else 'telemetry')
        per_int[it] = per_int.get(it, 0) + k
    mi = min(per_int.values()) if per_int else 0
    stt = 'PASS' if (mi >= 30 and test_days >= 3) else ('WARN' if (mi >= 15 and test_days >= 1.5) else 'FAIL')
    add('T2 test-set size', f'test events per intent {per_int}, test days {test_days:.2f}', stt,
        'PASS: >= 30 events per intent and >= 3 days', 'Small test sets give wide intervals and untestable FP comparisons.')
    if len(mild):
        det, da = mild['detection'].mean(), mild['disamb'].mean()
        hd = wilson_half(det / 100, n_det) if n_det else float('nan')
        hda = wilson_half(da / 100, n_da) if n_da else float('nan')
        worst = max(x for x in (hd, hda) if x == x) if (hd == hd or hda == hda) else float('nan')
        add('T3 precision (95 % CI half-width of MILD Det and DA)',
            f'Det {det:.1f} % +- {hd:.1f} pp (n={n_det}), DA {da:.1f} % +- {hda:.1f} pp (n={n_da})',
            grade(worst, 10, 15, higher=False), 'PASS <= 10 pp, WARN <= 15 pp',
            'Differences smaller than the interval cannot be claimed.')
        sd = max(mild['detection'].std(ddof=0), mild['disamb'].std(ddof=0))
        sp_p = os.path.join(tdir, 'opcurve', 'seed_spread_equal_fp.csv')
        sd_eq = None
        if os.path.exists(sp_p):
            sp = pd.read_csv(sp_p, index_col=0)
            if 'MILD' in sp.index:
                sd_eq = float(max(sp.loc['MILD', 'detection'], sp.loc['MILD', 'disamb']))
        val = sd_eq if sd_eq is not None and sd_eq == sd_eq else sd
        add('T4 seed stability (std of MILD Det/DA over seeds)',
            f'{val:.2f} pp' + (' at equal FP' if sd_eq is not None else '') + f' ({len(mild)} seeds)',
            grade(val, 5, 10, higher=False) if len(mild) > 1 else 'WARN', 'PASS <= 5 pp, WARN <= 10 pp (needs >= 2 seeds)',
            'A large spread means a single run would not be reproducible.')
        if not lb.empty and (lb['method'] == 'MILD').any():
            dv = lb[lb['method'] == 'MILD'].sort_values('score', ascending=False).iloc[0]
            gd, ga = abs(pct(dv['detection']) - det), abs(pct(dv['disamb']) - da)
            add('T5 generalisation (dev validation vs held-out test, MILD)',
                f'Det {pct(dv["detection"]):.1f} -> {det:.1f}, DA {pct(dv["disamb"]):.1f} -> {da:.1f}',
                grade(max(gd, ga), 10, 20, higher=False), 'PASS gap <= 10 pp, WARN <= 20 pp',
                'A large drop means the tuning over-fitted the development period or the test period differs.')
        fp = mild['fp_per_day'].mean()
        add('T6 false alarms of MILD on the test set', f'{fp:.2f} FP steps/day (budget {fp_budget})',
            grade(fp, 2 * fp_budget, 4 * fp_budget, higher=False), 'PASS <= 2 x budget, WARN <= 4 x budget',
            'The FP budget is tuned on development data. Much higher test FP means poor calibration.')
        dcols = [c for c in mild.columns if c.startswith('det_')]
        if dcols:
            pi = {c[4:]: round(float(mild[c].mean()), 1) for c in dcols}
            add('T7 no blind intent (MILD per-intent detection, min)', f'{pi}', grade(min(pi.values()), 20, 10),
                'PASS >= 20 %, WARN >= 10 %', 'An intent that is almost never detected makes macro averages misleading.')
    else:
        add('T3-T7 MILD results', 'missing', 'FAIL', 'MILD must have test runs', 'MILD jobs failed (see training.log).')
    base = tuned[tuned['method'] != 'MILD'].groupby('method')['detection'].mean()
    coll = {k: round(v, 1) for k, v in base.items() if v < 5}
    add('T8 baselines are functional', f'collapsed (Det < 5 %): {coll or "none"}', 'PASS' if not coll else 'WARN',
        'PASS if no baseline collapsed', 'Beating a collapsed baseline proves nothing. Check its job.log.')
    claims, noclaims = [], []
    if not sig.empty:
        for _, r in sig.iterrows():
            line = (f"{r['metric']} vs {r['baseline_name']}: MILD {r['mild']:.2f}, baseline {r['baseline']:.2f}, "
                    f"diff {r['diff']:.2f} [{r['ci_low']:.2f}, {r['ci_high']:.2f}], Holm p={r['p_holm']:.3g}"
                    if r['p_holm'] == r['p_holm'] else f"{r['metric']} vs {r['baseline_name']}: not testable")
            (claims if r['verdict'] == 'MILD significantly better' else noclaims).append(f"{line} -> {r['verdict']}")
        nt = int((sig['verdict'] == 'not testable (too few units)').sum())
        add('T9 significance', f'{len(claims)} supported claims, {nt} untestable comparisons, '
            f'{int((sig["verdict"] == "baseline significantly better").sum())} where a baseline is better',
            'PASS' if claims and nt == 0 else 'WARN', 'PASS: at least one supported claim and nothing untestable',
            'Only differences with Holm-corrected p < 0.05 may be written as "MILD outperforms".')
    else:
        add('T9 significance', 'no tests available', 'FAIL', 'tests must exist', 'see training.log')
    eq_p = os.path.join(tdir, 'opcurve', 'significance_equal_fp.csv')
    eq_claims, eq_other = [], []
    if os.path.exists(eq_p):
        eq = pd.read_csv(eq_p)
        for _, r in eq.iterrows():
            line = (f"{r['metric']} vs {r['baseline_name']} at equal FP: MILD {r['mild']:.2f}, baseline {r['baseline']:.2f}, "
                    f"diff {r['diff']:.2f}, median Holm p={r['p_holm']:.3g} -> {r['verdict']}")
            (eq_claims if r['verdict'] == 'MILD significantly better' else eq_other).append(line)
        worse = int((eq['verdict'] == 'baseline significantly better').sum())
        add('T10 comparison at equal false-alarm rate', f'{len(eq_claims)} supported claims, {worse} where a baseline is better',
            'PASS' if worse == 0 else 'WARN', 'PASS if no baseline is significantly better at equal FP',
            'Removes the effect of different alarm rates (training/opcurve/OPCURVE_REPORT.md).')
    stats = [x['status'] for x in T]
    verdict = 'NOT TRUSTWORTHY' if 'FAIL' in stats else ('USABLE WITH CAVEATS' if 'WARN' in stats else 'PAPER-READY')
    L = ['# Trust report for the O-RAN results', '', f'**Verdict: {verdict}**', '',
         '- PAPER-READY: report the numbers as the O-RAN result.',
         '- USABLE WITH CAVEATS: report them and state the caveats below (or collect more data to remove them).',
         '- NOT TRUSTWORTHY: do not put these numbers in the paper. Fix the FAIL items first.', '',
         '| criterion | value | status | rule |', '|---|---|---|---|']
    L += [f"| {x['criterion']} | {x['value']} | {x['status']} | {x['rule']} |" for x in T]
    L += ['', '## Why each criterion matters', ''] + [f"- **{x['criterion']}**: {x['why']}" for x in T]
    L += ['', '## Claims supported by the paired tests (write these)', ''] + ([f'- {c}' for c in claims] or ['- none'])
    L += ['', '## Comparisons that are NOT significant or favour a baseline', ''] + ([f'- {c}' for c in noclaims] or ['- none'])
    L += ['', '## Comparison at equal false-alarm rate (supported claims)', ''] + ([f'- {c}' for c in eq_claims] or ['- none'])
    L += ['', '## Comparison at equal false-alarm rate (others)', ''] + ([f'- {c}' for c in eq_other] or ['- none'])
    fps = None
    try:
        fps = fp_sources(tdir, ds, int(aud.get('dev_rows', aud.get('dev', -1)))) if ds else None
    except Exception as ex:                                 # the diagnostic must never break the report
        log(f'fp_sources: {ex}')
    if fps:
        L += ['', '## Where the false alarms of MILD (tuned) come from (diagnostic, not a criterion)', '',
              f"{fps['fp_steps']} false-alarm steps over {fps['runs']} final run(s): "
              f"{fps['share']['mimic']} % during or up to 12 ticks after a benign mimic, "
              f"{fps['share']['recovery_tail']} % within 12 ticks after a labelled episode (recovery tail), "
              f"{fps['share']['healthy']} % in plain healthy time.", '',
              'Benign mimics are short, harmless KPI excursions injected on purpose to test false alarms; alerts during '
              'them are real false alarms of the model, not a data problem. A large "healthy" share would point to a '
              'data problem (drift, unlabelled faults). Largest contributions: '
              + ', '.join(f'{k} {v}' for k, v in list(fps['by_intent_and_mimic'].items())[:6])]
        if fps.get('calibration_vs_test'):
            L += ['', 'Threshold-calibration period (the last 20 % of the development part, where (W, tau) are chosen '
                  'under the FP budget) against the test part, same thresholds:', '',
                  '| run | calibration days | calibration FP/day | mimics alerted (calibration) | test days | test FP/day | '
                  'mimics alerted (test) |', '|---|---|---|---|---|---|---|']
            for i, c in enumerate(fps['calibration_vs_test']):
                L.append(f"| {i + 1} | {c['val_days']} | {c['val_fp_per_day']} | {c['val_mimics_alerted']} of {c['val_mimics']} | "
                         f"{c['test_days']} | {c['test_fp_per_day']} | {c['test_mimics_alerted']} of {c['test_mimics']} |")
            L += ['', 'The thresholds are the lowest that meet the FP budget on the calibration period, so the FP rate '
                  'measured there tends to be biased low; with only a few benign mimics in it, the test FP rate can be several '
                  'times higher. A longer calibration period (more collected data) reduces this gap.']
    cav = [x for x in T if x['status'] != 'PASS']
    if cav:
        fix = {'T1': 'collect more segments', 'T2': 'collect more data (each 900-tick segment adds ~270 test ticks)',
               'T3': 'more test events (collect more)', 'T4': 'more seeds (--seeds 42,43,44,45,46)',
               'T5': 'more data or fewer trials; check drift in SUITABILITY_REPORT.md',
               'T6': 'lower --fp_budget or more development data', 'T7': 'check the observed fraction of that intent',
               'T8': 'inspect the baseline job.log', 'T9': 'more test events; --fp_block 120 for short test sets',
               'T10': 'more test events (collect more)'}
        L += ['', '## Caveats and how to remove them', '']
        L += [f"- {x['criterion']} ({x['status']}): {fix.get(x['criterion'].split()[0], 'see above')}" for x in cav]
    L += ['', f'Files: {tdir}/AUTOTRAIN_REPORT.md, final_test_summary.csv, significance_test_set.csv']
    with open(os.path.join(out, 'TRUST_REPORT.md'), 'w') as f:
        f.write('\n'.join(L) + '\n')
    pd.DataFrame(T).to_csv(os.path.join(out, 'trust_criteria.csv'), index=False)
    util.save_json(os.path.join(out, 'trust_verdict.json'), {'verdict': verdict, 'criteria': T, 'claims': claims,
                                                               'fp_sources': fps})
    return verdict


# =============================================================================
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state', default=os.path.expanduser('~/intact/mild/state'))
    ap.add_argument('--out', default=os.path.expanduser('~/intact/mild/train'))
    ap.add_argument('--train_python', default='')
    ap.add_argument('--max_rows', type=int, default=0)
    ap.add_argument('--check_only', action='store_true')
    ap.add_argument('--refresh', action='store_true')
    ap.add_argument('--quick', action='store_true')
    ap.add_argument('--trials', type=int, default=20)
    ap.add_argument('--seeds', default='42,43,44,45,46,47,48,49,50,51')
    ap.add_argument('--rerank_top', type=int, default=3)
    ap.add_argument('--mild_arch', default='v26', choices=['v23', 'v26', 'paper'])
    ap.add_argument('--baseline_features', default='onset', choices=['base', 'onset'])
    ap.add_argument('--fp_budget', type=float, default=3.0)
    ap.add_argument('--fp_block', type=int, default=240)
    ap.add_argument('--val_frac', type=float, default=0.4, help='v2.5: validation fraction (thresholds + early stop)')
    ap.add_argument('--variants', default='tuned', help='final-test variants: tuned (default) or tuned,paper')
    ap.add_argument('--fp_scope', default='own', choices=['any', 'own'], help='v2.7: false alarm = outside every intent window (any) or outside the intent\'s own windows (own, default)')
    ap.add_argument('--thr_budget', type=float, default=1.0, help='v2.5: fixed per-intent threshold FP budget (0 = searched)')
    ap.add_argument('--test_frac', type=float, default=0.30)
    ap.add_argument('--nice', type=int, default=10)
    ap.add_argument('--threads', type=int, default=4, help='CPU threads for TensorFlow (0 = all)')
    a = ap.parse_args()
    if a.quick:
        a.trials, a.seeds, a.rerank_top = 6, '42', 0
    a.state, a.out = os.path.abspath(os.path.expanduser(a.state)), os.path.abspath(os.path.expanduser(a.out))
    os.makedirs(a.out, exist_ok=True)
    util.set_log(os.path.join(a.out, 'train_now.log'))
    cfg = load_cfg(a.state)
    log(f'train_now: state={a.state} out={a.out}')
    snap = snapshot(a.state, a.out, a.refresh)
    rep, ds, targets = build_dataset(snap, a.out, cfg, a.max_rows)
    C, sv, extra = suitability(rep, ds, snap, a.test_frac)
    write_suitability(a.out, C, sv)
    log(f'suitability: {sv} ' + json.dumps({c["check"]: c["status"] for c in C}))
    log(f'see {os.path.join(a.out, "SUITABILITY_REPORT.md")}')
    if sv == 'UNSUITABLE':
        log('WARNING: data is UNSUITABLE for paper results. Training continues (results are exploratory).')
    if a.check_only:
        return
    rc, tdir = train(a, ds, a.out, cfg)
    v = trust(a.out, tdir, sv, extra, a.fp_budget, ds)
    log(f'TRUST VERDICT: {v}. Read {os.path.join(a.out, "TRUST_REPORT.md")} and {tdir}/AUTOTRAIN_REPORT.md')


if __name__ == '__main__':
    main()
