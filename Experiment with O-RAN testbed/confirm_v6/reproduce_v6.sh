#!/usr/bin/env bash
# reproduce_v6.sh -- the whole V6 experiment from a dataset folder, in one command.
#
#   bash reproduce_v6.sh DATASET_DIR WORK_DIR frozen   # use the frozen (pre-registered) configurations: reproduces Z4
#   bash reproduce_v6.sh DATASET_DIR WORK_DIR search   # from scratch: re-run the development search first
#
# DATASET_DIR = a folder with dataset.csv + events_by_intent.json (+ mimics.json), e.g. ~/intact/mild/v5_final/dataset
# WORK_DIR    = a NEW folder for this run (never reuse a folder of another dataset)
#
# Steps (each resumable: run the same command again after an interruption)
#   1. development slice: rows [0, 10080) of the dataset (search mode only)
#   2. development search: 20 trials per neural method + re-ranking on seeds 42-44 (search mode only, ~4-6 h)
#   3. freeze: selected_configurations.json + mlp_matched.json        (search mode)  | copy the frozen files (frozen mode)
#   4. confirmation A: MILD vs MLP (own selection) vs LSTM, + MLP with MILD's settings (one held-out test, 10 seeds)
#   5. confirmation B: the same with ONLY the MILD-settings MLP (as "MLP")
#   6. classic baselines, tuned on development, standard calibration path, own-intent false alarms
set -euo pipefail
DATA="${1:?dataset folder}"; WORK="${2:?work folder}"; MODE="${3:-frozen}"
KIT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PY="${MILD_TRAIN_PY:-$HOME/intact/mild/venv/bin/python}"
CODE="$KIT/MILD_V6/MILD_unified_repro"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=2 TF_NUM_INTEROP_THREADS=1 TF_NUM_INTRAOP_THREADS=2 \
       TF_ENABLE_ONEDNN_OPTS=0 TF_CPP_MIN_LOG_LEVEL=3
mkdir -p "$WORK"
[ -f "$DATA/dataset.csv" ] && [ -f "$DATA/events_by_intent.json" ] || { echo "no dataset.csv/events_by_intent.json in $DATA"; exit 1; }
echo "== dataset: $(($(wc -l < "$DATA/dataset.csv") - 1)) rows, mode: $MODE, work: $WORK"

if [ "$MODE" = predictions ]; then
  [ -f "$WORK/classic/classic_selected.json" ] || {
    echo "Missing $WORK/classic/classic_selected.json. Restore the original classic folder and extracted jobs." >&2
    exit 1
  }
  find "$WORK/classic/jobs" -name spec.json -print -quit 2>/dev/null | grep -q . || {
    echo "Missing extracted classical jobs under $WORK/classic/jobs. Extract the archived jobs first." >&2
    exit 1
  }
  for part in confirm_A confirm_B; do
    "$PY" "$CODE/confirm_v6.py" --from_jobs --run "$WORK/$part" --run "$WORK/classic" \
      --events_json "$DATA/events_by_intent.json" --out "$WORK/recomputed/$part"
  done
  echo "Reports rebuilt in $WORK/recomputed, including tuned classical baselines (no training or significance tests)."
  exit 0
fi

if [ "$MODE" = search ]; then
  echo "== 1. development slice (rows 0-10079)"
  "$PY" "$KIT/tools/make_dev_slice.py" "$DATA" "$WORK/development_data" 10080
  echo "== 2. development search (20 trials per method, re-rank on 3 seeds)"
  for m in mild mlp lstm; do
    ( cd "$CODE" && nice -n 10 "$PY" devcompare.py --protocol_v6 --method $m \
        --data_csv "$WORK/development_data/dataset.csv" --events_json "$WORK/development_data/events_by_intent.json" \
        --out "$WORK/dev_search" --trials 20 2>&1 | grep -vE '^\s+\[.*run ' | tail -5 )
  done
  echo "== 3. freeze the selected configurations"
  "$PY" "$KIT/tools/freeze_configs.py" "$WORK/dev_search" "$WORK/frozen"
elif [ "$MODE" = frozen ]; then
  echo "== 3. use the pre-registered frozen configurations"
  mkdir -p "$WORK/frozen"
  cp "$KIT/selected_configurations.json" "$WORK/frozen/selected_configurations.json"
  cp "$KIT/mlp_matched_trial10.json" "$WORK/frozen/mlp_matched.json"
else
  echo "MODE must be frozen or search"; exit 1
fi
"$PY" - "$WORK/frozen" <<'EOF'
import json, sys
d = sys.argv[1]
s = json.load(open(d + '/selected_configurations.json')); m = json.load(open(d + '/mlp_matched.json'))
s2 = dict(s); s2['mlp'] = {'trial': 'MILD generic settings', 'cfg': m['cfg']}
json.dump(s2, open(d + '/selected_mlp_mildsettings.json', 'w'), indent=2)
print('  frozen files ready:', sorted(s))
EOF

echo "== 4. confirmation A (own-selected MLP + MLP with MILD's settings), 10 seeds"
( cd "$CODE" && nice -n 10 "$PY" confirm_v6.py --data_csv "$DATA/dataset.csv" --events_json "$DATA/events_by_intent.json" \
    --selected "$WORK/frozen/selected_configurations.json" --mlp_matched "$WORK/frozen/mlp_matched.json" \
    --no_classic --seeds "${SEEDS:-42,43,44,45,46,47,48,49,50,51}" --pilot_rows "${PILOT_ROWS:-14400}" --out "$WORK/confirm_A" 2>&1 | tee "$WORK/confirm_A.log" | grep -vE '  run ' | tail -40 )
echo "== 5. confirmation B (only the MLP with MILD's settings), 10 seeds"
( cd "$CODE" && nice -n 10 "$PY" confirm_v6.py --data_csv "$DATA/dataset.csv" --events_json "$DATA/events_by_intent.json" \
    --selected "$WORK/frozen/selected_mlp_mildsettings.json" \
    --no_classic --seeds "${SEEDS:-42,43,44,45,46,47,48,49,50,51}" --pilot_rows "${PILOT_ROWS:-14400}" --out "$WORK/confirm_B" 2>&1 | tee "$WORK/confirm_B.log" | grep -vE '  run ' | tail -25 )
echo "== 6. classic baselines (tuned on development, standard calibration, own-intent false alarms)"
( cd "$CODE" && nice -n 10 "$PY" classic_tuned.py --data_csv "$DATA/dataset.csv" --events_json "$DATA/events_by_intent.json" \
    --calibration standard --fp_scope own --out "$WORK/classic" 2>&1 | grep -vE '  run ' | sed -n '/## Held-out test/,$p' )
echo
echo "DONE. Reports:"
echo "  $WORK/confirm_A/CONFIRM_REPORT.md   (MILD, MLP own selection, LSTM, MLP with MILD's settings)"
echo "  $WORK/confirm_B/CONFIRM_REPORT.md   (MILD, MLP with MILD's settings, LSTM)"
echo "  $WORK/classic/CLASSIC_REPORT.md     (LR-OvR, WKPI-Tuned, Dist-Target)"
[ "$MODE" = search ] && echo "  $WORK/dev_search/*_selected.json    (what the search chose; compare with the pre-registered files)"
exit 0
