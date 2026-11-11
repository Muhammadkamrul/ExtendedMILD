#!/usr/bin/env python3
"""
Resource exporter for the cloud services (runs in the root namespace).

Measures the real CPU, memory and disk utilisation of the three cloud
services. No formula and no assumed capacity is involved when cgroups are
available:

  mode 'cgroup' (preferred): every service runs in its own systemd scope with
      a CPU quota and a memory limit. Utilisation is read from the kernel:
        cpu_pct = 100 * sum(delta cpu.stat usage_usec) / (delta t * sum(quota cores))
        ram_pct = 100 * sum(memory.current) / sum(memory.max)
  mode 'psutil' (fallback): utilisation from the service processes, relative
      to the CPU cores they are pinned to; memory relative to the configured
      per-service memory budget (reported as 'psutil-budget' in the manifest).

  disk_pct = 100 * used / total of the dedicated, fixed-size storage volume
             (a loop-mounted ext4 file system) where the services keep their
             spool, results and audit logs.
"""
import argparse
import json
import os
import shutil
import time

import psutil
from prometheus_client import Gauge, start_http_server

CPU = Gauge('cloud_cpu_pct', 'CPU utilisation of the cloud services vs. their CPU allocation (%)')
RAM = Gauge('cloud_ram_pct', 'Memory utilisation of the cloud services vs. their memory limit (%)')
DISK = Gauge('cloud_disk_pct', 'Used share of the cloud storage volume (%)')
SVC_CPU = Gauge('service_cpu_pct', 'CPU utilisation of one service vs. its allocation (%)', ['service'])
SVC_RAM = Gauge('service_mem_bytes', 'Memory of one service (bytes)', ['service'])
MODE = Gauge('resource_exporter_mode', '1 = cgroup, 0 = psutil fallback')
HOST_CPU = Gauge('host_cpu_pct', 'Whole-host CPU utilisation (%) for diagnostics')


def read(path):
    with open(path) as f:
        return f.read().strip()


def cg_cpu_usage(cg):
    for line in read(os.path.join(cg, 'cpu.stat')).splitlines():
        k, v = line.split()
        if k == 'usage_usec':
            return int(v) / 1e6
    return 0.0


def cg_quota_cores(cg):
    q, p = read(os.path.join(cg, 'cpu.max')).split()
    return (int(q) / int(p)) if q != 'max' else psutil.cpu_count()


def cg_mem(cg):
    cur = int(read(os.path.join(cg, 'memory.current')))
    mx = read(os.path.join(cg, 'memory.max'))
    return cur, (int(mx) if mx != 'max' else psutil.virtual_memory().total)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=9100)
    ap.add_argument('--interval', type=float, default=1.0)
    ap.add_argument('--services', default='api,analytics,telemetry')
    ap.add_argument('--cgroups', default='', help='JSON {service: cgroup_dir}; empty = psutil mode')
    ap.add_argument('--run-dir', default='/tmp/mild_run', help='folder with <service>.pid files')
    ap.add_argument('--cores', default='', help='JSON {service: n_cores} for psutil mode')
    ap.add_argument('--mem-budget-mb', type=float, default=384.0, help='per-service budget in psutil mode')
    ap.add_argument('--vol', required=True)
    a = ap.parse_args()
    services = [x for x in a.services.split(',') if x]   # empty = disk-only mode
    cgs = json.loads(a.cgroups) if a.cgroups else {}
    cores = json.loads(a.cores) if a.cores else {}
    use_cg = bool(cgs) and bool(services) and all(os.path.exists(os.path.join(cgs[s], 'cpu.stat')) for s in services)
    MODE.set(1 if use_cg else 0)
    start_http_server(a.port)
    psutil.cpu_percent(None)
    prev_t, prev = time.time(), {}
    procs = {}
    while True:
        time.sleep(a.interval)
        now = time.time()
        dt = max(1e-3, now - prev_t)
        used_cores, alloc_cores, mem_cur, mem_max = 0.0, 0.0, 0.0, 0.0
        for s in services:
            try:
                if use_cg:
                    u = cg_cpu_usage(cgs[s])
                    qc = cg_quota_cores(cgs[s])
                    cur, mx = cg_mem(cgs[s])
                else:
                    if s not in procs:
                        pid = int(read(os.path.join(a.run_dir, f'{s}.pid')))
                        procs[s] = psutil.Process(pid)
                    p = procs[s]
                    t = p.cpu_times()
                    u = t.user + t.system
                    qc = float(cores.get(s, len(p.cpu_affinity()) if hasattr(p, 'cpu_affinity') else 1))
                    cur, mx = p.memory_info().rss, a.mem_budget_mb * 1024 * 1024
                du = max(0.0, u - prev.get(s, u))
                prev[s] = u
                used_cores += du / dt
                alloc_cores += qc
                mem_cur += cur
                mem_max += mx
                SVC_CPU.labels(s).set(100 * (du / dt) / max(qc, 1e-6))
                SVC_RAM.labels(s).set(cur)
            except Exception:
                procs.pop(s, None)
        if alloc_cores > 0:
            CPU.set(min(100.0, 100 * used_cores / alloc_cores))
        if mem_max > 0:
            RAM.set(min(100.0, 100 * mem_cur / mem_max))
        try:
            du = shutil.disk_usage(a.vol)
            DISK.set(100.0 * (du.total - du.free) / du.total)
        except Exception:
            pass
        HOST_CPU.set(psutil.cpu_percent(None))
        prev_t = now


if __name__ == '__main__':
    main()
