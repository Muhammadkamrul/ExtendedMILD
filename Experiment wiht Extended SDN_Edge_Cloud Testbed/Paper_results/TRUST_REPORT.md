# Trust report for the SDN results

**Verdict: USABLE WITH CAVEATS**

- PAPER-READY: report the numbers as the main SDN result.
- USABLE WITH CAVEATS: report them, and state the caveats below in the paper (or collect more data to remove them).
- NOT TRUSTWORTHY: do not put these numbers in the paper. Fix the FAIL items first.

| criterion | value | status | rule |
|---|---|---|---|
| T1 data suitability | SUITABLE | PASS | SUITABLE = PASS, MARGINAL = WARN |
| T2 test-set size | test events per intent {'api': 86, 'analytics': 69, 'telemetry': 104}, test days 6.20 | PASS | PASS: >= 30 events per intent and >= 3 days |
| T3 precision (95 % CI half-width of MILD Det and DA) | Det 77.2 % +- 5.1 pp (n=259), DA 77.7 % +- 7.3 pp (n=124) | PASS | PASS <= 10 pp, WARN <= 15 pp |
| T4 seed stability (std of MILD Det/DA over seeds, at equal FP) | 4.13 pp at equal FP; 8.12 pp with validation-tuned thresholds (5 seeds) | PASS | PASS <= 5 pp, WARN <= 10 pp (equal-FP spread) |
| T5 generalisation (dev validation vs held-out test, MILD) | Det 88.7 -> 77.2, DA 79.7 -> 77.7 | WARN | PASS gap <= 10 pp, WARN <= 20 pp |
| T6 false alarms of MILD on the test set | 4.18 FP steps/day (budget 3.0) | PASS | PASS <= 2 x budget, WARN <= 4 x budget |
| T7 no blind intent (MILD per-intent detection, min) | {'analytics': 57.4, 'api': 75.1, 'telemetry': 99.0} | PASS | PASS >= 20 %, WARN >= 10 % |
| T8 baselines are functional | collapsed (Det < 5 %): none | PASS | PASS if no baseline collapsed |
| T9 significance | 15 supported claims, 0 untestable comparisons, 0 where a baseline is better | PASS | PASS: at least one supported claim and nothing untestable |
| T10 comparison at equal false-alarm rate | 13 supported claims, 0 where a baseline is better | PASS | PASS if no baseline is significantly better at equal FP |

## Why each criterion matters

- **T1 data suitability**: Results cannot be better than the data (see SUITABILITY_REPORT.md).
- **T2 test-set size**: Small test sets give wide intervals and untestable FP comparisons.
- **T3 precision (95 % CI half-width of MILD Det and DA)**: Differences smaller than the interval cannot be claimed.
- **T4 seed stability (std of MILD Det/DA over seeds, at equal FP)**: The equal-FP spread measures how stable the learned risk ranking is. The spread with validation-tuned thresholds also contains threshold calibration noise, which T6 covers.
- **T5 generalisation (dev validation vs held-out test, MILD)**: A large drop means the tuning over-fitted the development period or the test period differs.
- **T6 false alarms of MILD on the test set**: The FP budget is tuned on development data. Much higher test FP means poor calibration.
- **T7 no blind intent (MILD per-intent detection, min)**: An intent that is almost never detected makes the macro averages misleading.
- **T8 baselines are functional**: Beating a collapsed baseline proves nothing. Check its job.log.
- **T9 significance**: Only differences with Holm-corrected p < 0.05 may be written as "MILD outperforms".
- **T10 comparison at equal false-alarm rate**: Removes the effect of different alarm rates (see training/opcurve/OPCURVE_REPORT.md).

## Claims supported by the paired tests (write these)

- detection_% vs MLP: MILD 80.00, baseline 65.95, diff 14.05 [8.19, 19.69], Holm p=9.84e-08 -> MILD significantly better
- disambiguation_% vs MLP: MILD 77.74, baseline 36.13, diff 41.61 [32.42, 50.81], Holm p=8.53e-14 -> MILD significantly better
- lead_time_min (common detections) vs MLP: MILD 6.41, baseline 5.41, diff 0.99 [0.70, 1.29], Holm p=1.02e-07 -> MILD significantly better
- detection_% vs LSTM: MILD 80.00, baseline 56.37, diff 23.63 [17.22, 30.04], Holm p=8.62e-14 -> MILD significantly better
- disambiguation_% vs LSTM: MILD 77.74, baseline 31.94, diff 45.81 [35.81, 55.81], Holm p=1.2e-16 -> MILD significantly better
- lead_time_min (common detections) vs LSTM: MILD 6.55, baseline 5.37, diff 1.19 [0.93, 1.45], Holm p=1.15e-14 -> MILD significantly better
- detection_% vs WKPI-Tuned: MILD 80.00, baseline 33.20, diff 46.80 [40.62, 52.82], Holm p=2.94e-38 -> MILD significantly better
- disambiguation_% vs WKPI-Tuned: MILD 77.74, baseline 0.00, diff 77.74 [70.00, 84.84], Holm p=2.52e-28 -> MILD significantly better
- lead_time_min (common detections) vs WKPI-Tuned: MILD 7.19, baseline 2.31, diff 4.87 [4.41, 5.34], Holm p=3.24e-15 -> MILD significantly better
- detection_% vs Dist-Target: MILD 80.00, baseline 21.62, diff 58.38 [51.04, 65.48], Holm p=6.54e-35 -> MILD significantly better
- disambiguation_% vs Dist-Target: MILD 77.74, baseline 20.97, diff 56.77 [48.06, 65.48], Holm p=9.49e-19 -> MILD significantly better
- lead_time_min (common detections) vs Dist-Target: MILD 4.86, baseline 1.82, diff 3.03 [2.30, 3.87], Holm p=1.02e-07 -> MILD significantly better
- detection_% vs LR-OvR: MILD 80.00, baseline 50.58, diff 29.42 [22.39, 36.37], Holm p=5.93e-15 -> MILD significantly better
- disambiguation_% vs LR-OvR: MILD 77.74, baseline 26.61, diff 51.13 [39.19, 62.74], Holm p=2.99e-12 -> MILD significantly better
- lead_time_min (common detections) vs LR-OvR: MILD 6.09, baseline 4.23, diff 1.86 [1.47, 2.25], Holm p=8.87e-13 -> MILD significantly better

## Comparisons that are NOT significant or favour a baseline (write "comparable" or report honestly)

- fp_steps_per_day vs MLP: MILD 4.14, baseline 0.54, diff 3.60 [1.36, 6.25], Holm p=0.013 -> no significant difference
- fp_steps_per_day vs LSTM: MILD 4.14, baseline 2.78, diff 1.36 [-2.34, 4.55], Holm p=0.477 -> no significant difference
- fp_steps_per_day vs WKPI-Tuned: MILD 4.14, baseline 3.47, diff 0.66 [-2.72, 4.04], Holm p=0.944 -> no significant difference
- fp_steps_per_day vs Dist-Target: MILD 4.14, baseline 4.89, diff -0.76 [-4.39, 2.56], Holm p=0.944 -> no significant difference
- fp_steps_per_day vs LR-OvR: MILD 4.14, baseline 2.53, diff 1.61 [-0.88, 3.98], Holm p=0.262 -> no significant difference

## Comparison at equal false-alarm rate (supported claims)

- disambiguation_% vs MLP at equal FP: MILD 76.29, baseline 55.32, diff 20.97, median Holm p=1.85e-05, seeds MILD/baseline better 5/0 -> MILD significantly better
- detection_% vs LSTM at equal FP: MILD 82.02, baseline 63.72, diff 18.29, median Holm p=3.88e-07, seeds MILD/baseline better 4/0 -> MILD significantly better
- disambiguation_% vs LSTM at equal FP: MILD 76.10, baseline 43.90, diff 32.20, median Holm p=1.85e-05, seeds MILD/baseline better 5/0 -> MILD significantly better
- lead_time_min (common detections) vs LSTM at equal FP: MILD 6.38, baseline 5.53, diff 0.84, median Holm p=6.44e-11, seeds MILD/baseline better 4/0 -> MILD significantly better
- detection_% vs WKPI-Tuned at equal FP: MILD 82.08, baseline 38.61, diff 43.47, median Holm p=2.59e-31, seeds MILD/baseline better 5/0 -> MILD significantly better
- disambiguation_% vs WKPI-Tuned at equal FP: MILD 76.29, baseline 0.00, diff 76.29, median Holm p=2.02e-27, seeds MILD/baseline better 5/0 -> MILD significantly better
- lead_time_min (common detections) vs WKPI-Tuned at equal FP: MILD 7.03, baseline 1.40, diff 5.63, median Holm p=3.35e-17, seeds MILD/baseline better 5/0 -> MILD significantly better
- detection_% vs Dist-Target at equal FP: MILD 82.08, baseline 20.85, diff 61.24, median Holm p=2.83e-38, seeds MILD/baseline better 5/0 -> MILD significantly better
- disambiguation_% vs Dist-Target at equal FP: MILD 76.29, baseline 20.16, diff 56.13, median Holm p=5.42e-20, seeds MILD/baseline better 5/0 -> MILD significantly better
- lead_time_min (common detections) vs Dist-Target at equal FP: MILD 5.11, baseline 1.68, diff 3.43, median Holm p=6.3e-09, seeds MILD/baseline better 5/0 -> MILD significantly better
- detection_% vs LR-OvR at equal FP: MILD 82.08, baseline 54.44, diff 27.64, median Holm p=2.08e-13, seeds MILD/baseline better 5/0 -> MILD significantly better
- disambiguation_% vs LR-OvR at equal FP: MILD 76.29, baseline 21.77, diff 54.52, median Holm p=2.91e-13, seeds MILD/baseline better 5/0 -> MILD significantly better
- lead_time_min (common detections) vs LR-OvR at equal FP: MILD 6.19, baseline 4.32, diff 1.87, median Holm p=5.13e-15, seeds MILD/baseline better 5/0 -> MILD significantly better

## Comparison at equal false-alarm rate (not significant or favouring a baseline)

- detection_% vs MLP at equal FP: MILD 82.08, baseline 81.16, diff 0.93, median Holm p=0.644, seeds MILD/baseline better 1/0 -> no significant difference
- lead_time_min (common detections) vs MLP at equal FP: MILD 6.00, baseline 5.77, diff 0.23, median Holm p=0.193, seeds MILD/baseline better 2/0 -> no significant difference

## Caveats and how to remove them

- T5 generalisation (dev validation vs held-out test, MILD) (WARN): more data or fewer trials; check drift in SUITABILITY_REPORT.md

Files: /home/windows/mild_sdn2_train_v3_30k/training/AUTOTRAIN_REPORT.md, final_test_summary.csv, significance_test_set.csv
