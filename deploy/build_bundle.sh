#!/usr/bin/env bash
# build_bundle.sh — build an OTA agent bundle.
#
# Produces  dist/sdr-agent-<version>.tar.gz  containing a top-level VERSION file, the
# agent payload (agent/ scripts/ paramkit/ requirements.txt), the default configs/
# (seed state for a fresh Pi), and the deploy/ scripts (provision + migrate + the
# systemd units). This single artifact feeds the agent's POST /admin/update endpoint
# (Phase 1), the client "Update" button, AND the client "Provision unit" flow
# (Phase 2), which unpacks it on a fresh Pi and runs deploy/provision_install.sh.
# For OTA the extra configs/ + deploy/ dirs land unused in the release dir (state is
# read from SDR_STATE_DIR); they only matter to the from-scratch provision path.
# A .sha256 sidecar is written alongside.
set -euo pipefail
cd "$(dirname "$0")/.."   # repo root

# Read AGENT_VERSION straight out of config.py with sed — no Python needed, so this
# runs anywhere bash + tar exist (Linux, macOS, Git Bash on Windows). On Windows the
# `python3` command hits the Microsoft Store alias stub and fails, which is why we
# avoid it here.
VERSION="$(sed -n -E 's/^AGENT_VERSION[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' agent/config.py | head -1)"
[ -n "$VERSION" ] || { echo "could not read AGENT_VERSION from agent/config.py" >&2; exit 1; }

OUT="dist/sdr-agent-${VERSION}.tar.gz"
mkdir -p dist
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "$VERSION" > "$STAGE/VERSION"
cp -r agent scripts paramkit configs deploy requirements.txt "$STAGE/"
find "$STAGE" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$STAGE" -name '*.pyc' -delete

tar -C "$STAGE" -czf "$OUT" VERSION agent scripts paramkit configs deploy requirements.txt
( cd dist && { sha256sum "$(basename "$OUT")" 2>/dev/null || shasum -a 256 "$(basename "$OUT")"; } > "$(basename "$OUT").sha256" )

echo "Built $OUT  (version $VERSION)"
