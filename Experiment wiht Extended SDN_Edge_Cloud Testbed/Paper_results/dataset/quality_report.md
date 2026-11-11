# Dataset quality report

rows: 29765  segments: 34
events per intent (labelled, observed): {'analytics': 231, 'api': 283, 'telemetry': 345}
co-drift pairs: 212
unobserved injected episodes (not labelled): 10

## Checks

- enough_ticks: FAIL
- enough_events_per_intent: FAIL
- enough_codrift_pairs: PASS
- observed_fraction_ok: PASS
- healthy_slo_exceedance_low: PASS
- buildup_ok: PASS
- kpis_vary: PASS

**valid = True, sufficient = False**

## Injected vs observed failures by type

- conflicting_analytics_cause: injected 98, observed 98 (100.0 %)
- conflicting_api_cause: injected 123, observed 123 (100.0 %)
- conflicting_victim: injected 221, observed 212 (95.9 %)
- independent_analytics_slow: injected 68, observed 68 (100.0 %)
- independent_api_latency: injected 62, observed 61 (98.4 %)
- independent_telemetry_slow: injected 67, observed 67 (100.0 %)
- nonlinear_analytics_xor: injected 65, observed 65 (100.0 %)
- nonlinear_api_xor: injected 99, observed 99 (100.0 %)
- nonlinear_telemetry_xor: injected 66, observed 66 (100.0 %)

SLO exceedance outside event windows: {'api': 0.013672251107259772, 'analytics': 6.418897233455292e-05, 'telemetry': 0.0}
build-up quantiles (ticks): {0.1: 8.0, 0.5: 12.0, 0.9: 16.0}
merge statistics: {'interpolated_values': 104, 'dropped_rows': 77, 'dropped_events_gap': 25, 'excluded_rows_jam': 0, 'dropped_events_jam': 0, 'segments_excluded': 0, 'dropped_rows_event_windows': 293}