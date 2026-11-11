"""
stack.py -- start, verify, drive and stop the SDN edge-to-cloud testbed.

RealStack : Mininet + OVS + Ryu (+ ofctl_rest) + 3 cloud services + exporters
            + Prometheus + load generators + IoT producers + iperf3 cross-traffic.
MockStack : the same services, exporters and traffic generators as local
            processes on 127.0.0.1 without Mininet. Used by `pipeline.py --mock`
            to test the whole pipeline on any machine without root. The
            network impairment is a no-op in mock mode.

Both classes expose the same interface:
  start()  preflight -> launch -> health checks (raises RuntimeError with a clear message)
  apply(knobs)       set service knobs, device bursts, cross-traffic and WAN impairment
  read_kpis()        dict of the 8 base KPIs (None when not measurable)
  reset(), stop(), manifest()
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

import requests

from . import util
from .kpi import DirectKPI, PromKPI

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TB = os.path.join(ROOT, 'testbed')
SVC_DIR = os.path.join(TB, 'services')


class _Base:
    def __init__(self, cfg, run_dir):
        self.cfg = cfg
        self.run_dir = run_dir
        self.logs = os.path.join(run_dir, 'logs')
        os.makedirs(self.logs, exist_ok=True)
        self.procs = []
        self.last_net = (None, None)
        self.sess = requests.Session()
        self.info = {'mode': self.__class__.__name__}
        self.sent, self.knob_failures = {}, {}
        self.burst_files = []
        self.weights_path = os.path.join(run_dir, 'snet_sri_weights.json')
        if not os.path.exists(self.weights_path):
            util.save_json(self.weights_path, cfg['snet_sri_weights'])

    # ---------------------------------------------------------------
    def _post(self, url, timeout=1.0):
        try:
            return self.sess.post(url, timeout=timeout).status_code == 200
        except Exception:
            return False

    def _ctl_url(self, service):
        """Out-of-band knob endpoint of a service (its port + 1000, own thread)."""
        base, port = self.urls[service].rsplit(':', 1)
        return f'{base}:{int(port) + 1000}/knob'

    def _popen(self, cmd, name, host=None, cwd=None, env=None):
        lf = open(os.path.join(self.logs, f'{name}.log'), 'a')
        e = dict(os.environ, PYTHONUNBUFFERED='1', OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1')
        if env:
            e.update(env)
        if host is not None:
            p = host.popen(cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=cwd, env=e)
        else:
            p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=cwd, env=e)
        self.procs.append((name, p))
        return p

    def check_alive(self):
        dead = [(n, p.poll()) for n, p in self.procs if p.poll() is not None]
        return dead

    def _set_knob(self, key, url, value, fmt='{:.4f}'):
        """Send a knob only when its value changed. A failed send is retried next tick."""
        v = fmt.format(value)
        if self.sent.get(key) == v:
            return True
        ok = self._post(f'{url}?mult={v}' if '?' not in url else url.format(v), timeout=1.0)
        if ok:
            self.sent[key] = v
        else:
            self.sent.pop(key, None)
            self.knob_failures[key] = self.knob_failures.get(key, 0) + 1
        return ok

    def apply(self, k):
        for s in ('api', 'analytics', 'telemetry'):
            self._set_knob(s, self._ctl_url(s), k[s])
        if self.burst_files:                  # Mininet: out-of-band control files (never crosses the WAN)
            for p in self.burst_files:
                try:
                    with open(p, 'w') as f:
                        f.write(f"{k['burst']:.3f}")
                except Exception:
                    pass
        else:
            for j, b in enumerate(self.burst_urls):
                self._set_knob(f'burst{j}', f'{b}/burst', k['burst'], '{:.3f}')
        try:
            with open(self.cross_ctl, 'w') as f:
                f.write(f"{k['cross']:.3f}")
        except Exception:
            pass
        prof = (round(k['delay_ms'], 1), round(k['loss_pct'], 2))
        if prof != self.last_net:
            self.set_net(*prof)
            self.last_net = prof

    def reset(self):
        self.sent = {}                       # force every knob to be re-sent
        self.apply({'api': 1, 'analytics': 1, 'telemetry': 1, 'burst': 1, 'cross': 1, 'delay_ms': 0, 'loss_pct': 0})

    def knobs_actual(self):
        """Read back the knob values from the services (used by the watchdog and the health check)."""
        out = {}
        for s in ('api', 'analytics', 'telemetry'):
            try:
                out[s] = float(self.sess.get(self._ctl_url(s), timeout=1.0).json()['mult'])
            except Exception:
                out[s] = None
        return out

    def rotate_logs(self):
        """Truncate the append-only service logs on the volume at a segment start.

        The API audit log and the analytics result store grow for the whole run,
        which made the storage KPI drift upwards from segment to segment. The
        telemetry spool is not touched because it is part of the telemetry fault.
        """
        vol = getattr(self, 'vol', None)
        if not vol or not os.path.isdir(vol) or not self.cfg.get('rotate_service_logs', True):
            return
        for name in os.listdir(vol):
            if name.startswith(('api_audit', 'analytics_results')):
                try:
                    open(os.path.join(vol, name), 'w').close()
                except Exception:
                    pass

    def read_kpis(self):
        return self.kpi.read()

    def stop(self):
        try:
            self.reset()
        except Exception:
            pass
        for n, p in reversed(self.procs):
            try:
                p.terminate()
            except Exception:
                pass
        t0 = time.time()
        for n, p in self.procs:
            while p.poll() is None and time.time() - t0 < 8:
                time.sleep(0.2)
            if p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass
        self.procs = []

    def manifest(self):
        return self.info

    # common health checks -------------------------------------------------
    def _health(self, timeout_s=120):
        util.log('health check: services')
        for s, u in self.urls.items():
            util.wait_http(f'{u}/healthz', 60)
        util.log('health check: KPIs readable (all 8 present for 10 consecutive reads)')
        good, t0, last = 0, time.time(), {}
        while good < 10:
            if time.time() - t0 > timeout_s:
                miss = [k for k, v in last.items() if v is None]
                raise RuntimeError(f'KPIs not readable after {timeout_s}s; missing={miss}; '
                                   f'targets={self.kpi.targets_up()}; check {self.logs}')
            last = self.read_kpis()
            good = good + 1 if all(v is not None for v in last.values()) else 0
            time.sleep(1.0)
        util.log(f'health check: KPIs OK {json.dumps({k: round(v, 2) for k, v in last.items()})}')
        dead = self.check_alive()
        if dead:
            raise RuntimeError(f'processes died during start-up: {dead}; see {self.logs}')
        util.log('health check: traffic generators produce traffic')
        t0 = time.time()
        while True:
            ok = all(os.path.exists(p) and os.path.getsize(p) > 0 for p in self.traffic_logs)
            if ok:
                bad = []
                for p in self.traffic_logs:
                    with open(p) as f:
                        rec = json.loads(f.readlines()[-1])
                    succ = rec.get('api_success', rec.get('post_success', 1.0))
                    if succ is not None and succ < 0.8:
                        bad.append((os.path.basename(p), succ))
                if bad:
                    raise RuntimeError(f'traffic generators see failures: {bad}')
                break
            if time.time() - t0 > 40:
                raise RuntimeError(f'no traffic logs after 40 s: {self.traffic_logs}')
            time.sleep(2)
        util.log('health check: out-of-band knob channel')
        self.sent = {}
        self.apply({'api': 1.5, 'analytics': 1, 'telemetry': 1, 'burst': 1, 'cross': 1, 'delay_ms': 0, 'loss_pct': 0})
        got = self.knobs_actual()
        self.reset()
        if got.get('api') != 1.5 or None in got.values():
            raise RuntimeError(f'knob control ports not answering correctly: {got}. Are the updated services in use?')
        util.log('health check: PASSED')


# =============================================================================
class RealStack(_Base):
    """Mininet + Ryu testbed (requires root)."""

    def start(self):
        cfg = self.cfg
        if os.geteuid() != 0:
            raise RuntimeError('RealStack needs root: run with sudo -E')
        from mininet.clean import cleanup as mn_cleanup
        from mininet.log import setLogLevel
        sys.path.insert(0, TB)
        import topology as T
        self.T = T
        setLogLevel('warning')

        util.log('preflight: binaries')
        need = {'ryu/osken': util.which('ryu-manager', 'osken-manager'), 'prometheus': util.which('prometheus'),
                'ovs-vsctl': util.which('ovs-vsctl'), 'iperf3': util.which('iperf3'), 'tc': util.which('tc'),
                'ping': util.which('ping'), 'mkfs.ext4': util.which('mkfs.ext4')}
        missing = [k for k, v in need.items() if not v]
        if missing:
            raise RuntimeError(f'missing binaries: {missing}. Run scripts/install_deps_ubuntu.sh')
        if util.run(['ovs-vsctl', 'show']).returncode != 0:
            util.run(['service', 'openvswitch-switch', 'start'])
            time.sleep(2)
            if util.run(['ovs-vsctl', 'show']).returncode != 0:
                raise RuntimeError('Open vSwitch is not running (sudo service openvswitch-switch start)')
        util.log('preflight: cleaning leftovers from earlier runs')
        killed = util.kill_leftovers()
        if killed:
            util.log(f'killed leftovers: {killed}')
        mn_cleanup()
        util.log('preflight: ports')
        self.of_port = util.free_port_or_alternative(cfg['of_port'], 'OpenFlow')
        self.rest_port = util.free_port_or_alternative(cfg['ryu_rest_port'], 'Ryu REST')
        self.prom_port = util.free_port_or_alternative(cfg['prom_port'], 'Prometheus')
        self.res_port = util.free_port_or_alternative(cfg['resource_exporter_port'], 'resource exporter')
        self.info.update({'of_port': self.of_port, 'rest_port': self.rest_port, 'prom_port': self.prom_port,
                          'resource_port': self.res_port, 'binaries': need})

        # storage volume ---------------------------------------------------
        self.vol = cfg['volume_mount']
        img = os.path.join(cfg['state_dir'], 'cloud_volume.img')
        util.run(['umount', '-l', self.vol])
        os.makedirs(self.vol, exist_ok=True)
        if os.path.exists(img):
            os.remove(img)
        util.run(['fallocate', '-l', f"{cfg['volume_mb']}M", img])
        r1 = util.run(['mkfs.ext4', '-F', '-q', '-m', '0', img])
        r2 = util.run(['mount', '-o', 'loop', img, self.vol])
        self.info['volume'] = 'loop-ext4' if (r1.returncode == 0 and r2.returncode == 0) else 'host-directory (fallback)'
        if self.info['volume'] != 'loop-ext4':
            util.log(f'WARNING: loop volume failed ({r1.stderr} {r2.stderr}); disk KPI uses the host file system', 'WARN')

        # controller -------------------------------------------------------
        mgr = need['ryu/osken']
        rest_app = 'os_ken.app.ofctl_rest' if 'osken' in mgr else 'ryu.app.ofctl_rest'
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        cmd = [mgr, '--ofp-tcp-listen-port', str(self.of_port), '--wsapi-host', '0.0.0.0', '--wsapi-port',
               str(self.rest_port), os.path.join(TB, 'controller', 'mild_switch.py'), rest_app]
        lf = open(os.path.join(self.logs, 'controller.log'), 'a')
        self.procs.append(('controller', subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, env=env)))
        time.sleep(3)
        if self.check_alive():
            raise RuntimeError(f'Ryu controller exited; see {self.logs}/controller.log')

        # network ----------------------------------------------------------
        util.log('starting Mininet topology (6 switches, 9 hosts)')
        self.built = T.build_network(controller_port=self.of_port)
        H = self.built.hosts
        t0 = time.time()
        while True:
            r = H['hcliA'].cmd(f"ping -c1 -W1 {T.SERVICE_IP['api']}")
            if ' 0% packet loss' in r:
                break
            if time.time() - t0 > 30:
                raise RuntimeError('data plane not forwarding (hcliA cannot reach hapi); see controller.log')
            time.sleep(1)
        self.built.net.pingAll(timeout='1')

        # services (optionally inside cgroup scopes) -----------------------
        root_url = f'http://{T.ROOT_IP}:{self.rest_port}'
        self.urls = {s: f'http://{T.SERVICE_IP[s]}:{T.SERVICE_PORT[s]}' for s in T.SERVICE_IP}
        args = {
            'api': ['--port', '8000', '--base-ms', str(cfg['api_base_ms']), '--controller', root_url, '--dpid', '4',
                    '--deadline-s', str(cfg.get('api_deadline_s', 2.0))],
            'analytics': ['--port', '8001', '--base-ms', str(cfg['analytics_base_ms']), '--api', self.urls['api'],
                          '--sched-rate', str(cfg['analytics_sched_rate'])],
            'telemetry': ['--port', '8002', '--base-ms', str(cfg['telemetry_base_ms']), '--analytics', self.urls['analytics']],
        }
        runid = str(int(time.time()))
        use_cg = cfg['use_cgroups'] and util.which('systemd-run') and os.path.exists('/sys/fs/cgroup/cgroup.controllers')
        cgdirs = {}
        for s, h in T.SERVICE_HOST.items():
            base = [util.python_exe(), os.path.join(SVC_DIR, f'{s}_service.py'), *args[s], '--run-dir', self.run_dir,
                    '--vol', self.vol]
            if use_cg:
                unit = f'mild-{s}-{runid}'
                cmd = ['systemd-run', '--scope', '--quiet', '--slice=mild.slice', f'--unit={unit}',
                       '-p', f"CPUQuota={cfg['service_cpu_quota_pct']}%", '-p', f"MemoryMax={cfg['service_mem_mb']}M", *base]
                cgdirs[s] = f'/sys/fs/cgroup/mild.slice/{unit}.scope'
            else:
                cmd = base
            self._popen(cmd, f'svc_{s}', host=H[h], cwd=SVC_DIR)
        time.sleep(4)
        if use_cg and not all(os.path.exists(os.path.join(d, 'cpu.stat')) for d in cgdirs.values()):
            util.log('WARNING: cgroup scopes not found; resource KPIs fall back to psutil', 'WARN')
            cgdirs = {}
        self.info['resource_mode'] = 'cgroup' if cgdirs else 'psutil'
        self.info['cgroups'] = cgdirs
        for s, u in self.urls.items():
            util.wait_http(f'{u}/healthz', 60)

        # exporters --------------------------------------------------------
        self._popen([util.python_exe(), os.path.join(TB, 'monitor', 'resource_exporter.py'), '--port', str(self.res_port),
                     '--cgroups', json.dumps(cgdirs), '--run-dir', self.run_dir, '--vol', self.vol,
                     '--mem-budget-mb', str(cfg['service_mem_mb'])], 'resource_exporter')
        probes = [f"{s},{T.SERVICE_IP[s]},{self.urls[s]}/healthz" for s in self.urls]
        for site, h in (('A', 'hcliA'), ('B', 'hcliB')):
            pa = sum([['--probe', p] for p in probes], [])
            self._popen([util.python_exe(), os.path.join(TB, 'monitor', 'netprobe_exporter.py'), '--site', site,
                         '--listen-port', '9101', '--weights', self.weights_path, *pa], f'netprobe_{site}', host=H[h])

        # prometheus -------------------------------------------------------
        prom_cfg = os.path.join(self.run_dir, 'mild_sdn2_prom.yml')
        jobs = [('resource', [f'127.0.0.1:{self.res_port}']), ('netprobe', ['10.0.0.11:9101', '10.0.0.21:9101']),
                ('api', ['10.0.0.101:9000']), ('analytics', ['10.0.0.102:9001']), ('telemetry', ['10.0.0.103:9002'])]
        # service metrics are scraped on the out-of-band control ports (port + 1000), never on the work port
        with open(prom_cfg, 'w') as f:
            f.write('global:\n  scrape_interval: 1s\n  evaluation_interval: 1s\n  scrape_timeout: 900ms\nscrape_configs:\n')
            for j, t in jobs:
                f.write(f"  - job_name: {j}\n    static_configs:\n      - targets: {json.dumps(t)}\n")
        tsdb = os.path.join(self.run_dir, 'prom-data')
        os.makedirs(tsdb, exist_ok=True)
        self._popen([need['prometheus'], f'--config.file={prom_cfg}', f'--storage.tsdb.path={tsdb}',
                     '--storage.tsdb.retention.time=6h', f'--web.listen-address=127.0.0.1:{self.prom_port}'], 'prometheus')
        util.wait_http(f'http://127.0.0.1:{self.prom_port}/-/ready', 60)
        self.kpi = PromKPI(f'http://127.0.0.1:{self.prom_port}', cfg['rate_window_s'])

        # traffic ----------------------------------------------------------
        day_s = 1440 * cfg['tick_s']
        tl = os.path.join(self.run_dir, 'traffic')
        os.makedirs(tl, exist_ok=True)
        self.traffic_logs = []
        for site, h, seed in (('A', 'hcliA', 11), ('B', 'hcliB', 21)):
            p = os.path.join(tl, f'loadgen_{site}.jsonl')
            self.traffic_logs.append(p)
            self._popen([util.python_exe(), os.path.join(TB, 'traffic', 'loadgen.py'), '--site', site,
                         '--api-url', f"{self.urls['api']}/work", '--analytics-url', f"{self.urls['analytics']}/submit?n={{}}",
                         '--api-rps', str(cfg['api_rps_per_site']), '--dash-rps', str(cfg['dash_rps_per_site']),
                         '--dash-jobs-mean', str(cfg['dash_jobs_mean']), '--day-s', str(day_s), '--seed', str(seed),
                         '--log', p], f'loadgen_{site}', host=H[h])
        self.burst_urls = []
        for site, h, ip, seed in (('A', 'hiotA', '10.0.0.12', 12), ('B', 'hiotB', '10.0.0.22', 22)):
            p = os.path.join(tl, f'iot_{site}.jsonl')
            self.traffic_logs.append(p)
            bf = os.path.join(self.run_dir, f'burst_ctl_{site}.txt')
            with open(bf, 'w') as f:
                f.write('1.0')
            self.burst_files.append(bf)
            self._popen([util.python_exe(), os.path.join(TB, 'traffic', 'iot_producer.py'), '--site', site,
                         '--url', f"{self.urls['telemetry']}/ingest", '--devices', str(cfg['devices_per_site']),
                         '--period', str(cfg['device_period_s']), '--day-s', str(day_s), '--seed', str(seed),
                         '--ctl-file', bf, '--log', p], f'iot_{site}', host=H[h])
            self.burst_urls.append(f'http://{ip}:9200')
        self._popen(['iperf3', '-s', '-p', '5201'], 'iperf3_server', host=H['hsink'])
        self.cross_ctl = os.path.join(self.run_dir, 'cross_ctl.txt')
        with open(self.cross_ctl, 'w') as f:
            f.write('1.0')
        time.sleep(1)
        for h, port in (('hcliB', 5201), ('hiotA', 5201)):
            self._popen([util.python_exe(), os.path.join(TB, 'traffic', 'crosstraffic.py'), '--server', '10.0.0.104',
                         '--port', str(port), '--base-mbps', str(cfg['cross_mbps_per_flow']), '--day-s', str(day_s),
                         '--ctl', self.cross_ctl, '--log', os.path.join(tl, f'cross_{h}.jsonl')], f'cross_{h}', host=H[h])
        self._health()
        self._verify_controller_and_wan()
        return self

    def _verify_controller_and_wan(self):
        T, H = self.T, self.built.hosts
        util.log('health check: Ryu northbound REST and actuation path')
        sw = requests.get(f'http://127.0.0.1:{self.rest_port}/stats/switches', timeout=5).json()
        if len(sw) < 6:
            raise RuntimeError(f'Ryu REST sees {len(sw)} switches (expected 6)')
        r = requests.post(f"{self.urls['api']}/actuate", json={'action': 'meter', 'target': '10.0.0.251'}, timeout=5).json()
        self.info['actuation_check'] = r.get('controller')
        if r.get('controller') != 'ok':
            util.log(f'WARNING: actuation through Ryu REST returned {r}', 'WARN')
        util.log('health check: WAN impairment changes the measured RTT')

        def rtt():
            out = H['hcliA'].cmd(f"ping -n -q -c 5 -i 0.2 {T.SERVICE_IP['api']}")
            try:
                return float(out.split('=')[-1].split('/')[1])
            except Exception:
                return None
        r0 = rtt()
        self.set_net(40.0, 0.0)
        time.sleep(1)
        r1 = rtt()
        self.set_net(0.0, 0.0)
        self.info['wan_check'] = {'rtt_base_ms': r0, 'rtt_with_40ms_ms': r1}
        if r0 is None or r1 is None or r1 - r0 < 60:
            raise RuntimeError(f'WAN impairment check failed: base RTT {r0} ms, with +40 ms each way {r1} ms')
        util.log(f'WAN check OK: RTT {r0:.1f} -> {r1:.1f} ms')

    def set_net(self, delay_ms, loss_pct):
        self.T.set_wan_impairment(self.built, delay_ms, loss_pct)

    def stop(self):
        super().stop()
        try:
            self.built.net.stop()
        except Exception:
            pass
        try:
            from mininet.clean import cleanup
            cleanup()
        except Exception:
            pass
        util.run(['umount', '-l', self.cfg['volume_mount']])


# =============================================================================
class MockStack(_Base):
    """Same processes on 127.0.0.1, no Mininet (for testing the pipeline)."""

    def start(self):
        cfg = self.cfg
        util.kill_leftovers()
        self.vol = tempfile.mkdtemp(prefix='mild_mockvol_')
        base = cfg.get('mock_port_base', 18000)
        ports = {'api': base, 'analytics': base + 1, 'telemetry': base + 2}
        for k in list(ports):
            ports[k] = util.free_port_or_alternative(ports[k], k)
        self.urls = {s: f'http://127.0.0.1:{p}' for s, p in ports.items()}
        args = {'api': ['--base-ms', str(cfg['api_base_ms']), '--deadline-s', str(cfg.get('api_deadline_s', 2.0))],
                'analytics': ['--base-ms', str(cfg['analytics_base_ms']), '--api', self.urls['api'],
                              '--sched-rate', str(cfg['analytics_sched_rate'])],
                'telemetry': ['--base-ms', str(cfg['telemetry_base_ms']), '--analytics', self.urls['analytics']]}
        for s in ports:
            self._popen([util.python_exe(), os.path.join(SVC_DIR, f'{s}_service.py'), '--port', str(ports[s]), *args[s],
                         '--run-dir', self.run_dir, '--vol', self.vol], f'svc_{s}', cwd=SVC_DIR)
        for s, u in self.urls.items():
            util.wait_http(f'{u}/healthz', 60)
        rp = util.free_port_or_alternative(base + 10, 'resource exporter')
        npp = util.free_port_or_alternative(base + 11, 'netprobe')
        self._popen([util.python_exe(), os.path.join(TB, 'monitor', 'resource_exporter.py'), '--port', str(rp),
                     '--run-dir', self.run_dir, '--vol', self.vol, '--cores', json.dumps({s: 1 for s in ports}),
                     '--mem-budget-mb', str(cfg['service_mem_mb'])], 'resource_exporter')
        pa = sum([['--probe', f'{s},127.0.0.1,{u}/healthz'] for s, u in self.urls.items()], [])
        self._popen([util.python_exe(), os.path.join(TB, 'monitor', 'netprobe_exporter.py'), '--site', 'A',
                     '--listen-port', str(npp), '--weights', self.weights_path, *pa], 'netprobe_A')
        time.sleep(2)
        self.kpi = DirectKPI({'res': f'http://127.0.0.1:{rp}/metrics', 'net': f'http://127.0.0.1:{npp}/metrics',
                              **{s: self._ctl_url(s).replace('/knob', '/metrics') for s in self.urls}}, cfg['rate_window_s'])
        tl = os.path.join(self.run_dir, 'traffic')
        os.makedirs(tl, exist_ok=True)
        day_s = 1440 * cfg['tick_s']
        p1, p2 = os.path.join(tl, 'loadgen_A.jsonl'), os.path.join(tl, 'iot_A.jsonl')
        self.traffic_logs = [p1, p2]
        ctl = util.free_port_or_alternative(base + 12, 'iot ctl')
        self._popen([util.python_exe(), os.path.join(TB, 'traffic', 'loadgen.py'), '--site', 'A',
                     '--api-url', f"{self.urls['api']}/work", '--analytics-url', f"{self.urls['analytics']}/submit?n={{}}",
                     '--api-rps', str(cfg['api_rps_per_site']), '--dash-rps', str(cfg['dash_rps_per_site']),
                     '--day-s', str(day_s), '--log', p1], 'loadgen_A')
        self._popen([util.python_exe(), os.path.join(TB, 'traffic', 'iot_producer.py'), '--site', 'A',
                     '--url', f"{self.urls['telemetry']}/ingest", '--devices', str(cfg['devices_per_site']),
                     '--period', str(cfg['device_period_s']), '--ctl-port', str(ctl), '--day-s', str(day_s), '--log', p2], 'iot_A')
        self.burst_urls = [f'http://127.0.0.1:{ctl}']
        self.cross_ctl = os.path.join(self.run_dir, 'cross_ctl.txt')
        self.info.update({'resource_mode': 'psutil', 'volume': 'temp-directory (mock)', 'wan': 'no-op (mock)'})
        self._health(timeout_s=90)
        return self

    def set_net(self, delay_ms, loss_pct):
        pass
