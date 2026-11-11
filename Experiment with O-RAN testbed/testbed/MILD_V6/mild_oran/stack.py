"""stack.py -- start, drive, measure and stop the MILD components on top of the running RAN (v2).

The RAN itself (Open5GS core, FlexRIC nearRT-RIC, gNB with its E2 agent, N srsUEs, ZMQ broker, and
jrt-controller + reverse proxy when the gNB config uses jbpf) is brought up and down by ranctl.py, in the
order of the testbed runbook. This module starts what MILD adds, ALWAYS AFTER the RAN is up, and stops it
ALWAYS BEFORE the RAN is taken down:

  order  role          process                                    runs as             purpose
  -----  ------------  -----------------------------------------  ------------------  -------------------------------
  1      appserver     mild_oran.ran.appserver                    user                edge app 10.45.0.1:18080 (sri probes)
  2      ue_agent_ueK  ip netns exec ueK ... ran.ue_agent         root, in ns ueK     ICMP + HTTP probes over the RAN,
                       (one per UE, K = 1..N)                                         DL traffic sink :5400
  3      traffic       mild_oran.ran.traffic                      user                DL application load of every UE
  4      xsla          mild_oran.xapps.xsla  (+ its mild_e2gw)    user, nice 5        analytics xApp     (Analytics intent)
  5      xrc           mild_oran.xapps.xrc   (+ its mild_e2gw)    user, nice 5        control/policy xApp (API intent)
  6      xkpm          mild_oran.xapps.xkpm  (+ its mild_e2gw)    user, nice 5        KPM monitor xApp   (Telemetry intent)
  7      loadgen       mild_oran.xapps.loadgen                    user, nice 5        tenant/operator policy clients
  then   checks        E2 indications at xKPM with all N UEs; UE identity map (F1AP UE ID -> ueK) by
                       per-UE traffic tests; every UE agent has an address and probe replies.

Every xApp opens its OWN E42 session to the FlexRIC nearRT-RIC (127.0.0.1:36422) through its own mild_e2gw
process (FlexRIC C SDK); E2 indications and E2SM-RC controls are real E2AP messages to/from the gNB DU.
Mock RAN (test without the RAN): fake_e2gw.py replaces mild_e2gw and fake_ran.py replaces the cell.

Interface used by the driver (same for RealStack and FakeStack):
  start(), apply(knobs), reset(), read_kpis(), ran_status(), check_alive(), stop(), manifest(),
  set_net(delay_ms, loss_pct), set_capacity(mbps), set_baseline(dict), set_scales(dict),
  set_fixed_load(mbps, only_ue=None), rotate_e2(), map_ues(), component_stats(), apply_tuning(dict)
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time

from . import util
from .kpi import KpiReader
from .netem import Netem, _ip

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

NEUTRAL = {'api': 1.0, 'analytics': 1.0, 'telemetry': 1.0, 'burst': 1.0, 'cell_load': 1.0,
           'delay_ms': 0.0, 'loss_pct': 0.0, 'telemetry_active': False}
XAPPS = ('xkpm', 'xsla', 'xrc')


def _expand_user(p):
    p = str(p)
    if p.startswith('~'):
        return util.invoking_user()[3] + p[1:]
    return p


def read_paths_env(cfg):
    """FlexRIC paths written by the testbed runbook F.3 (FR_RIC, FR_XAPP, FR_LIBDIR, FR_CONF, FR_E2AP)."""
    p = _expand_user(cfg['ric'].get('flexric_paths', '~/intact/configs/flexric/paths.env'))
    d = {'_file': p}
    try:
        for line in open(p):
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                d[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return d


def e2gw_path(cfg):
    """Where `mild-oran build-xapps` puts the E2 front end (inside the FlexRIC build tree)."""
    gw = cfg['ric'].get('e2gw', 'auto')
    if gw not in (None, '', 'auto'):
        return _expand_user(gw)
    pe = read_paths_env(cfg)
    ric = pe.get('FR_RIC')
    if ric:
        build = os.path.dirname(os.path.dirname(os.path.dirname(ric)))     # <B>/examples/ric/nearRT-RIC -> <B>
        return os.path.join(build, 'examples', 'xApp', 'c', 'mild', 'mild_e2gw')
    return _expand_user('~/intact/src/flexric/build/examples/xApp/c/mild/mild_e2gw')


def e2gw_command(cfg, mock):
    """Command line of the E2 front end of every xApp (E2Link adds -d <its own DB folder>/)."""
    if mock:
        return [sys.executable, '-u', '-m', 'mild_oran.ran.fake_e2gw'], None
    pe = read_paths_env(cfg)
    gw = e2gw_path(cfg)
    if not os.access(gw, os.X_OK):
        return None, f'E2 front end {gw} not built: run  mild-oran build-xapps  (runbook M2b)'
    if not pe.get('FR_CONF') or not pe.get('FR_LIBDIR'):
        return None, f"FlexRIC paths file {pe['_file']} missing FR_CONF/FR_LIBDIR (testbed runbook F.3)"
    lib = pe['FR_LIBDIR'] if pe['FR_LIBDIR'].endswith('/') else pe['FR_LIBDIR'] + '/'
    return [gw, '-c', pe['FR_CONF'], '-p', lib], None


class RealStack:
    def __init__(self, cfg, run_dir, ranctl, mock_ran=False):
        self.cfg = cfg
        self.mock = mock_ran
        self.ranctl = ranctl
        self.run_dir = run_dir
        self.state = cfg['state_dir']
        self.rt = os.path.join(self.state, 'runtime')
        self.logs = os.path.join(run_dir, 'logs')
        os.makedirs(self.logs, exist_ok=True)
        self.ports = dict(cfg['ports'])
        self.procs, self.args = {}, {}
        self.sent, self.knob_failures = {}, {}
        self.weights_path = os.path.join(self.state, 'snet_sri_weights.json')
        if not os.path.exists(self.weights_path):
            util.save_json(self.weights_path, cfg['snet_sri_weights'])
        cal = util.load_json(os.path.join(self.state, 'calibration.json'), {}) or {}
        self.baseline = dict(cal.get('probe_baseline') or {})
        self.capacity = float(cal.get('capacity_mbps') or cfg['ran'].get('capacity_nominal_mbps', 20.0))
        self.ues = ranctl.netns()                         # ['ue1', 'ue2', 'ue3']
        self.n = len(self.ues)
        self.slices = ranctl.ue_slices()                  # {'ue1': 1, 'ue2': 2, 'ue3': 1}
        cfg['ran']['_netns'] = list(self.ues)
        self.netem = Netem(cfg, mock_file=os.path.join(self.rt, 'mock_netem.json') if mock_ran else None)
        self.backend = cfg['network'].get('backend', 'netem')
        self.cell_load_now = None
        self.fixed_mbps, self.only_ue, self.traffic_on = None, None, True
        self.scales = dict(cfg.get('initial_scales', {}))
        self.kpi = None
        self.t0 = None
        self.ue_map = {}
        self.info = {'mode': 'mock RAN (fake cell + fake E2 front end, no netns, no tc)' if mock_ran else
                     'live RAN (srsRAN gNB + E2 agent, FlexRIC nearRT-RIC, N srsUEs, Open5GS)',
                     'network_backend': self.backend, 'n_ues': self.n, 'slices': self.slices}

    # ------------------------------------------------------------------ processes
    def _env(self):
        uid, gid, name, home = util.invoking_user()
        e = dict(os.environ, PYTHONUNBUFFERED='1', PYTHONPATH=ROOT, OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1',
                 HOME=home, USER=name, LOGNAME=name)
        e.pop('PYTHONSTARTUP', None)
        if self.mock:
            e['FAKE_RAN_STATE'] = self.ranctl.state_file
            e['FAKE_RAN_RC'] = self.ranctl.rc_file
        return e

    def _popen(self, role, args, as_root=False, nice=True, prefix=None):
        lf = open(os.path.join(self.logs, f'{role}.log'), 'a')
        lf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} start {role} =====\n")
        lf.flush()
        cmd = list(prefix or []) + (['nice', '-n', str(self.cfg['ric'].get('nice', 5))] if nice else []) + \
            [sys.executable, '-u', '-m'] + args
        kw = {}
        if util.is_root() and not as_root:
            uid, gid, _, _ = util.invoking_user()
            if uid != 0:
                kw.update(user=uid, group=gid, extra_groups=[])
        p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=ROOT,
                             env=self._env(), start_new_session=True, **kw)
        self.procs[role] = p
        self.args[role] = (args, as_root, nice, prefix)
        return p

    def pid(self, role):
        p = self.procs.get(role)
        return p.pid if p and p.poll() is None else None

    def _ctl(self, role):
        return self.ports[role] + self.ports['ctl_offset']

    def _wait_health(self, role, timeout=25):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.procs[role].poll() is not None:
                raise RuntimeError(f'{role} exited at start (code {self.procs[role].returncode}); '
                                   f'see {self.logs}/{role}.log')
            if util.http_get_json('127.0.0.1', self._ctl(role), '/health', timeout=1.0):
                return
            time.sleep(0.3)
        raise RuntimeError(f'{role} did not answer on its control port :{self._ctl(role)} within {timeout} s; '
                           f'see {self.logs}/{role}.log')

    def _check_ports(self):
        P = self.ports
        names = ['xkpm', 'xsla', 'xrc', 'loadgen']
        tcp = [P[n] for n in names] + [P[n] + P['ctl_offset'] for n in names] + [P['xsla_ingest']]
        busy = [(p, util.who_uses(p)) for p in tcp if not util.port_free(p)]
        if busy:
            raise RuntimeError('ports in use: ' + '; '.join(f':{p} by {w or "unknown"}' for p, w in busy) +
                               ' -> stop that program or change "ports" with --set ports.<name>=<port>')

    # ------------------------------------------------------------------ start (RAN already up)
    def start(self):
        cfg, P, ran, ric = self.cfg, self.ports, self.cfg['ran'], self.cfg['ric']
        killed = util.kill_leftovers()
        if killed:
            util.log(f'killed {len(killed)} leftover MILD processes from an earlier run')
        self.netem.clear()
        shutil.rmtree(self.rt, ignore_errors=True)
        os.makedirs(self.rt)
        util.chown_tree(self.rt)
        self._check_ports()
        if not self.mock and not util.is_root():
            raise RuntimeError('the live-RAN stack needs root (ip netns exec, tc). Start it through `mild-oran collect`, '
                               'which uses sudo.')
        gw, problem = e2gw_command(cfg, self.mock)
        if problem:
            raise RuntimeError(problem)
        slice_ues = {}
        for ue, sd in self.slices.items():
            slice_ues.setdefault(str(sd), []).append(ue)
        ric_cfg = dict(ric, _e2gw_cmd=gw, _plmn_hex=ran.get('plmn_hex', '00F110'), _slice_sst=ran.get('slice_sst', 1),
                       _slice_ues=slice_ues, _n_ues=self.n, _ues=self.ues)
        ric_cfg_path = os.path.join(self.rt, 'ric_cfg.json')
        util.save_json(ric_cfg_path, ric_cfg)
        util.chown_tree(self.rt)
        self.t0 = util.mono()
        self.traffic_on = False                                # no load until the UE map is known
        self._write_traffic_ctl(1.0)
        # 1. edge app on the data-network side
        if self.mock:
            self._popen('appserver', ['mild_oran.ran.appserver', '--bind', '127.0.0.1', '--port', str(ran['app_port']),
                                      '--mock_state', self.ranctl.state_file,
                                      '--mock_netem', os.path.join(self.rt, 'mock_netem.json'),
                                      '--udp_echo_port', str(P['mock_udp_echo'])], nice=False)
        else:
            self._popen('appserver', ['mild_oran.ran.appserver', '--bind', ran['upf_ip'], '--port',
                                      str(ran['app_port'])], nice=False)
        # 2. one UE agent per UE namespace
        for k, ue in enumerate(self.ues, 1):
            common = ['mild_oran.ran.ue_agent', '--ue', ue, '--state', os.path.join(self.rt, f'ue_agent_{ue}.json'),
                      '--http_port', str(ran['app_port']), '--ping_interval', str(ran['ping_interval_s']),
                      '--http_interval', str(ran['http_interval_s']), '--http_timeout', str(ran['http_timeout_s'])]
            if self.mock:
                self._popen(f'ue_agent_{ue}', common + ['--target', '127.0.0.1', '--mode', 'udp',
                                                        '--sink_port', str(ran['sink_port'] + k),
                                                        '--udp_echo_port', str(P['mock_udp_echo'])], nice=False)
            else:
                self._popen(f'ue_agent_{ue}', common + ['--target', ran['upf_ip'], '--mode', 'icmp',
                                                        '--sink_port', str(ran['sink_port'])],
                            as_root=True, nice=False, prefix=[_ip(), 'netns', 'exec', ue])
        # 3. DL application traffic of all UEs
        targs = ['mild_oran.ran.traffic', '--ctl', os.path.join(self.rt, 'traffic_ctl.json'),
                 '--agent_states', ','.join(os.path.join(self.rt, f'ue_agent_{ue}.json') for ue in self.ues),
                 '--state', os.path.join(self.rt, 'traffic.json'), '--t0', f'{self.t0:.3f}']
        if self.mock:
            targs += ['--port', str(P['mock_air']),
                      '--targets', ','.join(f"127.0.0.1:{P['mock_air'] + k}" for k in range(self.n))]
        else:
            targs += ['--port', str(ran['sink_port'])]
        self._popen('traffic', targs, nice=False)
        # 4-6. the three xApps, each with its own E2 front end (E42 session to the nearRT-RIC)
        common = ['--cfg', ric_cfg_path, '--runtime', self.rt]
        self._popen('xsla', ['mild_oran.xapps.xsla', '--port', str(P['xsla']), '--ingest_port', str(P['xsla_ingest']),
                             '--xrc_port', str(P['xrc']), '--xkpm_ctl', str(self._ctl('xkpm'))] + common)
        self._popen('xrc', ['mild_oran.xapps.xrc', '--port', str(P['xrc'])] + common)
        self._popen('xkpm', ['mild_oran.xapps.xkpm', '--port', str(P['xkpm']), '--sla_port', str(P['xsla_ingest'])]
                    + common)
        for r in XAPPS:
            self._wait_health(r)
        # 7. policy clients (tenants A/B, operator)
        self._popen('loadgen', ['mild_oran.xapps.loadgen', '--port', str(P['loadgen']), '--xrc_port', str(P['xrc']),
                                '--xsla_port', str(P['xsla']), '--t0', f'{self.t0:.3f}'] + common)
        self._wait_health('loadgen')
        self.kpi = KpiReader(cfg, P, self.rt, self.pid, self.weights_path, self.baseline, self.ues,
                             radio_fn=self.ranctl.radio_speed)
        self.wait_e2(float(ric.get('e2_wait_s', 30)) + 30)
        self.map_ues()
        self.traffic_on = True
        self._write_traffic_ctl(1.0)
        self._wait_probes()
        util.log('stack up: ' + ', '.join(f'{r}={p.pid}' for r, p in self.procs.items()))
        return self

    def wait_e2(self, timeout=60):
        """xKPM's E2 session is up and indications with all N UEs arrive."""
        t0 = time.time()
        lv = {}
        while time.time() - t0 < timeout:
            dead = self.check_alive()
            if dead:
                raise RuntimeError(f'components exited while waiting for E2: {dead}; see {self.logs}')
            lv = util.http_get_json('127.0.0.1', self._ctl('xkpm'), '/live', timeout=1.5) or {}
            if lv.get('connected') and lv.get('age_s') is not None and lv['age_s'] < 5 and \
                    (lv.get('n_ue') or 0) >= self.n:
                util.log(f"E2 up: node {lv.get('node')}, indications {lv.get('ind_hz', 0):.2f}/s, "
                         f"{lv.get('n_ue')} UEs in E2SM-KPM style 4")
                return lv
            time.sleep(1.0)
        tail = ''
        try:
            tail = open(os.path.join(self.rt, 'e2gw_xkpm.log')).read()[-600:]
        except OSError:
            pass
        raise RuntimeError(f'no E2SM-KPM indications with {self.n} UEs at xKPM within {timeout:.0f} s '
                           f'(last view {lv}). E2 front end log tail: {tail}')

    def _wait_probes(self, timeout=40):
        t0 = time.time()
        st = {}
        while time.time() - t0 < timeout:
            st = self.ran_status()
            if st['ue_ok'] and st['probe_ok']:
                util.log(f"user plane ok: UE addresses {st['ue_ips']}, newest probe reply "
                         f"{st['last_reply_age_s']:.1f} s ago (oldest over UEs)")
                return
            time.sleep(1.0)
        raise RuntimeError(f'user plane not usable within {timeout} s: {st}')

    # ------------------------------------------------------------------ UE identity (F1AP UE ID -> ueK)
    def map_ues(self, hold_s=6.0, settle_s=2.5):
        """E2SM-KPM style 4 names UEs only by their gNB-CU UE F1AP ID (attach order). Send DL traffic to ONE UE
        at a time and see which F1AP ID's DRB.UEThpDl rises. Writes runtime/ue_map.json (read by xKPM and xRC)."""
        f = float(self.cfg['tick_s']) / 4.0 if self.cfg.get('selftest') else 1.0
        rate = max(2.0, min(8.0, 0.35 * self.capacity))
        found, evidence = {}, {}
        for attempt in (1, 2):
            for ue in self.ues:
                if ue in found:
                    continue
                self.set_fixed_load(rate, only_ue=ue)
                time.sleep(settle_s * f)
                acc = {}
                t0 = time.time()
                while time.time() - t0 < hold_s * f:
                    lv = util.http_get_json('127.0.0.1', self._ctl('xkpm'), '/live', timeout=1.5) or {}
                    for fid, v in (lv.get('ue_raw') or {}).items():
                        acc.setdefault(str(fid), []).append(float(v or 0.0))
                    time.sleep(1.0 * f)
                means = sorted(((fid, sum(v) / len(v)) for fid, v in acc.items() if v), key=lambda x: -x[1])
                evidence[ue] = [(fid, round(m, 1)) for fid, m in means]
                if means and means[0][1] > 300.0 and (len(means) == 1 or means[0][1] >= 3.0 * max(means[1][1], 1.0)) \
                        and means[0][0] not in found.values():
                    found[ue] = means[0][0]
            if len(found) == self.n:
                break
            rate = min(12.0, rate * 1.5)
        self.set_fixed_load(None)
        method = 'traffic_test'
        if len(found) != self.n:
            lv = util.http_get_json('127.0.0.1', self._ctl('xkpm'), '/live', timeout=1.5) or {}
            ids = sorted((lv.get('ue_raw') or {}).keys(), key=lambda x: int(x) if str(x).isdigit() else 0)
            rest = [i for i in ids if i not in found.values()]
            for ue in self.ues:
                if ue not in found and rest:
                    found[ue] = rest.pop(0)
            method = 'partly_attach_order' if found else 'none'
            util.log(f'UE identity map: traffic test inconclusive ({evidence}); remaining UEs mapped in attach order',
                     'WARN')
        m = {'f1ap': {str(fid): {'ue': ue, 'sd': self.slices.get(ue)} for ue, fid in found.items()},
             'ue': {ue: {'f1ap': int(fid) if str(fid).isdigit() else fid, 'sd': self.slices.get(ue)}
                    for ue, fid in found.items()},
             'method': method, 'evidence_kbps': evidence, 'time': time.strftime('%Y-%m-%d %H:%M:%S')}
        util.save_json(os.path.join(self.rt, 'ue_map.json'), m)
        util.save_json(os.path.join(self.state, 'ue_map_last.json'), m)
        util.chown_tree(self.rt)
        self.ue_map = m
        util.log(f"UE identity map ({method}): " + ', '.join(f"{ue}=F1AP {v['f1ap']} (SD {v['sd']})"
                                                         for ue, v in m['ue'].items()))
        return m

    # ------------------------------------------------------------------ knobs
    def _knob(self, role, name, value):
        key = (role, name)
        v = f'{float(value):.4f}'
        if self.sent.get(key) == v:
            return True
        ok = util.http_post('127.0.0.1', self._ctl(role), f'/knob?name={name}&value={v}', timeout=1.0)
        if ok:
            self.sent[key] = v
        else:
            self.sent.pop(key, None)
            self.knob_failures[key] = self.knob_failures.get(key, 0) + 1
        return ok

    def _write_traffic_ctl(self, cell_load):
        shares = list(self.cfg['ran'].get('ue_shares') or [])[:self.n]
        d = {'cell_load': round(float(cell_load), 4), 'capacity_mbps': self.capacity,
             'ue_load_frac': self.cfg['ran'].get('ue_load_frac', 0.3), 'ue_shares': shares,
             'amp': self.cfg['ric'].get('load_amp', 0.5), 'day_s': self.cfg['ric'].get('day_s', 5760.0),
             'enabled': 1 if (self.traffic_on or self.fixed_mbps is not None) else 0}
        if self.fixed_mbps is not None:
            d['fixed_mbps'] = float(self.fixed_mbps)
            if self.only_ue:
                d['only_ue'] = self.ues.index(self.only_ue) + 1
        util.write_json_fast(os.path.join(self.rt, 'traffic_ctl.json'), d)
        self.cell_load_now = round(float(cell_load), 4)

    def set_fixed_load(self, mbps, only_ue=None):
        """Calibration only: a fixed total DL rate (split by ue_shares), or all of it to one UE; None = normal."""
        self.fixed_mbps, self.only_ue = mbps, (only_ue if mbps is not None else None)
        self._write_traffic_ctl(self.cell_load_now or 1.0)

    def set_capacity(self, mbps):
        self.capacity = float(mbps)
        self._write_traffic_ctl(self.cell_load_now or 1.0)

    def set_scales(self, sc):
        self.scales = dict(sc)

    def set_baseline(self, b):
        self.baseline.clear()
        self.baseline.update(b)

    def set_net(self, delay_ms, loss_pct):
        return self.netem.set(delay_ms, loss_pct)

    def apply(self, k):
        self._knob('xrc', 'work', k['api'])
        self._knob('xsla', 'work', k['analytics'])
        self._knob('xkpm', 'work', k['telemetry'])
        self._knob('xkpm', 'burst', k['burst'])              # extra real E2SM-KPM style-4 subscriptions
        cl = float(k['cell_load'])
        d, l = float(k['delay_ms']), float(k['loss_pct'])
        if self.backend == 'cell_load' and not self.mock:
            sc = self.scales
            sev = max(d / max(sc.get('delay_ms', 120.0), 1e-6), l / max(sc.get('loss_pct', 6.0), 1e-6))
            cl = max(cl, 1.0 + (sc.get('cell_load', 2.5) - 1.0) * min(1.0, sev))
            d = l = 0.0
        if self.cell_load_now is None or abs(cl - self.cell_load_now) > 1e-3:
            self._write_traffic_ctl(cl)
        if self.backend == 'netem' or self.mock:
            self.netem.set(d, l)

    def reset(self):
        self.sent = {}
        self.apply(NEUTRAL)

    def apply_tuning(self, tun):
        """Load tuning chosen by the remedy ladder (written into ric_cfg.json; xApps re-read it on restart)."""
        for k, v in (tun or {}).items():
            self.cfg['ric'][k] = v

    def read_kpis(self):
        return self.kpi.sample()

    # ------------------------------------------------------------------ status
    def ran_status(self):
        ran = self.cfg['ran']
        now = util.mono()
        lv = util.http_get_json('127.0.0.1', self._ctl('xkpm'), '/live', timeout=1.5) or {}
        age = lv.get('age_s')
        ips, reply_ages, fresh_all = {}, [], True
        for ue in self.ues:
            ag = util.load_json(os.path.join(self.rt, f'ue_agent_{ue}.json')) or {}
            fresh = bool(ag) and now - float(ag.get('t_mono', 0)) < 5.0
            fresh_all = fresh_all and fresh
            ips[ue] = ag.get('ue_ip') if fresh else None
            rep = [t for _, t, r in (ag.get('pings') or []) if r is not None]
            reply_ages.append((now - max(rep)) if rep else None)
        worst = None if any(x is None for x in reply_ages) else max(reply_ages)
        return {'e2_ok': bool(lv.get('connected')) and age is not None and age < float(ran.get('e2_stale_s', 10.0)),
                'e2_age_s': age, 'e2_hz': lv.get('ind_hz') or 0.0, 'n_ue_e2': lv.get('n_ue'),
                'ue_ok': fresh_all and all(ips.values()), 'ue_ips': ips,
                'probe_ok': worst is not None and worst < float(ran.get('probe_dead_s', 20.0)),
                'last_reply_age_s': worst, 'agents_fresh': fresh_all}

    def rotate_e2(self, wait=True):
        """Fresh E2 front end for every xApp (fresh E42 session + fresh FlexRIC SQLite file)."""
        for r in XAPPS:
            util.http_post('127.0.0.1', self._ctl(r), '/rotate', timeout=2.0)
        if wait:
            time.sleep(3.0)
            try:
                self.wait_e2(60)
            except RuntimeError as e:
                util.log(f'E2 rotation: {e}', 'WARN')
                return False
        return True

    def restart_role(self, role):
        """Restart one MILD process with its original arguments (remedy for a hung component)."""
        p = self.procs.get(role)
        if p is not None and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
                p.wait(6)
            except Exception:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except Exception:
                    pass
        args, as_root, nice, prefix = self.args[role]
        self._popen(role, args, as_root=as_root, nice=nice, prefix=prefix)
        if role in XAPPS or role == 'loadgen':
            self._wait_health(role)
        self.sent = {}

    def component_stats(self):
        """Counters of every component (for the traffic summary). Latency samples are left out."""
        out = {}
        for r in ('xkpm', 'xsla', 'xrc', 'loadgen'):
            s = util.http_get_json('127.0.0.1', self._ctl(r), '/stats?since=999999999', timeout=2.0)
            if s:
                s.pop('samples', None)
                s.pop('live', None)
                out[r] = s
        t = util.load_json(os.path.join(self.rt, 'traffic.json')) or {}
        out['traffic'] = {k: t.get(k) for k in ('rate_mbps', 'total_mbps', 'sent_bytes', 'errors')}
        out['ue_map'] = self.ue_map.get('ue')
        return out

    def check_alive(self):
        return [(r, p.returncode) for r, p in self.procs.items() if p.poll() is not None]

    def stop(self):
        """Stop every MILD process (xApps first, so their E2 front ends unsubscribe while the RIC is still up)."""
        try:
            self.netem.clear()
        except Exception:
            pass
        order = ['loadgen', 'xkpm', 'xsla', 'xrc', 'traffic'] + [r for r in self.procs if r.startswith('ue_agent')] + \
            ['appserver']
        for r in order + [r for r in self.procs if r not in order]:
            p = self.procs.get(r)
            if p is not None and p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except Exception:
                    try:
                        p.terminate()
                    except Exception:
                        pass
                if r in XAPPS:                      # give the E2 front end time to unsubscribe and exit
                    try:
                        p.wait(6)
                    except Exception:
                        pass
        t0 = time.time()
        while time.time() - t0 < 4 and any(p.poll() is None for p in self.procs.values()):
            time.sleep(0.2)
        for r, p in list(self.procs.items()):
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
        util.kill_leftovers()
        self.procs = {}

    def manifest(self):
        return dict(self.info, capacity_mbps=self.capacity, baseline=self.baseline,
                    pids={r: p.pid for r, p in self.procs.items()}, ports=self.ports, ue_map=self.ue_map.get('ue'),
                    e2gw=e2gw_command(self.cfg, self.mock)[0],
                    knob_failures={f'{a}.{b}': n for (a, b), n in self.knob_failures.items()})


def make_stack(cfg, run_dir, ranctl=None):
    if cfg.get('selftest'):
        from .fakestack import FakeStack, FakeStackV4
        return (FakeStackV4 if cfg.get('scenario') in ('v4', 'v5') else FakeStack)(cfg, run_dir)
    if ranctl is None:
        from .ranctl import make_ranctl
        ranctl = make_ranctl(cfg, cfg['state_dir'])
    return RealStack(cfg, run_dir, ranctl, mock_ran=bool(cfg.get('mock_ran')))
