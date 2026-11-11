# Revised SDN edge-to-cloud testbed (Task 3)

This folder replaces `sdn_edge_cloud_data_collection`. It keeps the same
architecture (Mininet + Open vSwitch + Ryu + Prometheus + a Python experiment
driver) and the same KPI schema, event taxonomy and labelling rule. It changes
four things.

1. **Topology** (Reviewer 1: "Fig. 7 the topology is very simple"). The single
   three-switch chain becomes a loop-free edge-to-cloud tree. It has two edge
   sites with different uplinks, a metro aggregation switch, a long-haul WAN
   link, a cloud gateway and two cloud racks. Background iperf3 traffic shares
   the WAN with the service traffic. The impairment is now injected on the
   long-haul WAN link, which matches the text of the paper. The original
   injected it on the client access interface `hroot-eth0`.
2. **Realistic services.** `time.sleep()` is gone. API requests compile and
   conflict-check real rule sets. Analytics jobs compute rolling statistics,
   z-scores, percentiles and correlation matrices. Telemetry validates,
   aggregates and spools real device records. The services form a real
   pipeline: telemetry → analytics (with backpressure) → API → Ryu northbound
   REST (it installs real OpenFlow rules).
3. **Measured resources.** Each service runs in its own cgroup with a CPU
   quota and a memory limit. CPU %, RAM % and disk % are read from the kernel
   and from a fixed-size loop-mounted ext4 volume. No formula and no assumed
   capacity is used. If cgroups are unavailable, psutil is used and the run
   manifest records it.
4. **Autonomy and validity.** One command runs preflight, calibration and
   validation, resumable collection, the data-quality gate, training of MILD
   and all baselines, and the reports.

---------------------------------------------------------------------------

## Version 2.1 (12 Oct 2026): fix of the "API jam"

The first real run showed that, from segment 9 on, the API latency was not
measurable any more and the reachability index stayed at 0. Cause and fixes:

* The fault knobs were set through the services' own worker pools. Under a
  strong API fault the reset request timed out, so the fault stayed active.
  **Fix:** every service now serves its knob on a separate control port
  (service port + 1000, own thread). Knobs are sent only when they change,
  with a 1 s timeout, and are re-sent every 25 ticks.
* Saturated requests never completed, so the latency histogram became empty
  (NaN) instead of high. **Fix:** deadline-based load shedding in the API. A
  request that waited more than 2 s is answered 504 at once, and its full
  waiting time is recorded as latency. Overload is now visible as high p95.
* A missing KPI cannot satisfy the failure predicate, so the adaptive severity
  loop raised the API severity again and again (7.5 to 40). **Fix:** episodes
  whose KPI was missing in more than 20 % of ticks are ignored by the
  adaptation. The API scale is bounded to [1.5, 15] and is reset once to the
  calibrated value.
* **Watchdog:** a run of 40 bad ticks (missing KPI or sri < 5) that also
  covers quiet time stops the segment, cuts it before the run, and restarts
  the testbed.
* The tick period now includes the knob update (constant 4.0 s per tick).
* The API audit log and the analytics result store are truncated at each
  segment start, so the storage KPI no longer drifts upwards.
* `quality.merge` drops the remaining rows of an event that had to be dropped
  (gap or jam), so they never become unlabelled "healthy" failures.
* `mild_sdn2/salvage.py` finds jam intervals in segments of version 1 and
  excludes them (originals are kept). It runs automatically.
* New tools: `train_now.py` (train on the data collected so far, with a data
  suitability report and a trust report), `check_collection.py` (read-only
  health check), and `pipeline.py --calibration_from OLD_STATE` (start a new
  state folder with an existing calibration).

---------------------------------------------------------------------------

## 1. What is automated and what you do by hand

| Step | Who | How |
|---|---|---|
| Install system packages, Ryu, the two conda environments | **you, once** | `bash scripts/install_deps_ubuntu.sh` |
| Port checks, cleaning leftovers, starting Ryu/Mininet/services/Prometheus/traffic | script | `pipeline.py` phase 1 |
| Health checks: services, 8 KPIs, traffic success, Ryu REST, actuation path, WAN impairment effect | script | phase 1 |
| Healthy baseline, intent targets, SNET/SRI weight validation, fault-severity calibration | script | phase 2 |
| Long collection in resumable 1-hour segments, with adaptive severity | script | phase 3 |
| Merge + validity/sufficiency checks + traffic statistics | script | phase 4 |
| MILD + 5 baselines: equal-budget tuning on dev data, held-out test, significance | script | phase 5 |
| Read `PIPELINE_SUMMARY.md`, `training/AUTOTRAIN_REPORT.md`, and send them to me | **you** | — |

---------------------------------------------------------------------------

## 2. One-time installation (by hand)

```bash
cd MILD_SDN_testbed_v2
bash scripts/install_deps_ubuntu.sh
```

The script does the following:
* installs mininet, openvswitch-switch, prometheus, iperf3, iproute2 and e2fsprogs;
* stops the Ubuntu Prometheus service, so port 9090 is free;
* creates the conda environment **ryu39** (Python 3.9) with the testbed
  packages and the patched Ryu, exactly as in your earlier set-up;
* creates the conda environment **mild-train** (Python 3.11) with
  TensorFlow + CUDA for your RTX 4060, and prints the GPUs it can see.

Keep `MILD_unified_repro` in the **same parent folder** as this folder. The
training phase uses its `autotrain.py`:

```
parent/
├── MILD_unified_repro/
└── MILD_SDN_testbed_v2/
```

---------------------------------------------------------------------------

## 3. Optional 10-minute dry run without Mininet

This checks the Python side on your laptop: services, exporters, traffic,
calibration, collector and merge. It uses local processes and needs no root.

```bash
conda activate ryu39
python pipeline.py --mock --state /tmp/mild_mock --phase all \
   --set tick_s=1 --set segment_ticks=150 --set target_ticks=300 --set calib_baseline_ticks=25 \
   --set calib_max_iter=2 --set min_total_ticks=100 --set min_events_per_intent=1 --set min_codrift_pairs=0
```

In mock mode the WAN impairment does nothing, and the faults do not need to
cross their thresholds. The dry run only shows that every phase runs.

---------------------------------------------------------------------------

## 4. The real run (one command)

```bash
conda activate ryu39
cd MILD_SDN_testbed_v2
sudo -E env PATH="$PATH" python pipeline.py \
     --state ~/mild_sdn2_state \
     --train_python "$(conda run -n mild-train which python)"
```

* `sudo -E env PATH="$PATH"` keeps the ryu39 interpreter. Mininet needs root.
* The default target is **45,000 time steps** (4 s each, so about 50 hours of
  collection). Calibration adds about 1–1.5 h before that.
* **To stop**, press `Ctrl+C`. The current segment is cut at its last quiet
  time step, the testbed is torn down, and nothing is lost.
* **To resume**, run exactly the same command. Finished phases and finished
  segments are skipped. Calibration is not repeated.
* To collect only for a few hours per session, add `--hours 8`. The script
  stops cleanly after 8 hours, and you resume later with the same command.
* To collect more, raise `--target_ticks` (for example `--target_ticks 60000`)
  and run the same command again.

What happens on screen: a line every minute with the current KPIs and active
events, a QA line after every segment (events, observed failures), and any
automatic severity adjustment.

---------------------------------------------------------------------------

## 5. How validity is guaranteed (self-calibration and self-validation)

**Before collection** (`calibration_report.md`):
* **Healthy baseline (150 steps).** Intent targets are derived and checked.
  The API target is max(300 ms, 3 × healthy p99). The analytics target is
  0.6 × the guaranteed throughput floor. The telemetry target is
  max(400, 4 × healthy p99 + 200). Healthy operation must violate each
  target in at most 1 % of steps, and must not miss KPIs, or the run stops
  with an explanation.
* **SNET/SRI weight validation.** The WAN impairment is swept from 0 up to
  140 ms and 12 % loss. The checks are:
  (a) both scores fall monotonically (Spearman ρ ≤ −0.9 for snet, ≤ −0.8 for sri);
  (b) healthy scores are at least 5 points above the nonlinear thresholds (90 and 85);
  (c) the strongest impairment drives them at least 5 points below those thresholds;
  (d) neither score saturates at mid-severity.
  If (c) fails, the weights are scaled up uniformly (their ratios are kept)
  and the sweep is repeated. The final weights are stored and used by the
  probes. The original weights (1.5 / 0.35 / 0.60 and 100 / 0.15 / 0.50) are
  the starting point.
* **Fault calibration.** One test episode is run per event type. The
  severity is adjusted until the failure predicate is crossed at 45–90 % of
  the window. This guarantees observed failures with a learnable build-up.
  Mimic episodes are checked **not** to cross.

**During collection** (`segments/seg_XXXX/qa.json`, `scale_history.json`): if
fewer than 80 % of the episodes of a type end in an observed failure, its
severity is raised by 15 %. If failures come too early (median crossing
< 35 % of the window), the severity is lowered. Labels always come from the
measured KPIs, never from the schedule.

**After collection** (`dataset/quality_report.md`):
* `observed_fraction_ok`: at least 70 % of the injected episodes of every
  type have an observed failure.
* `healthy_slo_exceedance_low`: at most 2 % of the steps outside event
  windows violate a target.
* `buildup_ok`: the median build-up is at least 4 steps.
* `kpis_vary`: every KPI varies.
* `enough_*`: at least 36,000 steps, at least 250 observed events per
  intent, and at least 150 co-drift pairs.

Training starts only when the dataset is valid and sufficient.

**Labelling.** Failure = the intent predicate holds for 3 consecutive steps
(the original rule). The rule `tq_rise ≥ 150` is removed. In the original SDN
data it made 97 % of the telemetry failures unobservable. With
`label_policy=strict` (default), injected episodes that never failed are not
labelled. They are listed in `events_unobserved.json` and reported.
Missing KPI values are interpolated only across gaps of at most 3 steps;
rows in longer gaps are dropped together with the events they touch.

---------------------------------------------------------------------------

## 6. Training and comparison (phase 5, automatic)

`MILD_unified_repro/autotrain.py` runs on the merged dataset:
1. Chronological split: development set 70 %, held-out test set 30 %. The
   cut points are placed where no event is active.
2. MILD, MLP, LSTM, WKPI-Tuned, Dist-Target and LR-OvR are tuned with the
   **same number of trials (20)** and the **same objective** on the
   development data only. Each search starts from your paper configuration
   (SDN: H = 15, α = 0.7, w_c = 0.6, w_T = 0.3, λ_s = 0.002).
3. The best configuration of each method is evaluated once on the held-out
   test set with seeds 42, 43 and 44. The same FP budget (3/day) applies to
   every method.
4. Paired significance tests are run (McNemar for detection and
   disambiguation, Wilcoxon for lead time and daily FP).
5. `AUTOTRAIN_REPORT.md` gives the verdict in your priority order: FP/day,
   disambiguation, lead time, detection. If MILD does not beat the best
   baseline on a metric, the report says so plainly, with the likely
   reasons and fixes.

The tuning budget is identical for all methods, on purpose. Giving MILD more
trials would make the comparison unfair, and a reviewer could object.

---------------------------------------------------------------------------

## 7. Output files (all under `--state`)

| File | Content |
|---|---|
| `pipeline.log` | full timeline |
| `calibration.json`, `calibration_report.md` | targets, weight validation, fault calibration |
| `segments/seg_XXXX/` | `rows.csv`, `truth.csv` (knobs, audit only), `events.jsonl`, `plan.json`, `meta.json`, `qa.json` |
| `runs/<time>/` | process logs, traffic logs (`traffic/*.jsonl`), `stack_manifest.json` |
| `dataset/dataset.csv`, `dataset/events_by_intent.json` | the merged MILD dataset (same schema as before + `n_missing`, `segment`) |
| `dataset/quality_report.md`, `traffic_summary.json` | validity, sufficiency and **measured traffic rates and payload sizes** |
| `training/AUTOTRAIN_REPORT.md` | results, verdict, significance |
| `training/final_test_summary.csv`, `significance_test_set.csv` | tables for the manuscript |
| `PIPELINE_SUMMARY.md` | one-page summary to send me |

---------------------------------------------------------------------------

## 8. Topology and traffic (for the manuscript)

```
            Edge site A                     Edge site B
     hcliA (users)  hiotA (devices)   hcliB (users)  hiotB (devices)
              [s1 edge-A]                     [s2 edge-B]
       100 Mb/s, 2 ms \                       / 50 Mb/s, 6 ms
                        [s3 metro/WAN aggregation]
                 long-haul WAN: 50 Mb/s, 15 ms, 0.05 % loss   <- impairment point
                        [s4 cloud gateway] --- hroot (Ryu REST, Prometheus, driver)
              1 Gb/s, 0.2 ms /            \ 1 Gb/s, 0.2 ms
                [s5 rack-1: hapi, han]   [s6 rack-2: hte, hsink]
```

| Traffic | Source → destination | Process | Default rate | Size |
|---|---|---|---|---|
| API policy requests | users at A, B → API (POST /work) | Poisson, diurnal ×(1 ± 0.5) | 6 req/s per site | log-normal rule set, median 25 rules (≈3 KB) |
| Analytics dashboard jobs | users at A, B → Analytics (POST /submit) | Poisson, diurnal | 1 req/s per site, mean 5 jobs | ≈0.1 KB |
| Device telemetry | 300 devices per site → Telemetry (POST /ingest) | every 10 s per device, 1-s batches | 30 records/s per site | ≈350 B per record, ≈10 KB per batch |
| Telemetry → Analytics | in-cloud, with backpressure | per aggregated row | ≈3 rows/s | 16 features per row |
| Scheduled inference | inside Analytics | Poisson | 4 jobs/s | — |
| Actuation | Analytics → API → Ryu REST (flow add) | on anomalies | event driven | 1 OpenFlow rule |
| Cross-traffic | hcliB, hiotA → hsink (iperf3 TCP) | diurnal, 8-s sessions | 2 × 8 Mb/s mean | — |
| Probes | each edge site → each service | 5 ICMP (0.2 s apart) + 1 HTTP per second | — | — |

One time step aggregates 4 s of measurements. One emulated day is 1440 steps,
the same convention as FP/day in the paper. The measured values from your
run are in `dataset/traffic_summary.json`. Please quote those in the
manuscript.

---------------------------------------------------------------------------

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `RealStack needs root` | run with `sudo -E env PATH="$PATH" python pipeline.py ...` |
| `missing binaries: [...]` | run `scripts/install_deps_ubuntu.sh` again |
| `Open vSwitch is not running` | `sudo systemctl start openvswitch-switch` |
| port warnings | handled automatically (leftovers are killed, or another port is used) |
| `WAN impairment check failed` | kernel `sch_netem` missing: `sudo modprobe sch_netem` |
| `cgroup scopes not found` | the run continues with psutil (recorded in the manifest); on Ubuntu 22.04+ cgroups v2 normally works |
| `baseline validation failed` | the laptop is overloaded: close other heavy programs, or lower `api_rps_per_site` with `--set` |
| hard crash, processes left behind | `bash scripts/stop_all.sh`, then re-run the same command |
