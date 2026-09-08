#!/usr/bin/env bash
# run_local.sh — start the agent locally for HEADLESS, NO-HARDWARE integration
# testing against the real PyQt6 client (sdr-client). NOT a deployment script;
# see provision_install.sh / install.sh for real units. Full recipe:
# docs/local-integration-run.md.
#
# What it does:
#   • stages a FLAT scripts dir from the sibling sdr-scripts checkout (the agent
#     serves <base>/scripts as a flat dir; the repo nests scripts under platform
#     folders), then
#   • starts `uvicorn agent.main:app` on 0.0.0.0:<port> with dev-friendly env.
#
# Run it BACKGROUNDED (it blocks while serving), then point the client at this
# host with sdr-client/tools/run_local.sh (or take a screenshot with
# sdr-client/tools/screenshot.py).
#
# Env overrides:
#   SDR_LOCAL_RUN_DIR   scratch root for staged code + state (default /tmp/sdr-local)
#   SDR_SCRIPTS_REPO    path to the sdr-scripts checkout (default: sibling ../sdr-scripts)
#   SDR_AGENT_PORT      listen port (default 8765)
#   SDR_UNIT_ID         advertised unit id (default broadcaster-lab)
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"                 # sdr-agent repo root
RUN_DIR="${SDR_LOCAL_RUN_DIR:-/tmp/sdr-local}"
BASE="$RUN_DIR/agent-base"
STATE="$RUN_DIR/agent-state"
PORT="${SDR_AGENT_PORT:-8765}"
SCRIPTS_REPO="${SDR_SCRIPTS_REPO:-$(cd "$HERE/../sdr-scripts" 2>/dev/null && pwd || true)}"

mkdir -p "$BASE/scripts" "$STATE/configs" "$STATE/logs"

# Stage the Raspberry Pi + b206 transmit scripts + mocks FLAT into <base>/scripts.
if [ -n "$SCRIPTS_REPO" ] && [ -d "$SCRIPTS_REPO/Raspberry pi + b206 mini-i" ]; then
    find "$SCRIPTS_REPO/Raspberry pi + b206 mini-i" -name '*.py' -exec cp -f {} "$BASE/scripts/" \;
    echo "staged $(ls "$BASE/scripts" | wc -l) script(s) from $SCRIPTS_REPO"
else
    echo "!! no sibling sdr-scripts checkout at '$SCRIPTS_REPO' — /scripts will be empty"
    echo "   (set SDR_SCRIPTS_REPO=/path/to/sdr-scripts)"
fi

# Seed a realistic sample calibration (Source flatness + cable → programmable
# attenuator → output cable) into the unit's data store, if absent — so the unit
# has calibration by default. The client edits/round-trips it via /files. See
# deploy/sample-calibration/ and docs/local-integration-run.md.
DATA="$STATE/data"; mkdir -p "$DATA"
for f in calibration.json components.yaml; do
    if [ ! -e "$DATA/$f" ] && [ -f "$HERE/deploy/sample-calibration/$f" ]; then
        cp "$HERE/deploy/sample-calibration/$f" "$DATA/$f"
        echo "seeded $f into the unit's data store"
    fi
done

# Register the three no-hardware MOCK transmit tasks (a PRN, a chirp and a CW tone) plus the mock
# step attenuator the calibration chain drives, so the seeded signals are actually ARMABLE with no
# radio. Each transmit task opts into calibration via SDR_CAL_SIGNAL_ID = its script's CAL_SIGNAL_ID
# (the agent injects this unit's resolved calibration), so the client renders the real power card.
# Only written if absent — a fresh RUN_DIR re-seeds. See docs/local-integration-run.md.
TASKS="$STATE/configs/tasks.yaml"
if [ ! -e "$TASKS" ] || ! grep -q "mock_prn" "$TASKS" 2>/dev/null; then
    cat > "$TASKS" <<YAML
# Seeded by deploy/run_local.sh for the local, headless, no-hardware unit. The three mock signals
# (PRN / chirp / CW) mirror the real scripts' parameters + power laws but transmit nothing; the
# attenuator is the mock the calibration chain's "atten_set" control drives. Delete this file (or
# the whole RUN_DIR) to re-seed. NOT for real units — deploy tasks from the client's Library there.
tasks:
  - name: mock_prn
    description: "Mock GPS C/A (1.023 Mcps) — no hardware; same params + power laws as the real PRN"
    command: [python3, "$BASE/scripts/mock_gps_ca_code_1.023Mcps_tx.py"]
    working_dir: "$BASE/scripts"
    env: { SDR_CAL_SIGNAL_ID: "GPS C/A (1.023 Mcps)" }
  - name: mock_chirp
    description: "Mock FM chirp / sweep — no hardware; same params + power laws as the real chirp"
    command: [python3, "$BASE/scripts/mock_fm_chirp_tx.py"]
    working_dir: "$BASE/scripts"
    env: { SDR_CAL_SIGNAL_ID: "Chirp/Sweep" }
  - name: mock_cw
    description: "Mock CW tone — no hardware; same params as the real cw_tx (calibrated dBm)"
    command: [python3, "$BASE/scripts/mock_cw_tx.py"]
    working_dir: "$BASE/scripts"
    env: { SDR_CAL_SIGNAL_ID: "cw_tone" }
  - name: atten_set
    description: "Mock step attenuator — the active component the calibration chain drives (no HW)"
    command: [python3, "$BASE/scripts/mock_atten.py"]
    working_dir: "$BASE/scripts"
YAML
    echo "seeded tasks.yaml (mock_prn / mock_chirp / mock_cw / atten_set)"
fi

# One RF-gated broadcast sequence per signal, so each is armable end to end from the client's
# Sequences tab with no hardware. The shape the owner asked for: the transmit task launches 1 s
# BEFORE on-air with RF OFF (muted pre-roll — set the level, radio silent), a TUNE turns RF ON at
# the on-air anchor and OFF again at the off-air anchor, and the task stops 1 s AFTER off-air.
# --power is in each signal's calibrated quantity (dBm for CW; dBm/Hz base density for PRN/chirp),
# mid-range for the seeded calibration. Only written if absent — a fresh RUN_DIR re-seeds.
SEQS="$STATE/configs/sequences.json"
if [ ! -e "$SEQS" ] || ! grep -q "seq-mock-prn" "$SEQS" 2>/dev/null; then
    cat > "$SEQS" <<'JSON'
{
  "sequences": [
    {
      "id": "seq-mock-prn",
      "name": "Mock PRN (GPS C/A) — RF-gated",
      "description": "Launch the GPS C/A mock 1 s before on-air with RF off; RF on at on-air, off at off-air; stop 1 s after. No hardware.",
      "types": ["broadcaster"],
      "steps": [
        {"anchor": "start", "offset_s": -1.0, "action": "start", "task_name": "mock_prn",
         "args": ["--prn", "1", "--freq", "1575.42", "--sidelobes", "5", "--power", "-150", "--rf", "off"],
         "replace_args": true},
        {"anchor": "start", "offset_s": 0.0, "action": "tune", "task_name": "mock_prn", "params": {"rf": "on"}},
        {"anchor": "stop", "offset_s": 0.0, "action": "tune", "task_name": "mock_prn", "params": {"rf": "off"}},
        {"anchor": "stop", "offset_s": 1.0, "action": "stop", "task_name": "mock_prn"}
      ]
    },
    {
      "id": "seq-mock-chirp",
      "name": "Mock chirp / sweep — RF-gated",
      "description": "Launch the FM-chirp mock 1 s before on-air with RF off; RF on at on-air, off at off-air; stop 1 s after. No hardware.",
      "types": ["broadcaster"],
      "steps": [
        {"anchor": "start", "offset_s": -1.0, "action": "start", "task_name": "mock_chirp",
         "args": ["--band-mode", "center_bw", "--freq", "1575.42", "--bw", "20", "--rate", "200", "--power", "-180", "--rf", "off"],
         "replace_args": true},
        {"anchor": "start", "offset_s": 0.0, "action": "tune", "task_name": "mock_chirp", "params": {"rf": "on"}},
        {"anchor": "stop", "offset_s": 0.0, "action": "tune", "task_name": "mock_chirp", "params": {"rf": "off"}},
        {"anchor": "stop", "offset_s": 1.0, "action": "stop", "task_name": "mock_chirp"}
      ]
    },
    {
      "id": "seq-mock-cw",
      "name": "Mock CW tone — RF-gated",
      "description": "Launch the CW mock 1 s before on-air with RF off; RF on at on-air, off at off-air; stop 1 s after. No hardware.",
      "types": ["broadcaster"],
      "steps": [
        {"anchor": "start", "offset_s": -1.0, "action": "start", "task_name": "mock_cw",
         "args": ["--freq", "1575420000", "--power", "-60", "--rf", "off"],
         "replace_args": true},
        {"anchor": "start", "offset_s": 0.0, "action": "tune", "task_name": "mock_cw", "params": {"rf": "on"}},
        {"anchor": "stop", "offset_s": 0.0, "action": "tune", "task_name": "mock_cw", "params": {"rf": "off"}},
        {"anchor": "stop", "offset_s": 1.0, "action": "stop", "task_name": "mock_cw"}
      ]
    }
  ]
}
JSON
    echo "seeded sequences.json (seq-mock-prn / seq-mock-chirp / seq-mock-cw)"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "==> agent base=$BASE state=$STATE"
echo "==> serving on http://${IP:-0.0.0.0}:$PORT  (point the client at ${IP:-<this-host>})"

exec env \
    SDR_AGENT_BASE="$BASE" \
    SDR_STATE_DIR="$STATE" \
    SDR_UNIT_ID="${SDR_UNIT_ID:-broadcaster-lab}" \
    SDR_UNIT_TYPE="${SDR_UNIT_TYPE:-broadcaster}" \
    PYTHONPATH="$HERE" \
    python3 -m uvicorn agent.main:app --host 0.0.0.0 --port "$PORT"
