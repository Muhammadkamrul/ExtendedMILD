"""
kpi.py -- read the 8 base KPIs of the MILD dataset.

Backend 'prometheus' (normal mode): PromQL instant queries against the
Prometheus server that scrapes all exporters and services.
Backend 'direct' (mock mode, no Prometheus binary): scrapes the exporters'
/metrics pages directly and computes the same rates and quantiles locally.

KPI definitions (identical in both backends)
  cpu_pct          cloud_cpu_pct          (resource exporter, real utilisation vs. allocation)
  ram_pct          cloud_ram_pct          (resource exporter, real memory vs. limit)
  storage_pct      cloud_disk_pct         (resource exporter, used share of the storage volume)
  snet             min over edge sites of snet_site    (active probes)
  sri              min over edge sites of sri_site     (active probes)
  api_latency      p95 of /work latency over the last W s, in ms (histogram)
  analytics_tput   jobs completed per second over the last W s
  telemetry_queue  telemetry_queue gauge (records not yet processed or delivered)
A KPI that cannot be read is returned as None. It is never replaced by a healthy value.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

import requests

KPI_NAMES = ['cpu_pct', 'ram_pct', 'storage_pct', 'snet', 'sri', 'api_latency', 'analytics_tput', 'telemetry_queue']


class PromKPI:
    def __init__(self, base, window_s=8):
        self.base = base
        self.s = requests.Session()
        W = f'[{int(window_s)}s]'
        self.q = {
            'cpu_pct': 'cloud_cpu_pct', 'ram_pct': 'cloud_ram_pct', 'storage_pct': 'cloud_disk_pct',
            'snet': 'min(snet_site)', 'sri': 'min(sri_site)',
            'api_latency': f'1000 * histogram_quantile(0.95, sum(rate(api_request_latency_seconds_bucket{{path="/work"}}{W})) by (le))',
            'analytics_tput': f'sum(rate(analytics_jobs_out_total{W}))',
            'telemetry_queue': 'sum(telemetry_queue)',
        }

    def query(self, expr) -> Optional[float]:
        try:
            r = self.s.get(f'{self.base}/api/v1/query', params={'query': expr}, timeout=3)
            res = r.json().get('data', {}).get('result', [])
            if not res:
                return None
            v = float(res[0]['value'][1])
            return None if v != v or v in (float('inf'), float('-inf')) else v
        except Exception:
            return None

    def read(self) -> Dict[str, Optional[float]]:
        return {k: self.query(e) for k, e in self.q.items()}

    def targets_up(self) -> Dict[str, bool]:
        try:
            j = self.s.get(f'{self.base}/api/v1/targets', timeout=3).json()
            return {t['labels'].get('job', '?') + '@' + t['labels'].get('instance', '?'): t['health'] == 'up'
                    for t in j['data']['activeTargets']}
        except Exception:
            return {}


class DirectKPI:
    """Scrape /metrics pages directly (mock mode)."""

    def __init__(self, endpoints: Dict[str, str], window_s=8):
        from prometheus_client.parser import text_string_to_metric_families  # noqa
        self.ep = endpoints
        self.W = window_s
        self.hist: List = []         # (t, snapshot)
        self.s = requests.Session()

    def _scrape(self):
        from prometheus_client.parser import text_string_to_metric_families
        snap = {}
        for name, url in self.ep.items():
            try:
                txt = self.s.get(url, timeout=2).text
                for fam in text_string_to_metric_families(txt):
                    for smp in fam.samples:
                        key = (smp.name, tuple(sorted(smp.labels.items())))
                        snap[key] = smp.value
            except Exception:
                pass
        return snap

    @staticmethod
    def _sum(snap, name, **lab):
        vals = [v for (n, l), v in snap.items() if n == name and all(dict(l).get(k) == x for k, x in lab.items())]
        return sum(vals) if vals else None

    @staticmethod
    def _min(snap, name):
        vals = [v for (n, l), v in snap.items() if n == name]
        return min(vals) if vals else None

    def read(self):
        now = time.time()
        snap = self._scrape()
        self.hist.append((now, snap))
        self.hist = [(t, s) for t, s in self.hist if now - t <= self.W + 2]
        old_t, old = self.hist[0]
        dt = now - old_t
        out = {'cpu_pct': self._sum(snap, 'cloud_cpu_pct'), 'ram_pct': self._sum(snap, 'cloud_ram_pct'),
               'storage_pct': self._sum(snap, 'cloud_disk_pct'), 'snet': self._min(snap, 'snet_site'),
               'sri': self._min(snap, 'sri_site'), 'telemetry_queue': self._sum(snap, 'telemetry_queue')}
        if dt > 0.5:
            a1, a0 = self._sum(snap, 'analytics_jobs_out_total'), self._sum(old, 'analytics_jobs_out_total')
            out['analytics_tput'] = (a1 - a0) / dt if a1 is not None and a0 is not None else None
            les = sorted({float(dict(l)['le']) for (n, l) in snap if n == 'api_request_latency_seconds_bucket'
                          and dict(l).get('path') == '/work'})
            cnt = []
            for le in les:
                key = str(le) if le != float('inf') else '+Inf'
                c1 = self._sum(snap, 'api_request_latency_seconds_bucket', path='/work', le=key)
                c0 = self._sum(old, 'api_request_latency_seconds_bucket', path='/work', le=key)
                cnt.append((le, (c1 or 0) - (c0 or 0)))
            tot = cnt[-1][1] if cnt else 0
            out['api_latency'] = None
            if tot > 0:
                target = 0.95 * tot
                prev_le, prev_c = 0.0, 0.0
                for le, c in cnt:
                    if c >= target:
                        if le == float('inf'):
                            out['api_latency'] = 1000 * prev_le
                        else:
                            frac = (target - prev_c) / max(c - prev_c, 1e-9)
                            out['api_latency'] = 1000 * (prev_le + frac * (le - prev_le))
                        break
                    prev_le, prev_c = le, c
        else:
            out['analytics_tput'] = None
            out['api_latency'] = None
        return out

    def targets_up(self):
        return {k: True for k in self.ep}
