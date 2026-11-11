# Build the O-RAN testbed and collect a dataset

This is optional. The dataset of the paper and regeneration of results need no testbed. Follow this guide only if you want to collect new data with the same design.

The guide has two parts:
- **Part I** builds the testbed. It follows `INTACT_testbed_runbook.md` (in this folder), which gives every command, its expected output, and a fix for each known failure.
- **Part II** installs the MILD package and collects scenario v5.

## What you need

| Item | Requirement |
|---|---|
| OS | Windows 11 with WSL2 and Ubuntu 24.04. A native Ubuntu 24.04 also works; skip the Windows steps of Part 1 of the runbook. |
| CPU | 8 or more cores. The gNB, three srsUEs, the ZMQ broker, the RIC and the xApps run at the same time. |
| RAM | 16 GB in the PC. Give WSL 12 GB: `memory=12GB` in `%UserProfile%\.wslconfig`, then `wsl --shutdown`. With less memory, the RAN processes are killed during the collection. |
| Disk | 60 GB free (builds and data). |
| Kernel | SCTP support (needed by E2). The runbook's preflight checks it, and Appendix A builds a WSL kernel with SCTP if it is missing. |

Versions used in the paper:
- **gNB:** srsRAN Project 25.10 (the jbpf fork at commit `cd6811af`), with its E2 agent (E2AP v3, E2SM-KPM v3);
- **UEs:** srsUE from srsRAN 4G;
- **RIC:** FlexRIC v3.0.0, built with the E2AP v2.03 codec and E2SM-KPM v3.00;
- **Core:** Open5GS 2.8.

The runbook pins every repository to a commit and checks them with `intact-src-verify`.

## Part I: the testbed (from `INTACT_testbed_runbook.md`)

Work through these sections in order. Each section ends with a check, and the next section assumes that check passed.

| Order | Runbook section | Needed for MILD? | What it gives |
|---|---|---|---|
| 1 | Part 0 | read only | why each design choice was made |
| 2 | Part 1 | yes | the WSL distro `intact` and the first snapshot |
| 3 | Part 2 | yes | `~/intact`, the environment file `~/intact/env.sh`, the checkpoint tool |
| 4 | Part 3 | yes | `intact-preflight`. If it reports `SCTP NOT available`, do Appendix A first |
| 5 | Part 4 | yes | the Open5GS core, `intact-core-health`, `intact-core-restart` |
| 6 | Part 5 | yes | the sources, pinned to known commits |
| 7 | Part 6 | yes | builds of the gNB, srsUE and jrt-controller (the gNB build includes jbpf; MILD does not use jbpf, but the launch scripts expect it) |
| 8 | Part 7 | yes | the gNB, UE and core configuration files |
| 9 | Part 8 | yes | `intact-up`, `intact-down`, `intact-health` and the launchers |
| 10 | Part 9 | yes | the first attach with traffic. **This gate must pass.** |
| 11 | Part 9b | yes | the telemetry setup. Among other changes, it sets the gNB inactivity timer to 7200 s, so that UEs are not released |
| 12 | Part 10, steps 10.1 and 10.2 | yes | the slices (the UEs use slices SD 1 and 2) and the NSSAI fix for Open5GS |
| 13 | Part 10, steps 10.3 to 10.6 | no | live PRB control through jbpf (not used by MILD) |
| 14 | Appendix E | yes | three UEs at once through a ZMQ broker (`intact-multiue`) |
| 15 | Appendix F | yes | FlexRIC near-RT RIC, the gNB config with E2 (`gnb_zmq_jbpf_e2.yaml`), `intact-run-ric` |
| 16 | Appendices B, C, D | no | experiments that are not part of this paper |
| - | Part 11 | when needed | troubleshooting and the diagnostics bundle |

When Part I is done, these must work:

```bash
source ~/intact/env.sh
intact-core-health          # CORE READY
intact-health               # one screen with the state of every layer
```

The Appendix F run must also show `Accepting RAN function ID 2` in `~/intact/logs/run/ric.out`.

## Part II: the MILD package and the scenario v5 collection

All commands run in the `intact` distro. Every new terminal starts with `source ~/intact/env.sh`.

**Step 1. Copy the package to its expected place.** The `mild-oran` command expects the package at `~/intact/mild/MILD_ORAN`.

```bash
mkdir -p ~/intact/mild
cp -r <repo>/oran/code/MILD_V6 ~/intact/mild/MILD_ORAN
bash ~/intact/mild/MILD_ORAN/install.sh
```

Expected: `INSTALL DONE.` The script creates `~/intact/mild/venv`, installs TensorFlow (CPU) and puts `mild-oran` into `~/intact/bin`.

**Step 2. Build the E2 front end of the xApps.** It is built in your FlexRIC tree and runs two self-tests.

```bash
mild-oran build-xapps
```

Expected: `BUILD-XAPPS OK.`

**Step 3. Create the gNB config with E2SM-RC.**

```bash
mild-oran ran-config
```

Expected: `RAN-CONFIG DONE: ~/intact/configs/gnb_zmq_jbpf_e2rc.yaml`.

**Step 4. Allow sudo without a password.** The collector restarts the RAN by itself, also at night, and the RAN scripts call sudo. This is a manual step: add a file in `/etc/sudoers.d/` for your user, then check it.

```bash
sudo -k; sudo -n true && echo "NOPASSWD OK"
```

**Step 5. Run the self-tests.** Neither needs the RAN.

```bash
mild-oran selftest          # about 10 min. Expected: SELFTEST PASSED
mild-oran ran down
mild-oran mocktest          # about 35 min. Real xApps against a stand-in cell. Expected: one RAN restart, then segments with QA verdicts
```

**Step 6. Run the preflight.**

```bash
mild-oran preflight --ran
```

Expected: `== preflight: 0 FAIL`. The RAN is left running.

**Step 7. Collect scenario v5.** Collection takes about 1 h of calibration plus about 17 h of data.

```bash
source ~/intact/env.sh; intact-core-restart          # with the RAN down
mild-oran collect --set scenario=v5 --target_ticks 30000
```

What you should see:
- `calibration 2b/3 (scenario v5): capacity-knee scan`, followed by three knee lines;
- `knee scan api: knee knob X raised to the v5 floor Y (0.45 of the scale)`;
- `calibration done`;
- `segment 0 (seed 3000) started; 720 ticks x 2.0 s`.

The collection stops by itself at 30,000 usable steps. Ctrl+C stops it at any time, and the same command resumes it. The collector checks the data every segment, repairs and restarts components by itself, and writes `ESCALATION.md` if it needs you.

Keep the PC awake during the collection.

**Step 8. Watch the collection.** Use a second terminal. The command is read-only and safe at any time.

```bash
mild-oran watch
```

**Step 9. Build the dataset folder and run the design check.**

```bash
mild-oran pilotcheck --headroom_only --out ~/intact/mild/my_v5
```

This writes `~/intact/mild/my_v5/dataset/` (the same files as `data/v5`) and `PILOT_REPORT.md`. The headroom check places one fixed threshold on the own KPI of each intent. For the scenario to be meaningful, that threshold must detect the failures but warn early for only a few of them. For the paper's data it detected every failure, but warned at least 20 s ahead for only 9.8% of them (see `../results/design_check/`).

**Step 10. Train and test.** Follow level 2 of `../README.md` with the new dataset:

```bash
cd <repo>/oran
DATA=~/intact/mild/my_v5/dataset PY=~/intact/mild/venv/bin/python bash reproduce.sh paper runs/my_v5
```

Paper mode uses the frozen configurations of the paper. To repeat the configuration search on the new data as well, use `search` instead of `paper`.

## What a new collection reproduces and what it does not

A new collection reproduces the design of the dataset:
- the fault types and their schedule (8 episodes per 24-min segment, half of them co-drift pairs);
- the benign look-alikes;
- the targets, which are calibrated on your machine;
- the labeling rules.

It does not reproduce the exact numbers of the paper, because these depend on random elements:
- the simulated radio;
- the traffic;
- the random fault schedule;
- the speed of your CPU (the ZMQ radio ran at about 0.7 times real time on our PC).
