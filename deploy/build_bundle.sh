#!/usr/bin/env bash
# build_bundle.sh — build an OTA agent bundle.
#
# Produces  dist/sdr-agent-<version>.tar.gz  containing a top-level VERSION file and
# the payload install.sh copies (agent/ scripts/ paramkit/ requirements.txt). This
# single artifact feeds both the agent's POST /admin/update endpoint and the client
# (which embeds it for the "Update" button). A .sha256 sidecar is written alongside.
set -euo pipefail
cd "$(dirname "$0")/.."   # repo root

VERSION="$(python3 - <<'PY'
import re
s = open("agent/config.py").read()
print(re.search(r'AGENT_VERSION\s*=\s*"([^"]+)"', s).group(1))
PY
)"

OUT="dist/sdr-agent-${VERSION}.tar.gz"
mkdir -p dist
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "$VERSION" > "$STAGE/VERSION"
cp -r agent scripts paramkit requirements.txt "$STAGE/"
find "$STAGE" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$STAGE" -name '*.pyc' -delete

tar -C "$STAGE" -czf "$OUT" VERSION agent scripts paramkit requirements.txt
( cd dist && { sha256sum "$(basename "$OUT")" 2>/dev/null || shasum -a 256 "$(basename "$OUT")"; } > "$(basename "$OUT").sha256" )

echo "Built $OUT  (version $VERSION)"
