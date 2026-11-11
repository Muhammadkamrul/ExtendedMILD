# MILD in O-RAN testbed: saved-results reproduction and fresh training

This repository provides MILD V6 code, frozen input data, saved experiment outputs and prediction jobs. It supports two workflows:

1. **Recompute the archived evaluation from saved predictions**, without training.
2. **Repeat development search and training** on the same dataset and seed list.

Prediction reproduction uses saved model outputs, not a trained-model checkpoint. The supplied sample jobs do not contain model weights or fitted preprocessing objects. Re-running model inference would require those additional artifacts.

## Repository contents

| Path | Contents |
|---|---|
| `confirm_v6/` | Experiment launcher, helpers, configuration files, and complete implementation under `MILD_V6/MILD_unified_repro/` |
| `dataset/` | The exact original dataset used for this release: `dataset.csv`, `events_by_intent.json`, and other retained data/provenance files |
| `repro_30k_search/` | Seven ZIP archives of saved search outputs, configurations, confirmation/classical results and prediction jobs; extract before use |
| `requirements.txt` | Dependencies for the reproduction/training workflow |
| `requirements_full_testbed.txt` | Additional pinned environment export supplied with the release (includes NVIDIA packages) |
| `python-version.txt` | Recorded Python-version information |
| `testbed/` | Additional testbed source; not needed to evaluate the saved data |

The following saved-run paths must be preserved:

- `repro_30k_search/confirm_A/`: results for MILD, independently selected MLP, LSTM, and MLP with MILD's settings.
- `repro_30k_search/confirm_B/`: results for MILD, LSTM, and MLP with MILD's settings (named MLP).
- `repro_30k_search/classic/`: independently tuned classical baselines (LR-OvR, WKPI-Tuned, Dist-Target), including `classic_selected.json`.
- `repro_30k_search/frozen/`: the configurations selected for this archived run.
- `repro_30k_search/dev_search/`: development search results and selection records.
- `repro_30k_search/development_data/`: retained development slice, if available.

Use the regenerated metrics-only reports for this release. Historical logs can retain old analysis text; no historical significance results are used by the workflows below. No significance tests or model win/loss/tie counts are computed by these workflows.

## Seeds and experiment identity

The supplied original execution log reports **10 confirmation seeds: 42,43,44,45,46,47,48,49,50,51**. The search candidate generator uses seed 7. Neural search/reranking uses seeds 42,43,44. Classical baselines are deterministic and run once with seed 42.

The supplied log reports 30,181 input rows. Preserve the exact original input files and row order. Search begins from the first 10,080 rows; the confirmation script applies its chronological split and pilot-row exclusion checks. The launcher defaults to 14,400 previously examined pilot rows. Do not change these settings when reproducing this archived experiment.

Reports include `seeds_used.csv`, listing the actual successful seed count and exact seed list for each method. Actual saved jobs take precedence over defaults when identifying which runs are available.

**Manuscript consistency:** this documented archived run has ten seeds. If the paper table uses fifteen seeds or another dataset/configuration selection, release that corresponding archive and use its actual seed list. Do not identify this ten-seed run as a reproduction of a fifteen-seed table without checking the archived results.

## 0. Extract the saved results before running

All commands below must run from **`Experiment with O-RAN testbed/`**, the directory containing this README, `confirm_v6/`, `dataset/` and `repro_30k_search/`. Quote the path when changing directories because its name contains spaces:

```bash
cd "/your/local/path/ExtendedMILD/Experiment with O-RAN testbed"
```

Replace the example with your actual local path. Download or clone the complete experiment directory first, including **all seven ZIPs** in `repro_30k_search/`:

| ZIP | Restored folder |
|---|---|
| `confirm_A_part01.zip` and `confirm_A_part02.zip` | `repro_30k_search/confirm_A/` (combined) |
| `confirm_B.zip` | `repro_30k_search/confirm_B/` |
| `classic.zip` | `repro_30k_search/classic/` |
| `dev_search.zip` | `repro_30k_search/dev_search/` |
| `development_data.zip` | `repro_30k_search/development_data/` |
| `frozen.zip` | `repro_30k_search/frozen/` |

The two confirmation-A files are **independent ordinary ZIPs** containing different files under the same `confirm_A/` directory. Extract both; do not concatenate them or create separate `confirm_A_part01/` and `confirm_A_part02/` directories.

### Automatic extraction and basic checks

Run the block below once using Python 3 (no extra packages are needed). It extracts all archives into the correct folders, whether an archive includes its named top-level folder or just that folder's contents. Existing extracted files with the same paths will be overwritten; original ZIPs remain unchanged.

```bash
python3 - <<'PY'
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

root = Path.cwd()
base = root / "repro_30k_search"
archives = [
    ("confirm_A_part01.zip", "confirm_A"),
    ("confirm_A_part02.zip", "confirm_A"),
    ("confirm_B.zip", "confirm_B"),
    ("classic.zip", "classic"),
    ("dev_search.zip", "dev_search"),
    ("development_data.zip", "development_data"),
    ("frozen.zip", "frozen"),
]
missing = [str(base / name) for name, _ in archives if not (base / name).is_file()]
if missing:
    raise SystemExit("Download the missing archives first:\n" + "\n".join(missing))

for name, folder in archives:
    with ZipFile(base / name) as z:
        files = [m for m in z.infolist() if not m.is_dir()]
        if not files:
            raise SystemExit(f"Empty archive: {name}")
        paths = [PurePosixPath(m.filename) for m in files]
        if any(p.is_absolute() or ".." in p.parts for p in paths):
            raise SystemExit(f"Unexpected archive paths: {name}")
        if all(p.parts[:2] == ("repro_30k_search", folder) for p in paths):
            destination = root
        elif all(p.parts[0] == folder for p in paths):
            destination = base
        else:
            destination = base / folder
        bad = z.testzip()
        if bad:
            raise SystemExit(f"Damaged ZIP {name}: {bad}")
        z.extractall(destination)
        print(f"Extracted {name}")

required = [
    root / "dataset/dataset.csv",
    root / "dataset/events_by_intent.json",
    root / "confirm_v6/reproduce_v6.sh",
    root / "confirm_v6/MILD_V6/MILD_unified_repro/confirm_v6.py",
    root / "confirm_v6/MILD_V6/MILD_unified_repro/recompute_saved_jobs.py",
    base / "classic/classic_selected.json",
]
missing = [str(p) for p in required if not p.is_file()]
for run in ("confirm_A", "confirm_B", "classic"):
    jobs = base / run / "jobs"
    specs = list(jobs.glob("*/*/spec.json"))
    if not specs:
        missing.append(str(jobs / "<method>/<job-id>/spec.json"))
    elif not list(jobs.glob("*/*/**/folds/fold1_timeseries.csv")):
        missing.append(str(jobs) + " (saved fold predictions missing)")
if missing:
    raise SystemExit("Check the extracted layout or missing uploads:\n" + "\n".join(missing))
print("Extraction and basic input checks passed. Continue with environment setup and prediction reproduction.")
PY
```

These are basic structure checks, not proof that every expected model/seed is present. Inspect each regenerated `seeds_used.csv` to confirm the actual available seeds. The evaluator also needs the policy-setting `fold*_metrics.json` files and completed job records; keep all files from each archived job together.

Windows users may instead use **Extract All** for each ZIP and copy its contents into the corresponding restored folder in the table, combining both confirmation-A parts. Avoid an extra nested folder such as `confirm_A/confirm_A/`. Run the Linux/WSL commands only after restoring the layout.

## Environment setup

Use a Linux shell (Ubuntu or WSL2). These workflows operate on existing data: Mininet, Ryu, Open5GS and a running testbed are not required.

Use Python 3.11 with its venv support installed. Check `python3.11 --version` first. From the repository root:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip check
python -c "import numpy,pandas,scipy,sklearn,tensorflow,tabulate; print('Environment imports OK')"
export MILD_TRAIN_PY="$PWD/.venv/bin/python"
```

The requirements file is copied from the supplied training implementation. It specifies minimum package versions, not a lockfile of the original runtime. Platform and package differences can affect retraining. CPU TensorFlow is sufficient. For a compatible Linux NVIDIA setup, optionally install GPU dependencies using `python -m pip install 'tensorflow[and-cuda]>=2.12'` and check GPU visibility with:

```bash
python -c "import tensorflow as tf; print(tf.config.list_physical_devices('GPU'))"
```

The release also includes `python-version.txt` and `requirements_full_testbed.txt`. Consult the recorded Python version when recreating that exported environment. The full export includes pinned NVIDIA/CUDA packages and is platform-specific; `requirements.txt` is the default setup above. A supplied export alone does not establish that those exact versions produced the archived results.

## 1. Inspect saved results

Read the archived CSVs and reports under `repro_30k_search/`. Confirmation summary files report means and standard deviations; `final_test_runs.csv` contains individual seed results. Classical results are under `classic/classic_test.csv` and `classic/CLASSIC_REPORT.md`.

The original workflow writes classical results separately from the neural confirmations. Prediction reproduction below combines them into each regenerated report.

## 2. Recompute from saved predictions: no training

### Required files

Each confirmation needs its extracted `jobs/` hierarchy and, if available, `PREREG_RECORD.json`. The classical directory needs `classic_selected.json` and its extracted jobs. Preserve all files within retained jobs, particularly:

| Input | Use |
|---|---|
| `spec.json`, `DONE` | Discover completed experiment jobs and their method/seed/configuration |
| `folds/fold*_timeseries.csv` | Saved prediction scores and MILD gate probabilities |
| `folds/fold*_metrics.json` | Read **only the policy parameters**, including the saved alert thresholds |
| `dataset/events_by_intent.json` | Original ground-truth event labels |

For classical jobs the fold directories are inside the `lr/`, `wkpi/` and `dist/` subdirectories. Old `result.json`, event/day outcome tables and summary CSV values are not read to calculate the new results.

**Complete Section 0 first.** Both confirmation-A ZIP parts must be extracted into the same restored `confirm_A/` folder. The scripts read extracted files, not ZIP contents.

### Run

From the repository root, with the environment active:

```bash
export MILD_TRAIN_PY="$PWD/.venv/bin/python"
bash confirm_v6/reproduce_v6.sh \
  "$PWD/dataset" "$PWD/repro_30k_search" predictions
```

This uses the original saved thresholds; it does not tune them on the test data. It rebuilds alarms, evaluates detection, DA, lead and FP, and writes:

- `repro_30k_search/recomputed/confirm_A/`
- `repro_30k_search/recomputed/confirm_B/`

Each contains `final_test_runs.csv`, `final_test_summary.csv`, `per_intent_summary.csv`, `seeds_used.csv`, `CONFIRM_REPORT.md` and `RECOMPUTE_MANIFEST.json` with hashes of the inputs used. Both reports include the selected classical baselines. Original reports and jobs remain untouched. Repeating this command overwrites the generated `recomputed/` outputs.

Compare generated values against the archived run tables. Detection and lead use the evaluator's macro aggregation; FP is a count of false-alarm steps per 1440 ticks, not necessarily a 24-hour wall-clock rate. Standard deviation is undefined for a single deterministic classical run and is shown as NaN.

This workflow supports completed `confirm` and `classic_test` jobs from this V6 experiment. A missing classical archive causes an error rather than silently omitting those models. Event-label hashes are checked against the original confirmation execution record where available.

## 3. Repeat the full search and training

Use a **new work directory** so cached jobs do not substitute for new computation:

```bash
export MILD_TRAIN_PY="$PWD/.venv/bin/python"
export SEEDS="42,43,44,45,46,47,48,49,50,51"
export PILOT_ROWS=14400

bash confirm_v6/reproduce_v6.sh \
  "$PWD/dataset" "$PWD/reruns/search_10seeds" search
```

The launcher performs:

1. Development slice extraction (first 10,080 rows).
2. Development search with 20 trials per neural method and reranking.
3. Export of the selected configurations and matched-MLP settings.
4. Confirmation A on the held-out part using the ten specified seeds.
5. Confirmation B using the MILD-settings MLP.
6. Classical-baseline development tuning and held-out evaluation.

Outputs are written under `reruns/search_10seeds/`, including separate confirmation and classical reports. No significance tests or win/loss/tie reports are generated by these launcher stages. The codebase retains legacy analysis utilities; the documented commands do not invoke their significance routines.

To resume an interrupted experiment, run the same command with the same work directory. To force genuinely new computation, choose a different unused directory. New hardware/software may produce different selected configurations and scores even with the same seeds; the archived prediction workflow is the direct way to audit the original numeric results.
