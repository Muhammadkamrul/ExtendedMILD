"""preflight.py -- automated checks before a MILD collection on the O-RAN testbed (v2: MILD runs the RAN itself).

`mild-oran preflight` prints one PASS / WARN / FAIL / INFO line per item and exits 0 only when there is no
FAIL. The same function runs automatically at the start of `mild-oran collect`.
`mild-oran preflight --ran` also brings the RAN up the way the collector will (ranctl.up) and checks E2 and
the UEs; the RAN is left running.

What is checked (live RAN):
  sudo        the collector runs as root; the RAN scripts run as you and need sudo WITHOUT a password
              (/etc/sudoers.d/99-nopasswd), because nobody is there to type it at 3 a.m.
  testbed     ~/intact/env.sh and the scripts intact-down, intact-core-health, intact-core-restart,
              intact-run-ric, intact-run-gnb, intact-multiue (+ intact-run-jrtc / intact-run-proxy for jbpf)
  FlexRIC     paths.env, nearRT-RIC binary, service models (libkpm_sm.so, librc_sm.so), flexric.conf
  E2 front    mild_e2gw built (mild-oran build-xapps) and the E2SM-RC encoder self-test passes
  UEs         multiue.env (MU_N, MU_SD), one netns per UE after the RAN is up
  gNB         the E2 config to use (E2SM-KPM on; E2SM-RC on = real actuation, off = xRC in shadow mode)
  network     tc netem on ogstun (and on tun_srsue in every UE namespace when the RAN is up)
  others      xapp_kpm_csv / iperf3 / jbpf slice apps that would disturb the experiment; ports; CPU, RAM, disk
"""
from __future__ import annotations

import os
import shutil
import time

from . import util
from .netem import Netem, _ip


class Report:
    def __init__(self):
        self.lines, self.fails, self.warns = [], 0, 0

    def ok(self, m):
        self._add('PASS', m)

    def warn(self, m):
        self.warns += 1
        self._add('WARN', m)

    def fail(self, m):
        self.fails += 1
        self._add('FAIL', m)

    def info(self, m):
        self._add('INFO', m)

    def _add(self, tag, m):
        line = f'  [{tag}] {m}'
        self.lines.append(line)
        print(line, flush=True)


def _pgrep(name):
    r = util.run(['pgrep', '-x', name], timeout=5)
    return [int(x) for x in r.stdout.split()] if r.returncode == 0 else []


def _yaml(path):
    try:
        import yaml
        return yaml.safe_load(open(path)) or {}
    except Exception:
        return {}


def run(cfg, state_dir, mock=False, ran=False):
    R = Report()
    P = cfg['ports']
    print('== MILD-ORAN preflight (v2: FlexRIC nearRT-RIC, real E2, N UEs)', flush=True)
    uid, gid, user, home = util.invoking_user()
    # ---------------- privileges and Python
    if mock:
        R.info('mock RAN: RAN, FlexRIC, netns and tc checks are skipped')
    elif not util.is_root():
        R.fail('not running as root: start with `mild-oran preflight` / `mild-oran collect` (they use sudo)')
    elif not os.environ.get('SUDO_USER'):
        R.warn('running as root without SUDO_USER: components and RAN scripts would run as root')
    else:
        R.ok(f'root via sudo; MILD components and the RAN scripts will run as user {user}')
    try:
        import numpy, pandas, psutil, scipy, sklearn, yaml  # noqa: F401
        R.ok('Python packages numpy, pandas, psutil, scipy, scikit-learn, pyyaml importable')
    except Exception as e:
        R.fail(f'Python package missing ({e}): run the installer again (runbook M2)')
    # ---------------- resources
    n = os.cpu_count() or 1
    (R.ok if n >= 8 else R.warn)(f'{n} CPUs visible in WSL (>= 8 recommended: gNB + {cfg["ran"].get("n_ues", "N")} '
                                 'srsUEs + broker + RIC + xApps)')
    try:
        mem = int([l for l in open('/proc/meminfo') if l.startswith('MemAvailable')][0].split()[1]) / 1024 / 1024
        (R.ok if mem >= 4 else R.warn)(f'{mem:.1f} GiB memory available (>= 4 GiB recommended with 3 UEs)' +
                                       ('' if mem >= 4 else ': close other programs, or give WSL more memory '
                                        '(memory=12GB in %UserProfile%\\.wslconfig, then wsl --shutdown; runbook M12)'))
    except Exception:
        pass
    os.makedirs(state_dir, exist_ok=True)
    free = shutil.disk_usage(state_dir).free / 1e9
    (R.ok if free >= 10 else (R.warn if free >= 3 else R.fail))(f'{free:.0f} GB free disk for {state_dir} '
                                                                 '(>= 10 GB recommended; a 40-h dataset needs ~1-2 GB)')
    if not mock:
        from .ranctl import RealRanCtl
        from .stack import e2gw_path, read_paths_env
        rc = RealRanCtl(cfg, state_dir)
        # ---------------- password-less sudo for the RAN scripts
        r = util.run(['sudo', '-u', user, '-H', 'sudo', '-n', 'true'], timeout=10)
        if r.returncode == 0:
            R.ok(f'user {user} can run sudo without a password (needed for automatic RAN restarts)')
        else:
            R.fail(f'user {user} needs sudo WITHOUT a password for automatic RAN restarts: '
                   f'echo "{user} ALL=(ALL:ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/99-nopasswd; '
                   'sudo chmod 440 /etc/sudoers.d/99-nopasswd; sudo visudo -c')
        # ---------------- testbed scripts
        envsh = os.path.join(rc.intact, 'env.sh')
        (R.ok if os.path.exists(envsh) else R.fail)(f'{envsh} ' + ('found' if os.path.exists(envsh) else
                                                                   'MISSING (testbed runbook Part 2)'))
        cfgpath = rc.gnb_cfg()
        jbpf = rc._has_jbpf(cfgpath) if os.path.exists(cfgpath) else False
        need = ['intact-down', 'intact-core-health', 'intact-core-restart', 'intact-run-ric', 'intact-run-gnb',
                'intact-multiue'] + (['intact-run-jrtc', 'intact-run-proxy'] if jbpf else [])
        code, out = rc._bash('for s in ' + ' '.join(need) + '; do command -v $s >/dev/null || echo "MISSING $s"; done',
                             timeout=20)
        miss = [l.split()[1] for l in out.splitlines() if l.startswith('MISSING')]
        if miss:
            R.fail(f'testbed scripts not found on PATH: {miss} (testbed runbook Parts 8, E and F)')
        else:
            R.ok(f'testbed scripts present: {", ".join(need)}')
        code, out = rc._bash('grep -c "nearRT-RIC" "$(command -v intact-down)"', timeout=10)
        if out.strip().isdigit() and int(out.strip()) > 0:
            R.ok('intact-down also stops the nearRT-RIC (Appendix E/F version)')
        else:
            R.fail('intact-down does not stop the nearRT-RIC: install the Appendix E version of intact-down '
                   '(testbed runbook E.2 block 1)')
        # ---------------- FlexRIC
        pe = read_paths_env(cfg)
        if not pe.get('FR_RIC'):
            R.fail(f"{pe['_file']} missing or incomplete: run intact-flexric-build (testbed runbook F.3)")
        else:
            (R.ok if os.access(pe['FR_RIC'], os.X_OK) else R.fail)(f"nearRT-RIC binary {pe['FR_RIC']}")
            lib = pe.get('FR_LIBDIR', '')
            have = [x for x in ('libkpm_sm.so', 'librc_sm.so') if os.path.exists(os.path.join(lib, x))]
            (R.ok if len(have) == 2 else R.fail)(f'service models in {lib}: {have} (need libkpm_sm.so and librc_sm.so)')
            (R.ok if os.path.exists(pe.get('FR_CONF', '')) else R.fail)(f"FlexRIC config {pe.get('FR_CONF')}")
            R.info(f"FlexRIC built for E2AP v{pe.get('FR_E2AP', '?')} (the E2 front end is built against the same tree)")
        gw = e2gw_path(cfg)
        if os.access(gw, os.X_OK):
            R.ok(f'MILD E2 front end built: {gw}')
            t = os.path.join(os.path.dirname(gw), 'mild_rc_enc_test')
            if os.access(t, os.X_OK):
                r = util.run([t], timeout=20)
                lay = 'srsRAN layout' in (r.stdout or '') and 'YES' in (r.stdout or '')
                if r.returncode == 0 and not lay:
                    R.fail('the MILD E2 front end was built from MILD v2.0 (E2SM-RC parameter layout srsRAN does not '
                           'answer): run  mild-oran build-xapps  again with v2.1 (runbook M2b)')
                else:
                    (R.ok if r.returncode == 0 else R.fail)(
                        'E2SM-RC slice-PRB-quota encoder self-test: ' +
                        ('passes (srsRAN layout, IDs 1-3,5-13, elements KeyFlagFalse)' if r.returncode == 0 else
                         f'FAILED ({(r.stdout + r.stderr)[-200:]})'))
        else:
            R.fail(f'MILD E2 front end not built ({gw}): run  mild-oran build-xapps  (runbook M2b)')
        # ---------------- UEs and gNB config
        nue = rc.n_ues()
        if rc.mu:
            R.ok(f"multi-UE setup: MU_N={rc.mu.get('MU_N')} UEs, slices (SD) {rc.ue_slices()} (multiue.env)")
        else:
            R.warn('multiue.env not found: assuming 1 UE (testbed runbook E.3 sets up N UEs)')
        if not os.path.exists(cfgpath):
            R.fail(f'gNB config {cfgpath} not found (testbed runbook F.4; or mild-oran ran-config)')
        else:
            y = _yaml(cfgpath)
            e2 = y.get('e2') or {}
            kpm = bool(e2.get('enable_du_e2') or e2.get('enable_cu_cp_e2') or e2.get('e2sm_kpm_enabled'))
            (R.ok if kpm else R.fail)(f'gNB config {os.path.basename(cfgpath)}: E2 section ' +
                                      ('present, E2SM-KPM on' if kpm else 'MISSING (needs the e2: section of F.4)'))
            if e2.get('e2sm_rc_enabled'):
                R.ok('E2SM-RC enabled in the gNB config: xRC sends real RIC CONTROL messages (if the RIC refuses '
                     'the E2 setup, the collector switches to the KPM-only config by itself)')
            else:
                R.warn('E2SM-RC is off in the gNB config: xRC runs in shadow mode (decides, logs, sends nothing). '
                       'For real actuation: mild-oran ran-config (creates gnb_zmq_jbpf_e2rc.yaml)')
            R.info(f"jbpf section in the gNB config: {'yes (jrt-controller + proxy are started too)' if jbpf else 'no'}")
        # ---------------- RAN state
        ok_ran, s = rc.healthy()
        if ok_ran:
            R.info(f"RAN is running and healthy ({s.get('srsue')} srsUE, addresses {s.get('ue_ip')}): it will be reused")
        else:
            R.info(f'RAN not (fully) running {s}: the collector starts it (ranctl: intact-down, core, RIC, gNB, UEs)')
        if ran:
            print('== bringing the RAN up (as the collector will) ...', flush=True)
            okr, info = rc.ensure_up()
            (R.ok if okr else R.fail)(f'RAN up: {info}')
            if okr:
                ric_out = rc._read('ric.out')
                R.ok('RIC accepted E2SM-KPM (RAN function 2)') if 'Accepting RAN function ID 2' in ric_out else \
                    R.fail('RIC did not accept RAN function 2 (E2SM-KPM): see ~/intact/logs/run/ric.out')
                if 'Accepting RAN function ID 3' in ric_out:
                    R.ok('RIC accepted E2SM-RC (RAN function 3): real actuation possible')
                else:
                    R.info('no E2SM-RC (RAN function 3) at the RIC: xRC will run in shadow mode')
                ok_ran, s = rc.healthy()
                R.info(f"radio speed {s.get('radio_speed')} x real time; UE addresses {s.get('ue_ip')}")
        # ---------------- tc netem
        ne = Netem(dict(cfg, ran=dict(cfg['ran'], _netns=rc.netns() if ok_ran else [])))
        okn, msg = ne.test()
        if okn:
            R.ok(f'tc netem works ({msg})' + ('' if ok_ran else '; UE namespaces are tested when the RAN is up'))
        elif cfg['network'].get('backend') == 'cell_load':
            R.warn(f'tc netem unavailable ({msg}); network backend is cell_load (documented fallback)')
        else:
            R.fail(f'tc netem unavailable ({msg}). Fix: sudo modprobe sch_netem; if that fails, the WSL kernel lacks '
                   'netem (runbook M12, row "tc netem unavailable"), or run with --set network.backend=cell_load')
        # ---------------- other programs that would disturb the experiment
        if _pgrep('xapp_kpm_csv'):
            R.warn('xapp_kpm_csv (Appendix F logger) is running: the collector stops it with intact-down at RAN start')
        if _pgrep('iperf3'):
            R.warn('iperf3 is running: extra traffic changes the RAN load; it is stopped by intact-down')
        jl = os.path.join(rc.run_dir, 'jrtc.out')
        if os.path.exists(jl) and time.time() - os.path.getmtime(jl) < 300:
            tail = util.run(['tail', '-n', '300', jl], timeout=5).stdout
            if 'SLICE_ALLOC_SET' in tail or 'pact_e2t' in tail:
                R.fail('a jbpf slice-control app (slice_mgmt / Gate A / pact_e2t) was active in the last 5 min: it '
                       'would fight xRC over the PRB quota. Unload it (testbed runbook Appendix C/D) and run again.')
    else:
        try:
            import mild_oran.ran.fake_e2gw  # noqa: F401
            import mild_oran.ran.fake_ran  # noqa: F401
            R.ok('stand-in RAN and E2 front end importable')
        except Exception as e:
            R.fail(f'stand-in RAN not importable: {e}')
    # ---------------- MILD ports
    busy = []
    names = ['xkpm', 'xsla', 'xrc', 'loadgen']
    for pnum in [P[x] for x in names] + [P[x] + P['ctl_offset'] for x in names] + [P['xsla_ingest']]:
        if not util.port_free(pnum):
            busy.append((pnum, util.who_uses(pnum)))
    left = util.our_leftovers()
    if left:
        R.warn(f'{len(left)} MILD processes from an earlier run are still alive; they will be killed at start')
    if busy and not left:
        R.fail('ports in use: ' + '; '.join(f':{p} {w}' for p, w in busy))
    elif not busy:
        R.ok('MILD component ports free (18101-18105, 18202, 19101-19105)')
    print(f'\n== preflight: {R.fails} FAIL, {R.warns} WARN', flush=True)
    return R
