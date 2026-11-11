"""netem.py -- user-plane impairment with Linux tc netem (the network part of the fault model).

Where the impairment is applied (both directions of every UE's user plane):
  downlink  egress of ogstun in the root namespace: packets from the data network
            (10.45.0.1) toward the UPF, i.e. the N6 side of the UPF, before GTP-U (all UEs);
  uplink    egress of tun_srsue inside every UE namespace ue1 .. ueN.
The knob delay_ms is the added ROUND-TRIP delay and loss_pct the round-trip loss;
they are split between the two directions with network.dl_share (default 0.5).

Never touched: the loopback interface (ZMQ radio samples, NGAP/SCTP, GTP-U), so the
RAN itself is never destabilised. Every change is idempotent (tc qdisc replace)
and clear() removes both qdiscs (also called on exit and by `mild-oran cleanup`).

Mock RAN: no tc; the values are written to a file that the mock app server reads.
"""
from __future__ import annotations

import os
import shutil
import subprocess

from . import util


def _ip():
    return shutil.which('ip') or ('/usr/sbin/ip' if os.path.exists('/usr/sbin/ip') else '/sbin/ip')


def _tc():
    return shutil.which('tc') or ('/usr/sbin/tc' if os.path.exists('/usr/sbin/tc') else '/sbin/tc')


class Netem:
    def __init__(self, cfg, mock_file=None):
        n = cfg['network']
        self.dl_dev, self.ul_dev = n['dl_dev'], n['ul_dev']
        self.share = float(n.get('dl_share', 0.5))
        self.limit = int(n.get('netem_limit', 20000))
        # v2.3.1 (fix from the user's run): an EMPTY _netns list means "the RAN is down, no UE namespace exists
        # yet" -> test ogstun only; the UE namespaces are tested once the RAN is up. Before, the empty list fell
        # back to ['ue1'] and preflight failed on a fresh state with "Cannot find device tun_srsue".
        ns = cfg['ran']['_netns'] if '_netns' in cfg['ran'] else (cfg['ran'].get('ue_netns') or ['ue1'])
        self.nss = [ns] if isinstance(ns, str) else list(ns)
        self.mock_file = mock_file
        self.last = None
        self.failures = 0

    def _run(self, args, ns=None):
        cmd = ([_ip(), 'netns', 'exec', ns] if ns else []) + [_tc()] + args
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return r.returncode == 0, (r.stderr or r.stdout).strip()
        except Exception as e:
            return False, str(e)

    def _apply(self, dev, ns, delay, loss):
        args = ['qdisc', 'replace', 'dev', dev, 'root', 'netem', 'limit', str(self.limit),
                'delay', f'{delay:.1f}ms', 'loss', f'{loss:.3f}%']
        return self._run(args, ns)

    def test(self):
        """Preflight: can netem be attached to both devices? Leaves nothing behind."""
        if self.mock_file:
            return True, 'mock (file-based impairment)'
        msgs = []
        ok_all = True
        for dev, ns in [(self.dl_dev, None)] + [(self.ul_dev, n) for n in self.nss]:
            ok, msg = self._apply(dev, ns, 0.0, 0.0)
            if not ok:
                ok_all = False
                msgs.append(f'{"ns " + ns + " " if ns else ""}{dev}: {msg}')
            self._run(['qdisc', 'del', 'dev', dev, 'root'], ns)
        return ok_all, '; '.join(msgs) or f'netem works on {self.dl_dev} and on {self.ul_dev} in {", ".join(self.nss)}'

    def set(self, delay_ms, loss_pct):
        key = (round(float(delay_ms), 1), round(float(loss_pct), 2))
        if key == self.last:
            return True
        if self.mock_file:
            util.write_json_fast(self.mock_file, {'delay_ms': key[0], 'loss_pct': key[1]})
            self.last = key
            return True
        if key == (0.0, 0.0):
            self.clear()
            self.last = key
            return True
        d, l = key
        ok1, m1 = self._apply(self.dl_dev, None, d * self.share, l * self.share)
        ok2, m2 = True, ''
        for n in self.nss:
            o, m = self._apply(self.ul_dev, n, d * (1 - self.share), l * (1 - self.share))
            ok2, m2 = ok2 and o, (m2 + ' ' + m) if not o else m2
        if ok1 and ok2:
            self.last = key
            return True
        self.failures += 1
        self.last = None
        util.log(f'netem update failed: {m1} {m2}', 'WARN')
        return False

    def clear(self):
        if self.mock_file:
            try:
                util.write_json_fast(self.mock_file, {'delay_ms': 0.0, 'loss_pct': 0.0})
            except Exception:
                pass
            self.last = (0.0, 0.0)
            return
        self._run(['qdisc', 'del', 'dev', self.dl_dev, 'root'])
        for n in self.nss:
            self._run(['qdisc', 'del', 'dev', self.ul_dev, 'root'], ns=n)
        self.last = (0.0, 0.0)

    def show(self):
        if self.mock_file:
            return util.load_json(self.mock_file, {})
        a = self._run(['qdisc', 'show', 'dev', self.dl_dev])[1]
        return {'dl': a, **{f'ul_{n}': self._run(['qdisc', 'show', 'dev', self.ul_dev], ns=n)[1] for n in self.nss}}
