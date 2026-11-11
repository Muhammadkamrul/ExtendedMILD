"""util.py -- logging, atomic JSON state, port checks, process helpers."""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

import psutil

_LOG = None


def set_log(path):
    global _LOG
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _LOG = open(path, 'a', buffering=1)


def log(msg, level='INFO'):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}"
    print(line, flush=True)
    if _LOG:
        _LOG.write(line + '\n')


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(obj, f, indent=2, default=str)
    os.replace(tmp, path)


def port_free(port, host='0.0.0.0'):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def who_uses(port):
    out = []
    try:
        for c in psutil.net_connections(kind='inet'):
            if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN and c.pid:
                try:
                    p = psutil.Process(c.pid)
                    out.append((c.pid, ' '.join(p.cmdline())[:160]))
                except Exception:
                    out.append((c.pid, '?'))
    except Exception:
        pass
    return out


OUR_PATTERNS = ['mild_switch.py', 'ofctl_rest', 'ryu-manager', 'osken-manager', 'api_service.py',
                'analytics_service.py', 'telemetry_service.py', 'resource_exporter.py', 'netprobe_exporter.py',
                'loadgen.py', 'iot_producer.py', 'crosstraffic.py', 'mild_sdn2_prom', 'iperf3 -s']


def kill_leftovers(extra=()):
    me = os.getpid()
    killed = []
    for p in psutil.process_iter(['pid', 'cmdline']):
        try:
            cmd = ' '.join(p.info['cmdline'] or [])
            if p.info['pid'] != me and any(k in cmd for k in list(OUR_PATTERNS) + list(extra)):
                p.kill()
                killed.append(cmd[:100])
        except Exception:
            pass
    return killed


def free_port_or_alternative(port, name, allow_kill=True, span=50):
    """Return a usable port: the requested one (after killing our own leftovers) or the next free one."""
    if port_free(port):
        return port
    users = who_uses(port)
    if allow_kill and users and all(any(k in cmd for k in OUR_PATTERNS + ['prometheus']) for _, cmd in users):
        for pid, cmd in users:
            log(f'port {port} ({name}) held by our leftover pid {pid}: {cmd} -> killing')
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
        time.sleep(1.0)
        if port_free(port):
            return port
    for p in range(port + 1, port + span):
        if port_free(p):
            log(f'port {port} ({name}) busy ({users}); using {p} instead', 'WARN')
            return p
    raise RuntimeError(f'no free port near {port} for {name}')


def which(*names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return ''


def run(cmd, check=False, timeout=60):
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=timeout)


def wait_http(url, timeout_s=30, ok=(200,)):
    import requests
    t0, last = time.time(), None
    while time.time() - t0 < timeout_s:
        try:
            r = requests.get(url, timeout=2)
            if r.status_code in ok:
                return True
            last = r.status_code
        except Exception as exc:
            last = exc
        time.sleep(0.5)
    raise RuntimeError(f'timeout waiting for {url}: {last}')


def python_exe():
    return sys.executable
