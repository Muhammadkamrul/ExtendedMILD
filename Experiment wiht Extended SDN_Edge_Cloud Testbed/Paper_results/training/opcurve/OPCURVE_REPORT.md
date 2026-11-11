# Comparison at equal false-alarm rate (post hoc operating points)

Each method is re-thresholded on the test period so that all methods produce the same number of
false-alarm steps per day. Same rule for every method (common healthy-time quantile, bisection).
The achieved FP rate can be slightly below the target because FP steps are discrete.
Mean over seeds. This analysis complements, and does not replace, the main protocol.

## Paired tests at the FP budget (3/day target), consensus over seeds

baseline_name                            metric  n_units   mild  baseline   diff  p_holm  seeds_mild_better  seeds_baseline_better                   verdict
          MLP                       detection_%      259 82.085    81.158  0.927   0.644                  1                      0 no significant difference
          MLP                  disambiguation_%      124 76.290    55.323 20.968   0.000                  5                      0 MILD significantly better
          MLP lead_time_min (common detections)      188  5.999     5.774  0.225   0.193                  2                      0 no significant difference
         LSTM                       detection_%      258 82.016    63.721 18.295   0.000                  4                      0 MILD significantly better
         LSTM                  disambiguation_%      123 76.098    43.902 32.195   0.000                  5                      0 MILD significantly better
         LSTM lead_time_min (common detections)      149  6.377     5.534  0.843   0.000                  4                      0 MILD significantly better
   WKPI-Tuned                       detection_%      259 82.085    38.610 43.475   0.000                  5                      0 MILD significantly better
   WKPI-Tuned                  disambiguation_%      124 76.290     0.000 76.290   0.000                  5                      0 MILD significantly better
   WKPI-Tuned lead_time_min (common detections)       98  7.026     1.398  5.628   0.000                  5                      0 MILD significantly better
  Dist-Target                       detection_%      259 82.085    20.849 61.236   0.000                  5                      0 MILD significantly better
  Dist-Target                  disambiguation_%      124 76.290    20.161 56.129   0.000                  5                      0 MILD significantly better
  Dist-Target lead_time_min (common detections)       45  5.112     1.681  3.431   0.000                  5                      0 MILD significantly better
       LR-OvR                       detection_%      259 82.085    54.440 27.645   0.000                  5                      0 MILD significantly better
       LR-OvR                  disambiguation_%      124 76.290    21.774 54.516   0.000                  5                      0 MILD significantly better
       LR-OvR lead_time_min (common detections)      128  6.194     4.325  1.869   0.000                  5                      0 MILD significantly better

## Seed spread at the FP budget (std over seeds, percentage points)

             detection  disamb
method                        
Dist-Target       0.00    0.00
LR-OvR            0.00    0.00
LSTM              8.74    6.64
MILD              2.81    4.13
MLP               1.33    4.72
WKPI-Tuned        0.00    0.00

## Variant: tuned

### Target 1 FP steps/day

             fp_per_day  detection  lead_all_detected  disamb
method                                                       
MILD               0.96      66.26               5.44   65.00
MLP                0.96      75.08               5.51   50.48
LSTM               0.96      54.53               5.08   34.96
Dist-Target        0.96      22.39               1.69   16.94
LR-OvR             0.96      44.82               4.07   15.32
WKPI-Tuned         0.96      20.04               1.27    0.00

### Target 2 FP steps/day

             fp_per_day  detection  lead_all_detected  disamb
method                                                       
MILD               1.92      75.12               5.60   70.97
MLP                1.92      77.52               5.60   52.74
LSTM               1.92      58.78               5.30   39.19
LR-OvR             1.92      49.58               4.16   22.58
Dist-Target        1.92      24.16               1.81   19.35
WKPI-Tuned         1.92      29.53               1.43    0.00

### Target 3 FP steps/day

             fp_per_day  detection  lead_all_detected  disamb
method                                                       
MILD               2.87      80.12               5.73   76.29
MLP                2.87      79.52               5.73   55.32
LSTM               2.89      62.74               5.54   43.90
LR-OvR             2.87      54.09               4.13   21.77
Dist-Target        2.87      25.03               1.91   20.16
WKPI-Tuned         2.87      33.46               1.44    0.00

### Target 5 FP steps/day

             fp_per_day  detection  lead_all_detected  disamb
method                                                       
MILD               4.79      88.87               5.94   83.87
MLP                4.79      82.87               5.89   59.52
LSTM               4.81      67.11               5.82   45.20
LR-OvR             4.79      56.47               4.31   23.39
Dist-Target        4.79      25.83               2.36   20.97
WKPI-Tuned         4.79      36.68               2.22    0.00

### Target 8 FP steps/day

             fp_per_day  detection  lead_all_detected  disamb
method                                                       
MILD               7.67      93.29               6.24   86.61
MLP                7.67      86.02               6.11   62.74
LSTM               7.70      73.50               6.20   49.27
LR-OvR             7.67      59.96               4.45   27.42
Dist-Target        7.67      26.31               2.75   21.77
WKPI-Tuned         7.67      38.62               2.84    0.00

## Variant: paper

### Target 1 FP steps/day

            fp_per_day  detection  lead_all_detected  disamb
method                                                      
MILD              0.96      58.03               5.64   58.55
MLP               0.96      36.99               4.63   25.97
LSTM              0.96      18.29               4.45   18.37
WKPI-Tuned        0.96      16.24               0.93    0.00

### Target 2 FP steps/day

            fp_per_day  detection  lead_all_detected  disamb
method                                                      
MILD              1.92      66.18               5.95   66.61
MLP               1.92      40.69               4.84   29.03
LSTM              1.92      20.87               4.54   20.65
WKPI-Tuned        1.92      22.46               1.56    2.42

### Target 3 FP steps/day

            fp_per_day  detection  lead_all_detected  disamb
method                                                      
MILD              2.87      72.37               6.11   72.74
MLP               2.87      42.83               5.02   29.68
LSTM              2.89      21.86               4.71   21.79
WKPI-Tuned        2.87      28.70               1.73    4.84

### Target 5 FP steps/day

            fp_per_day  detection  lead_all_detected  disamb
method                                                      
MILD              4.79      79.44               6.25   78.23
MLP               4.79      46.02               5.30   31.77
LSTM              4.81      24.43               4.79   24.39
WKPI-Tuned        4.79      37.20               1.92   10.48

### Target 8 FP steps/day

            fp_per_day  detection  lead_all_detected  disamb
method                                                      
MILD              7.67      86.07               6.48   83.23
MLP               7.67      50.38               5.75   34.19
LSTM              7.70      27.25               5.10   26.34
WKPI-Tuned        7.67      48.43               2.21   20.16

