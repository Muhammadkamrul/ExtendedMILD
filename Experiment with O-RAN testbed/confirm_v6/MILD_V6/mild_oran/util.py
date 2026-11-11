"""util.py -- logging, atomic JSON files, monotonic time, ports, users and processes.

Every time stamp that is used to compute an interval, a rate or a window is taken
from the MONOTONIC clock (time.monotonic()). WSL2 steps its wall clock back by
about 2 s roughly every 30 s (runbook B.4), so wall-clock times appear only as
human-readable labels in logs.
"""
from __future__ import annotations

import json
import os
import pwd
import signal
import socket
import subprocess
import sys
import time

_LOG = {'f': None, 'path': None}


def mono() -> float:
    return time.monotonic()


def set_log(path):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    _LOG['f'] = open(path, 'a', buffering=1)
    _LOG['path'] = path


def log(msg, level='INFO'):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}"
    print(line, flush=True)
    if _LOG['f']:
        try:
            _LOG['f'].write(line + '\n')
        except Exception:
            pass


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj, mode=None):
    d = os.path.dirname(path) or '.'
    os.makedirs(d, exist_ok=True)
    tmp = f'{path}.tmp{os.getpid()}'
    with open(tmp, 'w') as f:
        json.dump(obj, f, indent=2, default=_default)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def _default(o):
    if hasattr(o, 'item'):
        return o.item()
    if isinstance(o, set):
        return sorted(o)
    return str(o)


def write_json_fast(path, obj):
    """Atomic, compact write used by the agents several times per second."""
    tmp = f'{path}.tmp'
    with open(tmp, 'w') as f:
        json.dump(obj, f, separators=(',', ':'), default=_default)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


# ----------------------------------------------------------------------------- ports
def port_free(port, host='127.0.0.1', udp=False):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
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
        import psutil
        for c in psutil.net_connections(kind='inet'):
            if c.laddr and c.laddr.port == port and c.pid:
                try:
                    out.append((c.pid, ' '.join(psutil.Process(c.pid).cmdline())[:160]))
                except Exception:
                    out.append((c.pid, '?'))
    except Exception:
        pass
    return out


OUR_PATTERNS = ['mild_oran.xapps.', 'mild_oran.ran.', 'mild_oran/xapps/', 'mild_oran/ran/', 'mild_e2gw']
KEEP_PATTERNS = ['mild_oran.ran.fake_ran']        # the mock RAN belongs to MockRanCtl, not to the stack


def our_leftovers(keep_ran=True):
    found = []
    try:
        import psutil
        me = os.getpid()
        for p in psutil.process_iter(['pid', 'cmdline']):
            try:
                cmd = ' '.join(p.info['cmdline'] or [])
                if p.info['pid'] != me and any(k in cmd for k in OUR_PATTERNS) and \
                        not (keep_ran and any(k in cmd for k in KEEP_PATTERNS)):
                    found.append((p.info['pid'], cmd[:140]))
            except Exception:
                pass
    except Exception:
        pass
    return found


def kill_leftovers(keep_ran=True):
    killed = []
    for pid, cmd in our_leftovers(keep_ran):
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(cmd)
        except Exception:
            pass
    if killed:
        time.sleep(0.5)
    return killed


# ----------------------------------------------------------------------------- users
def invoking_user():
    """(uid, gid, name, home) of the human user, also when the pipeline runs under sudo."""
    name = os.environ.get('SUDO_USER')
    if name and os.geteuid() == 0:
        pw = pwd.getpwnam(name)
    else:
        pw = pwd.getpwuid(os.getuid())
    return pw.pw_uid, pw.pw_gid, pw.pw_name, pw.pw_dir


def is_root():
    return os.geteuid() == 0


def chown_tree(path):
    """Give files written by the root pipeline back to the user (best effort)."""
    if not is_root() or not os.path.exists(path):
        return
    uid, gid, _, _ = invoking_user()
    if uid == 0:
        return
    try:
        os.chown(path, uid, gid)
        for root, dirs, files in os.walk(path):
            for n in dirs + files:
                try:
                    os.chown(os.path.join(root, n), uid, gid, follow_symlinks=False)
                except Exception:
                    pass
    except Exception:
        pass


def run(cmd, timeout=20, check=False):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=check)


def dir_size_bytes(path):
    tot = 0
    try:
        for root, _, files in os.walk(path):
            for n in files:
                try:
                    tot += os.path.getsize(os.path.join(root, n))
                except OSError:
                    pass
    except Exception:
        pass
    return tot


def http_get_json(host, port, path, timeout=1.5):
    import http.client
    c = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        c.request('GET', path)
        r = c.getresponse()
        body = r.read()
        if r.status != 200:
            return None
        return json.loads(body)
    except Exception:
        return None
    finally:
        c.close()


def http_post(host, port, path, timeout=1.5, body=b''):
    import http.client
    c = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        c.request('POST', path, body=body, headers={'Content-Type': 'application/json'})
        r = c.getresponse()
        r.read()
        return r.status == 200
    except Exception:
        return False
    finally:
        c.close()


def python_exe():
    return sys.executable
