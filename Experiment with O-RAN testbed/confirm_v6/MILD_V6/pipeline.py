#!/usr/bin/env python3
"""
pipeline.py -- resumable, self-repairing MILD data collection on the O-RAN testbed (`mild-oran collect`).

  sudo -E env PATH="$PATH" <venv>/bin/python pipeline.py --state ~/intact/mild/state

Phases (each is skipped when already completed, so re-running the same command resumes):
  1. preflight  automated checks: sudo without password, testbed scripts, FlexRIC + E2 front end built,
                multi-UE setup, gNB E2 configs, netem, ports, CPUs, disk
  2. ran        the RAN is brought up by MILD itself, in the testbed order (ranctl.py): intact-down ->
                core health -> nearRT-RIC -> jrt-controller (jbpf configs) -> gNB with E2 -> proxy ->
                intact-multiue up (N UEs) -> checks. A healthy running RAN is reused.
  3. start      the MILD components on top (stack.py): edge app, one probe agent per UE namespace, DL
                traffic, xSLA, xRC, xKPM (each with its own FlexRIC E2 front end), clients; E2 indications
                with all N UEs; UE identity map (F1AP UE ID -> ueK) by traffic tests
  4. calibrate  plant (capacity, per-UE probe floor) + E2SM-RC check + baseline (targets) + snet/sri weights
                + fault-response calibration -> calibration.json / calibration_report.md
  5. collect    30-min segments (450 ticks of 4 s), each with its own seed, until --target_ticks:
                  every tick      watchdogs (RAN down -> automatic RAN restart; components dead -> restart)
                  every 5 min     mini-QA (remedy.Doctor): restart a hung component, rotate the E2 front
                                  ends, lower the load, or cut the segment
                  every segment   QA verdict (qa.json) -> remedy ladder (restart stack / RAN, re-derive the
                                  targets, lower the load, ...) -> severity adaptation; BAD segments are kept
                                  and their good windows salvaged
                  every 2 segments learnability check (1 h)
                Escalation (ESCALATION.md + stop) only when the ladder of a problem is exhausted.
  6. dataset    merge (whole segments + salvaged windows + archived attempts) + quality report
  7. summary    PIPELINE_SUMMARY.md
Training is a separate command (`mild-oran train`, train_now.py), in its own window.

Stop at any time with Ctrl+C: the current tick finishes, the segment is cut at its last quiet tick,
netem is removed and every MILD process is stopped (the RAN keeps running unless ran.stop_ran_on_exit).

Options:
  --phase X              run only phase X (preflight|ran|calibrate|collect|dataset|summary|learncheck|demo)
  --target_ticks N       collect until N usable ticks (default config: 36000 = 40 h)
  --hours H              stop this invocation after H hours (resume later with the same command)
  --set key=value        override any config value (repeatable; dotted keys for nested values)
  --new_attempt          archive the segments collected so far (attempts/attempt_NN/, nothing deleted; their
                         good data stays usable) and start a fresh collection with the same calibration
  --mock_ran             real MILD components, stand-in RAN and E2 front end, no netns/tc (test without the RAN)
  --selftest             process-free model of the whole testbed (data NOT for the paper)
  --keep_going           never stop on bad data (QA/learnability/escalation); only warn
  --skip_preflight       start without the preflight (not recommended)
  --recalibrate          redo the calibration (keeps collected segments; new segments use new targets)
  --phase demo --deploy DIR [--demo_ticks N]   live MILD demo (Algorithm 1) on the live testbed
"""
import argparse
import json
import os
import shutil
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mild_oran import util  # noqa: E402
from mild_oran import calibrate as C  # noqa: E402
from mild_oran.collector import SegmentStore, adapt_scales, run_segment, segment_qa  # noqa: E402
from mild_oran.quality import assess  # noqa: E402
from mild_oran.ranctl import make_ranctl  # noqa: E402
from mild_oran.remedy import Doctor  # noqa: E402
from mild_oran.stack import make_stack  # noqa: E402

STOP = {'flag': False}
LOCK = {'f': None}


def collector_lock(st, take, what=''):
    """One collector (or demo/preflight) per state folder. take=True holds the lock for the life of this process;
    take=False only tests whether another process holds it. Returns (free, holder description)."""
    import fcntl
    p = os.path.join(st, '.collector.lock')
    if not take and not os.path.exists(p):
        return True, ''
    try:
        f = open(p, 'a+') if take else open(p, 'r')
    except OSError:
        return True, ''
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.seek(0)
        who = f.read().strip()
        f.close()
        return False, who
    if take:
        f.seek(0)
        f.truncate()
        f.write(f"pid {os.getpid()} since {time.strftime('%Y-%m-%d %H:%M:%S')} ({what})\n")
        f.flush()
        LOCK['f'] = f
    else:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()
    return True, ''


def _sig(*_):
    if not STOP['flag']:
        util.log('stop requested: finishing the current tick, then cutting the segment and stopping the components', 'WARN')
    STOP['flag'] = True


def _set(cfg, key, val):
    d = cfg
    parts = key.split('.')
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = val


def load_cfg(a):
    cfg = json.load(open(os.path.join(HERE, 'config', 'oran.json')))
    if a.selftest:
        cfg.update({'tick_s': 0.04, 'sample_s': 0.01, 'segment_ticks': 700, 'min_segment_ticks': 100,
                    'target_ticks': 7000, 'warmup_ticks': 20, 'calib_baseline_ticks': 60, 'weight_sweep_hold_s': 0.3,
                    'calib_max_iter': 6, 'min_total_ticks': 5000, 'min_events_per_intent': 20, 'min_codrift_pairs': 5,
                    'events_per_segment': 16, 'min_gap': 10})
        cfg['qa'] = dict(cfg['qa'], learnability_every_segments=4, learnability_min_segments=4, check_every_ticks=75)
        cfg['ran'] = dict(cfg['ran'], ue_ip_missing_s=0.15, probe_dead_s=0.25, e2_stale_s=0.5)
        cfg['train'] = dict(cfg['train'], trials=2, seeds='42')
    if a.mock_ran:
        cfg.update({'tick_s': 2.0, 'sample_s': 0.5, 'segment_ticks': 150, 'min_segment_ticks': 50, 'target_ticks': 450,
                    'warmup_ticks': 10, 'calib_baseline_ticks': 30, 'weight_sweep_hold_s': 6, 'calib_max_iter': 2,
                    'min_total_ticks': 300, 'min_events_per_intent': 3, 'min_codrift_pairs': 1,
                    'events_per_segment': 5, 'dur_min': 10, 'dur_max': 16, 'min_gap': 6, 'lag_min': 2, 'lag_max': 4,
                    'victim_min_dur': 4, 'mimic_dur_min': 4, 'mimic_dur_max': 6, 'tail_ticks': 6,
                    'calib_drain_ticks': 6, 'watchdog_bad_ticks': 20,
                    'calib_fault_types': ['independent_telemetry_slow', 'nonlinear_api_xor', 'conflicting_api_cause',
                                          'mimic_network_only'],
                    'weight_sweep_levels': [[0, 0], [40, 2], [100, 6], [160, 12]],
                    # the first stand-in RAN answers no E2SM-RC control (as the real gNB did with the v2.0 encoding):
                    # the RC check fails, the RAN is restarted, the second check passes; that RAN crashes later
                    'mock': {'outage_after_s': 780, 'outages': 2, 'rc_hang_ups': 1}})
        cfg['qa'] = dict(cfg['qa'], learnability_every_segments=99, check_every_ticks=50, salvage_min_ticks=40)
    # --set and --target_ticks are remembered in <state>/overrides.json, so a resume without them keeps them
    st_dir = os.path.abspath(os.path.expanduser(a.state))
    ov_path = os.path.join(st_dir, 'overrides.json')
    ov = util.load_json(ov_path, {}) or {}
    new = {}
    for kv in a.set or []:
        k, v = kv.split('=', 1)
        try:
            v = json.loads(v)
        except Exception:
            pass
        new[k] = v
    if a.target_ticks:
        new['target_ticks'] = a.target_ticks
    if ov and not new:
        print(f'[settings] earlier --set/--target_ticks kept from {ov_path}: {ov}', flush=True)
    ov.update(new)
    for k, v in ov.items():
        if v is None:
            continue                          # --set key=null drops a remembered override
        _set(cfg, k, v)
    ov = {k: v for k, v in ov.items() if v is not None}
    if cfg.get('scenario') in ('v3', 'v4', 'v5'):   # v2.3/v2.4/v2.7: scenario settings, unless set explicitly with --set
        from mild_oran.scheduler import SCENARIOS
        keep = set(ov)
        if a.selftest or a.mock_ran:                 # the self-test / mock keep their own timing and sizes
            keep |= {'tick_s', 'sample_s', 'calib_baseline_ticks', 'target_ticks', 'min_total_ticks',
                     'min_events_per_intent', 'min_codrift_pairs', 'weight_sweep_hold_s'}
        for k, v in SCENARIOS[cfg['scenario']].items():
            if k not in keep:
                cfg[k] = v
        cfg['_v3_user_keys'] = sorted(ov)
    if new and getattr(a, 'phase', 'all') in ('all', 'ran', 'calibrate', 'collect'):
        os.makedirs(st_dir, exist_ok=True)
        util.save_json(ov_path, ov)
    cfg['mock_ran'] = bool(a.mock_ran)
    cfg['selftest'] = bool(a.selftest)
    cfg['state_dir'] = st_dir
    return cfg


# ----------------------------------------------------------------------------- attempts
def new_attempt(st, reason='user asked (--new_attempt)'):
    """Archive the current attempt: segments and the files that describe them move to attempts/attempt_NN/.
    Nothing is deleted; quality.merge() can still use their good data (qa.include_archived)."""
    root = os.path.join(st, 'attempts')
    os.makedirs(root, exist_ok=True)
    n = 1 + max([int(d.split('_')[1]) for d in os.listdir(root) if d.startswith('attempt_')] or [0])
    dst = os.path.join(root, f'attempt_{n:02d}')
    os.makedirs(dst)
    store = SegmentStore(st)
    usable = store.total_ticks()
    for name in ('segments', 'segment_verdicts.csv', 'scale_history.json', 'current_scales.json', 'remedy_state.json',
                 'remedies.jsonl', 'learncheck', 'ESCALATION.md'):
        p = os.path.join(st, name)
        if os.path.exists(p):
            shutil.move(p, os.path.join(dst, name))
    for name in ('calibration.json', 'calibration_report.md', 'snet_sri_weights.json'):
        p = os.path.join(st, name)
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(dst, name))
    util.save_json(os.path.join(dst, 'attempt.json'), {'attempt': n, 'archived': time.strftime('%Y-%m-%d %H:%M:%S'),
                                                       'reason': reason, 'usable_ticks': usable})
    state = util.load_json(os.path.join(st, 'state.json'), {}) or {}
    state['collection_done'] = False
    state['attempt'] = n + 1
    util.save_json(os.path.join(st, 'state.json'), state)
    util.log(f'attempt {n} archived in {dst} ({usable} usable ticks kept for later use); attempt {n + 1} starts now')
    return n + 1


# ----------------------------------------------------------------------------- reporting
def write_summary(cfg, rep=None):
    st = cfg['state_dir']
    cal = util.load_json(os.path.join(st, 'calibration.json'), {}) or {}
    L = ['# MILD on O-RAN -- collection summary', '', f'state folder: {st}', '',
         '## Calibration', '', f"capacity: {cal.get('capacity_mbps')} Mbit/s; probe floor: {cal.get('probe_baseline')}",
         f"targets: {cal.get('targets')}", f"E2SM-RC: {(cal.get('rc') or {}).get('mode')} "
         f"{(cal.get('rc') or {}).get('counts')}", f"weights passed: {(cal.get('weights') or {}).get('passed')}",
         f"fault calibration passed: {cal.get('faults_passed')}", '']
    store = SegmentStore(st)
    v = {}
    for i in store.finished():
        q = util.load_json(os.path.join(store.path(i), 'qa.json'), {}) or {}
        v[q.get('verdict', '?')] = v.get(q.get('verdict', '?'), 0) + 1
    statuses = {}
    for i in store.list():
        s = store.meta(i).get('status', '?')
        statuses[s] = statuses.get(s, 0) + 1
    L += ['## Segments', '', f'segments by status: {statuses}; usable ticks (whole + salvaged): {store.total_ticks()}; '
          f'QA verdicts: {v}', '']
    rs = util.load_json(os.path.join(st, 'remedy_state.json'), {}) or {}
    if rs.get('history'):
        L += ['## Self-repair (last 10 segments with an action)', '']
        L += [f"- segment {h['segment']}: {h['verdict']} {h.get('codes')} -> {h['actions']}"
              + (f" ESCALATE {h['escalate']}" if h.get('escalate') else '')
              for h in rs['history'] if h.get('actions') or h.get('escalate')][-10:]
        L += [f"- current load tuning: {rs.get('tuning')}", '']
    att = os.path.join(st, 'attempts')
    if os.path.isdir(att):
        L += ['## Archived attempts', '']
        for d in sorted(os.listdir(att)):
            a = util.load_json(os.path.join(att, d, 'attempt.json'), {}) or {}
            L.append(f"- {d}: archived {a.get('archived')}, usable ticks {a.get('usable_ticks')}, reason {a.get('reason')}")
        L.append('')
    if rep:
        L += ['## Dataset', '', f"rows {rep['rows']}, pieces {rep['segments']}, valid={rep['valid']}, "
              f"sufficient={rep['sufficient']}", f"events per intent: {rep['events_per_intent']}",
              f"checks: {rep['checks']}", '']
    lh = util.load_json(os.path.join(st, 'learncheck', 'learnability.json'), []) or []
    if lh:
        x = lh[-1]
        L += ['## Last learnability check', '', f"{x['time']}: {x['verdict']} (rows {x['rows']})",
              f"per intent: { {k: v.get('grade', v.get('note')) for k, v in x.get('intents', {}).items()} }",
              f"root cause: {x.get('root')}", '']
    with open(os.path.join(st, 'PIPELINE_SUMMARY.md'), 'w') as f:
        f.write('\n'.join(L) + '\n')


def append_verdict(st, i, qa, status):
    p = os.path.join(st, 'segment_verdicts.csv')
    new = not os.path.exists(p)
    h = qa.get('health') or {}
    with open(p, 'a') as f:
        if new:
            f.write('segment,verdict,status,rows,salvaged_ticks,missing_row_frac,tick_mean_s,e2_hz,radio_speed,'
                    'healthy_snet_p10,healthy_sri_p10,mimic_crossings,crosstalk,codes,reasons\n')
        reasons = ' | '.join(qa.get('bad', []) + qa.get('warn', [])).replace(',', ';')
        codes = ' '.join(f'{c}:{lv}' for c, lv in (qa.get('codes') or {}).items())
        f.write(f"{i},{qa['verdict']},{status},{h.get('rows')},{qa.get('salvaged_ticks', '')},{h.get('missing_row_frac')},"
                f"{h.get('tick_mean_s')},{h.get('e2_hz_median')},{h.get('radio_speed_median')},"
                f"{h.get('healthy_snet_p10')},{h.get('healthy_sri_p10')},"
                f"{h.get('mimic_crossings')},{h.get('crosstalk_crossings')},{codes},{reasons}\n")


# ----------------------------------------------------------------------------- the collector
class Collector:
    """Owns the RAN controller, the MILD stack and the doctor; every restart goes through here, so the order is
    always: stop the MILD stack -> (RAN down -> RAN up) -> start the MILD stack."""

    def __init__(self, cfg, a, st, doctor):
        self.cfg, self.a, self.st, self.doctor = cfg, a, st, doctor
        self.ranctl = make_ranctl(cfg, st)
        self.stack = None
        self.cal = None
        self.scales = None
        self.start_failures = 0

    def should_stop(self):
        return STOP['flag']

    def _seg_stop(self):
        return STOP['flag'] or bool(self.stack and self.stack.check_alive())

    def ran_up(self):
        ok, info = self.ranctl.ensure_up(self.should_stop)
        if not ok:
            raise RuntimeError(f'the RAN could not be brought up: {info}. See {self.st}/ran/*.log and '
                               '~/intact/logs/run/*.out (runbook M12)')
        return info

    def start_stack(self):
        run_dir = os.path.join(self.st, 'runs', time.strftime('%Y%m%d_%H%M%S'))
        os.makedirs(run_dir, exist_ok=True)
        self.stack = make_stack(self.cfg, run_dir, self.ranctl)
        self.stack.apply_tuning(self.doctor.tuning())
        util.log(f"starting the MILD components: {self.stack.info['mode']}")
        self.stack.start()
        self.doctor.stack = self.stack
        util.save_json(os.path.join(run_dir, 'stack_manifest.json'), self.stack.manifest())
        if self.cal:
            self.stack.set_capacity(self.cal.get('capacity_mbps') or self.cfg['ran'].get('capacity_nominal_mbps', 20.0))
            self.stack.set_baseline(self.cal.get('probe_baseline') or {})
        if self.scales:
            self.stack.set_scales(self.scales)
        C.SHOULD_STOP['fn'] = self._seg_stop
        util.chown_tree(self.st)

    def stop_stack(self):
        if self.stack is not None:
            util.log('stopping the MILD components')
            try:
                self.stack.stop()
            finally:
                self.stack = None
                self.doctor.stack = None

    def restart(self, kind, reason):
        """kind 'stack' or 'ran'. Clean order every time; up to 3 tries, then RAN restarts with back-off."""
        util.log(f'RESTART ({kind}): {reason}', 'WARN')
        self.stop_stack()
        for k in range(1, 4):
            if STOP['flag']:
                return False
            if kind == 'ran' or k > 1:
                ok, info = self.ranctl.restart(reason, self.should_stop)
                if not ok:
                    return False
            try:
                self.start_stack()
                self.start_failures = 0
                return True
            except RuntimeError as e:
                util.log(f'MILD components did not start (try {k}/3): {e}', 'WARN')
                self.stop_stack()
                kind = 'ran'
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state', default=os.path.expanduser('~/intact/mild/state'))
    ap.add_argument('--phase', default='all')
    ap.add_argument('--target_ticks', type=int, default=0)
    ap.add_argument('--hours', type=float, default=0)
    ap.add_argument('--set', action='append')
    ap.add_argument('--mock_ran', action='store_true')
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--keep_going', action='store_true')
    ap.add_argument('--skip_preflight', action='store_true')
    ap.add_argument('--recalibrate', action='store_true')
    ap.add_argument('--new_attempt', '--new-attempt', action='store_true')
    ap.add_argument('--ran', action='store_true', help='preflight: also bring the RAN up and check E2 and the UEs')
    ap.add_argument('--deploy', default='', help='deploy folder written by `mild-oran export` (phase demo)')
    ap.add_argument('--demo_ticks', type=int, default=450)
    a = ap.parse_args()
    cfg = load_cfg(a)
    st = cfg['state_dir']
    os.makedirs(st, exist_ok=True)
    util.set_log(os.path.join(st, 'pipeline.log'))
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    st_path = os.path.join(st, 'state.json')
    state = util.load_json(st_path, {}) or {}
    store = SegmentStore(st)
    if a.phase in ('all', 'ran', 'calibrate', 'collect', 'demo', 'preflight'):
        free, who = collector_lock(st, take=True, what=a.phase)
        if not free:
            util.log(f'another collection, demo or preflight is running on {st} ({who}). Only one may use the '
                     'testbed at a time: stop it first (Ctrl+C in its window). mild-oran status / learncheck / train '
                     'are safe meanwhile.', 'ERROR')
            sys.exit(3)
        util.save_json(os.path.join(st, 'config_used.json'), cfg)
        for i in store.list():                   # repair a segment interrupted by a hard kill
            if store.meta(i).get('status') == 'running':
                store.repair(i, cfg['min_segment_ticks'])
    else:
        free, who = collector_lock(st, take=False)
        if not free:
            util.log(f'phase {a.phase}: a collection is running in this state folder ({who}); working read-only on '
                     'the finished segments')
    if a.recalibrate:
        state['calibrated'] = False
        old = util.load_json(os.path.join(st, 'calibration.json'), {})
        if old:
            util.save_json(os.path.join(st, f'calibration_old_{time.strftime("%Y%m%d_%H%M%S")}.json'), old)
            os.remove(os.path.join(st, 'calibration.json'))
        util.save_json(st_path, state)
    if a.new_attempt and store.list():
        state['attempt'] = new_attempt(st)
        state = util.load_json(st_path, {}) or {}
        store = SegmentStore(st)
    cfg['_attempt'] = int(state.get('attempt', 1))
    phases = ['preflight', 'ran', 'start', 'calibrate', 'collect', 'dataset', 'summary'] if a.phase == 'all' \
        else [a.phase]
    t_begin = time.time()

    if a.phase == 'preflight':
        from mild_oran import preflight
        R = preflight.run(cfg, st, mock=cfg['mock_ran'] or cfg['selftest'], ran=a.ran)
        util.chown_tree(st)
        sys.exit(1 if R.fails else 0)
    if a.phase == 'learncheck':
        from mild_oran import learnability
        cal = util.load_json(os.path.join(st, 'calibration.json'), {}) or {}
        res = learnability.check(st, cfg, cal['targets'], subdir='learncheck_manual')
        print(open(os.path.join(st, 'learncheck_manual', 'LEARNABILITY_REPORT.md')).read())
        util.chown_tree(st)
        sys.exit(0 if res['verdict'] in ('PASS', 'WARN', 'INSUFFICIENT') else 1)

    doctor = Doctor(cfg, st, lambda: (col.cal or {}).get('targets') or cfg['targets'], keep_going=a.keep_going)
    col = Collector(cfg, a, st, doctor)

    if a.phase == 'ran':
        info = col.ran_up()
        util.log(f'RAN up: {info}')
        util.chown_tree(st)
        return
    if a.phase == 'demo':
        cal = util.load_json(os.path.join(st, 'calibration.json'), {}) or {}
        if not state.get('calibrated') or not cal.get('targets'):
            util.log('demo needs a calibrated state folder (run the collection first)', 'ERROR')
            sys.exit(2)
        if not a.deploy or not os.path.exists(os.path.join(a.deploy, 'mild_deploy.json')):
            util.log('demo needs --deploy <folder with mild_deploy.json> (run `mild-oran export` first)', 'ERROR')
            sys.exit(2)
        col.cal = cal
        col.scales = util.load_json(os.path.join(st, 'current_scales.json'), cal['scales'])
        try:
            col.ran_up()
            col.start_stack()
            from mild_oran.demo import run_demo
            run_demo(col.stack, cfg, cal, a.deploy, a.demo_ticks, lambda: STOP['flag'],
                     os.path.join(HERE, 'MILD_unified_repro'))
        finally:
            col.stop_stack()
            util.chown_tree(st)
        return

    need_stack = any(p in phases for p in ('start', 'calibrate', 'collect')) and \
        (not state.get('collection_done') or a.phase in ('calibrate', 'collect') or a.target_ticks)
    if state.get('collection_done') and a.target_ticks and store.total_ticks() < cfg['target_ticks']:
        state['collection_done'] = False
    rep = None
    stop_reason = None
    try:
        if need_stack and not state.get('collection_done'):
            if 'preflight' in phases and not a.skip_preflight and not cfg['selftest']:
                from mild_oran import preflight
                R = preflight.run(cfg, st, mock=cfg['mock_ran'])
                if R.fails:
                    util.log(f'preflight: {R.fails} FAIL item(s). Fix them (each line says how) and run the same command '
                             'again.', 'ERROR')
                    sys.exit(2)
            cal_path = os.path.join(st, 'calibration.json')
            col.cal = util.load_json(cal_path) or {'targets': dict(cfg['targets']), 'scales': dict(cfg['initial_scales'])}
            col.scales = util.load_json(os.path.join(st, 'current_scales.json'), None)
            util.log('phase ran: bringing the RAN up (or reusing a healthy one)')
            col.ran_up()
            ok = False
            for k in range(3):
                try:
                    col.start_stack()
                    ok = True
                    break
                except RuntimeError as e:
                    util.log(f'MILD components did not start ({e}); restarting the RAN and trying again', 'WARN')
                    col.stop_stack()
                    if STOP['flag'] or not col.ranctl.restart(f'start failed: {e}', col.should_stop)[0]:
                        break
            if not ok:
                raise RuntimeError('the MILD components could not be started on this RAN (3 tries)')

            def save(c):
                util.save_json(cal_path, c)
            cal = col.cal
            if not state.get('calibrated'):
                util.log('phase calibrate: plant, E2SM-RC check, baseline, weights, faults (about 1-2 h)')
                cal = calibrate_with_recovery(col, cfg, st, cal, save)
                C.write_report(cal, os.path.join(st, 'calibration_report.md'))
                state['calibrated'] = True
                util.save_json(st_path, state)
                util.log(f'calibration done: see {st}/calibration_report.md')
            col.cal = cal
            col.stack.set_capacity(cal.get('capacity_mbps') or cfg['ran'].get('capacity_nominal_mbps', 20.0))
            col.stack.set_baseline(cal.get('probe_baseline') or {})
            if 'collect' in phases:
                stop_reason = collect_loop(col, cfg, a, st, store, t_begin, save)
                if store.total_ticks() >= cfg['target_ticks']:
                    state = util.load_json(st_path, {}) or state
                    state['collection_done'] = True
                    util.save_json(st_path, state)
                    util.log('collection target reached')
    except C.Interrupted as exc:
        util.log(str(exc), 'WARN')
        STOP['flag'] = True
    except RuntimeError as exc:
        util.log(f'ERROR: {exc}', 'ERROR')
        STOP['flag'] = True
        stop_reason = 'error'
    finally:
        col.stop_stack()
        if cfg['ran'].get('stop_ran_on_exit') or cfg.get('mock_ran'):
            util.log('stopping the RAN (ran.stop_ran_on_exit)' if not cfg.get('mock_ran') else 'stopping the mock RAN')
            col.ranctl.down()
        util.chown_tree(st)
    if STOP['flag'] or stop_reason in ('bad_data', 'not_learnable', 'error', 'escalated'):
        util.log('stopped. Re-run the same command to resume.')
        write_summary(cfg)
        util.chown_tree(st)
        sys.exit(1 if stop_reason in ('bad_data', 'not_learnable', 'error', 'escalated') else 0)
    if 'dataset' in phases and store.pieces():
        util.log('phase dataset: merging segments (whole + salvaged + archived attempts) and checking data quality')
        cal = util.load_json(os.path.join(st, 'calibration.json'), {})
        rep, ds_dir = assess(st, cfg, cal['targets'])
        util.log(f"dataset: rows={rep['rows']} valid={rep['valid']} sufficient={rep['sufficient']} checks={rep['checks']}")
        if not rep['sufficient']:
            util.log('data not yet sufficient for paper results: collect more (raise --target_ticks), or train now on what '
                     'exists with `mild-oran train` (the trust report says how far it can be trusted)', 'WARN')
    if 'summary' in phases:
        write_summary(cfg, rep)
        util.log(f'summary written: {os.path.join(st, "PIPELINE_SUMMARY.md")}')
    util.chown_tree(st)


def calibrate_with_recovery(col, cfg, st, cal, save):
    """All calibration steps (each one resumable), with the same self-repair as the collection:
    - the RAN goes down during a step (watchdog, or the gNB died): clean restart (stack -> RAN down -> RAN up ->
      stack) and the step is redone; at most calib_max_ran_restarts times per run;
    - the E2SM-RC check fails (controls refused or never answered): one clean RAN restart and a second check; if
      that fails too, the gNB's KPM-only config is used from then on and xRC runs in shadow mode (the API intent
      and its KPI are unchanged; no control reaches the gNB; the calibration report says so)."""
    max_r = int(cfg.get('calib_max_ran_restarts', 3))
    restarts = 0
    while True:
        try:
            cal = C.step_plant(col.stack, cfg, cal)
            save(cal)
            cal = C.step_rc(col.stack, cfg, cal)
            save(cal)
            if C.rc_check_failed(cal):
                cal.setdefault('rc_history', []).append(cal.pop('rc'))
                n = len(cal['rc_history'])
                save(cal)
                if n == 1:
                    util.log('E2SM-RC check failed: restarting the RAN cleanly (a gNB that left a control unanswered '
                             'answers no further E2 request) and checking once more', 'WARN')
                    if not col.restart('ran', 'E2SM-RC check failed'):
                        raise C.Interrupted('calibration interrupted (the RAN could not be restarted after the '
                                            'failed E2SM-RC check)')
                    continue
                fb = col.ranctl.disable_rc(f'E2SM-RC check failed {n} times: {cal["rc_history"][-1].get("stuck") or ""}')
                if not fb:
                    raise RuntimeError('the E2SM-RC check failed twice and there is no KPM-only gNB config to fall '
                                       'back to (gnb_zmq_jbpf_e2.yaml). Send the diagnostics (runbook M12).')
                util.log(f'E2SM-RC check failed again: from now on the gNB uses the KPM-only config '
                         f'{os.path.basename(fb)}; xRC runs in shadow mode (it computes every control but sends none). '
                         'Delete <state>/ran_choice.json to try E2SM-RC again later.', 'WARN')
                cal['rc_fallback'] = {'gnb_cfg': fb, 'time': time.strftime('%Y-%m-%d %H:%M:%S')}
                save(cal)
                if not col.restart('ran', 'switching to the KPM-only gNB config'):
                    raise C.Interrupted('calibration interrupted (the RAN could not be restarted with the KPM-only '
                                        'config)')
                continue
            cal = C.step_baseline(col.stack, cfg, cal)
            save(cal)
            cal = C.step_weights(col.stack, cfg, cal, os.path.join(st, 'snet_sri_weights.json'))
            save(cal)
            if cfg.get('scenario') in ('v4', 'v5'):
                cal = C.step_knee(col.stack, cfg, cal, save)
                save(cal)
            cal = C.step_faults(col.stack, cfg, cal, save)
            save(cal)
            return cal
        except C.Interrupted as exc:
            if STOP['flag']:
                raise C.Interrupted(f'{exc}; re-run the same command to resume')
            restarts += 1
            try:
                ran_now = col.ranctl.status(quick=True)
            except Exception:
                ran_now = {}
            if restarts > max_r:
                raise C.Interrupted(f'{exc}; the calibration stopped after {max_r} automatic RAN restarts. RAN now: '
                                    f'{ran_now}. Send the diagnostics (runbook M12), then re-run the same command')
            util.log(f'{exc}. RAN now: {ran_now}. Restarting the RAN and resuming the calibration '
                     f'(automatic restart {restarts}/{max_r})', 'WARN')
            col.cal = cal
            if not col.restart('ran', str(exc)):
                raise C.Interrupted(f'{exc}; the RAN could not be restarted; re-run the same command to resume')


def run_actions(col, cfg, st, store, actions, save):
    """Execute the remedy ladder's actions before the next segment. Returns False if the collection must stop."""
    if not actions:
        return True
    cal = col.cal
    needs = None
    for act in actions:
        if act == 'retarget':
            tg = Doctor.retarget(st, store, cfg, cal)
            if tg and tg != cal['targets']:
                col.doctor.log('retarget (calibration self-correction)', old=cal['targets'], new=tg)
                cal['targets'] = tg
                cal.setdefault('retarget_history', []).append({'time': time.strftime('%Y-%m-%d %H:%M:%S'),
                                                               'targets': tg})
                save(cal)
        elif act == 'refresh_probe_floor':
            fl = Doctor.probe_floor(store)
            if fl:
                pb = dict(cal.get('probe_baseline') or {})
                old = pb.get('rtt_ms')
                if old and abs(fl['rtt_ms'] - old) > 0.1 * old:
                    pb['rtt_ms'] = fl['rtt_ms']
                    pb.pop('per_ue', None)                  # per-UE floors are stale too: use the cell-wide one
                    cal['probe_baseline'] = pb
                    save(cal)
                    col.doctor.log('probe floor refreshed (calibration self-correction)', old=old, new=fl['rtt_ms'])
                    if col.stack:
                        col.stack.set_baseline(pb)
        elif act == 'reduce_load':
            col.doctor.reduce_load_now('remedy ladder')
            needs = needs or 'stack'
        elif act == 'rebaseline':
            cal['baseline'] = {}
            needs = needs or 'stack'
            col.doctor.log('baseline calibration will be redone')
        elif act == 'rotate_e2':
            if col.stack and not col.stack.rotate_e2():
                needs = 'ran'
        elif act in ('restart_stack', 'remap_restart'):
            needs = needs or 'stack'
        elif act == 'restart_ran':
            needs = 'ran'
    if needs and not col.restart(needs, f'remedy ladder: {actions}'):
        return False
    if not cal.get('baseline'):
        cal = C.step_baseline(col.stack, cfg, cal)
        save(cal)
        col.cal = cal
    return True


def collect_loop(col, cfg, a, st, store, t_begin, save):
    from mild_oran import learnability
    qa_cfg = cfg['qa']
    cal = col.cal
    scales = util.load_json(os.path.join(st, 'current_scales.json'), cal['scales'])
    col.scales = scales
    col.stack.set_scales(scales)
    util.log(f"phase collect: {store.total_ticks()} / {cfg['target_ticks']} usable ticks so far "
             f"(attempt {cfg.get('_attempt', 1)})")
    fail_checks = 0
    last_check = len(store.finished())
    first = True
    while store.total_ticks() < cfg['target_ticks'] and not STOP['flag']:
        if a.hours and time.time() - t_begin > a.hours * 3600:
            util.log(f'--hours {a.hours} reached; stopping this invocation (resume later with the same command)')
            return 'hours'
        if not first and cfg['ric'].get('gw_rotate_each_segment', True) and not cfg.get('selftest'):
            col.stack.rotate_e2()                 # fresh E42 sessions + fresh FlexRIC SQLite files
        first = False
        i = (max(store.list()) + 1) if store.list() else 0
        if cfg.get('selftest'):                   # the self-test also exercises the self-repair
            inj = {1: 'hang', 3: 'slow', 5: 'outage'}.get(i)
            if inj:
                util.log(f'self-test: injecting "{inj}" into segment {i}')
                col.stack.inject(inj)
        util.log(f"segment {i} (seed {cfg['base_seed'] + i}) started; {cfg['segment_ticks']} ticks x {cfg['tick_s']} s")
        res = run_segment(col.stack, cfg, cal['targets'], scales, store, i, col._seg_stop, doctor=col.doctor)
        m = store.meta(i)
        actions, esc = [], []
        if m.get('status') in ('complete', 'truncated'):
            qa = segment_qa(store, i, cfg, cal['targets'])
            if qa['verdict'] == 'BAD':
                gw = qa.get('good_windows') or []
                store.set_meta(i, status='bad', bad_reasons=qa['bad'], good_windows=gw)
                util.log(f"segment {i} is BAD; kept on disk; salvaged {qa.get('salvaged_ticks', 0)} ticks in "
                         f"{len(gw)} good window(s) {gw}", 'WARN')
            util.save_json(os.path.join(store.path(i), 'qa.json'), qa)
            append_verdict(st, i, qa, store.meta(i).get('status'))
            ev = {k: (v['n'], round(v['observed_frac'], 2)) for k, v in qa['events'].items()}
            util.log(f"segment {i} QA verdict {qa['verdict']}: events {ev}")
            for r in qa['bad']:
                util.log(f'  BAD : {r}', 'WARN')
            for r in qa['warn']:
                util.log(f'  WARN: {r}', 'WARN')
            actions, esc = col.doctor.plan_after_segment(i, qa)
            recent = [util.load_json(os.path.join(store.path(j), 'qa.json'), {}) for j in store.finished()[-2:]]
            scales = adapt_scales(scales, recent, cfg['scale_bounds'], os.path.join(st, 'scale_history.json'))
            col.scales = scales
            util.save_json(os.path.join(st, 'current_scales.json'), scales)
            if esc:
                if a.keep_going:
                    util.log(f'ESCALATION for {esc} ignored (--keep_going); collection continues', 'WARN')
                else:
                    p = col.doctor.escalate(esc, qa, store)
                    util.log(f'STOPPING: the self-repair could not fix {esc}. Read {p}', 'ERROR')
                    return 'escalated'
        util.log(f"progress: {store.total_ticks()} / {cfg['target_ticks']} usable ticks")
        util.chown_tree(st)
        # ---- learnability every N segments
        nf = len(store.finished())
        if (nf - last_check >= int(qa_cfg.get('learnability_every_segments', 2))
                and nf >= int(qa_cfg.get('learnability_min_segments', 4)) and not STOP['flag']):
            last_check = nf
            util.log('learnability check on the data collected so far ...')
            try:
                lr = learnability.check(st, cfg, cal['targets'])
                util.log(f"learnability: {lr['verdict']} " + json.dumps(
                    {k: v.get('grade', v.get('note')) for k, v in lr.get('intents', {}).items()}) +
                    f" root={lr.get('root', {}).get('grade', lr.get('root', {}).get('note'))} "
                    f"(see {st}/learncheck/LEARNABILITY_REPORT.md)")
                for adv in lr.get('advice', []):
                    util.log(f'  advice: {adv}')
                fail_checks = fail_checks + 1 if lr['verdict'] == 'FAIL' else 0
            except Exception as exc:
                util.log(f'learnability check failed to run: {exc}', 'WARN')
            util.chown_tree(st)
            if fail_checks >= int(qa_cfg.get('failed_learnability_checks_to_stop', 2)) and \
                    qa_cfg.get('stop_on_bad_data') and not a.keep_going:
                util.log('STOPPING: the learnability check failed twice in a row. Read '
                         f'{st}/learncheck/LEARNABILITY_REPORT.md before collecting more (--keep_going to ignore).',
                         'ERROR')
                return 'not_learnable'
        if STOP['flag']:
            break
        # ---- what happened to the segment decides the restart (always: stop stack -> [RAN] -> start stack)
        if res == 'ran_down' or (res == 'cut' and (m.get('minqa_cut') or {}).get('ran')):
            why = (m.get('ran_down') or {}).get('why') or (m.get('minqa_cut') or {}).get('why')
            if not col.restart('ran', f'RAN watchdog: {why}'):
                return 'error' if not STOP['flag'] else None
            actions = [x for x in actions if x not in ('restart_ran', 'restart_stack', 'remap_restart')]
        elif res in ('unhealthy', 'cut') or (res == 'stopped' and col.stack and col.stack.check_alive()):
            dead = col.stack.check_alive() if col.stack else []
            if not col.restart('stack', f'{res}; dead components {dead}'):
                return 'error' if not STOP['flag'] else None
            actions = [x for x in actions if x not in ('restart_stack', 'remap_restart')]
        if not run_actions(col, cfg, st, store, actions, save):
            return 'error' if not STOP['flag'] else None
        cal = col.cal
    return None


if __name__ == '__main__':
    main()
