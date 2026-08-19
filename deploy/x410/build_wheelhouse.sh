#!/usr/bin/env bash
# build_wheelhouse.sh — build an ABI-correct wheelhouse for the agent's Python
# dependencies, ON THE X410 (or a byte-identical Yocto SDK sysroot).
#
# WHY on-device: pydantic-core (Rust), uvloop/httptools/websockets (C), psutil and
# inotify-simple (C) must match the X410's exact CPython ABI + arch + libc. The only
# way to be *certain* is to have that interpreter resolve and build the wheels. A
# laptop `pip download --platform` guesses the tags and silently mismatches.
#
# Run:
#   ssh root@<x410-host> 'bash -s' < deploy/x410/build_wheelhouse.sh
# It writes ./wheels/ next to itself (or $OUT). Copy that wheels/ into the agent
# bundle so deploy/x410/install.sh can install fully offline.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${OUT:-$HERE/wheels}"
# Point at the agent's requirements.txt — repo layout is deploy/x410/ under root.
REQ="${REQ:-$HERE/../../requirements.txt}"

command -v python3 >/dev/null || { echo "python3 not found — cannot build wheels" >&2; exit 1; }
[ -f "$REQ" ] || { echo "requirements.txt not found at $REQ (set REQ=)" >&2; exit 1; }

echo "==> Interpreter / target tags (these define what the wheels are valid for)"
python3 - <<'PY'
import sysconfig, platform, sys
print("  python  :", sys.version.split()[0])
print("  machine :", platform.machine())
print("  platform:", sysconfig.get_platform())
print("  abi tag :", f"cp{sys.version_info.major}{sys.version_info.minor}")
PY

echo "==> requirements: $REQ"
# NOTE: requirements.txt keeps psutil OUT of pip on the Pi (apt-managed there). On
# the X410 there is no apt, so psutil must come from pip too. Add it explicitly here
# unless the recon showed it already importable in the system Python.
NEED_PSUTIL=1
python3 -c "import psutil" 2>/dev/null && NEED_PSUTIL=0 && echo "  psutil already importable — not adding to wheelhouse"

mkdir -p "$OUT"
echo "==> Building wheels into $OUT (this compiles native extensions on-device)"
# `pip wheel` builds a wheel for every dependency, resolving them with THIS python
# so every tag is correct. Falls back to building from sdist when no wheel exists.
set -x
pip3 wheel --wheel-dir "$OUT" -r "$REQ"
[ "$NEED_PSUTIL" -eq 1 ] && pip3 wheel --wheel-dir "$OUT" psutil
set +x

echo "==> Wheelhouse contents:"
ls -1 "$OUT"
echo
echo "==> Sanity: try an OFFLINE resolve against the wheelhouse (no install)."
echo "    If this prints 'Would install ...' with no network reach, the set is complete."
pip3 install --no-index --find-links "$OUT" --dry-run -r "$REQ" \
    ${NEED_PSUTIL:+psutil} 2>&1 | tail -20 || \
    echo "    (dry-run unsupported on this pip — run install.sh on a scratch venv to verify)"

echo "==> Done. Bundle this wheels/ dir with the agent tarball for offline install."
