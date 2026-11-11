# MILD unified reproduction package (Task 2)

This package replaces the three per-dataset code folders
(`codes_statistical_bench`, `code_microservices`, `code_sdn_edge_cloud`) with one
set of Python scripts. The method code was identical across the three folders.
Only the dataset files and the command-line parameters differed. Those
parameters are now stored in a single file, `mild_repro/paper_config.py`.

One command reproduces every result table of the submitted manuscript. It
compares each reproduced number with the paper, runs paired significance tests
of MILD against every baseline, and writes a plain-language report.

---------------------------------------------------------------------------

## 1. What is automated and what you do by hand

| Step | Who does it | Command |
|---|---|---|
| Install Python packages | **you** (once) | `pip install -r requirements.txt` |
| Put the datasets in place and verify them | **script** (`prepare_data.py`) | one command, Section 3 |
| Train and evaluate MILD and the 5 baselines on 3 datasets | **script** (`run_all.py`) | one command, Section 4 |
| Ablation, sensitivity, multi-horizon, SHAP, disambiguation plot | **script** (`run_all.py`) | same command |
| Write all tables as CSV files | **script** | automatic |
| Compare every number with the paper (identical / close / different) | **script** | automatic |
| Significance tests vs every baseline | **script** | automatic |
| Write `REPORT.md` with the interpretation | **script** | automatic |
| Resume after an interruption | **script** | re-run the same command |
| Read `REPORT.md` and copy numbers into the manuscript | **you** | — |

---------------------------------------------------------------------------

## 2. Folder layout

```
MILD_unified_repro/
├── prepare_data.py            # stage + verify the three datasets
├── run_all.py                 # the single entry point (resumable)
├── autotrain.py               # tuning + held-out comparison on new data (Tasks 3, 4)
├── requirements.txt
├── mild_repro/
│   ├── paper_config.py        # ALL settings and ALL reported paper numbers
│   ├── core.py                # folds, features, labels, alert tuning, evaluation
│   ├── models.py              # MILD network + losses, MLP, LSTM
│   ├── runners.py             # MILD, MLP, LSTM, WKPI-Tuned, Dist-Target, LR-OvR
│   ├── opintel.py             # multi-horizon + SHAP, disambiguation figure
│   ├── stats.py               # paired significance tests
│   ├── report.py              # tables, paper comparison, REPORT.md
│   ├── jobs.py                # job list + single-job executor
│   └── generate_stat_benchmark.py   # verbatim copy of generate_hard_dataset.py
├── data/                      # created by prepare_data.py
└── results/                   # created by run_all.py
```

---------------------------------------------------------------------------

## 3. Step-by-step: first run

**Step 3.1. Create an environment (by hand, once).**

```bash
conda create -n mild-repro python=3.11 -y
conda activate mild-repro
cd MILD_unified_repro
pip install -r requirements.txt
```

To use the RTX 4060 on Linux, install the GPU build instead:
`pip install "tensorflow[and-cuda]"`. To check that TensorFlow sees the GPU, run
`python -c "import tensorflow as tf; print(tf.config.list_physical_devices('GPU'))"`.
If the list is empty, everything still runs on the CPU, only more slowly.

**Step 3.2. Stage the datasets (one command).**

```bash
python prepare_data.py \
   --microservices /path/to/dataset_and_events_Microservice.zip \
   --sdn           /path/to/dataset_and_events_RYU.zip
```

The script does the following:
* It regenerates the statistical benchmark with the paper settings (200,000
  minutes, seed 42, probabilities 0.60/0.20/0.20). It then checks that the
  result is the paper dataset: 200,000 rows and 56/53/51 labelled windows.
  The full file is not needed, because the generator is deterministic. I
  verified that its output is identical, value by value, to your partial file.
* It copies the two emulation datasets and checks their SHA-256 hashes against
  the files you sent me.
* It writes `data/data_manifest.json`. Every line of the screen output must
  start with `OK`.

**Step 3.3. Run everything (one command).**

```bash
python run_all.py
```

Default groups: `main,ablation,sensitivity,opintel`. To reproduce only the main
comparison tables (Tables V–XI) first, run:

```bash
python run_all.py --groups main
```

**Step 3.4. Read the results.**

* `results/REPORT.md` is the summary. Read this first.
* `results/tables/*.csv` contains one CSV per manuscript table (list below).
* `results/significance/<dataset>_significance.csv` holds the significance tests.
* `results/progress.json` shows which jobs are done or failed.
* `results/orchestrator.log` is the timeline. Each job also has its own `job.log`.

---------------------------------------------------------------------------

## 4. Stopping and resuming

Press `Ctrl+C` at any time. Then run **the same command** again.

* A finished job has a `DONE` file in its folder and is skipped.
* An interrupted job is restarted from its first fold. This keeps the random
  stream identical to the original scripts, which seed once per run.
* `--fold_resume` restarts an interrupted job at its first unfinished fold
  instead. This is faster, but later folds may then differ slightly from an
  uninterrupted run.
* Tables are rebuilt after every finished job, so partial results are always
  visible.

Useful commands:

```bash
python run_all.py --list                          # plan and status of every job
python run_all.py --datasets sdn --groups main    # one dataset, one group
python run_all.py --report_only                   # rebuild tables/report only
python run_all.py --groups seeds                  # repeat main runs, seeds 43-46
```

---------------------------------------------------------------------------

## 5. Output tables and the manuscript tables they reproduce

| CSV file | Manuscript |
|---|---|
| `statistical_mild_per_intent.csv` | Table V |
| `statistical_comparison.csv` | Table VI |
| `microservices_mild_per_intent.csv` | Table VII |
| `microservices_comparison.csv` | Table VIII |
| `sdn_mild_per_intent.csv` | Table IX |
| `sdn_comparison.csv` | Table X |
| `cross_dataset.csv` | Table XI |
| `<dataset>_ablation.csv` | Tables XII–XIV |
| `<dataset>_ablation_consistent.csv` | new: ablation around the main configuration |
| `<dataset>_sensitivity.csv` | Tables XV–XVII |
| `multihorizon.csv` | Sec. V-E3 (128 / 97 / 42 min) |
| `paper_vs_reproduced_ALL.csv` | every compared cell with its status |
| `<dataset>_seed_summary.csv` | new: mean ± std over seeds (group `seeds`) |

Each comparison table also contains these **extended** columns, which are not in
the paper but help with the reviewers:
* `FP episodes/day` counts contiguous false-alarm runs. The paper's FP/day
  counts alert time steps instead.
* `Disamb. incl. nonlinear roots` also scores the `nonlinear_*` root events of
  the emulation datasets.
* `missed events (FN)` is the number of missed failures (Reviewer 2,
  comment 3).

Each `disamb.png` figure is saved inside the MILD run folder
(`results/runs/<dataset>/mild/<hash>/fold<k>_disamb.png`). The SHAP figures and
tables are saved in the multi-horizon run folder.

---------------------------------------------------------------------------

## 6. How "identical" is decided

Every reproduced cell gets one of four labels:
* **IDENTICAL**: the absolute difference is at most 0.005, so the value is
  equal at the two printed decimals.
* **CLOSE**: within 2 percentage points (detection, disambiguation), within
  2 min (lead time), within 1 FP/day, or within 5 % of the paper value.
* **DIFFERENT**: outside these limits.
* **MISSING**: the job has not finished yet.

Bit-identical neural results are not expected on a different TensorFlow
version, or on GPU versus CPU, because the order of floating-point operations
changes the trained weights. The classical baselines (WKPI-Tuned, Dist-Target,
LR-OvR) do not depend on TensorFlow, so they should come out IDENTICAL. If they
do not, the data differ. Run the `seeds` group to see how much the neural
models vary between runs. A gap smaller than that spread is run-to-run noise.

---------------------------------------------------------------------------

## 7. Significance tests (what is computed)

Every model is evaluated on the same test events and the same test days, so all
tests are paired.

| Metric | Unit of analysis | Test |
|---|---|---|
| Detection | each labelled failure event | exact McNemar |
| Disambiguation | each root-cause event (paper rule) | exact McNemar |
| Lead time | events detected by both models | Wilcoxon signed-rank |
| FP per day | each test day (1440 steps) | Wilcoxon signed-rank |
| Fold-level (statistical benchmark only, 10 folds) | each test fold | Wilcoxon signed-rank |

The p-values are Holm-corrected over the five baselines. Each row also reports
the paired difference with a 95 % bootstrap confidence interval and a verdict:
`MILD significantly better`, `baseline significantly better`, or
`no significant difference`. Fold-level tests are not possible on the
emulation datasets, because "3-fold" blocked CV evaluates only two test blocks.

---------------------------------------------------------------------------

## 8. Faithfulness to the original code (read before quoting results)

The scripts reproduce the original behaviour. They do not correct it. These
original behaviours are kept on purpose, and each is documented in
`REPORT.md`:

1. Expanding-window blocked CV. The first block has no history and is skipped.
   So "11 folds" evaluates 10 blocks, and "3 folds" evaluates 2 blocks.
2. The classical baselines train on labels built with H = 120 for every
   dataset. They tune their EWMA thresholds on the training scores.
3. MLP and LSTM use H = 50 on the SDN dataset, while MILD uses H = 15. This
   follows your run commands. The test metric is the same for all models,
   because it is computed against the annotated failure times.
4. On SDN, the notebook launched the ablation variants and the w_c sweep with
   α = 0.6, while the main result uses α = 0.7. Group `ablation` reproduces
   the notebook. Group `ablation_consistent` re-runs every variant around the
   main configuration.
5. The multi-horizon run uses horizons 120/50/20, as in the notebook. The
   manuscript text says 120/60/20.

Two compatibility changes do not alter any computation. First, the code runs
on both Keras 2 and Keras 3. Second, the gate-selection `Lambda` binds its
index explicitly. Under Keras 3 the original late-binding lambda would make
every expert use the last intent's gate value.

---------------------------------------------------------------------------

## 9. `autotrain.py`: training on newly collected data (used by Tasks 3 and 4)

The two testbed pipelines call this script automatically after collection.
You can also run it on any dataset in the MILD format:

```bash
python autotrain.py --data_csv <dataset.csv> --events_json <events_by_intent.json> \
                    --out <folder> --paper_profile sdn --trials 20 --fp_budget 3 --seeds 42,43,44
```

1. The data are split in time: 70 % development, 30 % held-out test. Each cut
   is placed where no event is active.
2. MILD and the five baselines are tuned on the development data only, with
   the **same number of trials** and the **same objective** for every
   method. The objective gives FP/day and disambiguation the highest weight,
   then lead time, then detection. Every search includes the paper
   configuration of the method.
3. The best configuration of each method is evaluated once on the test set
   with several seeds. Then come the paired significance tests.
4. `AUTOTRAIN_REPORT.md` gives the verdict in your priority order. If MILD
   does not win a metric, it says so, with diagnostics and fixes.

Every trial is a resumable job (`jobs/<method>/<hash>/DONE`).

---------------------------------------------------------------------------

## 10. Approximate run time (i7-13650HX, RTX 4060)

| Group | Jobs | GPU | CPU only |
|---|---|---|---|
| main (3 datasets) | 12 | ~1.5–3 h | ~5–8 h |
| ablation + ablation_consistent | ~25 | ~3–5 h | ~10–15 h |
| sensitivity | ~25 | ~3–5 h | ~10–15 h |
| opintel | 3 | ~0.5 h | ~1–2 h |

The microservices dataset (453,600 rows) dominates the run time. The LSTM on
that dataset is the slowest single job.

## Additions in v2.4 (autotrain)

* `--rerank_top K --rerank_seeds 43,44`: the K best search trials of every neural
  method are re-run with extra seeds on the same development validation period,
  and the configuration with the best mean score is kept (`search_rerank.csv`).
* Significance is computed for every final seed (`significance_by_seed.csv`) and
  combined by a consensus rule: a verdict needs at least two thirds of the seeds
  and no seed with a significant opposite result (`significance_test_set.csv`).
* `opcurve.py` compares all methods at the same false-alarm rate. It re-places
  each method's thresholds on the saved test scores (no retraining) and repeats
  the paired tests (`opcurve/OPCURVE_REPORT.md`). autotrain runs it at the end;
  it can also be run on any earlier training folder.
