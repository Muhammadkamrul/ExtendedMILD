# WSL2 — srsRAN + Open5GS + Janus jbpf, bare-metal runbook

## How to use this runbook

This runbook brings up srsRAN gNB (with jbpf) + srsUE + Open5GS + jrt-controller as plain Linux processes inside one WSL2 distro, then changes a slice's PRB quota live through a jbpf codelet and shows the throughput change. No Kubernetes, no prebuilt images, no GRC broker in the main path (Appendix E adds a headless one for 3–5 UEs; Appendix F adds a FlexRIC nearRT-RIC with an E2 KPM xApp).
Note: Janus jbpf is not needed for our experiment. It is included as part of the package, for tentative future use.

**Read Part 0 first.** It explains every design choice by the failure it prevents. Then work Parts 1 to 10 strictly in order. Do not skip a gate: every gate exists because skipping it cost hours last time.

### Conventions

| Marker | Meaning |
| --- | --- |
| **\[PowerShell\]** | Run in Windows PowerShell, *not* in Linux. Admin rights only where it says *(Admin)*. |
| **\[WSL\]** | Run inside the `intact` distro ('intact' is a name given to the distro by us). Every new terminal starts with `source ~/intact/env.sh`. |
| **\[SCRIPT\]** | The step is automated by a script this runbook creates for you. You run one command and read its verdict. |
| **\[MANUAL\]** | You type or decide something yourself. |
| **Terminal T1 … T6** | A separate terminal is required, because that process runs in the foreground. Part 8 gives a tmux launcher that replaces T2–T5 with one command; the manual layout is kept for debugging. |
| **Expected** | What success looks like. If you see anything else, stop and go to the matching row in Part 11. |
| **If not** | The alternative command or path when the expected output does not appear. |

### How scripts are created

Every script is created with a `cat > file <<'EOF' … EOF` block. Copy the **whole** block, including the first and last line, and paste it into the terminal in one go. The quoted `'EOF'` means nothing inside is expanded at creation time, which is intentional. Scripts land in `~/intact/bin`, which is on your `PATH` once `env.sh` is sourced.

### Checkpoints

There are two layers, and both are used.

1. **Whole-distro snapshots** (Windows): `wsl --export` at four milestones, CP1–CP4. Restoring one undoes *anything*, including a broken apt or a deleted build tree.
2. **In-distro checkpoints** (Linux): `intact-cp mark <name>` records a finished step; `intact-cp save <label>` archives every config file, the Open5GS configs and the subscriber database; all configs live in a git repo so every edit is reversible with `git diff` / `git checkout`.

| Snapshot | Taken after | Restores you to |
| --- | --- | --- |
| CP1 | Part 2 | Clean distro with base packages and workspace |
| CP2 | Part 6 | Everything built, nothing configured |
| CP3 | Part 9 | First working attach with traffic |
| CP4 | Part 10 | Live PRB-quota control working |

### Time budget

Parts 1–4: about 1 hour. Part 6 builds: 45–90 minutes of machine time, mostly unattended. Parts 7–10: about 1 hour if the gates pass.

### Fixes from the first WSL run (laptop, 16 GB, C: drive)

Every item below is now built into the step it names; nothing has to be applied by hand.

| Step | What happened | Now in the runbook |
| --- | --- | --- |
| 1.1, 1.4, 1.7 | Paths assumed `D:`; snapshot export failed on rerun | All paths `C:\WSL`; snapshot uses `wsl --shutdown` and a timestamped file name, and checks the exit code (your version) |
| 1.3, Part 3 | 8 GB setting read as 7 GiB and failed the RAM gate | 16 GB guidance is `memory=9GB`–`10GB`; preflight rounds instead of truncating |
| 2.3, 5.2 | `set_vars.sh` printed `does not exist` errors | Expected until Part 6 ends; `env.sh` now logs them and shows a one-line count |
| 6.1 | pip got the container path `/jrtc/jbpf-protobuf/...` | Script drops container paths and installs `-r` from the local jrt-controller checkout (your fix) |
| 6.2 | `ENABLE_EXPORT=ON` stopped CMake | `ENABLE_EXPORT=OFF` (the srsRAN default; your fix); unused `AUTO_DETECT_ISA` removed; ZeroMQ link checked |
| 6.3 | GCC 13 `-Werror=array-bounds` in `phy_common.cc`, then `rrc.cc` | Your `phy_common.cc` `return;` kept (a real bug); `-DENABLE_WERROR=OFF`; `-DENABLE_RF_PLUGINS=OFF` as upstream |
| 6.7 | intact-build-verify reported SIGILL | False alarm: CMake's AVX-512 test programs (cmTC\_\*) crash by design on this CPU. The check now ignores them |
| 8.2, 9 | gnb.out showed only JBPF lines, never "gNB started" | The gNB's console output sat in a buffer because it was piped to tee. Launchers now use stdbuf -oL -eL |
| 7.1, 9 | UE stuck at Attaching UE..., Cell Selection failed | gNB config gains pdcch.common ss0\_index 0 / coreset0\_index 6 and upstream's PRACH values, as in jrtc-apps msft\_config\_zmq.yaml |
| 8.3, 8.4 | intact-health said "jrtc not running" while :3001 was listening; intact-down did not stop it | jrt-controller renames its process to jrtc\_main. intact-up, intact-down and intact-health now match 'jrtc\|jrtc\_main' |
| 7.1, 9b | UE dropped with "Received RRC Release" after a while | srsRAN releases a UE after 120 s without traffic (cu\_cp.inactivity\_timer default); srsUE 23.4 does not come back. Set to 7200 s, the maximum |
| 10.2 | With slicing on, UE rejected: AMF "Non cleartext IEs is included \[0xc\]", reject \[95\] | srsUE sent Requested NSSAI in the unprotected first message; Open5GS 2.8 refuses it. intact-fix-nssai drops that copy (the protected copy stays) and rebuilds srsue |

## Part 0 — What went wrong on PC2, and what changes

Almost every PC2 failure came from one source: Kubernetes gives every component a **new IP address on every restart**, while srsRAN's ZMQ links and Open5GS's service registry learn a peer's address **once, at startup**. Each restart left some component talking to a dead address. This runbook removes that whole class of failure by running every component as a normal process on fixed loopback addresses that never change.

What was proven on PC2 and is reused here: the jbpf agent registers with jrt-controller; the `slice_mgmt` codelet loads into the `mac_sched_slice_mgmt` hook; a `SLICE_ALLOC_SET` (30/70) was written and read back exactly; PRACH and RRC worked at CQI 15. What never worked: NAS registration, which failed with Open5GS service-mesh timeouts.

### Root cause → what this runbook does

| PC2 symptom | Root cause | This runbook |
| --- | --- | --- |
| `exit 132`, `trap invalid opcode` | Prebuilt images use AVX-512; consumer Intel CPUs lack it | Build everything from source on this machine; the compiler targets this CPU |
| Missing `NetworkAttachmentDefinition`, hugepage and Intel VF requests | Helm chart written for an SR-IOV testbed | No Kubernetes |
| PVC `Pending` (ReadWriteMany) | k3d storage provisioner | No Kubernetes, no PVCs |
| `jrtc-0` OOMKilled at 1 GiB | Container memory limit | Plain processes; Part 3 checks RAM |
| Image tag `-2` vs `-latest`; `install.sh --set-string` override | Floating upstream tags | No images; every repo pinned to a recorded commit |
| `taskset: failed ... 9-31` | Defaults for a 32-core server | No CPU pinning (WSL vCPUs are not pinned to physical cores anyway) |
| NG-U bind to `192.168.110.205` failed | SR-IOV address in config | gNB binds `127.0.0.1` |
| gNB stuck on AMF `192.168.101.50`, ConfigMap refresh delay | Address changed at runtime | AMF fixed at `127.0.0.5` |
| Bitnami MongoDB `not found`, SCP disable broke everything | Third-party Helm chart drift | Open5GS and MongoDB from official apt repositories, default config |
| SMF crash on PCRF hostname | Chart disabled PCRF | All Open5GS services kept at package defaults |
| `traffic` container deadlock | Trigger-file design | No trigger files; traffic is started by hand after attach |
| `kubectl set env` field-manager conflict | Imperative edits on Helm objects | No Kubernetes |
| UE stuck at `Attaching UE...` after restarts | ZMQ endpoints pinned to old pod IPs | Fixed `127.0.0.1:2000/2001`; gNB and UE always restarted together |
| `Registration reject [9]`/`[90]`, `Cannot receive SBI message` | SCP/NRF holding stale NF addresses | Fixed loopback NF addresses; ordered core restart script with registration check |
| `/dev/shm` 100% full | Sized exactly at jbpf's 1 GiB | Part 3 requires at least 2 GiB free |
| `jrtc-ctl` to `:30500` refused | NodePort vs container port | Bare-metal: jrt-controller REST on `127.0.0.1:3001` |
| Reverse proxy `:30450` refused | Proxy lived in another pod | Proxy runs on the same host, `127.0.0.1:30450` |
| `/slice_mgmt/slice_mgmt_app.py: no such file` | `JRTC_APPS`, `JBPF_CODELETS` unset | Exported by `env.sh` in every terminal |
| `400 Bad Request` on load | App already loaded | Scripts unload before load |
| Slice SD 162 vs `0000A2` | Decimal in gNB, hex in core | Slices use SD 1 and 2, identical in both bases |

One honest caveat: the PC2 registration failure was never proven down to a single line. The conditions that produced it (changing NF addresses behind SCP) cannot occur here, and Part 4's health script checks that every network function is registered before you start the RAN.

### Fixed address plan

Everything below lives on loopback inside the distro. None of it changes across restarts.

| Component | Address : port | Talks to |
| --- | --- | --- |
| MongoDB | 127.0.0.1:27017 | UDR, PCF, dbctl |
| Open5GS NRF / SCP | 127.0.0.10:7777 / 127.0.1.10:7777 | all core NFs |
| Open5GS AMF (NGAP, SCTP) | 127.0.0.5:38412 | gNB |
| Open5GS UPF (GTP-U) | 127.0.0.7:2152 | gNB |
| UPF tun `ogstun` | 10.45.0.1/16 | UE data, iperf3 server |
| gNB NGAP / NG-U bind | 127.0.0.1 | AMF / UPF |
| gNB ZMQ TX bind / RX connect | 127.0.0.1:2000 / :2001 | srsUE |
| srsUE ZMQ TX bind / RX connect | 127.0.0.1:2001 / :2000 | gNB |
| UE data interface | `tun_srsue`, 10.45.0.x, in netns `ue1` | UPF |
| jrt-controller REST | 127.0.0.1:3001 | `jrtc-ctl` |
| jbpf reverse proxy | 127.0.0.1:30450 | gNB LCM socket `/tmp/jbpf/jbpf_lcm_ipc` |
| jbpf data path | shared memory `jrt_controller` in `/dev/shm` | gNB ↔ jrt-controller |

Start order is always: core → jrt-controller → gNB → reverse proxy → UE → traffic → codelet load. Stop order is the reverse. The gNB and UE are always stopped and started together.

### Step 0 (optional, recommended) — harvest from PC2 before tearing it down

**\[MANUAL, on PC2\]** If the PC2 cluster is still running, save the exact gNB config it generated. It is the best schema reference for this srsRAN version and costs one minute.

```bash
mkdir -p ~/pc2_harvest && cd ~/pc2_harvest
kubectl exec -n ran srs-gnb-du1-0 -c gnb -- cat /opt/Scripts/config.yaml        > gnb_config_rendered.yaml
kubectl exec -n ran srs-gnb-du1-0 -c gnb -- cat /opt/Scripts/msft_config_zmq.yaml > gnb_msft_config_zmq.yaml
kubectl exec -n ran srs-ue1-du1-0 -c ue  -- cat /usr/local/bin/ue_zmq.conf        > ue_zmq.conf
cp "$JRTC_ROOT"/jrtc_apps/slice_mgmt/*.py "$JRTC_ROOT"/jrtc_apps/slice_mgmt/*.yaml .
tar czf ~/pc2_harvest.tgz -C ~ pc2_harvest && ls -la ~/pc2_harvest.tgz
```

**Expected:** `gnb_config_rendered.yaml` is non-empty and starts with gNB YAML keys such as `cu_cp:` or `cell_cfg:`. Copy `pc2_harvest.tgz` to the laptop. You will place it in `~/intact/configs/pc2/` in Part 2.

**If not** (cluster gone): skip. The configs in Part 7 were written for this srsRAN version and Part 7 includes a self-check that reveals any key the gNB does not accept.

## Part 1 — Windows side: checks, a fresh distro, the first snapshot

You will create a **new, dedicated** WSL distro called `intact`. Nothing already installed on the laptop (existing Ubuntu distros, Docker Desktop, Prometheus) is modified, and the whole experiment can be deleted with one command.

**Is WSL2 suitable?** Yes, with one hard condition: the WSL kernel must support **SCTP**, because the gNB talks to the AMF over SCTP. Whether the stock WSL kernel has it is not something I could confirm in advance, so Part 3 tests it in the first ten minutes. If it is missing, Appendix A builds a WSL kernel with SCTP (about 30 minutes, fully reversible). Everything else needed (TUN devices, network namespaces, shared memory, real-time scheduling) works in WSL2.

A 14-core / 20-thread laptop is almost certainly a hybrid Intel 12th–14th gen part. Those **do not have AVX-512**, which is exactly why the prebuilt Microsoft images crashed on your old laptop. Building from source avoids it completely.

### 1.1 Check the host — \[PowerShell\]

```powershell
wsl --version
(Get-CimInstance Win32_Processor) | Select-Object Name, NumberOfCores, NumberOfLogicalProcessors
[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB,1)
Get-PSDrive -PSProvider FileSystem | Select-Object Name, @{n='FreeGB';e={[math]::Round($_.Free/1GB)}}
wsl -l -v
```

**Expected:**

| Check | Required | Why |
| --- | --- | --- |
| `wsl --version` | WSL version 2.4 or newer, kernel 6.x | Needed for `--name`, mirrored-mode detection and kernel modules |
| Logical processors | 12 or more | Build speed; gNB real-time loop |
| Total RAM | 16 GB minimum, 32 GB comfortable | WSL gets 12–20 GB of it |
| Free disk on the drive that will hold the distro | 120 GB | Sources, builds, two snapshots |

**If not:** `wsl --version` fails or shows 1.x → run `wsl --update` (Admin), reboot, retry. RAM under 16 GB → the stack will run, but set `memory=10GB` below and build with fewer jobs (Part 6 computes this automatically).

Write down: the CPU name, total RAM, and free space on `C:`. This runbook keeps the distro and its snapshots under `C:\WSL`. Plan for about 60 GB for the distro plus 25–45 GB per snapshot; keep only the two newest snapshots.

### 1.2 Prevent sleep and throttling — \[PowerShell (Admin)\]

Sleep freezes the WSL VM and breaks the gNB's real-time loop.

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```

**\[MANUAL\]** Keep the laptop on mains power and set Windows *Power mode* to *Best performance* while experiments run.

### 1.3 Back up and write `.wslconfig` — \[PowerShell\]

`.wslconfig` is **global**: it applies to every distro and to Docker Desktop. Back it up first.

```powershell
if (Test-Path $env:USERPROFILE\.wslconfig) { Copy-Item $env:USERPROFILE\.wslconfig $env:USERPROFILE\.wslconfig.bak-before-intact; Get-Content $env:USERPROFILE\.wslconfig }
```

**\[MANUAL\]** If the file printed something, keep its existing lines and only add or change the keys below. Then write it (adjust `memory`: 16 GB laptop → 9GB or 10GB, leaving about 6 GB to Windows; 32 GB or more → total RAM minus 8 GB, at most 20 GB; `processors`: logical processors minus 4):

```powershell
@"
[wsl2]
memory=10GB
processors=16
swap=8GB
networkingMode=nat
"@ | Set-Content -Encoding ascii $env:USERPROFILE\.wslconfig
Get-Content $env:USERPROFILE\.wslconfig
```

`networkingMode=nat` matters. In *mirrored* mode, WSL shares localhost with Windows, so any Windows service on 9090, 27017, 3000 or 3001 (Prometheus, MongoDB, Grafana, Node dev servers) would collide with the core. In NAT mode the distro has its own loopback.

**Rollback:** `Copy-Item $env:USERPROFILE\.wslconfig.bak-before-intact $env:USERPROFILE\.wslconfig -Force; wsl --shutdown`.

### 1.4 Create the dedicated distro — \[PowerShell\]

```powershell
mkdir C:\WSL\intact, C:\WSL\backups -Force | Out-Null
wsl --install Ubuntu-24.04 --name intact --location C:\WSL\intact
```

**Expected:** it downloads Ubuntu 24.04, then asks for a Linux username and password. Use the same username as on PC2 (`kamrul`) so paths in your notes stay valid. You land at a `kamrul@...:~$` prompt.

**If not** (`--name` or `--location` rejected, older WSL):

```powershell
wsl --install -d Ubuntu-24.04          # creates a distro named Ubuntu-24.04; set user/password, then type exit
wsl --terminate Ubuntu-24.04
wsl --export Ubuntu-24.04 C:\WSL\backups\ubuntu2404-fresh.tar
wsl --import intact C:\WSL\intact C:\WSL\backups\ubuntu2404-fresh.tar
```

An imported distro logs in as root. Fix that in step 1.5 with the `[user]` line. Do **not** unregister `Ubuntu-24.04` if you were already using it for other work.

### 1.5 Configure the distro — \[WSL\]

Open it with `wsl -d intact` (or the new *intact* profile in Windows Terminal). Then:

```bash
sudo tee /etc/wsl.conf >/dev/null <<EOF
[boot]
systemd=true

[interop]
appendWindowsPath=false

[user]
default=$USER
EOF
cat /etc/wsl.conf
```

(If you are root after an import, replace `$USER` with `kamrul` and first run `adduser kamrul && usermod -aG sudo kamrul`.)

- `systemd=true`: MongoDB and Open5GS run as systemd services.
- `appendWindowsPath=false`: stops Windows programs (`docker.exe`, `python.exe`, `node.exe`) from shadowing Linux ones. This was a silent source of confusion in mixed setups.

Then restart the VM — **\[PowerShell\]**:

```powershell
wsl --shutdown
wsl -d intact
```

Back in **\[WSL\]**, verify:

```bash
ps -p 1 -o comm=          # Expected: systemd
uname -r                  # Expected: 6.x.y-microsoft-standard-WSL2
echo $PATH | tr : '\n' | grep -c /mnt/c    # Expected: 0
nproc; free -g | awk '/Mem:/{print $2" GiB"}'   # Expected: your processors / memory from .wslconfig
```

**If not:** `ps` prints `init` → `/etc/wsl.conf` was not read; check its contents and repeat `wsl --shutdown`. `nproc`/memory unchanged → `.wslconfig` has a typo; `Get-Content` it.

### 1.6 Docker Desktop, if installed

The main path of this runbook needs **no Docker at all**. If Docker Desktop is installed, open *Settings → Resources → WSL integration* and leave **intact switched off**. This keeps a `docker` CLI from appearing inside the distro and keeps Docker's published ports out of your way. Your other distros are unaffected.

### 1.7 Snapshot procedure (used at CP1–CP4) — \[PowerShell\]

```powershell
$cp = "CP1"      # set to CP2, CP3 or CP4 at the later checkpoints
"C: free before snapshot: {0:N0} GB" -f ((Get-PSDrive C).Free/1GB)
wsl --shutdown
Start-Sleep -Seconds 5
$backupPath = "C:\WSL\backups\intact-$cp-$(Get-Date -Format yyyyMMdd-HHmmss).vhdx"
wsl --export intact $backupPath --vhd
if ($LASTEXITCODE -eq 0) {
  Get-Item $backupPath | Select-Object FullName, Length
} else {
  Write-Host "Backup failed. Do not treat this file as a completed checkpoint."
}
```

**Expected:** a line with the file name and a size in bytes. Then reopen the distro with `wsl -d intact`. The timestamp in the name means a rerun never collides with an earlier file, and `wsl --shutdown` (rather than `--terminate`) releases the disk image fully, so the export cannot fail on a locked file. **If it fails:** run `wsl --list --verbose` and check that `intact` shows `Stopped`; if free space is under 50 GB, delete the oldest `intact-CP*.vhdx` in `C:\WSL\backups` first.

**Restore** (replaces the current distro with the snapshot — everything since is lost):

```powershell
$cp = "CP1"      # the checkpoint to go back to
$f = Get-ChildItem "C:\WSL\backups\intact-$cp-*.vhdx" | Sort-Object LastWriteTime | Select-Object -Last 1
"Restoring from: $($f.FullName)"
wsl --shutdown
wsl --unregister intact
wsl --import intact C:\WSL\intact $f.FullName --vhd
wsl -d intact
```

To inspect a snapshot without losing the current state, import it under another name: `wsl --import intact-test C:\WSL\intact-test <full path of an intact-CP2-*.vhdx> --vhd`.

Do not take CP1 yet; it comes at the end of Part 2.

## Part 2 — Inside the distro: workspace, environment, checkpoint tool

Everything lives under `~/intact` on the Linux filesystem. **Never build or run anything under `/mnt/c`**: it is 5–20× slower and breaks file permissions, symlinks and Unix sockets. Reading a file from `/mnt/c` once (to copy it in) is fine.

### 2.1 Base packages — \[WSL\]

```bash
sudo apt update && sudo apt -y full-upgrade
sudo DEBIAN_FRONTEND=noninteractive apt install -y \
  git curl wget gnupg ca-certificates software-properties-common rsync \
  iproute2 iputils-ping net-tools kmod lksctp-tools tmux jq ripgrep binutils \
  python3 python3-pip python3-venv iperf3
```

**Expected:** ends without `E:` lines. **If not:** `Could not resolve` → DNS inside WSL; run `cat /etc/resolv.conf`, then `wsl --shutdown` from PowerShell and retry. Behind a campus proxy or VPN, connect it before starting WSL.

### 2.2 Workspace layout — \[WSL\]

```bash
mkdir -p ~/intact/{src,configs,logs/run,checkpoints,backups,bin}
cd ~/intact/configs && git init -q && git config user.name "$USER" && git config user.email "$USER@localhost"
ls ~/intact
```

**Expected:** `backups  bin  checkpoints  configs  logs  src`.

| Folder | Holds |
| --- | --- |
| `src/` | jrtc-apps (with pinned srsRAN and srsUE submodules), jrt-controller |
| `configs/` | gNB, UE, deployment and params files — a git repo, every change committed |
| `logs/` | build logs, preflight reports, `run/` for live process output |
| `checkpoints/` | one marker file per finished step |
| `backups/` | config + database archives from `intact-cp save` |
| `bin/` | every script this runbook creates |

If you harvested from PC2 (Part 0, Step 0), copy it in now (adjust the Windows path):

```bash
mkdir -p ~/intact/configs/pc2 && tar xzf /mnt/c/Users/$USER/Downloads/pc2_harvest.tgz -C ~/intact/configs/pc2 --strip-components=1 && ls ~/intact/configs/pc2
```

### 2.3 The environment file — \[SCRIPT\]

Every terminal you open for this project must run `source ~/intact/env.sh` first. It sets every path the upstream scripts expect, including the two whose absence broke loading on PC2 (`JRTC_APPS`, `JBPF_CODELETS`).

```bash
cat > ~/intact/env.sh <<'EOF'
# INTACT environment. Run in EVERY terminal:  source ~/intact/env.sh
export INTACT="$HOME/intact"
export CFG="$INTACT/configs" LOGS="$INTACT/logs"
export JRTC_ROOT="$INTACT/src/jrtc-apps"
export SRSRANAPP_DIR="$JRTC_ROOT"
export SRSRAN_DIR="$JRTC_ROOT/containers/Docker/srsRAN_Project"   # pinned by jrtc-apps; headers match the codelets
export SRSUE_DIR="$JRTC_ROOT/containers/Docker/srsRAN_4G"         # the srsUE that RACH'd on PC2
export JRTC_PATH="$INTACT/src/jrt-controller"
export JRTC_APP_PATH="$JRTC_PATH/out/lib/"
export PATH="$INTACT/bin:/usr/local/go/bin:$HOME/go/bin:$HOME/.local/bin:$PATH"
export LD_LIBRARY_PATH="$JRTC_PATH/out/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# Upstream variables (reads jrtc-apps/.env and .env.local, created in Part 5)
if [ -f "$JRTC_ROOT/set_vars.sh" ]; then
  # set_vars.sh complains about every tool that is not built yet. That is normal until
  # Part 6 ends, so its messages go to a file and only a one-line count is shown.
  mkdir -p "$LOGS"
  pushd "$JRTC_ROOT" >/dev/null && source ./set_vars.sh 2>"$LOGS/set_vars.err"; popd >/dev/null
  _n=$(grep -c 'does not exist\|No such file' "$LOGS/set_vars.err" 2>/dev/null || true)
  if [ "${_n:-0}" -gt 0 ]; then echo "[env] $_n upstream tool(s) not built yet (normal before Part 6 ends): $LOGS/set_vars.err" >&2; fi
  unset _n
fi

# Values the bare-metal path needs even if set_vars.sh does not set them
export SRS_JBPF_DOCKER="${SRS_JBPF_DOCKER:-0}" USE_JRTC="${USE_JRTC:-1}"
export JRTC_APPS="${JRTC_APPS:-$JRTC_ROOT/jrtc_apps}"
export JBPF_CODELETS="${JBPF_CODELETS:-$JRTC_ROOT/codelets}"
export JRTC_CTL_BIN="${JRTC_CTL_BIN:-$JRTC_PATH/out/bin/jrtc-ctl}"
export JOBS="${JOBS:-$(awk -v n="$(nproc)" '/MemTotal/{j=int($2/1024/1024/2.5); if(j<1)j=1; print (j<n?j:n)}' /proc/meminfo)}"
EOF
grep -q 'intact/env.sh' ~/.bashrc || echo '[ -f ~/intact/env.sh ] && source ~/intact/env.sh' >> ~/.bashrc
source ~/intact/env.sh && echo "JOBS=$JOBS  SRSRAN_DIR=$SRSRAN_DIR"
```

**Expected:** `JOBS=` a number (about RAM ÷ 2.5 GB, capped at your processor count) and the srsRAN path. `JOBS` is the parallel build width: srsRAN needs up to 2.5 GB per compiler job, and running out of memory mid-build looks like a random compiler crash.

Appending to `~/.bashrc` means new terminals source it automatically; the explicit `source` line in each part is still there as a safeguard.

### 2.4 The checkpoint tool — \[SCRIPT\]

```bash
cat > ~/intact/bin/intact-cp <<'EOF'
#!/usr/bin/env bash
# intact-cp mark <name>        record that a step finished (and git-commit configs)
# intact-cp status             list finished steps
# intact-cp save <label>       archive configs, /etc/open5gs, subscriber DB, versions
# intact-cp list               list archives
# intact-cp restore <archive>  restore configs, /etc/open5gs and DB from an archive
set -uo pipefail
I="$HOME/intact"; CP="$I/checkpoints"; BK="$I/backups"; CFG="$I/configs"
mkdir -p "$CP" "$BK"
case "${1:-}" in
  mark)   n="${2:?usage: intact-cp mark <name>}"
          date -Is > "$CP/$(date +%Y%m%d-%H%M%S)_$n.done"
          git -C "$CFG" add -A >/dev/null 2>&1
          git -C "$CFG" commit -qm "checkpoint: $n" >/dev/null 2>&1 || true
          echo "checkpoint recorded: $n" ;;
  status) ls -1 "$CP" | sed 's/\.done$//' ;;
  save)   l="${2:?usage: intact-cp save <label>}"; ts=$(date +%Y%m%d-%H%M%S); d=$(mktemp -d)
          cp -a "$CFG" "$d/configs"
          if [ -d /etc/open5gs ]; then sudo cp -a /etc/open5gs "$d/etc-open5gs"; sudo chown -R "$USER" "$d/etc-open5gs"; fi
          command -v mongodump >/dev/null && mongodump --quiet --db open5gs --out "$d/mongo" 2>/dev/null
          { uname -a
            for r in jrtc-apps jrt-controller; do [ -d "$I/src/$r/.git" ] && echo "$r $(git -C "$I/src/$r" rev-parse HEAD)"; done
            dpkg -l 2>/dev/null | awk '/open5gs|mongodb-org /{print $2, $3}'; } > "$d/versions.txt"
          tar czf "$BK/$ts-$l.tgz" -C "$d" . && rm -rf "$d" && ls -la "$BK/$ts-$l.tgz" ;;
  list)   ls -la "$BK" ;;
  restore) a="${2:?usage: intact-cp restore <archive.tgz>}"
          read -rp "Restore configs, /etc/open5gs and DB from $a ? [y/N] " y; [ "$y" = y ] || exit 1
          d=$(mktemp -d); tar xzf "$a" -C "$d"
          rsync -a --delete --exclude .git "$d/configs/" "$CFG/"
          [ -d "$d/etc-open5gs" ] && sudo rsync -a "$d/etc-open5gs/" /etc/open5gs/
          [ -d "$d/mongo" ] && command -v mongorestore >/dev/null && mongorestore --quiet --drop "$d/mongo"
          rm -rf "$d"; echo "Restored. Now run: intact-core-restart" ;;
  *)      sed -n '2,6p' "$0" ;;
esac
EOF
chmod +x ~/intact/bin/intact-cp
intact-cp mark part2-base && intact-cp status
```

**Expected:** `checkpoint recorded: part2-base` and one line in the status list.

### 2.5 Snapshot CP1

**\[PowerShell\]** Run the snapshot procedure from 1.7 with the name `intact-CP1`. Then reopen the distro with `wsl -d intact`.

## Part 3 — Compatibility gates (automated preflight)

One script checks every hard requirement and every known conflict, prints PASS / WARN / FAIL per item, and saves a report. **Do not continue past a FAIL.** You can re-run it any time; once the stack is installed it recognises its own processes on the ports.

All WSL2 distros share **one network stack**. A MongoDB, Prometheus or Docker-published port in *another* distro, or in Docker Desktop, therefore blocks the same port here. The port check sees those listeners even though their processes live elsewhere.

### 3.1 Create and run the preflight — \[SCRIPT\]

```bash
cat > ~/intact/bin/intact-preflight <<'EOF'
#!/usr/bin/env bash
# intact-preflight: compatibility gates. Exit status 0 = no FAIL.
set -uo pipefail
R="$HOME/intact/logs/preflight-$(date +%Y%m%d-%H%M%S).txt"
exec > >(tee "$R") 2>&1
FAILS=0; WARNS=0
pass(){ printf '  [PASS] %s\n' "$*"; }
warn(){ printf '  [WARN] %s\n' "$*"; WARNS=$((WARNS+1)); }
fail(){ printf '  [FAIL] %s\n' "$*"; FAILS=$((FAILS+1)); }
sudo -v
echo "== 1. Platform"
k=$(uname -r)
[[ "$k" == *WSL2* ]] && pass "WSL2 kernel $k" || warn "kernel $k does not look like WSL2"
[ "$(ps -p 1 -o comm=)" = systemd ] && pass "systemd is PID 1" || fail "systemd not running (Part 1.5)"
case "$HOME" in /mnt/*) fail "home directory is under /mnt";; *) pass "Linux filesystem";; esac
mode=$(wslinfo --networking-mode 2>/dev/null || echo unknown)
[ "$mode" = mirrored ] && warn "networking mode is mirrored: Windows services share localhost ports" || pass "networking mode: $mode"
echo "== 2. CPU"
grep -m1 'model name' /proc/cpuinfo | sed 's/^/  /'
grep -qw avx2 /proc/cpuinfo && pass "AVX2 present" || fail "no AVX2: srsRAN PHY will be too slow"
if grep -qw avx512f /proc/cpuinfo; then echo "  [INFO] AVX-512 present"; else echo "  [INFO] no AVX-512: prebuilt images would crash here; building from source avoids it"; fi
[ "$(nproc)" -ge 8 ] && pass "$(nproc) logical CPUs" || warn "only $(nproc) logical CPUs"
echo "== 3. Memory and disk"
m=$(awk '/MemTotal/{printf "%d", $2/1024/1024 + 0.5}' /proc/meminfo)   # rounded: memory=9GB reports 9, not 8
if [ "$m" -ge 12 ]; then pass "RAM ${m} GiB"; elif [ "$m" -ge 8 ]; then warn "RAM ${m} GiB: builds slow, keep JOBS low"; else fail "RAM ${m} GiB < 8"; fi
d=$(df -BG --output=avail "$HOME" | tail -1 | tr -dc 0-9)
[ "$d" -ge 60 ] && pass "disk free ${d} GiB" || fail "disk free ${d} GiB < 60"
s=$(df -BM --output=avail /dev/shm | tail -1 | tr -dc 0-9)
[ "$s" -ge 2048 ] && pass "/dev/shm free ${s} MiB" || fail "/dev/shm free ${s} MiB < 2048 (jbpf takes 1024 MiB)"
echo "== 4. Kernel features"
sudo modprobe sctp 2>/dev/null
if python3 -c 'import socket; socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_SCTP).close()' 2>/dev/null; then
  pass "SCTP sockets work"
else
  fail "SCTP NOT available in this kernel -> Appendix A"
fi
[ -c /dev/net/tun ] && pass "/dev/net/tun present" || fail "/dev/net/tun missing"
if sudo ip netns add intact-probe 2>/dev/null; then sudo ip netns del intact-probe; pass "network namespaces work"; else fail "cannot create network namespaces"; fi
sudo chrt -f 1 true 2>/dev/null && pass "SCHED_FIFO works" || warn "SCHED_FIFO refused (jbpf agent priority)"
echo "== 5. Tool conflicts"
for t in docker python3 mongod mongosh node; do
  p=$(command -v "$t" 2>/dev/null || true)
  case "$p" in "") ;; /mnt/*) warn "$t resolves to a Windows binary: $p";; *) echo "  [INFO] $t -> $p";; esac
done
[ -S /var/run/docker.sock ] && warn "Docker socket present in this distro (not needed; Docker Desktop integration for 'intact' should be off)"
pgrep -x prometheus >/dev/null && warn "Prometheus runs in this distro (may hold 9090)"
echo "== 6. Ports"
while read -r p proto label; do
  case $proto in
    tcp)  l=$(ss -Hltn "sport = :$p");;
    udp)  l=$(ss -Hlun "sport = :$p");;
    sctp) l=$(ss -HlnS "sport = :$p" 2>/dev/null);;
  esac
  if [ -z "$l" ]; then pass "$p/$proto free ($label)"; continue; fi
  owner=$(sudo ss -Hlpn "sport = :$p" 2>/dev/null | grep -o 'users:(("[^"]*' | head -1 | sed 's/users:(("//')
  if echo "$owner" | grep -qE 'open5gs|mongod|gnb|srsue|jrtc|reverse|iperf3'; then
    echo "  [INFO] $p/$proto ($label) held by our own stack: $owner"
  else
    fail "$p/$proto ($label) IN USE by '${owner:-a process outside this distro}': $(echo "$l" | awk '{print $4}' | head -2 | tr '\n' ' ')"
  fi
done <<'P'
27017 tcp MongoDB
7777 tcp Open5GS-SBI
38412 sctp AMF-NGAP
2152 udp GTP-U
8805 udp PFCP
9090 tcp Open5GS-metrics
3868 tcp Diameter
2000 tcp ZMQ-gNB-TX
2001 tcp ZMQ-UE-TX
3001 tcp jrtc-REST
30450 tcp jbpf-reverse-proxy
20788 udp jrtc-IO-out
30400 udp jbpf-IO-in
5201 tcp iperf3
P
echo
echo "RESULT: $FAILS fail(s), $WARNS warning(s). Report: $R"
[ "$FAILS" -eq 0 ]
EOF
chmod +x ~/intact/bin/intact-preflight
intact-preflight
```

**Expected:** `RESULT: 0 fail(s)`. Warnings are acceptable if you understand them.

### 3.2 What to do for each FAIL

| FAIL line | Meaning | Fix |
| --- | --- | --- |
| `SCTP NOT available` | The WSL kernel was built without SCTP. The gNB cannot reach the AMF. | **Appendix A.** Then re-run `intact-preflight`. Nothing else in this runbook works until this passes. |
| `systemd not running` | `/etc/wsl.conf` not applied | Part 1.5, then `wsl --shutdown` |
| `/dev/shm free < 2048` | Too little shared memory | **\[WSL\]** `sudo mount -o remount,size=4G /dev/shm`. To make it permanent: `echo 'tmpfs /dev/shm tmpfs defaults,size=4G 0 0' \| sudo tee -a /etc/fstab` |
| `RAM < 8` / `disk free < 60` | Too small a VM | Raise `memory=` in `.wslconfig` / free disk; `wsl --shutdown` |
| `PORT ... IN USE by 'X'` | Something already listens there | First find who: **\[PowerShell\]** `wsl -l -v` lists running distros; stop the one you are not using with `wsl --terminate <name>`, or quit Docker Desktop. If you need that service, use the port alternative in the next table. |
| `no AVX2` | Very old or virtualised CPU | Not supported; use PC2 |

### 3.3 Port alternatives, if you must keep a conflicting service

| Port | Default owner here | Alternative |
| --- | --- | --- |
| 27017 | MongoDB | In Part 4, set `net.port: 27018` in `/etc/mongod.conf`, then in `/etc/open5gs/udr.yaml` and `pcf.yaml` change `db_uri` to `mongodb://localhost:27018/open5gs`, and `export DB_URI=mongodb://localhost:27018/open5gs` before using `open5gs-dbctl` |
| 9090 | Open5GS metrics (AMF, SMF, PCF, UPF) | In Part 4: `sudo sed -i 's/port: 9090/port: 9190/' /etc/open5gs/{amf,smf,pcf,upf}.yaml` |
| 2000 / 2001 | ZMQ radio | In Part 7, change **both** the gNB `device_args` and the UE `device_args` to 2100 / 2101 (gNB TX = UE RX; UE TX = gNB RX) |
| 30450 | jbpf reverse proxy | Start the proxy with another `--host-port` and put the same number in the deployment YAML `jbpf.device.port` (Part 10) |
| 5201 | iperf3 | Add `-p 5301` to both iperf3 server and client |
| 3001 | jrt-controller REST | Stop the other service (often a Node dev server); jrt-controller's port is not changed in this runbook |

### 3.4 Record the gate

```bash
intact-preflight && intact-cp mark part3-preflight-pass
```

## Part 4 — Open5GS core, native

The core is installed from the official Open5GS and MongoDB apt repositories and runs as systemd services with the **package's default loopback addresses**. The only edit is the PLMN (999/70 → 001/01). The rule for this part is: change as little as possible from defaults, because defaults are what thousands of other people run.

### 4.1 MongoDB 8.0 — \[WSL\]

```bash
curl -fsSL https://www.mongodb.org/static/pgp/server-8.0.asc | sudo gpg --dearmor -o /usr/share/keyrings/mongodb-server-8.0.gpg
echo "deb [ arch=amd64 signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg ] https://repo.mongodb.org/apt/ubuntu noble/mongodb-org/8.0 multiverse" | sudo tee /etc/apt/sources.list.d/mongodb-org-8.0.list
sudo apt update && sudo apt install -y mongodb-org
sudo systemctl enable --now mongod
sleep 3; mongosh --quiet --eval 'db.runCommand({ping:1})'
```

**Expected:** `{ ok: 1 }`. **If not:** `systemctl status mongod` shows failed → `sudo journalctl -u mongod -n 40`. `Address already in use` → port 27017 conflict (Part 3.3). `Illegal instruction` would mean no AVX, which Part 3 ruled out.

### 4.2 Open5GS — \[WSL\]

```bash
sudo add-apt-repository -y ppa:open5gs/latest
sudo apt update && sudo apt install -y open5gs
dpkg-query -W -f='open5gs ${Version}\n' open5gs
systemctl list-units 'open5gs-*' --no-legend --plain | awk '{print $1, $3}'
```

**Expected:** version `2.7.x`, and about 16 `open5gs-*d.service` units, each `active`. Some may log errors until the PLMN edit and ogstun exist; that is fine for now.

### 4.3 Set the PLMN to 001/01 — \[SCRIPT\]

The edit touches only uncommented lines, and backs up the whole config directory first.

```bash
cat > ~/intact/bin/intact-core-plmn <<'EOF'
#!/usr/bin/env bash
# Set PLMN 001/01 in Open5GS (amf, nrf, mme). Uncommented lines only. Backs up /etc/open5gs first.
set -euo pipefail
B=/etc/open5gs/.orig-$(date +%Y%m%d-%H%M%S); sudo mkdir -p "$B"; sudo cp /etc/open5gs/*.yaml "$B/"
for f in amf nrf mme; do
  sudo sed -i -E '/^\s*#/! s/(mcc:\s*)999/\1001/; /^\s*#/! s/(mnc:\s*)70\b/\101/' /etc/open5gs/$f.yaml
done
echo "backup: $B"
grep -nE '^\s*(mcc|mnc):' /etc/open5gs/{amf,nrf,mme}.yaml
if grep -qE '^\s*(mcc:\s*999|mnc:\s*70)\b' /etc/open5gs/{amf,nrf,mme}.yaml; then echo "FAIL: 999/70 still present"; exit 1; fi
echo "PLMN set to 001/01"
EOF
chmod +x ~/intact/bin/intact-core-plmn && intact-core-plmn
```

**Expected:** every listed line reads `mcc: 001` or `mnc: 01`, then `PLMN set to 001/01`. **Undo:** `sudo cp /etc/open5gs/.orig-<timestamp>/*.yaml /etc/open5gs/`.

### 4.4 The UPF tunnel `ogstun` — \[SCRIPT\]

The UPF delivers UE traffic through a TUN device called `ogstun`. The package relies on `systemd-networkd` to create it, which is not dependable in WSL, so a small unit guarantees it before the UPF starts.

```bash
sudo tee /etc/systemd/system/intact-ogstun.service >/dev/null <<'EOF'
[Unit]
Description=INTACT: create ogstun for the Open5GS UPF
Before=open5gs-upfd.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'ip link show ogstun >/dev/null 2>&1 || ip tuntap add name ogstun mode tun; ip addr replace 10.45.0.1/16 dev ogstun; ip link set ogstun up; sysctl -qw net.ipv4.ip_forward=1'

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now intact-ogstun
ip -br addr show ogstun
```

**Expected:** `ogstun  UNKNOWN  10.45.0.1/16 ...` (UNKNOWN is normal for TUN devices).

### 4.5 Ordered restart and health check — \[SCRIPT\]

The health script answers one question: can a gNB attach a UE right now? It checks every service, an actual SCTP connection to the AMF, the tunnel, that **each network function has re-registered with the NRF since it last started** (the exact thing that failed on PC2), the SMF–UPF PFCP association, and the subscriber count.

```bash
cat > ~/intact/bin/intact-core-health <<'EOF'
#!/usr/bin/env bash
# intact-core-health: is the 5G core ready for a gNB? Exit 0 = yes.
set -uo pipefail
F=0; ok(){ echo "  [ OK ] $*"; }; bad(){ echo "  [FAIL] $*"; F=$((F+1)); }
L=/var/log/open5gs
after_start(){ # file pattern: is pattern present after the last 'initialize...done'?
  local f="$L/$1.log" n; n=$(sudo grep -n 'initialize\.\.\.done' "$f" 2>/dev/null | tail -1 | cut -d: -f1)
  [ -n "$n" ] && sudo tail -n +"$n" "$f" | grep -q "$2"; }
systemctl is-active --quiet mongod && ok "mongod" || bad "mongod not active"
for n in nrf scp amf smf upf ausf udm udr pcf nssf bsf; do
  systemctl is-active --quiet open5gs-${n}d && ok "open5gs-${n}d active" || bad "open5gs-${n}d NOT active -> sudo journalctl -u open5gs-${n}d -n 30"
done
for n in scp amf smf ausf udm udr pcf nssf bsf; do
  after_start $n 'NF registered' && ok "$n registered with NRF" || bad "$n NOT registered with NRF since its last start -> sudo tail -30 $L/$n.log"
done
after_start smf 'PFCP associated' && ok "SMF-UPF PFCP associated" || bad "no PFCP association -> sudo tail -30 $L/smf.log"
sudo ss -HlnS 2>/dev/null | grep -q '127.0.0.5:38412' && ok "AMF listening SCTP 127.0.0.5:38412" || bad "AMF not listening on SCTP 38412"
python3 - <<'PY' && ok "SCTP connect to AMF" || bad "SCTP connect to AMF fails"
import socket,sys
s=socket.socket(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_SCTP); s.settimeout(3)
try: s.connect(("127.0.0.5",38412)); s.close()
except Exception as e: print("     ",e); sys.exit(1)
PY
ip -br addr show ogstun 2>/dev/null | grep -q '10.45.0.1/16' && ok "ogstun 10.45.0.1/16" || bad "ogstun missing -> sudo systemctl restart intact-ogstun open5gs-upfd"
c=$(mongosh --quiet open5gs --eval 'db.subscribers.countDocuments()' 2>/dev/null || echo 0)
[ "${c:-0}" -ge 1 ] && ok "$c subscriber(s) provisioned" || echo "  [WARN] no subscribers yet (Part 4.6)"
for n in amf smf; do e=$(sudo grep -cE 'ERROR|FATAL' $L/$n.log 2>/dev/null); [ "${e:-0}" -gt 0 ] && echo "  [INFO] $n.log has $e ERROR/FATAL lines in total (old ones included)"; done
echo; [ $F -eq 0 ] && echo "CORE READY" || echo "CORE NOT READY: $F problem(s)"
[ $F -eq 0 ]
EOF

cat > ~/intact/bin/intact-core-restart <<'EOF'
#!/usr/bin/env bash
# intact-core-restart: restart Open5GS in dependency order, then check health.
set -uo pipefail
sudo systemctl restart intact-ogstun mongod
sudo systemctl restart open5gs-nrfd; sleep 3
sudo systemctl restart open5gs-scpd; sleep 3
for n in udr udm ausf pcf bsf nssf upf smf amf; do sudo systemctl restart open5gs-${n}d; done
for n in hss pcrf mme sgwc sgwu; do systemctl is-enabled --quiet open5gs-${n}d 2>/dev/null && sudo systemctl restart open5gs-${n}d; done
echo "waiting 15 s for NRF registrations..."; sleep 15
intact-core-health
EOF
chmod +x ~/intact/bin/intact-core-health ~/intact/bin/intact-core-restart
intact-core-restart
```

**Expected:** every line `[ OK ]` except the subscriber warning, then `CORE READY`.

**If not:**

| FAIL | Next step |
| --- | --- |
| `open5gs-Xd NOT active` | `sudo journalctl -u open5gs-Xd -n 30`. A bind error means a port conflict (Part 3.3). |
| `X NOT registered with NRF` | Wait 20 s and run `intact-core-health` again. Still failing → `intact-core-restart`. Still failing → `sudo tail -30 /var/log/open5gs/X.log` and `.../nrf.log`; look for `Connection refused` to 127.0.1.10 (SCP) or 127.0.0.10 (NRF). |
| `no PFCP association` | `ogstun` missing or UPF failed; `sudo systemctl restart intact-ogstun open5gs-upfd open5gs-smfd` |
| `AMF not listening` / `SCTP connect fails` with SCTP passing in Part 3 | AMF crashed on config; `sudo tail -40 /var/log/open5gs/amf.log` (usually a YAML indentation slip after a manual edit; restore the `.orig-*` backup) |

### 4.6 Provision the subscriber — \[WSL\]

The IMSI, K and OPc are the values from the srsUE config that reached RRC on PC2.

```bash
V=$(dpkg-query -W -f='${Version}' open5gs | cut -d~ -f1 | cut -d- -f1)
sudo curl -fsSL -o /usr/local/bin/open5gs-dbctl "https://raw.githubusercontent.com/open5gs/open5gs/v$V/misc/db/open5gs-dbctl" \
  || sudo curl -fsSL -o /usr/local/bin/open5gs-dbctl https://raw.githubusercontent.com/open5gs/open5gs/main/misc/db/open5gs-dbctl
sudo chmod +x /usr/local/bin/open5gs-dbctl
open5gs-dbctl add_ue_with_apn 001010123456780 00112233445566778899aabbccddeeff 63BFA50EE6523365FF14C1F45F88737D internet
mongosh --quiet open5gs --eval 'db.subscribers.find({imsi:"001010123456780"},{_id:0,imsi:1,"security.k":1,"security.opc":1,"slice.sst":1,"slice.sd":1,"slice.session.name":1}).toArray()'
```

**Expected:** one document with `imsi: '001010123456780'`, `k: '00112233445566778899aabbccddeeff'`, `opc: '63BFA50EE6523365FF14C1F45F88737D'`, `sst: 1`, no `sd`, session `internet`.

**If not:** `mongosh: command not found` → `sudo apt install -y mongodb-mongosh`. A duplicate-key error means it already exists; check with the `find` line. To start over: `open5gs-dbctl remove 001010123456780`.

### 4.7 Checkpoint

```bash
intact-core-health && intact-cp save core-ready && intact-cp mark part4-core
```

## Part 5 — Get the source, pinned to known commits

Every repository is pinned to the exact commit recorded on PC2 (`JRTC_UPSTREAM_COMMIT.txt`, `JRTC_SUBMODULES.txt`). This matters for one specific reason: the `slice_mgmt` codelet is compiled against srsRAN's C++ headers, and the codelet's view of the scheduler structures must match the running gNB byte for byte. Using the srsRAN tree that jrtc-apps itself pins guarantees that.

The gNB is built from the official bare-metal path documented in jrtc-apps ([baremetal.md](https://github.com/microsoft/jrtc-apps/blob/main/docs/baremetal.md), [example with jrt-controller](https://github.com/microsoft/jrtc-apps/blob/main/docs/example_w_jrtc_baremetal.md)), which uses the jbpf-enabled srsRAN fork ([srsRAN\_Project\_jbpf](https://github.com/xfoukas/srsRAN_Project_jbpf/blob/main/README_JBPF.md)) and [jrt-controller](https://github.com/microsoft/jrt-controller).

### 5.1 jrtc-apps with its submodules — \[WSL\]

```bash
source ~/intact/env.sh
cd ~/intact/src
git clone https://github.com/microsoft/jrtc-apps.git
cd jrtc-apps
git checkout 9a096f1f031abc2469f677450aba2b1401fda697
git submodule update --init --recursive
./init_submodules.sh
git submodule status
```

**Expected** (first 8 characters must match; the trailing description may differ):

| Commit | Path |
| --- | --- |
| `a538e31b` | `Open5GS/p3/open5gs` (not used; core comes from apt) |
| `54f19e53` | `containers/Docker/mbedtls` |
| `ec29b0c1` | `containers/Docker/srsRAN_4G` — srsUE |
| `cd6811af` | `containers/Docker/srsRAN_Project` — the jbpf gNB |
| `bd009ef1` | `jbpf_protobuf` |

No line may start with `-` (not checked out) or `+` (wrong commit). **If not:** `git submodule update --init --recursive --force`, then re-check. Network drops during the recursive clone are common; rerunning resumes.

### 5.2 Select the bare-metal mode — \[WSL\]

Upstream reads `.env` and lets `.env.local` override it. These two values switch every upstream script from Docker builds to native builds, and turn on jrt-controller support.

```bash
cat > "$JRTC_ROOT/.env.local" <<'EOF'
SRS_JBPF_DOCKER=0
USE_JRTC=1
EOF
source ~/intact/env.sh
```

**Expected:** at most one line, `[env] N upstream tool(s) not built yet (normal before Part 6 ends)`. Upstream `set_vars.sh` checks for `jrtc-ctl`, `jbpf_lcm_cli`, `jbpf_protobuf_cli` and `setup_jrtc_env.sh`, which do not exist until 5.3 and Part 6 are done. The count drops to 0 after 6.5; `cat ~/intact/logs/set_vars.err` lists what is still missing. If your `env.sh` predates this fix you see the raw `Error: ... does not exist` lines instead; they are equally harmless, and rerunning the 2.3 block replaces them with the one-line count.

### 5.3 jrt-controller — \[WSL\]

```bash
cd ~/intact/src
git clone https://github.com/microsoft/jrt-controller.git
cd jrt-controller && ./init_submodules.sh
git log -1 --format='%H %cd'
```

### 5.4 Verify the source tree — \[SCRIPT\]

The most important line is the jbpf check: the gNB's jbpf agent and jrt-controller talk over a shared-memory protocol, and both must come from the **same jbpf commit**. On PC2 both were `b15dd030`.

```bash
cat > ~/intact/bin/intact-src-verify <<'EOF'
#!/usr/bin/env bash
# intact-src-verify: are all repos at the expected commits, and is jbpf identical on both sides?
set -uo pipefail
source ~/intact/env.sh
F=0; ok(){ echo "  [ OK ] $*"; }; bad(){ echo "  [FAIL] $*"; F=$((F+1)); }
want(){ local c; c=$(git -C "$1" rev-parse HEAD 2>/dev/null); [[ "$c" == $2* ]] && ok "$3 at ${c:0:10}" || bad "$3 at ${c:-missing}, expected $2"; }
want "$JRTC_ROOT"               9a096f1f "jrtc-apps"
want "$SRSRAN_DIR"              cd6811af "srsRAN_Project_jbpf (gNB)"
want "$SRSUE_DIR"               ec29b0c1 "srsRAN_4G (srsUE)"
want "$JRTC_ROOT/jbpf_protobuf" bd009ef1 "jbpf_protobuf"
j1=$(git -C "$SRSRAN_DIR/external/jbpf" rev-parse HEAD 2>/dev/null)
j2=$(git -C "$JRTC_PATH/jbpf-protobuf/jbpf" rev-parse HEAD 2>/dev/null)
if [ -n "$j1" ] && [ "$j1" = "$j2" ]; then ok "jbpf identical in gNB and jrt-controller (${j1:0:10})"; else bad "jbpf differs: gNB ${j1:-?} vs jrt-controller ${j2:-?}"; fi
for v in SRS_JBPF_DOCKER USE_JRTC SRSRAN_DIR JRTC_PATH JRTC_APPS JBPF_CODELETS JRTC_CTL_BIN JOBS; do echo "         $v=${!v:-<unset>}"; done
[ "${SRS_JBPF_DOCKER:-}" = 0 ] && ok "bare-metal mode" || bad "SRS_JBPF_DOCKER is '${SRS_JBPF_DOCKER:-}', must be 0"
[ "${SRSRAN_DIR:-}" = "$HOME/intact/src/jrtc-apps/containers/Docker/srsRAN_Project" ] && ok "SRSRAN_DIR points at the pinned tree" || bad "SRSRAN_DIR was changed by set_vars.sh: $SRSRAN_DIR"
[ $F -eq 0 ]
EOF
chmod +x ~/intact/bin/intact-src-verify && intact-src-verify
```

**Expected:** all `[ OK ]`.

**If the jbpf check fails** (jrt-controller has moved on since August), pin jrt-controller to the newest commit that still uses `jbpf-protobuf` at `bd009ef1`:

```bash
cd "$JRTC_PATH"
for c in $(git log --format=%H -- jbpf-protobuf); do
  s=$(git ls-tree "$c" jbpf-protobuf | awk '{print $3}')
  [[ "$s" == bd009ef1* ]] && { echo "use $c"; git checkout -q "$c"; break; }
done
git submodule update --init --recursive && intact-src-verify
```

**If `SRSRAN_DIR was changed`:** `set_vars.sh` overrides it. Add `SRSRAN_DIR=$HOME/intact/src/jrtc-apps/containers/Docker/srsRAN_Project` (with your real home path written out) to `$JRTC_ROOT/.env.local` and re-run.

### 5.5 Record provenance

```bash
mkdir -p "$CFG/provenance"
( cd "$JRTC_ROOT" && git rev-parse HEAD && git submodule status ) > "$CFG/provenance/jrtc-apps.txt"
( cd "$JRTC_PATH" && git rev-parse HEAD && git submodule status --recursive ) > "$CFG/provenance/jrt-controller.txt"
intact-cp mark part5-sources
```

## Part 6 — Build the gNB, srsUE, jrt-controller and the codelet

All four are compiled on this machine, so the compiler targets this CPU and the AVX-512 crash from your old laptop cannot occur. Every build writes a full log to `~/intact/logs/build-*.log`; when something fails, the last 40 lines of that log are what matters.

Run the builds in a **tmux** session so a closed terminal does not kill a 40-minute compile: `tmux new -s build`. Detach with `Ctrl-b d`, reattach with `tmux attach -t build`.

### 6.1 Build dependencies — \[WSL\]

First the union of what srsRAN, jbpf, srsUE and the codelet toolchain need:

```bash
source ~/intact/env.sh
sudo DEBIAN_FRONTEND=noninteractive apt install -y \
  build-essential cmake ninja-build pkg-config clang llvm lld \
  libfftw3-dev libmbedtls-dev libsctp-dev libyaml-cpp-dev libgtest-dev libzmq3-dev \
  libboost-all-dev libconfig++-dev libssl-dev libcunit1-dev libelf-dev \
  python3-dev python3-yaml protobuf-compiler libprotobuf-dev
```

jrt-controller documents its exact dependencies only inside its Dockerfiles. Rather than guess, this script reads them out of the Dockerfile for your Ubuntu version:

```bash
cat > ~/intact/bin/intact-deps-from-dockerfile <<'EOF'
#!/usr/bin/env python3
"""Print apt packages, pip packages and the Go version a Dockerfile installs."""
import re, sys
t = open(sys.argv[1]).read().replace("\\\n", " ")
apt, pip, go = set(), set(), set()
for cmd in re.split(r"&&|;|\n", t):
    m = re.search(r"\b(?:apt-get|apt)\s+(?:-\S+\s+)*install\s+(.*)", cmd)
    if m:
        apt |= {x.split("=")[0] for x in m.group(1).split() if not x.startswith("-") and re.match(r"^[a-z0-9][a-z0-9+.\-]*(=\S+)?$", x)}
    m = re.search(r"\bpip3?\s+install\s+(.*)", cmd)
    if m:
        pip |= {x for x in m.group(1).split() if not x.startswith("-")}
    go |= set(re.findall(r"go(\d+\.\d+(?:\.\d+)?)\.linux-amd64", cmd))
print("APT:", " ".join(sorted(apt)))
print("PIP:", " ".join(sorted(pip)))
print("GO:", " ".join(sorted(go)))
EOF
chmod +x ~/intact/bin/intact-deps-from-dockerfile
ls "$JRTC_PATH/deploy/"
DF="$JRTC_PATH/deploy/ubuntu24_04.Dockerfile"; [ -f "$DF" ] || DF="$JRTC_PATH/deploy/ubuntu22_04.Dockerfile"
intact-deps-from-dockerfile "$DF" | tee "$LOGS/jrtc-deps.txt"
```

**Expected:** three lines, `APT:` with a package list, `PIP:` (may be empty), `GO:` (a version, or empty). Install them, skipping any name that does not exist on Ubuntu 24.04:

```bash
PKGS=$(sed -n 's/^APT: //p' "$LOGS/jrtc-deps.txt")
OKP=$(for p in $PKGS; do apt-cache show "$p" >/dev/null 2>&1 && echo "$p"; done)
sudo DEBIAN_FRONTEND=noninteractive apt install -y $OKP
GOV=$(sed -n 's/^GO: //p' "$LOGS/jrtc-deps.txt" | awk '{print $NF}'); GOV=${GOV:-1.23.4}
curl -fsSL "https://go.dev/dl/go${GOV}.linux-amd64.tar.gz" -o /tmp/go.tgz && sudo rm -rf /usr/local/go && sudo tar -C /usr/local -xzf /tmp/go.tgz
go version
# Python packages. The Dockerfile installs "-r /jrtc/jbpf-protobuf/3p/nanopb/requirements.txt",
# a path inside the container. Drop container paths and use the same file from the local checkout.
PIPS=$(sed -n 's/^PIP: //p' "$LOGS/jrtc-deps.txt" | tr ' ' '\n' | grep -vE '^(-r|--requirement|/)' | tr '\n' ' ')
REQ="$JRTC_PATH/jbpf-protobuf/3p/nanopb/requirements.txt"
REQARGS=(); if [ -f "$REQ" ]; then REQARGS=(-r "$REQ"); else echo "WARN: $REQ missing -> cd $JRTC_PATH && git submodule update --init --recursive"; fi
python3 -m pip install --user --break-system-packages ctypesgen protobuf grpcio-tools $PIPS "${REQARGS[@]}"
command -v ctypesgen
python3 -c 'import ctypesgen, google.protobuf, grpc_tools; print("Python dependencies OK")'
```

**Expected:** `go version go1.2x.x linux/amd64` and a path ending in `.local/bin/ctypesgen`. **If not:** a `pip` failure on one package name → rerun without that name (`$PIPS` sometimes contains version pins with shell syntax).

### 6.2 srsRAN gNB with jbpf — \[SCRIPT\], 30–60 min

Upstream recipe: run `init_modules.sh`, then the normal srsRAN build with `-DENABLE_JBPF=on`, plus ZeroMQ for the virtual radio. `ENABLE_EXPORT` stays at its default, OFF: with ON, CMake stops with `export called with target "srsran_support" which requires target "jbpf" that is not in any export set`. Export only matters when srsRAN is installed as a library, and INTACT runs the binaries in place. There is no `AUTO_DETECT_ISA` option in this srsRAN version (CMake reports it as unused); the build already targets this CPU.

```bash
cat > ~/intact/bin/intact-build-gnb <<'EOF'
#!/usr/bin/env bash
# intact-build-gnb: srsRAN gNB with jbpf + ZeroMQ. Safe to rerun; make resumes where it stopped.
set -euo pipefail
source ~/intact/env.sh
cd "$SRSRAN_DIR"
[ -x ./init_modules.sh ] && ./init_modules.sh      # builds jbpf and the tools in out/bin
mkdir -p build && cd build
if ! cmake .. -DENABLE_JBPF=on -DENABLE_ZEROMQ=ON -DENABLE_EXPORT=OFF \
       -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF 2>&1 | tee "$LOGS/build-gnb-cmake.log"; then
  echo ">>> CMAKE FAILED, see $LOGS/build-gnb-cmake.log"; exit 1
fi
if ! make -j"$JOBS" 2>&1 | tee "$LOGS/build-gnb-make.log"; then
  echo ">>> BUILD FAILED, see the first 'error:' in $LOGS/build-gnb-make.log"; exit 1
fi
G="$SRSRAN_DIR/build/apps/gnb/gnb"
ls -la "$G"
ldd "$G" | grep -q libzmq && echo ">>> gnb links ZeroMQ" \
  || { echo ">>> STOP: gnb has no ZeroMQ radio. Send me: grep -in zmq $LOGS/build-gnb-cmake.log"; exit 1; }
ls -la "$SRSRAN_DIR"/out/bin/ 2>/dev/null || find "$SRSRAN_DIR" -name 'srsran_reverse_proxy' -type f
echo ">>> GNB BUILD OK"
EOF
chmod +x ~/intact/bin/intact-build-gnb && intact-build-gnb
```

**Expected:** the build ends at `[100%] Built target gnb`, then `>>> gnb links ZeroMQ`, a listing of `out/bin/` with at least `jbpf_lcm_cli`, `srsran_reverse_proxy` and `srsran_verifier_cli`, and `>>> GNB BUILD OK`. **Already built with the earlier script plus your `ENABLE_EXPORT=OFF` edit?** Do not rebuild. Check it in one line: `ldd "$SRSRAN_DIR/build/apps/gnb/gnb" | grep zmq` must print a `libzmq.so.5` line.

**If not:**

| Symptom | Fix |
| --- | --- |
| `c++: fatal error: Killed signal terminated program cc1plus` | Out of memory. `JOBS=4 intact-build-gnb` (it resumes where it stopped). |
| CMake `Could NOT find ...` naming a library | `apt-cache search <name>` and install its `-dev` package; rerun. |
| `out/bin` missing but `gnb` built | `cd $SRSRAN_DIR/build && make -j$JOBS srsran_reverse_proxy srsran_verifier_cli`; if those targets do not exist, `find $SRSRAN_DIR -name 'srsran_reverse_proxy*'` and note the path for Part 8. |
| Compile error inside `external/jbpf` | Submodules incomplete: `cd $JRTC_ROOT && git submodule update --init --recursive --force`, then rerun. |

### 6.3 srsUE — \[SCRIPT\], about 10 min

Only the `srsue` target is built, from the srsRAN\_4G tree that jrtc-apps pins (version 23.4.0). Upstream builds it with GCC 11; Ubuntu 24.04 has GCC 13, which adds new `array-bounds` warnings, and srsRAN\_4G turns every warning into an error. The script handles that in three ways, all tested on Ubuntu 24.04 with GCC 13.3. First, it fixes the one warning that is a real bug: `reset()` calls `reset_measurements(SRSRAN_MAX_CARRIERS)`, which resets every carrier and then falls through to write element 5 of 5-element arrays. Second, it configures with `-DENABLE_WERROR=OFF`, so the remaining false positives (in `rrc.cc` via the ASN.1 `dyn_array`) stay warnings. No other source edits are needed. Third, it uses `-DENABLE_RF_PLUGINS=OFF`, as upstream's `SRS-ue.Dockerfile` does, so ZeroMQ is linked into `srsue` itself. With plugins on, `srsue` would look for `libsrsran_rf_zmq.so` at run time, and `sudo` drops the library path that lets it find it.

```bash
cat > ~/intact/bin/intact-build-ue <<'EOF'
#!/usr/bin/env bash
# intact-build-ue: srsUE from the srsRAN_4G tree pinned by jrtc-apps, on Ubuntu 24.04 (GCC 13).
# Safe to rerun. Mirrors upstream SRS-ue.Dockerfile (-DENABLE_RF_PLUGINS=OFF), which uses GCC 11.
set -euo pipefail
source ~/intact/env.sh
cd "$SRSUE_DIR"
echo ">>> srsRAN_4G at $(git rev-parse --short HEAD), compiler: $(c++ --version | head -1)"

# 1. Real upstream bug that GCC 13 reports as array-bounds: reset() calls
#    reset_measurements(SRSRAN_MAX_CARRIERS); after resetting every carrier the function
#    falls through and writes index 5 of 5-element arrays. Fix: return after the loop.
python3 - "$SRSUE_DIR/srsue/src/phy/phy_common.cc" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); t = p.read_text()
old = "      reset_measurements(cc);\n    }\n  }\n"
new = "      reset_measurements(cc);\n    }\n    return;\n  }\n"
if new in t:
    print(">>> phy_common.cc: missing-return fix already present")
elif t.count(old) == 1:
    p.write_text(t.replace(old, new)); print(">>> phy_common.cc: missing-return fix applied")
else:
    sys.exit(">>> STOP: phy_common.cc does not look as expected; send: sed -n 650,662p " + str(p))
PY
mkdir -p "$CFG/provenance"
git diff -- srsue/src/phy/phy_common.cc > "$CFG/provenance/srsue-phy_common-return.patch"

# 2. Configure. ENABLE_WERROR=OFF: GCC 13 raises false-positive array-bounds warnings
#    (rrc.cc / asn1 dyn_array) and srsRAN_4G turns every warning into an error.
#    ENABLE_RF_PLUGINS=OFF: ZMQ is linked into srsue itself, so no .so has to be found at run time.
mkdir -p build && cd build
rm -f CMakeCache.txt
if ! cmake .. -DCMAKE_BUILD_TYPE=Release -DENABLE_WERROR=OFF -DENABLE_RF_PLUGINS=OFF ${UE_EXTRA_FLAGS:-} \
     2>&1 | tee "$LOGS/build-ue-cmake.log"; then
  echo ">>> CMAKE FAILED, see $LOGS/build-ue-cmake.log"; exit 1
fi
if grep 'CMAKE_CXX_FLAGS is' "$LOGS/build-ue-cmake.log" | grep -qE -- '-Werror( |$)'; then
  echo ">>> STOP: -Werror is still in the C++ flags"; exit 1
fi
grep -q 'Found libZEROMQ' "$LOGS/build-ue-cmake.log" && echo ">>> ZeroMQ found" \
  || { echo ">>> STOP: ZeroMQ not found -> sudo apt install -y libzmq3-dev, then rerun"; exit 1; }

# 3. Build only the srsue target
if ! make -j"$JOBS" srsue 2>&1 | tee "$LOGS/build-ue-make.log"; then
  echo ">>> BUILD FAILED, see the first 'error:' in $LOGS/build-ue-make.log"; exit 1
fi
B="$SRSUE_DIR/build/srsue/src/srsue"
ls -la "$B"
"$B" --version 2>&1 | head -3
ldd "$B" | grep -q libzmq && echo ">>> srsue links ZeroMQ directly" \
  || { echo ">>> STOP: srsue does not link libzmq"; exit 1; }
echo ">>> array-bounds warnings (expected, harmless): $(grep -c 'Warray-bounds' "$LOGS/build-ue-make.log" || true)"
echo ">>> SRSUE BUILD OK"
EOF
chmod +x ~/intact/bin/intact-build-ue && intact-build-ue
```

**Expected** (about 10 min with JOBS=3): `missing-return fix applied` (or `already present` on a rerun), `>>> ZeroMQ found`, the build ending at `[100%] Built target srsue`, `Version 23.4.0`, `>>> srsue links ZeroMQ directly`, a warning count of 1 (the rrc.cc false positive; 0 on a rerun that recompiles nothing), then `>>> SRSUE BUILD OK`. The patch is recorded in `~/intact/configs/provenance/srsue-phy_common-return.patch` (one added `return;`).

**If not:** a new `error:` that is not `array-bounds` → send me the 30 lines above the first `error:` in `~/intact/logs/build-ue-make.log`. Do not hand-edit other srsRAN\_4G sources; the ASN.1 `dyn_array::resize` warning in `rrc.cc` is a false positive and needs no patch. Last resort only, if the pinned tree cannot be built: the upstream 23.11 release (also 5G SA over ZMQ), built with the same script.

```bash
# LAST RESORT ONLY
git clone -b release_23_11 --depth 1 https://github.com/srsran/srsRAN_4G.git ~/intact/src/srsRAN_4G
sed -i 's|^export SRSUE_DIR=.*|export SRSUE_DIR="$INTACT/src/srsRAN_4G"|' ~/intact/env.sh
source ~/intact/env.sh && intact-build-ue
```

### 6.4 jrt-controller — \[SCRIPT\], about 10 min

The Python app loader is required: `slice_mgmt` is a Python app. The script checks for it and, if the default build skipped it, finds the CMake option that enables it and rebuilds.

```bash
cat > ~/intact/bin/intact-build-jrtc <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
source ~/intact/env.sh
cd "$JRTC_PATH"
source ./setup_jrtc_env.sh
mkdir -p build && cd build
cmake .. ${JRTC_EXTRA_FLAGS:-} 2>&1 | tee "$LOGS/build-jrtc-cmake.log"
make -j"$JOBS" 2>&1 | tee "$LOGS/build-jrtc-make.log"
[ "${PIPESTATUS[0]}" -eq 0 ] || { echo ">>> BUILD FAILED, see $LOGS/build-jrtc-make.log"; exit 1; }
if ! ls "$JRTC_PATH"/out/lib/libjrtc_pythonapp_loader.so >/dev/null 2>&1; then
  OPT=$(grep -rhoE 'option\(\s*[A-Z_]*PYTHON[A-Z_]*' --include=CMakeLists.txt "$JRTC_PATH" | sed -E 's/option\(\s*//' | sort -u | head -1)
  echo ">>> Python loader missing; rebuilding with -D${OPT:-BUILD_PYTHON_LOADER}=ON"
  cmake .. -D"${OPT:-BUILD_PYTHON_LOADER}"=ON && make -j"$JOBS"
fi
ls -la "$JRTC_PATH"/out/bin/jrtc "$JRTC_PATH"/out/bin/jrtc-ctl "$JRTC_PATH"/out/lib/libjrtc_pythonapp_loader.so
EOF
chmod +x ~/intact/bin/intact-build-jrtc && intact-build-jrtc
```

**Expected:** three files listed: `out/bin/jrtc`, `out/bin/jrtc-ctl`, `out/lib/libjrtc_pythonapp_loader.so`.

**If not:** `out/` elsewhere → `find $JRTC_PATH -name jrtc -type f -perm -u+x` and set `JRTC_PATH` accordingly. Python loader still missing → look at `$JRTC_PATH/helper_build_files/build_jrtc.sh`; upstream builds it with `BUILD_PYTHON_LOADER=1`, so `cd $JRTC_PATH && BUILD_PYTHON_LOADER=1 ./helper_build_files/build_jrtc.sh` is the second route. Go errors → `go version` must be the one from 6.1.

### 6.5 jbpf-protobuf (codelet serializer tool) — \[WSL\]

The bare-metal guide requires building the `jbpf_protobuf` submodule once. It provides `jbpf_protobuf_cli`, which generates the codelet's message serializers.

```bash
source ~/intact/env.sh
cd "$JRTC_ROOT/jbpf_protobuf"
[ -x ./init_submodules.sh ] && ./init_submodules.sh
for f in setup_*env*.sh; do [ -f "$f" ] && source "./$f"; done
cmake -B build && cmake --build build -j"$JOBS" 2>&1 | tee "$LOGS/build-jbpfp.log"
find "$JRTC_ROOT/jbpf_protobuf" -name jbpf_protobuf_cli -type f -perm -u+x
```

**Expected:** one path to `jbpf_protobuf_cli`. Note it; Part 6.6 checks the codelet Makefiles can find it.

### 6.6 Build the `slice_mgmt` codelet — \[SCRIPT\]

The script first does a **dry run** of the codelet Makefile and checks that every include directory and tool it references exists. That turns an opaque compiler error into a plain list of what is missing.

```bash
cat > ~/intact/bin/intact-build-codelet <<'EOF'
#!/usr/bin/env bash
set -uo pipefail
source ~/intact/env.sh
C="${1:-slice_mgmt}"
cd "$JBPF_CODELETS"
echo "== dry run: what the Makefile will use"
dry=$(make -n -C "$C" 2>&1) || { echo "$dry" | tail -20; echo "FAIL: make dry run"; exit 1; }
miss=0
for d in $(echo "$dry" | grep -oE '(^| )-I[^ ]+' | sed 's/^ *-I//' | sort -u); do
  [ -d "$d" ] && echo "  include ok   $d" || { echo "  MISSING DIR  $d"; miss=1; }
done
for b in $(echo "$dry" | grep -oE '(/[^ ;"]+/)?(clang\+\+|jbpf_protobuf_cli|ctypesgen|srsran_verifier_cli)' | sort -u); do
  { command -v "$b" >/dev/null || [ -x "$b" ]; } && echo "  tool ok      $b" || { echo "  MISSING TOOL $b"; miss=1; }
done
[ $miss -eq 0 ] || { echo "FAIL: fix the MISSING lines first (see 6.6 'If not')"; exit 1; }
echo "== build"
./make.sh -d "$C" -o cleanall >/dev/null 2>&1
./make.sh -d "$C" 2>&1 | tee "$LOGS/build-codelet-$C.log"
echo "== artifacts"
ls -la "$C"/*.o "$C"/*.pb "$C"/*.py "$C"/*serializer.so 2>&1
EOF
chmod +x ~/intact/bin/intact-build-codelet && intact-build-codelet slice_mgmt
```

**Expected:** every line `include ok` / `tool ok`, the verifier prints `Program terminates within ... instructions`, and the artifact list shows the same six files PC2 produced: `slice_mgmt.o`, `slice_mgmt.pb`, `slice_mgmt.py`, `slice_mgmt:slice_mgmt_req_serializer.so`, `slice_mgmt:slice_mgmt_ind_serializer.so` (plus `slice_mgmt.pb.h`). Lines like `Code is unreachable after ...` are normal verifier chatter.

**If not:**

| Symptom | Meaning | Fix |
| --- | --- | --- |
| `MISSING DIR /src/...` or `/nanopb` | The Makefile still uses the Docker build paths | `SRS_JBPF_DOCKER` is not 0 in this shell; `source ~/intact/env.sh; echo $SRS_JBPF_DOCKER` must print 0 |
| `MISSING DIR $SRSRAN_DIR/out/inc` | srsRAN build did not install jbpf headers | `cd $SRSRAN_DIR/build && make -j$JOBS install` is **not** wanted; instead check `find $SRSRAN_DIR -type d -name inc -path '*out*'` and report |
| `MISSING TOOL jbpf_protobuf_cli` | 6.5 output not on the expected path | Add the directory from 6.5 to `PATH` in `env.sh` |
| `MISSING TOOL ctypesgen` | pip user bin not on PATH | `source ~/intact/env.sh` (it adds `~/.local/bin`) |
| Anything else, or you cannot tell | The codelet Makefiles expect a variable I could not see in advance | Run the dump below and send me the file; I will give you the exact fix instead of guessing |

```bash
{ echo "### Makefile.defs"; cat "$JBPF_CODELETS/Makefile.defs"; echo "### Makefile.common"; cat "$JBPF_CODELETS/Makefile.common";
  echo "### set_vars.sh"; cat "$JRTC_ROOT/set_vars.sh"; echo "### .env"; cat "$JRTC_ROOT/.env"; echo "### env"; env | sort | grep -E 'SRS|JRTC|JBPF|PATH';
  echo "### dry run"; make -n -C "$JBPF_CODELETS/slice_mgmt" 2>&1 | head -60; } > ~/intact/logs/codelet-build-dump.txt; ls -la ~/intact/logs/codelet-build-dump.txt
```

### 6.7 Verify every build — \[SCRIPT\]

```bash
cat > ~/intact/bin/intact-build-verify <<'EOF'
#!/usr/bin/env bash
set -uo pipefail
source ~/intact/env.sh
F=0; ok(){ echo "  [ OK ] $*"; }; bad(){ echo "  [FAIL] $*"; F=$((F+1)); }
chk(){ [ -e "$1" ] && ok "$2: $1" || bad "$2 missing: $1"; }
PROXY=$(ls "$SRSRAN_DIR"/out/bin/srsran_reverse_proxy 2>/dev/null || find "$SRSRAN_DIR" -name srsran_reverse_proxy -type f -perm -u+x | head -1)
chk "$SRSRAN_DIR/build/apps/gnb/gnb" gNB
chk "${PROXY:-$SRSRAN_DIR/out/bin/srsran_reverse_proxy}" "reverse proxy"
chk "$SRSUE_DIR/build/srsue/src/srsue" srsUE
ldd "$SRSRAN_DIR/build/apps/gnb/gnb" 2>/dev/null | grep -q libzmq && ok "gNB has the ZeroMQ radio" || bad "gNB not linked to libzmq (6.2)"
ldd "$SRSUE_DIR/build/srsue/src/srsue" 2>/dev/null | grep -q libzmq && ok "srsUE has ZeroMQ built in" || bad "srsUE not linked to libzmq (6.3, ENABLE_RF_PLUGINS=OFF)"
chk "$JRTC_PATH/out/bin/jrtc" jrt-controller
chk "$JRTC_CTL_BIN" jrtc-ctl
chk "$JRTC_PATH/out/lib/libjrtc_pythonapp_loader.so" "python app loader"
for f in slice_mgmt.o slice_mgmt.pb slice_mgmt.py 'slice_mgmt:slice_mgmt_req_serializer.so' 'slice_mgmt:slice_mgmt_ind_serializer.so'; do chk "$JBPF_CODELETS/slice_mgmt/$f" codelet; done
"$SRSRAN_DIR/build/apps/gnb/gnb" --version 2>&1 | grep -m1 -i 'version' | sed 's/^/         /'
sudo dmesg -T 2>/dev/null | grep -i 'invalid opcode' | grep -v 'cmTC_' | grep -q . && bad "kernel logged an invalid opcode (SIGILL) outside CMake probes" || ok "no invalid-opcode traps (CMake cmTC_ probes ignored)"
echo "${PROXY}" > "$HOME/intact/configs/proxy_path.txt"
[ $F -eq 0 ] && echo "ALL BUILDS PRESENT"; [ $F -eq 0 ]
EOF
chmod +x ~/intact/bin/intact-build-verify && intact-build-verify
```

**Expected:** all `[ OK ]`, a version line such as `srsRAN 5G gNB version 25.x`, then `ALL BUILDS PRESENT`.

### 6.8 Snapshot CP2

```bash
intact-cp mark part6-builds
```

**\[PowerShell\]** Snapshot procedure from 1.7 with the name `intact-CP2`. This one matters most: it preserves an hour of compilation.

## Part 7 — Configuration files

Three files drive the run: the gNB YAML (two variants, with and without the jbpf section), the srsUE config, and later the slice\_mgmt deployment. All live in `~/intact/configs` and are committed to git, so `git -C ~/intact/configs diff` shows any change and `git checkout` undoes it.

The radio settings (band 3, 10 MHz, 15 kHz SCS, 11.52 Msps, 52 PRB) are the combination that produced RACH, RRC and CQI 15 on PC2. Only the addresses change: the GRC broker is gone, so gNB and UE connect directly.

### 7.1 gNB config — \[SCRIPT\]

```bash
source ~/intact/env.sh
cat > "$CFG/gnb_zmq.yaml" <<'EOF'
# srsRAN_Project_jbpf gNB: ZMQ radio, Open5GS native on loopback. No jbpf section.
cu_cp:
  inactivity_timer: 7200   # default 120 s releases an idle UE
  amf:
    addr: 127.0.0.5          # Open5GS AMF NGAP (package default)
    port: 38412
    bind_addr: 127.0.0.1
    supported_tracking_areas:
      - tac: 1
        plmn_list:
          - plmn: "00101"
            tai_slice_support_list:
              - sst: 1

ru_sdr:
  device_driver: zmq
  device_args: tx_port=tcp://127.0.0.1:2000,rx_port=tcp://127.0.0.1:2001,base_srate=11.52e6
  srate: 11.52
  tx_gain: 75
  rx_gain: 75

cell_cfg:
  dl_arfcn: 368500           # band 3, 1842.5 MHz, as on PC2
  band: 3
  channel_bandwidth_MHz: 10
  common_scs: 15
  plmn: "00101"
  tac: 1
  pci: 1
  pdcch:
    common:
      ss0_index: 0             # SearchSpace#0 that srsUE supports
      coreset0_index: 6        # CORESET#0 that srsUE supports (10 MHz, band 3)
    dedicated:
      ss2_type: common       # srsUE needs common search space
      dci_format_0_1_and_1_1: false   # and fallback DCI formats
  pdsch:
    mcs_table: qam64
  pusch:
    mcs_table: qam64
  prach:
    prach_config_index: 1
    total_nof_ra_preambles: 64
    nof_ssb_per_ro: 1
    nof_cb_preambles_per_ssb: 64

metrics:
  autostart_stdout_metrics: true   # prints the per-UE scheduler table every second

log:
  filename: /tmp/gnb.log
  all_level: warning
EOF

# jbpf variant: the section from upstream's bare-metal guide, verbatim
{ cat "$CFG/gnb_zmq.yaml"; cat <<'EOF'

jbpf:
  jbpf_run_path: "/tmp"
  jbpf_namespace: "jbpf"
  jbpf_enable_ipc: 1
  jbpf_standalone_io_out_ip: "127.0.0.1"
  jbpf_standalone_io_out_port: 20788
  jbpf_standalone_io_in_port: 30400
  jbpf_standalone_io_policy: 0
  jbpf_standalone_io_priority: 0
  jbpf_io_mem_size_mb: 1024
  jbpf_ipc_mem_name: "jrt_controller"
  jbpf_enable_lcm_ipc: 1
  jbpf_lcm_ipc_name: "jbpf_lcm_ipc"
  jbpf_agent_cpu: 0
  jbpf_agent_policy: 1
  jbpf_agent_priority: 30
  jbpf_maint_cpu: 0
  jbpf_maint_policy: 0
  jbpf_maint_priority: 0
EOF
} > "$CFG/gnb_zmq_jbpf.yaml"
sed -i 's/^# srsRAN_Project_jbpf gNB.*/# srsRAN_Project_jbpf gNB: ZMQ radio, Open5GS on loopback, jbpf -> jrt-controller./' "$CFG/gnb_zmq_jbpf.yaml"
python3 -c "import yaml; [yaml.safe_load(open(f)) for f in ['$CFG/gnb_zmq.yaml','$CFG/gnb_zmq_jbpf.yaml']]; print('YAML OK')"
```

**Expected:** `YAML OK`.

Two deliberate omissions: there is **no NG-U bind** setting, because srsRAN's default (`127.0.0.1`) is exactly right here and leaving it out avoids a version-specific key name; and there is **no CPU pinning**.

**If you harvested PC2's config:** compare key structure (not values) to catch any key this srsRAN version spells differently:

```bash
python3 - <<'PY'
import yaml, os
def keys(d, p=""):
    out = set()
    if isinstance(d, dict):
        for k, v in d.items(): out |= {p + k} | keys(v, p + k + ".")
    elif isinstance(d, list):
        for v in d: out |= keys(v, p)
    return out
cfg = os.path.expanduser("~/intact/configs")
mine = keys(yaml.safe_load(open(f"{cfg}/gnb_zmq_jbpf.yaml")))
pc2f = f"{cfg}/pc2/gnb_config_rendered.yaml"
if os.path.exists(pc2f):
    pc2 = keys(yaml.safe_load(open(pc2f)))
    print("In mine but NOT in PC2's working config (check spelling):")
    for k in sorted(mine - pc2): print("   ", k)
else:
    print("no PC2 harvest found; skip")
PY
```

Keys listed there are not necessarily wrong (PC2 used a different template), but any that look like a spelling variant of a PC2 key should be renamed to PC2's spelling.

### 7.2 srsUE config — \[SCRIPT\]

This is PC2's working `ue_zmq.conf` with two changes: direct ZMQ addresses instead of GRC, and `netns = ue1`, which puts the UE's data interface in its own network namespace. Without the namespace, the UE's `10.45.0.x` address would collide with the host's route to `ogstun` (`10.45.0.0/16`) and traffic would short-circuit inside the host instead of crossing the radio.

```bash
cat > "$CFG/ue_zmq.conf" <<'EOF'
[rf]
freq_offset = 0
tx_gain = 50
rx_gain = 40
srate = 11.52e6
nof_antennas = 1
device_name = zmq
device_args = tx_port=tcp://127.0.0.1:2001,rx_port=tcp://127.0.0.1:2000,base_srate=11.52e6

[rat.eutra]
nof_carriers = 0

[rat.nr]
bands = 3
nof_carriers = 1
max_nof_prb = 52
nof_prb = 52

[pcap]
enable = none

[log]
all_level = info
phy_lib_level = none
all_hex_limit = 32
filename = /tmp/ue.log
file_max_size = -1

[usim]
mode = soft
algo = milenage
opc  = 63BFA50EE6523365FF14C1F45F88737D
k    = 00112233445566778899aabbccddeeff
imsi = 001010123456780
imei = 353490069873310

[rrc]
release = 15
ue_category = 4

[nas]
apn = internet
apn_protocol = ipv4

[gw]
netns = ue1
ip_devname = tun_srsue
ip_netmask = 255.255.255.0

[gui]
enable = false
EOF
git -C "$CFG" add -A && git -C "$CFG" commit -qm "gNB and UE configs, stage 1 (no slicing)" && echo committed
```

### 7.3 Cross-check gNB, UE and core — \[SCRIPT\]

Every mismatch that cost time on PC2 is checked here before anything starts: ZMQ ports crossed correctly, sample rates equal, PLMN and TAC identical in gNB and AMF, the UE's IMSI present in the database with the same K, OPc and APN, and (once slices are on) the slice SD consistent across UE, gNB, AMF and subscriber.

```bash
cat > ~/intact/bin/intact-cfg-check <<'EOF'
#!/usr/bin/env python3
"""Cross-check gNB YAML, srsUE conf, Open5GS AMF config and the subscriber DB."""
import os, re, sys, json, subprocess, configparser, yaml
cfg = os.path.expanduser("~/intact/configs")
gnbf = sys.argv[1] if len(sys.argv) > 1 else f"{cfg}/gnb_zmq_jbpf.yaml"
F = 0
def ok(m): print("  [ OK ]", m)
def bad(m):
    global F; F += 1; print("  [FAIL]", m)
def chk(c, m): (ok if c else bad)(m)
g = yaml.safe_load(open(gnbf))
u = configparser.ConfigParser(strict=False); u.read(f"{cfg}/ue_zmq.conf")
ga = dict(x.split("=", 1) for x in g["ru_sdr"]["device_args"].split(","))
ua = dict(x.split("=", 1) for x in u["rf"]["device_args"].split(","))
port = lambda s: s.rsplit(":", 1)[1]
chk(port(ga["tx_port"]) == port(ua["rx_port"]), f"gNB TX {ga['tx_port']} -> UE RX {ua['rx_port']}")
chk(port(ua["tx_port"]) == port(ga["rx_port"]), f"UE TX {ua['tx_port']} -> gNB RX {ga['rx_port']}")
chk(ga.get("base_srate") == ua.get("base_srate"), f"base_srate gNB {ga.get('base_srate')} / UE {ua.get('base_srate')}")
plmn, tac = str(g["cell_cfg"]["plmn"]), int(g["cell_cfg"]["tac"])
amf = open("/etc/open5gs/amf.yaml").read()
mcc = set(re.findall(r"^\s*mcc:\s*(\d+)", amf, re.M)); mnc = set(re.findall(r"^\s*mnc:\s*(\d+)", amf, re.M))
chk(mcc == {plmn[:3]} and mnc == {plmn[3:]}, f"gNB PLMN {plmn} vs AMF mcc {mcc} mnc {mnc}")
tacs = set(re.findall(r"^\s*tac:\s*\[?\s*(\d+)", amf, re.M))
chk(str(tac) in tacs, f"gNB TAC {tac} vs AMF TAC {tacs}")
chk(g["cu_cp"]["amf"]["addr"] == "127.0.0.5", f"gNB AMF address {g['cu_cp']['amf']['addr']}")
imsi = u["usim"]["imsi"].strip()
q = f'JSON.stringify(db.subscribers.findOne({{imsi:"{imsi}"}},{{_id:0,security:1,slice:1}}))'
out = subprocess.run(["mongosh", "--quiet", "open5gs", "--eval", q], capture_output=True, text=True).stdout.strip()
sub = json.loads(out) if out and out != "null" else None
chk(sub is not None, f"subscriber {imsi} exists in MongoDB")
if sub:
    s = sub["security"]
    chk(s["k"].lower() == u["usim"]["k"].strip().lower(), "K matches UE")
    chk((s.get("opc") or "").lower() == u["usim"]["opc"].strip().lower(), "OPc matches UE")
    dnns = {sess["name"] for sl in sub["slice"] for sess in sl["session"]}
    chk(u["nas"]["apn"].strip() in dnns, f"UE APN '{u['nas']['apn'].strip()}' in subscriber DNNs {dnns}")
    if u.has_section("slicing") and u["slicing"].get("enable", "false").strip() == "true":
        usd = int(u["slicing"]["nssai-sd"]); ust = int(u["slicing"]["nssai-sst"])
        gsl = {(int(x["sst"]), int(x["sd"])) for x in g["cell_cfg"].get("slicing", []) if "sd" in x}
        chk((ust, usd) in gsl, f"UE slice {ust}/{usd} configured in gNB cell_cfg.slicing {sorted(gsl)}")
        ssd = {(int(sl["sst"]), int(str(sl["sd"]), 16)) for sl in sub["slice"] if "sd" in sl}
        chk((ust, usd) in ssd, f"UE slice {ust}/{usd} provisioned for subscriber {sorted(ssd)}")
        asd = {int(x, 16) for x in re.findall(r"^\s*sd:\s*([0-9A-Fa-f]{6})\b", amf, re.M)}
        chk(usd in asd, f"UE SD {usd} listed in AMF plmn_support {sorted(asd)}")
print("\nCONFIG CONSISTENT" if F == 0 else f"\n{F} MISMATCH(ES)")
sys.exit(1 if F else 0)
EOF
chmod +x ~/intact/bin/intact-cfg-check && intact-cfg-check
```

**Expected:** all `[ OK ]`, then `CONFIG CONSISTENT`. Fix any `[FAIL]` before Part 8; each line names both sides of the mismatch.

```bash
intact-cp mark part7-configs
```

## Part 8 — Operations scripts: start, stop, health

Four small launchers run one process each in the foreground and copy its output to `~/intact/logs/run/<name>.out`. `intact-up` starts them in order inside a tmux session and **waits for each one's ready line before starting the next**, so a failure stops at the component that caused it. `intact-down` always stops the UE and gNB together, which is the ZMQ rule from PC2.

### 8.1 Let `sudo` work across terminals — \[WSL\]

The gNB, UE, proxy and jrt-controller need root (real-time priority, TUN devices, locked shared memory). By default `sudo` remembers your password per terminal, so a process started in a new tmux window would sit invisibly at a password prompt. This makes one `sudo` authentication valid for all terminals for 15 minutes:

```bash
echo "Defaults:$USER timestamp_type=global" | sudo tee /etc/sudoers.d/intact-sudo >/dev/null
sudo chmod 440 /etc/sudoers.d/intact-sudo && sudo visudo -c
```

**Expected:** `/etc/sudoers.d/intact-sudo: parsed OK`. **If `visudo` reports an error:** `sudo rm /etc/sudoers.d/intact-sudo` immediately and retype the line.

### 8.2 The four launchers — \[SCRIPT\]

```bash
cat > ~/intact/bin/intact-run-jrtc <<'EOF'
#!/usr/bin/env bash
# Foreground: jrt-controller. REST API on :3001, shared memory 'jrt_controller'.
source ~/intact/env.sh
cd "$JRTC_PATH" && source ./setup_jrtc_env.sh
export JRTC_APP_PATH="$JRTC_PATH/out/lib/"
cd "$JRTC_PATH/out/bin"
exec > >(tee "$LOGS/run/jrtc.out") 2>&1
echo "[intact] jrt-controller start $(date -Is)"
exec sudo -E env "PATH=$PATH" "LD_LIBRARY_PATH=$LD_LIBRARY_PATH" "JRTC_APP_PATH=$JRTC_APP_PATH" PYTHONUNBUFFERED=1 ./jrtc
EOF

cat > ~/intact/bin/intact-run-gnb <<'EOF'
#!/usr/bin/env bash
# Foreground: gNB. Argument: config file (default: the jbpf variant).
source ~/intact/env.sh
C="${1:-$CFG/gnb_zmq_jbpf.yaml}"
cd "$SRSRAN_DIR/build/apps/gnb"
exec > >(tee "$LOGS/run/gnb.out") 2>&1
echo "[intact] gNB start $(date -Is) config=$C"
exec sudo stdbuf -oL -eL ./gnb -c "$C"     # line-buffered: 'gNB started' shows up live
EOF

cat > ~/intact/bin/intact-run-proxy <<'EOF'
#!/usr/bin/env bash
# Foreground: jbpf reverse proxy. REST :30450 -> gNB LCM socket.
source ~/intact/env.sh
P=$(cat "$CFG/proxy_path.txt" 2>/dev/null); P="${P:-$SRSRAN_DIR/out/bin/srsran_reverse_proxy}"
exec > >(tee "$LOGS/run/proxy.out") 2>&1
echo "[intact] reverse proxy start $(date -Is)"
exec sudo -E "$P" --host-port 30450 --address "/tmp/jbpf/jbpf_lcm_ipc"
EOF

cat > ~/intact/bin/intact-run-ue <<'EOF'
#!/usr/bin/env bash
# Foreground: srsUE. Data interface tun_srsue lands in netns ue1.
source ~/intact/env.sh
sudo ip netns list | grep -qw ue1 || sudo ip netns add ue1
cd "$SRSUE_DIR/build/srsue/src"
exec > >(tee "$LOGS/run/ue.out") 2>&1
echo "[intact] srsUE start $(date -Is)"
exec sudo stdbuf -oL -eL ./srsue "${1:-$CFG/ue_zmq.conf}"
EOF
chmod +x ~/intact/bin/intact-run-*
```

### 8.3 `intact-up`: ordered start with readiness gates — \[SCRIPT\]

```bash
cat > ~/intact/bin/intact-up <<'EOF'
#!/usr/bin/env bash
# intact-up [--no-jbpf]  start jrt-controller -> gNB -> proxy -> UE in tmux session 'intact'.
# Each step waits for its ready signal. The core must already be healthy.
set -uo pipefail
source ~/intact/env.sh
R="$LOGS/run"; mkdir -p "$R"
JBPF=1; [ "${1:-}" = "--no-jbpf" ] && JBPF=0
GCFG="$CFG/gnb_zmq_jbpf.yaml"; [ $JBPF -eq 0 ] && GCFG="$CFG/gnb_zmq.yaml"
NEVER='^\b$'
die(){ echo; echo "STOPPED: $*"; echo "Live output: $R/*.out   Inspect: tmux attach -t intact   Stop all: intact-down"; exit 1; }
wait_log(){ # file ok-pattern seconds label [error-pattern]
  local f=$1 okp=$2 t=$3 lab=$4 errp=${5:-$NEVER} i
  for ((i=0; i<t; i++)); do
    grep -qE "$okp" "$f" 2>/dev/null && { echo "  ready: $lab (${i}s)"; return 0; }
    if grep -qE "$errp" "$f" 2>/dev/null; then echo "  ERROR during $lab:"; grep -E "$errp" "$f" | tail -5 | sed 's/^/    /'; return 1; fi
    sleep 1
  done
  echo "  timeout (${t}s) waiting for $lab; last lines:"; tail -12 "$f" 2>/dev/null | sed 's/^/    /'; return 1; }
wait_port(){ local p=$1 t=$2 lab=$3 i; for ((i=0; i<t; i++)); do ss -Hltn "sport = :$p" | grep -q . && { echo "  ready: $lab on :$p (${i}s)"; return 0; }; sleep 1; done; echo "  timeout: $lab not listening on :$p"; return 1; }

echo "=== 0. Pre-checks"
tmux has-session -t intact 2>/dev/null && die "session 'intact' already exists (run intact-down)"
pgrep -x 'gnb|srsue|jrtc|jrtc_main' >/dev/null && die "gnb/srsue/jrtc already running (run intact-down)"
sudo -v || die "sudo authentication failed"
intact-core-health >/dev/null 2>&1 || { intact-core-health; die "core not ready (intact-core-restart)"; }
echo "  core ready"
intact-cfg-check "$GCFG" >/dev/null 2>&1 || { intact-cfg-check "$GCFG"; die "config mismatch"; }
echo "  configs consistent"
sudo rm -f /dev/shm/jbpf_io_* 2>/dev/null; sudo rm -rf /tmp/jbpf 2>/dev/null; rm -f "$R"/*.out
sudo ip netns list | grep -qw ue1 || sudo ip netns add ue1
tmux new-session -d -s intact -n shell

if [ $JBPF -eq 1 ]; then
  echo "=== 1. jrt-controller"
  tmux new-window -t intact -n jrtc intact-run-jrtc
  wait_port 3001 40 "jrt-controller REST" || die "jrt-controller did not start"
fi

echo "=== 2. gNB"
tmux new-window -t intact -n gnb "intact-run-gnb $GCFG"
GERR='srsRAN ERROR|Failed to connect to AMF|Illegal instruction|terminate called|Segmentation|was not expected|not a valid|INI was not able|Error:|could not be parsed'
wait_log "$R/gnb.out" 'gNB started' 90 "gNB (N2 to AMF)" "$GERR" || die "gNB failed (Part 11: gNB rows)"
grep -q 'N2: Connection to AMF.*completed' "$R/gnb.out" && echo "  N2 up: $(grep -o 'N2: Connection to AMF[^ ]* on [^ ]*' "$R/gnb.out" | tail -1)"
if [ $JBPF -eq 1 ]; then
  grep -q 'Registration succeeded' "$R/gnb.out" && echo "  jbpf agent registered with jrt-controller" || echo "  WARNING: no 'Registration succeeded' from jbpf (check $R/gnb.out)"
  echo "=== 3. jbpf reverse proxy"
  tmux new-window -t intact -n proxy intact-run-proxy
  wait_port 30450 20 "reverse proxy" || die "reverse proxy did not start"
fi

echo "=== 4. UE"
tmux new-window -t intact -n ue intact-run-ue
UERR='Registration Reject|registration reject|Received RRC Release|Unhandled Registration|Illegal instruction|Error opening'
for ((i=0; i<120; i++)); do
  ip4=$(sudo ip netns exec ue1 ip -4 -br addr show tun_srsue 2>/dev/null | awk '{print $3}')
  [ -n "$ip4" ] && break
  if grep -qE "$UERR" "$R/ue.out" 2>/dev/null; then grep -E "$UERR" "$R/ue.out" | tail -3 | sed 's/^/    /'; die "UE attach rejected (Part 11: attach rows)"; fi
  sleep 1
done
[ -n "${ip4:-}" ] || { tail -15 "$R/ue.out" | sed 's/^/    /'; die "UE got no IP within 120 s (Part 11)"; }
sudo ip netns exec ue1 ip route replace default dev tun_srsue
echo "  ready: UE attached, IP ${ip4}, default route via tun_srsue (${i}s)"
echo
echo "STACK UP. Windows: tmux attach -t intact  (Ctrl-b n/p to switch, Ctrl-b d to detach)"
EOF
chmod +x ~/intact/bin/intact-up
```

### 8.4 `intact-down` and `intact-health` — \[SCRIPT\]

```bash
cat > ~/intact/bin/intact-down <<'DOWNEOF'
#!/usr/bin/env bash
# intact-down: stop the UE(s), the multi-UE broker and the gNB TOGETHER (ZMQ rule), then the reverse proxy,
# jrt-controller, the KPM xApp and the nearRT-RIC (Appendix F), and iperf3 servers. The core stays up.
sudo pkill -INT -x srsue 2>/dev/null; sudo pkill -INT -x gnb 2>/dev/null; sleep 3
sudo pkill -KILL -x srsue 2>/dev/null; sudo pkill -KILL -x gnb 2>/dev/null
pkill -INT -f '[i]ntact-zmq-broker --ues' 2>/dev/null; sleep 1; pkill -KILL -f '[i]ntact-zmq-broker --ues' 2>/dev/null
rm -f "${HOME:?}"/intact/logs/run/multiue/*.pid 2>/dev/null
pkill -INT -x xapp_kpm_csv 2>/dev/null; sleep 1; pkill -INT -x nearRT-RIC 2>/dev/null; sleep 1
pkill -KILL -x xapp_kpm_csv 2>/dev/null; pkill -KILL -x nearRT-RIC 2>/dev/null
sudo pkill -f srsran_reverse_proxy 2>/dev/null
sudo pkill -INT -x 'jrtc|jrtc_main' 2>/dev/null; sleep 2; sudo pkill -KILL -x 'jrtc|jrtc_main' 2>/dev/null   # jrtc renames itself jrtc_main
pkill -f 'iperf3 -s' 2>/dev/null
tmux kill-session -t intact 2>/dev/null
if pgrep -ax 'gnb|srsue|jrtc|jrtc_main|nearRT-RIC|xapp_kpm_csv' || pgrep -af '[i]ntact-zmq-broker --ues'; then
  echo "WARNING: still running (above)"
else
  echo "RAN stack stopped (UEs, broker, gNB, proxy, jrt-controller, RIC, xApp); core still running"
fi
DOWNEOF

cat > ~/intact/bin/intact-health <<'EOF'
#!/usr/bin/env bash
# intact-health: one screen with the state of every layer.
source ~/intact/env.sh
R="$LOGS/run"
ok(){ echo "  [ OK ] $*"; }; no(){ echo "  [ -- ] $*"; }
echo "== Core"; intact-core-health 2>/dev/null | tail -1 | sed 's/^/  /'
echo "== Processes"
for p in 'jrtc|jrtc_main' gnb srsue; do pgrep -x "$p" >/dev/null && ok "${p%%|*} running" || no "${p%%|*} not running"; done
pgrep -f srsran_reverse_proxy >/dev/null && ok "reverse proxy running" || no "reverse proxy not running"
for pp in 3001:jrtc-REST 30450:proxy; do ss -Hltn "sport = :${pp%%:*}" | grep -q . && ok "${pp#*:} listening :${pp%%:*}" || no "${pp#*:} not listening :${pp%%:*}"; done
echo "== gNB"
grep -q 'gNB started' "$R/gnb.out" 2>/dev/null && ok "gNB started, N2 up" || no "gNB not started"
grep -q 'Registration succeeded' "$R/gnb.out" 2>/dev/null && ok "jbpf agent registered" || no "jbpf agent not registered"
grep -q 'Codeletset is loaded OK' "$R/gnb.out" 2>/dev/null && ok "codelet set loaded" || no "no codelet loaded"
echo "== UE"
ip4=$(sudo ip netns exec ue1 ip -4 -br addr show tun_srsue 2>/dev/null | awk '{print $3}')
[ -n "$ip4" ] && ok "UE IP $ip4" || no "UE has no IP"
[ -n "$ip4" ] && { sudo ip netns exec ue1 ping -c 3 -W 2 10.45.0.1 >/dev/null 2>&1 && ok "ping UE -> 10.45.0.1 works" || no "ping UE -> 10.45.0.1 fails"; }
echo "== System"
df -h /dev/shm | awk 'NR==2{print "  /dev/shm used "$5" of "$2}'
sudo dmesg -T 2>/dev/null | grep -qi 'invalid opcode' && echo "  [WARN] invalid-opcode trap in kernel log" || ok "no SIGILL traps"
EOF
chmod +x ~/intact/bin/intact-down ~/intact/bin/intact-health
```

### 8.5 Manual layout (for debugging, same commands)

If you prefer separate Windows Terminal tabs, open one per row, run `source ~/intact/env.sh` in each, and start the next only after the previous shows its ready line.

| Terminal | Command | Ready when you see |
| --- | --- | --- |
| T1 | *(none; the core runs under systemd)* `intact-core-health` | `CORE READY` |
| T2 | `intact-run-jrtc` | `Starting the jrtc_north_io app` (and `ss -ltn` shows :3001) |
| T3 | `intact-run-gnb` | `N2: Connection to AMF on 127.0.0.5:38412 completed`, `==== gNB started ===` |
| T4 | `intact-run-proxy` | no error, and `ss -ltn` shows :30450 |
| T5 | `intact-run-ue` | `PDU Session Establishment successful` (exact wording can vary) and an IP |
| T6 | traffic and codelet loading (Parts 9–10) | — |

Stop with `Ctrl-C` in T5 **and** T3 together (UE first, then gNB, within a few seconds), then T4, then T2. Or simply run `intact-down` from any terminal.

```bash
intact-cp mark part8-ops-scripts
```

## Part 9 — First end-to-end attach and traffic

The attach is done in two stages so that a failure points at one layer. **Stage A** runs gNB, UE and core with no jbpf at all. **Stage B** adds jrt-controller and the jbpf section. If Stage A attaches and Stage B does not, the problem is in jbpf; if Stage A fails, jbpf is not involved.

### 9.1 Stage A: attach without jbpf — \[SCRIPT\]

```bash
source ~/intact/env.sh
intact-up --no-jbpf
```

**Expected output of `intact-up`** (timings vary):

```
=== 0. Pre-checks
  core ready
  configs consistent
=== 2. gNB
  ready: gNB (N2 to AMF) (8s)
  N2 up: N2: Connection to AMF on 127.0.0.5:38412
=== 4. UE
  ready: UE attached, IP 10.45.0.2/24, default route via tun_srsue (12s)

STACK UP.
```

What the windows show (`tmux attach -t intact`, then `Ctrl-b n` to step through):

| Window | Lines that must appear, in this order |
| --- | --- |
| `gnb` | `Available radio types: zmq.` → `Cell pci=1, bw=10 MHz, ... dl_arfcn=368500 (n3)` → `N2: Connection to AMF on 127.0.0.5:38412 completed` → `==== gNB started ===` → a metrics table with a row for the UE's RNTI and CQI 15 |
| `ue` | `Attaching UE...` → `Random Access Complete. c-rnti=0x46..` → `RRC Connected` → `PDU Session Establishment successful. IP: 10.45.0.2` → `RRC NR reconfiguration successful.` (wording can differ slightly by version; the IP is what counts) |

Confirm from the core's side:

```bash
sudo grep -E 'Registration complete|InitialUEMessage|Registration reject' /var/log/open5gs/amf.log | tail -5
sudo grep -E 'UE SUPI|IPv4' /var/log/open5gs/smf.log | tail -3
```

**Expected:** an AMF `Registration complete` for `imsi-001010123456780` (no `Registration reject`), and an SMF line with `DNN[internet]` and `IPv4[10.45.0.2]`.

**If `intact-up` stops:** it names the step and prints the relevant lines. Go to Part 11, rows for that component. The three most likely here: *gNB stops on a config key* (the error names the key), *UE stuck after `Attaching UE...`* (ZMQ), *`Received RRC Release`* (registration rejected; the AMF log says why).

**If Stage A fails only because the jbpf gNB refuses to start without a jbpf section**, skip to Stage B; the jbpf build may require it.

### 9.2 Ping and throughput baseline — \[WSL\], terminal T6

The iperf3 server listens on the UPF's tunnel address on the host side; the client runs inside the UE's namespace, so every byte crosses UE → ZMQ radio → gNB → GTP-U → UPF.

```bash
source ~/intact/env.sh
sudo ip netns exec ue1 ping -c 5 10.45.0.1
iperf3 -s -B 10.45.0.1 -D --logfile "$LOGS/run/iperf3-server.log"
sleep 1
sudo ip netns exec ue1 iperf3 -c 10.45.0.1 -u -b 30M -R -t 20 -i 1 | tee "$LOGS/baseline-dl.txt"
sudo ip netns exec ue1 iperf3 -c 10.45.0.1 -u -b 5M -t 10 -i 1 | tee "$LOGS/baseline-ul.txt"
```

**Expected:** 5 ping replies (20–80 ms is normal for ZMQ), then per-second iperf3 lines. Note the **receiver** bitrate at the end of the downlink test, for example `18.4 Mbits/sec`. That number is your 100% reference for Part 10. Anything from about 8 to 30 Mbit/s is plausible for 10 MHz over ZMQ in WSL; what matters is that it is stable from second to second.

**If not:**

| Symptom | Fix |
| --- | --- |
| ping `Network is unreachable` | Missing route: `sudo ip netns exec ue1 ip route replace default dev tun_srsue` |
| ping 100% loss, UE has an IP | `ip -br addr show ogstun` must show 10.45.0.1/16; `sysctl net.ipv4.ip_forward` must be 1; `sudo systemctl restart intact-ogstun open5gs-upfd` |
| iperf3 `Connection refused` | Server not running: `pgrep -a iperf3`; start it again (port 5201 conflict → add `-p 5301` to both sides) |
| Throughput swings wildly or collapses to zero | CPU contention: close heavy Windows apps, keep mains power; check `top` in WSL for anything else using CPU |

### 9.3 Stage B: the full stack with jbpf — \[SCRIPT\]

```bash
intact-down
intact-up
```

**Expected** in addition to Stage A's lines:

```
=== 1. jrt-controller
  ready: jrt-controller REST on :3001 (2s)
=== 2. gNB
  ready: gNB (N2 to AMF) (9s)
  jbpf agent registered with jrt-controller
=== 3. jbpf reverse proxy
  ready: reverse proxy on :30450 (1s)
```

In the `gnb` window, the jbpf block you already know from PC2: `Unable to reserve 1G huge pages` → `Trying to reserve regular memory` → `Allocated memory for jbpf` → `Registration succeeded: memory name is jbpf_io_...` → `Started LCM IPC server`. The huge-page warning is expected; WSL has none and jbpf falls back correctly. In the `jrtc` window: `Negotiation was successful` and `Heap was created successfully`.

Repeat the ping and the 20-second downlink test from 9.2; the result should match Stage A within about 10%.

```bash
intact-health
```

**Expected:** every line `[ OK ]` except `no codelet loaded`, which is next.

**If Stage B fails but A worked:** jrt-controller window shows an error → Part 11 jbpf rows. gNB exits right after the jbpf lines → `/dev/shm` full (`df -h /dev/shm`) or jrt-controller not running when the gNB started.

### 9.4 Checkpoint CP3

```bash
intact-cp save first-attach && intact-cp mark part9-attach
intact-down
```

**\[PowerShell\]** Snapshot procedure from 1.7 with the name `intact-CP3`. This is the snapshot to return to if anything in Part 10 goes badly.

## Part 9b — Telemetry baseline: everything collectable without jbpf

Two scripts. **intact-telemetry-setup** \[SCRIPT, once, stack stopped\] switches every non-jbpf source on: it sets `cu_cp.inactivity_timer: 7200` (the default 120 s released an idle UE with `Received RRC Release`), turns on the gNB's JSON metrics for every layer on the websocket `ws://127.0.0.1:8001`, and makes srsUE write `/tmp/ue_metrics.csv` every second. It validates each gNB config with `gnb --dryrun` and restores the backup if the gNB rejects it. **intact-telemetry** \[SCRIPT\] then samples all sources into one long-format CSV (`ts_utc, source, entity, metric, value, unit`) plus an `inventory.txt` that lists every metric found and, for comparison, the 65 jbpf hooks and the codelet sets that could add more.

### 9b.1 Install and run the setup

```bash
intact-down
cat > ~/intact/bin/intact-telemetry-setup <<'SETUPEOF'
#!/usr/bin/env bash
# intact-telemetry-setup: one-time changes so every telemetry source is switched on. Safe to rerun.
#   1. gNB: cu_cp.inactivity_timer 7200 s (default 120 s caused "Received RRC Release")
#   2. gNB: JSON metrics for every layer, served on ws://127.0.0.1:8001 (remote control)
#   3. srsUE: per-second metrics CSV in /tmp/ue_metrics.csv
# Every edited file is backed up; each gNB config is validated with 'gnb --dryrun',
# and restored automatically if the gNB rejects it.
set -euo pipefail
source ~/intact/env.sh
TS=$(date +%Y%m%d-%H%M%S)
G="$SRSRAN_DIR/build/apps/gnb/gnb"
pgrep -x 'gnb|srsue' >/dev/null && { echo "STOP: gNB/UE running. Run intact-down first."; exit 1; }
sudo -v

echo "=== 1-2. gNB configs"
for f in "$CFG/gnb_zmq.yaml" "$CFG/gnb_zmq_jbpf.yaml"; do
  cp "$f" "$f.bak-tm-$TS"
  for mode in full nolayers; do
    python3 - "$f" "$mode" <<'PY'
import sys, re
f, mode = sys.argv[1], sys.argv[2]
t = open(f).read()
keep, skip = [], False                      # drop old top-level metrics/remote_control blocks
for line in t.splitlines(keepends=True):
    if re.match(r"(metrics|remote_control):\s*(#.*)?$", line): skip = True; continue
    if skip and line.strip() and not line[0].isspace(): skip = False
    if not skip: keep.append(line)
t = "".join(keep)
t = t.replace("  inactivity_timer: 7200   # default 120 s releases an idle UE\n", "")
if t.count("cu_cp:\n") != 1: sys.exit("STOP: no single 'cu_cp:' line in " + f)
t = t.replace("cu_cp:\n", "cu_cp:\n  inactivity_timer: 7200   # default 120 s releases an idle UE\n", 1)
layers = "" if mode == "nolayers" else (
 "  layers:\n    enable_sched: true\n    enable_mac: true\n    enable_rlc: true\n    enable_pdcp: true\n"
 "    enable_rrc: true\n    enable_ngap: true\n    enable_e1ap: true\n    enable_du_low: true\n")
block = ("\nmetrics:\n  autostart_stdout_metrics: true   # per-UE table on the console every second\n"
         "  enable_json: true                # every metric as JSON on the remote-control websocket\n" + layers +
         "\nremote_control:\n  enabled: true\n  bind_addr: 127.0.0.1\n  port: 8001\n")
m = re.search(r"\njbpf:\n", t)
t = (t[:m.start()] + "\n" + block.strip("\n") + "\n" + t[m.start():]) if m else (t.rstrip("\n") + "\n" + block)
t = re.sub(r"\n{3,}", "\n\n", t)
open(f, "w").write(t)
PY
    rc=0; out=$(cd "$(dirname "$G")" && sudo timeout 30 "$G" -c "$f" --dryrun 2>&1) || rc=$?
    if [ $rc -eq 0 ] && ! grep -qiE 'not expected|error|invalid|could not' <<<"$out"; then
      echo "  [ OK ] ${f##*/}: accepted by 'gnb --dryrun' ($mode)"; break
    fi
    echo "  [WARN] ${f##*/}: gnb rejected the $mode variant:"; sed 's/^/        /' <<<"$out" | tail -5
    if [ $mode = nolayers ]; then cp "$f.bak-tm-$TS" "$f"; echo "  [FAIL] ${f##*/}: restored original. Send me the lines above."; exit 1; fi
  done
done
python3 -c "import yaml,sys; [yaml.safe_load(open(f)) for f in sys.argv[1:]]; print('  [ OK ] YAML parses')" "$CFG/gnb_zmq.yaml" "$CFG/gnb_zmq_jbpf.yaml"
ss -Hltn 'sport = :8001' | grep -q . && echo "  [WARN] port 8001 already in use: the gNB metrics websocket will fail to listen" || echo "  [ OK ] port 8001 free"

echo "=== 3. srsUE metrics CSV"
f=~/intact/bin/intact-run-ue
if grep -q 'metrics_csv_enable' "$f"; then echo "  [ OK ] intact-run-ue: already writes /tmp/ue_metrics.csv"
else
  cp "$f" "$f.bak-tm-$TS"
  python3 - "$f" <<'PY'
import sys
f = sys.argv[1]; t = open(f).read()
old = '"${1:-$CFG/ue_zmq.conf}"'
if t.count(old) != 1: sys.exit("STOP: launch line not found in " + f)
new = old + (" --general.metrics_csv_enable=true --general.metrics_csv_filename=/tmp/ue_metrics.csv"
             " --general.metrics_period_secs=1 --general.metrics_csv_flush_period_sec=1")
open(f, "w").write(t.replace(old, new))
PY
  echo "  [DONE] intact-run-ue: now writes /tmp/ue_metrics.csv every second"
fi
echo
echo "SETUP DONE. Backups: *.bak-tm-$TS. Start the stack as usual; the gNB console now also prints"
echo "'Remote control server listening on 127.0.0.1:8001'. Then run: intact-telemetry 120"
SETUPEOF
chmod +x ~/intact/bin/intact-telemetry-setup
intact-telemetry-setup
```

**Expected:** `[ OK ] gnb_zmq.yaml: accepted by 'gnb --dryrun' (full)`, the same for `gnb_zmq_jbpf.yaml`, `YAML parses`, `port 8001 free`, `[DONE] intact-run-ue`, then `SETUP DONE`. A `(nolayers)` line instead of `(full)` is fine: the per-layer switches were refused, so the gNB still reports its default layers. `[FAIL] ... restored original` → nothing changed; send the lines above it.

### 9b.2 Install the collector, start the stack, record

```bash
cat > ~/intact/bin/intact_telemetry.py <<'PYEOF'
#!/usr/bin/env python3
"""intact-telemetry: sample every telemetry source the stack exposes WITHOUT jbpf into one CSV.

Output folder ~/intact/logs/telemetry/<UTC time>/:
  telemetry.csv     long format: ts_utc, source, entity, metric, value, unit
  gnb_metrics.jsonl raw JSON messages from the gNB metrics websocket (for reference)
  inventory.txt     what was found: metrics per source, plus what jbpf could add (hooks, codelets)

Sources (each is optional; a missing one is reported, never fatal):
  gnb_console  per-UE scheduler table the gNB prints every second (logs/run/gnb.out)
  gnb_json     every gNB layer's metrics, ws://127.0.0.1:8001 (needs intact-telemetry-setup)
  ue_csv       srsUE metrics, /tmp/ue_metrics.csv (needs intact-telemetry-setup)
  core_prom    Open5GS Prometheus counters/gauges (AMF, SMF, UPF, PCF, ...)
  net          interface counters: ogstun (core side) and tun_srsue (UE side) + derived bit rates
  proc         CPU % and memory of gnb, srsue, jrt-controller, proxy, Open5GS, MongoDB
  probe        one ping UE -> 10.45.0.1 per interval (RTT)
"""
import argparse, base64, csv, json, os, re, socket, struct, subprocess, sys, threading, time
import urllib.request
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
INTACT = os.path.join(HOME, "intact")
LOGS = os.environ.get("LOGS", os.path.join(INTACT, "logs"))
SI = {"k": 1e3, "M": 1e6, "G": 1e9, "n": 1e-9, "u": 1e-6}
STOP = threading.Event()


def utc(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).isoformat(timespec="milliseconds")


def num(s):
    """'29M' -> 29e6, '6.05M' -> 6.05e6, '0%' -> 0, '520n' -> 5.2e-07, 'n/a' -> None."""
    s = str(s).strip()
    if s in ("", "n/a", "nan", "-", "inf", "-inf"):
        return None
    s = s.rstrip("%")
    try:
        return float(s)
    except ValueError:
        pass
    if s[-1:] in SI:
        try:
            return float(s[:-1]) * SI[s[-1]]
        except ValueError:
            return None
    return None


class Out:
    def __init__(self, folder):
        self.f = open(os.path.join(folder, "telemetry.csv"), "w", newline="")
        self.w = csv.writer(self.f)
        self.w.writerow(["ts_utc", "source", "entity", "metric", "value", "unit"])
        self.lock = threading.Lock()
        self.seen = {}      # source -> {metric: (example value, unit)}
        self.rows = {}
        self.notes = {}     # source -> status text

    def row(self, source, entity, metric, value, unit="", ts=None):
        v = value if isinstance(value, (int, float)) else num(value)
        if v is None:
            return
        with self.lock:
            self.w.writerow([utc(ts), source, entity, metric, f"{v:.6g}", unit])
            self.seen.setdefault(source, {}).setdefault(metric, (f"{v:.6g}", unit))
            self.rows[source] = self.rows.get(source, 0) + 1

    def note(self, source, text):
        with self.lock:
            self.notes[source] = text

    def close(self):
        with self.lock:
            self.f.close()


# ---------------------------------------------------------------- gNB console table
DL_DEF = ["cqi", "ri", "mcs", "brate", "ok", "nok", "nok_pct", "bs"]
UL_DEF = ["pusch_snr", "rsrp", "ri", "mcs", "brate", "ok", "nok", "nok_pct", "bsr", "ta", "phr"]
UNITS = {"brate": "bit/s", "bs": "bytes", "bsr": "bytes", "ta": "s", "rsrp": "dBm", "pusch_snr": "dB",
         "nok_pct": "%", "phr": "dB"}


def names_from_header(part, default):
    toks = part.split()
    out = []
    for t in toks:
        t = {"(%)": "nok_pct", "dl_bs": "bs", "pusch": "pusch_snr"}.get(t, t)
        out.append(t)
    return out if len(out) == len(default) else default


def gnb_console(out, path):
    while not STOP.is_set() and not os.path.exists(path):
        out.note("gnb_console", f"waiting for {path}")
        time.sleep(1)
    if STOP.is_set():
        return
    dl, ul = DL_DEF, UL_DEF
    with open(path, errors="ignore") as f:
        f.seek(0, 2)
        out.note("gnb_console", "reading new lines of " + path)
        while not STOP.is_set():
            line = f.readline()
            if not line:
                time.sleep(0.2)
                continue
            if "pci" in line and "rnti" in line and "|" in line:
                parts = line.split("|")
                if len(parts) >= 3:
                    dl, ul = names_from_header(parts[1], DL_DEF), names_from_header(parts[2], UL_DEF)
                continue
            m = re.match(r"^\s*(\d+)\s+([0-9a-fA-F]+)\s*\|(.*)\|(.*)$", line)
            if not m:
                continue
            ent = f"pci={m.group(1)},rnti={m.group(2)}"
            for pre, names, vals in (("dl_", dl, m.group(3).split()), ("ul_", ul, m.group(4).split())):
                for n, v in zip(names, vals):
                    out.row("gnb_console", ent, pre + n, v, UNITS.get(n, ""))


# ---------------------------------------------------------------- gNB JSON metrics (websocket)
IDS = ("pci", "rnti", "ue", "ue_index", "cell_id", "du_id", "cu_cp_id", "cu_up_id", "lcid", "drb_id", "slice", "nci")


def flatten(out, obj, path, ent, ts):
    if isinstance(obj, dict):
        ids = [f"{k}={obj[k]}" for k in IDS if k in obj and not isinstance(obj[k], (dict, list))]
        e2 = ",".join([x for x in [ent] + ids if x])
        for k, v in obj.items():
            if k == "timestamp" or k in IDS:
                continue
            flatten(out, v, f"{path}.{k}" if path else k, e2, ts)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            has_id = isinstance(item, dict) and (any(k in item for k in IDS) or any(
                isinstance(v, dict) and any(k in v for k in IDS) for v in item.values()))
            flatten(out, item, path if has_id else f"{path}[{i}]", ent, ts)
    elif isinstance(obj, bool):
        out.row("gnb_json", ent or "gnb", path, 1.0 if obj else 0.0, "", ts)
    elif isinstance(obj, (int, float)):
        out.row("gnb_json", ent or "gnb", path, obj, "", ts)


class WS:
    def __init__(self, host, port):
        self.s = socket.create_connection((host, port), timeout=3)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = self.s.recv(4096)
            if not c:
                raise ConnectionError("closed during handshake")
            buf += c
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        if b" 101" not in head.split(b"\r\n")[0]:
            raise ConnectionError("no websocket upgrade: " + head.split(b"\r\n")[0].decode(errors="ignore"))
        self.s.settimeout(1.0)

    def send(self, data, opcode=1):
        data = data.encode() if isinstance(data, str) else data
        n, mask = len(data), os.urandom(4)
        hdr = bytes([0x80 | opcode])
        hdr += bytes([0x80 | n]) if n < 126 else (bytes([0x80 | 126]) + struct.pack("!H", n) if n < 65536
                                                  else bytes([0x80 | 127]) + struct.pack("!Q", n))
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _exact(self, n):
        while len(self.buf) < n:
            if STOP.is_set():
                raise ConnectionAbortedError
            try:
                c = self.s.recv(65536)
            except socket.timeout:
                continue
            if not c:
                raise ConnectionError("closed")
            self.buf += c
        d, self.buf = self.buf[:n], self.buf[n:]
        return d

    def messages(self):
        parts = []
        while True:
            b1, b2 = self._exact(2)
            fin, op, masked, n = b1 & 0x80, b1 & 0x0F, b2 & 0x80, b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._exact(8))[0]
            mk = self._exact(4) if masked else None
            p = self._exact(n)
            if mk:
                p = bytes(b ^ mk[i % 4] for i, b in enumerate(p))
            if op == 9:
                self.send(p, 10)
                continue
            if op == 8:
                raise ConnectionError("server closed")
            if op in (1, 2, 0):
                parts.append(p)
                if fin:
                    yield b"".join(parts).decode(errors="ignore")
                    parts = []


def gnb_json(out, folder, host, port):
    raw = open(os.path.join(folder, "gnb_metrics.jsonl"), "w")
    got = 0
    while not STOP.is_set():
        try:
            ws = WS(host, port)
            ws.send(json.dumps({"cmd": "metrics_subscribe"}))
            out.note("gnb_json", f"subscribed to ws://{host}:{port}")
            for msg in ws.messages():
                ts = time.time()
                raw.write(msg + "\n")
                try:
                    obj = json.loads(msg)
                except ValueError:
                    continue
                if isinstance(obj, dict) and set(obj) <= {"cmd", "timestamp", "error"}:
                    if "error" in obj:
                        out.note("gnb_json", "gNB answered: " + str(obj.get("error")))
                    continue
                got += 1
                flatten(out, obj, "", "", ts)
        except ConnectionAbortedError:
            break
        except Exception as e:  # gNB not up yet, restarted, or JSON metrics not enabled
            out.note("gnb_json", f"not available at ws://{host}:{port} ({e}); run intact-telemetry-setup, restart gNB"
                     if got == 0 else f"reconnecting after: {e}")
            STOP.wait(2)
    raw.close()


# ---------------------------------------------------------------- srsUE metrics CSV
UE_UNITS = {"rsrp": "dBm", "pl": "dB", "cfo": "Hz", "dl_snr": "dB", "dl_brate": "bit/s", "ul_brate": "bit/s",
            "dl_bler": "%", "ul_bler": "%", "ul_ta": "us", "ul_buff": "bytes", "proc_rmem_kB": "kB",
            "proc_vmem_kB": "kB", "sys_load": "%"}


def ue_csv(out, path):
    while not STOP.is_set() and not os.path.exists(path):
        out.note("ue_csv", f"waiting for {path} (run intact-telemetry-setup, then restart the UE)")
        time.sleep(1)
    if STOP.is_set():
        return
    with open(path, errors="ignore") as f:
        header = None
        out.note("ue_csv", "reading " + path)
        while not STOP.is_set():
            line = f.readline()
            if not line:
                time.sleep(0.3)
                continue
            fields = line.strip().split(";")
            if fields and fields[0] == "time":
                header = fields
                continue
            if not header or len(fields) < 4:
                continue
            rec = dict(zip(header, fields))
            t = num(rec.get("time"))
            ts = t / 1000.0 if t and t > 1e12 else None
            ent = f"cc={rec.get('cc', '0')},pci={rec.get('pci', '?')}"
            for k, v in rec.items():
                if k in ("time", "cc", "pci", "earfcn", "pci_neigh"):
                    continue
                out.row("ue_csv", ent, k, v, UE_UNITS.get(k, ""), ts)


# ---------------------------------------------------------------- Open5GS Prometheus
def prom_targets():
    targets = {}
    try:
        import yaml
        for fn in sorted(os.listdir("/etc/open5gs")):
            if not fn.endswith(".yaml"):
                continue
            try:
                d = yaml.safe_load(open(os.path.join("/etc/open5gs", fn))) or {}
            except Exception:
                continue
            for nf, body in d.items():
                if isinstance(body, dict) and isinstance(body.get("metrics"), dict):
                    for srv in body["metrics"].get("server", []) or []:
                        if isinstance(srv, dict) and srv.get("address"):
                            targets[nf] = (srv["address"], int(srv.get("port", 9090)))
    except Exception:
        pass
    return targets or {"amf": ("127.0.0.5", 9090), "smf": ("127.0.0.4", 9090),
                       "upf": ("127.0.0.7", 9090), "pcf": ("127.0.0.13", 9090)}


def core_prom(out, targets, helps):
    for nf, (addr, port) in targets.items():
        try:
            txt = urllib.request.urlopen(f"http://{addr}:{port}/metrics", timeout=1).read().decode(errors="ignore")
        except Exception as e:
            out.note("core_prom", f"{nf} {addr}:{port} unreachable ({e})")
            continue
        for line in txt.splitlines():
            if line.startswith("# HELP "):
                p = line[7:].split(" ", 1)
                helps.setdefault(p[0], p[1] if len(p) > 1 else "")
                continue
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(\S+)", line)
            if m:
                out.row("core_prom", nf + (m.group(2) or ""), m.group(1), m.group(3))


# ---------------------------------------------------------------- interfaces, processes, probe
NET_KEYS = ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets", "rx_dropped", "tx_dropped", "rx_errors", "tx_errors")


def net(out, last, interval):
    vals = {}
    for k in NET_KEYS:
        try:
            vals[("ogstun", k)] = int(open(f"/sys/class/net/ogstun/statistics/{k}").read())
        except OSError:
            pass
    try:
        j = json.loads(subprocess.run(["sudo", "-n", "ip", "-n", "ue1", "-s", "-j", "link", "show", "tun_srsue"],
                                      capture_output=True, text=True, timeout=2).stdout or "[]")
        st = j[0].get("stats64", {}) if j else {}
        for d in ("rx", "tx"):
            for k in ("bytes", "packets", "dropped", "errors"):
                if k in st.get(d, {}):
                    vals[("ue1/tun_srsue", f"{d}_{k}")] = int(st[d][k])
    except Exception:
        pass
    for (ifc, k), v in vals.items():
        out.row("net", ifc, k, v, "bytes" if "bytes" in k else "count")
        if k.endswith("_bytes") and (ifc, k) in last:
            out.row("net", ifc, k.replace("_bytes", "_bps"), max(0, v - last[(ifc, k)]) * 8 / interval, "bit/s")
    last.clear()
    last.update(vals)


PROC_RE = re.compile(r"^(gnb|srsue|jrtc|jrtc_main|srsran_reverse_|open5gs-\w+|mongod|iperf3)$")
CLK = os.sysconf("SC_CLK_TCK")


def proc(out, last, interval):
    cur = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            comm = open(f"/proc/{pid}/comm").read().strip()
            if not PROC_RE.match(comm):
                continue
            st = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
            ticks = int(st[11]) + int(st[12])
            rss = threads = 0
            for l in open(f"/proc/{pid}/status"):
                if l.startswith("VmRSS:"):
                    rss = int(l.split()[1])
                elif l.startswith("Threads:"):
                    threads = int(l.split()[1])
        except (OSError, IndexError, ValueError):
            continue
        ent = f"{comm}[{pid}]"
        cur[pid] = ticks
        if pid in last:
            out.row("proc", ent, "cpu_pct", (ticks - last[pid]) / CLK / interval * 100.0, "%")
        out.row("proc", ent, "rss_kB", rss, "kB")
        out.row("proc", ent, "threads", threads, "count")
    last.clear()
    last.update(cur)


def probe(out):
    try:
        r = subprocess.run(["sudo", "-n", "ip", "netns", "exec", "ue1", "ping", "-c", "1", "-W", "1", "10.45.0.1"],
                           capture_output=True, text=True, timeout=3)
        m = re.search(r"time=([\d.]+) ms", r.stdout)
        out.row("probe", "ue1->10.45.0.1", "ping_rtt", float(m.group(1)) if m else -1, "ms")
    except Exception:
        out.row("probe", "ue1->10.45.0.1", "ping_rtt", -1, "ms")


# ---------------------------------------------------------------- inventory of what jbpf could add
def jbpf_inventory():
    lines = []
    src = os.environ.get("SRSRAN_DIR", "")
    hooks = set()
    for sub in ("include", "lib"):
        for root, _, files in os.walk(os.path.join(src, sub)):
            for fn in files:
                if fn.endswith((".h", ".hpp", ".cpp", ".c")):
                    try:
                        hooks.update(re.findall(r"(?:DECLARE|DEFINE)_JBPF_HOOK\s*\(\s*(\w+)",
                                                open(os.path.join(root, fn), errors="ignore").read()))
                    except OSError:
                        pass
    lines.append(f"jbpf hooks compiled into this gNB ({len(hooks)}): codelets can attach to any of these")
    lines += ["  " + h for h in sorted(hooks)] or ["  (none found under $SRSRAN_DIR/include, lib)"]
    cdir = os.environ.get("JBPF_CODELETS", "")
    lines.append(f"\njbpf codelet sets shipped in {cdir}:")
    if os.path.isdir(cdir):
        for d in sorted(os.listdir(cdir)):
            p = os.path.join(cdir, d)
            if os.path.isdir(p):
                protos = [f for f in os.listdir(p) if f.endswith(".proto")]
                msgs = []
                for pf in protos:
                    msgs += re.findall(r"^\s*message\s+(\w+)", open(os.path.join(p, pf), errors="ignore").read(), re.M)
                lines.append(f"  {d:28s} messages: {', '.join(msgs) if msgs else '-'}")
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("duration", nargs="?", type=float, default=60, help="seconds to record (default 60)")
    ap.add_argument("interval", nargs="?", type=float, default=1, help="sampling interval in s (default 1)")
    ap.add_argument("--no-ping", action="store_true", help="do not send one ping per interval")
    ap.add_argument("--ws", default="127.0.0.1:8001", help="gNB remote-control websocket")
    ap.add_argument("--gnb-out", default=os.path.join(LOGS, "run", "gnb.out"))
    ap.add_argument("--ue-csv", default="/tmp/ue_metrics.csv")
    a = ap.parse_args()

    folder = os.path.join(LOGS, "telemetry", datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    os.makedirs(folder, exist_ok=True)
    out = Out(folder)
    host, port = a.ws.split(":")
    threads = [threading.Thread(target=gnb_console, args=(out, a.gnb_out), daemon=True),
               threading.Thread(target=gnb_json, args=(out, folder, host, int(port)), daemon=True),
               threading.Thread(target=ue_csv, args=(out, a.ue_csv), daemon=True)]
    for t in threads:
        t.start()
    targets, helps = prom_targets(), {}
    out.note("core_prom", "targets: " + ", ".join(f"{k}={v[0]}:{v[1]}" for k, v in targets.items()))
    last_net, last_proc = {}, {}
    t0 = time.time()
    print(f"Recording {a.duration:.0f} s every {a.interval:g} s into {folder}  (Ctrl-C stops early)")
    try:
        k = 0
        while time.time() - t0 < a.duration:
            tick = time.time()
            core_prom(out, targets, helps)
            net(out, last_net, a.interval)
            proc(out, last_proc, a.interval)
            if not a.no_ping:
                probe(out)
            k += 1
            if k % 10 == 0:
                print(f"  t={time.time() - t0:5.0f}s  rows: " +
                      "  ".join(f"{s}={n}" for s, n in sorted(out.rows.items())), flush=True)
            STOP.wait(max(0.0, a.interval - (time.time() - tick)))
    except KeyboardInterrupt:
        pass
    STOP.set()
    for t in threads:
        t.join(timeout=3)
    out.close()

    inv = [f"INTACT telemetry inventory, {utc()}, {a.duration:.0f} s at {a.interval:g} s", ""]
    allsrc = ["gnb_console", "gnb_json", "ue_csv", "core_prom", "net", "proc", "probe"]
    inv.append(f"{'source':12s} {'rows':>8s} {'metrics':>8s}  status")
    for s in allsrc:
        inv.append(f"{s:12s} {out.rows.get(s, 0):8d} {len(out.seen.get(s, {})):8d}  {out.notes.get(s, '')}")
    for s in allsrc:
        if out.seen.get(s):
            inv.append(f"\n== {s}: metric, example value, unit" + ("  [description]" if s == "core_prom" else ""))
            for m_, (v, u) in sorted(out.seen[s].items()):
                inv.append(f"  {m_:60s} {v:>14s} {u}" + (f"  [{helps.get(m_, '')}]" if s == "core_prom" else ""))
    inv.append("\n== WITH jbpf (not collected by this script; Part 10 onward)")
    inv += jbpf_inventory()
    open(os.path.join(folder, "inventory.txt"), "w").write("\n".join(inv) + "\n")
    print("\n".join(inv[:2 + 1 + len(allsrc)]))
    print(f"\nFiles: {folder}/telemetry.csv, inventory.txt, gnb_metrics.jsonl")


if __name__ == "__main__":
    main()
PYEOF
cat > ~/intact/bin/intact-telemetry <<'EOF'
#!/usr/bin/env bash
# intact-telemetry [seconds=60] [interval=1] [--no-ping]: record every non-jbpf telemetry source into one CSV.
source ~/intact/env.sh
sudo -v || exit 1
exec python3 ~/intact/bin/intact_telemetry.py "$@"
EOF
chmod +x ~/intact/bin/intact_telemetry.py ~/intact/bin/intact-telemetry
```

Start the stack (Stage B as in 9.3). The gNB window now also prints `Remote control server listening on 127.0.0.1:8001`. Then, in a spare window:

```bash
source ~/intact/env.sh
sudo ip netns exec ue1 iperf3 -c 10.45.0.1 -u -b 10M -R -t 100 >/dev/null &   # some load to see numbers move
intact-telemetry 120 1
```

**Expected:** a progress line every 10 s, then a table with rows for `gnb_console`, `gnb_json`, `ue_csv`, `core_prom`, `net`, `proc`, `probe`, and the folder `~/intact/logs/telemetry/<time>/` with `telemetry.csv`, `inventory.txt`, `gnb_metrics.jsonl`. A source with 0 rows names its reason in the status column (for example `waiting for /tmp/ue_metrics.csv`: the UE was started before the setup; restart it).

### 9b.3 Telemetry map: who sends what, to whom, over which protocol

Every telemetry stream on this testbed, with its exact producer, transport and consumer. All endpoints sit on the same WSL machine: either loopback 127.0.0.x, or the UE namespaces (`ue1`, and `ue2`…`ue5` with Appendix E). Rows 15–17 exist only with Appendices E and F.

There are three things to keep in mind when reading the table:

1. **No telemetry crosses SCTP, F1 or E1 here, and E2 carries telemetry only when Appendix F is set up (rows 15 and 16).** The gNB is monolithic, so CU-CP, CU-UP and DU exchange F1 and E1 messages as function calls inside one process (`gnb`), not on a wire. The other protocols on the wire carry signalling or user data, not telemetry:
   - NGAP over SCTP (gNB 127.0.0.1 → AMF 127.0.0.5:38412);
   - PFCP over UDP 8805 (SMF ↔ UPF);
   - the core's HTTP/2 SBI (via the SCP at 127.0.0.200);
   - GTP-U over UDP 2152 (UPF 127.0.0.7 ↔ gNB CU-UP);
   - ZMQ over TCP 127.0.0.1:2000/2001, which carries IQ samples and replaces the radio (with Appendix E also 2100–2501 through the broker).
2. **Exact field names.** The `inventory.txt` written by `intact-telemetry` (9b.2) lists every field your build actually emitted. The fields below for the CU-CP, CU-UP, RLC and PHY JSON reports are the main ones, not the complete list.
3. **Clocks.** WSL steps its wall clock (time of day) back by about 2 s roughly every 30 s; the monotonic clock is never stepped (B.4). A column marked "wall" therefore cannot be compared to sub-second precision with a column marked "monotonic".

| # | Level | Telemetry (units) | Produced by (exact source) | Transport / protocol: source → destination | Received by → stored as | Rate · time stamp | What it describes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | UE: radio and traffic | RSRP (dBm), path loss (dB), CFO (Hz), DL SNR (dB), DL/UL MCS (index), DL/UL bit rate (bit/s), DL/UL BLER (%), UL buffer (bytes), timing advance, attach flag | srsUE process `srsue` (root namespace; only its TUN `tun_srsue` is in `ue1`), metrics module | **local file write**, no network: `srsue` → `/tmp/ue_metrics.csv` (`;`-separated; options `--general.metrics_csv_*`, Part 9b). Several UEs (Appendix E): UE1 keeps `/tmp/ue_metrics.csv`, UE k writes `/tmp/ue<k>_metrics.csv` | `intact-telemetry` reads the file → `telemetry.csv` | 1 s · UE wall clock (ms) | link quality, traffic and errors as seen by the UE |
| 2 | UE: scheduling at the gNB | per UE: CQI, RI, DL/UL MCS (index), DL/UL MAC bit rate (bit/s, HARQ-acknowledged), DL HARQ OK/NOK (count), **DL buffer `dl_bs` (bytes)**, UL BSR (bytes), PUSCH/PUCCH SNR (dB), timing advance (ns), PHR (dB) | `gnb` process: DU-high MAC scheduler metrics | **WebSocket (RFC 6455) over TCP**, loopback: the gNB's remote-control server listens on **127.0.0.1:8001**; the client connects, sends `{"cmd":"metrics_subscribe"}`, and the gNB then **pushes** JSON text frames (`{"timestamp":…,"cells":[{"cell_metrics":…,"ue_list":[…]}]}`) | `intact-telemetry` → `gnb_metrics.jsonl` + `telemetry.csv`; `intact-kpm-log` (Gate A) → `gate-a-kpm.jsonl`; `pact-ran` (Appendix D) → `gnb_kpm.jsonl` | every `du_report_period` (500 ms after B.0; about 0.56 s measured) · gNB `timestamp` (wall) + arrival time (wall and monotonic) | per-UE radio performance and queued data, measured at the gNB: the KPM PACT uses |
| 2b | (same, as text) | the same per-UE table | `gnb` stdout (`autostart_stdout_metrics`) | **process pipe**: stdout → `tee` (`intact-run-gnb`) | `$LOGS/run/gnb.out` (text) | every report · none per line | human-readable view of row 2 |
| 3 | Cell: scheduler resources | decision latency avg/max (µs) and histogram, PDCCH/UCI allocation failures, late DL/UL HARQs, error indications (counts), Msg3 OK/NOK, PRACH delay (slots), PUCCH RB usage (avg RBs) | `gnb`: same scheduler report, `cell_metrics` object | as row 2 (same frames, WebSocket 127.0.0.1:8001) | as row 2 | as row 2 | control-channel use and scheduling problems |
| 4 | DU: RLC | per bearer: TX/RX SDUs and bytes, dropped SDUs, AM retransmissions (counts) | `gnb`: DU RLC metrics (`layers.enable_rlc`) | WebSocket 127.0.0.1:8001, JSON (separate frames) | `intact-telemetry` → `telemetry.csv` | `du_report_period` · gNB timestamp | link-layer queueing and loss per bearer |
| 5 | CU-CP: connections and sessions | RRC connected UEs, RRC setup attempts/successes, NGAP procedures (e.g. PDU-session resource setup), handovers (counts) | `gnb`: CU-CP metrics (`layers.enable_rrc`, `enable_ngap`) | WebSocket 127.0.0.1:8001, JSON | `intact-telemetry` | `cu_cp_report_period` (default 1000 ms) · gNB timestamp | connection, session and mobility activity |
| 6 | CU-UP: user-plane processing | PDCP DL/UL PDUs and bytes (→ Mbit/s), dropped PDUs, processing latency (µs), CPU usage (%) | `gnb`: CU-UP PDCP metrics (`layers.enable_pdcp`) | WebSocket 127.0.0.1:8001, JSON | `intact-telemetry` | `cu_up_report_period` (default 1000 ms) · gNB timestamp | PDCP processing performance |
| 7 | DU-low: PHY processing | PDSCH/PUSCH processing time, LDPC encoder/decoder latency (µs) and throughput, CPU | `gnb`: DU-low metrics (`layers.enable_du_low`) | WebSocket 127.0.0.1:8001, JSON | `intact-telemetry` | `du_report_period` · gNB timestamp | whether the software PHY keeps up |
| 8 | Core network | registered UEs (`ran_ue`), AMF sessions, connected gNBs, registration/authentication requests and successes (AMF); PDU-session creation requests/successes, PFCP peers (SMF); sessions, N3 GTP-U packets in/out (UPF); policy associations (PCF), all counts | Open5GS daemons' built-in Prometheus exporters: AMF **127.0.0.5:9090**, SMF **127.0.0.4:9090**, UPF **127.0.0.7:9090**, PCF **127.0.0.13:9090** | **HTTP/1.1 GET `/metrics` over TCP**, collector **pulls**; Prometheus text exposition format | `intact-telemetry` → `telemetry.csv` | each poll (1 s) · collector wall clock | core state and procedure outcomes |
| 9 | End-to-end throughput | offered/received rate (Mbit/s), bytes, datagrams, UDP loss (%), jitter (ms) | `iperf3` server in the root namespace, bound to 10.45.0.1 (`ogstun`), sends (`-R`); `iperf3` client in `ue1` (10.45.0.2, `tun_srsue`) receives and **measures** | control: **TCP 5201** client ↔ server; data: **UDP** 10.45.0.1 → 10.45.0.2 through the user plane: `ogstun` → `open5gs-upfd` → **GTP-U/UDP 2152** (127.0.0.7 → gNB CU-UP) → PDCP/RLC/MAC → **ZMQ/TCP 127.0.0.1:2000** (IQ samples) → `srsue` → `tun_srsue`. Several UEs (Appendix E): gNB TCP 2000 → **broker** → TCP 2k00 → `srsue` k → `tun_srsue` in netns `uek`; `intact-multiue iperf` uses one server port per UE, 5211…5215 | client writes JSON to stdout → `gate-a-dl.json` / `prb-dl.json` | 1 s intervals · start time wall (whole seconds), intervals on the monotonic clock | traffic delivered to the UE application, including several seconds of queueing under overload |
| 9b | End-to-end throughput (Appendix D) | sent and received rate (Mbit/s), loss, one-way delay (ms) | `pact_traffic.py send` (root namespace) → `pact_traffic.py sink` (in `ue1`) | **UDP 5300**, same user-plane path as row 9; rate steered over UDP 127.0.0.1:47010 | `traffic_tx.csv`, `traffic_rx_ue.csv` per run | 0.5 s · monotonic | the T1 load schedule as offered and as received |
| 10 | End-to-end delay | RTT (ms) | `ping` in `ue1`: ICMP Echo 10.45.0.2 → 10.45.0.1 and the reply back | **ICMP** over the user plane: UL via `tun_srsue` → `srsue` → **ZMQ/TCP 2001** → gNB → GTP-U → UPF → `ogstun`; DL the reverse (TCP 2000). Several UEs: UE k → TCP 2k01 → broker → TCP 2001 → gNB, DL via 2000 → broker → 2k00 | ping stdout → `gate-a-ping.txt` | every 0.2 s · `-D` wall clock | round-trip delay of the UE–core data path, dominated by the DL buffer when it is full |
| 11 | Slice control and readback | SST/SD; commanded and read-back min/max PRB (%) and priority; SFN/slot at which the gNB applied it; jbpf timestamp (ns); confirmation time (ms); match | command: `slice_mgmt` Python app inside `jrtc` (jrt-controller). Readback: the `slice_mgmt` eBPF codelet in `gnb` at hook `mac_sched_slice_mgmt` | **jbpf IO shared-memory channels** between `jrtc` and the jbpf agent in `gnb` (IPC memory `jrt_controller`, `/dev/shm/jbpf_io_*`; the agent registers over a Unix-domain socket under `/tmp/jbpf`). Messages are the nanopb C structs of `slice_mgmt.proto`: control input map `slice_request_map` (app → codelet), output map `slice_indication_map` (codelet → app). Loading (not telemetry): `jrtc-ctl` → HTTP REST 127.0.0.1:3001 (apps) and 127.0.0.1:30450 (reverse proxy) → Unix socket `/tmp/jbpf/jbpf_lcm_ipc` → agent | app → `$LOGS/run/jrtc.out` (stdout via `tee`) and `/tmp/gate_a.csv` | per SET (every 30 s in Gate A) · UTC wall, plus monotonic in harness v2; gNB jbpf ns clock | requested slice limits and proof that the MAC scheduler applied them |
| 11b | Slice control (Appendix D) | as row 11, plus ack and readback latency (ms) | `pact_e2t` app inside `jrtc` (same jbpf IO path to the codelet) | **UDP/JSON** on loopback: `pact-ran` → 127.0.0.1:**47000** (commands); `pact_e2t` → 127.0.0.1:**47001** (indications, acks, heartbeats) | `pact-ran` → `bridge.jsonl`, `telemetry.jsonl` | per epoch with a change · monotonic | PACT's writes and their readback |
| 12 | Available, not collected: jbpf telemetry codelets | MAC (BSR, CRC, HARQ, PHR, UCI stats), RLC and PDCP per bearer, FAPI DL/UL config per RNTI (PRBs, MCS, TBS histograms), RRC/NGAP events, UE contexts, jbpf performance | codelets at the corresponding gNB hooks (`codelets/` in jrtc-apps; none loaded besides `slice_mgmt`) | jbpf IO shared memory → `jrtc` → a jrtc app, or forwarded to the `jrtc-ctl` decoder (**gRPC**, port 30501) that prints JSON | not used so far | per codelet (stats reported about every 1 s) | finer scheduler detail, if ever needed |
| 13 | Control effect (derived) | before/after throughput (Mbit/s), lag (s), reports until visible, validity | `intact-gate-a-effect` (offline) from rows 2, 9, 10, 11 | **local file reads** | `gate-a-effect.csv` | per trial · one continuous clock (v2) | the throughput response to a cap change |
| 14 | Host and interfaces | CPU (%), memory (kB), threads (count), per-process statistics; interface bytes, packets, drops, errors | Linux kernel: procfs `/proc/stat`, `/proc/meminfo`, `/proc/<pid>/stat` and `status`, `/proc/net/dev` | **local file reads** (procfs), no network | `intact-telemetry` → `telemetry.csv` | 1 s · wall | computing load and network-interface activity |
| 15 | Cell over E2 (Appendix F) | E2SM-KPM style 1, cell level: `RRU.PrbUsedDl/Ul` (PRB), `RRU.PrbAvailDl/Ul` (PRB), `RRU.PrbTotDl/Ul` (%), `DRB.UEThpDl/Ul` (kbit/s), `DRB.RlcSduTransmittedVolumeDL/UL` (kbit), `DRB.RlcPacketDropRateDl` (%), `DRB.RlcSduDelayDl`, `DRB.AirIfDelayUl`, `DRB.RlcDelayUl` (0.1 ms), `RACH.PreambleDedCell` (count) | `gnb` process: the **DU's E2 agent**, E2SM-KPM service (RAN function ID 2), computed from the scheduler and RLC metrics (`metrics.layers.enable_sched`, `enable_rlc`) | **E2AP over SCTP** (ASN.1 aligned PER): gNB DU E2 agent (127.0.0.1, ephemeral port, client) → FlexRIC `nearRT-RIC` **127.0.0.1:36421**: E2 Setup, RIC Subscription, then a **RIC Indication** every report period. Then **E42 over SCTP**: `nearRT-RIC` **127.0.0.1:36422** → `xapp_kpm_csv` (the RIC relays the subscription and forwards each indication) | `xapp_kpm_csv` → `$LOGS/e2/kpm-<time>.csv` (+ FlexRIC's SQLite copy in `$LOGS/flexric/`) | one indication per subscription per period (1 s, srsRAN's minimum), values from the latest scheduler report · gNB `collectStartTime` (NTP, gNB wall clock) + arrival at the xApp (wall and monotonic) | cell load and throughput as an O-RAN near-RT RIC sees it |
| 16 | UE over E2 (Appendix F) | E2SM-KPM style 4 (indication format 3), one block per UE, UE ID = gNB-CU UE F1AP ID: the 14 UE-level metrics of row 15 (all but `RACH`), plus `CQI` (index); `RSRP`/`RSRQ` on request (in srsRAN 25.10 both are the PUSCH SNR, dB) | same E2 agent; the UE list comes from the RLC metrics | same path as row 15 | same CSV, `ue_id` = `du_f1ap:<n>` | as row 15 | per-UE KPIs for xApps (one row set per UE with Appendix E) |
| 17 | Emulation (Appendix E) | samples moved per second, downlink and uplink (Msps); speed = DL rate ÷ 11.52 Msps (× real time) | `intact-zmq-broker` (GNU Radio `probe_rate` blocks) | **process pipe**: stdout → file | `$LOGS/run/zmq-broker.out`, a line every 10 s; `intact-multiue status` | 10 s · monotonic | how fast the simulated radio runs; below 1.0 every rate measured in wall-clock time is lower by the same factor |

## Part 10 — Slices and live PRB-quota control through jbpf

The goal: while the UE streams downlink traffic, the `slice_mgmt` jrt-controller app changes its slice's `max_prb_policy_ratio` (30% ↔ 70%) through the jbpf codelet in the gNB scheduler, and the throughput steps up and down with each command. That is control with a physical effect, not only a readback.

The stock app needs at least **two** custom slices and swaps 30/70 between slice 0 and slice 1. The UE sits on slice 0, so its throughput alternates between roughly 30% and 70% of the Part 9 baseline. Slice SDs are **1 and 2**, which read the same in decimal (gNB, srsUE) and hex (Open5GS, `000001`), removing the 162-vs-`0000A2` trap.

### 10.1 Turn slices on — \[SCRIPT\]

The script saves the stage-1 files as `*.stage1.*`, then edits the gNB configs, the UE config, the AMF slice list, the NSSF slice table and the subscriber. Open5GS files are edited as text (YAML tools would turn `mcc: 001` into the number 1 and break the AMF).

```bash
cat > ~/intact/bin/intact-slices-on <<'EOF'
#!/usr/bin/env bash
# intact-slices-on: two slices (SST 1, SD 1 and 2); UE and subscriber on SD 1.
set -euo pipefail
source ~/intact/env.sh
for f in gnb_zmq.yaml gnb_zmq_jbpf.yaml; do [ -f "$CFG/${f%.yaml}.stage1.yaml" ] || cp "$CFG/$f" "$CFG/${f%.yaml}.stage1.yaml"; done
[ -f "$CFG/ue_zmq.stage1.conf" ] || cp "$CFG/ue_zmq.conf" "$CFG/ue_zmq.stage1.conf"

echo "== gNB: cell_cfg.slicing and tai_slice_support_list"
for f in gnb_zmq.yaml gnb_zmq_jbpf.yaml; do python3 - "$CFG/$f" <<'PY'
import sys, yaml
p = sys.argv[1]; d = yaml.safe_load(open(p))
sl = [{"sst": 1, "sd": 1}, {"sst": 1, "sd": 2}]
d["cell_cfg"]["slicing"] = sl
for ta in d["cu_cp"]["amf"]["supported_tracking_areas"]:
    for pl in ta["plmn_list"]:
        pl["tai_slice_support_list"] = [{"sst": 1}] + sl
yaml.safe_dump(d, open(p, "w"), sort_keys=False, default_flow_style=False)
print("  ", p, "slicing:", d["cell_cfg"]["slicing"])
PY
done

echo "== UE: request slice SST 1 / SD 1"
sed -i '/^\[slicing\]/,/^$/d' "$CFG/ue_zmq.conf"
printf '\n[slicing]\nenable = true\nnssai-sst = 1\nnssai-sd = 1\n' >> "$CFG/ue_zmq.conf"

echo "== Open5GS: AMF plmn_support and NSSF nsi"
sudo cp /etc/open5gs/amf.yaml /etc/open5gs/amf.yaml.pre-slices-$(date +%s)
sudo cp /etc/open5gs/nssf.yaml /etc/open5gs/nssf.yaml.pre-slices-$(date +%s)
sudo python3 - <<'PY'
import re
p = "/etc/open5gs/amf.yaml"; L = open(p).read().split("\n")
if not any(re.match(r"^\s*sd:\s*000001\b", x) for x in L):
    i = next(n for n, x in enumerate(L) if re.match(r"^\s*plmn_support:\s*$", x))
    j = next(n for n in range(i, len(L)) if re.match(r"^\s*s_nssai:\s*$", L[n]))
    k = next(n for n in range(j, len(L)) if re.match(r"^\s*- sst:\s*1\s*$", L[n]))
    ind = re.match(r"^(\s*)-", L[k]).group(1)
    L[k+1:k+1] = [f"{ind}- sst: 1", f"{ind}  sd: 000001", f"{ind}- sst: 1", f"{ind}  sd: 000002"]
    open(p, "w").write("\n".join(L)); print("   AMF: added SD 000001, 000002")
else: print("   AMF: already has SD 000001")
p = "/etc/open5gs/nssf.yaml"; L = open(p).read().split("\n")
try:
    if not any(re.match(r"^\s*sd:\s*000001\b", x) for x in L):
        i = next(n for n, x in enumerate(L) if re.match(r"^\s*nsi:\s*$", x))
        j = next(n for n in range(i, len(L)) if re.match(r"^\s*- uri:", L[n]))
        ind, uri = re.match(r"^(\s*)- uri:\s*(\S+)", L[j]).groups()
        k = next(n for n in range(j, len(L)) if re.match(r"^\s*sst:\s*1\s*$", L[n]))
        add = []
        for sd in ("000001", "000002"):
            add += [f"{ind}- uri: {uri}", f"{ind}  s_nssai:", f"{ind}    sst: 1", f"{ind}    sd: {sd}"]
        L[k+1:k+1] = add
        open(p, "w").write("\n".join(L)); print("   NSSF: added nsi entries for SD 000001, 000002")
    else: print("   NSSF: already has SD 000001")
except StopIteration:
    print("   NSSF: WARNING nsi block not in the expected shape; edit by hand (see 10.1 'If not')")
PY

echo "== Subscriber on slice SD 000001"
open5gs-dbctl remove 001010123456780 >/dev/null 2>&1 || true
open5gs-dbctl add_ue_with_slice 001010123456780 00112233445566778899aabbccddeeff 63BFA50EE6523365FF14C1F45F88737D internet 1 000001 >/dev/null
mongosh --quiet open5gs --eval 'JSON.stringify(db.subscribers.findOne({imsi:"001010123456780"},{_id:0,"slice.sst":1,"slice.sd":1}))'
git -C "$CFG" add -A && git -C "$CFG" commit -qm "stage 2: two slices" || true
echo "== Restarting core"
intact-core-restart && intact-cfg-check
EOF
chmod +x ~/intact/bin/intact-slices-on && intact-slices-on
```

**Expected:** the gNB files show `slicing: [{'sst': 1, 'sd': 1}, {'sst': 1, 'sd': 2}]`, the AMF and NSSF report their additions, the subscriber prints `{"slice":[{"sst":1,"sd":"000001"}]}`, `CORE READY`, and `intact-cfg-check` ends with `CONFIG CONSISTENT` including three new slice lines.

**If not:** `NSSF: WARNING` → open `/etc/open5gs/nssf.yaml`, find the `nsi:` list and add, at the same indentation as the existing `- uri:` entry, two more entries identical to it but with `sd: 000001` and `sd: 000002` under `s_nssai` next to `sst: 1`; then `intact-core-restart`. Any AMF failure after the edit → `sudo cp /etc/open5gs/amf.yaml.pre-slices-* /etc/open5gs/amf.yaml` (newest one) and `intact-core-restart`.

### 10.2 Attach on the slice — \[SCRIPT\]

```bash
intact-up
sudo grep -E 'S_NSSAI' /var/log/open5gs/smf.log | tail -2
sudo ip netns exec ue1 ping -c 3 10.45.0.1
```

**Expected:** `STACK UP`, an SMF line containing `S_NSSAI[SST:1 SD:0x1]`, and ping replies. Run the 20-second downlink test from 9.2 once more; it should match the baseline.

**If not:** UE now rejected with cause 62 or a slice-related cause in `amf.log` → the UE's requested slice is not allowed; run `intact``-cfg-c``heck` and fix the line it marks. Attach still works but SMF shows no SD → srsUE ignored `[slicing]`; check `ue.out` for a parse warning.

**If the UE prints `Received RRC Release` right after `RRC Connected`, and `amf.log` shows `Non cleartext IEs is included [0xc]` then `Registration reject [95]`:** this is a srsUE bug that slicing exposes, not a config error. With `[slicing] enable = true`, srsUE puts the Requested NSSAI into its first, unprotected Registration Request. TS 24.501 §4.4.6 allows only cleartext IEs there, and Open5GS 2.8 enforces that: mask `0xc` = UE security capability (`0x4`, allowed) + Requested NSSAI (`0x8`, not allowed). srsUE already sends the NSSAI again inside the protected Security Mode Complete, so the fix \[SCRIPT\] removes only the first copy and rebuilds srsue. It was tested on the same srsRAN\_4G commit (ec29b0c1) with GCC 13.

```bash
cat > ~/intact/bin/intact-fix-nssai <<'EOF'
#!/usr/bin/env bash
# intact-fix-nssai: srsUE puts Requested NSSAI in its first, unprotected Registration Request.
# Open5GS 2.8 rejects that: AMF "Non cleartext IEs is included [0xc]" -> Registration reject [95].
# Fix: leave it out of the first message. srsUE already sends it again inside the protected
# Security Mode Complete, which is where TS 24.501 4.4.6 wants it. Safe to rerun; rebuilds only srsue.
set -euo pipefail
source ~/intact/env.sh
pgrep -x 'gnb|srsue' >/dev/null && { echo "STOP: gNB/UE running. Run intact-down first."; exit 1; }
python3 - "$SRSUE_DIR" <<'PY'
import sys, shutil, time, pathlib
p = pathlib.Path(sys.argv[1]) / "srsue/src/stack/upper/nas_5g.cc"
t = p.read_text()
old = ("  if (cfg.enable_slicing) {\n"
       "    reg_req.requested_nssai_present = true;\n"
       "    s_nssai_t nssai;\n"
       "    set_nssai(nssai);\n"
       "    reg_req.requested_nssai.s_nssai_list.push_back(nssai);\n"
       "  }\n")
new = ("  // INTACT: Requested NSSAI is not a cleartext IE (TS 24.501 4.4.6); Open5GS >= 2.7 rejects it (cause 95)\n"
       "  // in the initial request. send_security_mode_complete() adds it inside the protected NAS container.\n"
       "  reg_req.requested_nssai_present = false;\n")
protected = "modified_registration_request.requested_nssai.s_nssai_list = {s_nssai};"
if protected not in t:
    sys.exit(">>> STOP: protected NSSAI block missing in nas_5g.cc; nothing changed")
if new in t:
    print(">>> nas_5g.cc: NSSAI fix already present")
elif t.count(old) == 1:
    shutil.copy2(p, f"{p}.bak-nssai-{time.strftime('%Y%m%d-%H%M%S')}")
    p.write_text(t.replace(old, new))
    print(">>> nas_5g.cc: NSSAI fix applied")
else:
    sys.exit(">>> STOP: nas_5g.cc not as expected; nothing changed")
PY
mkdir -p "$CFG/provenance"
git -C "$SRSUE_DIR" diff -- srsue/src/stack/upper/nas_5g.cc > "$CFG/provenance/srsue-nssai-cleartext.patch"
echo ">>> diff saved: $CFG/provenance/srsue-nssai-cleartext.patch"
if ! ( set -o pipefail; cmake --build "$SRSUE_DIR/build" --target srsue -j"$JOBS" 2>&1 | tee "$LOGS/build-ue-nssai.log" | tail -2 ); then
  echo ">>> BUILD FAILED, see $LOGS/build-ue-nssai.log"; exit 1
fi
B="$SRSUE_DIR/build/srsue/src/srsue"
[ "$B" -nt "$SRSUE_DIR/srsue/src/stack/upper/nas_5g.cc" ] && echo ">>> srsue rebuilt after the fix" || { echo ">>> STOP: srsue is older than the patched source"; exit 1; }
ldd "$B" | grep -q libzmq && echo ">>> srsue still links ZeroMQ" || { echo ">>> STOP: srsue lost libzmq"; exit 1; }
grep -A3 '^\[slicing\]' "$CFG/ue_zmq.conf" | sed 's/^/    ue_zmq.conf: /'
echo ">>> NSSAI FIX OK. Start jrtc -> gNB -> proxy -> UE as usual, then run intact-check-slice"
EOF

cat > ~/intact/bin/intact-check-slice <<'EOF'
#!/usr/bin/env bash
# intact-check-slice: did the UE register, and on which slice? Run after the UE prints its IP.
source ~/intact/env.sh
echo "== AMF, last registration attempt"
sudo grep -E 'Registration request|Non cleartext|Registration reject|Registration complete' /var/log/open5gs/amf.log | tail -4 | sed 's/^/  /'
echo "== SMF, last session (S_NSSAI must be SST:1 SD:0x1)"
sudo grep -E 'S_NSSAI' /var/log/open5gs/smf.log | tail -1 | sed 's/^/  /'
echo "== UE"
ip4=$(sudo ip netns exec ue1 ip -4 -br addr show tun_srsue 2>/dev/null | awk '{print $3}')
echo "  tun_srsue: ${ip4:-NO IP}"
[ -n "$ip4" ] && { sudo ip netns exec ue1 ip route replace default dev tun_srsue
  sudo ip netns exec ue1 ping -c 3 -W 2 10.45.0.1 | tail -2 | sed 's/^/  /'; }
EOF
chmod +x ~/intact/bin/intact-fix-nssai ~/intact/bin/intact-check-slice
intact-down; intact-fix-nssai
```

**Expected:** `NSSAI fix applied` (or `already present`), `srsue rebuilt after the fix`, `srsue still links ZeroMQ`, your `[slicing]` lines, then `NSSAI FIX OK`. Start the stack again (jrt-controller, gNB, proxy, then UE). When the UE prints `PDU Session Establishment successful`, run `intact-check-slice`. Pass = AMF `Registration complete` for `imsi-001010123456780`, SMF `S_NSSAI[SST:1 SD:0x1]`, and 3 ping replies. Older `reject [95]` lines stay in the log; only the last attempt counts.

### 10.3 Prepare the slice\_mgmt app for bare metal — \[WSL\]

Two edits, both on files inside jrtc-apps (tracked by its own git, so `git -C $JRTC_ROOT diff` shows them):

1. A bare-metal deployment file: the app talks to jrt-controller's REST port **3001** directly (30500 was the Kubernetes NodePort; that was the first load failure on PC2). The jbpf device port stays 30450, the reverse proxy.
2. Parameters: 30 s between swaps gives clean plateaus in the throughput trace, and `slice_update_sfn = None` applies each change on the next downlink slot instead of waiting for SFN 20 (on PC2 that wait added about 5 s and would distort any latency figure).

```bash
source ~/intact/env.sh
cd "$JRTC_APPS/slice_mgmt"
sed 's/port: 30500/port: 3001/' deployment.yaml > deployment_baremetal.yaml
grep -n 'port:' deployment_baremetal.yaml
cp -n slice_mgmt_app_params.py slice_mgmt_app_params.py.orig
sed -i -E 's/^slice_update_periodicity_secs = .*/slice_update_periodicity_secs = 30/; s/^slice_update_sfn = .*/slice_update_sfn = None/' slice_mgmt_app_params.py
grep -nE '^slice_(update|prb)' slice_mgmt_app_params.py
```

**Expected:** `port: 3001` (app) and `port: 30450` (jbpf device); `slice_update_periodicity_secs = 30`, `slice_update_sfn = None`, `slice_prb_percentage_low = 30`, `slice_prb_percentage_high = 70`.

### 10.4 Load the codelet and the app — Terminal T6

```bash
source ~/intact/env.sh
cd "$JRTC_APPS"
./unload.sh -y slice_mgmt/deployment_baremetal.yaml >/dev/null 2>&1 || true
./load.sh -y slice_mgmt/deployment_baremetal.yaml
```

**Expected from `load.sh`:** `successfully sent http request address="http://127.0.0.1:3001/app" code=200`, `loaded app id=1`, and the codelet POST to `http://127.0.0.1:30450` completing with `statusCode=201`.

**Then, in the tmux windows** (`tmux attach -t intact`):

| Window | Lines that confirm control works |
| --- | --- |
| `gnb` | `Registered codelet slice_mgmt to hook mac_sched_slice_mgmt` → `Codelet created and loaded successfully: slice_mgmt` → `Codeletset is loaded OK` |
| `jrtc` | `Sending SLICE_ALLOC_GET request` → `Received INDICATION ... num custom slices 2` → `slice 0 : pci 1 nssai 1/1 min 0 max 100` and `slice 1 ... nssai 1/2 ...` → after 30 s `Sending SLICE_ALLOC_SET` with `slice 0 ... max 30` and `slice 1 ... max 70` → `Received INDICATION` echoing `max 30` / `max 70` |

The last row is the write-and-readback proof you already achieved on PC2; here it happens with a UE attached and carrying traffic.

**If not:**

| Symptom | Fix |
| --- | --- |
| `400 Bad Request` from :3001 | App already loaded: run the `unload.sh` line, then `load.sh` again |
| `connection refused` :3001 or :30450 | jrt-controller or proxy not running: `intact-health` |
| `stat .../slice_mgmt_app.py: no such file` | `JRTC_APPS` empty in this shell: `source ~/intact/env.sh; echo $JRTC_APPS` |
| jrtc shows `Timeout waiting for stream 0` | The codelet did not reach the gNB: check the proxy window and the gNB for `Codeletset is loaded OK` |
| `num custom slices 0` or `1` | gNB started with the stage-1 config: `intact-down; intact-up` after 10.1 |
| `unload.sh` prints a 500 for the codelet DELETE | Seen on PC2 too; the app is still unloaded. Harmless. |

### 10.5 Capture the evidence — \[SCRIPT\]

Run downlink traffic for 150 s (five swaps) while the app alternates, then align each second of throughput with the command in force at that moment, and check every command against its readback.

```bash
sudo ip netns exec ue1 iperf3 -c 10.45.0.1 -u -b 30M -R -t 150 -i 1 --json > "$LOGS/prb-dl.json"

cat > ~/intact/bin/intact-prb-evidence <<'EOF'
#!/usr/bin/env python3
"""Align iperf3 per-second DL throughput with slice_mgmt SET commands; check each SET against its readback."""
import json, re, os, sys, csv, statistics as st, datetime as dt
L = os.path.expanduser("~/intact/logs")
ip = json.load(open(sys.argv[1] if len(sys.argv) > 1 else f"{L}/prb-dl.json"))
t0 = ip["start"]["timestamp"]["timesecs"]
samples = [(t0 + iv["sum"]["end"], iv["sum"]["bits_per_second"] / 1e6) for iv in ip["intervals"]]
sets, inds, mode, cur = [], [], None, None
for line in open(f"{L}/run/jrtc.out", errors="ignore"):
    m = re.match(r"(\d{4}-\d\d-\d\dT\S+)\s*:\s*slice_mgmt_app: (Sending SLICE_ALLOC_SET|Received INDICATION)", line)
    if m:
        mode, cur = ("set" if "SET" in m.group(2) else "ind"), dt.datetime.fromisoformat(m.group(1)).timestamp(); continue
    m = re.search(r"slice_mgmt_app:\s+slice 0 : .*max (\d+)", line)
    if m and mode:
        (sets if mode == "set" else inds).append((cur, int(m.group(1)))); mode = None
rows = []
for t, mbps in samples:
    eff = [v for (ts, v) in sets if ts <= t]
    rows.append((round(t, 3), round(mbps, 3), eff[-1] if eff else 100))
with open(f"{L}/prb-evidence.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["epoch_s", "dl_mbps", "slice0_max_prb_ratio"]); w.writerows(rows)
print("Throughput by commanded slice-0 PRB cap (first 3 s after each change excluded):")
chg = [ts for ts, _ in sets]
for cap in sorted({r[2] for r in rows}):
    v = [m for (t, m, c) in rows if c == cap and not any(0 <= t - x < 3 for x in chg)]
    if v: print(f"  max {cap:>3}%: n={len(v):>3}  mean {st.mean(v):6.2f} Mbit/s  median {st.median(v):6.2f}")
ok = 0
for ts, v in sets:
    nxt = [iv for (ti, iv) in inds if ti > ts]
    ok += bool(nxt and nxt[0] == v)
print(f"Readback: {ok}/{len(sets)} SET commands echoed exactly by the next INDICATION")
print(f"Per-second trace: {L}/prb-evidence.csv")
EOF
chmod +x ~/intact/bin/intact-prb-evidence && intact-prb-evidence
```

**Expected:** three groups with clearly separated means, ordered `max 30% < max 70% < max 100%` (100% being the seconds before the first SET), and `Readback: N/N`. The 30% mean will not be exactly 30% of baseline, because control channels and HARQ overhead do not scale with the cap; what matters is a clear, repeatable step at every swap.

The CSV has one row per second (`epoch_s, dl_mbps, slice0_max_prb_ratio`) and is ready to plot as throughput over time with the command shaded underneath.

**Reading these numbers (your run of 29 Oct).** The steady-state levels are what matter:

- **30 % → 8.57 Mbit/s** (median of the settled seconds);
- **70 % → 19.18 Mbit/s**;
- **uncapped: about 26.5 Mbit/s** (Part 9).

Throughput is therefore close to linear in the cap: about 0.27–0.29 Mbit/s per percentage point, or about 0.5 Mbit/s per PRB of the 52. So 30 % gives about 30–32 % of the uncapped rate and 70 % about 70–72 % of it. Two consequences:

- The cap persists in the gNB until the next SET. A throughput "sometimes about 10 and sometimes about 26 Mbit/s" between runs means the cap left in force was different.
- The UDP loss in iperf3 (about 72 % at 30 %, about 35 % at 70 %) is expected, because 30 Mbit/s was offered into a smaller pipe.

The script's group means (9.67 and 17.22) are pulled toward each other by an artefact, and B.4 identifies it: WSL steps its wall clock back by about 2 s every 30 s. The script labelled iperf3's seconds (timed on the monotonic clock) with wall-clock SET times, so many seconds were assigned to the wrong cap, increasingly so as the run went on. Measured at the gNB, the throughput follows a cap change within 0.3–0.8 s.

### 10.6 Unload and checkpoint CP4

```bash
cd "$JRTC_APPS" && ./unload.sh -y slice_mgmt/deployment_baremetal.yaml
mkdir -p "$CFG/evidence" && cp "$LOGS"/prb-dl.json "$LOGS"/prb-evidence.csv "$LOGS"/run/jrtc.out "$LOGS"/run/gnb.out "$CFG/evidence/"
intact-cp save prb-control && intact-cp mark part10-prb-control
intact-down
```

**\[PowerShell\]** Snapshot procedure from 1.7 with the name `intact-CP4`.

To go back to the single-slice setup at any time:

```bash
cp "$CFG/gnb_zmq.stage1.yaml" "$CFG/gnb_zmq.yaml"; cp "$CFG/gnb_zmq_jbpf.stage1.yaml" "$CFG/gnb_zmq_jbpf.yaml"; cp "$CFG/ue_zmq.stage1.conf" "$CFG/ue_zmq.conf"
open5gs-dbctl remove 001010123456780; open5gs-dbctl add_ue_with_apn 001010123456780 00112233445566778899aabbccddeeff 63BFA50EE6523365FF14C1F45F88737D internet
intact-cfg-check
```

(The extra AMF and NSSF slice entries can stay; they are harmless when unused.)

## Part 11 — Troubleshooting

Start every investigation with `intact-health`; it tells you which layer is broken. Then find the symptom below. If nothing matches, or a fix does not work, run `intact-diag` and send me the single file it produces: it contains every log, config and check result needed, so the next answer can be exact rather than another round of guessing.

### 11.1 The diagnostics bundle — \[SCRIPT\]

```bash
cat > ~/intact/bin/intact-diag <<'EOF'
#!/usr/bin/env bash
# intact-diag: bundle logs, configs and check results into one archive.
source ~/intact/env.sh
D=$(mktemp -d); O="$LOGS/diag-$(date +%Y%m%d-%H%M%S).tgz"
{ echo "### intact-health"; intact-health; echo "### core"; intact-core-health; echo "### cfg"; intact-cfg-check; echo "### src"; intact-src-verify; } > "$D/summary.txt" 2>&1
cp -a "$LOGS/run" "$D/run" 2>/dev/null
sudo cp /tmp/gnb.log /tmp/ue.log "$D/" 2>/dev/null
sudo tail -n 300 /var/log/open5gs/{amf,smf,upf,nrf,scp,ausf,udm,udr,pcf,nssf}.log > "$D/open5gs-tail.txt" 2>&1
cp "$CFG"/*.yaml "$CFG"/*.conf "$D/" 2>/dev/null
sudo cp /etc/open5gs/{amf,nssf,smf,upf,nrf}.yaml "$D/" 2>/dev/null
sudo dmesg -T | tail -100 > "$D/dmesg.txt"; uname -a > "$D/uname.txt"; sudo ss -tulpnS > "$D/sockets.txt" 2>&1
ls "$LOGS"/build-*.log >/dev/null 2>&1 && for f in "$LOGS"/build-*.log; do tail -60 "$f" > "$D/$(basename "$f")"; done
sudo chown -R "$USER" "$D"; tar czf "$O" -C "$D" . && rm -rf "$D"
echo "Send this file: $O"
echo "Windows path:   \\\\wsl.localhost\\intact${O}"
EOF
chmod +x ~/intact/bin/intact-diag
```

The Windows path printed at the end opens in File Explorer, so you can attach the archive directly.

### 11.2 Platform

| Symptom | Cause | Fix |
| --- | --- | --- |
| Preflight `SCTP NOT available` | WSL kernel without SCTP | Appendix A |
| After a Windows reboot nothing runs | Expected: only the core autostarts | `intact-core-health`, then `intact-up` |
| `/dev/shm` back to a small size after reboot | fstab line missing | Part 3.2, `/dev/shm` row |
| Everything slow, builds crawl | Files under `/mnt/c`, or Windows Defender scanning the distro disk | Keep work under `~/intact`; optionally add `C:\WSL` to Defender exclusions |
| Times jump, gNB complains after the laptop slept | VM clock drift after sleep | `intact-down`; **\[PowerShell\]** `wsl --shutdown`; reopen; `intact-up` |
| A process sits silently and never starts in tmux | `sudo` waiting for a password in that window | Part 8.1; run `sudo -v` before `intact-up` |

### 11.3 Core and registration

The AMF log states the reason for every reject: `sudo grep -iE 'reject|cause|fail' /var/log/open5gs/amf.log | tail`.

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Registration reject [9]` or `[90]`, `Cannot receive SBI message`, `HTTP response error [500]` | A network function is not reachable through SCP/NRF (the PC2 failure) | `intact-core-health` must show every NF registered; if not, `intact-core-restart`; then `intact-down; intact-up` |
| `Authentication failure`, `MAC failure` | K or OPc differs between UE and database | `intact-cfg-check` names it; re-add the subscriber (Part 4.6) |
| Reject with cause 11/15, or `PLMN` in the message | PLMN mismatch | `intact-cfg-check`; re-run `intact-core-plmn` |
| Reject cause 62 (no network slices available) | Requested slice not allowed | `intact-cfg-check` slice lines; Part 10.1 |
| PDU session rejected, `DNN` in SMF log | APN not provisioned | `intact-cfg-check` APN line |
| `open5gs-Xd` will not start after an edit | YAML indentation slip | Restore `/etc/open5gs/.orig-*` or `*.pre-slices-*`, `intact-core-restart` |

### 11.4 gNB

| Symptom (in `gnb.out`) | Cause | Fix |
| --- | --- | --- |
| `... was not expected`, `is not a known option`, `INI was not able to parse`, `could not be parsed` | A config key spelled differently in this srsRAN version | The message names the key. Find the right spelling with `$SRSRAN_DIR/build/apps/gnb/gnb --help 2>&1 \| grep -i <word>`, or compare with the PC2 harvest (Part 7.1). For `metrics` or `pdcch`/`pdsch` keys, deleting that block is safe for a first attach. |
| `Failed to connect to AMF ... Connection refused` | AMF down | `intact-core-health` |
| `Failed to connect to AMF ... Protocol not supported` | No SCTP | Appendix A |
| `Unable to allocate the required NG-U network resources` | Port 2152 taken on 127.0.0.1 | `sudo ss -ulpn \| grep 2152` and stop the owner (UPF uses 127.0.0.7, so it is not the cause) |
| gNB exits right after the jbpf lines | jrt-controller not running, or `/dev/shm` full | `intact-down; intact-up` (correct order); `df -h /dev/shm` |
| `Illegal instruction`, exit 132 | Should not happen with a native build | `sudo dmesg -T \| tail`; rebuild with `cmake .. -DAUTO_DETECT_ISA=OFF` added in `intact-build-gnb` |
| Metrics show `late`/underflow, or `Real-time failure` warnings | CPU contention | Close heavy Windows apps, mains power, Best performance mode |

### 11.5 UE attach and data path

| Symptom (in `ue.out`) | Cause | Fix |
| --- | --- | --- |
| Stuck at Attaching UE...; /tmp/ue.log shows Proc "Cell Selection" - Completed with failure | The UE cannot find the gNB's SSB/CORESET#0: the gNB uses a CORESET#0 or SearchSpace#0 that srsUE does not support | cell\_cfg.pdcch.common: ss0\_index 0, coreset0\_index 6 (7.1); then stop the UE and gNB and start both again, gNB first |
| Stuck at `Attaching UE...`, no `Random Access` line | No downlink samples: ports wrong, or gNB/UE from different runs | `intact-cfg-check` (ZMQ lines); then **always** `intact-down; intact-up` (gNB and UE together) |
| `Random Access Transmission` repeats, never `Complete` | gNB not receiving uplink | Same as above; check the UE TX → gNB RX port line |
| `RRC Connected` then `Received RRC Release` | NAS registration rejected | Read the cause in `amf.log` (11.3) |
| `Error opening` a TUN device or netns error | Not root, or netns missing | Launchers use `sudo`; `sudo ip netns add ue1` |
| UE has an IP, ping fails | Route, tunnel or forwarding | Part 9.2 table |
| Throughput unstable or zero | CPU contention or WSL throttling | As 11.4 last row |

### 11.6 jbpf, jrt-controller, codelet

| Symptom | Cause | Fix |
| --- | --- | --- |
| `jrtc: error while loading shared libraries` | `LD_LIBRARY_PATH` dropped by `sudo` | Use `intact-run-jrtc` (it passes the variable explicitly) |
| `JRTC_APP_PATH not set` in jrtc window | Environment not passed | Same launcher |
| No `Registration succeeded` in `gnb.out` | gNB started before jrt-controller | `intact-down; intact-up` |
| `Unable to reserve 1G huge pages` | Expected in WSL | None; regular memory is used |
| `400 Bad Request` on load | App already loaded | Unload, then load (Part 10.4) |
| `Timeout waiting for stream 0` | Codelet not in the gNB | Proxy window errors; gNB must show `Codeletset is loaded OK` |
| SET sent but the next INDICATION shows the old values | Readback taken before the change applied, or codelet built against different headers | Raise `slice_update_periodicity_secs`; run `intact-src-verify` and rebuild the codelet (6.6) |
| Readback correct, throughput does not change | UE not on slice 0, or traffic below the cap | `S_NSSAI[SST:1 SD:0x1]` must be in `smf.log`; keep `-b 30M` so the link is saturated |

## Appendix A — Custom WSL kernel with SCTP

Use this only if `intact-preflight` reports `SCTP NOT available`. You build Microsoft's own WSL kernel source with SCTP compiled in, and point WSL at it. About 30 minutes, fully reversible by deleting one line.

Two facts shape the configuration. First, the kernel setting is **global**: every distro and Docker Desktop will boot this kernel. Second, with a custom kernel the stock module package no longer matches, so anything you need must be built **into** the kernel (`=y`), not as a module. That is why SCTP, TUN and ISO9660 are forced to `y` below; ISO9660 keeps Docker Desktop working, a breakage reported for custom WSL kernels ([WSL issue 40573](https://github.com/microsoft/WSL/issues/40573)).

### A.1 Build — \[WSL\]

```bash
source ~/intact/env.sh
sudo apt install -y build-essential flex bison dwarves libssl-dev libelf-dev bc cpio python3
KV=$(uname -r | cut -d- -f1); echo "running kernel $KV"
cd ~/intact/src
git clone --depth 1 -b "linux-msft-wsl-$KV" https://github.com/microsoft/WSL2-Linux-Kernel.git wsl-kernel \
  || git clone --depth 1 https://github.com/microsoft/WSL2-Linux-Kernel.git wsl-kernel
cd wsl-kernel
cp Microsoft/config-wsl .config
./scripts/config --enable CONFIG_IP_SCTP --enable CONFIG_INET_SCTP_DIAG \
                 --enable CONFIG_TUN --enable CONFIG_NET_NS --enable CONFIG_ISO9660_FS \
                 --set-str CONFIG_LOCALVERSION "-intact-sctp"
make olddefconfig
grep -E '^CONFIG_(IP_SCTP|TUN|NET_NS|ISO9660_FS)=' .config
make -j"$(nproc)" bzImage 2>&1 | tee "$LOGS/build-kernel.log" | tail -3
mkdir -p /mnt/c/wsl-kernels && cp arch/x86/boot/bzImage /mnt/c/wsl-kernels/bzImage-intact-sctp
ls -la /mnt/c/wsl-kernels/
```

**Expected:** the `grep` prints all four as `=y`; the build ends with `Kernel: arch/x86/boot/bzImage is ready`; the file appears in `C:\wsl-kernels`.

**If not:** a `-b linux-msft-wsl-...` clone error is handled by the fallback clone of the default branch (a newer kernel, which is fine). `make` stops on a missing tool → install the package it names and rerun `make`. The build must run on the Linux filesystem (as here), never under `/mnt/c`.

### A.2 Point WSL at it — \[PowerShell\]

```powershell
Copy-Item $env:USERPROFILE\.wslconfig $env:USERPROFILE\.wslconfig.bak-before-kernel
Add-Content $env:USERPROFILE\.wslconfig "kernel=C:\\wsl-kernels\\bzImage-intact-sctp"
Get-Content $env:USERPROFILE\.wslconfig
wsl --shutdown
wsl -d intact
```

The `kernel=` line must sit inside the `[wsl2]` section (it does if `[wsl2]` is the only section). Note the doubled backslashes.

### A.3 Verify — \[WSL\]

```bash
uname -r          # Expected: ends with -intact-sctp (possibly with a trailing +)
intact-preflight  # Expected: [PASS] SCTP sockets work
```

**If WSL fails to start** after `wsl --shutdown`: **\[PowerShell\]** `Copy-Item $env:USERPROFILE\.wslconfig.bak-before-kernel $env:USERPROFILE\.wslconfig -Force; wsl --shutdown`. You are back on the stock kernel.

**Rollback later:** remove the `kernel=` line from `.wslconfig` and `wsl --shutdown`.

## Appendix B — Randomized Gate A harness

Part 10 proves control with the stock app, which alternates 30/70 strictly. Gate A needs the assignment drawn **at random** each trial (strict alternation cannot distinguish a real write from a system that alternates on its own), a fixed number of trials, and one machine-readable row per trial. This patcher makes exactly those changes to `slice_mgmt_app.py`.

It checks the code against the version reviewed on PC2 (jrtc-apps `9a096f1f`, the same commit pinned here) and **refuses to change anything** if an anchor differs, instead of guessing. It also fixes an upstream bug visible in PC2's trace: the first SET assigns `s0.priority` twice and never `s1.priority` (PC2 read back slice 0 priority 1, slice 1 priority 0).

### B.0 One-time preparation: gNB metrics every 0.5 s, and three helper scripts — \[SCRIPT\]

The stack is down after 10.6. Before you start it again, this block does two things.

First, it installs four helpers, used in B.1–B.4 and in Appendix D:

- **`intact-kpm-period`** sets `metrics.periodicity.du_report_period` to 500 ms in both gNB configs, so the gNB pushes its scheduler metrics (`dl_brate`, `dl_bs`, MCS, CQI) every 0.5 s instead of every 1 s. It backs each file up, checks it with `gnb --dryrun`, and restores the original automatically if the gNB rejects it.
- **`intact-kpm-log`** logs every gNB scheduler report with its arrival time on both clocks: wall (`t`) and monotonic (`tm`).
- **`intact-gate-a-effect`** is the B.4 analysis (version 2: one continuous clock, direction checked against the command, every window bounded to its own trial).
- **`intact-clock-check`** shows whether WSL steps the wall clock on your machine (B.4).

If you already ran B.0 once, paste the block again. It replaces the helpers with their current versions; the config change is idempotent.

Second, it runs `intact-down` (harmless if the stack is already down) and `intact-kpm-period 500`.

Paste the whole block.

```bash
cat > ~/intact/bin/intact-kpm-period <<'KPMPEOF'
#!/usr/bin/env bash
# intact-kpm-period [ms]: set metrics.periodicity.du_report_period (default 500 ms) in both gNB
# configs, so the gNB pushes scheduler metrics (dl_brate, dl_bs, MCS, CQI) every 0.5 s instead of 1 s.
# Each file is backed up, validated with 'gnb --dryrun', and restored automatically if rejected.
set -euo pipefail
source ~/intact/env.sh
MS="${1:-500}"
TS=$(date +%Y%m%d-%H%M%S)
G="$SRSRAN_DIR/build/apps/gnb/gnb"
pgrep -x 'gnb|srsue' >/dev/null && { echo "STOP: gNB/UE running. Run intact-down first."; exit 1; }
sudo -v
for f in "$CFG/gnb_zmq.yaml" "$CFG/gnb_zmq_jbpf.yaml"; do
  cp "$f" "$f.bak-kp-$TS"
  python3 - "$f" "$MS" <<'PY'
import sys, re
f, ms = sys.argv[1], int(sys.argv[2])
L = open(f).read().splitlines(keepends=True)
i = next((k for k, l in enumerate(L) if re.match(r"metrics:\s*(#.*)?$", l)), None)
if i is None:
    sys.exit("STOP: no top-level 'metrics:' block in " + f + " (run intact-telemetry-setup, Part 9b)")
j = i + 1                                   # end of the metrics block
while j < len(L) and (not L[j].strip() or L[j][0].isspace()):
    j += 1
blk = L[i + 1:j]
blk = [l for l in blk if not re.match(r"\s+du_report_period:", l)]
k = next((n for n, l in enumerate(blk) if re.match(r"  periodicity:\s*(#.*)?$", l)), None)
if k is None:
    blk = ["  periodicity:\n", f"    du_report_period: {ms}      # gNB scheduler metrics every {ms} ms (PACT epoch 0.5 s)\n"] + blk
else:
    blk.insert(k + 1, f"    du_report_period: {ms}      # gNB scheduler metrics every {ms} ms (PACT epoch 0.5 s)\n")
open(f, "w").write("".join(L[:i + 1] + blk + L[j:]))
PY
  rc=0; out=$(cd "$(dirname "$G")" && sudo timeout 30 "$G" -c "$f" --dryrun 2>&1) || rc=$?
  if [ $rc -eq 0 ] && ! grep -qiE 'not expected|error|invalid|could not' <<<"$out"; then
    echo "  [ OK ] ${f##*/}: du_report_period $MS accepted by 'gnb --dryrun'"
  else
    cp "$f.bak-kp-$TS" "$f"
    echo "  [FAIL] ${f##*/}: gnb rejected it, original restored. gnb said:"; sed 's/^/        /' <<<"$out" | tail -6; exit 1
  fi
done
grep -n -A3 '^metrics:' "$CFG/gnb_zmq_jbpf.yaml" | sed 's/^/        /'
echo "KPM PERIOD SET. Backups: *.bak-kp-$TS. Start the stack again (intact-up, or your manual windows)."
KPMPEOF

cat > ~/intact/bin/intact-kpm-log <<'KPMLEOF'
#!/usr/bin/env python3
"""intact-kpm-log OUT.jsonl : log every gNB scheduler report (ws://127.0.0.1:8001) with its arrival
time on THIS machine. One JSON line per report: t (wall clock, s), tm (monotonic clock, s), dl_brate (bit/s),
dl_bs (bytes), dl_mcs, cqi, n_ue. Use tm for timing: WSL steps its wall clock back every ~30 s.
Runs until Ctrl-C or kill; reconnects if the gNB restarts."""
import base64, json, os, socket, struct, sys, time

def ws_messages(host="127.0.0.1", port=int(os.environ.get("KPM_PORT", "8001"))):
    s = socket.create_connection((host, port), timeout=3)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = s.recv(4096)
        if not c:
            raise ConnectionError("closed during handshake")
        buf += c
    head, buf = buf.split(b"\r\n\r\n", 1)
    if b" 101" not in head.split(b"\r\n")[0]:
        raise ConnectionError("no websocket upgrade")
    msg = json.dumps({"cmd": "metrics_subscribe"}).encode()
    mask = os.urandom(4)
    s.sendall(bytes([0x81, 0x80 | len(msg)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(msg)))
    s.settimeout(None)

    def exact(n):
        nonlocal buf
        while len(buf) < n:
            c = s.recv(65536)
            if not c:
                raise ConnectionError("closed")
            buf += c
        d, buf = buf[:n], buf[n:]
        return d
    parts = []
    while True:
        b1, b2 = exact(2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", exact(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", exact(8))[0]
        mk = exact(4) if b2 & 0x80 else None
        p = exact(n)
        if mk:
            p = bytes(b ^ mk[i % 4] for i, b in enumerate(p))
        op = b1 & 0x0F
        if op == 8:
            raise ConnectionError("server closed")
        if op == 9:
            s.sendall(bytes([0x8A, 0x80 | len(p)]) + b"\0\0\0\0" + p)
            continue
        if op in (0, 1, 2):
            parts.append(p)
            if b1 & 0x80:
                yield b"".join(parts).decode(errors="ignore")
                parts = []

out = open(sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/intact/logs/kpm.jsonl"), "a")
n = 0
while True:
    try:
        for m in ws_messages():
            t, tm = time.time(), time.monotonic()
            try:
                o = json.loads(m)
            except ValueError:
                continue
            cells = o.get("cells") if isinstance(o, dict) else None
            if not isinstance(cells, list):
                continue
            row = {"t": round(t, 3), "tm": round(tm, 3), "n_ue": 0, "dl_brate": 0.0, "dl_bs": 0.0, "dl_mcs": 0.0, "cqi": 0.0}
            for c in cells:
                for u in (c.get("ue_list") or []):
                    row["n_ue"] += 1
                    for k in ("dl_brate", "dl_bs", "dl_mcs", "cqi"):
                        row[k] += float(u.get(k, 0) or 0)
            if row["n_ue"] > 1:
                row["dl_mcs"] /= row["n_ue"]; row["cqi"] /= row["n_ue"]
            out.write(json.dumps(row) + "\n"); out.flush()
            n += 1
            if n == 1:
                print(f"intact-kpm-log: first report logged ({row['n_ue']} UE, {row['dl_brate']/1e6:.2f} Mbit/s)", flush=True)
    except KeyboardInterrupt:
        break
    except Exception as e:
        print(f"intact-kpm-log: {e}; retrying in 2 s", file=sys.stderr, flush=True)
        time.sleep(2)
KPMLEOF

cat > ~/intact/bin/intact-gate-a-effect <<'GAEEOF'
#!/usr/bin/env python3
"""intact-gate-a-effect (v2): how fast does a slice-cap change take effect, at the gNB and at the UE?

Inputs (B.2/B.3): gate_a.csv (SET times + commands), gate-a-kpm.jsonl (gNB scheduler reports),
gate-a-dl.json (iperf3 receiver at the UE), gate-a-ping.txt (UE ping RTT).

v2 fixes three problems of v1:
  * CLOCKS. WSL steps its wall clock (time of day) back by ~2 s about every 30 s; the monotonic clock is
    never stepped. iperf3 measures elapsed time on the monotonic clock while the app, ping and the KPM
    logger stamped wall-clock times, so the UE timeline drifted ~2 s per trial against the SET times.
    v2 puts every event on ONE continuous clock: the monotonic stamps when they exist (tm in the KPM log,
    t_sent_mono in gate_a.csv), otherwise the wall clock with the steps detected in the KPM log removed.
  * DIRECTION. The expected direction of each step comes from the COMMAND (cap up -> throughput up), and a
    trial whose levels moved the other way, or with no traffic, is marked invalid instead of given a lag.
  * BOUNDS. Every search and every level window stays inside its own trial (SET .. next SET).
Usage: intact-gate-a-effect [evidence_dir] [logs_dir]
"""
import csv, json, os, re, statistics as st, sys, datetime as dt

E = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/intact/configs/evidence")
L = sys.argv[2] if len(sys.argv) > 2 else os.path.expanduser("~/intact/logs")


def iso(s):
    return dt.datetime.fromisoformat(s).timestamp()


# ---------------------------------------------------------------- the continuous clock (from the KPM log)
kpm = []
for line in open(os.path.join(L, "gate-a-kpm.jsonl")):
    line = line.strip()
    if line:
        kpm.append(json.loads(line))
if not kpm:
    sys.exit("gate-a-kpm.jsonl is empty: was intact-kpm-log running during B.2?")
have_tm = all("tm" in o for o in kpm)
diffs = [b["t"] - a["t"] for a, b in zip(kpm, kpm[1:])]
period = st.median([d for d in diffs if d > 0]) if diffs else 0.5
pairs, c, steps = [], None, []                      # (wall, continuous) in file order
for i, o in enumerate(kpm):
    if have_tm:
        c = o["tm"]
        if i and (o["t"] - kpm[i - 1]["t"]) - (o["tm"] - kpm[i - 1]["tm"]) < -0.2:
            steps.append((o["t"] - kpm[i - 1]["t"]) - (o["tm"] - kpm[i - 1]["tm"]))
    else:
        if c is None:
            c = o["t"]
        else:
            d = o["t"] - kpm[i - 1]["t"]
            if d < 0 or d > 5 * period:             # a clock step (or a gap): count one report period
                steps.append(d - period)
                d = period
            c += d
    pairs.append((o["t"], c))
    o["c"] = c


def wall_to_c(w, hint=None):
    """Map a wall-clock time onto the continuous clock. After a backward step a wall time can occur
    twice; the candidate closest to `hint` (the expected continuous time) wins."""
    cands = []
    for (w0, c0), (w1, c1) in zip(pairs, pairs[1:]):
        if w0 <= w <= w1 and 0 <= w1 - w0 < 5 * period:
            cands.append(c0 + (w - w0) * (c1 - c0) / (w1 - w0) if w1 > w0 else c0)
    if not cands:
        w0, c0 = min(pairs, key=lambda p: abs(p[0] - w))
        cands = [c0 + (w - w0)]
    return min(cands, key=lambda x: abs(x - hint)) if hint is not None else cands[0]


# ---------------------------------------------------------------- trials (SET times on the continuous clock)
trials = list(csv.DictReader(open(os.path.join(E, "gate_a.csv"))))
use_mono = have_tm and trials and trials[0].get("t_sent_mono") not in (None, "")
prev_c = None
for r in trials:
    if use_mono:
        r["c"] = float(r["t_sent_mono"])
    else:
        r["c"] = wall_to_c(iso(r["t_sent_utc"]), None if prev_c is None else prev_c + 30.0)
    prev_c = r["c"]
trials.sort(key=lambda r: r["c"])
spacing = st.median([b["c"] - a["c"] for a, b in zip(trials, trials[1:])]) if len(trials) > 1 else 30.0
for k, r in enumerate(trials):
    r["c_next"] = trials[k + 1]["c"] if k + 1 < len(trials) else r["c"] + spacing

# ---------------------------------------------------------------- series on the continuous clock
gnb = [(o["c"], o["dl_brate"] / 1e6) for o in kpm if o.get("n_ue", 0) >= 1]
ue, ue_note = [], ""
try:
    ip = json.load(open(os.path.join(L, "gate-a-dl.json")))
    start_c = wall_to_c(ip["start"]["timestamp"]["timesecs"] + 0.5, kpm[0]["c"])   # timesecs is whole seconds
    ue = [(start_c + iv["sum"]["end"], iv["sum"]["bits_per_second"] / 1e6) for iv in ip["intervals"]]
    ue_note = "iperf3 elapsed time is monotonic; its start time is known to +-0.5 s"
except Exception as e:
    ue_note = f"no UE-side iperf data ({e})"
ping, hint = [], None
try:
    for line in open(os.path.join(L, "gate-a-ping.txt")):
        m = re.match(r"\[(\d+\.\d+)\].*time=([\d.]+) ms", line)
        if m:
            cc = wall_to_c(float(m.group(1)), hint)
            hint = cc + 0.2
            ping.append((cc, float(m.group(2))))
except Exception as e:
    print(f"  (no ping log: {e})")


def level(series, a, b):
    v = [y for t, y in series if a <= t <= b]
    return st.median(v) if len(v) >= 2 else None


def crossing(series, t0, t1, mid, up):
    for t, y in series:
        if t0 < t < t1 and ((y > mid) if up else (y < mid)):
            return t
    return None


def slope(pts):
    if len(pts) < 3:
        return None
    mx, my = st.mean(p[0] for p in pts), st.mean(p[1] for p in pts)
    den = sum((p[0] - mx) ** 2 for p in pts)
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / den if den else None


# ---------------------------------------------------------------- per trial
f = lambda x, w=5, p=1: ("-" if x is None else f"{x:{w}.{p}f}").rjust(w)
print(f"clock: {'monotonic stamps' if use_mono else 'wall clock with steps removed'}; wall-clock steps seen in the KPM log: "
      f"{len(steps)}" + (f" (median {st.median(steps):+.2f} s)" if steps else ""))
print(f"gNB report period: {period:.2f} s.  UE timeline: {ue_note}")
print(f"{'trial':>5} {'t(min)':>6} {'cap':>8} {'gNB pre->post':>14} {'gNB lag':>7} {'rep#':>4} {'UE pre->post':>14} {'UE lag':>6} {'ping ms':>8}  status")
rows, prev = [], 100
T0 = trials[0]["c"] if trials else 0
for r in trials:
    t, tn, cap = r["c"], r["c_next"], int(r["cmd_s0_max"])
    pr = [y for tt, y in ping if t <= tt < tn]
    row = {"trial": r["trial"], "t_min": round((t - T0) / 60, 2), "from": prev, "to": cap,
           "ping_ms": st.median(pr) if pr else None, "status": "no change"}
    if cap != prev:
        up = cap > prev
        gp, gq = level(gnb, t - 8, t - 0.3), level(gnb, t + 10, tn - 0.5)
        uqp, uqq = level(ue, t - 8, t), level(ue, t + 15, tn)
        row.update(gnb_pre=gp, gnb_post=gq, ue_pre=uqp, ue_post=uqq)
        if gp is None or gq is None or min(gp, gq) < 1.0:
            row["status"] = "INVALID: no traffic in a window"
        elif (gq > gp + 2.0) != up or abs(gq - gp) < 2.0:
            row["status"] = "INVALID: gNB level moved against the command"
        else:
            tc = crossing(gnb, t, tn, (gp + gq) / 2, up)
            row["gnb_lag"] = None if tc is None else tc - t
            row["reports"] = None if tc is None else sum(1 for tt, _ in gnb if t < tt <= tc)
            row["status"] = "ok"
            if uqp is not None and uqq is not None and abs(uqq - uqp) >= 2.0 and (uqq > uqp) == up:
                tu = crossing(ue, t, tn, (uqp + uqq) / 2, up)
                row["ue_lag"] = None if tu is None else tu - t
            elif uqp is not None and uqq is not None:
                row["status"] = "ok at gNB; UE levels did not follow (check UE timeline)"
    print(f"{r['trial']:>5} {row['t_min']:6.1f} {prev:>3}->{cap:<3} {f(row.get('gnb_pre'))}->{f(row.get('gnb_post'),6)} "
          f"{f(row.get('gnb_lag'),7,2)} {('-' if row.get('reports') is None else str(row['reports'])):>4} "
          f"{f(row.get('ue_pre'))}->{f(row.get('ue_post'),6)} {f(row.get('ue_lag'),6)} {f(row['ping_ms'],8,0)}  {row['status']}")
    rows.append(row)
    prev = cap

keys = ["trial", "t_min", "from", "to", "gnb_pre", "gnb_post", "gnb_lag", "reports", "ue_pre", "ue_post", "ue_lag", "ping_ms", "status"]
with open(os.path.join(E, "gate-a-effect.csv"), "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=keys)
    w.writeheader()
    w.writerows([{k: x.get(k) for k in keys} for x in rows])

# ---------------------------------------------------------------- throughput by commanded cap (settled samples only)
print("\nThroughput by commanded slice-0 cap, settled samples (gNB: 3 s after the SET to the next SET;"
      " UE: 10 s after the SET to the next SET):")
by = {}
for r in trials:
    cap = int(r["cmd_s0_max"])
    by.setdefault(cap, {"gnb": [], "ue": []})
    by[cap]["gnb"] += [y for t, y in gnb if r["c"] + 3 <= t < r["c_next"] and y > 0.5]
    by[cap]["ue"] += [y for t, y in ue if r["c"] + 10 <= t < r["c_next"] and y > 0.5]
for cap in sorted(by):
    g, u = by[cap]["gnb"], by[cap]["ue"]
    print(f"  max {cap:3d}%:  gNB n={len(g):4d} median {f(st.median(g) if g else None,6,2)} Mbit/s   "
          f"UE n={len(u):4d} median {f(st.median(u) if u else None,6,2)} Mbit/s")

ok = [x for x in rows if x["status"].startswith("ok")]
gl = [(x["t_min"], x["gnb_lag"]) for x in ok if x.get("gnb_lag") is not None]
ul = [(x["t_min"], x["ue_lag"]) for x in ok if x.get("ue_lag") is not None]
pl = [(x["t_min"], x["ping_ms"]) for x in rows if x.get("ping_ms") is not None]
rep = [x["reports"] for x in ok if x.get("reports") is not None]
print()
for name, pts, unit in (("gNB-side lag", gl, "s"), ("UE-side lag", ul, "s"), ("UE ping RTT", pl, "ms")):
    if pts:
        s = slope(pts)
        print(f"  {name:13s}: median {st.median(p[1] for p in pts):8.2f} {unit}  max {max(p[1] for p in pts):8.2f} {unit}"
              f"  (n={len(pts)}), trend {'n/a' if s is None else f'{s:+.2f} {unit}/min'}")
changed = [x for x in rows if x["status"] != "no change"]
print(f"  changed trials: {len(changed)}, valid: {len(ok)}, invalid: {len(changed) - len(ok)}")
print()
caps = sorted(by)
ordered = len(caps) >= 2 and all(by[a]["gnb"] and by[b]["gnb"] and st.median(by[a]["gnb"]) + 2 < st.median(by[b]["gnb"])
                                 for a, b in zip(caps, caps[1:]))
print(f"GATE A throughput criterion (gNB side): {'PASS' if ordered else 'FAIL'} - throughput ordered by commanded cap")
if rep:
    n1 = sum(1 for x in rep if x <= 1)
    n2 = sum(1 for x in rep if x <= 2)
    print(f"CONTROL TIMING: the step was visible in the 1st gNB report after the SET in {n1}/{len(rep)} trials and by the 2nd in "
          f"{n2}/{len(rep)}; median {st.median(p[1] for p in gl):.2f} s, max {max(p[1] for p in gl):.2f} s.")
    print("  A controller deciding once per report therefore sees the effect of its write 1-2 decisions later "
          "(PACT's lease of 2 epochs spans this). This run spaced SETs 30 s apart; it does not test back-to-back writes.")
if ul:
    s = slope(ul)
    print(f"UE SIDE: median lag {st.median(p[1] for p in ul):.1f} s, trend {s:+.2f} s/min." if s is not None else "")
    if pl:
        print(f"  The UE ping RTT in the same trials is {st.median(p[1] for p in pl)/1000:.1f} s (median): queueing in the full DL"
              " buffer delays everything the UE receives by seconds.")
    if s is not None and abs(s) < 0.5:
        print("  No growing lag remains once all events are on one clock: the growth seen before was the wall-clock steps.")
print(f"Per-trial table: {os.path.join(E, 'gate-a-effect.csv')}")
GAEEOF

cat > ~/intact/bin/intact-clock-check <<'CLKEOF'
#!/usr/bin/env python3
"""intact-clock-check [seconds]: compare the wall clock (time of day) with the monotonic clock in this
WSL distro. Prints every time the wall clock is stepped, and the drift between steps. Default 120 s."""
import sys, time
dur = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
w0, m0 = time.time(), time.monotonic()
prev = w0 - m0
steps = []
print(f"watching for {dur:.0f} s (wall - monotonic offset, sampled every 0.1 s) ...", flush=True)
while time.monotonic() - m0 < dur:
    time.sleep(0.1)
    off = time.time() - time.monotonic()
    if abs(off - prev) > 0.05:
        steps.append((time.monotonic() - m0, off - prev))
        print(f"  t={time.monotonic() - m0:7.1f} s  wall clock stepped by {off - prev:+.3f} s", flush=True)
    prev = off
w1, m1 = time.time(), time.monotonic()
el_m, el_w = m1 - m0, w1 - w0
print(f"monotonic advanced {el_m:.1f} s, wall clock advanced {el_w:.1f} s ({(el_w - el_m) / el_m * 100:+.1f} %), "
      f"{len(steps)} step(s)" + (f", total {sum(s for _, s in steps):+.2f} s" if steps else ""))
print("RESULT:", "wall clock is stepped -> never mix wall-clock and monotonic timestamps" if steps
      else "no steps in this window")
CLKEOF

chmod +x ~/intact/bin/intact-kpm-period ~/intact/bin/intact-kpm-log ~/intact/bin/intact-gate-a-effect ~/intact/bin/intact-clock-check
intact-down
intact-kpm-period 500
```

**Expected:** two lines, `[ OK ] gnb_zmq.yaml: du_report_period 500 accepted by 'gnb --dryrun'` and the same for `gnb_zmq_jbpf.yaml`. Then the first lines of the `metrics:` block, now containing `periodicity:` / `du_report_period: 500`, and finally `KPM PERIOD SET`.

**If not:** a `[FAIL]` line means the file was restored unchanged; send me the six lines printed under it. If you ever rerun `intact-telemetry-setup` (Part 9b), rerun `intact-kpm-period` afterwards, because that setup rewrites the `metrics:` block.

**\[MANUAL\] Then start the stack exactly as you did for Part 10** (your separate windows for jrt-controller, gNB, reverse proxy and UE, or `intact-up`), and wait until the UE has its IP address. The gNB window now prints its per-UE table twice per second. Then open one more terminal for the iperf3 server and leave it running for B.2:

```bash
iperf3 -s -B 10.45.0.1
```

### B.1 Patch — \[SCRIPT\]

```bash
cat > ~/intact/bin/intact-gate-a-patch <<'EOF'
#!/usr/bin/env python3
"""Patch jrtc_apps/slice_mgmt for randomized Gate A trials (version 2).
v2: stops sending SETs after gate_a_trials trials, and records monotonic timestamps
(t_sent_mono, t_obs_mono) next to UTC, because the WSL wall clock is stepped back every ~30 s.
A file already patched by v1 is upgraded in place. Changes nothing if the code differs from what was reviewed."""
import os, sys, shutil, textwrap
A = os.path.join(os.environ["JRTC_APPS"], "slice_mgmt")
app, par = os.path.join(A, "slice_mgmt_app.py"), os.path.join(A, "slice_mgmt_app_params.py")
T = open(app).read()
L = T.split("\n")
def find(test, start, what):
    for i in range(start, len(L)):
        if test(L[i].strip()): return i
    sys.exit(f"ANCHOR NOT FOUND: {what}. Nothing changed. Send me slice_mgmt_app.py.")
def ind(line): return line[:len(line) - len(line.lstrip())]
HEADER_V1 = 'obs_sfn,obs_slot,match\\n'
HEADER_V2 = 'obs_sfn,obs_slot,match,t_sent_mono,t_obs_mono\\n'
ROW_V1 = "{data.sfn},{data.slot_index},{match}\\n')"
ROW_V2 = "{data.sfn},{data.slot_index},{match},{_GATE_A[\"t_sent_mono\"]:.3f},{time.monotonic():.3f}\\n')"
STOP = r'''if _GATE_A["n"] >= _GATE_A["trials"]:
    params.slice_update_periodicity_secs = 10 ** 8   # no further SETs: Gate A is complete
    state.logger.log_msg(True, False, "", f'GATE_A done: {_GATE_A["n"]} trials, no further SETs (unload the app when convenient)')'''

if "_GATE_A" in T:                                     # ---------- upgrade a v1-patched file
    if "GATE_A done" in T:
        sys.exit("already patched (v2). Restore slice_mgmt_app.py.pre-gate-a to start over.")
    if T.count(HEADER_V1) != 1 or T.count(ROW_V1) != 1:
        sys.exit("Patched file differs from the reviewed v1 patch. Nothing changed. Send me slice_mgmt_app.py.")
    k = find(lambda s: s == '_GATE_A["t_sent"] = dt.datetime.now(dt.timezone.utc)', 0, "t_sent line")
    L.insert(k + 1, ind(L[k]) + '_GATE_A["t_sent_mono"] = time.monotonic()')
    r = find(lambda s: s == '_GATE_A["commanded"] = None', k + 2, "end of RECORD block")
    L[r + 1:r + 1] = textwrap.indent(STOP, ind(L[r])).split("\n")
    T2 = "\n".join(L).replace(HEADER_V1, HEADER_V2).replace(ROW_V1, ROW_V2)
    shutil.copy(app, app + ".pre-gate-a-v2")
    open(app, "w").write(T2)
    print("upgraded v1 -> v2:", app, "(backup: .pre-gate-a-v2; original stock file still in .pre-gate-a)")
    sys.exit(0)

HARNESS = r'''
# ---- Gate A harness (inserted by intact-gate-a-patch v2) ----
import random as _rnd
_GATE_A = {"rng": _rnd.Random(getattr(params, "gate_a_seed", 12345)),
           "trials": getattr(params, "gate_a_trials", 20),
           "csv": getattr(params, "gate_a_csv", "/tmp/gate_a.csv"),
           "enabled": getattr(params, "gate_a_enabled", False),
           "n": 0, "commanded": None, "t_sent": None, "t_sent_mono": None, "header": False}

def _gate_a_header():
    if not _GATE_A["header"]:
        with open(_GATE_A["csv"], "w") as f:
            f.write("trial,t_sent_utc,t_obs_utc,latency_ms,cmd_s0_max,cmd_s1_max,obs_s0_max,obs_s1_max,obs_sfn,obs_slot,match,t_sent_mono,t_obs_mono\n")
        _GATE_A["header"] = True
'''
SWAP = r'''
if _GATE_A["enabled"]:
    hi_on_s0 = _GATE_A["rng"].random() < 0.5
    lo, hi = params.slice_prb_percentage_low, params.slice_prb_percentage_high
    s0.min_prb_policy_ratio = 0
    s1.min_prb_policy_ratio = 0
    s0.max_prb_policy_ratio = hi if hi_on_s0 else lo
    s1.max_prb_policy_ratio = lo if hi_on_s0 else hi
    s0.priority = 1
    s1.priority = 1
    _GATE_A["commanded"] = (s0.max_prb_policy_ratio, s1.max_prb_policy_ratio)
    _GATE_A["t_sent"] = dt.datetime.now(dt.timezone.utc)
    _GATE_A["t_sent_mono"] = time.monotonic()
elif not state.first_request_sent:
    s0.min_prb_policy_ratio = 0
    s0.max_prb_policy_ratio = params.slice_prb_percentage_low
    s0.priority = 1
    s1.min_prb_policy_ratio = 0
    s1.max_prb_policy_ratio = params.slice_prb_percentage_high
    s1.priority = 1  # upstream set s0.priority twice and never s1
else:'''
RECORD = r'''
if _GATE_A["enabled"] and _GATE_A["commanded"] is not None:
    _gate_a_header()
    t_obs = dt.datetime.now(dt.timezone.utc)
    cmd = _GATE_A["commanded"]
    obs = (slices[0].max_prb_policy_ratio, slices[1].max_prb_policy_ratio) if data.slice_count >= 2 else (None, None)
    lat = (t_obs - _GATE_A["t_sent"]).total_seconds() * 1000.0
    _GATE_A["n"] += 1
    match = int(obs == cmd)
    with open(_GATE_A["csv"], "a") as f:
        f.write(f'{_GATE_A["n"]},{_GATE_A["t_sent"].isoformat()},{t_obs.isoformat()},{lat:.1f},{cmd[0]},{cmd[1]},{obs[0]},{obs[1]},{data.sfn},{data.slot_index},{match},{_GATE_A["t_sent_mono"]:.3f},{time.monotonic():.3f}\n')
    state.logger.log_msg(True, False, "", f'GATE_A trial {_GATE_A["n"]}/{_GATE_A["trials"]}: cmd={cmd} obs={obs} match={bool(match)} latency={lat:.1f}ms')
    _GATE_A["commanded"] = None
''' + STOP + "\n"
h = find(lambda s: s.startswith("def app_handler("), 0, "def app_handler")
k = find(lambda s: s == "if not state.first_request_sent:", h, "if not state.first_request_sent:")
expect = ["s0.min_prb_policy_ratio = 0", "s0.max_prb_policy_ratio = params.slice_prb_percentage_low", "s0.priority = 1",
          "s1.min_prb_policy_ratio = 0", "s1.max_prb_policy_ratio = params.slice_prb_percentage_high", "s0.priority = 1", "else:"]
got = [L[k + 1 + i].strip() for i in range(len(expect))]
if got != expect:
    sys.exit("Swap block differs from the reviewed code. Nothing changed. Found:\n  " + "\n  ".join(got))
r = find(lambda s: s == "state.slice_allocation = data", k, "state.slice_allocation = data")
shutil.copy(app, app + ".pre-gate-a")
L[r:r] = textwrap.indent(RECORD.strip("\n"), ind(L[r])).split("\n")
L[k:k + 8] = textwrap.indent(SWAP.strip("\n"), ind(L[k])).split("\n")
L[h:h] = HARNESS.split("\n")
open(app, "w").write("\n".join(L))
if "gate_a_enabled" not in open(par).read():
    with open(par, "a") as f:
        f.write('\n# ---- Gate A ----\ngate_a_enabled = True\ngate_a_trials = 20\ngate_a_seed = 12345\ngate_a_csv = "/tmp/gate_a.csv"\n')
print("patched (v2):", app, "(backup: .pre-gate-a)")
EOF
chmod +x ~/intact/bin/intact-gate-a-patch
source ~/intact/env.sh && intact-gate-a-patch && python3 -c 'import sys; p=sys.argv[1]; compile(open(p,"rb").read(), p, "exec"); print("syntax OK")' "$JRTC_APPS/slice_mgmt/slice_mgmt_app.py"
```

**Expected:** `patched (v2): ...` (or, if you had already applied the first version, `upgraded v1 -> v2: ...`), then `syntax OK`. The check uses `compile()`, which writes nothing. The first version used `py_compile`, which tries to write a `.pyc` file into `slice_mgmt/__pycache__`; that folder belongs to root because jrt-controller runs the app as root. That `Permission denied` came after the patch had been applied, so the patch itself was fine.

Version 2 changes two things. The harness **stops sending SETs after `gate_a_trials` trials** and logs `GATE_A done`. Each row of `gate_a.csv` also carries **monotonic timestamps** (`t_sent_mono`, `t_obs_mono`).

**If `ANCHOR NOT FOUND`, `Swap block differs` or `Patched file differs`:** nothing was changed; send me the file it names.

### B.2 Run 20 trials — Terminal T6

**Prerequisites.** The stack must be up after B.0, the UE attached, and the iperf3 server running in its own terminal. The block first starts two background loggers:

- `intact-kpm-log`: every gNB scheduler report goes to `gate-a-kpm.jsonl`;
- a UE ping to 10.45.0.1 every 0.2 s, with timestamps, goes to `gate-a-ping.txt`.

It then loads the patched app and starts the 13-minute downlink test. The loggers let B.4 time each cap change at the gNB and at the UE on one clock.

```bash
source ~/intact/env.sh
cd "$JRTC_APPS"
./unload.sh -y slice_mgmt/deployment_baremetal.yaml >/dev/null 2>&1 || true
sudo rm -f /tmp/gate_a.csv; rm -f "$LOGS"/gate-a-kpm.jsonl "$LOGS"/gate-a-ping.txt
intact-kpm-log "$LOGS/gate-a-kpm.jsonl" > "$LOGS/gate-a-kpm.out" 2>&1 &
sudo ip netns exec ue1 ping -D -i 0.2 10.45.0.1 > "$LOGS/gate-a-ping.txt" 2>&1 &
./load.sh -y slice_mgmt/deployment_baremetal.yaml
sudo ip netns exec ue1 iperf3 -c 10.45.0.1 -u -b 30M -R -t 780 -i 1 --json > "$LOGS/gate-a-dl.json" &
sleep 5; cat "$LOGS/gate-a-kpm.out"
tail -f "$LOGS/run/jrtc.out" | grep --line-buffered 'GATE_A'
```

**Expected:** after about 5 s, `intact-kpm-log: first report logged (1 UE, ... Mbit/s)`. Then 20 lines `GATE_A trial n/20: cmd=(30, 70) obs=(30, 70) match=True latency=...ms`, with the two orderings in an irregular sequence, followed by `GATE_A done: 20 trials, no further SETs`.

At about 30 s per trial this takes about 11 minutes. The traffic runs for 13 minutes (`-t 780`), so the last trial's 30 s observation window is complete before it stops. The first version used 660 s, which ended inside trial 20's window and made that trial unusable.

After `GATE_A done` appears, wait 30 s more, then press `Ctrl-C`. This stops only the `tail`; the loggers and iperf3 keep running until B.3. Stay in the same terminal for B.3. (With a v1-patched app, which never says `GATE_A done`, do the same 30 s after `trial 20/20`. B.3 unloads the app.)

### B.3 Evaluate — \[WSL\]

```bash
source ~/intact/env.sh
(cd "$JRTC_APPS" && ./unload.sh -y slice_mgmt/deployment_baremetal.yaml >/dev/null 2>&1; echo "slice_mgmt app unloaded")
echo "waiting for iperf3 to finish ..."; while pgrep -f 'iperf3 -c 10.45.0.1' >/dev/null; do sleep 2; done
pkill -f intact-kpm-log; sudo pkill -f 'ping -D -i 0.2 10.45.0.1'
mkdir -p "$CFG/evidence" && sudo cp /tmp/gate_a.csv "$CFG/evidence/gate_a.csv" && sudo chown "$USER" "$CFG/evidence/gate_a.csv"
cp "$LOGS"/gate-a-dl.json "$LOGS"/gate-a-kpm.jsonl "$LOGS"/gate-a-ping.txt "$LOGS"/run/jrtc.out "$CFG/evidence/"
cd "$CFG/evidence"
awk -F, 'NR>1{n++; m+=$11} END{print "trials:", n, " matches:", m}' gate_a.csv
awk -F, 'NR>1{print $5"/"$6}' gate_a.csv | sort | uniq -c
awk -F, 'NR>1{printf "%s ", $5} END{print ""}' gate_a.csv
awk -F, 'NR>1{s+=$4; if($4>mx)mx=$4} END{printf "readback latency ms (app: SET sent -> INDICATION processed): mean %.1f  max %.1f\n", s/(NR-1), mx}' gate_a.csv
intact-gate-a-effect
```

**Gate A passes** when all of the following hold:

- `matches: 20` of `trials: 20`;
- both `30/70` and `70/30` appear (random draws, so 7/13 is as valid as 10/10);
- the third line (the slice-0 command sequence) is not a strict alternation;
- `intact-gate-a-effect` prints `GATE A throughput criterion (gNB side): PASS`, meaning the gNB-measured throughput is ordered by the commanded cap.

`intact-prb-evidence` is no longer part of the criterion. It labels iperf3 seconds with wall-clock SET times, and B.4 shows why that mixes two clocks.

The `readback latency` line is measured inside the app, from sending the SET to processing the confirming indication. With the stock app's 0.25 s loop sleep it is dominated by that sleep (B.4), so it is not the platform's control latency.

**Undo the patch:** `cp "$JRTC_APPS/slice_mgmt/slice_mgmt_app.py.pre-gate-a" "$JRTC_APPS/slice_mgmt/slice_mgmt_app.py"`, or set `gate_a_enabled = False` in the params file to keep the code but restore the stock behaviour.

### B.4 How fast does a cap change take effect? Reading `intact-gate-a-effect` — \[SCRIPT output, last command of B.3\]

**The question.** A controller that decides every 0.5 s (PACT, Appendix D) needs to know two delays:

- how long after a SET the MAC scheduler actually serves at the new cap, measured **at the gNB**;
- how much later the UE application sees it, measured **at the UE**.

**What we learned from your first run (29 Oct).**

- **Control path: correct.** All 20 SETs were confirmed (20/20). In every valid changed trial the gNB-measured throughput moved in the commanded direction (about 9.7 ↔ 22.1 Mbit/s) within 0.3–0.8 s, that is, in the first or second scheduler report after the SET.
- **The `latency ms` of about 250 ms is not the control path.** The stock `slice_mgmt` app runs its loop with `sleep_timeout_secs = 0.25`, so the confirming indication waits up to 0.25 s in the app's queue before it is processed. The PACT bridge (Appendix D) uses a 5 ms loop; D.5 measures its real readback time.
- **The UE side was mis-timed, not slow.** WSL steps its **wall clock** back by about 2 s roughly every 30 s; the **monotonic** clock is never stepped. Your KPM log shows 23 such steps.
  - The SET times (app), the KPM arrival times and the ping times were wall-clock stamps.
  - iperf3 counts its seconds on the monotonic clock from a single wall-clock start time.
  - The two timelines therefore slid apart by about 2 s per trial. That explains why the "UE lag" grew 3 → 8 → 10 → 18 s, then jumped back, and why later trials showed the UE level moving against the command (trials 11–19). It is also why `intact-prb-evidence` grouped the 30 % and 70 % seconds wrongly, and most likely the growing lag already seen in Part 10.
- **What remains at the UE is queueing.** The ping RTT was steady at about 2.5 s while the cap was 70 % and about 5.7 s while it was 30 %. Those values equal the full DL buffer (`dl_bs` ≈ 6.1 MB) divided by the served rate (6.1 MB × 8 / 19 Mbit/s ≈ 2.6 s; / 8.6 Mbit/s ≈ 5.7 s). Everything the UE receives is therefore seconds old while 30 Mbit/s is offered into a smaller pipe. That is a property of overload, not of the control path.
- **Trial 20 was invalid.** The 660 s iperf3 run ended inside its observation window (fixed: 780 s).

**What `intact-gate-a-effect` (version 2) prints:**

- the clock it used: monotonic stamps when the KPM log and `gate_a.csv` have them; otherwise the wall clock with the steps detected in the KPM log removed. It also prints how many wall-clock steps it saw.
- one row per trial:
  - the cap change;
  - the gNB levels before and after, and the gNB lag;
  - `rep#`, the number of gNB reports until the change was visible;
  - the UE levels before and after, and the UE lag;
  - the median ping RTT;
  - a **status**: `ok`, `no change` (same cap again, a valid command/readback trial with no step to time), or `INVALID: ...` (no traffic in a window, or the level moved against the command; such a trial is never given a lag).
- throughput by commanded cap, using settled samples only, on both sides;
- the lag summaries, then the **Gate A throughput criterion**, **CONTROL TIMING** and **UE SIDE** conclusions.

**Re-analyse your existing run first.** No new experiment is needed. Paste the B.0 block again (it installs version 2 of the helpers; the config step only reconfirms 500 ms), then:

```bash
intact-gate-a-effect
intact-clock-check 120
```

**Expected:**

- `GATE A throughput criterion (gNB side): PASS`.
- CONTROL TIMING: the change visible by the 2nd gNB report in all valid trials.
- UE SIDE: a lag of a few seconds that no longer grows (trend near 0 s/min), next to a ping RTT of the same size.
- `intact-clock-check` lists the wall-clock steps it sees in 2 minutes (expect a few steps of about −2 s) and ends with `RESULT: wall clock is stepped ...`.

| Result | Meaning | Next |
| --- | --- | --- |
| gNB criterion PASS, change visible by the 2nd report | the control path works and its effect is observable one to two 0.5 s decisions later; PACT's 2-epoch lease spans that | Gate A is complete; go to Appendix D |
| UE lag a few seconds, steady, ≈ ping RTT | queueing in the full DL buffer under overload | nothing to fix. PACT measures at the gNB, and its offered loads mostly stay below capacity |
| UE lag still growing after version 2 | an effect not explained by the clock | send `gate-a-effect.csv`, `gate-a-kpm.jsonl`, `gate-a-ping.txt`, `gate_a.csv` from `~/intact/configs/evidence` |
| gNB criterion FAIL, or gNB lag of several seconds | the cap is applied late | stop and send the same files plus `jrtc.out` and `gnb.out` |

**What this experiment does not show.** SETs were 30 s apart, so it does not test writes back-to-back every 0.5 s. Appendix D's `pact-ran sweep` (phase C) and the live runs (`readback errors`, `stale`) cover that.

**Should you rerun Gate A?** Only if you want the UE-side numbers on monotonic stamps rather than reconstructed ones. In that case run B.1 (it upgrades the patched app to v2), then B.2 and B.3 as written.

**About the baseline (uncapped throughput):** Appendix D.6 (`pact-ran sweep`) measures it properly, as the saturated gNB-side throughput at cap 100 % together with the whole cap-to-throughput curve.

## Appendix C — Other parameters: what is controllable today

Only one **runtime control** path ships in jrtc-apps: the `slice_mgmt` codelet on the `mac_sched_slice_mgmt` hook. Every other codelet in `codelets/` (`fapi_*`, `mac`, `rlc`, `pdcp`, `rrc`, `ngap`, `ue_contexts`, `perf`, `xran_packets`) is telemetry. That one codelet already covers two of your INTACT knob types, because each slice carries both a floor and a ceiling.

| INTACT knob | srsRAN mechanism | Runtime via jbpf today? | How |
| --- | --- | --- | --- |
| PRB quota (guaranteed share) | slice `min_prb_policy_ratio` | **Yes** | `SLICE_ALLOC_SET`, field 4 of `slice_t` |
| PRB cap (ceiling) | slice `max_prb_policy_ratio` | **Yes** (used in Part 10) | `SLICE_ALLOC_SET`, field 5 |
| Scheduler weight | slice `priority` (0–254) | **Yes** | `SLICE_ALLOC_SET`, field 6 |
| MCS cap | `cell_cfg.pdsch.max_ue_mcs`, `cell_cfg.pusch.max_ue_mcs` | No | gNB YAML, takes effect on restart |
| TX power / gain | `ru_sdr.tx_gain` | No, and not meaningful over ZMQ | Over ZMQ there is no radio channel; gain does not change link quality the way it does over the air |

To drive the three slice fields to arbitrary values (not just 30/70), change `slice_prb_percentage_low/high` in the params file, or set the three fields directly where the patched app builds `s0` and `s1`; the request message already carries all of them.

Adding live MCS or power control means adding a new control hook in the srsRAN scheduler (C++), rebuilding the gNB, and writing a matching codelet and app. That is a development task, not configuration. To see exactly which hooks this srsRAN build exposes:

```bash
source ~/intact/env.sh
grep -rhoE 'DECLARE_JBPF_HOOK\(\s*[a-z_0-9]+' "$SRSRAN_DIR" --include='*.h' | sed -E 's/.*\(\s*//' | sort -u
ls "$JRTC_ROOT/docs/"   # srsran_hooks.md and codelets.md describe them
```

For offline experiments in the meantime, an MCS cap can be applied per run through the YAML (restart between conditions), which is enough to measure its effect but not to arbitrate it live.

## Appendix D — PACT on this testbed (srsRAN + Open5GS + jbpf), MILD included

This appendix runs your PACT method (the paper's W3R-V arbiter) as a live controller of this RAN: tenant and host xApps ask to change slice PRB settings, PACT decides every 0.5 s who holds authority over which knob (outer loop, leases of 2 epochs) and what value is actually written (inner loop: value synthesis plus the safety mediator), MILD supplies the failure probabilities that drive urgency and the safety gate, and C3 keeps tenant floors. Everything PACT decides is written into the gNB's MAC scheduler through jbpf and read back. Nothing in PACT's mediation code is changed.

The code ships as one bundle, **pact\_ran\_v2.tar.gz** (attached to the chat message of 29 October; it replaces v1). The bundle holds the code only; every command is in this appendix. Version 2 takes every time stamp from the monotonic clock and ends each decision epoch at the next gNB scheduler report. B.4 explains both changes.

### D.0 E2 and the near-RT RIC: how PACT runs on this testbed

**Since 30 Oct:** Appendix F adds a real E2 interface (FlexRIC nearRT-RIC with E2SM-KPM) for monitoring. PACT, as described in this appendix, does not use it. PACT still controls through jbpf and reads its KPIs from the gNB's websocket, and both paths can run at the same time.

**Your reading is correct.** PACT's paper places the arbiter in a near-RT RIC that talks to the gNB over E2 (E2SM-KPM reports in, E2SM-RC control out). Janus has neither. jbpf runs small eBPF programs (codelets) **inside** the gNB, and jrt-controller hosts the apps that exchange messages with those codelets over shared memory. There is no E2AP, no E2 service model and no RIC platform on this path.

**Why that does not stop PACT.** PACT is a conflict-mediation function. It sits between the xApps and the RAN and needs exactly three things, none of which is specific to E2:

1. a KPI report every epoch (0.5 s),
2. a way to write a control value and confirm that the RAN applied it,
3. xApps that propose writes as black boxes.

The PACT repository already isolates the RAN behind one interface, `intact/ran/base.py`, whose docstring says that moving to srsRAN means writing one new file while "the mediation code does not change by a single line". This appendix does exactly that.

| O-RAN element assumed by the paper | What plays that role here | Code |
| --- | --- | --- |
| E2 node with an E2 agent | srsRAN gNB with the jbpf agent; the `slice_mgmt` codelet at hook `mac_sched_slice_mgmt` | already loaded in Part 10 |
| E2SM-RC control: slice-level PRB quota (3GPP RRMPolicyRatio min/max) | `SLICE_ALLOC_SET`: `min_prb_policy_ratio`, `max_prb_policy_ratio`, `priority` per slice (same 3GPP semantics) | codelet `slice_mgmt.cpp` |
| RIC control acknowledgement / readback | `slice_mgmt_ind`: the configuration the MAC scheduler is using, with SFN/slot | codelet |
| E2SM-KPM report | the gNB's own scheduler metrics (per-UE `dl_brate`, `dl_bs`, MCS, CQI, HARQ), pushed every 500 ms on the remote-control websocket | `pact_ran/kpm.py` |
| E2 termination inside the near-RT RIC | **`pact_e2t`**: a jrt-controller Python app that relays between the codelet and a UDP socket on 127.0.0.1 | `jrtc/pact_e2t/pact_e2t_app.py` |
| near-RT RIC platform, xApps, conflict mitigation | **`pact-ran`**: one Python process that hosts the xApps, MILD and PACT (outer loop, inner loop, C3) | `pact_ran/` + your PACT repo |
| A1 policy from the non-RT RIC (tenant envelopes) | the scenario file | `pact_ran/scenario.py` |
| RAN backend in the PACT repo (`AnalyticRAN`) | `LiveBackend` (real gNB) and `TestbedTwin` (digital twin of this gNB) | `pact_ran/live.py`, `pact_ran/twin.py` |

The only coupling to your repository is `pact_ran/glue.py`, which rebinds two names in `intact/w_revised/core.py` (`AnalyticRAN` and `build_apps`). Everything else (`select3`/`scores3` of W3R-V, `World.advance` with the QACM-contract value synthesis, `InnerLoop.decide`, `compute_weights` with the MILD urgency `u(p̂) = 1 + λu·p̂/(1 − p̂ + ε0)`, the C3 deficit Λ, `MildMoE` with the lead-time focal loss and isotonic calibration, `PairedEffects`, `calibrate`, `calibrate_req`, `w3_reliability`) is imported from the repo and runs unchanged.

**Why the bridge is a separate process and not PACT inside jrt-controller.** jrt-controller runs Python apps in sub-interpreters, in which numpy and scikit-learn are not safe to import. The bridge therefore uses only the standard library; PACT runs in its own virtual environment. This is also the O-RAN layering: the E2 termination is thin, and the logic sits above it.

**What is real and what uses the twin.** Paired-twin estimation of α/β/γ needs the same plant state replayed under several authority sets, which a live gNB cannot do. PACT's paper does this on a simulator; here it is done on a twin built from measurements of this gNB (D.6).

| Runs on the real gNB | Runs on the twin |
| --- | --- |
| every PACT decision, every xApp proposal, every write and its readback, all KPIs and every reported result | the offline calibration only: sensitivity sweep, paired-twin α/β/γ, the request-aware W3R-V model, and MILD's first training |
| MILD inference every epoch, and MILD recalibration or fine-tuning on held-control labels measured on the real gNB (D.8) | a pre-flight comparison of all methods (D.7) |

**The alternative, for later.** The srsRAN gNB in this build also contains srsRAN's native E2 agent (`e2: enable_du_e2`, E2SM-KPM and E2SM-RC). Connecting it to a near-RT RIC such as FlexRIC would give the standard interface. PACT would then need one more backend file speaking E2 instead of UDP; the mediation code would stay the same. It is not needed for the results here and has not been validated on this machine.

**Timing.** The near-RT RIC class is 10 ms to 1 s. PACT decides **once per gNB scheduler report**. The gNB pushes a report every 500 ms nominal (B.0); about 0.56 s is measured under ZMQ, because the radio's slot clock runs a little slower than the machine's clock. Each epoch ends when the next report arrives, so the decisions never drift against the KPIs. Gate A (B.4) showed that a cap change is visible at the gNB within 1–2 reports; D.5 measures the bridge's readback time. All times are taken on the monotonic clock, because WSL steps its wall clock (B.4).

### D.1 The scenario (PACT-R1): one UE, two tenants and the host

There is one UE (on slice SD1) and one control codelet. The scenario is designed so that PACT's three ingredients all matter: a C1 conflict on a shared knob, a cross-tenant coupling, and failures that can be seen coming.

| Object | Definition |
| --- | --- |
| Knob `cap0` | SD1 `max_prb_policy_ratio`, 10–100 %, step 5 (regulative) |
| Knob `res1` | SD2 `min_prb_policy_ratio` = T2's guaranteed share, 0–40 %, step 5 (allocative, envelope 40) |
| Host isolation rule (coupling) | what is written for SD1 is `min(cap0, 100 − res1)`: PRBs promised to T2 are not lent to T1 |
| Tenant T1 (ω 1.2, floor 0.85) | the UE's slice. Offered DL load follows a fixed schedule: levels 1.0, 0.7, 1.45, 1.1 × (half the measured capacity at 100 %), 20 s each, 6 s ramps |
| Tenant T2 (ω 1.0, floor 0.85) | a reserved-capacity tenant with no UE (emulated). Planned demand 10, 30, 10, 20 % of PRBs, 30 s each, 4 s ramps |
| Host H (ω 1.0, floor 0.65) | wants fewer PRBs in use (an energy/PRB budget) |
| i1 (T1, π 1.0) | delivery ratio = gNB `dl_brate` / offered load ≥ 0.95 |
| i2 (T1, π 0.6) | queueing delay = `dl_bs`·8 / `dl_brate` ≤ 150 ms |
| i3 (T2, π 0.8) | reservation coverage `res1` / demand ≥ 1.0 |
| i4 (H, π 0.5) | PRB utilisation ≤ 60 % (from `dl_brate` through the measured cap curve) |
| Claim j1 (xApp `x_T1_dlv`) | T1's xApp raises `cap0` whenever i1 or i2 is failing |
| Claim j2 (xApp `x_H_eng`) | PACT's own `EnergyXApp`, unchanged: pulls `cap0` toward 40 % |
| Claim j3 (xApp `x_T2_res`) | sets `res1` to cover T2's current demand |
| C1 | j1 and j2 both write `cap0`: they can never hold authority together |
| Coupled pairs (γ) | (j1, j3) and (j2, j3), through the isolation rule |

PACT's hyper-parameters are the paper's: λu 0.1, ε0 0.01, θΛ 0.3, Λmax 10, τ\_risk 0.65, δ 0.02, ε\_i 0.02, epoch 0.5 s, lease 2 epochs, MILD horizon 6 epochs (3 s), slew limit 25 % of the knob range per write (50 % for `res1`). The margins are PACT's `g_i = d_i(KPI − T_i)/|T_i|`, clipped to ±2.

Two honest limits are worth stating in the paper. First, with one UE, T2's tenant is emulated: its KPI is the reservation that is really written into, and read back from, the gNB, measured against a planned demand. Second, the downlink traffic comes from a schedule (`tools/pact_traffic.py`), which is the testbed counterpart of PACT's forecastable load profile.

### D.2 Before you start

**Go/no-go:** Gate A has passed: B.3 shows 20/20 readback matches, and `intact-gate-a-effect` prints `GATE A throughput criterion (gNB side): PASS`. B.4 also shows that a cap change is visible at the gNB within 1–2 scheduler reports. B.0 has set the gNB report period to 500 ms (about 0.56 s measured); `pact-ran check` verifies this.

**Time and space:** about 1.5 GB for the Python environment.

| Step | Time | Stack needed? |
| --- | --- | --- |
| D.3 install | 5–10 min (downloads) | no |
| D.4 self-test | 5 min | no |
| D.5 bridge and probe | 3 min | yes |
| D.6 plant sweep | 5 min | yes |
| D.7 calibration + twin pre-flight | 3–5 min | no |
| D.8 MILD on real data | 32 min | yes |
| D.9 one live run / full campaign | about 7 min / about 2 h | yes |

### D.3 Install — \[MANUAL\] two downloads, then \[SCRIPT\]

**\[MANUAL, Windows\]** Save `pact_ran_v2.tar.gz` (from the chat) and your `PACT_repo.zip` into your Windows **Downloads** folder. A browser may rename a repeat download to `pact_ran_v2 (1).tar.gz`; the commands below always take the newest one.

**\[WSL\]** Copy both files into Linux, unpack the bundle, and run its installer:

```bash
source ~/intact/env.sh
mkdir -p ~/intact/pact && cd ~/intact/pact
F=$(ls -t /mnt/c/Users/*/Downloads/pact_ran_v2*.tar.gz | head -1); echo "bundle: $F"
cp "$F" ~/intact/pact/pact_ran_v2.tar.gz
Z=$(ls -t /mnt/c/Users/*/Downloads/PACT_repo*.zip | head -1); echo "PACT repo: $Z"
cp "$Z" ~/intact/pact/PACT_repo.zip
sha256sum pact_ran_v2.tar.gz
rm -rf ~/intact/pact/pact_ran            # an older bundle's code only; the venv and the data folders are kept
tar xzf pact_ran_v2.tar.gz
bash ~/intact/pact/pact_ran/install.sh
```

**What the installer does** \[SCRIPT\]:

1. It checks `env.sh`, jrtc-apps, the built codelet types (`$JBPF_CODELETS/slice_mgmt/slice_mgmt.py`) and Part 10's deployment file.
2. It unpacks your repository to `~/intact/pact/PACT_repo`.
3. It creates the venv `~/intact/pact/venv` with your repository's `requirements.txt`, installing `python3-venv` with apt if needed.
4. It copies the bridge into `$JRTC_APPS/pact_e2t/` and writes `deployment_baremetal.yaml` there, taking the ports from Part 10's file.
5. It installs the command `~/intact/bin/pact-ran`.
6. It runs `pact-ran check`.

**Expected:** the `sha256sum` line matches the checksum given with the bundle in the chat. The installer then prints `[ OK ]` for every step, a `pact-ran check` section ending in `CHECK PASSED`, and finally `INSTALL DONE`. Two check lines read `[INFO]` (no plant profile or calibration yet), which is correct at this point.

**If you installed v1 before:** this replaces its code; the Python venv, the PACT repository and your data folders are kept. If the `pact_e2t` app is loaded, unload it and load it again (D.5) so that jrt-controller runs the new bridge. Repeat D.6 and D.7 after installing v2: the calibration now stores the measured report period.

**If not:**

| Message | Fix |
| --- | --- |
| `[FAIL] ... slice_mgmt.py missing` | the codelet was not built; redo Part 10's codelet build |
| `[FAIL] bundle is in ...` | the archive was unpacked somewhere else; repeat the block exactly (it must end up at `~/intact/pact/pact_ran`) |
| `[FAIL] pip install (network?)` | rerun `install.sh` (it resumes); check `curl -sI https://pypi.org` |
| `[FAIL] ... du_report_period 500` in the check | B.0 was not done; do it, then rerun `pact-ran check` |

### D.4 Self-test, offline — \[SCRIPT\]

This proves the whole chain on your laptop without touching the RAN.

- **Part 1** calibrates PACT on the twin and runs two methods.
- **Part 2** starts a fake gNB (it receives the real UDP traffic and serves srsRAN-format metrics on a websocket) and the **real** bridge app under a stand-in jrt-controller runtime. It then runs probe, sweep, calibrate, mild-collect, mild-adapt, run and report through the same code paths as the live testbed, on ports that do not collide with the real stack.

```bash
pact-ran selftest
```

**Expected (about 5 min):** `PROBE PASSED`, four `[ OK ]` sweep verdict lines, `CALIBRATION DONE`, `MILD-COLLECT DONE`, `MILD-ADAPT DONE`, two `RUN DONE` lines, a table, `live-path checks: epochs 60, readback mismatches 0, stale epochs 0`, then `SELFTEST PASSED`.

With so little data, the MILD evidence lines in the self-test may say `gate fail`. That is expected; only the plumbing is being tested. Delete the scratch folder it names (`rm -rf /tmp/pact-selftest-...`).

**If not:** send the last 30 lines.

### D.5 Start the stack with the bridge instead of slice\_mgmt, then probe

**Rule:** load either `slice_mgmt/` (Parts 10 and B) **or** `pact_e2t/` (this appendix), never both. Both drive the same codelet, and the bridge keeps the deployment name `slice_mgmt` on purpose, because jrtc-ctl derives the codelet's stream ids from it.

**\[MANUAL\]** Start the stack as you did for Part 10 and Gate A (your separate windows: jrt-controller, gNB, proxy, UE; or `intact-up`). Wait for the UE's IP address. You do **not** need an iperf3 server for this appendix, because `pact-ran` brings its own traffic.

**\[WSL\] Terminal T6:**

```bash
source ~/intact/env.sh
cd "$JRTC_APPS"
./unload.sh -y slice_mgmt/deployment_baremetal.yaml >/dev/null 2>&1 || true
./unload.sh -y pact_e2t/deployment_baremetal.yaml  >/dev/null 2>&1 || true
./load.sh -y pact_e2t/deployment_baremetal.yaml
sleep 3; grep 'pact_e2t' "$LOGS/run/jrtc.out" | tail -3
sudo -v
pact-ran probe
```

**Expected:**

- `load.sh` prints the same success lines as in 10.4: `code=200` and `statusCode=201`.
- `jrtc.out` shows `pact_e2t: started: commands on udp 127.0.0.1:47000 ...`, then `SLICE_ALLOC_GET sent` and `INDICATION #1 ... [{'sst': 1, 'sd': 1, ...}, {'sst': 1, 'sd': 2, ...}]`.
- `pact-ran probe` prints:
  - `UDP ping to bridge: 10/10`,
  - the two slices,
  - five `SET SD1 max=... : ack ok, readback N ms` lines (expect tens of ms),
  - `report period 0.56 s` (0.5 s nominal; anything from 0.5 to 0.6 s is normal under ZMQ),
  - `PROBE PASSED`.

`sudo -v` caches your password, so `pact-ran` can start its UE-side receiver (`ip netns exec ue1`); without it the runs still work, with a note.

**If not:**

| Symptom | Fix |
| --- | --- |
| `FAIL: no heartbeat from the pact_e2t bridge` | the app is not running: look in `jrtc.out` for a Python traceback after `pact_e2t`; `400 Bad Request` from load.sh = already loaded (unload, load again) |
| bridge alive but `no INDICATION` | the codelet did not load: gnb.out must show `Codeletset is loaded OK`; also make sure slice\_mgmt/ is unloaded |
| `no scheduler metrics from ws://127.0.0.1:8001` | gNB without metrics JSON/remote control (Part 9b), or no UE attached |
| `report period 1.0 s` | B.0 not applied to the running config: `intact-down`, `intact-kpm-period`, start again |

### D.6 Measure the plant (the twin is built from this) — \[SCRIPT\]

```bash
pact-ran sweep
```

It runs three phases, about 5 minutes, with its own UDP traffic to the UE. It never needs iperf3.

| Phase | What it does | What it measures |
| --- | --- | --- |
| A | light load (4 and 8 Mbit/s) at cap 100 % | MAC rate / application rate (`mac_overhead`), queueing-delay floor, gNB report period |
| B | load above capacity; SD1 cap stepped 100 → 10 % in 5 % steps, 6 s each | the saturated **gNB-side** throughput at every cap (the "cap curve"; its 100 % point is the proper baseline you asked about), the largest DL buffer, the report-to-report noise |
| C | cap 30 % ↔ 70 % every 6 s | SET → INDICATION readback latency, and SET → first gNB report past half-way (the gNB-side effect lag) |

**Expected:** each line shows a cap and its throughput, decreasing roughly linearly from about 27–29 Mbit/s at 100 % to about 3–5 Mbit/s at 10 %. Then come four verdict lines:

- `[ OK ] gNB report period 0.56 s` (or any value from 0.5 to 0.6 s)
- `[ OK ] cap curve monotone ...`
- `[ OK ] readback ... median N ms`
- `[ OK ] gNB-side effect lag median ≤ 1 s ...`

The run ends with `SWEEP DONE: ~/intact/pact/data/testbed_profile.json`. The file and `data/logs/sweep/sweep_reports.csv` (every raw report) are the plant evidence for the paper.

**If** the effect-lag line is a `[WARN]` with a lag of several seconds, the scheduler side is slow on this machine. Either use longer epochs everywhere below (add `--epoch-s 1.0` to `calibrate`; the runs inherit it) or send me `sweep_reports.csv`.

### D.7 Calibrate PACT on the twin, then a pre-flight on the twin — \[SCRIPT\]

The stack may be up or down for this step.

```bash
pact-ran calibrate --fresh
pact-ran simulate
pact-ran report
```

`calibrate` builds the scenario from your measured plant, and then runs PACT's own calibration on the twin.

- **`C.calibrate`** (24 episodes × 160 epochs; each episode starts at a different point of the load cycle) does three things:
  - the sensitivity sweep `s_{p,i}` per load regime;
  - MILD training on held-control time-to-failure labels (cloned plant, horizon 6 epochs), with lead-time focal loss and isotonic calibration;
  - paired-twin α/β/γ (ExtraTrees).
- **`w3_reliability`** gives κ\_i and σ\_i.
- **`calibrate_req`** builds the request-aware W3R-V model.

The output ends with:

- one MILD line per intent: safe samples, future failures, PR-AUC, base rate, lift, and PACT's evidence gate PASS/fail;
- the β and γ RMSE against their mean magnitudes;
- κ and σ.

`i4` showing `0 future failures ... gate fail` is normal. Under held controls the PRB use cannot rise above the cap, so the energy intent only fails after someone raises the cap. MILD then reports p ≈ 0 for i4, and urgency stays at 1 for it.

`simulate` runs every method 3 times (different noise) for 720 epochs on the twin with the same load schedule as the live runs:

- **`W3R-V`**: PACT.
- **`W3R-V-noMILD`**: MILD replaced by PACT's reactive risk proxy, both in the urgency weight and in the safety gate.
- **`W3R-V-nourgency`**: u ≡ 1.
- **`W3R-V-noC3`**: θΛ = 0.
- **`B3-contract`** and **`QACM-contract`**: PACT's baselines.
- **`uncoordinated`**: no mediator; every xApp writes whenever it wants, in random order, and the last writer wins.
- **`all-reject`**: controls frozen.

**What the twin predicted with the placeholder plant (Part 10 numbers)**, 3 seeds × 720 epochs:

| Method | WIF |
| --- | --- |
| W3R-V | 0.913 |
| W3R-V-noMILD | 0.883 |
| W3R-V-nourgency | 0.913 |
| W3R-V-noC3 | 0.915 |
| B3-contract | 0.919 |
| QACM-contract | 0.919 |
| uncoordinated | 0.662 |
| all-reject | 0.697 |

Read this as follows. MILD is worth about 3 points of WIF to PACT here. Any mediation is worth more than 20 points over none. In this regime PACT ties the strongest baselines rather than beating them, because "hold the cap near 60 % and only move at the peaks" is already close to optimal.

Your measured plant will move these numbers. If you want a regime in which the arbitration matters more (for the paper's declared scope), explore it here first, because twin runs take seconds. Every scenario value can be overridden, for example:

```bash
pact-ran calibrate --fresh --set 'ran.load_profiles.T1.levels=[1.0,0.7,1.6,1.1]' --set intents.3.target=50
```

The overrides are stored with the calibration, and later steps reuse them.

### D.8 MILD on the real gNB — \[SCRIPT\], stack up, bridge loaded

PACT trains MILD on **held-control** labels: will intent *i* fail within 3 s if nobody touches the controls? On the twin, a cloned plant answers that question. On the real gNB the same label is obtained by actually holding. Each probe cycle is 4 epochs with a random authority set (as in PACT's risk episodes), then a recorded state followed by 6 epochs with no writes; the first epoch at which a margin goes negative is the label.

```bash
sudo -v
pact-ran mild-collect --minutes 30
pact-ran mild-adapt
```

**`mild-collect`** prints progress every 12 probes, with failures per intent so far. It runs 30 minutes, about 360 probes, and writes `data/mild/probes_<time>.jsonl`. You can run it twice; `mild-adapt` uses every file.

**`mild-adapt`** splits the probes into interleaved time blocks:

- fine-tune 40 %,
- isotonic calibration 20 %,
- selection 20 %,
- test 20 %.

It then compares three versions of MILD on the real **selection** split, and keeps the one with the lowest Brier score:

- **twin**: as trained on the twin;
- **twin + real isotonic**: the twin network with its probability layer refit on real data;
- **fine-tuned + real isotonic**: the MoE weights trained further on real labels, at a small learning rate, with early stopping.

PACT's evidence gate is reported on the real **test** split for every version. The kept version is saved as `data/mild/mild_real.pkl` with `mild_real_evidence.json`; from then on, `run`, `campaign` and `simulate` use it automatically. `--mild twin` forces the twin version.

**Expected:** typically the gate shows PASS for i1 and i2, the intents whose failures follow the load ramps. A gate `fail` for i4 has the same reason as on the twin.

**If** most intents show `n/a`, too few failures happened during collection. Collect another 30 minutes before adapting.

### D.9 Live PACT runs — \[SCRIPT\], stack up, bridge loaded

One run of PACT first:

```bash
sudo -v
pact-ran run --method W3R-V --tag r1
```

Each run does the following:

1. It drains the DL buffer (no traffic, cap 100 %).
2. It starts the run at the next gNB report. From then on, each epoch ends when the next report arrives (about every 0.56 s), so every decision sees exactly one fresh report. All times use the monotonic clock.
3. It starts the T1 traffic schedule and creates PACT's `World` on the live backend.
4. It runs 720 epochs (about 6.7 min at 0.56 s per report). Every 20 epochs it prints the lease, `cap0`, `res1`, the margins g, the MILD probabilities p, and the gNB throughput and buffer.
5. It restores both slices to 0/100 at the end, or on Ctrl-C. One Ctrl-C finishes the current epoch and saves; a second Ctrl-C aborts hard.

**Expected end line:**

`WIF ... intents [...] outcomes {'admit': .., 'override': .., 'reject': ..} stale 0 readback errors 0 overruns 0`

- `stale` counts epochs without a fresh gNB report. A few per run are fine.
- `readback errors` must be 0. Each SET has to be read back from the MAC scheduler within 1 s, otherwise it is re-sent and counted.
- `overruns` counts decisions that took longer than an epoch. It should be 0.

Then the campaign: all 8 methods × 2 repetitions, in a randomized order per repetition, so that no method always runs first or last:

```bash
sudo -v
pact-ran campaign --reps 2
pact-ran report
```

The campaign takes about 1 h 50 min. For a shorter first pass, give a method list: `pact-ran campaign --reps 2 --methods W3R-V,W3R-V-noMILD,B3-contract,uncoordinated` (about 55 min). Keep the laptop plugged in and awake (Part 1.2).

### D.10 Reading the results — `pact-ran report`

`pact-ran report` prints one table for LIVE (the real gNB) and one for SIM (the twin), and writes `data/report.csv`.

| Column | Meaning |
| --- | --- |
| WIF | PACT's metric: π-weighted fraction of epochs in which each intent's margin is ≥ 0 (mean ± SD over repetitions) |
| contract | the same, weighted by π·ω |
| i1 … i4 | per-intent fulfilment |
| C1 | same-knob write collisions (0 for every mediated method by construction; large for `uncoordinated`) |
| C3sh | mean shortfall below the tenant floors ρ\_min |
| adm/ovr/rej | the inner loop's dispositions (admit, override = value changed, reject) |
| warned | failure onsets preceded by a MILD alarm (p ≥ τ\_risk 0.65) within 3 s, out of all onsets: MILD's lead time measured live |
| stale, rbErr | data-quality counters (must be near 0) |

Below each table, `paired difference W3R-V minus method` compares runs with the same repetition tag. Positive values mean PACT is better.

For the paper:

- the **LIVE** rows are the result;
- the **SIM** rows show how well the twin predicts the real gNB (the sim-to-real gap);
- `testbed_profile.json`, `cal/calibration_summary.json` and `mild/mild_real_evidence.json` document how the controller was built.

### D.11 Where everything is

| Path | Content |
| --- | --- |
| `~/intact/pact/PACT_repo/` | your repository, unchanged |
| `~/intact/pact/pact_ran/` | the bundle (README.md lists every file) |
| `~/intact/pact/venv/` | Python environment for `pact-ran` |
| `$JRTC_APPS/pact_e2t/` | bridge app, its parameters (`cmd_port` 47000, `ind_port` 47001), deployment file |
| `~/intact/pact/data/testbed_profile.json` | measured plant (D.6) |
| `~/intact/pact/data/cal/` | twin calibration: `pact_cal.pkl`, `scenario.json` (the exact scenario used), `calibration_summary.json`, PACT's episode files |
| `~/intact/pact/data/mild/` | real probes, `mild_real.pkl`, `mild_real_evidence.json` |
| `~/intact/pact/data/runs/live/<method>/<tag>/` | `telemetry.jsonl` (one record per epoch: lease, writes and dispositions, controls, g, p, weights, Λ, KPIs, readback and timing), `result.json`, the gNB report log, the bridge message log, the traffic logs |
| `~/intact/pact/data/runs/sim/<method>/s<seed>/` | the same for twin runs |

### D.12 Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `cannot bind udp 127.0.0.1:47001` | another `pact-ran` is still running: `pkill -f pact_ran.cli` |
| `no gNB scheduler report for N epochs` during a run | gNB stopped or the UE detached: check the gnb and ue windows; restart the stack, reload the bridge (D.5) |
| `the gNB reports no UE` | UE detached; restart the UE window and wait for its IP |
| many `stale` epochs | the report period is far from 0.5–0.6 s (B.0), or the laptop is overloaded; close other programs |
| `WARNING bridge refused SET` | the slice keys in the scenario do not match the gNB: SD1 and SD2 must exist (Part 10.1) |
| `readback errors` > 0 | `jrtc.out` shows why: look for `pact_e2t` errors; set `verbose = True` in `$JRTC_APPS/pact_e2t/pact_e2t_params.py`, unload and load again |
| throughput stuck after a run | `pact-ran restore` (puts both slices back to 0/100) |
| want to go back to Part 10 / Gate A | `cd $JRTC_APPS && ./unload.sh -y pact_e2t/deployment_baremetal.yaml && ./load.sh -y slice_mgmt/deployment_baremetal.yaml` |
| uninstall | `rm -rf ~/intact/pact ~/intact/bin/pact-ran "$JRTC_APPS/pact_e2t"` |

## Appendix E — Three to five UEs at once, through a ZMQ broker

This appendix runs 2 to 5 srsUEs (3 by default) against the same gNB at the same time. **Your current UE is kept exactly as it is:** it becomes UE1 with the same IMSI, key, slice and namespace `ue1`, and the single-UE way of working (Parts 9–10, Appendices B–D) keeps working unchanged. Nothing is removed. Four things are added: GNU Radio from Ubuntu, a broker script, one config file per UE, and one subscriber per extra UE.

**Can UEs be added to the setup you have now, or must all be configured together?** The configuration is added on top of what you have: UE1 stays, UE2…UE5 are new. At run time, however, the number of UEs is fixed for each run. Three facts force this:

1. A ZMQ link carries a continuous sample stream, and every sample is exchanged in lockstep.
2. The broker adds the uplink of all N UEs sample by sample, so it waits for every one of them. If one UE is missing, the radio stops for all of them.
3. The gNB must be restarted whenever its radio peers restart. This is the same rule as with one UE (Part 0).

So you choose N once with `intact-multiue setup N`. Changing N means: stop, run `setup` again, start.

### E.0 How it works

Every ZMQ link is a pair of sockets. The side that **sends** samples binds a REP socket and answers requests. The side that **receives** samples connects a REQ socket and asks for them. The broker sits between the gNB and the UEs:

| Link | Sender (binds REP) | Receiver (connects REQ) | TCP port on 127.0.0.1 |
| --- | --- | --- | --- |
| downlink, gNB → broker | gNB | broker | 2000 |
| uplink sum, broker → gNB | broker | gNB | 2001 |
| downlink copy, broker → UE k | broker | srsUE k | 2k00: 2100, 2200, 2300, 2400, 2500 |
| uplink, UE k → broker | srsUE k | broker | 2k01: 2101, 2201, 2301, 2401, 2501 |

The broker copies every gNB sample to every UE and adds the UEs' uplink samples together, as the air would. It can also attenuate each UE separately ("path loss", 0 dB by default). It is a headless GNU Radio flowgraph with the same design as Microsoft's multi-UE broker in jrtc-apps (`containers/Docker/srs_grc/GRC_multi_ue_headless.py`, already in your checkout) and srsRAN's multi-UE tutorial. It differs in three ways:

- it binds to 127.0.0.1 only;
- its throttle caps the sample rate at real time (slowdown 1, where Microsoft's uses 2);
- it prints every 10 s how many samples per second it moves.

The gNB keeps its ports 2000/2001, so **the gNB config does not change**. Only the UEs use new ports.

**Tested on 30 Oct.** The broker ran with GNU Radio 3.10.9, the version Ubuntu 24.04 installs. Stand-in gNB and UE processes spoke srsRAN's exact ZMQ protocol (a 1-byte request, answered with a block of samples).

- Every UE received the gNB's samples without a gap, scaled by its path loss.
- The gNB received exactly the sum of the UEs' uplinks.
- `intact-multiue up` brought 3 UEs to "attached".
- Ctrl-C stopped every process and removed the UE interfaces.

The real srsRAN binaries were not run in that test (they cannot be built there). Every script therefore checks each step and stops with an exact message.

**First real run (30 Oct):** the broker worked (0.91× real time, all ZMQ links established), but no UE attached: srsUE always sent PRACH preamble 0, so the UEs collided on every attempt. E.3b fixes this in srsUE; `intact-multiue up` now refuses to start without that fix.

**What setup creates by default (3 UEs; 4 and 5 follow the same pattern):**

| UE | IMSI | Slice SD | Namespace | ZMQ RX / TX | UE output | srsUE metrics CSV | iperf3 port |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ue1 | 001010123456780 (unchanged) | 1 | `ue1` | 2100 / 2101 | `$LOGS/run/ue1.out`, `/tmp/ue1.log` | `/tmp/ue_metrics.csv` (the single-UE file, so `intact-telemetry` keeps working) | 5211 |
| ue2 | 001010123456781 | 2 | `ue2` | 2200 / 2201 | `$LOGS/run/ue2.out`, `/tmp/ue2.log` | `/tmp/ue2_metrics.csv` | 5212 |
| ue3 | 001010123456782 | 1 | `ue3` | 2300 / 2301 | `$LOGS/run/ue3.out`, `/tmp/ue3.log` | `/tmp/ue3_metrics.csv` | 5213 |
| ue4, ue5 | …783, …784 | 2, 1 | `ue4`, `ue5` | 2400/2401, 2500/2501 | `ue4.out`, `ue5.out` | `/tmp/ue4_metrics.csv`, `/tmp/ue5_metrics.csv` | 5214, 5215 |

The UEs alternate between the two slices of Part 10, so both tenants have UEs. `--sd 1,1,2` chooses the slices yourself. All UEs share UE1's key and OPc, which is fine for a test network.

**How many UEs can the laptop run?** In ZMQ mode no deadline can be missed: when the CPU cannot keep up, the simulated radio clock simply runs slower than the wall clock. With one UE your stack already runs at about 0.9× real time: the gNB reports every 0.56 s instead of every 0.5 s (B.4).

- Each extra srsUE needs roughly one more CPU core for its baseband processing, and the broker about one more (estimates).
- With 16 CPUs given to WSL, `setup` recommends at most 5 UEs. It computes (CPUs − 4) × 2/3, capped at 5.
- The speed is measured, not assumed. The broker prints it every 10 s, and `intact-multiue status` derives it from the gNB's report period.
- Below about 0.5×, attach takes longer and every throughput measured in wall-clock time drops by the same factor. Use fewer UEs if that happens.

### E.1 What runs where, and when

| Step | Command | State required | Window |
| --- | --- | --- | --- |
| E.2 Install the scripts | paste the block | any; it only writes files | any (T6) |
| E.3 One-time setup | `intact-down`, then `intact-multiue setup 3` | gNB and UEs **down**; core **up** (MongoDB for the subscribers) | T6 |
| E.3b srsUE PRACH fix | paste the block; it runs `intact-down` and `intact-fix-ue-prach` | gNB and UEs **down** (it rebuilds srsue); once | T6 |
| E.4 Start | T2, T3, T4 as usual, then `intact-multiue up` | the gNB **up and fresh**: started, with no UE connected since | T5 replaces `intact-run-ue` |
| E.5 Check and use | `intact-multiue status`, `intact-multiue iperf 5 20` | UEs attached | T6 |
| E.6 Stop | Ctrl-C in T5, or `intact-multiue down`, or `intact-down` | any | any |

The windows with several UEs:

| Window | Command | Ready when you see |
| --- | --- | --- |
| T1 | *(core under systemd)* `intact-core-health` | `CORE READY` |
| T2 | `intact-run-jrtc` | `:3001` listening (as in 8.5) |
| T3 | `intact-run-gnb` (same config as with one UE) | `==== gNB started ===` |
| T4 | `intact-run-proxy` | `:30450` listening |
| T5 | `intact-multiue up` | `ALL 3 UEs ATTACHED.`, then one status line every 30 s |
| T6 | `intact-multiue status`, `intact-multiue iperf 5 20`, `load.sh` … | — |

Without jbpf, skip T2 and T4 and start T3 with `intact-run-gnb $CFG/gnb_zmq.yaml`.

### E.2 Install the scripts — \[SCRIPT\]

The block creates three scripts:

- **`intact-zmq-broker`**: the GNU Radio broker.
- **`intact-multiue`**: setup, start, status, per-UE iperf3, and stop.
- **A new `intact-down`**, the same one now shown in Part 8.4. It also stops the broker, and Appendix F's nearRT-RIC and xApp.

There are two blocks. Paste each one whole, in order; they write files only, and nothing is started.

**Block 1: the broker and the new `intact-down`.**

```bash
source ~/intact/env.sh
mkdir -p ~/intact/bin
cat > ~/intact/bin/intact-zmq-broker <<'BREOF'
#!/usr/bin/env python3
"""intact-zmq-broker: headless GNU Radio broker that connects ONE srsRAN gNB to N srsUEs over ZeroMQ.

Each ZMQ link is a REQ/REP pair: the side that TRANSMITS samples binds a REP socket and answers
requests; the side that RECEIVES samples connects a REQ socket and asks. The broker sits in the middle:

  downlink  gNB TX  tcp://127.0.0.1:2000 (gNB binds)  -> broker REQ source -> [throttle] -> x gain_k
                                                     -> broker REP sink tcp://127.0.0.1:<base+100(k-1)>   -> UE k RX
  uplink    UE k TX tcp://127.0.0.1:<base+100(k-1)+1> (UE binds) -> broker REQ source -> x gain_k -> SUM
                                                     -> broker REP sink tcp://127.0.0.1:2001              -> gNB RX

With the default base 2100: UE1 RX 2100 / TX 2101, UE2 2200 / 2201, ... UE5 2500 / 2501.
The gNB keeps its single-UE ports (2000/2001), so its config does not change.

Run it with the Python that has GNU Radio (Ubuntu: /usr/bin/python3). Ctrl-C stops it.
Every --status-s seconds it prints the sample rate it moves; speed = rate / sample rate, i.e. how fast
simulated radio time runs compared with the wall clock (1.00 = real time).
"""
import argparse, signal, sys, threading, time

from gnuradio import blocks, gr, zeromq


def ports(k, base):
    rx = base + 100 * (k - 1)
    return rx, rx + 1                      # (UE RX port, UE TX port)


class Broker(gr.top_block):
    def __init__(self, a):
        gr.top_block.__init__(self, "intact_zmq_broker", catch_exceptions=True)
        n, ip, cs = a.ues, a.addr, gr.sizeof_gr_complex
        gains = [10 ** (-pl / 20.0) for pl in a.pathloss]
        self.gnb_src = zeromq.req_source(cs, 1, f"tcp://{ip}:{a.gnb_tx}", a.zmq_timeout, False, -1)
        self.gnb_sink = zeromq.rep_sink(cs, 1, f"tcp://{ip}:{a.gnb_rx}", a.zmq_timeout, False, -1)
        self.dl_rate = blocks.probe_rate(cs, 1000.0, 0.15)
        self.ul_rate = blocks.probe_rate(cs, 1000.0, 0.15)
        dl = self.gnb_src
        if a.slowdown > 0:
            self.thr = blocks.throttle(cs, a.srate / a.slowdown, True)
            self.connect(self.gnb_src, self.thr)
            dl = self.thr
        self.connect(dl, self.dl_rate)
        self.add = blocks.add_vcc(1)
        self.blk = []
        for k in range(1, n + 1):
            rx, tx = ports(k, a.base)
            g_dl = blocks.multiply_const_cc(gains[k - 1])
            g_ul = blocks.multiply_const_cc(gains[k - 1])
            ue_sink = zeromq.rep_sink(cs, 1, f"tcp://{ip}:{rx}", a.zmq_timeout, False, -1)
            ue_src = zeromq.req_source(cs, 1, f"tcp://{ip}:{tx}", a.zmq_timeout, False, -1)
            self.connect(dl, g_dl, ue_sink)
            self.connect(ue_src, g_ul, (self.add, k - 1))
            self.blk += [g_dl, g_ul, ue_sink, ue_src]
        self.connect(self.add, self.gnb_sink)
        self.connect(self.add, self.ul_rate)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ues", type=int, required=True, help="number of UEs (1-8)")
    p.add_argument("--addr", default="127.0.0.1")
    p.add_argument("--gnb-tx", type=int, default=2000, help="gNB TX port (gNB binds)")
    p.add_argument("--gnb-rx", type=int, default=2001, help="gNB RX port (broker binds)")
    p.add_argument("--base", type=int, default=2100, help="UE1 RX port; UE k uses base+100(k-1) and +1")
    p.add_argument("--pathloss", default="", help="dB per UE, space separated (default 0 for all)")
    p.add_argument("--srate", type=float, default=11.52e6, help="sample rate of the cell (10 MHz: 11.52e6)")
    p.add_argument("--slowdown", type=float, default=1.0,
                   help="throttle at srate/slowdown; 1 = never faster than real time; 0 = no throttle")
    p.add_argument("--zmq-timeout", type=int, default=100, help="ms")
    p.add_argument("--status-s", type=float, default=10.0)
    p.add_argument("--check", action="store_true", help="build the flowgraph, print the port map, do not run")
    a = p.parse_args()
    if not 1 <= a.ues <= 8:
        sys.exit("--ues must be 1..8")
    a.pathloss = [float(x) for x in a.pathloss.split()] if a.pathloss.strip() else [0.0] * a.ues
    if len(a.pathloss) != a.ues:
        sys.exit(f"--pathloss needs {a.ues} values, got {len(a.pathloss)}")
    print(f"[broker] GNU Radio {gr.version()}  UEs={a.ues}  srate={a.srate/1e6:.2f} Msps  "
          f"throttle={'off' if a.slowdown <= 0 else f'{a.srate/a.slowdown/1e6:.2f} Msps (slowdown {a.slowdown})'}", flush=True)
    print(f"[broker] gNB TX tcp://{a.addr}:{a.gnb_tx} -> broker;  broker -> tcp://{a.addr}:{a.gnb_rx} gNB RX", flush=True)
    for k in range(1, a.ues + 1):
        rx, tx = ports(k, a.base)
        print(f"[broker] UE{k}: broker -> RX tcp://{a.addr}:{rx}   UE TX tcp://{a.addr}:{tx} -> broker   "
              f"path loss {a.pathloss[k-1]:.1f} dB", flush=True)
    tb = Broker(a)
    if a.check:
        print("[broker] CHECK OK: flowgraph built (not started)", flush=True)
        return
    stop = threading.Event()

    def on_sig(*_):
        stop.set()
    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)
    tb.start()
    print("[broker] flowgraph started", flush=True)
    t0 = time.monotonic()
    while not stop.wait(a.status_s):
        dl, ul = tb.dl_rate.rate(), tb.ul_rate.rate()
        print(f"[broker] t={time.monotonic()-t0:7.0f}s  DL {dl/1e6:6.2f} Msps  UL {ul/1e6:6.2f} Msps  "
              f"speed {dl/a.srate:4.2f}x real time" + ("   (no samples: is every UE and the gNB running?)" if dl < 1 else ""),
              flush=True)
    print("[broker] stopping ...", flush=True)
    tb.stop()
    tb.wait()
    print("[broker] stopped", flush=True)


if __name__ == "__main__":
    main()
BREOF
chmod +x ~/intact/bin/intact-zmq-broker

cat > ~/intact/bin/intact-down <<'DOWNEOF'
#!/usr/bin/env bash
# intact-down: stop the UE(s), the multi-UE broker and the gNB TOGETHER (ZMQ rule), then the reverse proxy,
# jrt-controller, the KPM xApp and the nearRT-RIC (Appendix F), and iperf3 servers. The core stays up.
sudo pkill -INT -x srsue 2>/dev/null; sudo pkill -INT -x gnb 2>/dev/null; sleep 3
sudo pkill -KILL -x srsue 2>/dev/null; sudo pkill -KILL -x gnb 2>/dev/null
pkill -INT -f '[i]ntact-zmq-broker --ues' 2>/dev/null; sleep 1; pkill -KILL -f '[i]ntact-zmq-broker --ues' 2>/dev/null
rm -f "${HOME:?}"/intact/logs/run/multiue/*.pid 2>/dev/null
pkill -INT -x xapp_kpm_csv 2>/dev/null; sleep 1; pkill -INT -x nearRT-RIC 2>/dev/null; sleep 1
pkill -KILL -x xapp_kpm_csv 2>/dev/null; pkill -KILL -x nearRT-RIC 2>/dev/null
sudo pkill -f srsran_reverse_proxy 2>/dev/null
sudo pkill -INT -x 'jrtc|jrtc_main' 2>/dev/null; sleep 2; sudo pkill -KILL -x 'jrtc|jrtc_main' 2>/dev/null   # jrtc renames itself jrtc_main
pkill -f 'iperf3 -s' 2>/dev/null
tmux kill-session -t intact 2>/dev/null
if pgrep -ax 'gnb|srsue|jrtc|jrtc_main|nearRT-RIC|xapp_kpm_csv' || pgrep -af '[i]ntact-zmq-broker --ues'; then
  echo "WARNING: still running (above)"
else
  echo "RAN stack stopped (UEs, broker, gNB, proxy, jrt-controller, RIC, xApp); core still running"
fi
DOWNEOF
chmod +x ~/intact/bin/intact-down
ls -la ~/intact/bin/intact-zmq-broker ~/intact/bin/intact-down
```

**Expected:** two lines from `ls -la`.

**Block 2: `intact-multiue`.**

```bash
source ~/intact/env.sh
cat > ~/intact/bin/intact-multiue <<'MUEOF'
#!/usr/bin/env bash
# intact-multiue: run 2-5 srsUEs against ONE gNB through the ZMQ broker (runbook Appendix E).
#
#   intact-multiue setup N [--sd 1,2,1]   one-time: UE configs, subscribers, checks          [stack DOWN]
#   intact-multiue up [--slowdown X] [--pathloss "0 0 0"]
#                                         start the UEs + broker, wait for attach, monitor     [gNB UP and fresh]
#   intact-multiue status                 every UE, the broker, the ZMQ links, gNB per-UE KPIs [any time]
#   intact-multiue iperf [Mbit/s per UE] [seconds] [--ul]
#                                         one iperf3 per UE, all at the same time              [UEs attached]
#   intact-multiue down                   stop the UEs, the broker AND the gNB (together)      [any time]
set -uo pipefail
source ~/intact/env.sh
MU="$CFG/multiue"; RUN="$LOGS/run"; PIDS="$LOGS/run/multiue"; mkdir -p "$MU" "$PIDS"
SRSUE="$SRSUE_DIR/build/srsue/src/srsue"
BROKER="$INTACT/bin/intact-zmq-broker"
O5="${OPEN5GS_ETC:-/etc/open5gs}"
BPAT='[i]ntact-zmq-broker --ues'     # matches the running broker only (the [i] keeps pgrep from matching shells that mention it)
ok(){ echo "  [ OK ] $*"; }; warn(){ echo "  [WARN] $*"; }; fail(){ echo "  [FAIL] $*"; }
die(){ fail "$*"; exit 1; }
rxport(){ echo $((2100 + 100 * ($1 - 1))); }
txport(){ echo $((2101 + 100 * ($1 - 1))); }
listening(){ ss -Hltn "sport = :$1" 2>/dev/null | grep -q .; }
load_env(){ [ -f "$MU/multiue.env" ] || die "not set up yet: run 'intact-multiue setup N' first (stack down)"; source "$MU/multiue.env"; }
ue_ip(){ sudo ip netns exec "ue$1" ip -4 -br addr show tun_srsue 2>/dev/null | awk '{print $3}'; }
pid_alive(){ [ -f "$1" ] && sudo kill -0 "$(cat "$1")" 2>/dev/null; }
PRACH_SRC="$SRSUE_DIR/srsue/src/stack/mac_nr/proc_ra_nr.cc"
prach_fixed(){ grep -q 'INTACT: TS 38.331 RACH-ConfigCommon' "$PRACH_SRC" 2>/dev/null && [ "$SRSUE" -nt "$PRACH_SRC" ]; }   # E.3b
UERR='Registration Reject|registration reject|Received RRC Release|Unhandled Registration|Illegal instruction|Error opening|Error:'

# ------------------------------------------------------------------------------------------- setup
setup(){
  local N="${1:-}"; shift || true
  local SDLIST=""
  while [ $# -gt 0 ]; do case "$1" in --sd) SDLIST="${2:-}"; shift 2 ;; *) die "unknown option $1" ;; esac; done
  [[ "$N" =~ ^[2-8]$ ]] || die "usage: intact-multiue setup N [--sd 1,2,1]   (N = 2..8, 3-5 recommended)"
  echo "=== 0. Pre-checks (the stack must be down)"
  if pgrep -x 'gnb|srsue' >/dev/null || pgrep -f "$BPAT" >/dev/null; then
    pgrep -ax 'gnb|srsue'; die "gNB, srsUE or broker running. Stop them first: intact-down"
  fi
  sudo -v || die "sudo authentication failed"
  [ -x "$SRSUE" ] || die "srsue not built: $SRSUE (Part 6.3)"
  if prach_fixed; then ok "srsUE picks a random PRACH preamble (E.3b fix built in)"; else warn "srsUE always sends PRACH preamble 0: run intact-fix-ue-prach (E.3b) before 'intact-multiue up'"; fi
  [ -f "$CFG/ue_zmq.conf" ] || die "$CFG/ue_zmq.conf missing (Part 7.2)"
  systemctl is-active --quiet mongod || die "MongoDB not running: sudo systemctl start mongod"
  command -v open5gs-dbctl >/dev/null || die "open5gs-dbctl missing (Part 4.6)"
  command -v mongosh >/dev/null || die "mongosh missing: sudo apt install -y mongodb-mongosh"
  local NP MEMA REC
  NP=$(nproc); MEMA=$(awk '/MemAvailable/{printf "%.1f", $2/1048576}' /proc/meminfo)
  REC=$(( (NP - 4) * 2 / 3 )); [ "$REC" -gt 5 ] && REC=5; [ "$REC" -lt 1 ] && REC=1
  ok "WSL has $NP CPUs and ${MEMA} GiB free memory: recommended at most $REC UEs"
  [ "$N" -gt "$REC" ] && warn "N=$N is above that: radio time will run slower than real time (the broker prints the speed)"

  echo "=== 1. GNU Radio (the broker)"
  local GR_PY="" p
  for p in /usr/bin/python3 /usr/bin/python3.12; do
    [ -x "$p" ] && "$p" -c 'from gnuradio import gr, zeromq, blocks' 2>/dev/null && { GR_PY=$p; break; }
  done
  if [ -z "$GR_PY" ]; then
    echo "  installing GNU Radio from Ubuntu (about 600 MB, a few minutes) ..."
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y gnuradio >"$LOGS/apt-gnuradio.log" 2>&1 \
      || die "apt-get install gnuradio failed: tail -20 $LOGS/apt-gnuradio.log"
    for p in /usr/bin/python3 /usr/bin/python3.12; do
      [ -x "$p" ] && "$p" -c 'from gnuradio import gr, zeromq, blocks' 2>/dev/null && { GR_PY=$p; break; }
    done
  fi
  [ -n "$GR_PY" ] || die "GNU Radio is installed but 'from gnuradio import zeromq' fails; send: /usr/bin/python3 -c 'from gnuradio import zeromq'"
  ok "GNU Radio $("$GR_PY" -c 'from gnuradio import gr; print(gr.version())') via $GR_PY"
  [ -x "$BROKER" ] || die "$BROKER missing: paste the E.2 block again"

  echo "=== 2. UE configs in $MU (UE1 keeps your current IMSI and slice)"
  local PLAN
  PLAN=$(python3 - "$CFG/ue_zmq.conf" "$MU" "$N" "$SDLIST" "$GR_PY" <<'PY'
import configparser, os, sys
src, mu, n, sdlist, grpy = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5]
def load():
    c = configparser.ConfigParser(strict=False, interpolation=None); c.optionxform = str
    if not c.read(src): sys.exit(f"STOP: cannot read {src}")
    return c
c = load()
for s in ("rf", "usim", "nas", "gw"):
    if not c.has_section(s): sys.exit(f"STOP: section [{s}] missing in {src}")
dev = dict(x.split("=", 1) for x in c["rf"]["device_args"].replace(" ", "").split(","))
base_srate = dev.get("base_srate", "11.52e6")
slicing = c.has_section("slicing") and c["slicing"].get("enable", "false").strip().lower() == "true"
if slicing:
    sd1 = int(c["slicing"]["nssai-sd"]); other = 2 if sd1 == 1 else 1
    sds = [int(x) for x in sdlist.split(",")] if sdlist else [sd1 if k % 2 == 1 else other for k in range(1, n + 1)]
    if len(sds) != n: sys.exit(f"STOP: --sd needs {n} values, got {len(sds)}")
else:
    if sdlist: sys.exit("STOP: --sd given but slices are off in ue_zmq.conf (Part 10.1 turns them on)")
    sds = [None] * n
imsi0, imei0 = c["usim"]["imsi"].strip(), c["usim"]["imei"].strip()
for k in range(1, n + 1):
    u = load()
    rx, tx = 2100 + 100 * (k - 1), 2101 + 100 * (k - 1)
    u["rf"]["device_args"] = f"tx_port=tcp://127.0.0.1:{tx},rx_port=tcp://127.0.0.1:{rx},base_srate={base_srate}"
    u["usim"]["imsi"] = str(int(imsi0) + k - 1).zfill(len(imsi0))
    u["usim"]["imei"] = str(int(imei0) + k - 1).zfill(len(imei0))
    u["gw"]["netns"] = f"ue{k}"
    if not u.has_section("log"): u.add_section("log")
    u["log"]["filename"] = f"/tmp/ue{k}.log"
    if u.has_section("pcap"): u["pcap"]["enable"] = "none"
    if slicing: u["slicing"]["nssai-sd"] = str(sds[k - 1])
    with open(os.path.join(mu, f"ue{k}.conf"), "w") as f:
        f.write(f"# UE{k} for the multi-UE broker (generated by intact-multiue setup from ue_zmq.conf; do not edit)\n")
        u.write(f)
    print(k, u["usim"]["imsi"], sds[k - 1] if slicing else "-", u["usim"]["k"].strip(), u["usim"]["opc"].strip(), c["nas"]["apn"].strip())
with open(os.path.join(mu, "multiue.env"), "w") as f:
    f.write(f'MU_N={n}\nMU_SLICING={1 if slicing else 0}\nMU_SD="{" ".join(str(s) for s in sds) if slicing else ""}"\n'
            f'MU_IMSI="{" ".join(str(int(imsi0) + k - 1).zfill(len(imsi0)) for k in range(1, n + 1))}"\n'
            f'MU_PATHLOSS="{" ".join(["0"] * n)}"\nMU_SLOWDOWN=1\nMU_SRATE={base_srate}\nMU_GR_PY={grpy}\n')
PY
) || die "UE config generation failed (message above)"
  local k imsi sd K OPC APN
  while read -r k imsi sd K OPC APN; do
    ok "ue$k.conf: IMSI $imsi, slice SD ${sd}, netns ue$k, ZMQ RX $(rxport "$k") / TX $(txport "$k"), log /tmp/ue$k.log"
  done <<<"$PLAN"

  echo "=== 3. Subscribers in Open5GS"
  local doc have_sd
  while read -r k imsi sd K OPC APN; do
    doc=$(mongosh --quiet open5gs --eval "JSON.stringify(db.subscribers.findOne({imsi:\"$imsi\"},{_id:0,security:1,slice:1}))" 2>/dev/null)
    have_sd=$(python3 -c '
import json,sys
d=json.loads(sys.argv[1]) if sys.argv[1] not in ("","null") else None
if not d: print("none"); sys.exit()
ok=d["security"]["k"].lower()==sys.argv[2].lower() and (d["security"].get("opc") or "").lower()==sys.argv[3].lower()
sds=sorted({int(str(s.get("sd","0")),16) for s in d.get("slice",[]) if "sd" in s})
print(("ok" if ok else "keymismatch")+":"+",".join(map(str,sds)))' "$doc" "$K" "$OPC")
    if [ "$k" = 1 ] && { { [ "$sd" = - ] && [[ "$have_sd" == ok:* ]]; } || [ "$have_sd" = "ok:$sd" ]; }; then
      ok "UE1 $imsi already provisioned (slice SD ${have_sd#ok:}); left unchanged"; continue
    fi
    open5gs-dbctl remove "$imsi" >/dev/null 2>&1 || true
    if [ "$sd" = - ]; then
      open5gs-dbctl add_ue_with_apn "$imsi" "$K" "$OPC" "$APN" >/dev/null || die "open5gs-dbctl add_ue_with_apn $imsi failed"
    else
      open5gs-dbctl add_ue_with_slice "$imsi" "$K" "$OPC" "$APN" 1 "$(printf '%06x' "$sd")" >/dev/null || die "open5gs-dbctl add_ue_with_slice $imsi failed"
    fi
    doc=$(mongosh --quiet open5gs --eval "JSON.stringify(db.subscribers.findOne({imsi:\"$imsi\"},{_id:0,'slice.sst':1,'slice.sd':1}))" 2>/dev/null)
    if [ -n "$doc" ] && [ "$doc" != null ]; then ok "UE$k $imsi provisioned: $doc"; else die "UE$k $imsi not found after provisioning"; fi
  done <<<"$PLAN"

  echo "=== 4. Cross-checks: gNB, AMF and NSSF know every slice; ports and sample rates match"
  python3 - "$CFG" "$O5" "$MU" <<'PY' || die "cross-check failed (lines above)"
import configparser, os, re, sys, yaml
cfg, o5, mu = sys.argv[1:4]
env = dict(l.strip().split("=", 1) for l in open(os.path.join(mu, "multiue.env")) if "=" in l)
sds = [int(x) for x in env["MU_SD"].strip('"').split()] if env["MU_SLICING"] == "1" else []
bad = 0
for g in ("gnb_zmq.yaml", "gnb_zmq_jbpf.yaml"):
    p = os.path.join(cfg, g)
    if not os.path.exists(p): continue
    d = yaml.safe_load(open(p))
    dev = dict(x.split("=", 1) for x in d["ru_sdr"]["device_args"].replace(" ", "").split(","))
    ports_ok = dev["tx_port"].endswith(":2000") and dev["rx_port"].endswith(":2001")
    print(f"  [{' OK ' if ports_ok else 'FAIL'}] {g}: gNB ZMQ TX {dev['tx_port']} RX {dev['rx_port']} (the broker expects 2000/2001)"); bad += not ports_ok
    u = configparser.ConfigParser(strict=False, interpolation=None); u.read(os.path.join(mu, "ue1.conf"))
    ua = dict(x.split("=", 1) for x in u["rf"]["device_args"].split(","))
    sr_ok = float(dev.get("base_srate", "0")) == float(ua.get("base_srate", "-1"))
    print(f"  [{' OK ' if sr_ok else 'FAIL'}] {g}: base_srate {dev.get('base_srate')} = UE {ua.get('base_srate')}"); bad += not sr_ok
    if sds:
        have = {int(x["sd"]) for x in d.get("cell_cfg", {}).get("slicing", []) if "sd" in x}
        miss = sorted(set(sds) - have)
        print(f"  [{' OK ' if not miss else 'FAIL'}] {g}: cell_cfg.slicing has SD {sorted(have)}" + (f", MISSING {miss} (run intact-slices-on, Part 10.1)" if miss else "")); bad += bool(miss)
for f in ("amf.yaml", "nssf.yaml"):
    if not sds: break
    try: t = open(os.path.join(o5, f)).read()
    except OSError as e: print(f"  [WARN] cannot read {f}: {e}"); continue
    have = {int(x, 16) for x in re.findall(r"^\s*sd:\s*([0-9A-Fa-f]{6})\b", t, re.M)}
    miss = sorted(set(sds) - have)
    print(f"  [{' OK ' if not miss else 'FAIL'}] {f}: slice SDs {sorted(have)}" + (f", MISSING {miss} (Part 10.1)" if miss else "")); bad += bool(miss)
sys.exit(1 if bad else 0)
PY
  echo "=== 5. Broker self-check"
  source "$MU/multiue.env"
  "$MU_GR_PY" "$BROKER" --ues "$MU_N" --srate "$MU_SRATE" --check 2>&1 | grep -v vmcircbuf | sed 's/^/  /'
  git -C "$CFG" add -A >/dev/null 2>&1 && git -C "$CFG" commit -qm "multi-UE: $MU_N UEs" >/dev/null 2>&1 || true
  echo
  echo "SETUP DONE: $MU_N UEs. Start order: core -> jrt-controller -> gNB -> proxy -> 'intact-multiue up' (instead of intact-run-ue)."
}

# ------------------------------------------------------------------------------------------- stop
stop_all(){
  local f
  echo "  stopping UEs ..."
  for f in "${PIDS:?}"/ue*.pid; do [ -f "$f" ] && sudo kill -INT "$(cat "$f")" 2>/dev/null; done
  sleep 3; sudo pkill -KILL -x srsue 2>/dev/null
  echo "  stopping the broker ..."
  [ -f "${PIDS:?}/broker.pid" ] && kill -INT "$(cat "${PIDS:?}/broker.pid")" 2>/dev/null
  sleep 1; pkill -KILL -f "$BPAT" 2>/dev/null
  if pgrep -x gnb >/dev/null; then
    echo "  stopping the gNB (it cannot continue without its radio peers) ..."
    sudo pkill -INT -x gnb 2>/dev/null; sleep 3; sudo pkill -KILL -x gnb 2>/dev/null
  fi
  rm -f "${PIDS:?}"/*.pid
  if pgrep -ax 'gnb|srsue' || pgrep -af "$BPAT"; then echo "  WARNING: still running (above)"; else echo "  UEs, broker and gNB stopped."; fi
  echo "  jrt-controller and the reverse proxy were left alone. For a clean restart: intact-down, then T2..T5 again in order."
}
down(){ stop_all; }

# ------------------------------------------------------------------------------------------- up
up(){
  load_env
  local SLOW="$MU_SLOWDOWN" PL="$MU_PATHLOSS" k f i
  while [ $# -gt 0 ]; do case "$1" in
    --slowdown) SLOW="$2"; shift 2 ;;
    --pathloss) PL="$2"; shift 2 ;;
    *) die "unknown option $1" ;; esac; done
  echo "=== 0. Pre-checks"
  prach_fixed || die "srsUE always sends PRACH preamble 0, so UEs started together collide on every attempt. Run: intact-down; intact-fix-ue-prach (E.3b); then start again"
  ok "srsUE picks a random PRACH preamble (E.3b fix built in)"
  pgrep -x srsue >/dev/null && die "a srsUE is already running (single-UE window?). Stop everything with intact-down and start again"
  pgrep -f "$BPAT" >/dev/null && die "a broker is already running: intact-multiue down"
  pgrep -x gnb >/dev/null || die "the gNB is not running. Start it first in its own window (T3): intact-run-gnb"
  grep -q 'gNB started' "$RUN/gnb.out" 2>/dev/null || die "the gNB has not printed 'gNB started' yet (see $RUN/gnb.out)"
  ok "gNB running: $(ps -o args= -C gnb | head -1)"
  if ss -Htn state established '( sport = :2000 or dport = :2001 )' 2>/dev/null | grep -q .; then
    ss -Htn state established '( sport = :2000 or dport = :2001 )' 2>/dev/null | sed 's/^/    /'
    die "something is still connected to the gNB's ZMQ ports (an earlier UE?). Restart the gNB (intact-down, then T2..T4), then run this again"
  fi
  ok "gNB is fresh: nothing connected to its ZMQ ports 2000/2001"
  listening 2001 && die "port 2001 is already in use (an old single-UE srsUE?): intact-down"
  for ((k = 1; k <= MU_N; k++)); do
    [ -f "$MU/ue$k.conf" ] || die "$MU/ue$k.conf missing: intact-multiue setup $MU_N"
    listening "$(rxport "$k")" && die "port $(rxport "$k") already in use"
    listening "$(txport "$k")" && die "port $(txport "$k") already in use"
  done
  ok "ports 2001 and $(rxport 1)..$(txport "$MU_N") free"
  sudo -v || die "sudo authentication failed"

  echo "=== 1. Start $MU_N srsUEs in the background (output: $RUN/ue<k>.out)"
  for ((k = 1; k <= MU_N; k++)); do
    sudo ip netns list | grep -qw "ue$k" || sudo ip netns add "ue$k"
    f="/tmp/ue${k}_metrics.csv"; [ "$k" = 1 ] && f=/tmp/ue_metrics.csv
    rm -f "${RUN:?}/ue$k.out" "${PIDS:?}/ue$k.pid"
    ( cd "$(dirname "$SRSUE")" && sudo setsid bash -c 'echo $$ > "$1"; shift; exec stdbuf -oL -eL "$@"' _ \
        "$PIDS/ue$k.pid" "$SRSUE" "$MU/ue$k.conf" --general.metrics_csv_enable=true --general.metrics_csv_filename="$f" \
        --general.metrics_period_secs=1 --general.metrics_csv_flush_period_sec=1 >"$RUN/ue$k.out" 2>&1 </dev/null & )
  done
  trap 'echo; echo "=== Ctrl-C: stopping UEs, broker and gNB together"; stop_all; exit 0' INT TERM HUP
  for ((k = 1; k <= MU_N; k++)); do
    for ((i = 0; i < 30; i++)); do listening "$(txport "$k")" && break; sleep 1; done
    if ! listening "$(txport "$k")"; then
      tail -8 "$RUN/ue$k.out" | sed 's/^/    /'; fail "UE$k did not open its TX port $(txport "$k") within 30 s"; stop_all; exit 1
    fi
    ok "UE$k running (pid $(cat "$PIDS/ue$k.pid" 2>/dev/null)), TX port $(txport "$k") open"
  done

  echo "=== 2. Start the broker (output: $RUN/zmq-broker.out)"
  rm -f "${RUN:?}/zmq-broker.out"
  setsid "$MU_GR_PY" "$BROKER" --ues "$MU_N" --slowdown "$SLOW" --pathloss "$PL" --srate "$MU_SRATE" \
    >"$RUN/zmq-broker.out" 2>&1 </dev/null &
  echo $! >"$PIDS/broker.pid"
  for ((i = 0; i < 20; i++)); do grep -q 'flowgraph started' "$RUN/zmq-broker.out" 2>/dev/null && break; sleep 1; done
  grep -q 'flowgraph started' "$RUN/zmq-broker.out" || { tail -8 "$RUN/zmq-broker.out" | sed 's/^/    /'; fail "broker did not start"; stop_all; exit 1; }
  ok "broker running (pid $(cat "$PIDS/broker.pid")), slowdown $SLOW, path loss [$PL] dB"

  local TMAX=$((90 + 30 * MU_N)) t ip4 n_att=0
  declare -A ATT=()
  echo "=== 3. Waiting for every UE to attach (up to ${TMAX} s)"
  for ((t = 0; t < TMAX; t += 2)); do
    for ((k = 1; k <= MU_N; k++)); do
      [ -n "${ATT[$k]:-}" ] && continue
      ip4=$(ue_ip "$k")
      if [ -n "$ip4" ]; then
        sudo ip netns exec "ue$k" ip route replace default dev tun_srsue
        ATT[$k]=$ip4; n_att=$((n_att + 1)); ok "UE$k attached after ${t} s: IP $ip4, default route via tun_srsue"
      elif grep -qE "$UERR" "$RUN/ue$k.out" 2>/dev/null; then
        ATT[$k]="FAILED"; fail "UE$k: $(grep -E "$UERR" "$RUN/ue$k.out" | tail -1)"
      elif ! pid_alive "$PIDS/ue$k.pid"; then
        ATT[$k]="EXITED"; fail "UE$k process exited: $(tail -1 "$RUN/ue$k.out")"
      fi
    done
    [ "${#ATT[@]}" -eq "$MU_N" ] && break
    sleep 2
  done
  echo
  local -a IM SDS
  read -r -a IM <<<"$MU_IMSI"; read -r -a SDS <<<"${MU_SD:-}"
  printf '  %-4s %-16s %-4s %-18s %s\n' UE IMSI SD IP "last UE line"
  for ((k = 1; k <= MU_N; k++)); do
    printf '  %-4s %-16s %-4s %-18s %s\n' "ue$k" "${IM[$((k - 1))]}" "${SDS[$((k - 1))]:--}" "${ATT[$k]:-not attached}" \
      "$(grep -E 'Attaching|Random Access|RRC Connected|PDU Session|Reject|Release|Error' "$RUN/ue$k.out" 2>/dev/null | tail -1 | cut -c1-60)"
  done
  echo
  if [ "$n_att" -eq "$MU_N" ]; then echo "ALL $MU_N UEs ATTACHED."; else echo "$n_att of $MU_N UEs attached (E.6 lists the causes)."; fi
  echo "This window now monitors. In another window: intact-multiue status | intact-multiue iperf 5 20"
  echo "Ctrl-C here stops all UEs, the broker AND the gNB."
  local na dead
  while true; do
    sleep 30
    na=0; dead=""
    for ((k = 1; k <= MU_N; k++)); do [ -n "$(ue_ip "$k")" ] && na=$((na + 1)); pid_alive "$PIDS/ue$k.pid" || dead="$dead ue$k"; done
    echo "$(date +%H:%M:%S) attached $na/$MU_N${dead:+, EXITED:$dead} | $(grep '\[broker\] t=' "$RUN/zmq-broker.out" | tail -1 | sed 's/.*DL/DL/')"
    kill -0 "$(cat "$PIDS/broker.pid" 2>/dev/null)" 2>/dev/null || echo "  WARNING: the broker has exited: tail $RUN/zmq-broker.out"
  done
}

# ------------------------------------------------------------------------------------------- status
status(){
  load_env
  local k ip4 st rx tx er et
  local -a IM SDS
  echo "== UEs (configured: $MU_N)"
  read -r -a IM <<<"$MU_IMSI"; read -r -a SDS <<<"${MU_SD:-}"
  printf '  %-4s %-16s %-3s %-8s %-22s %-17s %s\n' UE IMSI SD PROCESS "ZMQ RX/TX (estab.)" IP "last UE line"
  for ((k = 1; k <= MU_N; k++)); do
    if pid_alive "$PIDS/ue$k.pid"; then st="running"; else st="stopped"; fi
    rx=$(rxport "$k"); tx=$(txport "$k")
    er=$(ss -Htn state established "( sport = :$rx )" 2>/dev/null | wc -l); et=$(ss -Htn state established "( sport = :$tx )" 2>/dev/null | wc -l)
    ip4=$(ue_ip "$k")
    printf '  %-4s %-16s %-3s %-8s %-22s %-17s %s\n' "ue$k" "${IM[$((k - 1))]}" "${SDS[$((k - 1))]:--}" "$st" "$rx($er)/$tx($et)" "${ip4:--}" \
      "$(grep -E 'Attaching|Random Access|RRC Connected|PDU Session|Reject|Release|Error' "$RUN/ue$k.out" 2>/dev/null | tail -1 | cut -c1-50)"
  done
  echo "== Broker"
  if pgrep -f "$BPAT" >/dev/null; then echo "  running: $(grep '\[broker\] t=' "$RUN/zmq-broker.out" | tail -1)"; else echo "  not running"; fi
  echo "  gNB TX :2000 established $(ss -Htn state established '( sport = :2000 )' 2>/dev/null | wc -l), gNB RX :2001 established $(ss -Htn state established '( sport = :2001 )' 2>/dev/null | wc -l)  (1 and 1 while running)"
  echo "== gNB"
  if pgrep -x gnb >/dev/null; then
    echo "  running: $(ps -o args= -C gnb | head -1)"
    python3 - <<'PY'
import base64, json, os, socket, struct, time
def reports(n=3, host="127.0.0.1", port=8001, timeout=6.0):
    s = socket.create_connection((host, port), timeout=3)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf: buf += s.recv(4096)
    buf = buf.split(b"\r\n\r\n", 1)[1]
    m = json.dumps({"cmd": "metrics_subscribe"}).encode(); mk = os.urandom(4)
    s.sendall(bytes([0x81, 0x80 | len(m)]) + mk + bytes(b ^ mk[i % 4] for i, b in enumerate(m)))
    s.settimeout(timeout); out = []
    def exact(k):
        nonlocal buf
        while len(buf) < k:
            c = s.recv(65536)
            if not c: raise ConnectionError("closed")
            buf += c
        d, buf = buf[:k], buf[k:]; return d
    while len(out) < n:
        b1, b2 = exact(2); L = b2 & 0x7F
        if L == 126: L = struct.unpack("!H", exact(2))[0]
        elif L == 127: L = struct.unpack("!Q", exact(8))[0]
        p = exact(L)
        try: o = json.loads(p.decode(errors="ignore"))
        except ValueError: continue
        if isinstance(o, dict) and isinstance(o.get("cells"), list): out.append((time.monotonic(), o))
    return out
try:
    r = reports()
    per = (r[-1][0] - r[0][0]) / (len(r) - 1)
    ues = [u for c in r[-1][1]["cells"] for u in (c.get("ue_list") or [])]
    print(f"  scheduler report every {per:.2f} s (configured 0.5 s: radio time runs at about {0.5/per:.2f}x real time); {len(ues)} UE(s) in the cell")
    print(f"  {'rnti':>6} {'cqi':>4} {'dl_mcs':>6} {'DL Mbit/s':>10} {'UL Mbit/s':>10} {'dl_bs kB':>9}")
    for u in ues:
        print(f"  {u.get('rnti', '?'):>6} {u.get('cqi', 0):>4} {u.get('dl_mcs', 0):>6} {u.get('dl_brate', 0)/1e6:>10.2f} "
              f"{u.get('ul_brate', 0)/1e6:>10.2f} {u.get('dl_bs', 0)/1e3:>9.1f}")
except Exception as e:
    print(f"  (no scheduler metrics from ws://127.0.0.1:8001: {e}; Part 9b turns them on)")
PY
  else echo "  not running"; fi
  echo "== Core (last AMF line per IMSI)"
  local im
  for im in $MU_IMSI; do echo "  $im: $(sudo grep "imsi-$im" /var/log/open5gs/amf.log 2>/dev/null | grep -E 'Registration|reject|Deregistration' | tail -1 | cut -c1-110)"; done
}

# ------------------------------------------------------------------------------------------- iperf
iperf(){
  load_env
  local RATE="${1:-5}" DUR="${2:-20}" DIR="-R" LAB="downlink" k ip4 OUT
  [ "${3:-}" = "--ul" ] && { DIR=""; LAB="uplink"; }
  OUT="$LOGS/multiue-iperf-$(date +%Y%m%d-%H%M%S)"; mkdir -p "$OUT"
  echo "=== $LAB UDP, $RATE Mbit/s offered per UE, $DUR s, all UEs at the same time (results: $OUT)"
  local -a UES=()
  for ((k = 1; k <= MU_N; k++)); do
    ip4=$(ue_ip "$k"); if [ -n "$ip4" ]; then UES+=("$k"); else echo "  ue$k not attached: skipped"; fi
  done
  [ "${#UES[@]}" -gt 0 ] || die "no UE attached"
  for k in "${UES[@]}"; do
    pkill -f "iperf3 -s -B 10.45.0.1 -p $((5210 + k))" 2>/dev/null
    iperf3 -s -B 10.45.0.1 -p $((5210 + k)) -1 -D --logfile "$OUT/server-ue$k.log"
  done
  sleep 1
  for k in "${UES[@]}"; do
    sudo ip netns exec "ue$k" iperf3 -c 10.45.0.1 -p $((5210 + k)) -u -b "${RATE}M" $DIR -t "$DUR" -i 1 --json >"$OUT/ue$k.json" 2>&1 &
  done
  wait
  python3 - "$OUT" "${UES[@]}" <<'PY'
import json, sys
out, ues = sys.argv[1], sys.argv[2:]
tot = 0.0
print(f"  {'UE':<5} {'received Mbit/s':>16} {'loss %':>8} {'jitter ms':>10}")
for k in ues:
    try:
        d = json.load(open(f"{out}/ue{k}.json")); s = d["end"]["sum"]
        r = s["bits_per_second"] / 1e6; tot += r
        print(f"  ue{k:<3} {r:>16.2f} {s.get('lost_percent', 0):>8.1f} {s.get('jitter_ms', 0):>10.2f}")
    except Exception as e:
        print(f"  ue{k:<3} no result ({e}); see {out}/ue{k}.json")
print(f"  {'sum':<5} {tot:>16.2f}")
PY
}

case "${1:-}" in
  setup)  shift; setup "$@" ;;
  up)     shift; up "$@" ;;
  status) status ;;
  iperf)  shift; iperf "$@" ;;
  down)   down ;;
  *)      sed -n '2,10p' "$0" ;;
esac
MUEOF
chmod +x ~/intact/bin/intact-multiue
intact-multiue
```

**Expected:** the script's usage, i.e. the five commands listed at its top (`setup`, `up`, `status`, `iperf`, `down`).

### E.3 One-time setup — \[SCRIPT\], stack down, core up

```bash
source ~/intact/env.sh
intact-down
intact-multiue setup 3
```

For 5 UEs, run `intact-multiue setup 5`. To choose the slices, add `--sd 1,2,1` (one SD per UE). What the script does, in order:

1. **Pre-checks:**
   - no gNB, srsUE or broker running;
   - `sudo` works;
   - srsue is built;
   - `ue_zmq.conf` exists;
   - MongoDB, `open5gs-dbctl` and `mongosh` are present.

   It then prints the number of CPUs and the free memory in WSL, and the recommended maximum number of UEs.
2. **GNU Radio:** it finds a Python with `gnuradio` (Ubuntu's `/usr/bin/python3`). If there is none, it installs `gnuradio` with apt (about 600 MB, a few minutes; output in `$LOGS/apt-gnuradio.log`).
3. **UE configs:** `~/intact/configs/multiue/ue1.conf` … `ueN.conf` are copies of your `ue_zmq.conf` with six changes:
   - the ZMQ ports;
   - IMSI = yours + (k − 1);
   - IMEI = yours + (k − 1);
   - netns `uek`;
   - log file `/tmp/uek.log`;
   - the slice SD.

   `ue_zmq.conf` itself is not touched.
4. **Subscribers:**
   - UE1's subscriber is left unchanged if its key, OPc and slice already match.
   - Every other IMSI is removed (if present) and added with `open5gs-dbctl add_ue_with_slice <imsi> <K> <OPc> internet 1 <SD>`, exactly as in Part 10.1.
   - Each subscriber is then read back from MongoDB.
5. **Cross-checks:**
   - both gNB configs still use ZMQ ports 2000/2001;
   - their `base_srate` equals the UEs';
   - every SD used appears in the gNB's `cell_cfg.slicing`, in `amf.yaml` and in `nssf.yaml`.
6. **Broker self-check:** the flowgraph is built once without being run, and the port map is printed. The configs are committed to git.

**Expected:**

```
=== 0. Pre-checks (the stack must be down)
  [WARN] srsUE always sends PRACH preamble 0: run intact-fix-ue-prach (E.3b) before 'intact-multiue up'
  [ OK ] WSL has 16 CPUs and 9.1 GiB free memory: recommended at most 5 UEs
=== 1. GNU Radio (the broker)
  [ OK ] GNU Radio 3.10.9.2 via /usr/bin/python3
=== 2. UE configs in /home/kamrul/intact/configs/multiue (UE1 keeps your current IMSI and slice)
  [ OK ] ue1.conf: IMSI 001010123456780, slice SD 1, netns ue1, ZMQ RX 2100 / TX 2101, log /tmp/ue1.log
  [ OK ] ue2.conf: IMSI 001010123456781, slice SD 2, netns ue2, ZMQ RX 2200 / TX 2201, log /tmp/ue2.log
  [ OK ] ue3.conf: IMSI 001010123456782, slice SD 1, netns ue3, ZMQ RX 2300 / TX 2301, log /tmp/ue3.log
=== 3. Subscribers in Open5GS
  [ OK ] UE1 001010123456780 already provisioned (slice SD 1); left unchanged
  [ OK ] UE2 001010123456781 provisioned: {"slice":[{"sst":1,"sd":"000002"}]}
  [ OK ] UE3 001010123456782 provisioned: {"slice":[{"sst":1,"sd":"000001"}]}
=== 4. Cross-checks: gNB, AMF and NSSF know every slice; ports and sample rates match
  [ OK ] gnb_zmq.yaml: gNB ZMQ TX tcp://127.0.0.1:2000 RX tcp://127.0.0.1:2001 (the broker expects 2000/2001)
  [ OK ] gnb_zmq.yaml: base_srate 11.52e6 = UE 11.52e6
  [ OK ] gnb_zmq.yaml: cell_cfg.slicing has SD [1, 2]
  ... the same three lines for gnb_zmq_jbpf.yaml ...
  [ OK ] amf.yaml: slice SDs [1, 2]
  [ OK ] nssf.yaml: slice SDs [1, 2]
=== 5. Broker self-check
  [broker] GNU Radio 3.10.9.2  UEs=3  srate=11.52 Msps  throttle=11.52 Msps (slowdown 1.0)
  [broker] UE1: broker -> RX tcp://127.0.0.1:2100   UE TX tcp://127.0.0.1:2101 -> broker   path loss 0.0 dB
  ...
  [broker] CHECK OK: flowgraph built (not started)

SETUP DONE: 3 UEs. Start order: core -> jrt-controller -> gNB -> proxy -> 'intact-multiue up' (instead of intact-run-ue).
```

The exact CPU count and memory are yours. A `[WARN] N=... is above that` line is a warning, not a stop.

**If not:**

| Message | Fix |
| --- | --- |
| `gNB, srsUE or broker running` | `intact-down`, then run setup again |
| `MongoDB not running` | `sudo systemctl start mongod`, or `intact-core-restart` |
| `apt-get install gnuradio failed` | `tail -20 ~/intact/logs/apt-gnuradio.log` (usually a network or apt lock problem; `sudo apt update` and retry) |
| `cell_cfg.slicing ... MISSING`, `amf.yaml ... MISSING` | slices are not on: run `intact-slices-on` (Part 10.1), then setup again |
| `--sd given but slices are off` | without Part 10 there are no slices; drop `--sd` |
| `base_srate ... FAIL` | the gNB and `ue_zmq.conf` disagree; `intact-cfg-check` names the fix (Part 7.3) |

### E.3b srsUE fix: a random PRACH preamble — \[SCRIPT\], stack down, about 5 min

**Why (found on the first real run, 30 Oct).** With 3 UEs, every UE stayed at `Random Access Transmission ... preamble_index=0`, and `/tmp/ue<k>.log` showed the same cycle seven times: preamble 0 sent, RAR received, Msg3 sent, `Contention Resolution Timer expired`. The cause is in srsUE, not in the broker or the gNB:

1. The gNB leaves `totalNumberOfRA-Preambles` out of SIB1 when it is 64. In TS 38.331 that means all 64 preambles are available for random access.
2. srsUE copies the value only when it is present (`make_mac_rach_cfg` in `lib/src/asn1/rrc_nr_utils.cc`). Otherwise `rach_cfg.nof_preambles` stays 0, and `proc_ra_nr.cc` then always sends **preamble 0**.
3. With one UE that does no harm. With the broker, all UEs run in lockstep on the same radio clock, so they send preamble 0 at the same moment. The gNB sees one preamble and sends one RAR, and all UEs send their Msg3 on the same resources. The broker adds the Msg3 signals together, the gNB cannot decode the sum, no contention resolution is sent, and all UEs retry at the same moment, every time.

**The fix:** when the value is absent, use 64, and draw the preamble uniformly from 0 to 63. Each UE seeds its own random generator from the clock at start-up, so the UEs pick different preambles. The gNB then detects them separately and gives each UE its own RAR and Msg3 grant. Two UEs pick the same preamble with probability 1/64 per attempt; the next attempt draws again. The patch also corrects an off-by-one in srsUE's own branch: its random range includes both ends, so it could pick preamble N.

**What the block does:**

1. Runs `intact-down`.
2. Creates `intact-fix-ue-prach`, which:
   - refuses to run while a gNB, srsUE or broker is running;
   - checks that your gNB configs use 64 contention-based preambles on one SSB (otherwise this fix would be wrong, so it stops without changing anything);
   - patches only `srsue/src/stack/mac_nr/proc_ra_nr.cc` (backup next to the file, diff in `~/intact/configs/provenance/srsue-prach-preamble.patch`);
   - rebuilds only the `srsue` target;
   - checks that the binary is newer than the source, still links ZeroMQ, and that the NSSAI fix of Part 10.2 is still there.
3. Adds a check to your installed `intact-multiue`: `up` now refuses to start if srsUE lacks this fix, and `setup` warns. E.2 Block 2 above already contains the same lines, so a fresh install gets them too.
4. Runs `intact-fix-ue-prach`.

Single-UE mode is not affected: one UE with a random preamble attaches exactly as before. You do **not** need to run `intact-multiue setup` again.

```bash
source ~/intact/env.sh
intact-down
cat > ~/intact/bin/intact-fix-ue-prach <<'PRFIXEOF'
#!/usr/bin/env bash
# intact-fix-ue-prach: let srsUE pick a random PRACH preamble, so several UEs started together can attach.
# Cause: the gNB omits totalNumberOfRA-Preambles from SIB1 when it is 64 (TS 38.331: absent = all 64 preambles
# are used for random access). srsUE then leaves rach_cfg.nof_preambles = 0 and ALWAYS sends preamble 0. With the
# ZMQ broker all UEs run in lockstep, so every attempt collides: same preamble, same RAR, same Msg3 resources; the
# gNB cannot decode the summed Msg3s, contention resolution never arrives, and all UEs retry together forever.
# Fix: absent => 64, and draw the preamble uniformly from 0..N-1 (the old code drew 0..N, one too many).
# Changes one file (srsue/src/stack/mac_nr/proc_ra_nr.cc); rebuilds only srsue. Safe to rerun.
set -euo pipefail
source ~/intact/env.sh
F="$SRSUE_DIR/srsue/src/stack/mac_nr/proc_ra_nr.cc"
B="$SRSUE_DIR/build/srsue/src/srsue"
echo "=== 0. Pre-checks"
if pgrep -x 'gnb|srsue' >/dev/null || pgrep -f '[i]ntact-zmq-broker --ues' >/dev/null; then
  pgrep -ax 'gnb|srsue'; echo "STOP: gNB, srsUE or broker running. Run intact-down first."; exit 1
fi
[ -f "$F" ] || { echo "STOP: $F not found"; exit 1; }
[ -d "$SRSUE_DIR/build" ] || { echo "STOP: $SRSUE_DIR/build missing (Part 6.3)"; exit 1; }
echo "=== 1. gNB PRACH settings (the fix assumes all 64 preambles are contention-based)"
python3 - "$CFG" <<'PY'
import os, sys, yaml
cfg, bad = sys.argv[1], 0
for name in ("gnb_zmq.yaml", "gnb_zmq_jbpf.yaml", "gnb_zmq_jbpf_e2.yaml"):
    p = os.path.join(cfg, name)
    if not os.path.exists(p): continue
    pr = (yaml.safe_load(open(p)).get("cell_cfg") or {}).get("prach") or {}
    tot = pr.get("total_nof_ra_preambles", 64); cb = pr.get("nof_cb_preambles_per_ssb", 64); ro = pr.get("nof_ssb_per_ro", 1)
    ok = tot == 64 and cb == 64 and ro == 1
    print(f"  [{' OK ' if ok else 'FAIL'}] {name}: total_nof_ra_preambles={tot}, nof_cb_preambles_per_ssb={cb}, nof_ssb_per_ro={ro}")
    bad += not ok
if bad: sys.exit("STOP: the gNB does not use 64 contention-based preambles on one SSB; this fix would be wrong. Nothing changed.")
PY
echo "=== 2. Patch $F"
python3 - "$F" <<'PY'
import sys, shutil, time, pathlib
p = pathlib.Path(sys.argv[1]); t = p.read_text()
old = ("  if (rach_cfg.nof_preambles) {\n"
       "    preamble_index = srsran_random_uniform_int_dist(random_gen, 0, rach_cfg.nof_preambles);\n"
       "  } else {\n"
       "    preamble_index = 0;\n"
       "  }\n")
new = ("  // INTACT: TS 38.331 RACH-ConfigCommon: totalNumberOfRA-Preambles absent => all 64 preambles are used for RA.\n"
       "  // srsUE left nof_preambles = 0 then and always sent preamble 0, so UEs started together (ZMQ multi-UE) collided\n"
       "  // on every attempt. srsran_random_uniform_int_dist() includes both ends, hence N - 1.\n"
       "  {\n"
       "    const uint32_t intact_nof_ra_preambles = rach_cfg.nof_preambles ? rach_cfg.nof_preambles : 64;\n"
       "    preamble_index = srsran_random_uniform_int_dist(random_gen, 0, (int)intact_nof_ra_preambles - 1);\n"
       "  }\n")
if new in t:
    print("  [ OK ] fix already present")
elif t.count(old) == 1:
    shutil.copy2(p, f"{p}.bak-prach-{time.strftime('%Y%m%d-%H%M%S')}")
    p.write_text(t.replace(old, new)); print("  [ OK ] fix applied (backup next to the file)")
else:
    sys.exit("STOP: proc_ra_nr.cc is not as expected; nothing changed. Send: sed -n 170,195p " + str(p))
PY
mkdir -p "$CFG/provenance"
git -C "$SRSUE_DIR" diff -- srsue/src/stack/mac_nr/proc_ra_nr.cc > "$CFG/provenance/srsue-prach-preamble.patch"
echo "  diff saved: $CFG/provenance/srsue-prach-preamble.patch ($(grep -c '^[-+] ' "$CFG/provenance/srsue-prach-preamble.patch") changed lines)"
echo "=== 3. Rebuild srsue only (a few minutes; log $LOGS/build-ue-prach.log)"
if ! ( set -o pipefail; cmake --build "$SRSUE_DIR/build" --target srsue -j"${JOBS:-4}" 2>&1 | tee "$LOGS/build-ue-prach.log" | tail -2 ); then
  echo "STOP: build failed: grep -n -m3 error $LOGS/build-ue-prach.log"; exit 1
fi
echo "=== 4. Verify"
[ "$B" -nt "$F" ] && echo "  [ OK ] srsue rebuilt after the fix" || { echo "STOP: $B is older than the patched source"; exit 1; }
ldd "$B" | grep -q libzmq && echo "  [ OK ] srsue still links ZeroMQ" || { echo "STOP: srsue lost libzmq"; exit 1; }
grep -q 'INTACT: Requested NSSAI' "$SRSUE_DIR/srsue/src/stack/upper/nas_5g.cc" && echo "  [ OK ] the NSSAI fix of Part 10.2 is still in place" \
  || echo "  [WARN] the NSSAI fix of Part 10.2 is not in nas_5g.cc (only matters with slices on)"
echo
echo "PRACH FIX OK. Restart: intact-down, then T2 jrtc, T3 gNB, T4 proxy, T5 intact-multiue up."
PRFIXEOF
chmod +x ~/intact/bin/intact-fix-ue-prach
python3 - ~/intact/bin/intact-multiue <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); t = p.read_text()
MARK = "prach_fixed(){"
edits = [
 ("UERR='Registration Reject",
  "PRACH_SRC=\"$SRSUE_DIR/srsue/src/stack/mac_nr/proc_ra_nr.cc\"\n"
  "prach_fixed(){ grep -q 'INTACT: TS 38.331 RACH-ConfigCommon' \"$PRACH_SRC\" 2>/dev/null && [ \"$SRSUE\" -nt \"$PRACH_SRC\" ]; }   # E.3b\n"
  "UERR='Registration Reject", 1),
 ("  [ -x \"$SRSUE\" ] || die \"srsue not built: $SRSUE (Part 6.3)\"\n",
  "  [ -x \"$SRSUE\" ] || die \"srsue not built: $SRSUE (Part 6.3)\"\n"
  "  if prach_fixed; then ok \"srsUE picks a random PRACH preamble (E.3b fix built in)\"; else warn \"srsUE always sends PRACH preamble 0: run intact-fix-ue-prach (E.3b) before 'intact-multiue up'\"; fi\n", 1),
 ("  echo \"=== 0. Pre-checks\"\n  pgrep -x srsue",
  "  echo \"=== 0. Pre-checks\"\n"
  "  prach_fixed || die \"srsUE always sends PRACH preamble 0, so UEs started together collide on every attempt. Run: intact-down; intact-fix-ue-prach (E.3b); then start again\"\n"
  "  ok \"srsUE picks a random PRACH preamble (E.3b fix built in)\"\n"
  "  pgrep -x srsue", 1),
]
if MARK in t:
    print("  [ OK ] intact-multiue already has the PRACH check"); sys.exit(0)
for old, new, n in edits:
    if t.count(old) != n: sys.exit(f"STOP: intact-multiue is not the E.2 version (anchor not found: {old[:40]!r}); paste E.2 Block 2 again, then rerun")
for old, new, n in edits: t = t.replace(old, new)
p.write_text(t); print("  [ OK ] intact-multiue: PRACH check added to 'setup' and 'up'")
PY
intact-fix-ue-prach
```

**Expected:**

```
RAN stack stopped (UEs, broker, gNB, proxy, jrt-controller, RIC, xApp); core still running
  [ OK ] intact-multiue: PRACH check added to 'setup' and 'up'
=== 0. Pre-checks
=== 1. gNB PRACH settings (the fix assumes all 64 preambles are contention-based)
  [ OK ] gnb_zmq.yaml: total_nof_ra_preambles=64, nof_cb_preambles_per_ssb=64, nof_ssb_per_ro=1
  [ OK ] gnb_zmq_jbpf.yaml: total_nof_ra_preambles=64, nof_cb_preambles_per_ssb=64, nof_ssb_per_ro=1
=== 2. Patch /home/kamrul/intact/src/jrtc-apps/containers/Docker/srsRAN_4G/srsue/src/stack/mac_nr/proc_ra_nr.cc
  [ OK ] fix applied (backup next to the file)
  diff saved: /home/kamrul/intact/configs/provenance/srsue-prach-preamble.patch (10 changed lines)
=== 3. Rebuild srsue only (a few minutes; log /home/kamrul/intact/logs/build-ue-prach.log)
[100%] Built target srsue
=== 4. Verify
  [ OK ] srsue rebuilt after the fix
  [ OK ] srsue still links ZeroMQ
  [ OK ] the NSSAI fix of Part 10.2 is still in place

PRACH FIX OK. Restart: intact-down, then T2 jrtc, T3 gNB, T4 proxy, T5 intact-multiue up.
```

A rerun prints `already has the PRACH check` and `fix already present`, and the rebuild does nothing.

**Then:** E.4 again, all five windows in order (a fresh gNB is required). `intact-multiue up` now prints `[ OK ] srsUE picks a random PRACH preamble (E.3b fix built in)` first. In `/tmp/ue<k>.log` each UE shows a different `preamble_index`, then `Random Access Complete`, `RRC Connected` and `PDU Session Establishment successful`.

**If not:**

| Message | Fix |
| --- | --- |
| `gNB, srsUE or broker running` | `intact-down`, then `intact-fix-ue-prach` |
| `the gNB does not use 64 contention-based preambles` | your gNB `prach:` section differs from Part 7.1. Send the `[FAIL]` line; nothing was changed |
| `proc_ra_nr.cc is not as expected` | send the output of the `sed -n 170,195p ...` command it prints |
| `intact-multiue is not the E.2 version` | paste E.2 Block 2 again (it already contains the check), then run `intact-fix-ue-prach` |
| `build failed` | `grep -n -m3 error ~/intact/logs/build-ue-prach.log` and send the lines |
| `srsue lost libzmq` | rerun `intact-build-ue` (Part 6.3), then this block |

### E.4 Start the stack with N UEs — \[MANUAL order\], then \[SCRIPT\] in T5

Open the windows in this order. Each one starts with `source ~/intact/env.sh`, and the next starts only after the previous one is ready.

1. **T1:** `intact-core-health` → `CORE READY`.
2. **T2:** `intact-run-jrtc`.
3. **T3:** `intact-run-gnb`. Wait for `==== gNB started ===`. This is a fresh gNB: no UE has been connected to it yet.
4. **T4:** `intact-run-proxy`.
5. **T5:** `intact-multiue up`.

What `intact-multiue up` does, with the reason for each step:

1. **Pre-checks:**
   - no srsUE and no broker is running;
   - the gNB is running and has printed `gNB started`;
   - nothing is connected to ports 2000/2001, so the gNB is fresh. A gNB that has already talked to a UE cannot take a new set of radio peers.
   - all UE ports are free.
2. **Starts the N srsUEs in the background.** Each runs in its own session, writes `$LOGS/run/ue<k>.out`, and gets the same metrics CSV options `intact-run-ue` uses. The script waits until each UE has opened its TX port.
3. **Starts the broker** (`$LOGS/run/zmq-broker.out`) and waits for `flowgraph started`.
4. **Waits for attach**, up to 90 s + 30 s per UE. As each UE gets an IP, it adds the default route in that UE's namespace, as `intact-up` does for UE1. A `Registration Reject`, a `Received RRC Release` or an exited process is reported for that UE; the others carry on.
5. **Prints a table** of all UEs, then **stays in the foreground as the monitor**: one line every 30 s with the number of attached UEs and the broker's speed.

**Ctrl-C in T5** stops all UEs, the broker **and the gNB**, because they can only run together. jrt-controller and the proxy keep running.

**Expected in T5:**

```
=== 0. Pre-checks
  [ OK ] srsUE picks a random PRACH preamble (E.3b fix built in)
  [ OK ] gNB running: ./gnb -c /home/kamrul/intact/configs/gnb_zmq_jbpf.yaml
  [ OK ] gNB is fresh: nothing connected to its ZMQ ports 2000/2001
  [ OK ] ports 2001 and 2100..2301 free
=== 1. Start 3 srsUEs in the background (output: /home/kamrul/intact/logs/run/ue<k>.out)
  [ OK ] UE1 running (pid 41234), TX port 2101 open
  [ OK ] UE2 running (pid 41245), TX port 2201 open
  [ OK ] UE3 running (pid 41257), TX port 2301 open
=== 2. Start the broker (output: /home/kamrul/intact/logs/run/zmq-broker.out)
  [ OK ] broker running (pid 41270), slowdown 1, path loss [0 0 0] dB
=== 3. Waiting for every UE to attach (up to 180 s)
  [ OK ] UE1 attached after 14 s: IP 10.45.0.2/24, default route via tun_srsue
  [ OK ] UE3 attached after 18 s: IP 10.45.0.3/24, default route via tun_srsue
  [ OK ] UE2 attached after 24 s: IP 10.45.0.4/24, default route via tun_srsue

  UE   IMSI             SD   IP                 last UE line
  ue1  001010123456780  1    10.45.0.2/24       PDU Session Establishment successful. IP: 10.45.0.2
  ue2  001010123456781  2    10.45.0.4/24       PDU Session Establishment successful. IP: 10.45.0.4
  ue3  001010123456782  1    10.45.0.3/24       PDU Session Establishment successful. IP: 10.45.0.3

ALL 3 UEs ATTACHED.
This window now monitors. In another window: intact-multiue status | intact-multiue iperf 5 20
Ctrl-C here stops all UEs, the broker AND the gNB.
14:02:41 attached 3/3 | DL  10.10 Msps  UL  10.10 Msps  speed 0.88x real time
```

The times, PIDs, IP order and speed shown are illustrative. The UEs attach one after another, and the SMF hands out IPs in the order the UEs register, so `ue2` may get `.4`.

In the **gNB window (T3)** the metrics table now has one row per UE, each with its own RNTI and CQI 15. The jrt-controller and proxy windows show nothing new.

### E.5 Check and use — T6

```bash
source ~/intact/env.sh
intact-multiue status
intact-multiue iperf 5 20          # 5 Mbit/s downlink to every UE at the same time, for 20 s
intact-multiue iperf 2 20 --ul     # 2 Mbit/s uplink from every UE
```

**Expected from `status`:**

- each UE `running`, with `2k00(1)/2k01(1)` (one established connection on each of its two ZMQ ports) and its IP;
- the broker's last line with the speed;
- `gNB TX :2000 established 1, gNB RX :2001 established 1`;
- under `== gNB`, the report period and speed derived from the gNB's own scheduler reports, then one line per UE with RNTI, CQI, MCS, DL and UL Mbit/s and the DL buffer;
- under `== Core`, the last AMF line per IMSI, normally `Registration complete`.

**Expected from `iperf`:** a table with each UE's received rate, loss and jitter, plus the sum. With 5 Mbit/s per UE and 3 UEs, each UE should get close to 5 Mbit/s with little loss. The cell carries about 26 Mbit/s at 1.0× (Part 10.5), and proportionally less at lower speed. Above that, the scheduler shares the cell and the loss column grows. The raw iperf3 JSON files are in the `multiue-iperf-<time>` folder the command names.

**With slices and `slice_mgmt` (Part 10, Appendix B):** the PRB cap applies to a slice as a whole, not to one UE. With the default mapping, UE1 and UE3 share slice SD 1's cap and UE2 has SD 2's. To see it, load the `slice_mgmt` app as in 10.4 and run `intact-multiue iperf 15 60`: the sum of UE1 and UE3 follows the 30 % / 70 % swaps, and UE2 moves the opposite way.

### E.6 Stop, and go back to one UE

- **Ctrl-C in T5** stops the UEs, the broker and the gNB. For a clean restart run `intact-down`, then T2, T3, T4 and T5 again.
- **From any window:**
  - `intact-multiue down` does the same;
  - `intact-down` stops everything (UEs, broker, gNB, proxy, jrt-controller, and the RIC and xApp of Appendix F).
- **Back to one UE:** `intact-down`, then start T2–T4 as before and run `intact-run-ue` in T5. `ue_zmq.conf` was never changed, so single-UE mode is exactly as it was. The extra subscribers can stay in the database.
- **Change the number of UEs:** `intact-down`, then `intact-multiue setup <N>` again.
- **Remove multi-UE completely (optional):**

```bash
for i in 1 2 3 4; do open5gs-dbctl remove 00101012345678$i; done
rm -r ~/intact/configs/multiue
for i in 2 3 4 5; do sudo ip netns del ue$i 2>/dev/null; done
```

### E.7 Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `up` stops with `something is still connected to the gNB's ZMQ ports` | an earlier UE or broker has used this gNB. Run `intact-down`, restart T2–T4, then `intact-multiue up` |
| `UE<k> did not open its TX port` | that srsUE failed at start: `tail -30 ~/intact/logs/run/ue<k>.out` (a config parse error names the line) |
| the broker prints `speed 0.00` or `(no samples ...)` | one of the N UEs or the gNB is not running. `intact-multiue status` shows which link has `(0)` established |
| all UEs stay at `Attaching UE...` | the broker is not running, or `base_srate` differs (setup checks it). `tail ~/intact/logs/run/zmq-broker.out` |
| some UEs attach, others show `Registration Reject` | that IMSI is not provisioned, or its slice is not allowed. Run `intact-multiue setup N` again (stack down), then `sudo grep imsi-<IMSI> /var/log/open5gs/amf.log \| tail` |
| a UE repeats `Random Access` | occasional PRACH collisions are normal when UEs start together. If it persists, use fewer UEs or `intact-multiue up --slowdown 1.5` |
| `speed` below 0.5× | too many UEs for the CPUs: use fewer, close heavy Windows programs, keep mains power. `--slowdown 0` removes the cap but does not make anything faster |
| a UE later prints `Received RRC Release` | the radio stalled (check the broker line in T5) or the UE was idle beyond the inactivity timer (7200 s in this runbook) |
| `GNU Radio is installed but 'from gnuradio import zeromq' fails` | the system Python is not Ubuntu's. Send the output of `/usr/bin/python3 -c 'from gnuradio import zeromq'` |
| all UEs stay at `Random Access Transmission ... preamble_index=0`; `/tmp/ue<k>.log` repeats `Contention Resolution Timer expired` | srsUE without the E.3b fix: all UEs send preamble 0 at the same moment and collide. Run `intact-down`, E.3b, then E.4 again |

### E.8 What else changes with several UEs

- **gNB metrics.** `intact-kpm-log` and `intact-gate-a-effect` (Appendix B) add up all UEs' `dl_brate`; the `n_ue` column shows how many. That gives a cell total. Per UE, use `intact-multiue status`, or Appendix F's E2 CSV, which has one row per UE.
- **`intact-telemetry` (9b)** still reads UE1's CSV (`/tmp/ue_metrics.csv`) and pings from `ue1`. The other UEs' CSVs are `/tmp/ue<k>_metrics.csv`.
- **Appendix D (PACT-R1)** is a one-UE scenario: run it in single-UE mode. A multi-UE PACT scenario, with tenants as slices holding several UEs, needs its own design.
- **`intact-up` (tmux, Part 8.3)** is single-UE. With several UEs, use the windows above.

## Appendix F — FlexRIC nearRT-RIC and a KPM xApp over E2, saved to CSV

This appendix adds a standard O-RAN E2 path next to jbpf:

- the gNB's own E2 agent, in the DU, connects over SCTP to a FlexRIC nearRT-RIC;
- an xApp subscribes to E2SM-KPM through the RIC and writes every measurement it receives to a CSV file.

jbpf stays as it is for control (`slice_mgmt`, Gate A, PACT); E2 is used here for monitoring. It works with one UE or with Appendix E's 3–5 UEs. No existing file is changed: the gNB gets a second config file, `gnb_zmq_jbpf_e2.yaml`, and FlexRIC is installed under `~/intact/opt/flexric` and `~/intact/src/flexric`.

### F.0 What runs, how it talks, and which versions

| Component | Process | Endpoint (all on 127.0.0.1) | Protocol |
| --- | --- | --- | --- |
| gNB E2 agent (DU) | `gnb` | client, ephemeral port → **36421** | **E2AP over SCTP** (ASN.1 aligned PER). E2SM-KPM is RAN function ID 2 |
| FlexRIC nearRT-RIC | `nearRT-RIC` | listens on SCTP **36421** (E2, the gNB) and **36422** (E42, the xApps) | E2AP towards the gNB; **E42 over SCTP** towards xApps (FlexRIC's xApp interface: E2AP-style messages relayed by the RIC) |
| KPM xApp | `xapp_kpm_csv` | client → **36422** | E42 over SCTP; writes a CSV file |

The message flow:

1. **E2 setup.** The gNB sends an E2 Setup Request to the RIC when it starts. The request names RAN function 2 (E2SM-KPM), its report styles and its metric names. The RIC answers with an E2 Setup Response.
2. **xApp connects.** The xApp sends an E42 Setup to the RIC. The RIC answers with the list of connected E2 nodes and their RAN functions.
3. **Subscriptions.** The xApp sends two RIC Subscription Requests, which the RIC forwards to the gNB. Each has:
   - event trigger: periodic, every 1000 ms;
   - action definition: **style 1** (format 1) = cell-level metrics, or **style 4** (format 4, a condition the gNB applies to all UEs) = per-UE metrics.

   The gNB answers each with a RIC Subscription Response.
4. **Indications.** Every period, the gNB sends one RIC Indication per subscription. Its header carries the collection start time; its message is format 1 for style 1, or format 3 with one block per UE for style 4. The RIC forwards it to the xApp, which writes one CSV row per metric (and per UE).
5. **Ctrl-C** in the xApp sends a Subscription Delete for each subscription.

**Versions.**

- **gNB:** your gNB is srsRAN\_Project\_jbpf at `cd6811af`, which is srsRAN 25.10. Its E2 code uses the E2AP v03.00 ASN.1 and E2SM-KPM v3 structures.
- **FlexRIC:** v3.0.0 (12 Oct 2026, [github.com/duranta-project/flexric](https://github.com/duranta-project/flexric), the project's new home), built with the **E2AP v2.03 codec and E2SM-KPM v3.00**. This is the combination the srsRAN / OCUDU FlexRIC tutorial documents: KPM v3.00 with FlexRIC's default E2AP.
- **Fallback:** if the RIC cannot decode the gNB's E2 Setup, the build script rebuilds with `--e2ap 3` (FlexRIC's E2AP v3.01 codec).

**Why a new xApp instead of FlexRIC's `xapp_kpm_moni`:**

- `xapp_kpm_moni` subscribes to every report style the E2 node advertises, but it only has handlers for styles 1 and 4. srsRAN advertises styles 1–5, so the stock xApp calls an empty handler for style 2 and crashes. FlexRIC fixed this in its development branch on 20 Oct 2026, after v3.0.0.
- `xapp_kpm_moni` also treats the collection start time as microseconds, while srsRAN sends an NTP timestamp.

`xapp_kpm_csv` subscribes only to styles 1 and 4, converts the NTP time, accepts indications with or without the optional measurement-name list, and writes CSV.

**What srsRAN 25.10 offers over E2SM-KPM** (DU agent; source: `lib/e2/e2sm/e2sm_kpm/e2sm_kpm_du_meas_provider_impl.cpp`):

| Metric | Level | Unit in the CSV | Meaning |
| --- | --- | --- | --- |
| `RRU.PrbUsedDl`, `RRU.PrbUsedUl` | cell, UE | PRB (mean per slot) | PRBs used |
| `RRU.PrbAvailDl`, `RRU.PrbAvailUl` | cell, UE | PRB | PRBs left free |
| `RRU.PrbTotDl`, `RRU.PrbTotUl` | cell, UE | % | PRB utilisation |
| `DRB.UEThpDl`, `DRB.UEThpUl` | cell, UE | kbit/s | RLC-level throughput |
| `DRB.RlcSduTransmittedVolumeDL`, `...UL` | cell, UE | kbit | RLC SDU volume in the period |
| `DRB.RlcPacketDropRateDl` | cell, UE | % | RLC SDUs dropped |
| `DRB.RlcSduDelayDl` | cell, UE | 0.1 ms | RLC SDU latency, downlink |
| `DRB.AirIfDelayUl`, `DRB.RlcDelayUl` | cell, UE | 0.1 ms | uplink delays |
| `RACH.PreambleDedCell` | cell | count | PRACH preambles received |
| `CQI` | UE (not advertised; accepted when requested) | index 0–15 | mean CQI |
| `RSRP`, `RSRQ` | UE (not advertised) | dB | in srsRAN 25.10 both return the **PUSCH SNR**, not RSRP/RSRQ |

**Four facts about these numbers:**

1. **Period.** The smallest period srsRAN supports is 1 s.
2. **Values.** Each indication carries values computed from the gNB's latest scheduler report (every 0.5 s after B.0) and its RLC metrics. They are not an average over the whole second.
3. **UE ID.** The UE is identified by its **gNB-CU UE F1AP ID** (0, 1, 2, … in attach order), not by IMSI or RNTI. Compare `DRB.UEThpDl` with the per-UE rates of `intact-multiue status` or `intact-multiue iperf` to tell them apart.
4. **Prerequisites.** Style 4 lists a UE only once the gNB has RLC metrics for it, which `intact-e2-config` switches on (`metrics.layers.enable_rlc`).

**Verification done on 30 Oct:**

- The xApp source compiles without warnings (`-Wall -Wextra`) against FlexRIC v3.0.0's headers, for both E2AP v2 and v3.
- Its CSV writer was run on synthetic KPM indications and wrote the expected rows. The indications covered format 1 and format 3 with two UEs, with and without the measurement-name list, an NTP collection time, and a "no value" record.
- `intact-e2-config` was run on a copy of your gNB config. It added only the `e2` and `pcap` sections.

What was not run: the RIC and the gNB together. That environment has no SCTP and cannot build them. Every script therefore checks each step and names the exact fix if something differs.

### F.1 What runs where, and when

| Step | Command | State required | Window |
| --- | --- | --- | --- |
| F.2 Build FlexRIC and the xApp | paste the block; it runs `intact-flexric-build` | stack **down** (the build needs the CPU and replaces the RIC binaries); 15–25 min | any (T6) |
| F.3 gNB config with E2 | paste the block; it runs `intact-e2-config` | gNB **down** (validated with `gnb --dryrun`) | T6 |
| F.4 Launchers and summary | paste the block | any; it only writes files | T6 |
| F.5 Run | T0 `intact-run-ric` → T2 → T3 `intact-run-gnb $CFG/gnb_zmq_jbpf_e2.yaml` → T4 → T5 → T7 `intact-run-kpm-xapp` | the RIC **before** the gNB; the xApp **after** the UE(s) attach | two new windows: **T0** (RIC) and **T7** (xApp) |
| F.6 Read the CSV | `intact-kpm-summary` | any | T6 |
| F.7 Stop | Ctrl-C in T7, then `intact-down` | any | any |

The windows with E2:

| Window | Command | Ready when you see |
| --- | --- | --- |
| T0 (new) | `intact-run-ric` | `[iApp]: nearRT-RIC IP Address = 127.0.0.1, PORT = 36422`. Later, when the gNB starts: `[E2AP]: E2 SETUP-REQUEST rx from PLMN 00101 ...` and `[NEAR-RIC]: Accepting RAN function ID 2 with def = ORAN-E2SM-KPM` |
| T2 | `intact-run-jrtc` | as before |
| T3 | `intact-run-gnb $CFG/gnb_zmq_jbpf_e2.yaml` | `E2AP: Connection to Near-RT-RIC on 127.0.0.1:36421 completed`, then `N2: Connection to AMF ... completed` and `==== gNB started ===` |
| T4 | `intact-run-proxy` | as before |
| T5 | `intact-run-ue`, or `intact-multiue up` (Appendix E) | an IP for each UE |
| T7 (new) | `intact-run-kpm-xapp` | `[kpm-csv] SUBSCRIBED (2)`, then one line per indication |
| T6 | `intact-kpm-summary`, `tail -f ~/intact/logs/e2/kpm-latest.csv` | — |

### F.2 Build FlexRIC and the xApp — \[SCRIPT\], stack down, 15–25 min

There are two blocks:

- **Block 1** stores the xApp's C source in `~/intact/configs/flexric/xapp_kpm_csv.c`. The configs folder is a git repository, so the source is versioned with your configs.
- **Block 2** runs `intact-down`, then creates `intact-flexric-build` and runs it. It is safe to rerun. If the build is interrupted, run `intact-flexric-build` again.

What the script does:

0. **Pre-checks:**
   - no gNB, srsUE, nearRT-RIC or xApp running;
   - `sudo` works;
   - gcc is 10, 12 or 13; FlexRIC refuses gcc 11, and Ubuntu 24.04 has 13.
1. **Packages:** installs the build packages with apt: `cmake`, `libsctp-dev`, `libpcre2-dev`, and autotools, `bison` and `flex` for asn1c.
2. **asn1c:** installs the ASN.1 compiler FlexRIC pins (mouse07410/asn1c, commit `940dd5fa`) under `/opt/asn1c`.
   - Every FlexRIC xApp links an NR RRC library that is generated with this compiler at build time.
   - It is built in a temporary folder, and only `make install` runs as root.
   - Skipped when the same commit is already installed.
3. **FlexRIC source:** clones FlexRIC into `~/intact/src/flexric` from GitHub (GitLab as fallback), checks out tag `v3.0.0` and verifies the commit `18c33250…`.
4. **The xApp:** copies the source from block 1 into `examples/xApp/c/monitor/xapp_kpm_csv.c` and appends a 6-line CMake target to that folder's `CMakeLists.txt`. That append is the only change to FlexRIC's own files, and `git -C ~/intact/src/flexric diff` shows it.
5. **Configure:** Release, E2AP v2 (or v3 with `--e2ap 3`), E2SM-KPM v3.00, no unit tests, install prefix `~/intact/opt/flexric`.
6. **Build only what is needed:** `nearRT-RIC`, the service-model libraries and `xapp_kpm_csv`. The other example xApps are not built.
7. **Install:** the service models go into `~/intact/opt/flexric/lib/flexric/`. Nothing of FlexRIC goes to `/usr/local`.
8. **Configuration:**
   - writes `~/intact/configs/flexric/flexric.conf`: RIC address 127.0.0.1; the xApp's own SQLite copy of the data in `~/intact/logs/flexric/`;
   - writes `~/intact/configs/flexric/paths.env`, which the launchers read.
9. **Verify:** runs the RIC once with `-h`, and records the versions and the CMake diff in `~/intact/configs/provenance/flexric.txt`.

**Block 1: the xApp source** (about 430 lines of C).

```bash
mkdir -p ~/intact/configs/flexric
cat > ~/intact/configs/flexric/xapp_kpm_csv.c <<'XEOF'
/*
 * xapp_kpm_csv.c - INTACT runbook, Appendix F.
 *
 * Subscribes to E2SM-KPM on every connected E2 node that offers it (the srsRAN gNB DU) and writes
 * every measurement it receives to a CSV file, one row per (indication, UE, metric).
 *
 *   Report style 1 = E2 node (cell) level measurements      -> rows with an empty ue_id
 *   Report style 4 = common-condition UE-level measurements -> one row per UE and metric
 *
 * Unlike the stock xapp_kpm_moni it subscribes ONLY to the styles it can encode (1 and 4); the srsRAN
 * gNB also advertises styles 2, 3 and 5, for which the stock xApp has no handler and crashes.
 *
 * Settings (environment variables; FlexRIC itself parses the command-line flags -c -p -a -d -n):
 *   KPM_CSV          output file, appended; header written when the file is new  (default /tmp/xapp_kpm.csv)
 *   KPM_PERIOD_MS    report period = granularity period, ms; srsRAN minimum 1000  (default 1000)
 *   KPM_STYLES       which styles to subscribe to                                 (default "1,4")
 *   KPM_CELL_METRICS style-1 metric names, comma separated, or "all"              (default all advertised)
 *   KPM_UE_METRICS   style-4 metric names, comma separated, or "all"              (default all advertised + CQI)
 *   KPM_WAIT_S       seconds to wait for an E2 node with KPM to appear            (default 30)
 *   XAPP_DURATION    seconds to run (FlexRIC); default: until Ctrl-C
 */
#include "../../../../src/xApp/e42_xapp_api.h"
#include "../../../../src/util/alg_ds/alg/defer.h"
#include "../../../../src/util/byte_array.h"
#include "../../../../src/util/ngran_types.h"
#include "../../../../src/util/e.h"

#include <assert.h>
#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define KPM_RAN_FUNC_ID 2
#define MAX_NAMES 64

static pthread_mutex_t mtx = PTHREAD_MUTEX_INITIALIZER;
static FILE* csv = NULL;
static char node_str[96] = "unknown";
static uint64_t n_ind[6] = {0};
static uint64_t n_rows = 0;
static uint32_t period_ms = 1000;

/* srsRAN units (lib/e2/e2sm/e2sm_kpm/e2sm_kpm_du_meas_provider_impl.cpp) */
static const char* unit_of(const char* m)
{
  static const char* map[][2] = {
      {"RRU.PrbUsedDl", "PRB"}, {"RRU.PrbUsedUl", "PRB"}, {"RRU.PrbAvailDl", "PRB"}, {"RRU.PrbAvailUl", "PRB"},
      {"RRU.PrbTotDl", "%"}, {"RRU.PrbTotUl", "%"}, {"DRB.UEThpDl", "kbit/s"}, {"DRB.UEThpUl", "kbit/s"},
      {"DRB.RlcSduTransmittedVolumeDL", "kbit"}, {"DRB.RlcSduTransmittedVolumeUL", "kbit"},
      {"DRB.RlcPacketDropRateDl", "%"}, {"DRB.RlcSduDelayDl", "0.1ms"}, {"DRB.AirIfDelayUl", "0.1ms"},
      {"DRB.RlcDelayUl", "0.1ms"}, {"RACH.PreambleDedCell", "count"}, {"CQI", "index"},
      {"RSRP", "dB(PUSCH SNR)"}, {"RSRQ", "dB(PUSCH SNR)"}};
  for (size_t i = 0; i < sizeof(map) / sizeof(map[0]); i++)
    if (strcmp(map[i][0], m) == 0)
      return map[i][1];
  return "";
}

/* collectStartTime: srsRAN sends a 64-bit NTP timestamp (seconds since 1900 << 32 | fraction);
 * FlexRIC's own agents send microseconds since 1970. Returns Unix seconds. */
static double collect_to_unix(uint64_t v)
{
  const uint64_t ntp_off = 2208988800ULL;
  if ((v >> 32) > ntp_off && (v >> 32) < 6000000000ULL)
    return (double)((v >> 32) - ntp_off) + (double)(v & 0xffffffffULL) / 4294967296.0;
  if (v > 100000000000000ULL) return (double)v / 1e6; /* microseconds */
  if (v > 100000000000ULL) return (double)v / 1e3;    /* milliseconds */
  return (double)v;
}

static void ue_id_str(ue_id_e2sm_t const* u, char* out, size_t n)
{
  switch (u->type) {
    case GNB_DU_UE_ID_E2SM:
      if (u->gnb_du.ran_ue_id != NULL)
        snprintf(out, n, "du_f1ap:%u/ran:%" PRIu64, u->gnb_du.gnb_cu_ue_f1ap, *u->gnb_du.ran_ue_id);
      else
        snprintf(out, n, "du_f1ap:%u", u->gnb_du.gnb_cu_ue_f1ap);
      break;
    case GNB_UE_ID_E2SM:
      if (u->gnb.ran_ue_id != NULL)
        snprintf(out, n, "amf_ngap:%" PRIu64 "/ran:%" PRIu64, u->gnb.amf_ue_ngap_id, *u->gnb.ran_ue_id);
      else
        snprintf(out, n, "amf_ngap:%" PRIu64, u->gnb.amf_ue_ngap_id);
      break;
    case GNB_CU_UP_UE_ID_E2SM:
      snprintf(out, n, "cuup_e1ap:%u", u->gnb_cu_up.gnb_cu_cp_ue_e1ap);
      break;
    default:
      snprintf(out, n, "ue_type_%d", (int)u->type);
  }
}

static void meas_name(meas_type_t const* t, char* out, size_t n)
{
  if (t->type == NAME_MEAS_TYPE) {
    char* s = cp_ba_to_str(t->name);
    snprintf(out, n, "%s", s);
    free(s);
  } else {
    snprintf(out, n, "meas_id_%u", (unsigned)t->id);
  }
}

static void write_frm1(kpm_ind_msg_format_1_t const* m, kpm_act_def_format_1_t const* req, int style,
                       const char* ue, const char* tw_iso, double tw, double tm, double tc, int* first_val,
                       char* summary, size_t summary_len)
{
  uint32_t gp = (m->gran_period_ms != NULL) ? *m->gran_period_ms : (req ? req->gran_period_ms : 0);
  for (size_t j = 0; j < m->meas_data_lst_len; j++) {
    meas_data_lst_t const* d = &m->meas_data_lst[j];
    int incomplete = (d->incomplete_flag != NULL && *d->incomplete_flag == TRUE_ENUM_VALUE) ? 1 : 0;
    for (size_t i = 0; i < d->meas_record_len; i++) {
      char name[128];
      if (m->meas_info_lst_len == d->meas_record_len)
        meas_name(&m->meas_info_lst[i].meas_type, name, sizeof(name));
      else if (req != NULL && req->meas_info_lst_len == d->meas_record_len)
        meas_name(&req->meas_info_lst[i].meas_type, name, sizeof(name));
      else
        snprintf(name, sizeof(name), "record_%zu", i);
      meas_record_lst_t const* r = &d->meas_record_lst[i];
      char val[48] = "";
      const char* vt = "none";
      if (r->value == INTEGER_MEAS_VALUE) {
        snprintf(val, sizeof(val), "%u", r->int_val);
        vt = "int";
      } else if (r->value == REAL_MEAS_VALUE) {
        snprintf(val, sizeof(val), "%.6g", r->real_val);
        vt = "real";
      }
      fprintf(csv, "%s,%.6f,%.6f,%s,%d,%s,%s,%s,%s,%s,%.6f,%u,%d\n", tw_iso, tw, tm, node_str, style, ue, name, val,
              vt, unit_of(name), tc, gp, incomplete);
      n_rows++;
      if (*first_val && val[0] != '\0' && (strcmp(name, "DRB.UEThpDl") == 0 || strcmp(name, "RRU.PrbTotDl") == 0)) {
        size_t l = strlen(summary);
        snprintf(summary + l, summary_len - l, " %s%s%s=%s%s", ue[0] ? ue : "", ue[0] ? ":" : "", name, val,
                 unit_of(name));
      }
    }
  }
}

static void sm_cb_kpm(sm_ag_if_rd_t const* rd)
{
  if (rd == NULL || rd->type != INDICATION_MSG_AGENT_IF_ANS_V0 || rd->ind.type != KPM_STATS_V3_0)
    return;
  kpm_ind_data_t const* ind = &rd->ind.kpm.ind;
  kpm_act_def_t const* ad = rd->ind.kpm.act_def;
  kpm_act_def_format_1_t const* req = NULL;
  int style = 1;
  if (ad != NULL && ad->type == FORMAT_4_ACTION_DEFINITION) {
    req = &ad->frm_4.action_def_format_1;
    style = 4;
  } else if (ad != NULL && ad->type == FORMAT_1_ACTION_DEFINITION) {
    req = &ad->frm_1;
  }
  struct timespec rt, mt;
  clock_gettime(CLOCK_REALTIME, &rt);
  clock_gettime(CLOCK_MONOTONIC, &mt);
  double tw = rt.tv_sec + rt.tv_nsec / 1e9, tm = mt.tv_sec + mt.tv_nsec / 1e9;
  char iso[96];
  struct tm g;
  gmtime_r(&rt.tv_sec, &g);
  snprintf(iso, sizeof(iso), "%04d-%02d-%02dT%02d:%02d:%02d.%03ldZ", g.tm_year + 1900, g.tm_mon + 1, g.tm_mday,
           g.tm_hour, g.tm_min, g.tm_sec, (long)(rt.tv_nsec / 1000000));
  double tc = 0;
  if (ind->hdr.type == FORMAT_1_INDICATION_HEADER)
    tc = collect_to_unix(ind->hdr.kpm_ric_ind_hdr_format_1.collectStartTime);

  char summary[512] = "";
  int show = 1;
  size_t n_ue = 0;
  pthread_mutex_lock(&mtx);
  if (ind->msg.type == FORMAT_1_INDICATION_MESSAGE) {
    write_frm1(&ind->msg.frm_1, req, style, "", iso, tw, tm, tc, &show, summary, sizeof(summary));
  } else if (ind->msg.type == FORMAT_3_INDICATION_MESSAGE) {
    n_ue = ind->msg.frm_3.ue_meas_report_lst_len;
    for (size_t u = 0; u < n_ue; u++) {
      char ue[64];
      ue_id_str(&ind->msg.frm_3.meas_report_per_ue[u].ue_meas_report_lst, ue, sizeof(ue));
      write_frm1(&ind->msg.frm_3.meas_report_per_ue[u].ind_msg_format_1, req, style, ue, iso, tw, tm, tc, &show,
                 summary, sizeof(summary));
    }
  } else {
    printf("[kpm-csv] indication message format %d not handled\n", (int)ind->msg.type);
  }
  fflush(csv);
  n_ind[style]++;
  uint64_t ni = n_ind[style], nr = n_rows;
  pthread_mutex_unlock(&mtx);
  if (style == 4)
    printf("[kpm-csv] %s style 4 #%" PRIu64 ": %zu UE(s)%s | rows %" PRIu64 "\n", iso, ni, n_ue, summary, nr);
  else
    printf("[kpm-csv] %s style 1 #%" PRIu64 ":%s | rows %" PRIu64 "\n", iso, ni, summary, nr);
  fflush(stdout);
}

/* --------------------------------------------------------------------------- subscription building */
static int split_names(const char* env, char names[][64], int max)
{
  int n = 0;
  if (env == NULL || env[0] == '\0' || strcmp(env, "all") == 0)
    return -1;
  char* copy = strdup(env);
  char* save = NULL;
  for (char* tok = strtok_r(copy, ", ", &save); tok != NULL && n < max; tok = strtok_r(NULL, ", ", &save))
    snprintf(names[n++], 64, "%s", tok);
  free(copy);
  return n;
}

static label_info_lst_t no_label(void)
{
  label_info_lst_t l = {0};
  l.noLabel = ecalloc(1, sizeof(enum_value_e));
  *l.noLabel = TRUE_ENUM_VALUE;
  return l;
}

static kpm_act_def_format_1_t frm1_for(char names[][64], int n)
{
  kpm_act_def_format_1_t f = {0};
  f.meas_info_lst_len = n;
  f.meas_info_lst = ecalloc(n, sizeof(meas_info_format_1_lst_t));
  for (int i = 0; i < n; i++) {
    f.meas_info_lst[i].meas_type.type = NAME_MEAS_TYPE;
    f.meas_info_lst[i].meas_type.name = cp_str_to_ba(names[i]);
    f.meas_info_lst[i].label_info_lst_len = 1;
    f.meas_info_lst[i].label_info_lst = ecalloc(1, sizeof(label_info_lst_t));
    f.meas_info_lst[i].label_info_lst[0] = no_label();
  }
  f.gran_period_ms = period_ms;
  f.cell_global_id = NULL;
  f.meas_bin_range_info_lst_len = 0;
  f.meas_bin_info_lst = NULL;
  return f;
}

/* srsRAN returns every UE for any condition; the condition below (S-NSSAI SST == 1) is the one the
 * stock FlexRIC xApp encodes, so it is known to round-trip through FlexRIC's KPM encoder. */
static test_info_lst_t sst_equals(int sst)
{
  test_info_lst_t t = {0};
  t.test_cond_type = S_NSSAI_TEST_COND_TYPE;
  t.S_NSSAI = TRUE_TEST_COND_TYPE;
  t.test_cond = ecalloc(1, sizeof(test_cond_e));
  *t.test_cond = EQUAL_TEST_COND;
  t.test_cond_value = ecalloc(1, sizeof(test_cond_value_t));
  t.test_cond_value->type = OCTET_STRING_TEST_COND_VALUE;
  t.test_cond_value->octet_string_value = ecalloc(1, sizeof(byte_array_t));
  t.test_cond_value->octet_string_value->len = 1;
  t.test_cond_value->octet_string_value->buf = ecalloc(1, sizeof(uint8_t));
  t.test_cond_value->octet_string_value->buf[0] = (uint8_t)sst;
  return t;
}

static ric_report_style_item_t const* find_style(kpm_ran_function_def_t const* d, int style)
{
  for (size_t i = 0; i < d->sz_ric_report_style_list; i++)
    if ((int)d->ric_report_style_list[i].report_style_type == style - 1) /* enum is 0-based */
      return &d->ric_report_style_list[i];
  return NULL;
}

static int names_for(ric_report_style_item_t const* it, const char* env, int add_cqi, char names[][64])
{
  int n = split_names(env, names, MAX_NAMES);
  if (n >= 0)
    return n;
  n = 0;
  for (size_t i = 0; i < it->meas_info_for_action_lst_len && n < MAX_NAMES; i++) {
    char* s = cp_ba_to_str(it->meas_info_for_action_lst[i].name);
    snprintf(names[n++], 64, "%s", s);
    free(s);
  }
  if (add_cqi && n < MAX_NAMES) {
    int have = 0;
    for (int i = 0; i < n; i++)
      have |= (strcmp(names[i], "CQI") == 0);
    if (!have)
      snprintf(names[n++], 64, "CQI");
  }
  return n;
}

int main(int argc, char* argv[])
{
  const char* path = getenv("KPM_CSV") ? getenv("KPM_CSV") : "/tmp/xapp_kpm.csv";
  if (getenv("KPM_PERIOD_MS")) period_ms = (uint32_t)atoi(getenv("KPM_PERIOD_MS"));
  if (period_ms < 100) period_ms = 1000;
  const char* styles = getenv("KPM_STYLES") ? getenv("KPM_STYLES") : "1,4";
  int want1 = strchr(styles, '1') != NULL, want4 = strchr(styles, '4') != NULL;
  int wait_s = getenv("KPM_WAIT_S") ? atoi(getenv("KPM_WAIT_S")) : 30;

  struct stat st;
  int new_file = (stat(path, &st) != 0 || st.st_size == 0);
  csv = fopen(path, "a");
  if (csv == NULL) {
    fprintf(stderr, "[kpm-csv] cannot open %s: %s\n", path, strerror(errno));
    return 1;
  }
  if (new_file)
    fprintf(csv, "t_wall_utc,t_wall_s,t_mono_s,e2_node,style,ue_id,metric,value,value_type,unit,"
                 "collect_start_unix_s,gran_period_ms,incomplete\n");
  fflush(csv);
  printf("[kpm-csv] writing %s (period %u ms, styles %s)\n", path, period_ms, styles);

  fr_args_t args = init_fr_args(argc, argv);
  init_xapp_api(&args); /* returns once connected to the nearRT-RIC (E42) */
  printf("[kpm-csv] connected to the nearRT-RIC\n");

  e2_node_arr_xapp_t nodes = {0};
  int kpm_nodes = 0;
  for (int t = 0; t <= wait_s; t++) {
    nodes = e2_nodes_xapp_api();
    for (size_t i = 0; i < nodes.len; i++)
      for (size_t r = 0; r < nodes.n[i].len_rf; r++)
        if (nodes.n[i].rf[r].id == KPM_RAN_FUNC_ID && nodes.n[i].rf[r].defn.type == KPM_RAN_FUNC_DEF_E)
          kpm_nodes++;
    if (kpm_nodes > 0)
      break;
    free_e2_node_arr_xapp(&nodes);
    if (t == 0)
      printf("[kpm-csv] no E2 node with KPM yet; waiting up to %d s (is the gNB running with E2?)\n", wait_s);
    sleep(1);
  }
  if (kpm_nodes == 0) {
    fprintf(stderr, "[kpm-csv] STOP: no E2 node offers E2SM-KPM (RAN function %d). Start the RIC first, then the "
                    "gNB with the E2 config, then this xApp.\n", KPM_RAN_FUNC_ID);
    while (try_stop_xapp_api() == false) usleep(1000);
    fclose(csv);
    return 2;
  }
  printf("[kpm-csv] E2 nodes connected: %d\n", nodes.len);

  sm_ans_xapp_t h[16];
  int nh = 0;
  for (size_t i = 0; i < nodes.len && nh < 14; i++) {
    e2_node_connected_xapp_t* n = &nodes.n[i];
    snprintf(node_str, sizeof(node_str), "%03u-%0*u/%s/nb%u/du%" PRIu64, n->id.plmn.mcc, n->id.plmn.mnc_digit_len,
             n->id.plmn.mnc, get_ngran_name(n->id.type), n->id.nb_id.nb_id, n->id.cu_du_id ? *n->id.cu_du_id : 0);
    for (size_t r = 0; r < n->len_rf; r++) {
      if (n->rf[r].id != KPM_RAN_FUNC_ID || n->rf[r].defn.type != KPM_RAN_FUNC_DEF_E)
        continue;
      kpm_ran_function_def_t const* def = &n->rf[r].defn.kpm;
      printf("[kpm-csv] node %s advertises %zu KPM report style(s):\n", node_str, def->sz_ric_report_style_list);
      for (size_t s = 0; s < def->sz_ric_report_style_list; s++) {
        ric_report_style_item_t const* it = &def->ric_report_style_list[s];
        printf("           style %d (%zu metrics):", (int)it->report_style_type + 1, it->meas_info_for_action_lst_len);
        for (size_t k = 0; k < it->meas_info_for_action_lst_len; k++) {
          char* nm = cp_ba_to_str(it->meas_info_for_action_lst[k].name);
          printf(" %s", nm);
          free(nm);
        }
        printf("\n");
      }
      for (int style = 1; style <= 4; style += 3) {
        if ((style == 1 && !want1) || (style == 4 && !want4))
          continue;
        ric_report_style_item_t const* it = find_style(def, style);
        if (it == NULL) {
          printf("[kpm-csv] WARNING: style %d not advertised by %s; skipped\n", style, node_str);
          continue;
        }
        char names[MAX_NAMES][64];
        int nn = names_for(it, getenv(style == 1 ? "KPM_CELL_METRICS" : "KPM_UE_METRICS"), style == 4, names);
        if (nn <= 0) {
          printf("[kpm-csv] WARNING: no metrics for style %d; skipped\n", style);
          continue;
        }
        kpm_sub_data_t sub = {0};
        sub.ev_trg_def.type = FORMAT_1_RIC_EVENT_TRIGGER;
        sub.ev_trg_def.kpm_ric_event_trigger_format_1.report_period_ms = period_ms;
        sub.sz_ad = 1;
        sub.ad = ecalloc(1, sizeof(kpm_act_def_t));
        if (style == 1) {
          sub.ad[0].type = FORMAT_1_ACTION_DEFINITION;
          sub.ad[0].frm_1 = frm1_for(names, nn);
        } else {
          sub.ad[0].type = FORMAT_4_ACTION_DEFINITION;
          sub.ad[0].frm_4.matching_cond_lst_len = 1;
          sub.ad[0].frm_4.matching_cond_lst = ecalloc(1, sizeof(matching_condition_format_4_lst_t));
          sub.ad[0].frm_4.matching_cond_lst[0].test_info_lst = sst_equals(1);
          sub.ad[0].frm_4.action_def_format_1 = frm1_for(names, nn);
        }
        printf("[kpm-csv] subscribing style %d, %d metric(s):", style, nn);
        for (int k = 0; k < nn; k++)
          printf(" %s", names[k]);
        printf("\n");
        h[nh] = report_sm_xapp_api(&n->id, KPM_RAN_FUNC_ID, &sub, sm_cb_kpm);
        if (h[nh].success) {
          printf("[kpm-csv] style %d subscription accepted\n", style);
          nh++;
        } else {
          printf("[kpm-csv] WARNING: style %d subscription failed\n", style);
        }
        free_kpm_sub_data(&sub);
      }
    }
  }
  if (nh == 0) {
    fprintf(stderr, "[kpm-csv] STOP: no subscription accepted\n");
    while (try_stop_xapp_api() == false) usleep(1000);
    free_e2_node_arr_xapp(&nodes);
    fclose(csv);
    return 3;
  }
  printf("[kpm-csv] SUBSCRIBED (%d). Rows go to %s. Ctrl-C to stop.\n", nh, path);
  fflush(stdout);

  xapp_wait_end_api();

  for (int i = 0; i < nh; i++)
    rm_report_sm_xapp_api(h[i].u.handle);
  while (try_stop_xapp_api() == false)
    usleep(1000);
  free_e2_node_arr_xapp(&nodes); /* kept until now: the subscriptions refer to the node ids */
  pthread_mutex_lock(&mtx);
  fclose(csv);
  csv = NULL;
  pthread_mutex_unlock(&mtx);
  printf("[kpm-csv] stopped: %" PRIu64 " style-1 and %" PRIu64 " style-4 indications, %" PRIu64 " rows in %s\n",
         n_ind[1], n_ind[4], n_rows, path);
  return 0;
}
XEOF
wc -l ~/intact/configs/flexric/xapp_kpm_csv.c
```

**Expected:** `431 /home/kamrul/intact/configs/flexric/xapp_kpm_csv.c`.

**Block 2: build.**

```bash
source ~/intact/env.sh
intact-down
cat > ~/intact/bin/intact-flexric-build <<'FREOF'
#!/usr/bin/env bash
# intact-flexric-build [--e2ap 2|3]: FlexRIC v3.0.0 (nearRT-RIC + service models) and the INTACT KPM-to-CSV
# xApp, installed under ~/intact/opt/flexric (nothing of FlexRIC goes to /usr/local). Safe to rerun.
#   --e2ap 2 (default): E2AP v2.03 codec + E2SM-KPM v3.00, the combination documented for srsRAN/OCUDU + FlexRIC.
#   --e2ap 3          : E2AP v3.01 codec + E2SM-KPM v3.00 (fallback if the RIC cannot decode the gNB's E2 Setup).
# Also installs the asn1c compiler FlexRIC pins (mouse07410/asn1c 940dd5fa) under /opt/asn1c: every FlexRIC xApp
# links an NR RRC library that is generated with it.
set -euo pipefail
source ~/intact/env.sh
E2AP=2
while [ $# -gt 0 ]; do case "$1" in --e2ap) E2AP="${2:-}"; shift 2 ;; *) echo "unknown option $1"; exit 1 ;; esac; done
[[ "$E2AP" =~ ^[23]$ ]] || { echo "STOP: --e2ap must be 2 or 3"; exit 1; }
TAG=v3.0.0; COMMIT=18c3325091ad6890a706ebb2bba38d3c0fb741e5
ASN1C_COMMIT=940dd5fa9f3917913fd487b13dfddfacd0ded06e
SRC="$INTACT/src/flexric"; B="$SRC/build"; PREFIX="$INTACT/opt/flexric"
J="${JOBS:-4}"

echo "=== 0. Pre-checks"
if pgrep -x 'gnb|srsue|nearRT-RIC|xapp_kpm_csv' >/dev/null; then
  pgrep -ax 'gnb|srsue|nearRT-RIC|xapp_kpm_csv'
  echo "STOP: stop the stack first (intact-down): the build needs the CPU and replaces the RIC and xApp binaries"; exit 1
fi
sudo -v
GV=$(gcc -dumpversion | cut -d. -f1)
[ "$GV" = 11 ] && { echo "STOP: gcc 11 is not supported by FlexRIC (Ubuntu 24.04 has gcc 13: check 'gcc --version')"; exit 1; }
[ "$GV" -lt 10 ] && { echo "STOP: gcc $GV is too old for FlexRIC (10, 12 or 13 needed)"; exit 1; }
echo "  [ OK ] gcc $(gcc -dumpversion), cmake $(cmake --version | head -1 | awk '{print $3}'), $J build jobs"

echo "=== 1. Packages"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y build-essential cmake git libsctp-dev libpcre2-dev \
  autoconf automake libtool m4 bison flex >"$LOGS/apt-flexric.log" 2>&1 \
  || { echo "STOP: apt-get failed: tail -20 $LOGS/apt-flexric.log"; exit 1; }
echo "  [ OK ] build packages"

echo "=== 2. asn1c (mouse07410 fork, commit ${ASN1C_COMMIT:0:8}) in /opt/asn1c"
if [ -x /opt/asn1c/bin/asn1c ] && [ -f /opt/asn1c/.intact-commit ] && [ "$(cat /opt/asn1c/.intact-commit)" = "$ASN1C_COMMIT" ]; then
  echo "  [ OK ] already installed: $(/opt/asn1c/bin/asn1c -v 2>&1 | head -1)"
else
  T=$(mktemp -d); trap 'rm -rf "${T:?}"' EXIT
  git clone -q https://github.com/mouse07410/asn1c "$T/asn1c"
  git -C "$T/asn1c" checkout -q "$ASN1C_COMMIT"
  ( cd "$T/asn1c" && autoreconf -iv >"$LOGS/build-asn1c.log" 2>&1 && ./configure --prefix=/opt/asn1c >>"$LOGS/build-asn1c.log" 2>&1 \
      && make -j"$J" >>"$LOGS/build-asn1c.log" 2>&1 ) || { echo "STOP: asn1c build failed: tail -30 $LOGS/build-asn1c.log"; exit 1; }
  ( cd "$T/asn1c" && sudo make install >>"$LOGS/build-asn1c.log" 2>&1 ) || { echo "STOP: asn1c install failed: tail -30 $LOGS/build-asn1c.log"; exit 1; }
  echo "$ASN1C_COMMIT" | sudo tee /opt/asn1c/.intact-commit >/dev/null
  echo "  [ OK ] installed: $(/opt/asn1c/bin/asn1c -v 2>&1 | head -1)"
fi

echo "=== 3. FlexRIC $TAG source in $SRC"
if [ ! -d "$SRC/.git" ]; then
  git clone -q https://github.com/duranta-project/flexric.git "$SRC" 2>/dev/null \
    || git clone -q https://gitlab.eurecom.fr/mosaic5g/flexric.git "$SRC" \
    || { echo "STOP: cannot clone FlexRIC from GitHub or GitLab (network?)"; exit 1; }
fi
git -C "$SRC" fetch -q --tags origin 2>/dev/null || true
git -C "$SRC" checkout -q -- examples/xApp/c/monitor/CMakeLists.txt 2>/dev/null || true   # drop an earlier INTACT append
git -C "$SRC" checkout -q "$TAG" || { echo "STOP: tag $TAG not found in $SRC"; exit 1; }
[ "$(git -C "$SRC" rev-parse HEAD)" = "$COMMIT" ] || { echo "STOP: $TAG is $(git -C "$SRC" rev-parse HEAD), expected $COMMIT"; exit 1; }
echo "  [ OK ] FlexRIC at $TAG (${COMMIT:0:10})"

echo "=== 4. The INTACT xApp: examples/xApp/c/monitor/xapp_kpm_csv.c"
X="$CFG/flexric/xapp_kpm_csv.c"
[ -f "$X" ] || { echo "STOP: $X missing: paste the F.2 xApp block first"; exit 1; }
cp "$X" "$SRC/examples/xApp/c/monitor/xapp_kpm_csv.c"
cat >> "$SRC/examples/xApp/c/monitor/CMakeLists.txt" <<'CMEOF'

# ---- INTACT: xapp_kpm_csv (runbook Appendix F) ----
add_executable(xapp_kpm_csv
               xapp_kpm_csv.c
               ../../../../src/util/alg_ds/alg/defer.c
              )
target_link_libraries(xapp_kpm_csv PUBLIC e42_xapp -pthread -lsctp -ldl)
CMEOF
git -C "$SRC" diff --stat
echo "  [ OK ] xApp source and CMake target added (git -C $SRC diff shows the CMake lines)"

echo "=== 5. Configure (E2AP v$E2AP, E2SM-KPM v3.00, Release)"
rm -rf "${B:?}"
cmake -S "$SRC" -B "$B" -DCMAKE_BUILD_TYPE=Release -DE2AP_VERSION="E2AP_V$E2AP" -DKPM_VERSION=KPM_V3_00 \
  -DUNIT_TEST=FALSE -DCMAKE_INSTALL_PREFIX="$PREFIX" -DASN1C_EXEC=/opt/asn1c/bin/asn1c >"$LOGS/build-flexric-cmake.log" 2>&1 \
  || { echo "STOP: cmake failed: tail -30 $LOGS/build-flexric-cmake.log"; exit 1; }
grep -E 'Selected (E2AP_VERSION|KPM Version)' "$LOGS/build-flexric-cmake.log" | sed 's/^/  /'

echo "=== 6. Build: nearRT-RIC, the service models and xapp_kpm_csv (10-20 min)"
TARGETS="nearRT-RIC xapp_kpm_csv kpm_sm rc_sm mac_sm rlc_sm pdcp_sm slice_sm tc_sm gtp_sm ccc_sm"
if ! cmake --build "$B" -j"$J" --target $TARGETS >"$LOGS/build-flexric-make.log" 2>&1; then
  grep -m5 -E 'error|Error' "$LOGS/build-flexric-make.log" | sed 's/^/    /'
  echo "STOP: build failed. Send me the 40 lines above the first 'error' in $LOGS/build-flexric-make.log"; exit 1
fi
echo "  [ OK ] built"

echo "=== 7. Install into $PREFIX"
cmake --install "$B" >"$LOGS/build-flexric-install.log" 2>&1 || { echo "STOP: install failed: tail $LOGS/build-flexric-install.log"; exit 1; }
LIBDIR=$(dirname "$(find "$PREFIX" -name libkpm_sm.so | head -1)")
[ -f "$LIBDIR/libkpm_sm.so" ] || { echo "STOP: libkpm_sm.so not installed"; exit 1; }
echo "  [ OK ] service models in $LIBDIR: $(cd "$LIBDIR" && ls *.so | tr '\n' ' ')"

echo "=== 8. Configuration ($CFG/flexric)"
mkdir -p "$CFG/flexric" "$LOGS/flexric" "$LOGS/e2"
cat > "$CFG/flexric/flexric.conf" <<CONFEOF
[NEAR-RIC]
NEAR_RIC_IP = 127.0.0.1

[XAPP]
DB_DIR = $LOGS/flexric/
CONFEOF
cat > "$CFG/flexric/paths.env" <<PATHEOF
FR_RIC=$B/examples/ric/nearRT-RIC
FR_XAPP=$B/examples/xApp/c/monitor/xapp_kpm_csv
FR_LIBDIR=$LIBDIR/
FR_CONF=$CFG/flexric/flexric.conf
FR_E2AP=$E2AP
PATHEOF
cat "$CFG/flexric/paths.env" | sed 's/^/  /'

echo "=== 9. Verify"
source "$CFG/flexric/paths.env"
for f in "$FR_RIC" "$FR_XAPP"; do [ -x "$f" ] && echo "  [ OK ] $f" || { echo "  [FAIL] missing $f"; exit 1; }; done
"$FR_RIC" -h 2>&1 | grep -q 'nearRT-RIC IP address' && echo "  [ OK ] nearRT-RIC runs (-h)" || echo "  [WARN] nearRT-RIC -h printed no usage text"
mkdir -p "$CFG/provenance"
{ echo "flexric $TAG $COMMIT E2AP_V$E2AP KPM_V3_00 Release"; echo "asn1c $ASN1C_COMMIT"; git -C "$SRC" diff; } > "$CFG/provenance/flexric.txt"
git -C "$CFG" add -A >/dev/null 2>&1 && git -C "$CFG" commit -qm "FlexRIC $TAG (E2AP v$E2AP)" >/dev/null 2>&1 || true
echo
echo "FLEXRIC BUILD OK (E2AP v$E2AP, KPM v3.00). Next: intact-e2-config (F.3)."
FREOF
chmod +x ~/intact/bin/intact-flexric-build
intact-flexric-build
```

**Expected** (15–25 min; the asn1c and NR RRC parts take the longest):

```
=== 0. Pre-checks
  [ OK ] gcc 13, cmake 3.28.3, 4 build jobs
=== 1. Packages
  [ OK ] build packages
=== 2. asn1c (mouse07410 fork, commit 940dd5fa) in /opt/asn1c
  [ OK ] installed: ASN.1 Compiler, v0.9.29
=== 3. FlexRIC v3.0.0 source in /home/kamrul/intact/src/flexric
  [ OK ] FlexRIC at v3.0.0 (18c3325091)
=== 4. The INTACT xApp: examples/xApp/c/monitor/xapp_kpm_csv.c
 examples/xApp/c/monitor/CMakeLists.txt | 7 +++++++
  [ OK ] xApp source and CMake target added (git -C /home/kamrul/intact/src/flexric diff shows the CMake lines)
=== 5. Configure (E2AP v2, E2SM-KPM v3.00, Release)
  -- Selected E2AP_VERSION: E2AP_V2
  -- Selected KPM Version: KPM_V3_00
=== 6. Build: nearRT-RIC, the service models and xapp_kpm_csv (10-20 min)
  [ OK ] built
=== 7. Install into /home/kamrul/intact/opt/flexric
  [ OK ] service models in /home/kamrul/intact/opt/flexric/lib/flexric: libccc_sm.so libgtp_sm.so libkpm_sm.so ...
=== 8. Configuration (/home/kamrul/intact/configs/flexric)
  FR_RIC=/home/kamrul/intact/src/flexric/build/examples/ric/nearRT-RIC
  FR_XAPP=/home/kamrul/intact/src/flexric/build/examples/xApp/c/monitor/xapp_kpm_csv
  FR_LIBDIR=/home/kamrul/intact/opt/flexric/lib/flexric/
  FR_CONF=/home/kamrul/intact/configs/flexric/flexric.conf
  FR_E2AP=2
=== 9. Verify
  [ OK ] .../nearRT-RIC
  [ OK ] .../xapp_kpm_csv
  [ OK ] nearRT-RIC runs (-h)

FLEXRIC BUILD OK (E2AP v2, KPM v3.00). Next: intact-e2-config (F.3).
```

The asn1c version text may differ. The library folder may be `lib/` or `lib64/`: the script finds it.

**If not:**

| Stop message | Fix |
| --- | --- |
| `stop the stack first` | `intact-down`, then `intact-flexric-build` |
| `asn1c build failed` | send the last 30 lines of `~/intact/logs/build-asn1c.log` |
| `cannot clone FlexRIC` | no route to GitHub or GitLab: check `git ls-remote https://github.com/duranta-project/flexric.git` |
| `cmake failed` | `tail -30 ~/intact/logs/build-flexric-cmake.log`. A `Could NOT find ...` names a missing `-dev` package |
| `build failed` | send the 40 lines above the first `error` in `~/intact/logs/build-flexric-make.log` |
| `c++: fatal error: Killed signal` (in the make log) | out of memory: `JOBS=2 intact-flexric-build` |

### F.3 The gNB config with E2 — \[SCRIPT\], gNB down

`intact-e2-config` writes `~/intact/configs/gnb_zmq_jbpf_e2.yaml`: your current `gnb_zmq_jbpf.yaml` plus three things.

1. **An `e2:` section:**
   - E2 agent in the DU, which is where the scheduler lives;
   - E2SM-KPM on, E2SM-RC and E2SM-CCC off: control stays with jbpf, and the RIC would refuse functions it was not asked for;
   - RIC at 127.0.0.1:36421, local bind address 127.0.0.1.
2. **`metrics.layers.enable_sched: true` and `enable_rlc: true`.** srsRAN computes the E2SM-KPM values from these two metric sources; without RLC metrics, style 4 lists no UE. Both are probably already on from Part 9b; the script makes sure.
3. **An E2AP packet capture** in `/tmp/gnb_e2ap_du.pcap`, if this build accepts the keys. Open it with Wireshark 4.0.7 or newer.

The new file is checked with `gnb --dryrun`. If the gNB refuses the capture keys, the script retries without them. `gnb_zmq_jbpf.yaml` is not changed. To build the same from the config without jbpf: `intact-e2-config ~/intact/configs/gnb_zmq.yaml`, which writes `gnb_zmq_e2.yaml`.

```bash
source ~/intact/env.sh
cat > ~/intact/bin/intact-e2-config <<'E2CEOF'
#!/usr/bin/env bash
# intact-e2-config [source.yaml]: write <source>_e2.yaml (default: gnb_zmq_jbpf_e2.yaml) = the source gNB config
# plus (1) an E2 agent in the DU with only E2SM-KPM, connecting to the FlexRIC nearRT-RIC at 127.0.0.1:36421,
# (2) the scheduler and RLC metrics that E2SM-KPM is computed from, (3) an E2AP packet capture (if accepted).
# The source file is not changed. The result is validated with 'gnb --dryrun'. Safe to rerun.
set -euo pipefail
source ~/intact/env.sh
SRCF="${1:-$CFG/gnb_zmq_jbpf.yaml}"; OUT="${SRCF%.yaml}_e2.yaml"
G="$SRSRAN_DIR/build/apps/gnb/gnb"
pgrep -x gnb >/dev/null && { echo "STOP: the gNB is running. Run intact-down first."; exit 1; }
[ -f "$SRCF" ] || { echo "STOP: $SRCF not found"; exit 1; }
[ -x "$G" ] || { echo "STOP: gNB binary not found: $G"; exit 1; }
sudo -v
echo "=== source: $SRCF"
echo "=== output: $OUT"
for variant in pcap nopcap; do
  python3 - "$SRCF" "$OUT" "$variant" <<'PY'
import sys, yaml
src, out, variant = sys.argv[1:4]
d = yaml.safe_load(open(src))
d["e2"] = {"enable_du_e2": True,          # E2 agent in the DU (the scheduler lives there)
           "e2sm_kpm_enabled": True,      # E2SM-KPM: monitoring
           "e2sm_rc_enabled": False,      # control stays with jbpf (Part 10); RC not needed
           "e2sm_ccc_enabled": False,
           "addr": "127.0.0.1", "port": 36421, "bind_addr": "127.0.0.1"}
m = d.get("metrics") or {}
lay = m.get("layers") or {}
lay["enable_sched"] = True   # PRB use, throughput, CQI come from the scheduler metrics
lay["enable_rlc"] = True     # the per-UE list, volumes, delays and drop rate come from the RLC metrics
m["layers"] = lay
d["metrics"] = m
p = d.get("pcap") or {}
if variant == "pcap":
    p["e2ap_enable"] = True
    p["e2ap_du_filename"] = "/tmp/gnb_e2ap_du.pcap"
else:
    p.pop("e2ap_enable", None); p.pop("e2ap_du_filename", None)
if p: d["pcap"] = p
else: d.pop("pcap", None)
yaml.safe_dump(d, open(out, "w"), sort_keys=False, default_flow_style=False)
PY
  rc=0; out=$(cd "$(dirname "$G")" && sudo timeout 30 "$G" -c "$OUT" --dryrun 2>&1) || rc=$?
  if [ $rc -eq 0 ] && ! grep -qiE 'not expected|error|invalid|could not' <<<"$out"; then
    echo "  [ OK ] ${OUT##*/} accepted by 'gnb --dryrun' ($variant)"; break
  fi
  echo "  [WARN] gnb rejected the $variant variant:"; sed 's/^/        /' <<<"$out" | tail -5
  if [ "$variant" = nopcap ]; then rm -f "$OUT"; echo "  [FAIL] no E2 config written; $SRCF is unchanged. Send me the lines above."; exit 1; fi
done
python3 - "$OUT" <<'PY'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1]))
print("  e2:", d["e2"]); print("  metrics.layers:", d["metrics"]["layers"])
print("  metrics.periodicity:", (d["metrics"].get("periodicity") or {}), " pcap:", d.get("pcap", {}))
print("  jbpf section kept:", "jbpf" in d, "  slicing:", d.get("cell_cfg", {}).get("slicing"))
PY
git -C "$CFG" add -A >/dev/null 2>&1 && git -C "$CFG" commit -qm "gNB with E2 agent: ${OUT##*/}" >/dev/null 2>&1 || true
echo "E2 CONFIG DONE. Start the gNB with: intact-run-gnb $OUT   (after the nearRT-RIC, F.5)"
E2CEOF

chmod +x ~/intact/bin/intact-e2-config
intact-e2-config
```

**Expected:**

```
=== source: /home/kamrul/intact/configs/gnb_zmq_jbpf.yaml
=== output: /home/kamrul/intact/configs/gnb_zmq_jbpf_e2.yaml
  [ OK ] gnb_zmq_jbpf_e2.yaml accepted by 'gnb --dryrun' (pcap)
  e2: {'enable_du_e2': True, 'e2sm_kpm_enabled': True, 'e2sm_rc_enabled': False, 'e2sm_ccc_enabled': False, 'addr': '127.0.0.1', 'port': 36421, 'bind_addr': '127.0.0.1'}
  metrics.layers: {'enable_sched': True, 'enable_mac': True, 'enable_rlc': True, ...}
  metrics.periodicity: {'du_report_period': 500}  pcap: {'e2ap_enable': True, 'e2ap_du_filename': '/tmp/gnb_e2ap_du.pcap'}
  jbpf section kept: True   slicing: [{'sst': 1, 'sd': 1}, {'sst': 1, 'sd': 2}]
E2 CONFIG DONE. Start the gNB with: intact-run-gnb /home/kamrul/intact/configs/gnb_zmq_jbpf_e2.yaml   (after the nearRT-RIC, F.5)
```

`(nopcap)` instead of `(pcap)` is fine: only the capture is missing. `[FAIL] no E2 config written` means nothing was changed; send the lines above it.

### F.4 Launchers and the summary — \[SCRIPT\]

This block creates three scripts:

- **`intact-run-ric`** (window T0) runs the RIC in the foreground and copies its output to `~/intact/logs/run/ric.out`. It refuses to start if a RIC is already running or SCTP port 36421/36422 is taken. It warns if the gNB is already up, because the gNB connects to the RIC only when it starts.
- **`intact-run-kpm-xapp`** (window T7) checks, in order, before it starts the xApp:
  - the RIC is up and ready;
  - the gNB is running and printed `E2AP: Connection to Near-RT-RIC ... completed`;
  - the RIC received the gNB's E2 Setup;
  - how many UEs are attached.

  It writes the CSV to `~/intact/logs/e2/kpm-<time>.csv`, points `~/intact/logs/e2/kpm-latest.csv` at it, and copies its console to `~/intact/logs/run/xapp-kpm.out`.
- **`intact-kpm-summary`** (any window) reads a CSV and prints, per style, UE and metric: count, last value, mean, min and max.

```bash
source ~/intact/env.sh
cat > ~/intact/bin/intact-run-ric <<'RICEOF'
#!/usr/bin/env bash
# Foreground: FlexRIC nearRT-RIC. E2 (gNB) on SCTP 127.0.0.1:36421, E42 (xApps) on SCTP 127.0.0.1:36422.
# Start it BEFORE the gNB: the gNB connects to the RIC once, when it starts.
source ~/intact/env.sh
source "$CFG/flexric/paths.env" 2>/dev/null || { echo "STOP: run intact-flexric-build first (F.2)"; exit 1; }
pgrep -x nearRT-RIC >/dev/null && { echo "STOP: a nearRT-RIC is already running (pid $(pgrep -x nearRT-RIC))"; exit 1; }
if sudo ss -HlnS 2>/dev/null | grep -qE ':3642[12]\b'; then sudo ss -HlnpS | grep -E ':3642[12]\b'; echo "STOP: SCTP port 36421 or 36422 is in use"; exit 1; fi
pgrep -x gnb >/dev/null && echo "WARNING: the gNB is already running. It only connects to the RIC at start: restart it after this RIC is up."
exec > >(tee "$LOGS/run/ric.out") 2>&1
echo "[intact] nearRT-RIC start $(date -Is), E2AP v$FR_E2AP, service models $FR_LIBDIR"
cd "$(dirname "$FR_RIC")"
exec stdbuf -oL -eL "$FR_RIC" -c "$FR_CONF" -p "$FR_LIBDIR"
RICEOF

cat > ~/intact/bin/intact-run-kpm-xapp <<'XAPPEOF'
#!/usr/bin/env bash
# Foreground: the KPM-to-CSV xApp. Needs the nearRT-RIC (window T0) and the gNB started with the E2 config (T3).
# Options (environment): KPM_PERIOD_MS (default 1000; srsRAN minimum 1000), KPM_STYLES ("1,4"),
#   KPM_CELL_METRICS / KPM_UE_METRICS (comma lists or "all"), KPM_CSV (output file), XAPP_DURATION (seconds).
source ~/intact/env.sh
source "$CFG/flexric/paths.env" 2>/dev/null || { echo "STOP: run intact-flexric-build first (F.2)"; exit 1; }
R="$LOGS/run"; E="$LOGS/e2"; mkdir -p "$E"
pgrep -x xapp_kpm_csv >/dev/null && { echo "STOP: the KPM xApp is already running (pid $(pgrep -x xapp_kpm_csv))"; exit 1; }
pgrep -x nearRT-RIC >/dev/null || { echo "STOP: the nearRT-RIC is not running. Window T0: intact-run-ric"; exit 1; }
grep -q 'PORT = 36422' "$R/ric.out" 2>/dev/null || { echo "STOP: the nearRT-RIC is not ready yet (see $R/ric.out)"; exit 1; }
pgrep -x gnb >/dev/null || { echo "STOP: the gNB is not running. Window T3: intact-run-gnb $CFG/gnb_zmq_jbpf_e2.yaml"; exit 1; }
if ! grep -q 'E2AP: Connection to Near-RT-RIC' "$R/gnb.out" 2>/dev/null; then
  echo "STOP: the gNB is not connected to the RIC (no 'E2AP: Connection to Near-RT-RIC ... completed' in $R/gnb.out)."
  echo "      Was it started with gnb_zmq_jbpf_e2.yaml, and AFTER the RIC? Restart: intact-down, then T0, T2, T3, T4, T5."; exit 1
fi
grep -q 'E2 SETUP-REQUEST rx' "$R/ric.out" || { echo "STOP: the RIC has not received the gNB's E2 Setup (see $R/ric.out)"; exit 1; }
n=0; for ns in $(ip netns list 2>/dev/null | awk '{print $1}' | grep -E '^ue[0-9]+$'); do
  [ -n "$(sudo -n ip netns exec "$ns" ip -4 -br addr show tun_srsue 2>/dev/null)" ] && n=$((n + 1)); done
[ "$n" -gt 0 ] && echo "[intact] $n UE(s) attached" || echo "[intact] WARNING: no UE attached yet: per-UE (style 4) rows appear once a UE attaches"
export KPM_CSV="${KPM_CSV:-$E/kpm-$(date +%Y%m%d-%H%M%S).csv}"
ln -sfn "$KPM_CSV" "$E/kpm-latest.csv"
exec > >(tee "$R/xapp-kpm.out") 2>&1
echo "[intact] KPM xApp start $(date -Is) -> $KPM_CSV"
cd "$(dirname "$FR_XAPP")"
exec stdbuf -oL -eL "$FR_XAPP" -c "$FR_CONF" -p "$FR_LIBDIR"
XAPPEOF

cat > ~/intact/bin/intact-kpm-summary <<'SUMEOF'
#!/usr/bin/env python3
"""intact-kpm-summary [file.csv]: what did the KPM xApp record? (default: ~/intact/logs/e2/kpm-latest.csv)
Per style, UE and metric: rows, last value, mean, min, max; indications per minute; time span."""
import csv, os, statistics as st, sys
from collections import defaultdict
p = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/intact/logs/e2/kpm-latest.csv")
rows = list(csv.DictReader(open(p)))
if not rows:
    sys.exit(f"{p}: no rows yet (is the xApp subscribed and is a UE attached?)")
t = [float(r["t_mono_s"]) for r in rows]
span = max(t) - min(t)
ind = defaultdict(set)
for r in rows: ind[r["style"]].add(r["t_mono_s"])
print(f"{p}\n  {len(rows)} rows over {span:.0f} s; " +
      ", ".join(f"style {s}: {len(v)} indications ({len(v) / max(span, 1) * 60:.0f}/min)" for s, v in sorted(ind.items())))
age = [float(r["t_wall_s"]) - float(r["collect_start_unix_s"]) for r in rows if r["collect_start_unix_s"] not in ("", "0.000000")]
if age:
    print(f"  arrival minus collectStartTime: median {st.median(age):.2f} s (expect about one report period: the gNB stamps the start of each period;\n   both are wall-clock times, so a WSL clock step adds a +-2 s outlier now and then; use t_mono_s for timing)")
g = defaultdict(list)
for r in rows:
    if r["value"] != "": g[(r["style"], r["ue_id"] or "cell", r["metric"], r["unit"])].append(float(r["value"]))
print(f"\n  {'style':<5} {'ue':<12} {'metric':<30} {'unit':<14} {'n':>5} {'last':>10} {'mean':>10} {'min':>10} {'max':>10}")
for (s, ue, m, u), v in sorted(g.items()):
    print(f"  {s:<5} {ue:<12} {m:<30} {u:<14} {len(v):>5} {v[-1]:>10.4g} {st.mean(v):>10.4g} {min(v):>10.4g} {max(v):>10.4g}")
nv = sum(1 for r in rows if r["value"] == "")
if nv: print(f"\n  {nv} rows carried no value (srsRAN reports 'no value' when a metric has no sample in the period)")
SUMEOF
chmod +x ~/intact/bin/intact-run-ric ~/intact/bin/intact-run-kpm-xapp ~/intact/bin/intact-kpm-summary
ls -la ~/intact/bin/intact-run-ric ~/intact/bin/intact-run-kpm-xapp ~/intact/bin/intact-kpm-summary
```

**Expected:** three lines from `ls -la`.

### F.5 Run — \[MANUAL order\], one window per row

Order matters twice:

- **the RIC before the gNB:** the gNB connects to the RIC once, when it starts, and does not retry;
- **the xApp after the UEs:** per-UE rows appear only for attached UEs. The xApp can start earlier, but then the first per-UE rows come later.

Every window starts with `source ~/intact/env.sh`.

1. **T1:** `intact-down` (a clean start), then `intact-core-health` → `CORE READY`.
2. **T0 (new window):** `intact-run-ric`. Wait for:

```
[intact] nearRT-RIC start 2026-10-30T..., E2AP v2, service models /home/kamrul/intact/opt/flexric/lib/flexric/
[UTIL]: Setting the config -c file to /home/kamrul/intact/configs/flexric/flexric.conf
[UTIL]: Setting path -p for the shared libraries to /home/kamrul/intact/opt/flexric/lib/flexric/
[NEAR-RIC]: nearRT-RIC IP Address = 127.0.0.1, PORT = 36421
[NEAR-RIC]: Initializing Task Manager with 2 threads
[iApp]: Initializing ...
[iApp]: nearRT-RIC IP Address = 127.0.0.1, PORT = 36422
```

FlexRIC also prints one line per service model it loads; the exact wording depends on the version.

3. **T2:** `intact-run-jrtc`, as always.
4. **T3:** `intact-run-gnb $CFG/gnb_zmq_jbpf_e2.yaml`. Expected, in addition to the usual lines:
   - in **T3**: `E2AP: Connection to Near-RT-RIC on 127.0.0.1:36421 completed`, then `N2: Connection to AMF on 127.0.0.5:38412 completed` and `==== gNB started ===`;
   - in **T0**, at the same moment: `[E2AP]: E2 SETUP-REQUEST rx from PLMN 00101 Node ID 411 RAN type ngran_gNB_DU ID 0` and `[NEAR-RIC]: Accepting RAN function ID 2 with def = ORAN-E2SM-KPM`. The node ID and DU ID are whatever your gNB uses.
5. **T4:** `intact-run-proxy`, as always.
6. **T5:** `intact-run-ue` for one UE, or `intact-multiue up` for Appendix E's UEs. Wait for the IP(s).
7. **T7 (new window):** `intact-run-kpm-xapp`. Expected:

```
[intact] 3 UE(s) attached
[intact] KPM xApp start 2026-10-30T... -> /home/kamrul/intact/logs/e2/kpm-20261030-140512.csv
[kpm-csv] writing /home/kamrul/intact/logs/e2/kpm-20261030-140512.csv (period 1000 ms, styles 1,4)
[kpm-csv] connected to the nearRT-RIC
[kpm-csv] E2 nodes connected: 1
[kpm-csv] node 001-01/ngran_gNB_DU/nb411/du0 advertises 5 KPM report style(s):
           style 1 (15 metrics): DRB.AirIfDelayUl DRB.RlcDelayUl ... RRU.PrbUsedUl
           style 2 (14 metrics): ...
           style 3 (14 metrics): ...
           style 4 (14 metrics): ...
           style 5 (14 metrics): ...
[kpm-csv] subscribing style 1, 15 metric(s): ...
[kpm-csv] style 1 subscription accepted
[kpm-csv] subscribing style 4, 15 metric(s): ... CQI
[kpm-csv] style 4 subscription accepted
[kpm-csv] SUBSCRIBED (2). Rows go to /home/kamrul/intact/logs/e2/kpm-20261030-140512.csv. Ctrl-C to stop.
[kpm-csv] 2026-10-30T11:05:14.012Z style 1 #1: RRU.PrbTotDl=12% DRB.UEThpDl=1450.2kbit/s | rows 15
[kpm-csv] 2026-10-30T11:05:14.013Z style 4 #1: 3 UE(s) du_f1ap:0:DRB.UEThpDl=480.1kbit/s du_f1ap:1:... | rows 60
...
```

The numbers of metrics are those srsRAN 25.10 advertises: 15 at cell level, 14 per UE. The xApp adds `CQI` to the per-UE list. The values shown are illustrative.

In **T0** the RIC prints the subscription traffic, for example `[NEAR-RIC]: Report Service Asked from nb_id = 411`. In **T3** the gNB's console does not change; with `all_level: warning`, `/tmp/gnb.log` shows only E2 problems.

8. **Make the numbers move:** in T6, `intact-multiue iperf 5 30`, or for one UE `sudo ip netns exec ue1 iperf3 -c 10.45.0.1 -u -b 10M -R -t 30`. `DRB.UEThpDl` in T7 rises to about the offered rate per UE, and `RRU.PrbTotDl` rises with the load.

**Options for the xApp:** environment variables set in front of the command, for example:

```bash
KPM_UE_METRICS="DRB.UEThpDl,RRU.PrbUsedDl,CQI" KPM_STYLES=4 XAPP_DURATION=600 intact-run-kpm-xapp
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `KPM_PERIOD_MS` | 1000 | report period and granularity; srsRAN's minimum is 1000 |
| `KPM_STYLES` | `1,4` | `1` = cell level, `4` = per UE |
| `KPM_CELL_METRICS`, `KPM_UE_METRICS` | all advertised (+ `CQI` per UE) | comma-separated names from the table in F.0 |
| `KPM_CSV` | `~/intact/logs/e2/kpm-<time>.csv` | output file; appended, header written when new |
| `XAPP_DURATION` | until Ctrl-C | seconds to run, then unsubscribe and exit |

### F.6 Read the CSV

One row per metric per indication, and per UE for style 4:

| Column | Meaning |
| --- | --- |
| `t_wall_utc`, `t_wall_s` | arrival at the xApp, wall clock (UTC text, and Unix seconds) |
| `t_mono_s` | arrival, monotonic clock: **use this for timing**, because WSL steps the wall clock (B.4) |
| `e2_node` | PLMN / node type / gNB ID / DU ID, e.g. `001-01/ngran_gNB_DU/nb411/du0` |
| `style` | 1 = cell level, 4 = per UE |
| `ue_id` | empty for style 1; `du_f1ap:<n>` (gNB-CU UE F1AP ID) for style 4 |
| `metric`, `value`, `value_type`, `unit` | the E2SM-KPM name, its value (empty for "no value"), `int`/`real`/`none`, and the unit from the F.0 table |
| `collect_start_unix_s` | the gNB's collection start time (NTP converted to Unix seconds, gNB wall clock) |
| `gran_period_ms`, `incomplete` | granularity period, and 1 if the gNB marked the record unreliable |

```bash
intact-kpm-summary                       # the latest CSV
intact-kpm-summary ~/intact/logs/e2/kpm-20261030-140512.csv
```

**Expected:**

- a header line with the number of rows, the time span and indications per minute for each style (about 60 per minute at 1.0×; fewer when the radio runs slower, since the gNB's E2 timer follows the radio's clock);
- the median of arrival minus `collectStartTime`, about one report period;
- one line per (style, UE, metric) with count, last, mean, min and max.

**Cross-checks that prove the data is right:**

- **Throughput.** `DRB.UEThpDl` (kbit/s) per UE ≈ the UE's `DL Mbit/s` × 1000 in `intact-multiue status`, measured at the same time. Expect a few % difference: the scheduler's rate (status) includes MAC overhead, while E2's is measured at RLC.
- **Slice cap.** With `slice_mgmt` alternating 30/70 (Part 10), `RRU.PrbTotDl` of the UEs on slice SD 1 follows the cap under full load.
- **UE count.** The number of distinct `ue_id` values in style-4 rows equals the number of attached UEs.

FlexRIC's xApp library also stores every indication in its own SQLite file in `~/intact/logs/flexric/xapp_db_<time>`, one file per run. The CSV is the primary output; the SQLite file can be deleted.

### F.7 Stop

- **Ctrl-C in T7** unsubscribes, then prints `stopped: N style-1 and M style-4 indications, R rows in <file>`.
- **`intact-down`** then stops everything, including the RIC.

**Restart rule:** whenever the gNB restarts, restart the RIC first, because the gNB connects to the RIC only when it starts. If only the RIC restarts while the gNB keeps running, the gNB loses E2 until it is restarted too. `intact-down` followed by F.5 always works.

### F.8 Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| T3 never prints `E2AP: Connection to Near-RT-RIC ... completed` | the RIC was not running when the gNB started, or the gNB was started with the old config. Run `intact-down`, then F.5 from step 2 with `gnb_zmq_jbpf_e2.yaml` |
| T0 prints `Unknown RAN function ID, thus rejecting` and stops | the gNB offered RC or CCC: its config was not made by `intact-e2-config`. Rerun F.3 |
| T0 stops or asserts right after `E2 SETUP-REQUEST rx`, or never prints `Accepting RAN function ID 2` | E2AP codec mismatch. Run `intact-down; intact-flexric-build --e2ap 3`, then F.5 again. Still failing: send `~/intact/logs/run/ric.out`, `~/intact/logs/run/gnb.out` and `/tmp/gnb_e2ap_du.pcap` |
| T7: `STOP: the gNB is not connected to the RIC` | as in the first row: wrong start order or config |
| T7 hangs before `connected to the nearRT-RIC` | the RIC is not running or not ready: T0 must show the `PORT = 36422` line |
| T7: `no E2 node offers E2SM-KPM` | the gNB's E2 setup did not complete. Check T0 for `Accepting RAN function ID 2` |
| T7: `style N subscription failed` | the gNB refused the action. \`grep -i -E 'e2sm |
| style 1 rows appear, style 4 never does | no UE attached yet, or RLC metrics are off. Rerun F.3, which sets `enable_rlc` |
| some metrics have an empty `value` (`none`) | the gNB had no sample in that period, for example uplink delay without uplink traffic |
| `DRB.UEThpDl` is 0 although iperf3 runs | the traffic goes to another UE (check `ue_id` against `status`), or the UE is on a slice capped to a few % |
