#!/usr/bin/env python3
"""
check_collection.py -- read-only health view of a running (or finished) O-RAN collection
(`mild-oran status` once, `mild-oran watch` every 60 s).

It never writes into the state folder, so it is safe to run in another window while the
collector is running. Per finished segment it shows the QA verdict and the numbers behind it:

  status      complete / truncated (cut before an outage) / bad (QA BAD; kept, good windows salvaged)
  verdict     OK / WARN / BAD from the per-segment QA (reasons in segment_verdicts.csv)
  salv        ticks salvaged from a BAD segment (its good windows)
  miss%       ticks with at least one KPI not measured          (expected ~0; BAD > 20 %)
  tick_s      mean wall length of a tick                         (expected ~4.0 s; overload > 4.4)
  e2_hz       E2SM-KPM indications per second at xKPM            (expected ~1.6-1.9: styles 1 + 4, ~1.1 s each)
  radio       ZMQ radio speed, x real time                       (expected ~0.85-0.95)
  snet10/sri10  10th percentile of snet / sri in healthy time    (expected well above 90 / 85)
  obs%        injected episodes whose failure was observed       (expected >= 70-80 %)
  mimicX      benign mimics that crossed a target                (expected 0-1 per segment)
  xtalk       unlabelled crossings of other intents in windows   (expected small)
Plus: the running segment's progress, how long ago the collector last wrote its log, the last
learnability verdict, and the time left to the target.
"""
import argparse
import glob
import json
import os
import signal
import sys
import time

signal.signal(signal.SIGPIPE, signal.SIG_DFL)   # `mild-oran status | head` must not print a traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def load(p, d=None):
    try:
        return json.load(open(p))
    except Exception:
        return d


def once(state, target):
    rows = []
    running = None
    for d in sorted(glob.glob(os.path.join(state, 'segments', 'seg_*'))):
        m = load(os.path.join(d, 'meta.json'), {}) or {}
        q = load(os.path.join(d, 'qa.json'), {}) or {}
        if m.get('status') == 'running':
            try:
                n = sum(1 for _ in open(os.path.join(d, 'rows.csv'))) - 1
            except Exception:
                n = 0
            running = (os.path.basename(d), n, m.get('started'))
            continue
        h = q.get('health') or {}
        ev = q.get('events') or {}
        n_ev = sum(g['n'] for g in ev.values())
        obs = sum(g['observed'] for g in ev.values())
        rnd = lambda v, p=2: None if v is None else round(v, p)  # noqa: E731
        salv = sum(b - a for a, b in m.get('good_windows') or []) if m.get('status') == 'bad' else None
        rows.append({'segment': os.path.basename(d), 'status': m.get('status'), 'ticks': m.get('ticks'),
                     'verdict': q.get('verdict', '-'), 'salv': salv,
                     'miss%': None if h.get('missing_row_frac') is None else round(100 * h['missing_row_frac'], 1),
                     'tick_s': h.get('tick_mean_s'), 'e2_hz': rnd(h.get('e2_hz_median')),
                     'radio': rnd(h.get('radio_speed_median')),
                     'snet10': None if h.get('healthy_snet_p10') is None else round(h['healthy_snet_p10'], 1),
                     'sri10': None if h.get('healthy_sri_p10') is None else round(h['healthy_sri_p10'], 1),
                     'events': n_ev, 'obs%': round(100 * obs / n_ev) if n_ev else None,
                     'mimicX': h.get('mimic_crossings'), 'xtalk': h.get('crosstalk_crossings'),
                     'ran_down': 'YES' if m.get('ran_down') else '', 'watchdog': 'YES' if m.get('watchdog') else ''})
    print(time.strftime('%Y-%m-%d %H:%M:%S'), 'state', state)
    if rows:
        import pandas as pd
        pd.set_option('display.width', 220)
        pd.set_option('display.max_rows', 500)
        df = pd.DataFrame(rows)
        print(df.to_string(index=False))
        ok = df[df['status'].isin(['complete', 'truncated'])]
        total = int(ok['ticks'].fillna(0).sum()) + int(df['salv'].fillna(0).sum())
        print(f"\nusable ticks: {total} / {target}   verdicts: {df['verdict'].value_counts().to_dict()}")
        last = df.tail(3)
        bad = last[last['verdict'] == 'BAD']
        if len(bad):
            print(f'!! {len(bad)} of the last 3 segments are BAD: read segment_verdicts.csv for the reasons')
    else:
        total = 0
        print('no finished segment yet')
    if running:
        print(f'running: {running[0]} at tick {running[1]} (started {running[2]})')
    log = os.path.join(state, 'pipeline.log')
    if os.path.exists(log):
        age = time.time() - os.path.getmtime(log)
        print(f'pipeline.log last written {int(age)} s ago' + ('  <-- collector stopped or waiting for the RAN?' if age > 180 else ''))
        try:
            tail = open(log).read().splitlines()[-4:]
            print('last log lines:\n  ' + '\n  '.join(tail))
        except Exception:
            pass
    esc = os.path.join(state, 'ESCALATION.md')
    if os.path.exists(esc):
        print(f'!! ESCALATION: the self-repair gave up on a problem; read {esc}')
    rem = os.path.join(state, 'remedies.jsonl')
    if os.path.exists(rem):
        try:
            last = [json.loads(x) for x in open(rem).read().splitlines()[-3:]]
            print('last self-repair actions:')
            for r in last:
                print(f"  {r.get('time')} {r.get('what')} " + json.dumps({k: v for k, v in r.items()
                                                                       if k not in ('time', 'what', 'observed')})[:160])
        except Exception:
            pass
    ran = load(os.path.join(state, 'ran', 'last_up.json'))
    if ran:
        print(f"RAN last brought up {ran.get('time')}: {ran.get('n_ues')} UEs, steps {ran.get('steps')}, "
              f"radio {ran.get('radio_speed')}, E2SM-RC offered {ran.get('rc_offered')}")
    att = glob.glob(os.path.join(state, 'attempts', 'attempt_*', 'attempt.json'))
    if att:
        print('archived attempts (their good data is still merged): ' +
              ', '.join(f"{os.path.basename(os.path.dirname(p))}={(load(p, {}) or {}).get('usable_ticks')} ticks" for p in att))
    lh = load(os.path.join(state, 'learncheck', 'learnability.json'), []) or []
    if lh:
        x = lh[-1]
        print(f"last learnability check {x['time']}: {x['verdict']} "
              f"{ {k: v.get('grade', v.get('note')) for k, v in x.get('intents', {}).items()} } "
              f"root={x.get('root', {}).get('grade', x.get('root', {}).get('note'))}")
    cfg = load(os.path.join(state, 'config_used.json'), {}) or {}
    tick = float(cfg.get('tick_s', 4.0))
    left = max(0, target - total)
    print(f'time to target at {tick:g} s per tick: ~{left * tick / 3600:.1f} h of collection')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state', default=os.path.expanduser('~/intact/mild/state'))
    ap.add_argument('--target', type=int, default=0)
    ap.add_argument('--watch', type=int, default=0, help='repeat every N seconds')
    a = ap.parse_args()
    cfg = load(os.path.join(a.state, 'config_used.json'), {}) or {}
    target = a.target or int(cfg.get('target_ticks', 36000))
    while True:
        if a.watch:
            os.system('clear')
        once(a.state, target)
        if not a.watch:
            break
        time.sleep(a.watch)


if __name__ == '__main__':
    main()
