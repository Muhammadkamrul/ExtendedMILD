"""ranctl.py -- the RAN side of the testbed, brought up and down by MILD itself (automatic restart).

The RAN is YOUR stack, started with YOUR scripts from the testbed runbook, in YOUR order (Appendices E
and F), always as you (never as root; the scripts call sudo themselves, which needs the NOPASSWD rule):

  up   0. intact-down                        clean slate: UEs, broker, gNB, proxy, jrt-controller, RIC, xApp
       1. intact-core-health                 Open5GS must say CORE READY (else intact-core-restart, once)
       2. intact-run-ric                     FlexRIC nearRT-RIC; wait for "PORT = 36422" in ric.out
       3. intact-run-jrtc                    only if the gNB config has a jbpf section; wait for :3001
       4. intact-run-gnb <config with E2>    wait for "gNB started"; the gNB must connect to the RIC
                                             ("E2AP: Connection to Near-RT-RIC") and the RIC must accept
                                             RAN function 2 (E2SM-KPM); RAN function 3 (E2SM-RC) is noted
       5. intact-run-proxy                   only with jbpf; wait for :30450
       6. intact-multiue up                  N srsUEs + the ZMQ broker; wait for "ALL N UEs ATTACHED."
       7. checks                             every UE namespace has an address and pings 10.45.0.1
  down    intact-down, then the launcher processes MILD started are ended; nothing is left running.

Every launcher runs detached (its own session) with its output in <state>/ran/<name>.log; the testbed
scripts also keep writing ~/intact/logs/run/*.out as usual, and those are what the checks read.
A failed step stops the attempt with the step's reason; restart() repeats down + up with back-off
(ran.restart_backoff_s) until the RAN is up or ran.ran_down_wait_max_h has passed.

If the gNB config with E2SM-RC makes the RIC refuse the E2 setup, up() switches once, automatically, to
the KPM-only config (Appendix F's gnb_zmq_jbpf_e2.yaml) and records the choice in <state>/ran_choice.json;
xRC then runs in 'shadow' actuation mode.

MockRanCtl (mock-RAN test only) starts and stops the stand-in RAN (fake_ran.py) the same way.
"""
from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import sys
import time

from . import util

GERR = re.compile(r'srsRAN ERROR|Failed to connect to AMF|Illegal instruction|terminate called|Segmentation|'
                  r'was not expected|not a valid|INI was not able|could not be parsed')
RIC_REJECT = re.compile(r'Unknown RAN function|thus rejecting|Assertion .* failed|Aborted')


def _pgrep(pattern, full=False):
    r = util.run(['pgrep', '-f' if full else '-x', pattern], timeout=5)
    return [int(x) for x in r.stdout.split()] if r.returncode == 0 else []


class RealRanCtl:
    def __init__(self, cfg, state_dir):
        self.cfg = cfg
        self.ran = cfg['ran']
        self.st = state_dir
        self.dir = os.path.join(state_dir, 'ran')
        os.makedirs(self.dir, exist_ok=True)
        uid, gid, self.user, self.home = util.invoking_user()
        ih = str(self.ran.get('intact_home', '~/intact'))
        self.intact = (self.home + ih[1:]) if ih.startswith('~') else ih
        self.env = self._intact_env()
        self.cfgdir = self.env.get('CFG') or os.path.join(self.intact, 'configs')
        self.logs = self.env.get('LOGS') or os.path.join(self.intact, 'logs')
        self.run_dir = os.path.join(self.logs, 'run')
        self.mu = self._multiue_env()
        self.procs = {}
        self.choice_path = os.path.join(state_dir, 'ran_choice.json')

    # ------------------------------------------------------------------ helpers
    def _bash(self, script, timeout=60):
        """Run a short bash snippet as the user, with env.sh sourced. Returns (rc, output)."""
        cmd = f'source {shlex.quote(self.intact)}/env.sh >/dev/null 2>&1; {script}'
        if util.is_root() and self.user != 'root':
            full = ['sudo', '-u', self.user, '-H', 'bash', '-c', cmd]
        else:
            full = ['bash', '-c', cmd]
        try:
            r = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
            return r.returncode, (r.stdout + r.stderr)
        except subprocess.TimeoutExpired as e:
            return 124, f'timeout after {timeout} s: {(e.stdout or "")[-300:]}'

    def _intact_env(self):
        rc, out = self._bash('echo "CFG=$CFG"; echo "LOGS=$LOGS"; echo "SRSRAN_DIR=$SRSRAN_DIR"', timeout=30)
        env = {}
        for line in out.splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                if k in ('CFG', 'LOGS', 'SRSRAN_DIR') and v:
                    env[k] = v.strip()
        return env

    def _multiue_env(self):
        p = os.path.join(self.cfgdir if hasattr(self, 'cfgdir') else '', 'multiue', 'multiue.env')
        d = {}
        try:
            for line in open(p):
                if '=' in line:
                    k, v = line.strip().split('=', 1)
                    d[k] = v.strip('"')
        except OSError:
            pass
        return d

    def n_ues(self):
        n = self.ran.get('n_ues', 'auto')
        if n == 'auto':
            return int(self.mu.get('MU_N', 1))
        return int(n)

    def ue_slices(self):
        """{'ue1': 1, 'ue2': 2, ...} from multiue.env MU_SD (or ran.ue_slice_sd)."""
        v = self.ran.get('ue_slice_sd', 'auto')
        if v == 'auto':
            sds = [int(x) for x in (self.mu.get('MU_SD') or '').split()] or [1] * self.n_ues()
        else:
            sds = [int(x) for x in v]
        return {f'ue{k + 1}': sds[k] if k < len(sds) else sds[-1] for k in range(self.n_ues())}

    def netns(self):
        return [f'ue{k}' for k in range(1, self.n_ues() + 1)]

    def gnb_cfg(self):
        ch = util.load_json(self.choice_path, {}) or {}
        if ch.get('gnb_cfg') and os.path.exists(ch['gnb_cfg']):
            return ch['gnb_cfg']
        if self.ran.get('gnb_cfg', 'auto') != 'auto':
            g = str(self.ran['gnb_cfg'])
            return (self.home + g[1:]) if g.startswith('~') else g       # ~ = the invoking user's home, not root's
        for c in self.ran.get('gnb_cfg_candidates', []):
            p = os.path.join(self.intact, c)
            if os.path.exists(p):
                return p
        return os.path.join(self.intact, 'configs', 'gnb_zmq_jbpf_e2.yaml')

    def _has_jbpf(self, cfgpath):
        if self.ran.get('jbpf', 'auto') != 'auto':
            return bool(self.ran['jbpf'])
        try:
            import yaml
            return 'jbpf' in (yaml.safe_load(open(cfgpath)) or {})
        except Exception:
            try:
                return any(l.startswith('jbpf:') for l in open(cfgpath))
            except OSError:
                return False

    def _read(self, name):
        try:
            with open(os.path.join(self.run_dir, name), errors='ignore') as f:
                return f.read()
        except OSError:
            return ''

    def _fresh(self, *outs):
        """Move the previous run's ~/intact/logs/run/<x>.out aside BEFORE a launcher starts, so that a ready line
        left over from the previous run can never be mistaken for the new one."""
        for o in outs:
            p = os.path.join(self.run_dir, o)
            if os.path.exists(p):
                try:
                    os.replace(p, p + '.prev')
                except OSError:
                    try:
                        open(p, 'w').close()
                    except OSError:
                        pass

    def _launch(self, name, script, outs=()):
        """Start one testbed launcher detached (own session) as the user; output in <state>/ran/<name>.log."""
        self._fresh(*outs)
        lf = open(os.path.join(self.dir, f'{name}.log'), 'w')
        lf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {script} =====\n")
        lf.flush()
        cmd = f'source {shlex.quote(self.intact)}/env.sh >/dev/null 2>&1; exec {script}'
        full = ['sudo', '-u', self.user, '-H', 'bash', '-c', cmd] if (util.is_root() and self.user != 'root') \
            else ['bash', '-c', cmd]
        p = subprocess.Popen(full, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True)
        self.procs[name] = p
        util.save_json(os.path.join(self.dir, 'launchers.json'), {k: v.pid for k, v in self.procs.items()})
        return p

    def _wait(self, fn, timeout, step=1.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            r = fn()
            if r:
                return r, time.time() - t0
            time.sleep(step)
        return None, time.time() - t0

    @staticmethod
    def _listening(port, sctp=False):
        r = util.run(['ss', '-Hl' + ('n --sctp' if sctp else 'tn'), f'sport = :{port}'] if not sctp else
                     ['ss', '-Hln', '--sctp', f'sport = :{port}'], timeout=5)
        return r.returncode == 0 and bool(r.stdout.strip())

    def ue_ip(self, ns):
        from .netem import _ip
        r = util.run([_ip(), 'netns', 'exec', ns, _ip(), '-4', '-br', 'addr', 'show', self.cfg['network']['ul_dev']],
                     timeout=5)
        parts = r.stdout.split()
        return parts[2] if r.returncode == 0 and len(parts) >= 3 else None

    def ping(self, ns, n=3):
        from .netem import _ip
        p = util.run([_ip(), 'netns', 'exec', ns, 'ping', '-c', str(n), '-i', '0.3', '-W', '2', self.ran['upf_ip']],
                     timeout=15)
        if p.returncode != 0:                       # default route missing after a UE start: add it, once
            util.run([_ip(), 'netns', 'exec', ns, _ip(), 'route', 'replace', 'default', 'dev',
                      self.cfg['network']['ul_dev']], timeout=5)
            p = util.run([_ip(), 'netns', 'exec', ns, 'ping', '-c', str(n), '-i', '0.3', '-W', '2', self.ran['upf_ip']],
                         timeout=15)
        return p.returncode == 0

    # ------------------------------------------------------------------ status
    def status(self, quick=False):
        s = {'gnb': bool(_pgrep('gnb')), 'srsue': len(_pgrep('srsue')), 'ric': bool(_pgrep('nearRT-RIC')),
             'broker': bool(_pgrep('[i]ntact-zmq-broker --ues', full=True)),
             'jrtc': bool(_pgrep('jrtc') or _pgrep('jrtc_main')), 'n_ues_cfg': self.n_ues()}
        if not quick:
            s['ue_ip'] = {ns: self.ue_ip(ns) for ns in self.netns()}
        sp = self.radio_speed()
        if sp is not None:
            s['radio_speed'] = sp
        return s

    def healthy(self):
        s = self.status()
        n = self.n_ues()
        ok = s['gnb'] and s['ric'] and s['srsue'] >= n and (s['broker'] or n == 1) and \
            all(s['ue_ip'].get(ns) for ns in self.netns())
        return bool(ok), s

    def radio_speed(self):
        """Simulated radio time / wall time, from the broker's last status line (None if unknown)."""
        txt = self._read('zmq-broker.out')[-2000:]
        m = re.findall(r'speed\s+([0-9.]+)x real time', txt)
        return float(m[-1]) if m else None

    def rc_offered(self):
        return 'Accepting RAN function ID 3' in self._read('ric.out')

    # ------------------------------------------------------------------ down / up
    def down(self):
        """Clean slate. The MILD stack must already be stopped (the pipeline always stops it first); any E2 front
        end still alive is ended here so that no xApp session outlives the RIC."""
        util.run(['pkill', '-TERM', '-x', 'mild_e2gw'], timeout=5)
        rc, out = self._bash('intact-down', timeout=90)
        for name, p in list(self.procs.items()):
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except Exception:
                    pass
        time.sleep(1.0)
        for name, p in list(self.procs.items()):
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except Exception:
                    pass
        self.procs = {}
        old = util.load_json(os.path.join(self.dir, 'launchers.json'), {}) or {}
        for pid in old.values():                     # launchers of an earlier collector process
            try:
                os.killpg(int(pid), signal.SIGKILL)
            except Exception:
                pass
        util.save_json(os.path.join(self.dir, 'launchers.json'), {})
        pats = (('gnb', 'gnb', False), ('srsue', 'srsue', False), ('RIC', 'nearRT-RIC', False),
                ('broker', '[i]ntact-zmq-broker --ues', True), ('xapp_kpm_csv', 'xapp_kpm_csv', False),
                ('mild_e2gw', 'mild_e2gw', False))
        left = [n for n, pat, full in pats if _pgrep(pat, full)]
        if left:
            util.log(f'RAN down: still running after intact-down: {left}; killing them', 'WARN')
            self._bash('sudo pkill -KILL -x srsue; sudo pkill -KILL -x gnb; pkill -KILL -x nearRT-RIC; '
                       "pkill -KILL -x xapp_kpm_csv; pkill -KILL -x mild_e2gw; pkill -KILL -f '[i]ntact-zmq-broker --ues'",
                       timeout=30)
        t0 = time.time()                             # wait until every process is really gone (ports released)
        while time.time() - t0 < 15 and [n for n, pat, full in pats if _pgrep(pat, full)]:
            time.sleep(0.5)
        left = [n for n, pat, full in pats if _pgrep(pat, full)]
        while time.time() - t0 < 15 and (self._listening(36421, sctp=True) or self._listening(36422, sctp=True)):
            time.sleep(0.5)
        return rc == 0 and not left, (out.strip().splitlines()[-1:] if out else []) + ([f'still running: {left}'] if left else [])

    def up(self):
        """One attempt. Returns (ok, reason, info)."""
        n = self.n_ues()
        info = {'n_ues': n, 'steps': {}}
        cfgpath = self.gnb_cfg()
        info['gnb_cfg'] = cfgpath
        if not os.path.exists(cfgpath):
            return False, f'gNB config {cfgpath} not found (testbed runbook F.3, or mild-oran ran-config)', info
        util.log(f'RAN up 0/7: intact-down (clean slate); gNB config {os.path.basename(cfgpath)}, {n} UE(s)')
        self.down()
        # 1. core
        util.log('RAN up 1/7: Open5GS core health (intact-core-health)')
        rc, out = self._bash('intact-core-health 2>&1 | tail -3', timeout=60)
        if 'CORE READY' not in out:
            util.log('RAN up: core not ready, running intact-core-restart once', 'WARN')
            self._bash('intact-core-restart 2>&1 | tail -3', timeout=180)
            rc, out = self._bash('intact-core-health 2>&1 | tail -3', timeout=60)
            if 'CORE READY' not in out:
                return False, f'Open5GS core not ready: {out.strip()[-200:]}', info
        info['steps']['core'] = 'ok'
        # 2. RIC
        util.log('RAN up 2/7: FlexRIC nearRT-RIC (intact-run-ric), waiting for "PORT = 36422"')
        self._launch('ric', 'intact-run-ric', outs=('ric.out',))
        r, dt = self._wait(lambda: 'PORT = 36422' in self._read('ric.out'),
                           float(self.ran.get('start_timeout_ric_s', 30)))
        if not r:
            return False, 'nearRT-RIC did not become ready (no "PORT = 36422" in ric.out)', info
        info['steps']['ric_s'] = round(dt, 1)
        jbpf = self._has_jbpf(cfgpath)
        info['jbpf'] = jbpf
        # 3. jrt-controller (only when the gNB config uses jbpf)
        if jbpf:
            util.log('RAN up 3/7: jrt-controller (intact-run-jrtc), waiting for :3001')
            self._launch('jrtc', 'intact-run-jrtc', outs=('jrtc.out',))
            r, dt = self._wait(lambda: self._listening(3001), 40)
            if not r:
                return False, 'jrt-controller did not listen on :3001 (needed by the jbpf gNB config)', info
            info['steps']['jrtc_s'] = round(dt, 1)
        # 4. gNB with E2
        util.log(f'RAN up 4/7: gNB (intact-run-gnb {os.path.basename(cfgpath)}), waiting for "gNB started" and the E2 setup')
        self._launch('gnb', f'intact-run-gnb {shlex.quote(cfgpath)}', outs=('gnb.out',))

        def gnb_ready():
            g = self._read('gnb.out')
            if GERR.search(g):
                return 'error'
            if not _pgrep('gnb') and 'gNB started' not in g and time.time() - t_gnb > 10:
                return 'exited'
            if 'gNB started' in g:
                return 'ok'
            return None
        t_gnb = time.time()
        r, dt = self._wait(gnb_ready, float(self.ran.get('start_timeout_gnb_s', 120)))
        if r != 'ok':
            tail = [l for l in self._read('gnb.out').splitlines() if GERR.search(l)][-2:]
            return False, f'gNB did not start ({r or "timeout"}): {tail}', info
        info['steps']['gnb_s'] = round(dt, 1)

        def e2_ok():
            ric = self._read('ric.out')
            if RIC_REJECT.search(ric) or not _pgrep('nearRT-RIC'):
                return 'rejected'
            if 'Accepting RAN function ID 2' in ric and 'E2AP: Connection to Near-RT-RIC' in self._read('gnb.out'):
                return 'ok'
            return None
        r, dt = self._wait(e2_ok, 20)
        if r != 'ok':
            ric_tail = self._read('ric.out').strip().splitlines()[-3:]
            if 'e2rc' in os.path.basename(cfgpath) or self._rc_enabled(cfgpath):
                fb = self._fallback_cfg(cfgpath)
                if fb:
                    util.log(f'RAN up: the RIC refused the E2 setup of {os.path.basename(cfgpath)} ({ric_tail}); '
                             f'switching to the KPM-only config {os.path.basename(fb)} (xRC actuation: shadow)', 'WARN')
                    util.save_json(self.choice_path, {'gnb_cfg': fb, 'rc': False, 'why': f'RIC refused {cfgpath}',
                                                      'time': time.strftime('%Y-%m-%d %H:%M:%S')})
                    return False, 'RIC refused the E2 setup with E2SM-RC; retrying with the KPM-only config', info
            return False, f'E2 setup not completed ({r or "timeout"}): RIC {ric_tail}', info
        info['steps']['e2_s'] = round(dt, 1)
        info['rc_offered'] = self.rc_offered()
        util.log(f"RAN up: E2 setup done in {dt:.0f} s: the RIC accepted E2SM-KPM (RAN function 2); "
                 f"E2SM-RC (function 3) {'accepted' if info['rc_offered'] else 'not offered'}")
        # 5. reverse proxy (jbpf only)
        if jbpf:
            util.log('RAN up 5/7: jbpf reverse proxy (intact-run-proxy)')
            self._launch('proxy', 'intact-run-proxy', outs=('proxy.out',))
            r, dt = self._wait(lambda: self._listening(30450), 20)
            if not r:
                util.log('RAN up: jbpf reverse proxy did not listen on :30450 (MILD does not need it; continuing)',
                         'WARN')
        # 6. UEs + broker
        util.log(f'RAN up 6/7: {n} UEs and the ZMQ broker (intact-multiue up), waiting for "ALL {n} UEs ATTACHED"')
        self._launch('multiue', 'intact-multiue up', outs=('zmq-broker.out',))
        mu_log = os.path.join(self.dir, 'multiue.log')

        def ues_ready():
            try:
                txt = open(mu_log, errors='ignore').read()[-6000:]
            except OSError:
                return None
            if f'ALL {n} UEs ATTACHED' in txt:
                return 'ok'
            if re.search(r'of \d+ UEs attached|\[FAIL\]', txt):
                return 'fail'
            if self.procs['multiue'].poll() is not None:
                return 'exited'
            return None
        r, dt = self._wait(ues_ready, float(self.ran.get('start_timeout_ues_s', 240)), step=2.0)
        if r != 'ok':
            tail = open(mu_log, errors='ignore').read().strip().splitlines()[-4:] if os.path.exists(mu_log) else []
            return False, f'UEs did not all attach ({r or "timeout"}): {tail}', info
        info['steps']['ues_s'] = round(dt, 1)
        # 7. checks
        util.log('RAN up 7/7: every UE namespace has an address and pings 10.45.0.1')
        bad = [ns for ns in self.netns() if not self.ue_ip(ns) or not self.ping(ns)]
        if bad:
            return False, f'UE namespaces without a working user plane: {bad}', info
        info['radio_speed'] = self.radio_speed()
        util.save_json(os.path.join(self.dir, 'last_up.json'), dict(info, time=time.strftime('%Y-%m-%d %H:%M:%S')))
        return True, 'RAN up', info

    def _rc_enabled(self, cfgpath):
        try:
            import yaml
            return bool(((yaml.safe_load(open(cfgpath)) or {}).get('e2') or {}).get('e2sm_rc_enabled'))
        except Exception:
            return False

    def _fallback_cfg(self, cfgpath):
        for c in self.ran.get('gnb_cfg_candidates', []):
            p = os.path.join(self.intact, c)
            if os.path.exists(p) and os.path.abspath(p) != os.path.abspath(cfgpath) and not self._rc_enabled(p):
                return p
        return None

    def disable_rc(self, why):
        """From the next RAN start on, use the KPM-only gNB config (xRC then runs in shadow mode). Returns the config
        path, or None when there is no KPM-only config to fall back to."""
        cur = self.gnb_cfg()
        fb = self._fallback_cfg(cur) if self._rc_enabled(cur) or 'e2rc' in os.path.basename(cur) else None
        if not fb:
            return None
        util.save_json(self.choice_path, {'gnb_cfg': fb, 'rc': False, 'why': why,
                                          'time': time.strftime('%Y-%m-%d %H:%M:%S')})
        return fb

    def restart(self, reason, should_stop=lambda: False, t_limit=None):
        """down + up with back-off until up, the user stops, or ran_down_wait_max_h passes."""
        backs = list(self.ran.get('restart_backoff_s', [20, 60, 120, 300, 600, 900]))
        t0 = time.time()
        limit = t_limit or float(self.ran.get('ran_down_wait_max_h', 12)) * 3600
        k = 0
        while not should_stop():
            k += 1
            util.log(f'RAN restart attempt {k} ({reason})')
            ok, why, info = self.up()
            if ok:
                util.log(f"RAN up after attempt {k}: {info.get('n_ues')} UEs, steps {info.get('steps')}, "
                         f"radio speed {info.get('radio_speed')}, E2SM-RC offered {info.get('rc_offered')}")
                return True, info
            util.log(f'RAN restart attempt {k} failed: {why}', 'WARN')
            if 'retrying with the KPM-only config' in why:
                continue
            if time.time() - t0 > limit:
                util.log('RAN still down after ran_down_wait_max_h: giving up', 'ERROR')
                return False, {'why': why}
            wait = backs[min(k - 1, len(backs) - 1)]
            util.log(f'next RAN restart attempt in {wait} s (Ctrl+C stops the collection)')
            t1 = time.time()
            while time.time() - t1 < wait and not should_stop():
                time.sleep(1.0)
        return False, {'why': 'stopped'}

    def ensure_up(self, should_stop=lambda: False):
        ok, s = self.healthy()
        if ok:
            util.log(f"RAN already running and healthy: {s.get('srsue')} srsUE, UE addresses {s.get('ue_ip')}")
            return True, {'reused': True, 'status': s}
        return self.restart('RAN not running or not healthy at start', should_stop)


class MockRanCtl:
    """Stand-in RAN (fake_ran.py) for the mock test: same interface as RealRanCtl."""

    def __init__(self, cfg, state_dir):
        self.cfg = cfg
        self.ran = cfg['ran']
        self.st = state_dir
        self.dir = os.path.join(state_dir, 'ran')
        os.makedirs(self.dir, exist_ok=True)
        self.rt = os.path.join(state_dir, 'ranrt')
        os.makedirs(self.rt, exist_ok=True)
        self.p = None
        self.state_file = os.path.join(self.rt, 'fake_ran.json')
        self.rc_file = os.path.join(self.rt, 'fake_ran_rc.json')
        self.choice_path = os.path.join(state_dir, 'ran_choice.json')
        self.ups = 0

    def n_ues(self):
        n = self.ran.get('n_ues', 'auto')
        return 3 if n == 'auto' else int(n)

    def ue_slices(self):
        v = self.ran.get('ue_slice_sd', 'auto')
        sds = [1, 2, 1, 2, 1] if v == 'auto' else [int(x) for x in v]
        return {f'ue{k + 1}': sds[k] for k in range(self.n_ues())}

    def netns(self):
        return [f'ue{k}' for k in range(1, self.n_ues() + 1)]

    def gnb_cfg(self):
        return 'mock'

    def status(self, quick=False):
        s = util.load_json(self.state_file, {}) or {}
        alive = self.p is not None and self.p.poll() is None and util.mono() - float(s.get('t_mono', 0)) < 5
        return {'gnb': alive, 'srsue': self.n_ues() if alive else 0, 'ric': alive, 'broker': alive,
                'n_ues_cfg': self.n_ues(), 'ue_ip': {ns: '127.0.0.1' if alive else None for ns in self.netns()},
                'radio_speed': s.get('speed')}

    def healthy(self):
        s = self.status()
        return bool(s['gnb']), s

    def radio_speed(self):
        return (util.load_json(self.state_file, {}) or {}).get('speed')

    def rc_offered(self):
        return (util.load_json(self.choice_path, {}) or {}).get('rc', True) is not False

    def disable_rc(self, why):
        util.save_json(self.choice_path, {'gnb_cfg': 'mock-kpm-only', 'rc': False, 'why': why,
                                          'time': time.strftime('%Y-%m-%d %H:%M:%S')})
        return 'mock-kpm-only'

    def down(self):
        if self.p is not None and self.p.poll() is None:
            try:
                os.killpg(self.p.pid, signal.SIGTERM)
            except Exception:
                pass
            try:
                self.p.wait(5)
            except Exception:
                pass
        self.p = None
        try:
            os.remove(self.state_file)
        except OSError:
            pass
        return True, []

    def up(self):
        self.down()
        P = self.cfg['ports']
        mock = self.cfg.get('mock', {})
        self.ups += 1
        outage = float(mock.get('outage_after_s', 0) or 0) if self.ups <= int(mock.get('outages', 1)) else 0.0
        # E2SM-RC behaviour of the stand-in gNB for this start: offered or not, and whether controls go unanswered
        # (mock.rc_hang_ups = how many RAN starts answer no control, to test the RC check's restart and fallback)
        util.save_json(os.path.join(self.rt, 'fake_rc_mode.json'),
                       {'offered': self.rc_offered(), 'hang': self.ups <= int(mock.get('rc_hang_ups', 0) or 0)})
        lf = open(os.path.join(self.dir, 'fake_ran.log'), 'a')
        cmd = [sys.executable, '-u', '-m', 'mild_oran.ran.fake_ran', '--state', self.state_file, '--rc', self.rc_file,
               '--air_port', str(P['mock_air']), '--n_ues', str(self.n_ues()),
               '--capacity_mbps', str(self.ran.get('capacity_nominal_mbps', 20.0)), '--outage_after_s', str(outage)]
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=root, start_new_session=True,
                                  env=dict(os.environ, PYTHONPATH=root))
        t0 = time.time()
        while time.time() - t0 < 15:
            if (util.load_json(self.state_file, {}) or {}).get('t_mono'):
                return True, 'mock RAN up', {'n_ues': self.n_ues(), 'steps': {'fake_ran_s': round(time.time() - t0, 1)},
                                             'rc_offered': self.rc_offered(), 'radio_speed': 0.9}
            time.sleep(0.3)
        return False, 'fake RAN did not start', {}

    def restart(self, reason, should_stop=lambda: False, t_limit=None):
        for k in range(1, 4):
            if should_stop():
                break
            util.log(f'RAN restart attempt {k} ({reason})')
            ok, why, info = self.up()
            if ok:
                util.log(f"RAN up after attempt {k}: {info}")
                return True, info
            time.sleep(2)
        return False, {'why': 'mock RAN failed'}

    def ensure_up(self, should_stop=lambda: False):
        ok, s = self.healthy()
        if ok:
            return True, {'reused': True}
        return self.restart('mock RAN not running', should_stop)


def make_ranctl(cfg, state_dir):
    if cfg.get('selftest'):
        from .fakestack import FakeRanCtl
        return FakeRanCtl(cfg, state_dir)
    return MockRanCtl(cfg, state_dir) if cfg.get('mock_ran') else RealRanCtl(cfg, state_dir)


def main():
    """mild-oran ran up|down|status|restart: the same automation, by hand."""
    import argparse
    import json
    ap = argparse.ArgumentParser(description='MILD RAN control (runs your testbed scripts in the right order)')
    ap.add_argument('action', choices=['up', 'down', 'status', 'restart'])
    ap.add_argument('--state', default=os.path.expanduser('~/intact/mild/state'))
    a = ap.parse_args()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = json.load(open(os.path.join(root, 'config', 'oran.json')))
    ov = util.load_json(os.path.join(os.path.expanduser(a.state), 'overrides.json'), {}) or {}
    for k, v in ov.items():
        d = cfg
        parts = k.split('.')
        for p in parts[:-1]:
            d = d.setdefault(p, {})
        d[parts[-1]] = v
    os.makedirs(a.state, exist_ok=True)
    util.set_log(os.path.join(a.state, 'ran', 'ranctl_manual.log'))
    r = RealRanCtl(cfg, os.path.expanduser(a.state))
    rc = 0
    try:
        if a.action == 'status':
            print(json.dumps(r.status(), indent=1))
            print('gNB config:', r.gnb_cfg(), ' UEs:', r.n_ues(), ' slices:', r.ue_slices())
        elif a.action == 'down':
            ok, msg = r.down()
            print('RAN down' if ok else 'RAN down, but:', msg)
        elif a.action == 'up':
            ok, why, info = r.up()
            if not ok and 'retrying with the KPM-only config' in why:
                ok, why, info = r.up()
            print('OK' if ok else 'FAILED', why, json.dumps(info))
            rc = 0 if ok else 1
        else:
            ok, info = r.restart('manual restart')
            rc = 0 if ok else 1
    finally:
        util.chown_tree(a.state)
    sys.exit(rc)


if __name__ == '__main__':
    main()
