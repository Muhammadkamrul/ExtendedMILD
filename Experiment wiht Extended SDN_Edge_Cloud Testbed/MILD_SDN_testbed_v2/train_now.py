#!/usr/bin/env python3
"""
train_now.py -- train MILD and all baselines on the SDN data collected so far,
without touching the running collection, and say how far the results can be
trusted.

  python train_now.py --state ~/mild_sdn2_state --train_python ~/mild-venv/bin/python

What it does (every step is recorded in --out and the script is resumable):
  1. snapshot   copies the finished segments of --state (read-only access) into
                <out>/snapshot. The segment that is still being collected is
                skipped. A snapshot is reused until new segments are finished
                (or --refresh is given).
  2. salvage    marks the "API jam" intervals of collector version 1 in the
                snapshot copy (see mild_sdn2/salvage.py). The originals are not
                changed.
  3. dataset    merges the snapshot into dataset.csv + events_by_intent.json
                and writes the standard quality report. dataset.csv is only
                rewritten when its content changed, so finished training jobs
                are reused when you run the script again.
  4. suitability  12 checks (size, events, observed failures, label sanity,
                missing values, drift, ...). Verdict SUITABLE / MARGINAL /
                UNSUITABLE. Training runs in every case (unless --check_only);
                the verdict is carried into the trust report.
  5. train      autotrain.py: chronological development/test split at quiet
                points, equal trial budget for every method, held-out test with
                several seeds, paired significance tests with Holm correction.
                Jobs are cached, so an interrupted run continues where it stopped.
  6. trust      TRUST_REPORT.md: 9 criteria that decide whether the numbers can
                go into the paper, and which comparative claims are supported.
                Verdict PAPER-READY / USABLE WITH CAVEATS / NOT TRUSTWORTHY.

Options:
  --out DIR          output folder (default ~/mild_sdn2_train_now)
  --max_rows N       use only the first N clean rows (in collection order)
  --check_only       stop after step 4
  --refresh          rebuild the snapshot even if no new segment is finished
  --trials/--seeds/--fp_budget/--fp_block/--test_frac   training settings
  --nice N           run training with this nice level (default 10, protects a
                     collection that is running at the same time)
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
from mild_sdn2 import util  # noqa: E402
from mild_sdn2 import salvage as SV  # noqa: E402
from mild_sdn2.collector import SegmentStore  # noqa: E402
from mild_sdn2.kpi import KPI_NAMES  # noqa: E402
from mild_sdn2.quality import assess  # noqa: E402

LOG = {'f': None}


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    if LOG['f']:
        with open(LOG['f'], 'a') as f:
            f.write(line + '\n')


def sha(path):
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def load_cfg():
    return json.load(open(os.path.join(HERE, 'config', 'testbed.json')))


# =============================================================================
# 1-3. snapshot, salvage, dataset
# =============================================================================
MAIN_OFFSET = 5000          # main-state segments are numbered from here when extra sources are used


def _finished(state):
    src = SegmentStore(state)
    segs = [i for i in src.list()
            if src.meta(i).get('status_before_salvage', src.meta(i).get('status')) in ('complete', 'truncated')]
    return src, segs


def snapshot(state, out, refresh, extras=()):
    """Copy finished segments. Extra sources come first (training only), the main state last."""
    src, segs = _finished(state)
    if not segs:
        raise SystemExit('No finished segment in the state folder yet.')
    off = MAIN_OFFSET if extras else 0
    sig = [('main', i, src.meta(i).get('ticks'), src.meta(i).get('status')) for i in segs]
    ex_lists = []
    for k, ex in enumerate(extras):
        esrc, esegs = _finished(ex)
        if any(i >= 1000 for i in esegs):
            raise SystemExit(f'{ex}: more than 1000 segments are not supported as an extra source')
        ex_lists.append((k, ex, esrc, esegs))
        sig += [(f'extra{k}:{os.path.abspath(ex)}', i, esrc.meta(i).get('ticks'), esrc.meta(i).get('status')) for i in esegs]
    snap = os.path.join(out, 'snapshot')
    man_p = os.path.join(snap, 'snapshot_manifest.json')
    man = util.load_json(man_p, {})
    if not refresh and man.get('segments') == [list(x) for x in sig]:
        log(f'snapshot: reusing ({len(sig)} finished segments, nothing new)')
        return snap, False, off
    if os.path.exists(snap):
        shutil.rmtree(snap)
    os.makedirs(os.path.join(snap, 'segments'))
    for k, ex, esrc, esegs in ex_lists:
        for i in esegs:
            dst = os.path.join(snap, 'segments', f'seg_{k * 1000 + i:04d}')
            shutil.copytree(esrc.path(i), dst)
            m = util.load_json(os.path.join(dst, 'meta.json'), {})
            m['source'] = os.path.abspath(ex)
            m['training_only'] = True
            util.save_json(os.path.join(dst, 'meta.json'), m)
    for i in segs:
        shutil.copytree(src.path(i), os.path.join(snap, 'segments', f'seg_{off + i:04d}'))
    for f in ('calibration.json', 'calibration_report.md', 'current_scales.json', 'scale_history.json'):
        p = os.path.join(state, f)
        if os.path.exists(p):
            shutil.copy(p, snap)
    runs = os.path.join(state, 'runs')                 # traffic logs only (for the traffic summary)
    if os.path.isdir(runs):
        for r in os.listdir(runs):
            tdir = os.path.join(runs, r, 'traffic')
            if os.path.isdir(tdir):
                shutil.copytree(tdir, os.path.join(snap, 'runs', r, 'traffic'))
    util.save_json(man_p, {'created': time.strftime('%Y-%m-%d %H:%M:%S'), 'source': os.path.abspath(state),
                           'extras': [os.path.abspath(e) for e in extras], 'segments': [list(x) for x in sig]})
    log(f'snapshot: copied {len(segs)} main segments' +
        (f' and {sum(len(x[3]) for x in ex_lists)} training-only segments from {len(ex_lists)} extra source(s)'
         if ex_lists else ''))
    return snap, True, off


KPI_CHECK = ['cpu_pct', 'ram_pct', 'storage_pct', 'snet', 'sri', 'api_latency', 'analytics_tput', 'telemetry_queue']


def _healthy_rows(store, idxs):
    out = []
    for i in idxs:
        d = store.path(i)
        try:
            r = pd.read_csv(os.path.join(d, 'rows.csv'))
        except Exception:
            continue
        m = store.meta(i)
        r = r[r['t'] < m.get('ticks', len(r))]
        plan = util.load_json(os.path.join(d, 'plan.json'), {}).get('plan', [])
        rmax = int(m.get('recovery_max', 10)) + 2
        q = np.array([not any(e['start'] <= t < e['end'] + rmax for e in plan) for t in r['t']], dtype=bool)
        exc = util.load_json(os.path.join(d, 'exclude.json'), {}).get('intervals', [])
        for a0, b0 in exc:
            q &= ~((r['t'].values >= a0) & (r['t'].values < b0))
        out.append(r[q])
    return pd.concat(out) if out else pd.DataFrame(columns=KPI_CHECK)


def check_extras(snap, off, out, force):
    """Accept an extra source only if its healthy-time KPI levels match the main state.

    Shift = |median(extra) - median(main)| / IQR(main), per KPI, in healthy time.
    A source with any shift > 1.0 is left out (unless --force_extra), because the
    same KPI would then not mean the same thing in training and test.
    """
    store = SegmentStore(snap)
    fin = store.finished()
    main = [i for i in fin if i >= off]
    srcs = sorted({i // 1000 for i in fin if i < off})
    if not srcs:
        return []
    hm = _healthy_rows(store, main)
    iqr = {k: float(hm[k].quantile(0.75) - hm[k].quantile(0.25)) or float(hm[k].std() or 1.0) for k in KPI_CHECK}
    rep = []
    L = ['# Extra (training-only) data sources', '',
         'Rows from these sources are placed before the main state and are used only for development '
         '(training, validation, tuning). The held-out test set comes only from the main state.', '',
         'Compatibility rule: in healthy time, every KPI median must lie within 1 IQR of the main state.', '']
    for k in srcs:
        idx = [i for i in fin if i // 1000 == k and i < off]
        he = _healthy_rows(store, idx)
        shift = {c: round(abs(float(he[c].median()) - float(hm[c].median())) / max(iqr[c], 1e-9), 2) for c in KPI_CHECK}
        worst = max(shift.values())
        ok = worst <= 1.0 or force
        name = store.meta(idx[0]).get('source', f'extra{k}')
        rep.append({'source': name, 'segments': len(idx), 'max_shift_iqr': worst, 'accepted': ok, 'shift': shift})
        L += [f'## {name}', '', f'segments: {len(idx)}; max shift: {worst} IQR -> '
              f'**{"ACCEPTED" if ok else "REJECTED"}**' + (' (forced)' if force and worst > 1.0 else ''), '',
              '| KPI | shift (IQR units) |', '|---|---|'] + [f'| {c} | {v} |' for c, v in shift.items()] + ['']
        if not ok:
            for i in idx:
                m = store.meta(i)
                m['status_before_extra_check'] = m['status']
                m['status'] = 'rejected_extra_source'
                util.save_json(os.path.join(store.path(i), 'meta.json'), m)
        log(f"extra source {name}: max healthy-KPI shift {worst} IQR -> {'accepted' if ok else 'REJECTED'}")
    with open(os.path.join(out, 'EXTRA_SOURCES_REPORT.md'), 'w') as f:
        f.write('\n'.join(L) + '\n')
    return rep


def limit_rows(snap, max_rows, off=0):
    """Keep the first max_rows clean ticks of the MAIN state (extra sources are not counted)."""
    store = SegmentStore(snap)
    total, cut = 0, []
    for i in [x for x in store.finished() if x >= off]:
        m = store.meta(i)
        if total >= max_rows:
            m['status_before_limit'] = m['status']
            m['status'] = 'skipped_max_rows'
            util.save_json(os.path.join(store.path(i), 'meta.json'), m)
            cut.append(i)
        else:
            total += m.get('ticks', 0) - m.get('excluded_ticks', 0)
    if cut:
        log(f'--max_rows {max_rows}: using {total} clean ticks, segments {cut} left out')


def build_dataset(snap, out, cfg, max_rows, off=0, force_extra=False):
    sv = SV.salvage_state(snap, cfg['min_segment_ticks'])
    SV.write_report(sv, os.path.join(out, 'SALVAGE_REPORT.md'))
    if max_rows:
        limit_rows(snap, max_rows, off)
    extras = check_extras(snap, off, out, force_extra) if off else []
    cal = util.load_json(os.path.join(snap, 'calibration.json'), {})
    if not cal.get('targets'):
        raise SystemExit('calibration.json with targets not found in the state folder.')
    rep, ds_dir = assess(snap, cfg, cal['targets'])
    final = os.path.join(out, 'dataset')
    os.makedirs(final, exist_ok=True)
    changed = False
    for f in ('dataset.csv', 'events_by_intent.json', 'events_unobserved.json', 'quality_report.json',
              'quality_report.md', 'traffic_summary.json'):
        a, b = os.path.join(ds_dir, f), os.path.join(final, f)
        if not os.path.exists(a):
            continue
        if os.path.exists(b) and sha(a) == sha(b):
            continue                                   # keep the old file and its mtime (training cache)
        shutil.copy2(a, b)
        changed = changed or f in ('dataset.csv', 'events_by_intent.json')
    log(f"dataset: {rep['rows']} rows, events {rep['events_per_intent']}, "
        f"{'changed' if changed else 'unchanged'} since the last run")
    data = pd.read_csv(os.path.join(final, 'dataset.csv'), usecols=['segment'])
    segn = data['segment'].str[4:].astype(int)
    n_main, n_all = int((segn >= off).sum()), len(data)
    return rep, final, sv, cal['targets'], n_main, n_all, extras


# =============================================================================
# 4. suitability
# =============================================================================
def grade(v, good, ok, higher=True):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return 'FAIL'
    if higher:
        return 'PASS' if v >= good else ('WARN' if v >= ok else 'FAIL')
    return 'PASS' if v <= good else ('WARN' if v <= ok else 'FAIL')


def suitability(rep, ds, sv, snap, test_frac, targets):
    data = pd.read_csv(os.path.join(ds, 'dataset.csv'))
    ev = json.load(open(os.path.join(ds, 'events_by_intent.json')))
    store = SegmentStore(snap)
    sv = [r for r in (sv or []) if store.meta(r['segment']).get('status') != 'rejected_extra_source']
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
        'Co-drift pairs are what makes disambiguation hard. Too few make the DA comparison weak.')
    inj = rep.get('events_by_type_injected_observed', {})
    fr = {k: (v[1] / v[0] if v[0] else 0.0) for k, v in inj.items()}
    worst = min(fr.values()) if fr else 0.0
    low = {k: round(v, 2) for k, v in fr.items() if v < 0.7}
    add('observed failure fraction per type (min)', round(worst, 2), grade(worst, 0.7, 0.5),
        'PASS >= 0.7, WARN >= 0.5', 'Types whose injected faults rarely become failures are under-represented '
        f'in the labels (strict policy). Low types: {low or "none"}.')
    slo = rep.get('slo_exceedance_outside_windows', {})
    ws = max(slo.values()) if slo else float('nan')
    add('SLO exceedance outside event windows (max)', round(ws, 4), grade(ws, 0.02, 0.05, higher=False),
        'PASS <= 2 %, WARN <= 5 %', 'Violations in healthy time are failures without a label. They count as false '
        'alarms for every model.')
    tot_v1 = sum(r['ticks'] for r in sv) if sv else 0
    exc_v1 = sum(r['ticks'] - r['clean_ticks'] for r in sv) if sv else 0
    jf = exc_v1 / tot_v1 if tot_v1 else 0.0
    add('collector-v1 ticks excluded as API jam', f'{exc_v1} of {tot_v1} ({100 * jf:.0f} %)',
        'PASS' if jf <= 0.1 else 'WARN', 'information (WARN > 10 %)',
        'These rows are not used. The kept rows were checked tick by tick.')
    ms = rep.get('merge', {})
    mf = ms.get('interpolated_values', 0) / max(1, n * len(KPI_NAMES))
    add('interpolated KPI values', f"{ms.get('interpolated_values', 0)} ({100 * mf:.2f} %)",
        grade(mf, 0.01, 0.03, higher=False), 'PASS <= 1 %, WARN <= 3 %',
        'Short gaps (<= 3 ticks) are interpolated between real measurements. Longer gaps are removed.')
    bq = rep.get('buildup_quantiles', {})
    bm = bq.get(0.5, bq.get('0.5'))
    add('median build-up (failure_time - start), ticks', bm, grade(bm, 4, 2), 'PASS >= 4, WARN >= 2',
        'Lead time can only be positive when failures build up over several ticks.')
    # healthy-KPI drift between the development and the test part
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
        'PASS <= 1, WARN <= 2', f'A large shift means the test period behaves differently from the training period. Per KPI: {drift}')
    vers = {}
    for i in store.finished():
        v = store.meta(i).get('code_version', 1)
        vers[v] = vers.get(v, 0) + 1
    add('segments by collector version', json.dumps(vers), 'PASS',
        'information', 'Version 1 = before the jam fix (salvaged), version 2 = fixed collector.')
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
    L = ['# Data suitability report', '', f'**Verdict: {verdict}**', '',
         'SUITABLE: train and report. MARGINAL: train, results need the caveats listed in TRUST_REPORT.md. '
         'UNSUITABLE: training still runs, but the results are exploratory only.', '',
         '| check | value | status | rule |', '|---|---|---|---|']
    for c in C:
        L.append(f"| {c['check']} | {c['value']} | {c['status']} | {c['rule']} |")
    L += ['', '## What each check means', '']
    L += [f"- **{c['check']}**: {c['meaning']}" for c in C]
    with open(os.path.join(out, 'SUITABILITY_REPORT.md'), 'w') as f:
        f.write('\n'.join(L) + '\n')
    pd.DataFrame(C).to_csv(os.path.join(out, 'suitability_checks.csv'), index=False)


# =============================================================================
# 5. training
# =============================================================================
def train(a, ds, out):
    repo = os.path.abspath(os.path.join(HERE, '..', 'MILD_unified_repro'))
    script = os.path.join(repo, 'autotrain.py')
    if not os.path.exists(script):
        raise SystemExit(f'autotrain.py not found at {script}. Keep MILD_unified_repro next to this folder.')
    tdir = os.path.join(out, 'training')
    cmd = [a.train_python or sys.executable, script, '--data_csv', os.path.join(ds, 'dataset.csv'),
           '--events_json', os.path.join(ds, 'events_by_intent.json'), '--out', tdir, '--paper_profile', 'sdn',
           '--trials', str(a.trials), '--fp_budget', str(a.fp_budget), '--test_frac', f'{a.test_frac_eff:.6f}',
           '--seeds', a.seeds, '--fp_block', str(a.fp_block), '--rerank_top', str(a.rerank_top),
           '--opcurve_targets', '1,2,3,5,8']
    if a.nice and shutil.which('nice'):
        cmd = ['nice', '-n', str(a.nice)] + cmd
    log('training: ' + ' '.join(cmd))
    log(f'training log: {os.path.join(out, "training.log")} (jobs are cached; re-running continues)')
    t0 = time.time()
    with open(os.path.join(out, 'training.log'), 'a') as lf:
        rc = subprocess.call(cmd, cwd=repo, stdout=lf, stderr=subprocess.STDOUT)
    log(f'training finished rc={rc} after {(time.time() - t0) / 60:.1f} min')
    return rc, tdir


# =============================================================================
# 6. trust
# =============================================================================
def wilson_half(p, n):
    if not n:
        return float('nan')
    z = 1.96
    den = 1 + z * z / n
    return 100 * z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den


def pct(x):
    return x * 100 if x is not None and abs(x) <= 1.0 + 1e-9 else x


def trust(out, tdir, suit_verdict, suit_extra, fp_budget):
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
    aud = util.load_json(os.path.join(tdir, 'audit.json'), {})
    sig = pd.read_csv(os.path.join(tdir, 'significance_test_set.csv')) if os.path.exists(
        os.path.join(tdir, 'significance_test_set.csv')) else pd.DataFrame()
    lb = pd.read_csv(os.path.join(tdir, 'search_leaderboard.csv')) if os.path.exists(
        os.path.join(tdir, 'search_leaderboard.csv')) else pd.DataFrame()
    mild = tuned[tuned['method'] == 'MILD']

    add('T1 data suitability', suit_verdict, {'SUITABLE': 'PASS', 'MARGINAL': 'WARN'}.get(suit_verdict, 'FAIL'),
        'SUITABLE = PASS, MARGINAL = WARN', 'Results cannot be better than the data (see SUITABILITY_REPORT.md).')
    # test size
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
    st = 'PASS' if (mi >= 30 and test_days >= 3) else ('WARN' if (mi >= 15 and test_days >= 1.5) else 'FAIL')
    add('T2 test-set size', f'test events per intent {per_int}, test days {test_days:.2f}', st,
        'PASS: >= 30 events per intent and >= 3 days', 'Small test sets give wide intervals and untestable FP comparisons.')
    # precision of MILD's own metrics
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
        if sd_eq is not None:
            add('T4 seed stability (std of MILD Det/DA over seeds, at equal FP)',
                f'{sd_eq:.2f} pp at equal FP; {sd:.2f} pp with validation-tuned thresholds ({len(mild)} seeds)',
                grade(sd_eq, 5, 10, higher=False), 'PASS <= 5 pp, WARN <= 10 pp (equal-FP spread)',
                'The equal-FP spread measures how stable the learned risk ranking is. The spread with '
                'validation-tuned thresholds also contains threshold calibration noise, which T6 covers.')
        else:
            add('T4 seed stability (std of MILD Det/DA over seeds)', f'{sd:.2f} pp over {len(mild)} seeds',
                grade(sd, 5, 10, higher=False), 'PASS <= 5 pp, WARN <= 10 pp',
                'A large spread means a single run would not be reproducible.')
        if not lb.empty and (lb['method'] == 'MILD').any():
            dv = lb[lb['method'] == 'MILD'].sort_values('score', ascending=False).iloc[0]
            gd = abs(pct(dv['detection']) - det)
            ga = abs(pct(dv['disamb']) - da)
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
                'PASS >= 20 %, WARN >= 10 %', 'An intent that is almost never detected makes the macro averages misleading.')
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
                    f"diff {r['diff']:.2f}, median Holm p={r['p_holm']:.3g}, seeds MILD/baseline better "
                    f"{int(r['seeds_mild_better'])}/{int(r['seeds_baseline_better'])} -> {r['verdict']}")
            (eq_claims if r['verdict'] == 'MILD significantly better' else eq_other).append(line)
        worse = int((eq['verdict'] == 'baseline significantly better').sum())
        add('T10 comparison at equal false-alarm rate', f'{len(eq_claims)} supported claims, {worse} where a baseline is better',
            'PASS' if worse == 0 else 'WARN', 'PASS if no baseline is significantly better at equal FP',
            'Removes the effect of different alarm rates (see training/opcurve/OPCURVE_REPORT.md).')
    stats = [x['status'] for x in T]
    verdict = 'NOT TRUSTWORTHY' if 'FAIL' in stats else ('USABLE WITH CAVEATS' if 'WARN' in stats else 'PAPER-READY')
    L = ['# Trust report for the SDN results', '', f'**Verdict: {verdict}**', '',
         '- PAPER-READY: report the numbers as the main SDN result.',
         '- USABLE WITH CAVEATS: report them, and state the caveats below in the paper (or collect more data to remove them).',
         '- NOT TRUSTWORTHY: do not put these numbers in the paper. Fix the FAIL items first.', '',
         '| criterion | value | status | rule |', '|---|---|---|---|']
    L += [f"| {x['criterion']} | {x['value']} | {x['status']} | {x['rule']} |" for x in T]
    L += ['', '## Why each criterion matters', ''] + [f"- **{x['criterion']}**: {x['why']}" for x in T]
    L += ['', '## Claims supported by the paired tests (write these)', '']
    L += [f'- {c}' for c in claims] or ['- none']
    L += ['', '## Comparisons that are NOT significant or favour a baseline (write "comparable" or report honestly)', '']
    L += [f'- {c}' for c in noclaims] or ['- none']
    L += ['', '## Comparison at equal false-alarm rate (supported claims)', '']
    L += [f'- {c}' for c in eq_claims] or ['- none (or not computed)']
    L += ['', '## Comparison at equal false-alarm rate (not significant or favouring a baseline)', '']
    L += [f'- {c}' for c in eq_other] or ['- none']
    caveats = [x for x in T if x['status'] != 'PASS']
    if caveats:
        L += ['', '## Caveats and how to remove them', '']
        fix = {'T1': 'collect more segments with the fixed collector', 'T2': 'collect more data (each 900-tick segment adds ~270 test ticks)',
               'T3': 'more test events (collect more)', 'T4': 'more seeds (--seeds 42,43,44,45,46)',
               'T5': 'more data or fewer trials; check drift in SUITABILITY_REPORT.md',
               'T6': 'lower --fp_budget or more development data', 'T7': 'check the observed fraction of that intent',
               'T8': 'inspect the baseline job.log', 'T9': 'more test events; untestable FP tests need more blocks (--fp_block 240 or 120)',
               'T10': 'more test events (collect more main-state data)'}
        for x in caveats:
            L.append(f"- {x['criterion']} ({x['status']}): {fix.get(x['criterion'].split()[0], 'see above')}")
    L += ['', f'Files: {tdir}/AUTOTRAIN_REPORT.md, final_test_summary.csv, significance_test_set.csv']
    with open(os.path.join(out, 'TRUST_REPORT.md'), 'w') as f:
        f.write('\n'.join(L) + '\n')
    pd.DataFrame(T).to_csv(os.path.join(out, 'trust_criteria.csv'), index=False)
    util.save_json(os.path.join(out, 'trust_verdict.json'), {'verdict': verdict, 'criteria': T, 'claims': claims})
    return verdict


# =============================================================================
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state', default=os.path.expanduser('~/mild_sdn2_state'))
    ap.add_argument('--out', default=os.path.expanduser('~/mild_sdn2_train_now'))
    ap.add_argument('--train_python', default='')
    ap.add_argument('--max_rows', type=int, default=0)
    ap.add_argument('--check_only', action='store_true')
    ap.add_argument('--refresh', action='store_true')
    ap.add_argument('--trials', type=int, default=20)
    ap.add_argument('--seeds', default='42,43,44,45,46')
    ap.add_argument('--extra_state', action='append', default=[],
                    help='earlier state folder whose finished segments are added as TRAINING-ONLY data (repeatable)')
    ap.add_argument('--force_extra', action='store_true', help='accept extra sources even if their KPI levels differ')
    ap.add_argument('--rerank_top', type=int, default=3)
    ap.add_argument('--fp_budget', type=float, default=3.0)
    ap.add_argument('--fp_block', type=int, default=240)
    ap.add_argument('--test_frac', type=float, default=0.30)
    ap.add_argument('--nice', type=int, default=10)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    LOG['f'] = os.path.join(a.out, 'train_now.log')
    util.set_log(os.path.join(a.out, 'train_now.log'))
    cfg = load_cfg()
    log(f'train_now: state={a.state} out={a.out}')
    snap, _, off = snapshot(a.state, a.out, a.refresh, a.extra_state)
    rep, ds, sv, targets, n_main, n_all, ex_rep = build_dataset(snap, a.out, cfg, a.max_rows, off, a.force_extra)
    # the test set is the last test_frac of the MAIN state only; extra sources are training-only
    a.test_frac_eff = a.test_frac * n_main / max(n_all, 1)
    if n_all != n_main:
        log(f'rows: {n_main} from the main state + {n_all - n_main} training-only rows from extra sources; '
            f'test = last {int(a.test_frac * n_main)} rows of the main state (test_frac {a.test_frac_eff:.4f} of all rows)')
    C, sv_verdict, extra = suitability(rep, ds, sv, snap, a.test_frac_eff, targets)
    if n_all != n_main:
        C.insert(1, {'check': 'rows: main state + training-only extra sources', 'value': f'{n_main} + {n_all - n_main}',
                     'status': 'PASS', 'rule': 'information',
                     'meaning': 'Extra sources add training data only; see EXTRA_SOURCES_REPORT.md.'})
        sv_verdict = 'UNSUITABLE' if any(c['status'] == 'FAIL' for c in C) else (
            'MARGINAL' if any(c['status'] == 'WARN' for c in C) else 'SUITABLE')
    write_suitability(a.out, C, sv_verdict)
    log(f'suitability: {sv_verdict} ' + json.dumps({c["check"]: c["status"] for c in C}))
    log(f'see {os.path.join(a.out, "SUITABILITY_REPORT.md")}')
    if sv_verdict == 'UNSUITABLE':
        log('WARNING: data is UNSUITABLE for paper results. Training continues (results are exploratory).')
    if a.check_only:
        return
    rc, tdir = train(a, ds, a.out)
    v = trust(a.out, tdir, sv_verdict, extra, a.fp_budget)
    log(f'TRUST VERDICT: {v}. Read {os.path.join(a.out, "TRUST_REPORT.md")}')


if __name__ == '__main__':
    main()
