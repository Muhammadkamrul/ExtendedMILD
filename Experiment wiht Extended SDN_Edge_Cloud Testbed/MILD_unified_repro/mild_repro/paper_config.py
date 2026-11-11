"""
paper_config.py -- every setting and every reported number of the submitted
manuscript, in one place.

Settings come from the run-commands file and the three notebooks
(main_GitHub.ipynb). Reported numbers are copied from the LaTeX tables.
Nothing here is tuned; this file only describes what was run and reported.
"""

DATASETS = {
    'statistical': {
        'label': 'Statistical benchmark',
        'csv': 'data/statistical/dataset.csv',
        'events': 'data/statistical/events_by_intent.json',
        # generate_hard_dataset.py arguments used for the paper
        'generator': {'minutes': 200000, 'seed': 42, 'p_nonlinear': 0.60, 'p_codrift': 0.20, 'p_independent': 0.20},
    },
    'microservices': {
        'label': 'Microservices testbed',
        'csv': 'data/microservices/dataset.csv',
        'events': 'data/microservices/events_by_intent.json',
        'sha256_csv': 'daea4651232b906b8ef4b0e56fa5f939fd565e472ba912c73cb962629bef83c5',
        'sha256_events': '17e5b35e2752b76e02839751c2eafe2526e7afdd8ea993b00c1361d29952f58c',
    },
    'sdn': {
        'label': 'SDN edge-to-cloud testbed',
        'csv': 'data/sdn/dataset.csv',
        'events': 'data/sdn/events_by_intent.json',
        'sha256_csv': 'ddf96d31474289dbe55192ddec372d313b717de4defc70310bc186acb5c2a744',
        'sha256_events': '9e7cda0669aeb82be9a3d71208620f61ee48499fc14f7cdd10108ac25e7e4df8',
    },
}

COMMON_MILD = {'seed': 42, 'temp': 2.0, 'use_teacher_gate': True, 'lambda_decorr': 1e-4,
               'teacher_max_iter': 600, 'patience': 8}

MILD = {
    'statistical': dict(COMMON_MILD, horizon=120, folds=11, epochs=30, batch=512, fp_budget=1.0,
                        gate_supervise=0.7, gate_sparsity=0.005, distill_alpha=0.9, gate_teacher=0.7),
    'microservices': dict(COMMON_MILD, horizon=50, folds=3, epochs=20, batch=128, fp_budget=3.0,
                          gate_supervise=0.6, gate_sparsity=0.005, distill_alpha=0.6, gate_teacher=0.3),
    'sdn': dict(COMMON_MILD, horizon=15, folds=3, epochs=20, batch=256, fp_budget=3.0,
                gate_supervise=0.6, gate_sparsity=0.002, distill_alpha=0.7, gate_teacher=0.3),
}

MLP = {
    'statistical': {'seed': 42, 'horizon': 120, 'folds': 11, 'epochs': 30, 'batch': 512, 'fp_budget': 1.0},
    'microservices': {'seed': 42, 'horizon': 50, 'folds': 3, 'epochs': 20, 'batch': 128, 'fp_budget': 3.0},
    'sdn': {'seed': 42, 'horizon': 50, 'folds': 3, 'epochs': 20, 'batch': 128, 'fp_budget': 3.0},
}
LSTM = {
    'statistical': {'seed': 42, 'horizon': 120, 'lookback': 60, 'folds': 11, 'epochs': 30, 'batch': 512, 'fp_budget': 1.0},
    'microservices': {'seed': 42, 'horizon': 50, 'lookback': 40, 'folds': 3, 'epochs': 20, 'batch': 128, 'fp_budget': 3.0},
    'sdn': {'seed': 42, 'horizon': 50, 'lookback': 40, 'folds': 3, 'epochs': 20, 'batch': 128, 'fp_budget': 3.0},
}
CLASSIC = {   # label_horizon=120 is hard-coded in baselines_multiintent_gem_corrected.py
    'statistical': {'seed': 42, 'folds': 11, 'fp_budget': 1.0, 'label_horizon': 120},
    'microservices': {'seed': 42, 'folds': 3, 'fp_budget': 3.0, 'label_horizon': 120},
    'sdn': {'seed': 42, 'folds': 3, 'fp_budget': 3.0, 'label_horizon': 120},
}

# Base configuration from which the ablation / w_c-sweep runs were launched in
# the notebooks. For SDN the notebook used distill_alpha=0.6, not the 0.7 of the
# main result. Group 'ablation' reproduces the notebook; group
# 'ablation_consistent' re-runs every variant around the main configuration.
ABLATION_BASE_AS_RUN = {
    'statistical': MILD['statistical'],
    'microservices': MILD['microservices'],
    'sdn': dict(MILD['sdn'], distill_alpha=0.6),
}
ABLATION_VARIANTS = {
    'full': {},
    'no_distill': {'distill_alpha': 1.0},
    'no_gate_cause_sup': {'gate_supervise': 0.0},
    'no_teacher_aug_gate': {'use_teacher_gate': False},
    'no_teacher_kl_gate': {'gate_teacher': 0.0},
    'no_decorrelation': {'lambda_decorr': 0.0},
}
SENSITIVITY = {
    'statistical': {'distill_alpha': [0.3, 0.5, 0.7, 0.9, 1.0], 'gate_supervise': [0.0, 0.3, 0.5, 0.7, 0.9]},
    'microservices': {'distill_alpha': [0.3, 0.5, 0.6, 0.7, 0.9, 1.0], 'gate_supervise': [0.0, 0.3, 0.5, 0.6, 0.7, 0.9]},
    'sdn': {'distill_alpha': [0.3, 0.5, 0.6, 0.7, 0.9, 1.0], 'gate_supervise': [0.0, 0.3, 0.5, 0.6, 0.7, 0.9]},
}

MULTIHORIZON = {
    'statistical': dict(seed=42, horizons=[120, 50, 20], fold=3, folds=5, intent='telemetry', epochs=30, batch=512,
                        fp_budget=0.5, gate_supervise=0.7, gate_sparsity=0.005, distill_alpha=0.9, gate_teacher=0.7, temp=2.0),
    'microservices': dict(seed=42, horizons=[100, 50, 20], fold=3, folds=5, intent='telemetry', epochs=20, batch=128,
                          fp_budget=3.0, gate_supervise=0.6, gate_sparsity=0.005, distill_alpha=0.6, gate_teacher=0.3, temp=2.0),
    'sdn': dict(seed=42, horizons=[3, 5, 15, 30], fold=2, folds=5, intent='analytics', epochs=20, batch=256,
                fp_budget=1.0, gate_supervise=0.6, gate_sparsity=0.002, distill_alpha=0.7, gate_teacher=0.3, temp=2.0),
}
DISAMB_PLOT_FOLD = {'statistical': 7, 'microservices': 3, 'sdn': 3}

# ----------------------------------------------------------------------------
# numbers reported in the submitted manuscript
# columns: detection %, lead time (min), FP/day, disambiguation %
# ----------------------------------------------------------------------------
METHODS = ['MILD', 'MLP', 'LSTM', 'WKPI-Tuned', 'Dist-Target', 'LR-OvR']

PAPER_COMPARISON = {
    'statistical': {'MILD': [98.89, 100.09, 4.97, 89.67], 'MLP': [99.44, 92.77, 5.05, 81.03],
                    'LSTM': [97.11, 88.13, 5.35, 80.60], 'WKPI-Tuned': [99.07, 78.42, 15.73, 66.68],
                    'Dist-Target': [78.39, 57.95, 9.40, 60.82], 'LR-OvR': [99.44, 82.67, 6.23, 79.02]},
    'microservices': {'MILD': [91.32, 31.76, 8.17, 66.80], 'MLP': [79.77, 24.00, 4.41, 41.22],
                      'LSTM': [86.85, 29.41, 19.54, 59.23], 'WKPI-Tuned': [0.13, 6.50, 3.80, 0.13],
                      'Dist-Target': [11.77, 7.54, 11.99, 5.55], 'LR-OvR': [9.14, 9.15, 61.07, 7.07]},
    'sdn': {'MILD': [92.06, 8.62, 8.30, 88.97], 'MLP': [55.52, 8.62, 5.09, 51.79],
            'LSTM': [35.91, 5.97, 14.73, 40.77], 'WKPI-Tuned': [4.34, 1.26, 7.20, 0.51],
            'Dist-Target': [5.26, 2.12, 5.14, 1.54], 'LR-OvR': [10.56, 1.15, 4.70, 10.26]},
}
# value printed in the notebook output where it differs from the manuscript
NOTEBOOK_DIFFERENCES = {('statistical', 'WKPI-Tuned', 'fp_per_day'): 35.73}

PAPER_MILD_PER_INTENT = {   # (det mean, det std, lead mean, lead std); overall FP and DA (mean, std)
    'statistical': {'analytics': (96.67, 10.00, 91.53, 7.73), 'api': (100.00, 0.00, 97.63, 12.04),
                    'telemetry': (100.00, 0.00, 111.10, 10.07), 'fp': (4.97, 6.03), 'da': (89.67, 10.04)},
    'microservices': {'analytics': (82.34, 1.03, 29.73, 0.72), 'api': (99.10, 0.62, 34.30, 1.77),
                      'telemetry': (92.51, 0.34, 31.25, 0.58), 'fp': (8.17, 0.67), 'da': (66.80, 0.13)},
    'sdn': {'analytics': (100.00, 0.00, 10.63, 0.23), 'api': (100.00, 0.00, 7.76, 0.64),
            'telemetry': (76.18, 10.29, 7.48, 1.02), 'fp': (8.30, 0.34), 'da': (88.97, 0.26)},
}

PAPER_ABLATION = {
    'statistical': {'full': [98.89, 100.09, 4.97, 89.67], 'no_distill': [96.11, 100.47, 5.13, 87.04],
                    'no_gate_cause_sup': [98.89, 100.31, 6.49, 87.61], 'no_teacher_aug_gate': [100.0, 98.27, 4.13, 89.71],
                    'no_teacher_kl_gate': [91.11, 93.83, 4.78, 79.22], 'no_decorrelation': [100.0, 100.04, 5.77, 88.67]},
    'microservices': {'full': [91.32, 31.76, 8.17, 66.80], 'no_distill': [91.57, 32.84, 11.96, 67.35],
                      'no_gate_cause_sup': [91.50, 32.65, 19.39, 20.65], 'no_teacher_aug_gate': [91.65, 32.37, 8.72, 69.34],
                      'no_teacher_kl_gate': [90.57, 30.60, 7.52, 65.93], 'no_decorrelation': [90.87, 31.66, 7.95, 65.76]},
    'sdn': {'full': [92.06, 8.62, 8.30, 88.97], 'no_distill': [80.20, 8.51, 8.74, 87.18],
            'no_gate_cause_sup': [95.64, 8.52, 8.88, 87.95], 'no_teacher_aug_gate': [76.34, 7.67, 7.68, 81.28],
            'no_teacher_kl_gate': [59.21, 7.19, 6.34, 67.44], 'no_decorrelation': [90.78, 8.23, 8.06, 88.72]},
}

PAPER_SENSITIVITY = {
    'statistical': {('distill_alpha', 0.3): [99.44, 92.60, 7.68, 89.67], ('distill_alpha', 0.5): [98.33, 94.31, 6.65, 89.67],
                    ('distill_alpha', 0.7): [98.33, 96.06, 6.92, 89.71], ('distill_alpha', 0.9): [98.89, 100.09, 4.97, 89.67],
                    ('distill_alpha', 1.0): [96.11, 100.47, 5.13, 87.04],
                    ('gate_supervise', 0.0): [98.89, 100.31, 6.49, 87.61], ('gate_supervise', 0.3): [98.89, 100.49, 6.11, 89.09],
                    ('gate_supervise', 0.5): [98.89, 100.36, 6.18, 92.05], ('gate_supervise', 0.7): [98.89, 100.09, 4.97, 89.67],
                    ('gate_supervise', 0.9): [98.33, 101.00, 6.24, 91.38]},
    'microservices': {('distill_alpha', 0.3): [75.98, 25.69, 7.25, 56.66], ('distill_alpha', 0.5): [90.21, 29.43, 6.79, 66.74],
                      ('distill_alpha', 0.6): [91.32, 31.76, 8.17, 66.80], ('distill_alpha', 0.7): [91.83, 32.82, 10.32, 68.31],
                      ('distill_alpha', 0.9): [91.43, 32.94, 12.47, 65.10], ('distill_alpha', 1.0): [91.57, 32.84, 11.96, 67.35],
                      ('gate_supervise', 0.0): [91.50, 32.65, 19.39, 20.65], ('gate_supervise', 0.3): [91.57, 32.00, 10.89, 64.27],
                      ('gate_supervise', 0.5): [90.58, 31.63, 7.01, 65.95], ('gate_supervise', 0.6): [91.32, 31.76, 8.17, 66.80],
                      ('gate_supervise', 0.7): [90.40, 31.48, 9.33, 64.34], ('gate_supervise', 0.9): [91.07, 31.82, 9.70, 68.18]},
    'sdn': {('distill_alpha', 0.3): [90.46, 7.56, 8.54, 85.64], ('distill_alpha', 0.5): [89.64, 8.11, 7.97, 87.18],
            ('distill_alpha', 0.6): [92.35, 8.38, 8.59, 88.72], ('distill_alpha', 0.7): [92.06, 8.62, 8.30, 88.97],
            ('distill_alpha', 0.9): [93.73, 9.37, 10.08, 89.49], ('distill_alpha', 1.0): [80.20, 8.51, 8.74, 87.18],
            ('gate_supervise', 0.0): [95.64, 8.52, 8.88, 87.95], ('gate_supervise', 0.3): [93.34, 8.37, 8.35, 87.95],
            ('gate_supervise', 0.5): [91.63, 8.27, 8.35, 88.21], ('gate_supervise', 0.6): [92.06, 8.62, 8.30, 88.97],
            ('gate_supervise', 0.7): [90.39, 8.19, 8.30, 87.69], ('gate_supervise', 0.9): [90.78, 8.13, 8.11, 89.23]},
}
# Paper text (Sec. V-E3): first telemetry alerts ~128, 97, 42 min before failure
# for horizons reported as 120, 60, 20 (the notebook command used 120, 50, 20).
PAPER_MULTIHORIZON = {'statistical': {120: 128, 60: 97, 50: 97, 20: 42}}

# Comparison tolerances used to label each reproduced cell
TOLERANCE = {
    'identical_abs': 0.005,        # equal at the printed two-decimal precision
    'close_abs': {'detection': 2.0, 'lead': 2.0, 'fp_per_day': 1.0, 'disamb': 2.0},
    'close_rel': 0.05,             # or within 5 % of the paper value
}
