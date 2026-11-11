# V2.8-dev — development-only architecture/calibration study (2026-11-09)

This opt-in change implements the V6 handoff items 1–4. Read `../README_V6.md` and
`../DEVELOPMENT_REPORT.md`. No collection scenario or legacy default is changed.

- `gate_use=attr_only` separates alarm scoring from root-cause gate scaling.
- Development-only search includes multiply/residual, scale/attr_only and teacher
  on/off within the same 20-candidate budget as MLP/LSTM. Shared alarm-loss options
  are available to every neural method.
- `protocol_v6` supports chronological OOF calibration with earlier-only fitting
  and early stopping, one shared causal feature matrix and an explicit no-alert
  threshold fallback. An opt-in classical path supports the same calibration modes.
- `devcompare.py --protocol_v6` uses a physically restricted development snapshot,
  writes immutable candidate plans, re-ranks the top three with two additional seeds,
  and never runs the full-data held-out phase.
- Reports distinguish FP steps/1440 ticks from physical days, and historical actionable
  detection from correct-cause alerts that meet the >=10-tick lead requirement.
- The published gate/input architecture is retained as a diagnostic anchor. The
  original focal-loss broadcast is retained, disclosed and offered to baselines.

The results, complete negative outcomes and selected configurations accompany this
package. They are exploratory development results, not new held-out evidence.

---

# MILD on O-RAN, version 2.7 (srsRAN gNB + E2 agent, FlexRIC nearRT-RIC, 3 srsUEs, Open5GS, WSL2)

## Changes in 2.7 (2026-11-08): intent-specific false alarms, scenario v5, paper protocol as default

- `--fp_scope own` (autotrain, opcurve, pilot_check; default of `mild-oran train`): an alert of intent k is a
  false alarm unless intent k itself has an event window at that time. Up to 2.6 (`--fp_scope any`, the paper
  definition) an alert counted as false only outside the windows of EVERY intent, so alarms raised for the wrong
  tenant during another tenant's episode were free. Used for threshold calibration, evaluation and the equal-FP
  report, identically for every method.
  Check on the 32.4k v4 data (tuned configurations of the user's run, 3 seeds): fp_scope own -> MILD Det 77.0 /
  DA 77.5 / FP 6.6; MLP 59.2 / 65.0 / 6.4; LSTM 49.2 / 58.3 / 2.6 (fp_scope any: 84.8 / 79.2 / 8.8 vs
  86.5 / 65.8 / 8.9 vs 77.0 / 58.8 / 3.3).
- Scenario v5 (`--set scenario=v5`) = v4 + an API precursor (the API pre phase climbs to at least 45 % of the API
  scale, knee_floor; on the testbed v4 API faults had no early signal and every method detected ~60 %) + half of
  the episodes co-drift (multi-tenant contention).
- `mild-oran train` defaults are now the paper protocol: 10 seeds, `--mild_arch v26 --baseline_features onset
  --val_frac 0.4 --thr_budget 1 --variants tuned --fp_scope own`. pilot_check uses the same protocol.

## Changes in 2.6 (2026-11-08): identical inputs for MILD, MLP and LSTM; published gate

- Up to 2.5 the MLP saw only the current base KPIs, the LSTM the last 20-40 steps of them, while MILD also got
  rolling statistics and (since 2.3) change features (d1, d3, z60). Same data, different inputs.
- `--baseline_features onset` now gives the MLP AND the LSTM exactly MILD's inputs.
- `--mild_arch v26`: the published gate (expert x g) with the change features. The (1+g) gate of 2.3-2.5 did not
  help on the 30k v4 data (5 seeds: detection -2.0, lead -1.8 steps vs the published gate).
- `ablation.py`: one-change-at-a-time ablation with the tuned configurations of a finished run (no new search).
- Check on the 30k data, full autotrain (6 trials, 3 seeds), `--mild_arch v26 --baseline_features onset
  --val_frac 0.4 --thr_budget 1`: MILD Det 86.9 / DA 80.3 / FP 5.0 / lead 22.1; MLP 86.6 / 63.2 / 4.9 / 19.8;
  LSTM 79.2 / 58.3 / 6.5 / 19.0. MILD significantly better in DA and lead (common detections) than every baseline;
  detection equal to the MLP; FP not significantly different.

## Changes in 2.5 (2026-11-07): threshold calibration (training only, no new data)

- Diagnosis on the 30k v4 testbed run: 135 of 140 MILD false-alarm steps (seed 42) fall on look-alikes
  (mimic_partial_analytics, mimic_network_only), which on the testbed degrade the KPI as far as a real fault
  before failure. Thresholds were placed on the last 20 % of each training period (2.9 days, 3 partial-analytics
  look-alikes vs 6 in the test part), so every learned model exceeded its budget 5-16x, and the search gave MILD a
  looser per-intent budget (2) than MLP/LSTM (1).
- New autotrain / `mild-oran train` options, same for every method:
  `--val_frac 0.4` (validation part used for early stopping and threshold placement; paper 0.2) and
  `--thr_budget 1` (per-intent threshold budget, fixed in every variant instead of searched; 1 x 3 intents = the
  operational 3/day). Defaults are unchanged, so earlier runs reproduce.
- Check on the 30k testbed data, full autotrain (6 trials, 3 seeds): main protocol MILD Det 83.5 / DA 78.9 /
  FP 4.2 per day / lead 19.4 ticks; MLP 65.2 / 49.6 / 6.1 / 18.2; LSTM 77.6 / 50.0 / 8.6 / 17.0 (before: MILD FP
  22.8, MLP 11.3, LSTM 13.6). MILD significantly better than every baseline on detection, DA and lead (Holm).

## Changes in 2.4 (2026-11-06): scenario v4 "capacity knee" (Path B)
Why: in v2/v3 a fault's own intent KPI degraded early and steadily, so one fixed threshold on it detected most
failures in time and lead was capped by short build-ups (13-19 ticks, convex onsets): learned models could not
show their value on detection and lead.
- Scenario v4 (`--set scenario=v4`; v2/v3 unchanged): 2-s ticks and 4-s KPI windows (2x data per hour; target
  30,000 ticks ~ 17 h); 24-min segments with 8 failures + 4 look-alikes; every fault climbs LINEARLY for 26-38
  ticks while the xApp still keeps up (the intent KPI stays healthy; CPU, the cause's KPIs and the network move)
  and collapses within 3-5 ticks once the xApp reaches its capacity knee. Median build-up ~37 ticks.
  Co-drift victims fail a few ticks after their cause and are labelled from the cause's start.
  Look-alikes: partial ramps that stop at 36-60 % of the knee (mimic_level 0.6), tenant-demand creeps (CPU rises
  with the cell load), network-only dips, short surges. See scheduler.V4, build_plan_v4, hockey().
- Calibration step 2b (v4 only): capacity-knee scan per xApp (knob 1 -> kmax over 60 ticks): knee = knob where
  the intent KPI moves half-way out of its healthy band for 3 ticks; fail = knob where it crosses the target.
  scale = 1 + 1.6 x (fail - 1); kneek_<x> stored in the scales (calibration_report.md, section knee).
- load_pct (= E2SM-KPM RRU.PrbTotDl) is written to the v4 dataset and is a model input of EVERY method
  (core.feature_base), because tenant demand also moves the xApps' CPU.
- pilot_check.py: headroom is now judged on ACTIONABLE detection of the one-KPI threshold (alert >= 20 s before
  the failure): PASS < 50 %, WARN 50-70 %, FAIL >= 70 %.
  Below 12,000 rows the MILD-vs-MLP/LSTM comparison is INFORMATION ONLY and the verdict uses the headroom alone
  (on the v4 simulator the same process gives NO-GO at 7,200 ticks, GO WITH CAUTION at 14,400 and a clear MILD
  win at 26,600: small pilots starve MILD). The main-protocol FP/day is now shown in the comparison.
- MILD architecture: v2.3 (residual gate + onset features) stays the default; a slow-trend feature set
  (`features: trend`) was tested and is NOT used (worse on the v4 simulation).
- fakestack.FakeStackV4: simulator of v4 fitted to the real v3 traces (self-test and design only).
- autotrain: when the median build-up is >= 2 x the paper horizon, MILD, MLP and LSTM start from a horizon of
  0.8 x the median build-up (v4: 30 ticks); the search still tries the build-up quantiles (same grid for all).
- opcurve: new table "False alarms needed for equal detection" (fp_at_equal_detection.csv).
- Simulation check (simulated v4, 26.6k ticks, full protocol, 3 seeds, held-out test part):
  main protocol MILD Det 99.0 / DA 83.3 / FP 5.0 per day / lead 22.2 ticks; MLP 90.5 / 56.1 / 9.7 / 24.5;
  LSTM 92.9 / 31.8 / 20.5 / 25.9. At equal FP 3/day: MILD Det 97.7 vs 89.2 (MLP) and 85.4 (LSTM), DA 81.8 vs
  55.1 and 34.8, lead 24.2 vs 22.5 (MLP, significant in 3/3 seeds) and 24.4 vs 23.2 (LSTM, 1/1 seeds).
  FP/day needed to detect 90 % of failures: MILD 0.72, MLP 9.9, LSTM 17.8. A simulation is not the testbed:
  the v4 pilot gate decides.

## Changes in 2.3.1 (2026-11-06)
- netem.py: preflight on a fresh state (RAN down, no UE namespace yet) no longer fails with "Cannot find device
  tun_srsue" (the fix the user applied by hand during the v3 pilot).
- pilot_check.py: training runs with nice 10 and 4 threads, so `mild-oran pilotcheck` can run next to a collection.

## Changes in 2.3 (2026-11-05): MILD v2.3 model + scenario v3 + pilot gate
Root cause of "MILD does not beat MLP/LSTM on detection/lead" on the 36k v2 dataset: the data. A single fixed
threshold on the failing intent's own KPI detects 96.6 % of the test failures (pilot_check.py headroom check),
so detection and lead are saturated for every competent model; only disambiguation has headroom.
A harder scenario then exposed a MILD design flaw: the gate (trained on root-cause labels) MULTIPLIED each
intent's expert, so during a co-drift it silenced the risk head of the failing victim intent.
- MILD v2.3 (autotrain/train_now --mild_arch v23, the default; --mild_arch paper = published MILD):
  residual gate expert_i x (1 + g_i); onset features (d1, d3, z60 of six KPIs) next to the rolling statistics;
  head distillation weight searched in 0.5-1.0 (start 1.0; the teacher still feeds and supervises the gate).
  Developed on development data only (36k dev part and simulator); the 36k test part was not used.
- Scenario v3 (`--set scenario='"v3"'`, opt-in; default stays v2): tenant demand wave + short surges, convex onsets,
  longer co-drift lags, 6 failures + 6 look-alikes per segment (incl. network-only and partial co-drift mimics),
  balanced intents. The demand also runs during calibration (targets include it). See scheduler.V3.
- pilot_check.py / `mild-oran pilotcheck`: headroom check + MILD vs MLP/LSTM on a pilot -> GO / NO-GO.
- --baseline_features onset: fairness ablation (MLP with MILD v2.3's input features).
- MILD_unified_repro/devcompare.py: dev-only model comparison (inner validation period, equal FP).

## Changes in 2.2.1 (training only, 2026-11-02; collection unchanged)
- BUG FIX, equal-FP comparison (opcurve.py): it read every cached final job in training/jobs/, including the jobs of
  EARLIER datasets (same role names), and kept whichever it found first. The 27k-row report of 2 Nov mixed test
  periods of the 26k, 27k and older datasets (see n_events in opcurve_runs.csv: 233 and 245 in one table), so its
  numbers, T4 (seed spread at equal FP) and T10 were not valid. autotrain now writes training/final_jobs.json (the
  final jobs of this run); opcurve.py and the trust report read only those.
- Threshold rule of the TUNED variant of every method (autotrain --thr_rule, default expected_lead): maximise
  detection rate x mean lead (a missed event counts 0). The paper's rule maximises the mean lead over the detected
  events only and can pick a threshold that misses half of the events (MILD tuned, seed 42: API detection 44-48 %).
  The paper variant keeps the paper rule. --thr_rule lead restores the 2.2 behaviour.

## Changes in 2.2 (after the first full training, 2026-11-02)
- `ram_pct` is now the memory use above its own 60-tick running minimum in the same segment
  (`mild_oran/kpi_norm.py`). The raw value carried the memory history of the processes: on the first O-RAN
  dataset it stepped from 8.6 % to 6.8 % at one process restart (seg_0051), which shifted the healthy level by
  2.9 IQR between the development and the test part (suitability FAIL) and made the models fail on the test
  part (MILD tuned detection 96.7 -> 72.2 %). The raw value does not respond to any fault. The change is applied
  when the dataset is built (quality.py), so every segment collected so far is used as it is; the raw value is
  kept as the context column `ram_raw_pct` (not a model input). The live demo (deploy.py) applies the same rule.
- The dataset folder has a new file `mimics.json` (benign mimic windows, from each segment's plan.json; not labels).
  TRUST_REPORT.md has a new section "Where the false alarms of MILD (tuned) come from": the share of false-alarm
  steps during benign mimics, in recovery tails and in plain healthy time, and a table that compares the
  threshold-calibration period (MILD jobs now also write folds/fold1_val_timeseries.csv) with the test part.
  On the first dataset (20,449 rows, after the ram_pct change) every false alarm of MILD (tuned) was an alert on
  a benign mimic; the calibration period had 1.99 days and 20 mimics, so its FP estimate is biased low. The
  remedy is a longer development part (more data); no protocol setting was changed.
- New option `balanced_intents` (default false, so new segments stay comparable with the old ones): with the
  default 10 events per segment the scheduler gives every segment 4 nonlinear (2 of them API), 4 co-drift and 2
  independent (API + Analytics) episodes; Telemetry fails only as a co-drift victim or a nonlinear episode, and
  the 3 mimics are API, Analytics and Telemetry mimics (no network-only mimic). State this in the paper.

## Changes in 2.1 (after the first live run, 2026-10-31)
- E2SM-RC "Slice-level PRB quota" control now uses the parameter layout srsRAN decodes (IDs 1,2,3,5,...,13, every
  element KeyFlagFalse); the IDs are read from the gNB's own RAN function definition when it advertises them.
  v2.0 used IDs 1..12: srsRAN never answered the control, its E2 agent stopped answering anything else, and the
  gNB died about a minute later.
- mild_e2gw reports a request the E2 node never answered as a timeout (FlexRIC v3.0.0 returns "success" after 6 s)
  and exits with code 5; xRC counts only real CONTROL ACKs and stops sending controls after a timeout; the
  collector restarts the RAN when that happens.
- The E2SM-RC check is strict; when it fails: one clean RAN restart and a second check, then the KPM-only gNB
  config (xRC in shadow mode). Calibration restarts the RAN by itself when it goes down (up to 3 times) and checks
  that the gNB survived the plant step. v2.0 plant / RC results are redone automatically.
- xKPM sends one subscription command at a time. New: `mild-oran diag` (one archive with every log, incl. the
  gNB's own log file); `mild-oran ran-config` also turns on the gNB's E2AP log (info).

The complete procedure (use case, installation, every command with its expected output, what can go wrong
and what to do) is in the runbook document "MILD on O-RAN: use case and runbook (v2)". This file is only a
map of the package.

```
MILD_ORAN/
  install.sh               one-time installer (venv, packages, TensorFlow, mild-oran command)
  bin/mild-oran            the command you use (installed into ~/intact/bin)
  pipeline.py              collection: preflight, RAN up, components, calibration, segments, QA, self-repair
  check_collection.py      read-only health table (mild-oran status / watch)
  train_now.py             snapshot + dataset + suitability + MILD/baselines training + trust report
  config/oran.json         every setting (override with --set key=value)
  c/                       mild_e2gw: the xApps' FlexRIC E2 front end (C), tests; built by mild-oran build-xapps
  mild_oran/
    ranctl.py              the RAN in the testbed order: intact-down, core, RIC, jrtc, gNB, proxy, UEs; restarts
    stack.py               MILD components on top of the RAN, UE identity map, knobs, E2 rotation
    kpi.py                 the 8 KPIs and the context columns
    collector.py           ticks, watchdogs, labels, segments, segment QA, salvage
    remedy.py              mini-QA every 5 min, remedy ladders, escalation
    calibrate.py           plant, E2SM-RC check, baseline, weights, fault severities
    quality.py             dataset merge (whole + salvaged + archived attempts) and quality report
    learnability.py        hourly learnability probe
    scheduler.py           fault episodes and knobs
    kpi_norm.py            v2.2: ram_pct above its running minimum (no memory history)
  pilot_check.py           v2.3: go / no-go of a pilot dataset (mild-oran pilotcheck)
    preflight.py           automated checks
    xapps/xkpm.py          KPM monitor xApp       -> Telemetry intent (backlog)
    xapps/xsla.py          SLA analytics xApp     -> Analytics intent (jobs/s)
    xapps/xrc.py           policy / E2SM-RC xApp  -> API intent (p95 latency)
    xapps/loadgen.py       tenant / operator clients
    xapps/common.py        shared parts, E2Link (supervises one mild_e2gw per xApp)
    ran/ue_agent.py        probes inside each UE namespace
    ran/traffic.py         DL traffic of all UEs
    ran/appserver.py       edge app 10.45.0.1:18080
    ran/fake_ran.py, ran/fake_e2gw.py   stand-ins for the mock test only
  MILD_unified_repro/      the paper's training and evaluation code
```
