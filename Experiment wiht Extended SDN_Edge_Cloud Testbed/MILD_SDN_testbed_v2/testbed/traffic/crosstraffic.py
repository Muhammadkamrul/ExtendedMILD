#!/usr/bin/env python3
"""
Background cross-traffic over the WAN (iperf3 client), run in an edge host.

Sends TCP traffic to the iperf3 server in the cloud rack in back-to-back
--chunk-s second sessions. The rate follows the emulated day (base *
(1 + A sin)), and is multiplied by the value in the control file --ctl
(written by the experiment driver to create congestion episodes). Each
session result (achieved Mbps, retransmits) is appended to --log.
"""
import argparse
import json
import math
import subprocess
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--server', required=True)
    ap.add_argument('--port', type=int, default=5201)
    ap.add_argument('--base-mbps', type=float, default=8.0)
    ap.add_argument('--amp', type=float, default=0.5)
    ap.add_argument('--day-s', type=float, default=1440 * 4.0)
    ap.add_argument('--chunk-s', type=int, default=8)
    ap.add_argument('--ctl', required=True)
    ap.add_argument('--log', required=True)
    a = ap.parse_args()
    t0 = time.time()
    while True:
        try:
            mult = float(open(a.ctl).read().strip() or 1.0)
        except Exception:
            mult = 1.0
        rate = max(0.2, a.base_mbps * (1 + a.amp * math.sin(2 * math.pi * (time.time() - t0) / a.day_s)) * mult)
        cmd = ['iperf3', '-c', a.server, '-p', str(a.port), '-t', str(a.chunk_s), '-b', f'{rate:.2f}M', '-J']
        rec = {'ts': time.time(), 'target_mbps': round(rate, 2), 'ctl_mult': mult}
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=a.chunk_s + 10).stdout
            j = json.loads(out)
            s = j['end']['sum_sent']
            rec.update({'achieved_mbps': round(s['bits_per_second'] / 1e6, 2), 'retransmits': s.get('retransmits')})
        except Exception as exc:
            rec['error'] = str(exc)[:120]
            time.sleep(1)
        with open(a.log, 'a') as f:
            f.write(json.dumps(rec) + '\n')


if __name__ == '__main__':
    main()
