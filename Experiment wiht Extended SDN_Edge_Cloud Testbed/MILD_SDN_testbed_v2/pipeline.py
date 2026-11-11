#!/usr/bin/env python3
"""
pipeline.py -- fully automatic, resumable SDN testbed pipeline (Task 3).

  sudo -E env PATH="$PATH" python pipeline.py --state ~/mild_sdn2_state

Phases (each one is skipped when already completed, so re-running resumes):
  1. start      preflight (binaries, OVS, ports, leftovers), start Ryu, Mininet,
                services, exporters, Prometheus, traffic; health checks
  2. calibrate  baseline + SNET/SRI weight validation + fault-response calibration
  3. collect    900-tick segments (1 h at 4 s per tick) with per-segment seeds, until
                --target_ticks is reached and the data is sufficient. Severity
                adapts between segments.
  4. dataset    merge + quality report (valid? sufficient?)
  5. train      MILD and all baselines: development/test split, equal-budget tuning,
                held-out test, significance tests, AUTOTRAIN_REPORT.md
  6. summary    PIPELINE_SUMMARY.md

Stop at any time with Ctrl+C. The current segment is cut at its last quiet
tick, the testbed is torn down cleanly, and the next run continues from there.

Options:
  --mock                run without Mininet/Ryu (local processes) to test the pipeline
  --phase X             run only phase X (start|calibrate|collect|dataset|train|summary)
  --target_ticks N      stop collecting after N ticks (default from config)
  --hours H             alternative: collect for at most H wall-clock hours in this invocation
  --train_python PATH   python with TensorFlow for phase 5 (default: same interpreter)
  --set key=value       override any config value (repeatable, JSON values allowed)
  --selftest            1-minute check of calibration, collection, quality gate and training with a
                        process-free model of the testbed (no root, no Docker; data NOT for the paper)
"""
import argparse
import json
import os
import pwd
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mild_sdn2 import util  # noqa: E402
from mild_sdn2 import calibrate as C  # noqa: E402
from mild_sdn2.collector import SegmentStore, adapt_scales, run_segment, segment_qa  # noqa: E402
from mild_sdn2.quality import assess  # noqa: E402
from mild_sdn2 import salvage as SV  # noqa: E402

STOP = {'flag': False}


def _sig(*_):
    if not STOP['flag']:
        util.log('stop requested: finishing the current tick, then cutting the segment and shutting down', 'WARN')
    STOP['flag'] = True


def load_cfg(a):
    cfg = json.load(open(os.path.join(HERE, 'config', 'testbed.json')))
    for kv in a.set or []:
        k, v = kv.split('=', 1)
        try:
            v = json.loads(v)
        except Exception:
            pass
        cfg[k] = v
    if getattr(a, 'selftest', False):
        cfg.update({'tick_s': 0.04, 'sample_s': 0.01, 'segment_ticks': 700, 'min_segment_ticks': 100, 'target_ticks': 7000,
                    'warmup_ticks': 20, 'calib_baseline_ticks': 60, 'weight_sweep_hold_s': 0.3, 'calib_max_iter': 6,
                    'min_total_ticks': 5000, 'min_events_per_intent': 20, 'min_codrift_pairs': 5,
                    'events_per_segment': 16, 'dur_min': 14, 'dur_max': 28, 'mimic_dur_min': 6, 'mimic_dur_max': 12, 'min_gap': 10,
                    'p_nonlinear': 0.35, 'p_codrift': 0.35, 'p_independent': 0.30,
                    'train': dict(cfg['train'], trials=2, seeds='42', test_frac=0.3)})
    if a.target_ticks:
        cfg['target_ticks'] = a.target_ticks
    cfg['mock'] = a.mock
    cfg['state_dir'] = a.state
    cfg['selftest'] = getattr(a, 'selftest', False)
    return cfg


def make_stack(cfg, run_dir):
    if cfg.get('selftest'):
        from mild_sdn2.fakestack import FakeStack
        return FakeStack(cfg, run_dir)
    from mild_sdn2.stack import MockStack, RealStack
    return (MockStack if cfg['mock'] else RealStack)(cfg, run_dir)


def phase_calibrate(stack, cfg, st_path, state):
    cal_path = os.path.join(cfg['state_dir'], 'calibration.json')
    cal = util.load_json(cal_path) or {'targets': dict(cfg['targets']), 'scales': dict(cfg['initial_scales'])}
    C.SHOULD_STOP['fn'] = lambda: STOP['flag'] or bool(stack.check_alive())

    def save(c):
        util.save_json(cal_path, c)
    cal = C.step_baseline(stack, cfg, cal)
    save(cal)
    cal = C.step_weights(stack, cfg, cal, stack.weights_path)
    save(cal)
    if 'final_weights' in cal.get('weights', {}):
        util.save_json(os.path.join(cfg['state_dir'], 'snet_sri_weights.json'), cal['weights']['final_weights'])
    cal = C.step_faults(stack, cfg, cal, save)
    save(cal)
    C.write_report(cal, os.path.join(cfg['state_dir'], 'calibration_report.md'))
    state['calibrated'] = True
    util.save_json(st_path, state)
    return cal


def chown_to_user(path):
    u = os.environ.get('SUDO_USER')
    if u and os.geteuid() == 0:
        pw = pwd.getpwnam(u)
        subprocess.run(['chown', '-R', f'{pw.pw_uid}:{pw.pw_gid}', path])
        return u
    return None


def phase_train(cfg, a, ds_dir):
    tr = cfg['train']
    repo = os.path.abspath(os.path.join(HERE, '..', 'MILD_unified_repro'))
    script = os.path.join(repo, 'autotrain.py')
    if not os.path.exists(script):
        raise RuntimeError(f'autotrain.py not found at {script}. Keep MILD_unified_repro next to this folder.')
    out = os.path.join(cfg['state_dir'], 'training')
    cmd = [a.train_python or sys.executable, script, '--data_csv', os.path.join(ds_dir, 'dataset.csv'),
           '--events_json', os.path.join(ds_dir, 'events_by_intent.json'), '--out', out,
           '--paper_profile', tr['paper_profile'], '--trials', str(tr['trials']), '--fp_budget', str(tr['fp_budget']),
           '--test_frac', str(tr['test_frac']), '--seeds', tr['seeds']]
    user = chown_to_user(cfg['state_dir'])
    if tr.get('fp_block'):
        cmd += ['--fp_block', str(tr['fp_block'])]
    if user:
        cmd = ['sudo', '-u', user, '-H', 'env', f"PATH={os.environ.get('PATH', '')}",
               f"LD_LIBRARY_PATH={os.environ.get('LD_LIBRARY_PATH', '')}"] + cmd
    util.log('training: ' + ' '.join(cmd))
    with open(os.path.join(cfg['state_dir'], 'training.log'), 'a') as lf:
        rc = subprocess.call(cmd, cwd=repo, stdout=lf, stderr=subprocess.STDOUT)
    if rc != 0:
        raise RuntimeError(f'training failed (rc={rc}); see {cfg["state_dir"]}/training.log')
    return out


def write_summary(cfg, rep, train_dir):
    L = ['# SDN testbed pipeline summary', '', f'state folder: {cfg["state_dir"]}', '']
    cal = util.load_json(os.path.join(cfg['state_dir'], 'calibration.json'), {})
    L += ['## Calibration', '', f"targets: {cal.get('targets')}", f"weights passed: {cal.get('weights', {}).get('passed')}",
          f"fault calibration passed: {cal.get('faults_passed')}", '']
    if rep:
        L += ['## Dataset', '', f"rows {rep['rows']}, segments {rep['segments']}, valid={rep['valid']}, "
              f"sufficient={rep['sufficient']}", f"events per intent: {rep['events_per_intent']}", '']
    if train_dir and os.path.exists(os.path.join(train_dir, 'AUTOTRAIN_REPORT.md')):
        L += ['## Training (see training/AUTOTRAIN_REPORT.md)', '']
        txt = open(os.path.join(train_dir, 'AUTOTRAIN_REPORT.md')).read()
        i = txt.find('## 4. Verdict')
        L += [txt[i:txt.find('## 5.')] if i >= 0 else '(verdict not available)']
    with open(os.path.join(cfg['state_dir'], 'PIPELINE_SUMMARY.md'), 'w') as f:
        f.write('\n'.join(L))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state', default=os.path.expanduser('~/mild_sdn2_state'))
    ap.add_argument('--mock', action='store_true')
    ap.add_argument('--phase', default='all')
    ap.add_argument('--target_ticks', type=int, default=0)
    ap.add_argument('--hours', type=float, default=0)
    ap.add_argument('--train_python', default='')
    ap.add_argument('--set', action='append')
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--calibration_from', default='',
                    help='state folder of an earlier run: reuse its calibration (targets, SNET/SRI weights, scales) '
                         'in a new, empty --state folder and skip phase 2')
    a = ap.parse_args()
    os.makedirs(a.state, exist_ok=True)
    if a.calibration_from:
        src = os.path.abspath(os.path.expanduser(a.calibration_from))
        stp = os.path.join(a.state, 'state.json')
        st0 = json.load(open(stp)) if os.path.exists(stp) else {}
        if not st0.get('calibrated'):
            if not os.path.exists(os.path.join(src, 'calibration.json')):
                sys.exit(f'--calibration_from: no calibration.json in {src}')
            import shutil
            for f in ('calibration.json', 'calibration_report.md', 'snet_sri_weights.json'):
                if os.path.exists(os.path.join(src, f)):
                    shutil.copy(os.path.join(src, f), a.state)
            st0.update({'calibrated': True, 'calibration_copied_from': src, 'scales_reset_v2': True})
            with open(stp, 'w') as fh:
                json.dump(st0, fh, indent=2)
            print(f'calibration copied from {src} (phase 2 will be skipped)', flush=True)
    cfg = load_cfg(a)
    util.set_log(os.path.join(a.state, 'pipeline.log'))
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    st_path = os.path.join(a.state, 'state.json')
    state = util.load_json(st_path, {})
    util.save_json(os.path.join(a.state, 'config_used.json'), cfg)
    store = SegmentStore(a.state)
    for i in store.list():                       # repair interrupted segments first
        if store.meta(i).get('status') == 'running':
            store.repair(i, cfg['min_segment_ticks'])
    if not cfg.get('selftest'):
        sv = SV.salvage_state(a.state, cfg['min_segment_ticks'])   # exclude API-jam intervals of collector v1
        if sv:
            SV.write_report(sv, os.path.join(a.state, 'SALVAGE_REPORT.md'))
            kept = [r for r in sv if r['status'] != 'excluded']
            util.log(f"salvage: {len(sv)} version-1 segments, {len(kept)} kept, "
                     f"{sum(r['clean_ticks'] for r in kept)} clean ticks (see SALVAGE_REPORT.md)")
    phases = ['start', 'calibrate', 'collect', 'dataset', 'train', 'summary'] if a.phase == 'all' else [a.phase]
    need_stack = any(p in phases for p in ('start', 'calibrate', 'collect')) and \
        (not state.get('collection_done') or a.phase in ('start', 'calibrate', 'collect'))
    stack, rep, train_dir = None, None, None
    t_begin = time.time()
    try:
        if need_stack:
            run_dir = os.path.join(a.state, 'runs', time.strftime('%Y%m%d_%H%M%S'))
            os.makedirs(run_dir, exist_ok=True)
            w = os.path.join(a.state, 'snet_sri_weights.json')
            if os.path.exists(w):
                util.save_json(os.path.join(run_dir, 'snet_sri_weights.json'), util.load_json(w))
            stack = make_stack(cfg, run_dir)
            util.log(f'phase 1: starting the testbed ({"self-test model" if cfg.get("selftest") else ("mock" if cfg["mock"] else "Mininet + Ryu")})')
            stack.start()
            util.save_json(os.path.join(run_dir, 'stack_manifest.json'), stack.manifest())
        cal = util.load_json(os.path.join(a.state, 'calibration.json'), {})
        if 'calibrate' in phases and stack and not state.get('calibrated'):
            util.log('phase 2: calibration and validation')
            cal = phase_calibrate(stack, cfg, st_path, state)
        if 'collect' in phases and stack and not state.get('collection_done'):
            if not state.get('calibrated'):
                raise RuntimeError('run calibration first (phase calibrate)')
            scales = util.load_json(os.path.join(a.state, 'current_scales.json'), cal['scales'])
            v1 = [j for j in store.list() if store.meta(j).get('code_version', 1) < 2]
            if not state.get('scales_reset_v2') and not v1:
                state['scales_reset_v2'] = True          # fresh state: nothing to reset
                util.save_json(st_path, state)
            if not state.get('scales_reset_v2'):
                # collector v1 let the API severity run away (7.5 -> 40) because unmeasurable
                # episodes looked like missed failures. Restart from the calibrated values once.
                old = dict(scales)
                for k in ('api', 'nl_api'):
                    scales[k] = cal['scales'].get(k, scales[k])
                for k, (lo, hi) in cfg['scale_bounds'].items():
                    if k in scales:
                        scales[k] = min(hi, max(lo, scales[k]))
                util.save_json(os.path.join(a.state, 'current_scales.json'), scales)
                util.log(f'scales reset to calibrated values: api {old.get("api")} -> {scales["api"]}, '
                         f'nl_api {old.get("nl_api")} -> {scales["nl_api"]}')
                state['scales_reset_v2'] = True
                util.save_json(st_path, state)
            util.log(f'phase 3: collection, {store.total_ticks()} / {cfg["target_ticks"]} ticks so far')
            while store.total_ticks() < cfg['target_ticks'] and not STOP['flag']:
                if a.hours and time.time() - t_begin > a.hours * 3600:
                    util.log(f'--hours {a.hours} reached; stopping this invocation (resume later)')
                    break
                i = (max(store.list()) + 1) if store.list() else 0
                util.log(f'segment {i} (seed {cfg["base_seed"] + i}) started')
                res = run_segment(stack, cfg, cal['targets'], scales, store, i,
                                  lambda: STOP['flag'] or bool(stack.check_alive()))
                if res == 'unhealthy' or stack.check_alive():
                    why = 'watchdog: testbed unresponsive' if res == 'unhealthy' else f'processes died: {stack.check_alive()}'
                    util.log(f'{why} -> restarting the testbed', 'WARN')
                    stack.stop()
                    stack = make_stack(cfg, os.path.join(a.state, 'runs', time.strftime('%Y%m%d_%H%M%S')))
                    stack.start()
                qa = segment_qa(store, i)
                util.save_json(os.path.join(store.path(i), 'qa.json'), qa)
                util.log(f'segment {i} QA: ' + json.dumps({k: (v['n'], round(v['observed_frac'], 2)) for k, v in qa.items()}))
                v2 = [j for j in store.finished() if store.meta(j).get('code_version', 1) >= 2]
                recent = [util.load_json(os.path.join(store.path(j), 'qa.json'), {}) for j in v2[-2:]]
                scales = adapt_scales(scales, recent, cfg['scale_bounds'], os.path.join(a.state, 'scale_history.json'))
                util.save_json(os.path.join(a.state, 'current_scales.json'), scales)
                util.log(f'progress: {store.total_ticks()} / {cfg["target_ticks"]} ticks')
            if store.total_ticks() >= cfg['target_ticks']:
                state['collection_done'] = True
                util.save_json(st_path, state)
    except C.Interrupted as exc:
        dead = stack.check_alive() if stack else []
        util.log(f'{exc}' + (f'; dead processes: {dead}' if dead else ''), 'WARN')
        STOP['flag'] = True
    finally:
        if stack:
            util.log('stopping the testbed')
            stack.stop()
    if STOP['flag']:
        util.log('stopped by user. Re-run the same command to resume.')
        return
    if 'dataset' in phases and store.finished():
        util.log('phase 4: merging segments and checking data quality')
        cal = util.load_json(os.path.join(a.state, 'calibration.json'), {})
        rep, ds_dir = assess(a.state, cfg, cal['targets'])
        util.log(f"dataset: rows={rep['rows']} valid={rep['valid']} sufficient={rep['sufficient']} checks={rep['checks']}")
        if not rep['sufficient'] and state.get('collection_done'):
            util.log('data not yet sufficient: raise --target_ticks and re-run to collect more', 'WARN')
        if 'train' in phases:
            if rep['valid'] and (rep['sufficient'] or a.phase == 'train'):
                util.log('phase 5: training MILD and baselines')
                train_dir = phase_train(cfg, a, ds_dir)
            else:
                util.log('phase 5 skipped: dataset not valid/sufficient yet (see dataset/quality_report.md)', 'WARN')
    if 'summary' in phases:
        write_summary(cfg, rep, train_dir or os.path.join(a.state, 'training'))
        util.log(f'summary written: {os.path.join(a.state, "PIPELINE_SUMMARY.md")}')


if __name__ == '__main__':
    main()
