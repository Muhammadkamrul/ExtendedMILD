#!/usr/bin/env python3
"""traffic.py -- downlink application traffic of the N UEs of the live cell.

Sends UDP datagrams from the data-network side (root namespace) to every UE's current address (read from
that UE's agent state file), so every byte crosses UPF -> GTP-U -> gNB -> ZMQ broker -> srsUE. The offered
rate of UE k is

    rate_k = capacity_mbps * ue_load_frac * share_k * D_k(t) * cell_load

  capacity_mbps   saturated DL throughput of the cell, measured at calibration
  ue_load_frac    mean application load of all UEs together as a fraction of capacity (default 0.30)
  share_k         UE k's share (ue_shares, e.g. 0.40 / 0.35 / 0.25): different rates also let calibration
                  tell the UEs apart in the gNB's per-UE KPM report
  D_k(t)          daily profile 1 + amp*sin(2*pi*t/day_s) times UE k's own Ornstein-Uhlenbeck noise
  cell_load       fault knob (radio congestion: a traffic surge of the cell's users)
  fixed_mbps      calibration only: a fixed TOTAL rate (split by share_k), or with only_ue = k all of it
                  to UE k (used to map the gNB's F1AP UE IDs to ue1..ueN)

Control: a small JSON file written by the driver, re-read every second (cell_load, capacity_mbps, ...).
A UE without an address is skipped. Pacing: 5-ms slots with fractional carry, 1200-byte payloads.
"""
from __future__ import annotations

import argparse
import os
import socket
import time

from mild_oran.util import load_json, write_json_fast
from mild_oran.xapps.common import Diurnal

mono = time.monotonic


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--ctl', required=True, help='control JSON written by the driver')
    ap.add_argument('--agent_states', required=True, help='comma-separated ue_agent state files, UE 1..N')
    ap.add_argument('--state', required=True, help='own state file')
    ap.add_argument('--port', type=int, default=5400)
    ap.add_argument('--t0', type=float, default=0.0)
    ap.add_argument('--targets', default='', help='mock RAN: comma-separated host:port per UE')
    ap.add_argument('--payload', type=int, default=1200)
    a = ap.parse_args()
    t0 = a.t0 or mono()
    states = a.agent_states.split(',')
    n = len(states)
    fixed_targets = [x.rsplit(':', 1) for x in a.targets.split(',')] if a.targets else None
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 22)
    defaults = {'cell_load': 1.0, 'capacity_mbps': 20.0, 'ue_load_frac': 0.3, 'amp': 0.5, 'day_s': 5760.0,
                'enabled': 1, 'ue_shares': [1.0 / n] * n}
    ctl = dict(defaults)
    prof = [None] * n
    dest = [None] * n
    rate = [0.0] * n
    carry = [0.0] * n
    sent_b = [0] * n
    errs = 0
    last_ctl = last_state = 0.0
    payload = os.urandom(a.payload)
    slot = 0.005
    nxt = mono()
    while True:
        now = mono()
        if now - last_ctl >= 1.0:
            last_ctl = now
            c = load_json(a.ctl)
            if isinstance(c, dict):
                ctl = dict(defaults)
                ctl.update(c)
            shares = list(ctl.get('ue_shares') or defaults['ue_shares'])[:n]
            shares += [shares[-1] if shares else 1.0 / n] * (n - len(shares))
            tot = sum(shares) or 1.0
            shares = [x / tot for x in shares]
            for k in range(n):
                if prof[k] is None or prof[k].P != float(ctl.get('day_s', 5760.0)):
                    prof[k] = Diurnal(t0, float(ctl.get('day_s', 5760.0)), float(ctl.get('amp', 0.5)), seed=41 + k)
                if fixed_targets:
                    dest[k] = (fixed_targets[k][0], int(fixed_targets[k][1]))
                else:
                    st = load_json(states[k]) or {}
                    ip = st.get('ue_ip')
                    dest[k] = (ip, a.port) if ip else None
                f = prof[k].factor()
                rate[k] = float(ctl['capacity_mbps']) * float(ctl['ue_load_frac']) * shares[k] * f * float(ctl['cell_load'])
                if ctl.get('fixed_mbps') is not None:
                    only = ctl.get('only_ue')
                    if only is None:
                        rate[k] = float(ctl['fixed_mbps']) * shares[k]
                    else:
                        rate[k] = float(ctl['fixed_mbps']) if int(only) == k + 1 else 0.0
                if not ctl.get('enabled', 1):
                    rate[k] = 0.0
        if now - last_state >= 1.0:
            last_state = now
            try:
                write_json_fast(a.state, {'t_mono': now, 'rate_mbps': [round(x, 3) for x in rate],
                                          'total_mbps': round(sum(rate), 3), 'dest': [d[0] if d else None for d in dest],
                                          'sent_bytes': sent_b, 'errors': errs, 'cell_load': ctl.get('cell_load'),
                                          'only_ue': ctl.get('only_ue')})
            except Exception:
                pass
        for k in range(n):
            if dest[k] and rate[k] > 0:
                carry[k] += rate[k] * 1e6 * slot / 8.0 / a.payload
                m = int(carry[k])
                carry[k] -= m
                for _ in range(m):
                    try:
                        s.sendto(payload, dest[k])
                        sent_b[k] += a.payload
                    except Exception:
                        errs += 1
                        break
            else:
                carry[k] = 0.0
        nxt += slot
        dt = nxt - mono()
        if dt > 0:
            time.sleep(dt)
        elif dt < -0.5:
            nxt = mono()                      # fell behind (CPU starvation): do not burst to catch up


if __name__ == '__main__':
    main()
