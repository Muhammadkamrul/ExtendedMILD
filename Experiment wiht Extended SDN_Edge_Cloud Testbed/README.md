# MILD: extended SDN testbed and paper reproduction

This release accompanies the **extended SDN-based testbed** experiments in the MILD paper. It covers Protocol B, not the first SDN testbed or the O-RAN experiments.

Choose the workflow that matches your goal:

| Goal | Start here | Requires running Mininet? |
|---|---|---|
| Inspect the published results | [Saved results](#saved-results) | No |
| Retrain and evaluate on the paper dataset | [Reproduce the evaluation](#reproduce-the-evaluation) | No |
| Audit dataset construction | [Dataset provenance](#dataset-provenance) | No |
| Collect a new dataset on your machine | [Optional collection of new data](#optional-collection-of-new-data) | Yes |

## Repository layout

Keep these three directories beside this README:

- `MILD_SDN_testbed_v2/`: testbed, collector, calibration, dataset construction, quality checks, and training wrapper.
- `MILD_unified_repro/`: the matching training/evaluation implementation, including `autotrain.py` and all its dependencies.
- `mild_sdn2_train_v3_30k_PaperData/`: frozen dataset, published results, logs, and collection snapshot.

The testbed wrapper locates `MILD_unified_repro` as a sibling directory. Despite its name, `MILD_SDN_testbed_v2` implements the extended testbed described here.

## Saved results

No installation is needed to inspect these files under `mild_sdn2_train_v3_30k_PaperData/`:

| File or directory | Purpose |
|---|---|
| `training/final_test_summary.csv` | Aggregate final test results; compare the tuned variants with the main paper table |
| `training/final_test_runs.csv` | Individual final runs, including model variants and seeds |
| `training/AUTOTRAIN_REPORT.md` | Search, final evaluation, and interpretation |
| `training/significance_test_set.csv` | Test-set significance results |
| `training/significance_by_seed.csv` | Seed-level significance output |
| `training/search_leaderboard.csv` | Development-set search results |
| `training/search_rerank.csv` | Reranked candidates |
| `training/audit.json` | Dataset/split audit |
| `training/opcurve/` | Equal-FP operating-curve outputs |
| `training/jobs/` | Individual job configurations, predictions, metrics, and any retained model artifacts |
| `train_now.log`, `training.log` | Original execution settings and progress |
| `SUITABILITY_REPORT.md`, `suitability_checks.csv` | Dataset suitability assessment |
| `TRUST_REPORT.md`, `trust_verdict.json`, `trust_criteria.csv` | Reliability assessment and caveats |
| `SALVAGE_REPORT.md` | Dataset salvage record |

Use the `tuned` variants for the main Protocol B comparison. Original-profile (`paper`) variants are separate comparisons and should not be averaged together with tuned variants. Classical baselines are deterministic; repeated seed entries do not constitute independent randomized fits.

The manuscript reports the following tuned-model means:

| Model | Detection (%) | Lead time (steps) | FP / 1440 steps | Disambiguation accuracy (%) |
|---|---:|---:|---:|---:|
| MILD | 77.18 | 5.15 | 4.18 | 77.74 |
| MLP | 63.50 | 4.96 | 0.54 | 36.13 |
| LSTM | 53.51 | 4.96 | 2.82 | 31.94 |
| WKPI-Tuned | 27.77 | 1.13 | 3.51 | 0.00 |
| Dist-Target | 25.83 | 2.01 | 4.95 | 20.97 |
| LR-OvR | 51.05 | 3.52 | 2.56 | 26.61 |

One step is a **4-second tick**. The FP denominator of 1440 steps is a simulated day, equivalent to 96 minutes of emulated time. It is not a 24-hour wall-clock rate. Detection and lead time are macro-averaged across intents as described in the paper.

## Frozen paper dataset and protocol

The input files are:

- `mild_sdn2_train_v3_30k_PaperData/dataset/dataset.csv`
- `mild_sdn2_train_v3_30k_PaperData/dataset/events_by_intent.json`

The original training log records **29,765 rows** and **859 labeled intent events**: 231 Analytics, 283 API, and 345 Telemetry events. These are intent-event counts, not necessarily distinct injected root-cause episodes.

Protocol B uses a chronological development/test split. The original audit reports **20,835 development rows and 8,930 test rows**, with a nominal test fraction of 0.30. Preserve the code's split logic; do not shuffle rows or substitute a random split. Search and threshold selection use development data; the held-out test part is reserved for final evaluation. Five training seeds share that same test partition.

## Reproduce the evaluation

### 1. Prepare the training environment

Use a Linux shell (Ubuntu or WSL2), starting at the repository root. Data-only training does not require root privileges, Ryu, Mininet, or a running testbed.

Use the training dependencies supplied with the released `MILD_unified_repro` version. The testbed installer specifies Python 3.11 and TensorFlow, NumPy, pandas, scikit-learn, SciPy, matplotlib, SHAP, and tabulate. A baseline CPU environment can be created as follows:

```bash
python3.11 -m venv .venv-train
source .venv-train/bin/activate
python -m pip install --upgrade pip
python -m pip install -r MILD_unified_repro/requirements.txt
```

The supplied requirements use minimum versions rather than exact pins; they are not a recovered lockfile of the original experiment. Prefer the archived environment specification if supplied with the release. For GPU training, use a TensorFlow/CUDA combination compatible with your machine. The original command used `/home/windows/mild-venv/bin/python`; this local interpreter path does not identify all installed versions.

### 2. Run the logged experiment on the frozen data

From the repository root with the training environment active:

```bash
REPO_ROOT="$PWD"
mkdir -p "$REPO_ROOT/reproduced"
cd "$REPO_ROOT/MILD_unified_repro"

python autotrain.py \
  --data_csv "$REPO_ROOT/mild_sdn2_train_v3_30k_PaperData/dataset/dataset.csv" \
  --events_json "$REPO_ROOT/mild_sdn2_train_v3_30k_PaperData/dataset/events_by_intent.json" \
  --out "$REPO_ROOT/reproduced/training" \
  --paper_profile sdn \
  --trials 20 \
  --fp_budget 3.0 \
  --test_frac 0.300000 \
  --seeds 42,43,44,45,46 \
  --fp_block 240 \
  --rerank_top 3 \
  --search_seed 7 \
  --rerank_seeds 43,44 \
  --opcurve_targets 1,2,3,5,8 \
  > "$REPO_ROOT/reproduced/training.log" 2>&1
```

This is the command recorded in `train_now.log`, with portable input/output paths and without the optional `nice -n 10` scheduling prefix. It additionally makes the supplied training code's defaults explicit: search seed 7 and reranking seeds 43,44. Initial search trials use seed 42; neural candidates are reranked over seeds 42,43,44. The profile name is `sdn` even though these data come from the extended testbed. Do not change it to an invented extended-testbed profile.

Write to a fresh output directory to perform a new computation. The training pipeline caches jobs; using the archived `training/` directory could reuse prior computations. Keep the paper archive unchanged.

The original run finished successfully in approximately **97.4 minutes** on the author's machine; runtime depends on hardware and software versions.

### 3. Check the outputs

Inspect `reproduced/training/AUTOTRAIN_REPORT.md`, `audit.json`, `final_test_runs.csv`, `final_test_summary.csv`, significance files, and `opcurve/` against the corresponding archived files.

Check the dataset counts, split counts, seeds, and model variants first. Then compare tuned-model means and standard deviations. Small numerical differences can arise from TensorFlow versions, GPU kernels, or platform differences. A large difference requires checking the code revision, environment, input files, and selected configurations.

The CLI `--fp_budget 3.0` is the logged search setting. It should not be replaced with 1 simply because the selected MILD configuration has a threshold budget of 1 in the paper. The paper describes a search over threshold FP budgets; consult the `cfg` JSON column of `final_test_runs.csv` for the final per-model settings. The saved tuned MILD configuration confirms horizon 10, FP budget 1, batch size 128, 20 epochs, distillation alpha 0.8, gate supervision 0.5, gate teacher 0.5, gate sparsity 0.001, decorrelation weight 0.0001, and temperature 2.0. The equal-FP curves and tuned operating-point comparison are different evaluations.

The supplied significance implementation tests each seed separately with Holm correction, then uses a consensus rule requiring at least two thirds of seeds and no significant opposite result. The aggregate `p_holm` is a median over seeds, not a pooled p-value. See `mild_repro/stats.py` and the reports for the full procedure.

Seed-level paired tests measure consistency over training seeds on one shared test partition, not generalization over independently collected datasets. Read the archived trust report alongside significance results: the original wrapper reported **USABLE WITH CAVEATS**.

## Dataset provenance

`train_now.log` records a snapshot of **34 completed main segments** from the author's collection state, followed by dataset construction and training on 14 October 2026. The original local source was `mild_sdn2_state_v3`; the original output directory was `mild_sdn2_train_v3_30k`.

Preserve `snapshot/` together with its manifests, segment data, calibration/configuration files, and any retained quality/provenance files. It supports auditing how the frozen dataset was built. Retraining on the frozen CSV/JSON above is the primary reproduction workflow; reviewers do not need to rebuild the dataset or collect new measurements.

The collection design uses two edge sites, a WAN, and two cloud racks, with API, Analytics, and Telemetry services. Faults and mimics generate precursor behavior and co-drift. Labeled failures require observed violations; see the collector, calibration, salvage, and quality-check code and the manuscript for exact rules.

## Optional collection of new data

New collection is useful for testing the method on another machine. It produces a **new experiment**, not a replacement for the frozen paper dataset: service performance, calibration, adaptive fault severity, scheduling, and measurements depend on the host.

Use Ubuntu/WSL2 with functioning Open vSwitch, Mininet, network namespaces, traffic control, and sufficient CPU/memory. Keep the collection environment (Python 3.9 for Ryu) separate from the training environment (Python 3.11).

The detailed setup and architecture guide is [the testbed README](MILD_SDN_testbed_v2/README.md). Its historical three-seed training description is superseded by the five-seed command above. Its default collection target is 45,000 ticks, not the 29,765 retained rows of the paper dataset.

### Install and check the testbed

With Conda installed, the provided installer creates `ryu39` and `mild-train`:

```bash
cd MILD_SDN_testbed_v2
bash scripts/install_deps_ubuntu.sh
conda activate ryu39
python pipeline.py --selftest --state "$HOME/mild_sdn2_selftest" \
  --train_python "$(conda run -n mild-train which python)"
```

The installer changes system services, installs packages, and patches Ryu. Its dependencies are not a complete pinned environment. If the Mininet import fails inside the Python 3.9 environment, confirm that the Ubuntu Mininet package is installed and expose its system package directory:

```bash
export PYTHONPATH="/usr/lib/python3/dist-packages${PYTHONPATH:+:$PYTHONPATH}"
python -c "from mininet.net import Mininet; print('Mininet import OK')"
```

### Collect into a separate state directory

With `ryu39` active, from `MILD_SDN_testbed_v2/`:

```bash
TRAIN_PYTHON="$(conda run -n mild-train which python)"
sudo -E env PATH="$PATH" "$(command -v python)" pipeline.py \
  --state "$HOME/mild_sdn2_new_state" \
  --target_ticks 30000 \
  --train_python "$TRAIN_PYTHON"
```

This example requests 30,000 ticks for a new run. Retained clean rows can differ after segment trimming and quality processing. It does not reconstruct the exact paper collection history. The full pipeline calibrates, collects, merges, checks quality, and may invoke its own training phase; that phase is not a substitute for the explicitly logged paper command.

Press Ctrl+C once to stop cleanly. To resume, use the same state directory and command. Never point a new collection at the frozen paper-data directory. Use the retained state configuration and calibration files to audit overrides; `config/testbed.json` alone describes defaults.

For a fresh five-seed training and assessment of the newly collected data:

```bash
python train_now.py \
  --state "$HOME/mild_sdn2_new_state" \
  --out "$HOME/mild_sdn2_new_analysis" \
  --train_python "$TRAIN_PYTHON" \
  --trials 20 --seeds 42,43,44,45,46 \
  --rerank_top 3 --fp_budget 3.0 --fp_block 240 --test_frac 0.30
```

Read the new suitability and trust reports before interpreting results. Do not combine new-data scores with the archived paper scores.

## Release provenance

The experiment command and counts above were recovered from the original logs. The expected metrics were transcribed from the accompanying manuscript. The uploaded testbed source confirms the wrapper arguments. The supplied dataset was checked directly: 29,765 CSV rows and event counts 283 API, 231 Analytics, and 345 Telemetry. The saved runs contain all five seeds and both variants (60 rows total). Recomputing tuned means and sample standard deviations from those runs agrees with the supplied summary and manuscript to two decimal places. No full training rerun was performed during this verification.

The `training.zip` supplied for documentation contains summaries and operating-curve outputs but **does not contain `jobs/`**. Include the original `training/jobs/` in the public archive if available. It contains job specifications and time-series predictions needed to independently recompute the archived significance and equal-FP analyses without retraining. A fresh full training run creates its own jobs. If jobs are omitted from the release, state that the release supports inspection and retraining but not recomputation from archived predictions alone.

The raw `snapshot/` and original full runtime environment were not included in this documentation check. Exact recovery of the outer `train_now.py` shell invocation, package versions, and complete collection history cannot be inferred from defaults alone.

### Input checksums (SHA-256)

```text
cb99518d9e49129c3ff3eb1de3d6eb221cca2070e73e58e70119f035165c75a9  dataset/dataset.csv
99ca5bb5fa79e1ab84134ce91b0b85964abbf635a46342d6b5b554ed23a5e86a  dataset/events_by_intent.json
```

### Assembling the release from the ZIP files

Extract `MILD_unified_repro.zip` **into a directory named `MILD_unified_repro/`**: its files are at the archive root. Similarly, extract `training.zip` **into `mild_sdn2_train_v3_30k_PaperData/training/`**. The dataset archive already contains a `dataset/` directory; extract it into `mild_sdn2_train_v3_30k_PaperData/`. Do not extract all three archives into one directory. Add the original logs/reports, `snapshot/`, and `training/jobs/` from the paper-data folder. Exclude `__pycache__/` and `.pyc` files.

