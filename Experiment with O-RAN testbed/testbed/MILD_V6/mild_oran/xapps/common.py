"""common.py -- shared machinery of the MILD xApps and the load generator.

Every MILD xApp (xKPM, xSLA, xRC) is one Linux process tree:
  * the Python application (its real function: KPM processing, analytics, policy/control API);
  * its own E2 front end, the FlexRIC xApp program mild_e2gw (E42 over SCTP to the nearRT-RIC),
    supervised by the E2Link class below (restarted when it exits, rotated per segment);
  * an out-of-band CONTROL port (data port + 1000, own thread).
Every process has:
  * a DATA plane (its real function: E2 ingest, analytics, control API, ...);
  * an out-of-band CONTROL port (data port + 1000, own thread) that serves
      GET  /health            liveness
      GET  /stats             counters and gauges (JSON)
      POST /knob?name=X&value=V   fault knob (the experiment driver uses it)
    The control port never shares a worker with the data plane, so a saturated
    component can still be measured and reset (lesson of the SDN "API jam").

Service time model. Each request / record / job runs its own logic (parsing,
validation, conflict checks, statistics) and is then completed with CPU-bound
work up to a calibrated nominal CPU time. A fault knob multiplies that time.
The work is measured in THREAD CPU time (time.thread_time), so a component that
waits for the CPU (or for the Python GIL) takes longer in wall time, exactly
like a CPU-starved container.
"""
from __future__ import annotations

import json
import os
import random
import socket
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

mono = time.monotonic


def log(name, msg):
    print(f"[{time.strftime('%H:%M:%S')}] {name}: {msg}", flush=True)


def burn(ms: float) -> int:
    """Consume `ms` milliseconds of THIS thread's CPU time with integer arithmetic."""
    if ms <= 0:
        return 0
    t_end = time.thread_time() + ms / 1000.0
    x, acc = 0x9E3779B9, 0
    while time.thread_time() < t_end:
        for _ in range(150):
            x = (x * 1103515245 + 12345) & 0xFFFFFFFF
            acc ^= (x >> 7)
    return acc


class Knobs:
    def __init__(self, **defaults):
        self._v = dict(defaults)
        self._lock = threading.Lock()

    def get(self, k, default=1.0):
        with self._lock:
            return float(self._v.get(k, default))

    def set(self, k, v):
        with self._lock:
            self._v[k] = float(v)

    def all(self):
        with self._lock:
            return dict(self._v)


class LatRing:
    """Completed-request latencies with sequence numbers (incremental fetch by the driver)."""

    def __init__(self, cap=20000):
        self.buf = deque(maxlen=cap)
        self.seq = 0
        self.lock = threading.Lock()

    def add(self, t_done, lat_ms, shed=False):
        with self.lock:
            self.seq += 1
            self.buf.append((self.seq, round(t_done, 4), round(lat_ms, 2), 1 if shed else 0))

    def since(self, s, limit=5000):
        with self.lock:
            out = [x for x in self.buf if x[0] > s]
            last = self.seq
        return out[-limit:], last


class RateMeter:
    """Events per second over a sliding window (monotonic)."""

    def __init__(self, window=10.0):
        self.w = window
        self.t = deque()
        self.lock = threading.Lock()

    def hit(self, n=1):
        now = mono()
        with self.lock:
            for _ in range(n):
                self.t.append(now)
            while self.t and now - self.t[0] > self.w:
                self.t.popleft()

    def rate(self):
        now = mono()
        with self.lock:
            while self.t and now - self.t[0] > self.w:
                self.t.popleft()
            return len(self.t) / self.w


class CtlServer:
    """The out-of-band control port of a component."""

    def __init__(self, name, port, knobs: Knobs, stats_fn, routes=None, host='127.0.0.1'):
        self.name, self.port, self.knobs, self.stats_fn = name, port, knobs, stats_fn
        self.routes = routes or {}
        srv = self

        class H(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj, separators=(',', ':')).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def _body(self):
                n = int(self.headers.get('Content-Length') or 0)
                return self.rfile.read(n) if n else b''

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                try:
                    if u.path == '/health':
                        return self._send(200, {'ok': 1, 'name': srv.name, 'pid': os.getpid()})
                    if u.path == '/stats':
                        st = srv.stats_fn(q)
                        st['knobs'] = srv.knobs.all()
                        st['t_mono'] = mono()
                        return self._send(200, st)
                    if ('GET', u.path) in srv.routes:
                        return self._send(200, srv.routes[('GET', u.path)](q, b''))
                    self._send(404, {'error': 'not found'})
                except Exception as e:  # never let the control port die
                    self._send(500, {'error': str(e)})

            def do_POST(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                body = self._body()
                try:
                    if u.path == '/knob':
                        if 'name' in q:
                            srv.knobs.set(q['name'], float(q['value']))
                        else:                       # /knob?work=2.5&burst=1.0
                            for k, v in q.items():
                                srv.knobs.set(k, float(v))
                        return self._send(200, srv.knobs.all())
                    if ('POST', u.path) in srv.routes:
                        return self._send(200, srv.routes[('POST', u.path)](q, body))
                    self._send(404, {'error': 'not found'})
                except Exception as e:
                    self._send(500, {'error': str(e)})

        self.httpd = ThreadingHTTPServer((host, port), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name=f'{name}-ctl').start()


class LineServer:
    """TCP server that receives newline-delimited JSON (E2-like association / internal stream).

    One reader thread per connection. `on_line(bytes)` may block: that is how
    backpressure propagates to the sender through TCP.
    """

    def __init__(self, name, port, on_line, host='127.0.0.1'):
        self.name, self.on_line = name, on_line
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(16)
        self.conns = 0
        threading.Thread(target=self._accept, daemon=True, name=f'{name}-accept').start()

    def _accept(self):
        while True:
            c, _ = self.sock.accept()
            c.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 18)
            self.conns += 1
            threading.Thread(target=self._read, args=(c,), daemon=True).start()

    def _read(self, c):
        f = c.makefile('rb', buffering=1 << 16)
        try:
            for line in f:
                if line.strip():
                    self.on_line(line)
        except Exception:
            pass
        finally:
            try:
                c.close()
            except Exception:
                pass
            self.conns -= 1


class LineClient:
    """Persistent TCP client for newline-delimited JSON, reconnecting on failure."""

    def __init__(self, host, port, name='client'):
        self.addr = (host, port)
        self.name = name
        self.s = None
        self.fail = 0

    def send(self, data: bytes) -> bool:
        for _ in range(2):
            try:
                if self.s is None:
                    self.s = socket.create_connection(self.addr, timeout=3)
                    self.s.settimeout(None)          # blocking: backpressure stalls here
                self.s.sendall(data if data.endswith(b'\n') else data + b'\n')
                return True
            except Exception:
                self.fail += 1
                try:
                    if self.s:
                        self.s.close()
                except Exception:
                    pass
                self.s = None
                time.sleep(0.2)
        return False


def proc_rss_kb():
    try:
        with open('/proc/self/status') as f:
            for l in f:
                if l.startswith('VmRSS:'):
                    return int(l.split()[1])
    except Exception:
        pass
    return 0


class Diurnal:
    """Load profile 1 + amp*sin(2*pi*t/period), times an Ornstein-Uhlenbeck noise term.

    t is measured from a common origin `t0` (monotonic) passed by the driver,
    so every load generator follows the same 'day'.
    """

    def __init__(self, t0, period_s, amp=0.5, ou_tau=120.0, ou_sigma=0.15, seed=1):
        self.t0, self.P, self.amp = t0, period_s, amp
        self.tau, self.sig = ou_tau, ou_sigma
        self.x = 0.0
        self.last = mono()
        self.rng = random.Random(seed)

    def factor(self):
        import math
        now = mono()
        dt = max(0.0, now - self.last)
        self.last = now
        if dt > 0:
            a = math.exp(-dt / self.tau)
            self.x = a * self.x + self.sig * math.sqrt(max(0.0, 1 - a * a)) * self.rng.gauss(0, 1)
        base = 1.0 + self.amp * math.sin(2 * math.pi * (now - self.t0) / self.P)
        return max(0.05, base * math.exp(self.x))


def common_args(ap):
    ap.add_argument('--port', type=int, required=True, help='data-plane port')
    ap.add_argument('--ctl_port', type=int, default=0, help='control port (default port+1000)')
    ap.add_argument('--cfg', required=True, help='JSON file with the RIC section of the config')
    ap.add_argument('--runtime', required=True, help='runtime folder (state files)')
    return ap


def load_cfg(path):
    with open(path) as f:
        return json.load(f)


def keep_alive():
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        sys.exit(0)


# ----------------------------------------------------------------------------- E2 front end
class E2Link:
    """This xApp's E42 session with the FlexRIC nearRT-RIC: supervises one mild_e2gw process.

    The gateway prints JSON events on stdout (hello, ready, rc_action, sub, unsub, ind, ctrl_tx, ctrl,
    node_lost, error, bye) and FlexRIC's own library lines ("[xApp]: CONTROL ACK rx", ...). Every JSON event
    is passed to on_event(ev) in the reader thread; 'ctrl' events get ev['ack'] = 'ack' | 'fail' | '?' from
    the library line printed between ctrl_tx and ctrl. Commands go to its stdin (send()).

    When the gateway exits (gNB gone, RIC gone, FlexRIC assert on a timeout), it is restarted after a back-off
    (2, 4, 8 ... 30 s). rotate() restarts it on purpose (between segments): each FlexRIC xApp writes every
    indication into its own SQLite file, and a fresh process starts a fresh file (the old one is deleted).
    """

    def __init__(self, name, role, ric_cfg, runtime, on_event, styles='1,4'):
        self.name, self.role, self.styles = name, role, styles
        self.cfg = ric_cfg
        self.on_event = on_event
        self.dbdir = os.path.join(runtime, 'flexric_db', name)
        self.logf = os.path.join(runtime, f'e2gw_{name}.log')
        self.cmd = list(ric_cfg.get('_e2gw_cmd') or [])
        self.p = None
        self.wlock = threading.Lock()
        self.stopping = False
        self.st = {'connected': False, 'ready': None, 'node': None, 'rc': None, 'n_subs': 0, 'starts': 0,
                   'exits': {}, 'last_ind': None, 'n_ind': 0, 'last_exit': None, 'lib_tail': deque(maxlen=30),
                   'timeouts': 0, 'last_timeout': None, 'rc_params': None}
        self._ctrl_lib = None
        self._t_start = 0.0

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name=f'{self.name}-e2gw').start()
        return self

    def _clean_db(self):
        try:
            os.makedirs(self.dbdir, exist_ok=True)
            for fn in os.listdir(self.dbdir):
                try:
                    os.remove(os.path.join(self.dbdir, fn))
                except OSError:
                    pass
        except Exception:
            pass

    def _loop(self):
        import subprocess
        back = 2.0
        while not self.stopping:
            if not self.cmd:
                log(self.name, 'no E2 gateway command configured (ric._e2gw_cmd); E2 disabled')
                return
            self._clean_db()
            env = dict(os.environ, MILD_ROLE=self.role, MILD_STYLES=self.styles,
                       MILD_PERIOD_MS=str(int(self.cfg.get('kpm_period_ms', 1000))),
                       MILD_WAIT_S=str(int(self.cfg.get('e2_wait_s', 30))),
                       MILD_PLMN=str(self.cfg.get('_plmn_hex', '00F110')))
            cmd = self.cmd + ['-d', self.dbdir.rstrip('/') + '/']
            try:
                lf = open(self.logf, 'a')
                lf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} start {' '.join(cmd)} =====\n")
                lf.flush()
                self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          env=env, text=True, bufsize=1, cwd=os.path.dirname(cmd[0]) or None)
            except Exception as e:
                log(self.name, f'cannot start the E2 gateway ({e}); retrying in {back:.0f} s')
                time.sleep(back)
                back = min(30.0, back * 2)
                continue
            self.st['starts'] += 1
            self._t_start = mono()
            try:
                for line in self.p.stdout:
                    if not line.startswith('{'):
                        s = line.strip()
                        if s:
                            self.st['lib_tail'].append(s[:200])
                            lf.write(s + '\n')
                            if 'CONTROL ACK' in s:
                                self._ctrl_lib = 'ack'
                            elif 'CONTROL FAILURE' in s:
                                self._ctrl_lib = 'fail'
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    self._track(ev)
                    if ev.get('ev') not in ('ind',):
                        lf.write(line if line.endswith('\n') else line + '\n')
                        lf.flush()
                    try:
                        self.on_event(ev)
                    except Exception as e:
                        log(self.name, f'E2 event handler error: {e}')
            except Exception as e:
                log(self.name, f'E2 gateway reader error: {e}')
            rc = self.p.wait()
            lf.write(f"===== exit {rc} =====\n")
            lf.close()
            self.st['connected'] = False
            self.st['n_subs'] = 0
            self.st['exits'][str(rc)] = self.st['exits'].get(str(rc), 0) + 1
            self.st['last_exit'] = {'rc': rc, 't': mono(), 'tail': list(self.st['lib_tail'])[-5:]}
            try:
                self.on_event({'ev': 'gw_exit', 'rc': rc})
            except Exception:
                pass
            if self.stopping:
                break
            if mono() - self._t_start > 60:
                back = 2.0
            if rc == 5:
                log(self.name, f"the E2 node did not answer a {(self.st.get('last_timeout') or {}).get('what', 'request')} "
                               f'(FlexRIC session unusable, gateway exit 5); restarting it in {back:.0f} s')
            elif rc != 0 or not getattr(self, '_rotating', False):
                log(self.name, f'E2 gateway exited (code {rc}); restarting in {back:.0f} s')
                time.sleep(back)
                back = min(30.0, back * 2)
            self._rotating = False

    def _track(self, ev):
        e = ev.get('ev')
        if e == 'ready':
            self.st.update(connected=True, ready=mono(), node=ev.get('node'), rc=bool(ev.get('rc')))
        elif e in ('sub', 'unsub') and ev.get('ok'):
            self.st['n_subs'] = int(ev.get('n', self.st['n_subs']))
        elif e == 'ind':
            self.st['last_ind'] = mono()
            self.st['n_ind'] += 1
        elif e == 'ctrl_tx':
            self._ctrl_lib = None
        elif e == 'ctrl':
            # 'ack' only when FlexRIC printed "CONTROL ACK rx" for this control; an answer that never came is a timeout
            ev['ack'] = 'timeout' if ev.get('timeout') else (self._ctrl_lib or ('?' if ev.get('ok') else 'fail'))
        elif e == 'e2_timeout':
            self.st['timeouts'] += 1
            self.st['last_timeout'] = {'what': ev.get('what'), 'ms': ev.get('ms'), 't': mono()}
        elif e == 'rc_params':
            self.st['rc_params'] = {'source': ev.get('source'), 'ids': ev.get('ids')}
        elif e == 'node_lost':
            self.st['connected'] = False

    def send(self, cmd):
        with self.wlock:
            p = self.p
            if p is None or p.poll() is not None:
                return False
            try:
                p.stdin.write(cmd.rstrip('\n') + '\n')
                p.stdin.flush()
                return True
            except Exception:
                return False

    def rotate(self, timeout=8.0):
        """Fresh gateway (fresh E42 session and SQLite file). Returns when the old one has exited."""
        p = self.p
        if p is None or p.poll() is not None:
            return
        self._rotating = True
        self.send('quit')
        t0 = mono()
        while p.poll() is None and mono() - t0 < timeout:
            time.sleep(0.1)
        if p.poll() is None:
            p.kill()

    def stop(self):
        self.stopping = True
        p = self.p
        if p is not None and p.poll() is None:
            self.send('quit')
            t0 = mono()
            while p.poll() is None and mono() - t0 < 5:
                time.sleep(0.1)
            if p.poll() is None:
                p.kill()

    def pid(self):
        p = self.p
        return p.pid if p is not None and p.poll() is None else None

    def status(self):
        s = {k: v for k, v in self.st.items() if k != 'lib_tail'}
        now = mono()
        s['ind_age_s'] = (now - s['last_ind']) if s['last_ind'] else None
        lt = s.get('last_timeout')
        s['last_timeout_age_s'] = (now - lt['t']) if lt else None
        s['alive'] = self.pid() is not None
        s['up_s'] = (now - self._t_start) if s['alive'] else 0.0
        return s


def stop_on_signal(*links):
    """SIGTERM/SIGINT: stop the gateways cleanly (unsubscribe) before the process exits."""
    import signal as _s

    def h(*_):
        for l in links:
            try:
                l.stop()
            except Exception:
                pass
        os._exit(0)
    _s.signal(_s.SIGTERM, h)
    _s.signal(_s.SIGINT, h)
