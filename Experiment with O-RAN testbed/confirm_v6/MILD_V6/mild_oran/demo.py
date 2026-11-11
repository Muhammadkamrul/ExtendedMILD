"""demo.py -- live MILD demo (Algorithm 1) on a fresh fault schedule of the live testbed (`mild-oran demo`).

The deployed model scores every tick online; alerts print the intent at risk, the root-cause intent and the
driving KPIs. Nothing is written into the dataset; files go to <state>/demo/<time>/.
"""
import json
import os
import sys
import time

from . import util


def run_demo(stack, cfg, cal, deploy_dir, ticks, should_stop, repro_dir):
    """Online MILD on a fresh schedule (never written into the dataset)."""
    import csv
    import numpy as np
    from .collector import TickRunner, run_ticks
    from .deploy import MildOnline
    from .scheduler import build_plan
    out = os.path.join(cfg['state_dir'], 'demo', time.strftime('%Y%m%d_%H%M%S'))
    os.makedirs(out, exist_ok=True)
    mild = MildOnline(deploy_dir)
    dcfg = dict(cfg, events_per_segment=max(4, int(ticks / 45)))
    plan, labels = build_plan(dcfg, cfg['base_seed'] + 90000 + int(time.time()) % 1000, ticks)
    util.save_json(os.path.join(out, 'plan.json'), {'plan': plan, 'labels': labels})
    rows, evs = [], []
    f = open(os.path.join(out, 'demo.csv'), 'w', newline='')
    cols = ['t', 'active'] + [f'{a}_{n}' for a in ('p', 'ema', 'gate') for n in mild.intents] + ['alerts', 'root', 'top_kpis']
    w = csv.DictWriter(f, fieldnames=cols)
    w.writeheader()
    state = {'prev': set()}

    def on_row(row, truth):
        r = mild.step(row)
        rec = {'t': row['t'], 'active': truth['active'], 'alerts': ';'.join(r['alerts']), 'root': r['root'] or '',
               'top_kpis': ' '.join(f'{a}({b:+.3f})' for a, b in r['top_kpis'])}
        for n in mild.intents:
            rec[f'p_{n}'], rec[f'ema_{n}'], rec[f'gate_{n}'] = round(r['p'][n], 4), round(r['ema'][n], 4), round(r['gate'][n], 4)
        w.writerow(rec)
        f.flush()
        rows.append((row['t'], r))
        cur = set(r['alerts'])
        if cur != state['prev']:
            if cur:
                util.log(f"MILD ALERT t={row['t']}: intents {sorted(cur)}  root cause -> {r['root']} "
                         f"(gate {max(r['gate'].values()):.2f})  driving KPIs: {rec['top_kpis']}  | injected: [{truth['active']}]")
            else:
                util.log(f"MILD: alerts cleared at t={row['t']}  | injected: [{truth['active']}]")
            state['prev'] = cur

    runner = TickRunner(stack, cfg, cal['targets'], util.load_json(os.path.join(cfg['state_dir'], 'current_scales.json'),
                                                                   cal['scales']))
    stack.reset()
    ok = run_ticks(runner, plan, labels, 0, ticks, on_row, evs.append, lambda: should_stop() or bool(stack.check_alive()))
    f.close()
    obs = {'api': [], 'analytics': [], 'telemetry': []}
    for e in evs:
        if e['failure_observed']:
            obs[e['intent']].append({k: e[k] for k in ('start', 'failure_time', 'end', 'type')})
    util.save_json(os.path.join(out, 'events_by_intent.json'), obs)
    if rows and any(obs.values()):
        sys.path.insert(0, repro_dir)
        from mild_repro import core
        T = np.array([t for t, _ in rows])
        ema = {n: np.array([r['ema'][n] for _, r in rows]) for n in mild.intents}
        alerts = {n: T[np.array([n in r['alerts'] for _, r in rows])] for n in mild.intents}
        gate = np.array([[r['gate'][n] for n in mild.intents] for _, r in rows])
        m, rec, _ = core.evaluate(alerts, obs, T, ema, gate_probs=gate, fold_t0=T[0])
        rec.to_csv(os.path.join(out, 'demo_events.csv'), index=False)
        util.save_json(os.path.join(out, 'demo_metrics.json'), m)
        util.log('DEMO RESULT (online, this schedule only): ' + json.dumps(
            {k: {'detected': f"{v['tp']}/{v['tp'] + v['fn']}", 'lead_ticks': round(v['avg_lead_time'], 1)}
             for k, v in m['per_intent'].items()}) + f"  root-cause accuracy {100 * m['overall']['disamb_accuracy']:.0f} % "
            f"over {m['overall']['num_cause_events']} root events, FP steps/day {m['overall']['fp_per_day']:.1f}")
    util.log(f'demo files: {out} (demo.csv has one row per tick: risks, EWMA, gate, alerts, root cause, KPIs)')
    return ok
