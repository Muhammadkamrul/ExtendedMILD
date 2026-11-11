# Automatic training and comparison report

Dataset: `/home/windows/mild_sdn2_train_v3_30k/dataset/dataset.csv`

## 1. Dataset audit

```
{
  "rows": 29765,
  "events_total": 859,
  "events_by_intent": {
    "analytics": 231,
    "api": 283,
    "telemetry": 345
  },
  "events_by_type": {
    "conflicting_analytics_cause": 98,
    "conflicting_api_cause": 123,
    "conflicting_victim": 212,
    "independent_analytics_slow": 68,
    "independent_api_latency": 61,
    "independent_telemetry_slow": 67,
    "nonlinear_analytics_xor": 65,
    "nonlinear_api_xor": 99,
    "nonlinear_telemetry_xor": 66
  },
  "observed_fraction_by_intent": {
    "analytics": 1.0,
    "api": 1.0,
    "telemetry": 1.0
  },
  "buildup_quantiles": {
    "0.1": 8.0,
    "0.25": 10.0,
    "0.5": 12.0,
    "0.75": 14.0,
    "0.9": 16.0
  },
  "missing_values": 0,
  "dev_rows": 20835,
  "test_rows": 8930,
  "inner_validation_start": 15626,
  "lt_ref": 12.0
}
```

## 2. Protocol

* Chronological split. Development set: first 20835 rows. Held-out test set: last 8930 rows. The cut points are placed where no event is active.
* Every method was tuned with the same trial budget (20) and the same objective on the development set only. The objective is: DA + 0.5·Det + 25·min(LT/LT_ref, 1.5) − 4·FP/day − 20·max(0, FP/day − budget).
* All methods tune their alert thresholds under the same FP budget of 3.0/day.
* The 3 best trials of each neural method were re-run with seeds 43,44 on the same development validation period; the configuration with the best mean score was kept (search_rerank.csv).
* The held-out test set was used once, for the final evaluation, with seeds 42,43,44,45,46.

## 3. Held-out test results (mean and std over seeds)

                    detection        lead       fp_per_day       disamb        fp_episodes_per_day       disamb_all_roots      
                         mean   std  mean   std       mean   std   mean    std                mean   std             mean   std
method      variant                                                                                                            
Dist-Target paper       26.32  0.00  2.67  0.00       8.62  0.00  21.77   0.00                5.11  0.00            24.62  0.00
            tuned       25.83  0.00  2.01  0.00       4.95  0.00  20.97   0.00                3.51  0.00            23.59  0.00
LR-OvR      paper       21.48  0.00  4.64  0.00       8.30  0.00  34.68   0.00                1.76  0.00            22.56  0.00
            tuned       51.05  0.00  3.52  0.00       2.56  0.00  26.61   0.00                1.28  0.00            30.77  0.00
LSTM        paper       24.44  2.66  5.27  0.60      13.31  6.48  24.35   5.57                1.89  0.54            21.33  3.71
            tuned       53.51  9.64  4.96  0.39       2.82  1.46  31.94   7.71                0.80  0.41            38.97  9.14
MILD        paper       89.12  3.34  6.35  0.20       9.33  1.66  85.81   4.14                3.64  0.35            85.33  2.78
            tuned       77.18  9.07  5.15  0.55       4.18  1.81  77.74   5.28                2.20  0.93            76.10  5.93
MLP         paper       41.99  9.10  4.81  0.52      11.59  8.65  25.32  10.18                2.56  0.69            25.13  9.04
            tuned       63.50  5.05  4.96  0.17       0.54  0.43  36.13   8.11                0.45  0.36            50.67  7.28
WKPI-Tuned  paper       39.21  0.00  2.03  0.00       5.59  0.00  17.74   0.00                4.63  0.00            22.56  0.00
            tuned       27.77  0.00  1.13  0.00       3.51  0.00   0.00   0.00                2.56  0.00             8.72  0.00

## 4. Verdict (priority order: FP/day, disambiguation, lead time, detection)

- **FP/day**: MILD 4.18 vs best baseline MLP 0.54 → MILD WORSE (paired test vs MLP: no significant difference, Holm p=0.013)
- **Disambiguation accuracy**: MILD 77.74 vs best baseline MLP 36.13 → MILD better or equal (paired test vs MLP: MILD significantly better, Holm p=8.53e-14)
- **Lead time**: MILD 5.15 vs best baseline MLP 4.96 → MILD better or equal (paired test vs MLP: MILD significantly better, Holm p=1.02e-07) [means are over each model's own detections; the paired test uses events detected by both]
- **Detection rate**: MILD 77.18 vs best baseline MLP 63.50 → MILD better or equal (paired test vs MLP: MILD significantly better, Holm p=9.84e-08)

**MILD does not beat the best baseline on every metric.** Below are the likely reasons and fixes, based on the dataset audit and the per-intent results:

- FP/day is higher. Check whether the validation FP budget was met but not the test one. That points to a shift between the development and test periods, so collect more segments or use more seeds. A lower `--fp_budget` or a longer EWMA span grid can help.

## 5. Paired significance tests on the held-out test set (consensus over seeds 42,43,44,45,46)

Each seed is tested separately (MILD seed s vs baseline seed s, Holm-corrected). A verdict needs at least two thirds of the seeds and no seed with a significant opposite result. Means are over seeds; p_holm is the median. Per-seed results: significance_by_seed.csv.

baseline_name                            metric  n_units    mild  baseline    diff  ci_low  ci_high  p_holm  seeds_mild_better  seeds_baseline_better                   verdict
          MLP                       detection_%      259 80.0000   65.9459 14.0541  8.1853  19.6950  0.0000                  4                      0 MILD significantly better
          MLP                  disambiguation_%      124 77.7419   36.1290 41.6129 32.4153  50.8065  0.0000                  5                      0 MILD significantly better
          MLP lead_time_min (common detections)      151  6.4061    5.4149  0.9912  0.6966   1.2881  0.0000                  5                      0 MILD significantly better
          MLP                  fp_steps_per_day       38  4.1368    0.5368  3.6000  1.3579   6.2526  0.0130                  0                      3 no significant difference
         LSTM                       detection_%      259 80.0000   56.3707 23.6293 17.2201  30.0386  0.0000                  4                      0 MILD significantly better
         LSTM                  disambiguation_%      124 77.7419   31.9355 45.8065 35.8065  55.8105  0.0000                  5                      0 MILD significantly better
         LSTM lead_time_min (common detections)      135  6.5512    5.3652  1.1860  0.9296   1.4506  0.0000                  5                      0 MILD significantly better
         LSTM                  fp_steps_per_day       38  4.1368    2.7789  1.3579 -2.3368   4.5474  0.4773                  0                      1 no significant difference
   WKPI-Tuned                       detection_%      259 80.0000   33.2046 46.7954 40.6178  52.8185  0.0000                  5                      0 MILD significantly better
   WKPI-Tuned                  disambiguation_%      124 77.7419    0.0000 77.7419 70.0000  84.8427  0.0000                  5                      0 MILD significantly better
   WKPI-Tuned lead_time_min (common detections)       85  7.1887    2.3148  4.8739  4.4118   5.3405  0.0000                  5                      0 MILD significantly better
   WKPI-Tuned                  fp_steps_per_day       38  4.1368    3.4737  0.6632 -2.7158   4.0421  0.9441                  0                      0 no significant difference
  Dist-Target                       detection_%      259 80.0000   21.6216 58.3784 51.0425  65.4826  0.0000                  5                      0 MILD significantly better
  Dist-Target                  disambiguation_%      124 77.7419   20.9677 56.7742 48.0645  65.4839  0.0000                  5                      0 MILD significantly better
  Dist-Target lead_time_min (common detections)       42  4.8556    1.8218  3.0338  2.3044   3.8723  0.0000                  5                      0 MILD significantly better
  Dist-Target                  fp_steps_per_day       38  4.1368    4.8947 -0.7579 -4.3895   2.5595  0.9441                  1                      0 no significant difference
       LR-OvR                       detection_%      259 80.0000   50.5792 29.4208 22.3938  36.3726  0.0000                  5                      0 MILD significantly better
       LR-OvR                  disambiguation_%      124 77.7419   26.6129 51.1290 39.1935  62.7419  0.0000                  5                      0 MILD significantly better
       LR-OvR lead_time_min (common detections)      113  6.0905    4.2305  1.8600  1.4740   2.2512  0.0000                  5                      0 MILD significantly better
       LR-OvR                  fp_steps_per_day       38  4.1368    2.5263  1.6105 -0.8842   3.9789  0.2625                  0                      1 no significant difference

## 6. Selected configurations

- MILD: `{"batch": 128, "distill_alpha": 0.8, "epochs": 20, "fp_block_ticks": 240, "fp_budget": 1.0, "gate_sparsity": 0.001, "gate_supervise": 0.5, "gate_teacher": 0.5, "horizon": 10, "lambda_decorr": 0.0001, "patience": 8, "seed": 42, "teacher_max_iter": 600, "temp": 2.0, "use_teacher_gate": true}` (search score -70.07)
- MLP: `{"batch": 256, "epochs": 30, "fp_block_ticks": 240, "fp_budget": 1.0, "horizon": 10, "seed": 42}` (search score 22.36)
- LSTM: `{"batch": 128, "epochs": 20, "fp_block_ticks": 240, "fp_budget": 1.0, "horizon": 10, "lookback": 40, "seed": 42}` (search score -25.87)
- WKPI-Tuned: `{"fp_block_ticks": 240, "fp_budget": 1.0, "label_horizon": 15, "seed": 42}` (search score -160.16)
- Dist-Target: `{"fp_block_ticks": 240, "fp_budget": 1.0, "label_horizon": 120, "seed": 42}` (search score -537.65)
- LR-OvR: `{"fp_block_ticks": 240, "fp_budget": 1.0, "label_horizon": 12, "seed": 42}` (search score -5.99)