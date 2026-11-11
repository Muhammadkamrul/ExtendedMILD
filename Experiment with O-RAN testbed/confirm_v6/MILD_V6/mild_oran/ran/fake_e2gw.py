#!/usr/bin/env python3
"""fake_e2gw.py -- stand-in for mild_e2gw (the FlexRIC xApp front end), for the mock-RAN test ONLY.

Speaks exactly the same stdin/stdout protocol as mild_e2gw.c (JSON events on stdout, FlexRIC-like library
lines, the commands sub / unsub / ctrl / nodes / quit on stdin) and turns the stand-in RAN's state file into
E2SM-KPM indications (format 1 cell, format 3 one block per UE, srsRAN metric names and units: kbit/s,
PRB, %, 0.1 ms). RC controls are written to the stand-in RAN's quota file and acknowledged after a short
delay, preceded by "[xApp]: CONTROL ACK rx" as FlexRIC prints it.
Accepts and ignores FlexRIC's flags (-c -p -d -n).
"""
from __future__ import annotations

import json
import os
import random
import sys
import threading
import time

mono = time.monotonic
lock = threading.Lock()


def out(obj):
    with lock:
        sys.stdout.write((obj if isinstance(obj, str) else json.dumps(obj, separators=(',', ':'))) + '\n')
        sys.stdout.flush()


def load(p):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def main():
    role = os.environ.get('MILD_ROLE', 'kpm')
    styles = os.environ.get('MILD_STYLES', '1,4')
    period = float(os.environ.get('MILD_PERIOD_MS', '1000')) / 1000.0
    state = os.environ.get('FAKE_RAN_STATE', '')
    rcf = os.environ.get('FAKE_RAN_RC', '')
    rng = random.Random(os.getpid())
    out({'ev': 'hello', 'role': role, 'pid': os.getpid(), 'styles': styles, 'period_ms': int(period * 1000)})
    out('[xAapp]: Initializing ...')
    out('[xApp]: E42 SETUP-REQUEST tx')
    t0 = mono()
    while not load(state):
        if mono() - t0 > 30:
            out({'ev': 'error', 'what': 'no E2 node with RAN function 2 after 30 s'})
            sys.exit(2)
        time.sleep(0.5)
    out('[xApp]: E42 SETUP-RESPONSE rx')
    node = '001-01/ngran_gNB_DU/nb411/du0'
    rcm = load(os.path.join(os.path.dirname(state), 'fake_rc_mode.json')) or {'offered': True, 'hang': False}
    if rcm.get('offered', True):
        out({'ev': 'rc_action', 'style': 2, 'action': 6, 'name': 'Slice-level PRB quota'})
        out({'ev': 'rc_params', 'source': 'gnb', 'advertised': 12, 'matched': 12,
             'ids': {'list': 1, 'group': 2, 'policy': 3, 'member_list': 5, 'member': 6, 'plmn': 7, 'snssai': 8,
                     'sst': 9, 'sd': 10, 'min': 11, 'max': 12, 'ded': 13}})
    out({'ev': 'ready', 'role': role, 'node': node, 'kpm': 1, 'rc': 1 if rcm.get('offered', True) else 0})
    subs = []
    if role == 'kpm':
        for s in (1, 4):
            if str(s) in styles:
                subs.append(s)
                out({'ev': 'sub', 'ok': 1, 'style': s, 'metrics': 15 if s == 1 else 14, 'n': len(subs), 'ms': 30.0})
    stop = threading.Event()

    def indications():
        nxt = mono()
        while not stop.is_set():
            st = load(state) or {}
            sp = float(st.get('speed', 0.9) or 0.9)
            nxt += period / max(0.3, sp)
            time.sleep(max(0.0, nxt - mono()))
            if not st or mono() - float(st.get('t_mono', 0)) > 5:
                out({'ev': 'node_lost', 'node': node})
                os._exit(4)
            tc = time.time()
            c = st.get('cell', {})
            for s in list(subs):
                if s == 1:
                    m = {'DRB.UEThpDl': round(c.get('dl_mbps', 0) * 1000, 1), 'DRB.UEThpUl': 12,
                         'RRU.PrbTotDl': round(c.get('prb_pct', 0)), 'RRU.PrbUsedDl': round(c.get('prb_pct', 0) * 0.52, 1),
                         'RRU.PrbAvailDl': round(52 - c.get('prb_pct', 0) * 0.52, 1),
                         'DRB.RlcSduDelayDl': round(10 * 1000 * max(u.get('qd_s', 0) for u in st.get('ues', {}).values() or [{}]) + rng.uniform(15, 30), 1),
                         'DRB.RlcPacketDropRateDl': 0, 'RACH.PreambleDedCell': None}
                    out({'ev': 'ind', 'fmt': 1, 't_mono': mono(), 't_wall': time.time(), 'tc': tc, 'm': m, 'incomplete': 0})
                else:
                    ues = []
                    for name, u in sorted(st.get('ues', {}).items(), key=lambda x: x[1]['f1ap']):
                        m = {'DRB.UEThpDl': round(u.get('dl_mbps', 0) * 1000, 1), 'DRB.UEThpUl': 4,
                             'RRU.PrbTotDl': round(c.get('prb_pct', 0)), 'RRU.PrbUsedDl': round(c.get('prb_pct', 0) * 0.52, 1),
                             'DRB.RlcSduDelayDl': round(10 * 1000 * u.get('qd_s', 0) + rng.uniform(15, 30), 1),
                             'DRB.RlcPacketDropRateDl': 0, 'DRB.AirIfDelayUl': None}
                        ues.append({'id': f"du_f1ap:{u['f1ap']}", 'f1ap': u['f1ap'], 'm': m})
                    out({'ev': 'ind', 'fmt': 3, 't_mono': mono(), 't_wall': time.time(), 'tc': tc, 'ues': ues,
                         'incomplete': 0})

    if role == 'kpm':
        threading.Thread(target=indications, daemon=True).start()
    else:
        def watch():
            while not stop.is_set():
                time.sleep(3)
                st = load(state)
                if not st or mono() - float(st.get('t_mono', 0)) > 5:
                    out({'ev': 'node_lost', 'node': node})
                    os._exit(4)
        threading.Thread(target=watch, daemon=True).start()
    for line in sys.stdin:
        w = line.split()
        if not w:
            continue
        if w[0] == 'sub' and role == 'kpm':
            time.sleep(0.03)
            subs.append(int(w[1]))
            out({'ev': 'sub', 'ok': 1, 'style': int(w[1]), 'metrics': 14, 'n': len(subs), 'ms': 30.0})
        elif w[0] == 'unsub':
            if len(subs) > 2:
                s = subs.pop()
                out({'ev': 'unsub', 'ok': 1, 'style': s, 'n': len(subs), 'ms': 20.0})
            else:
                out({'ev': 'unsub', 'ok': 0, 'n': len(subs)})
        elif w[0] == 'ctrl' and len(w) == 8:
            sid, f1ap, sst, sd, mn, mx, ded = (int(x) for x in w[1:])
            out({'ev': 'ctrl_tx', 'id': sid})
            t = mono()
            if not rcm.get('offered', True):
                out({'ev': 'ctrl', 'id': sid, 'ok': 0, 'why': 'node offers no E2SM-RC'})
                continue
            if rcm.get('hang'):                        # the stand-in gNB answers nothing (as the real one did)
                time.sleep(6.0)
                out('[xApp]: Successfully received CONTROL response')
                ms = round((mono() - t) * 1000, 2)
                out({'ev': 'ctrl', 'id': sid, 'ok': 0, 'timeout': 1, 'ms': ms, 'f1ap': f1ap, 'min': mn, 'max': mx})
                out({'ev': 'e2_timeout', 'what': 'control', 'ms': ms, 'node': node})
                os._exit(5)
            time.sleep(rng.uniform(0.005, 0.03))
            q = load(rcf) or {}
            q[str(sd)] = {'min': mn, 'max': mx, 't': time.time()}
            tmp = rcf + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(q, f)
            os.replace(tmp, rcf)
            out('[xApp]: CONTROL ACK rx')
            out('[xApp]: Successfully received CONTROL response')
            out({'ev': 'ctrl', 'id': sid, 'ok': 1, 'ms': round((mono() - t) * 1000, 2), 'f1ap': f1ap, 'min': mn, 'max': mx})
        elif w[0] == 'nodes':
            out({'ev': 'nodes', 'n': 1, 'nodes': [{'nb': 411, 'rf': [2, 3]}]})
        elif w[0] == 'quit':
            break
    stop.set()
    out({'ev': 'bye'})


if __name__ == '__main__':
    main()
