#!/usr/bin/env python3
"""ue_agent.py -- active probes and traffic sink that run INSIDE the UE's network namespace.

Started by the driver as root, one per UE, with `ip netns exec ue<k> <python> -m mild_oran.ran.ue_agent ...`,
so every probe crosses the real 5G user plane:
    UE k (tun_srsue, ns ue<k>) -> srsUE k -> ZMQ broker -> gNB -> GTP-U -> UPF -> ogstun (10.45.0.1)

Threads
  pinger     ICMP echo to the UPF/data-network address every ping_interval (raw socket,
             own monotonic send time per sequence number; mode 'udp' = UDP echo, used by
             the mock RAN without root)
  http       HTTP GET http://<target>:<http_port>/health every http_interval (2 s timeout),
             served by appserver.py on the data-network side (the UE's edge service)
  sink       UDP sink for the downlink traffic (traffic.py), counts the received rate
  ue_ip      the UE's current IPv4 address (re-read every 2 s; it can change after a re-attach)
  writer     publishes everything to a JSON state file every 0.25 s (atomic rename)

The driver computes snet and sri from this file (8-s windows, monotonic times).
"""
from __future__ import annotations

import argparse
import os
import socket
import struct
import threading
import time
import urllib.request
from collections import deque

mono = time.monotonic


def _csum(b: bytes) -> int:
    if len(b) % 2:
        b += b'\0'
    s = sum(struct.unpack('!%dH' % (len(b) // 2), b))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return (~s) & 0xFFFF


class Agent:
    def __init__(self, a):
        self.a = a
        self.lock = threading.Lock()
        self.sent = {}                       # seq -> t_send
        self.pings = deque(maxlen=400)       # [seq, t_send, rtt_ms|None]
        self.idx = {}
        self.http = deque(maxlen=80)         # [t_send, ok, rtt_ms]
        self.sink = {'bytes': 0, 'pkts': 0}
        self.sink_rate = deque(maxlen=20)
        self.ue_ip = None
        self.err = {}
        self.ident = os.getpid() & 0xFFFF

    # ------------------------------------------------------------------ ping (ICMP or UDP echo)
    def _record_send(self, seq, t):
        with self.lock:
            item = [seq, round(t, 4), None]
            self.pings.append(item)
            self.idx[seq] = item
            if len(self.idx) > 800:
                for k in list(self.idx.keys())[:400]:
                    self.idx.pop(k, None)

    def _record_reply(self, seq, t):
        with self.lock:
            item = self.idx.get(seq)
            if item is not None and item[2] is None:
                item[2] = round((t - item[1]) * 1000.0, 3)

    def _icmp(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
        except PermissionError as e:
            self.err['ping'] = f'raw ICMP socket not permitted ({e}); run as root'
            return
        s.settimeout(0.5)

        def rx():
            while True:
                try:
                    data, _ = s.recvfrom(2048)
                except socket.timeout:
                    continue
                except Exception:
                    time.sleep(0.1)
                    continue
                t = mono()
                ihl = (data[0] & 0x0F) * 4
                if len(data) < ihl + 8:
                    continue
                typ, _code, _cs, ident, seq = struct.unpack('!BBHHH', data[ihl:ihl + 8])
                if typ == 0 and ident == self.ident:
                    self._record_reply(seq, t)

        threading.Thread(target=rx, daemon=True).start()
        seq = 0
        nxt = mono()
        while True:
            seq = (seq + 1) & 0xFFFF
            payload = b'mild-oran-probe!' * 3
            hdr = struct.pack('!BBHHH', 8, 0, 0, self.ident, seq)
            pkt = struct.pack('!BBHHH', 8, 0, _csum(hdr + payload), self.ident, seq) + payload
            t = mono()
            try:
                s.sendto(pkt, (self.a.target, 0))
                self._record_send(seq, t)
            except Exception as e:                   # no route (UE detached): counts as lost
                self._record_send(seq, t)
                self.err['ping_send'] = str(e)
            nxt += self.a.ping_interval
            time.sleep(max(0.0, nxt - mono()))

    def _udp_echo(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)

        def rx():
            while True:
                try:
                    data, _ = s.recvfrom(256)
                    self._record_reply(int(data.split(b':')[1]), mono())
                except socket.timeout:
                    continue
                except Exception:
                    time.sleep(0.05)

        threading.Thread(target=rx, daemon=True).start()
        seq, nxt = 0, mono()
        while True:
            seq = (seq + 1) & 0xFFFF
            t = mono()
            try:
                s.sendto(f'echo:{seq}:{self.a.ue}'.encode(), (self.a.target, self.a.udp_echo_port))
            except Exception as e:
                self.err['ping_send'] = str(e)
            self._record_send(seq, t)
            nxt += self.a.ping_interval
            time.sleep(max(0.0, nxt - mono()))

    # ------------------------------------------------------------------ HTTP probe
    def _http(self):
        url = f'http://{self.a.target}:{self.a.http_port}/health'
        nxt = mono()
        while True:
            t = mono()
            ok, rtt = 0, None
            try:
                req = urllib.request.Request(url, headers={'X-UE': self.a.ue})
                with urllib.request.urlopen(req, timeout=self.a.http_timeout) as r:
                    r.read()
                    ok = 1 if r.status == 200 else 0
                rtt = (mono() - t) * 1000.0
            except Exception as e:
                self.err['http'] = str(e)[:80]
            with self.lock:
                self.http.append([round(t, 4), ok, None if rtt is None else round(rtt, 2)])
            nxt += self.a.http_interval
            time.sleep(max(0.0, nxt - mono()))

    # ------------------------------------------------------------------ sink and address
    def _sink(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 22)
        s.bind(('0.0.0.0', self.a.sink_port))
        buf = bytearray(65536)
        while True:
            n = s.recv_into(buf)
            with self.lock:
                self.sink['bytes'] += n
                self.sink['pkts'] += 1

    def _ue_ip(self):
        while True:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect((self.a.target, 9))
                ip = s.getsockname()[0]
                s.close()
                self.ue_ip = None if ip.startswith('0.') else ip
            except Exception:
                self.ue_ip = None
            time.sleep(2.0)

    def _writer(self):
        from mild_oran.util import write_json_fast
        last_b, last_t = 0, mono()
        k = 0
        while True:
            time.sleep(0.25)
            now = mono()
            with self.lock:
                pings = [list(x) for x in self.pings]
                http = [list(x) for x in self.http]
                b = self.sink['bytes']
                pk = self.sink['pkts']
            k += 1
            if k % 4 == 0:
                self.sink_rate.append((b - last_b) * 8 / max(1e-6, now - last_t) / 1e6)
                last_b, last_t = b, now
            try:
                write_json_fast(self.a.state, {'t_mono': now, 'pid': os.getpid(), 'ue': self.a.ue, 'ue_ip': self.ue_ip,
                                               'pings': pings, 'http': http, 'ping_interval': self.a.ping_interval,
                                               'sink_bytes': b, 'sink_pkts': pk,
                                               'sink_mbps': self.sink_rate[-1] if self.sink_rate else 0.0,
                                               'err': self.err})
            except Exception:
                pass

    def start(self):
        ping = self._icmp if self.a.mode == 'icmp' else self._udp_echo
        for fn in (ping, self._http, self._sink, self._ue_ip, self._writer):
            threading.Thread(target=fn, daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--state', required=True)
    ap.add_argument('--target', default='10.45.0.1')
    ap.add_argument('--http_port', type=int, default=18080)
    ap.add_argument('--http_timeout', type=float, default=2.0)
    ap.add_argument('--sink_port', type=int, default=5400)
    ap.add_argument('--ping_interval', type=float, default=0.2)
    ap.add_argument('--http_interval', type=float, default=1.0)
    ap.add_argument('--mode', choices=['icmp', 'udp'], default='icmp')
    ap.add_argument('--udp_echo_port', type=int, default=18081)
    ap.add_argument('--ue', default='ue1', help='name of this UE (ue1..ueN)')
    a = ap.parse_args()
    Agent(a).start()
    while True:
        time.sleep(3600)


if __name__ == '__main__':
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    main()
