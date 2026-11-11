#!/usr/bin/env bash
# One-time installation for the revised SDN testbed (Ubuntu 22.04 / 24.04).
# Usage:  bash scripts/install_deps_ubuntu.sh            (asks for sudo where needed)
set -euo pipefail
ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)

echo "[1/5] system packages"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y mininet openvswitch-switch prometheus iperf3 \
     iproute2 iputils-ping curl net-tools e2fsprogs util-linux git python3-pip
# The Ubuntu prometheus package starts its own service on port 9090. The pipeline
# starts its own Prometheus and would pick another port, but stopping the system
# service keeps things simple:
sudo systemctl disable --now prometheus 2>/dev/null || true
sudo systemctl enable --now openvswitch-switch

echo "[2/5] conda environment for the testbed (Python 3.9, needed by Ryu)"
if ! command -v conda >/dev/null; then echo "Install Miniconda/Anaconda first"; exit 1; fi
eval "$(conda shell.bash hook)"
conda env list | grep -q '^ryu39 ' || conda create -y -n ryu39 python=3.9
conda activate ryu39
python -m pip install --upgrade pip
python -m pip install -r "$ROOT_DIR/requirements.txt"

echo "[3/5] Ryu (patched as in your earlier set-up)"
if ! command -v ryu-manager >/dev/null; then
  cd /tmp && rm -rf ryu && git clone https://github.com/faucetsdn/ryu.git && cd ryu
  sed -i 's/_main_module()._orig_get_script_args = easy_install.get_script_args/_main_module()._orig_get_script_args = lambda: []/' ryu/hooks.py
  python -m pip install . && python -m pip install "eventlet==0.30.2"
fi
ryu-manager --version || true

echo "[4/5] training environment (Python 3.11 + TensorFlow with GPU support)"
conda env list | grep -q '^mild-train ' || conda create -y -n mild-train python=3.11
conda activate mild-train
python -m pip install --upgrade pip
python -m pip install "tensorflow[and-cuda]" numpy pandas scikit-learn scipy matplotlib shap tabulate
python -c "import tensorflow as tf; print('GPUs:', tf.config.list_physical_devices('GPU'))"

echo "[5/5] done"
echo "Next:  conda activate ryu39 && cd $ROOT_DIR && sudo -E env PATH=\"\$PATH\" python pipeline.py \\"
echo "         --state ~/mild_sdn2_state --train_python \$(conda run -n mild-train which python)"
