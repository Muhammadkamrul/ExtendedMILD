#!/usr/bin/env bash
# install.sh -- set up MILD on the O-RAN testbed inside the `intact` WSL distro. Idempotent: safe to rerun.
#
#   bash ~/intact/mild/MILD_ORAN/install.sh            # CPU TensorFlow (or GPU if nvidia-smi works in WSL)
#   bash ~/intact/mild/MILD_ORAN/install.sh --no-tf    # collection only (train elsewhere)
#   bash ~/intact/mild/MILD_ORAN/install.sh --cpu      # force the CPU build of TensorFlow
#
# What it does, step by step (each prints [ OK ] / [FAIL] with the fix):
#   1. checks where it runs (the package must live in ~/intact/mild/MILD_ORAN, Linux filesystem)
#   2. installs the few system packages it needs (python3-venv, iproute2) if missing
#   3. creates the Python environment ~/intact/mild/venv and installs requirements.txt
#   4. installs TensorFlow (training only; GPU build when the NVIDIA driver is visible in WSL)
#   5. loads the netem queueing discipline (sch_netem) if it is a module
#   6. installs the `mild-oran` command into ~/intact/bin (already on PATH through env.sh)
#   7. import test of every module
# The xApps' E2 front end (C, FlexRIC SDK) is built separately: mild-oran build-xapps (runbook M2b).
set -uo pipefail
PKG="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
M="$HOME/intact/mild"
VENV="$M/venv"
TF=1; CPU=0
for x in "$@"; do case "$x" in --no-tf) TF=0;; --cpu) CPU=1;; esac; done
ok(){ echo "  [ OK ] $*"; }; bad(){ echo "  [FAIL] $*"; exit 1; }; info(){ echo "  [INFO] $*"; }

echo "== 1. location"
[ "$PKG" = "$M/MILD_ORAN" ] && ok "package at $PKG" || info "package at $PKG (expected $M/MILD_ORAN; the mild-oran command assumes that path, or set MILD_HOME)"
case "$PKG" in /mnt/*) bad "the package is under /mnt (Windows filesystem): copy it to ~/intact/mild (runbook M2)";; esac
[ -f "$HOME/intact/env.sh" ] && ok "found ~/intact/env.sh (INTACT testbed runbook)" || info "~/intact/env.sh not found: fine, but the runbook assumes it"
mkdir -p "$M" "$HOME/intact/bin"

echo "== 2. system packages"
need=()
python3 -c "import venv, ensurepip" >/dev/null 2>&1 || need+=(python3-venv)
command -v tc >/dev/null 2>&1 || need+=(iproute2)
command -v ping >/dev/null 2>&1 || need+=(iputils-ping)
python3 -c "import yaml" >/dev/null 2>&1 || need+=(python3-yaml)
if [ ${#need[@]} -gt 0 ]; then
  info "installing ${need[*]} (sudo)"; sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}" || bad "apt-get install ${need[*]} failed"
fi
ok "python3 $(python3 -c 'import sys;print(".".join(map(str,sys.version_info[:3])))'), tc, ping present"

echo "== 3. Python environment $VENV"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV" || bad "python3 -m venv failed"
"$VENV/bin/pip" install -q --upgrade pip >/dev/null 2>&1
"$VENV/bin/pip" install -q -r "$PKG/requirements.txt" || bad "pip install -r requirements.txt failed (network? check: curl -sI https://pypi.org)"
ok "collection packages installed"

echo "== 4. TensorFlow (training)"
if [ $TF -eq 1 ]; then
  if "$VENV/bin/python" -c "import tensorflow" >/dev/null 2>&1; then
    ok "TensorFlow already installed: $("$VENV/bin/python" -c 'import tensorflow as t;print(t.__version__)' 2>/dev/null)"
  elif [ $CPU -eq 0 ] && command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
    info "NVIDIA GPU visible in WSL: installing tensorflow[and-cuda] (about 3 GB, 5-15 min)"
    "$VENV/bin/pip" install -q "tensorflow[and-cuda]" || bad "tensorflow[and-cuda] failed; rerun with --cpu"
    ok "TensorFlow (GPU build) installed"
  else
    info "installing the CPU build of TensorFlow (about 600 MB)"
    "$VENV/bin/pip" install -q tensorflow || bad "pip install tensorflow failed"
    ok "TensorFlow (CPU) installed"
  fi
  "$VENV/bin/python" -c "import tensorflow as tf; g=tf.config.list_physical_devices('GPU'); print('  [INFO] TensorFlow', tf.__version__, 'GPUs:', g or 'none (CPU training)')" 2>/dev/null
else
  info "--no-tf: TensorFlow skipped (collection and learnability checks do not need it)"
fi

echo "== 5. netem"
if sudo modprobe sch_netem 2>/dev/null; then ok "sch_netem loaded (or built in)"; else info "modprobe sch_netem failed: fine if netem is built into the kernel; mild-oran preflight tests it"; fi

echo "== 6. the mild-oran command"
install -m 0755 "$PKG/bin/mild-oran" "$HOME/intact/bin/mild-oran"
chmod +x "$PKG"/*.py "$PKG"/bin/* 2>/dev/null
command -v mild-oran >/dev/null 2>&1 && ok "mild-oran on PATH ($(command -v mild-oran))" || info "~/intact/bin not on PATH in this shell: run  source ~/intact/env.sh"

echo "== 7. import test"
(cd "$PKG" && "$VENV/bin/python" - <<'PY') || bad "import test failed"
import importlib
mods = ['mild_oran.util', 'mild_oran.kpi', 'mild_oran.stack', 'mild_oran.collector', 'mild_oran.calibrate',
        'mild_oran.quality', 'mild_oran.learnability', 'mild_oran.preflight', 'mild_oran.ranctl', 'mild_oran.remedy',
        'mild_oran.demo', 'mild_oran.xapps.xkpm', 'mild_oran.xapps.xsla', 'mild_oran.xapps.xrc',
        'mild_oran.xapps.loadgen', 'mild_oran.ran.ue_agent', 'mild_oran.ran.traffic', 'mild_oran.ran.appserver',
        'mild_oran.ran.fake_ran', 'mild_oran.ran.fake_e2gw']
for m in mods:
    importlib.import_module(m)
print('  [ OK ] all', len(mods), 'modules import')
PY
echo
echo "INSTALL DONE. Next: mild-oran build-xapps   (runbook M2b), then mild-oran selftest (M3)"
