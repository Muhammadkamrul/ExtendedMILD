#!/usr/bin/env python3
"""appserver.py -- the UE's edge service on the data-network side (target of the sri HTTP probes).

Real RAN:  binds the UPF tunnel address 10.45.0.1 (ogstun) on port 18080 and answers
           GET /health. Every probe from the UE crosses the radio and the core.
Mock RAN:  (--mock_state) also runs a UDP echo for the ping probes, and delays or drops
           answers according to the UE's queueing delay in the stand-in cell (fake_ran.py) and
           the emulated impairment, so the probe-based scores react as they would over the radio.
"""
from __future__ import annotations

import argparse
import json
import random
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mild_oran.util import load_json

mono = time.monotonic


class Mock:
    """Mock RAN: delay = radio floor + the UE's queueing delay in the stand-in cell + emulated netem."""

    def __init__(self, ran_state, netem_file):
        self.ran_state, self.netem_file = ran_state, netem_file
        self.v = {'qd': {}, 'delay': 0.0, 'loss': 0.0}
        self.rng = random.Random(3)
        threading.Thread(target=self._poll, daemon=True).start()

    def _poll(self):
        while True:
            g = load_json(self.ran_state) or {}
            n = load_json(self.netem_file) or {}
            self.v = {'qd': {k: float(u.get('qd_s', 0.0)) for k, u in (g.get('ues') or {}).items()},
                      'delay': float(n.get('delay_ms', 0.0)) / 1000.0, 'loss': float(n.get('loss_pct', 0.0)) / 100.0,
                      'alive': bool(g)}
            time.sleep(0.2)

    def delay(self, ue=None):
        j = abs(self.rng.gauss(0, 0.004))
        qd = self.v['qd'].get(ue, max(self.v['qd'].values() or [0.0]))
        return 0.025 + j + min(qd, 2.0) + self.v['delay']

    def lost(self):
        return (not self.v.get('alive', True)) or self.rng.random() < min(0.95, self.v['loss'])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--bind', default='10.45.0.1')
    ap.add_argument('--port', type=int, default=18080)
    ap.add_argument('--mock_state', default='')
    ap.add_argument('--mock_netem', default='')
    ap.add_argument('--udp_echo_port', type=int, default=18081)
    a = ap.parse_args()
    mock = Mock(a.mock_state, a.mock_netem) if a.mock_state else None

    class H(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'

        def log_message(self, *x):
            pass

        def do_GET(self):
            if mock:
                if mock.lost():
                    time.sleep(3.0)
                    return
                time.sleep(mock.delay(self.headers.get('X-UE')))
            b = json.dumps({'ok': 1, 'svc': 'edge-app'}).encode()
            self.send_response(200 if self.path.startswith('/health') else 404)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    h = ThreadingHTTPServer((a.bind, a.port), H)
    h.daemon_threads = True
    threading.Thread(target=h.serve_forever, daemon=True).start()
    print(f'appserver: http://{a.bind}:{a.port}/health' + (' (mock)' if mock else ''), flush=True)
    if mock:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((a.bind, a.udp_echo_port))

        def reply(data, addr, d):
            time.sleep(d)
            try:
                s.sendto(data, addr)
            except Exception:
                pass

        while True:
            data, addr = s.recvfrom(256)
            if mock.lost():
                continue
            parts = data.split(b':')
            ue = parts[2].decode(errors='ignore') if len(parts) > 2 else None
            threading.Thread(target=reply, args=(data, addr, mock.delay(ue)), daemon=True).start()
    while True:
        time.sleep(3600)


if __name__ == '__main__':
    main()
