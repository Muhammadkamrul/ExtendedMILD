#!/usr/bin/env python3
"""fake_ran.py -- stand-in for gNB + N srsUEs + broker, for the mock-RAN test ONLY (no data for the paper).

A small cell model: every UE has a downlink FIFO buffer fed by the traffic sender (UDP to air_port + k - 1
for UE k) and drained by a proportional-fair scheduler with capacity_mbps, scaled by the radio speed
(0.85-0.95, like the ZMQ broker). The per-slice PRB quota written by the stand-in E2 gateway (RC control,
file --rc) caps a slice's share of the cell. Every 0.25 s the state file gets, per UE and for the cell:
throughput (Mbit/s), buffer (bytes), queueing delay, PRB use, and the F1AP UE ID of every UE (a fixed
permutation, so that the UE-identity calibration has something to find).

--outage_after_s X exits after X seconds (simulates a RAN crash; the collector must restart the RAN).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import socket
import threading
import time

mono = time.monotonic


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--state', required=True)
    ap.add_argument('--rc', required=True)
    ap.add_argument('--air_port', type=int, default=18802)
    ap.add_argument('--n_ues', type=int, default=3)
    ap.add_argument('--capacity_mbps', type=float, default=20.0)
    ap.add_argument('--buffer_bytes', type=int, default=3_000_000)
    ap.add_argument('--outage_after_s', type=float, default=0.0)
    a = ap.parse_args()
    n = a.n_ues
    rng = random.Random(7)
    perm = list(range(n))
    rng.shuffle(perm)                     # ue k -> F1AP ID perm[k-1]
    sds = [1, 2, 1, 2, 1][:n]
    lock = threading.Lock()
    buf = [0] * n
    rx_bytes = [0] * n
    drop = [0] * n

    def rx(k):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 22)
        s.bind(('127.0.0.1', a.air_port + k))
        b = bytearray(65536)
        while True:
            m = s.recv_into(b)
            with lock:
                if buf[k] + m <= a.buffer_bytes:
                    buf[k] += m
                    rx_bytes[k] += m
                else:
                    drop[k] += m

    for k in range(n):
        threading.Thread(target=rx, args=(k,), daemon=True).start()
    t_start = mono()
    last = mono()
    served = [0.0] * n
    speed = 0.9
    tick = 0
    ewma_rate = [0.0] * n
    while True:
        time.sleep(0.05)
        now = mono()
        dt = now - last
        last = now
        if a.outage_after_s and now - t_start > a.outage_after_s:
            print('fake RAN: simulated outage', flush=True)
            os._exit(3)
        speed = max(0.6, min(1.0, speed + rng.gauss(0, 0.005)))
        rc = {}
        try:
            with open(a.rc) as f:
                rc = json.load(f)
        except Exception:
            pass
        cap_bytes = a.capacity_mbps * 1e6 / 8 * dt * speed
        with lock:
            want = list(buf)
        # slice caps (max PRB ratio), then proportional share among UEs with data
        alloc = [0.0] * n
        left = cap_bytes
        act = [k for k in range(n) if want[k] > 0]
        for _ in range(3):
            if not act or left <= 1:
                break
            share = left / len(act)
            nxt = []
            for k in act:
                q = rc.get(str(sds[k]), {})
                cap_k = cap_bytes * float(q.get('max', 100)) / 100.0
                give = min(share, want[k] - alloc[k], max(0.0, cap_k - alloc[k]))
                alloc[k] += give
                left -= give
                if want[k] - alloc[k] > 1 and alloc[k] < cap_k - 1:
                    nxt.append(k)
            act = nxt
        with lock:
            for k in range(n):
                buf[k] = max(0, buf[k] - int(alloc[k]))
                served[k] = alloc[k]
            bsnap = list(buf)
        for k in range(n):
            ewma_rate[k] = 0.8 * ewma_rate[k] + 0.2 * (served[k] * 8 / max(dt, 1e-3) / 1e6)
        tick += 1
        if tick % 5 == 0:
            cell = sum(ewma_rate)
            prb = min(100.0, 100.0 * cell / max(1e-6, a.capacity_mbps * speed))
            ues = {}
            for k in range(n):
                qd = bsnap[k] * 8 / max(ewma_rate[k] * 1e6, 2e5)
                ues[f'ue{k + 1}'] = {'f1ap': perm[k], 'sd': sds[k], 'dl_mbps': round(ewma_rate[k], 4),
                                     'buf': bsnap[k], 'qd_s': round(min(qd, 5.0), 4), 'drop_bytes': drop[k]}
            st = {'t_mono': now, 'speed': round(speed, 3), 'cell': {'dl_mbps': round(cell, 4), 'prb_pct': round(prb, 2)},
                  'ues': ues, 'rc': rc}
            tmp = a.state + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(st, f)
            os.replace(tmp, a.state)


if __name__ == '__main__':
    main()
