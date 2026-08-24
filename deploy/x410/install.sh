#!/usr/bin/env bash
# install.sh (X410) — install the SDR agent onto an Ettus/NI X410, offline, with a
# self-contained Python (the system python3.7 is too old for the agent's stack and
# is left untouched — it's what UHD/GNU Radio use). Counterpart to the Debian/Pi
# deploy/provision_install.sh. Validated on NI Alchemy/Zeus (aarch64, systemd 243).
#
# Run as root on the X410, from inside an unpacked bundle dir containing:
#   python-aarch64.tar.gz   a python-build-standalone CPython (aarch64, install_only)
#   wheels/                 aarch64 cp311 wheels for requirements.txt + psutil + uvloop
#   agent/ scripts/ paramkit/ configs/ requirements.txt   the agent code
# Build that bundle on a PC with internet — see deploy/x410/README.md.
#
# Everything lands under $PERSIST_ROOT (default /data, the X410's persistent
# partition — survives reboot; a full NI OS image update would wipe /etc, so the
# service unit is re-established by re-running this). Uninstall: deploy/x410/uninstall.sh.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

PERSIST_ROOT="${PERSIST_ROOT:-/data}"
touch "$PERSIST_ROOT/.sdr_w" 2>/dev/null && rm -f "$PERSIST_ROOT/.sdr_w" || {
    echo "PERSIST_ROOT=$PERSIST_ROOT is not writable — set it to the persistent path" >&2
    exit 1
}

PYROOT="$PERSIST_ROOT/python"            # bundled interpreter (isolated from system py)
BASE="$PERSIST_ROOT/sdr-agent"           # agent code (real bytes, on persistent storage)
SHARED="$PERSIST_ROOT/sdr-agent-shared"  # state: configs/logs/run (survives updates)
# Stable, Pi-identical mount point for the agent. It's a symlink to $BASE, so the
# real code/scripts stay on the update-surviving /data partition while the paths
# the agent reports and bakes into task commands match the Pi's /opt layout. That
# lets one "shared" library task (command: /opt/sdr-agent/scripts/foo.py) run on
# both a Pi and an X410. The symlink lives on the rootfs, so a full NI OS image
# update wipes it — re-running this script recreates it (as it does the unit file).
LINK="/opt/sdr-agent"
PYBIN="$PYROOT/bin/python3"
PORT="${SDR_AGENT_PORT:-8765}"
UNIT_ID="${SDR_UNIT_ID:-$(hostname)}"

HERE="$(cd "$(dirname "$0")/../.." && pwd)"   # bundle root (this script is deploy/x410/)

echo "==> Installing SDR agent under $PERSIST_ROOT (unit id: $UNIT_ID)"

# 1) Bundled Python — extract once; the tarball has a top-level python/ dir.
if [ ! -x "$PYBIN" ]; then
    PYTAR="$(ls "$HERE"/python*aarch64*.tar.gz "$HERE"/cpython-*.tar.gz 2>/dev/null | head -1 || true)"
    [ -n "$PYTAR" ] || { echo "no python-*aarch64*.tar.gz in the bundle" >&2; exit 1; }
    echo "==> Extracting bundled Python from $(basename "$PYTAR")"
    rm -rf "$PYROOT"
    tar -xzf "$PYTAR" -C "$PERSIST_ROOT"      # creates $PERSIST_ROOT/python
    [ -x "$PYBIN" ] || { echo "expected $PYBIN after extract" >&2; exit 1; }
fi
echo "    interpreter: $("$PYBIN" --version 2>&1)"

# 2) Agent code.
echo "==> Laying down agent code at $BASE"
mkdir -p "$BASE"
cp -a "$HERE/agent" "$HERE/paramkit" "$HERE/requirements.txt" "$BASE/"
mkdir -p "$BASE/scripts"
[ -d "$HERE/scripts" ] && cp -a "$HERE/scripts/." "$BASE/scripts/" 2>/dev/null || true

# 2b) Pi-identical path: /opt/sdr-agent -> $BASE. Refuse to clobber a real dir there
# (a genuine /opt install), but freshen/repoint an existing symlink. Everything the
# service references below goes through $LINK, so /info and task commands read /opt.
mkdir -p "$(dirname "$LINK")"
if [ -L "$LINK" ] || [ ! -e "$LINK" ]; then
    ln -sfn "$BASE" "$LINK"
    echo "    linked $LINK -> $BASE"
else
    echo "$LINK exists and is not a symlink — refusing to clobber it" >&2
    exit 1
fi

# 3) State — seed default configs only where absent (never clobber a unit's state).
echo "==> Preparing state at $SHARED"
mkdir -p "$SHARED/configs" "$SHARED/logs" "$SHARED/run"
if [ -d "$HERE/configs" ]; then
    for f in "$HERE/configs/."/*; do
        [ -e "$f" ] || continue
        n="$(basename "$f")"
        [ -e "$SHARED/configs/$n" ] || cp -a "$f" "$SHARED/configs/$n"
    done
fi

# 4) Dependencies — offline, into the bundled Python (never the system one).
echo "==> Installing Python dependencies offline into the bundle"
[ -d "$HERE/wheels" ] || { echo "no wheels/ in the bundle — see deploy/x410/README.md" >&2; exit 1; }
"$PYBIN" -m pip install --no-index --find-links "$HERE/wheels" \
    --root-user-action=ignore --disable-pip-version-check --no-input \
    -r "$BASE/requirements.txt" psutil
"$PYBIN" -c "import fastapi, uvicorn, uvloop, pydantic, psutil, zeroconf, ruamel.yaml, yaml, multipart, websockets, inotify_simple" \
    && echo "    all agent deps import OK"

# 5) systemd service — bundled Python + persistent paths + the env UHD/agent need.
echo "==> Writing systemd service"
cat > /etc/systemd/system/sdr-agent.service <<EOF
[Unit]
Description=SDR Agent (X410)
After=network.target

[Service]
Type=simple
WorkingDirectory=$LINK
Environment=PYTHONPATH=$LINK
Environment=HOME=/root
Environment=SDR_AGENT_BASE=$LINK
Environment=SDR_STATE_DIR=$SHARED
Environment=SDR_UNIT_ID=$UNIT_ID
Environment=SDR_MDNS_EXCLUDE_IFACES=int0
${SDR_API_KEY:+Environment=SDR_API_KEY=$SDR_API_KEY}
ExecStart=$PYBIN -m uvicorn agent.main:app --host 0.0.0.0 --port $PORT
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

echo "==> Enabling + starting the agent"
systemctl daemon-reload
systemctl enable sdr-agent
systemctl restart sdr-agent
sleep 2
systemctl --no-pager --lines=0 status sdr-agent | head -4 || true
echo "==> Health:"; curl -s "http://127.0.0.1:$PORT/health" && echo
echo "==> Done. Point FleetView at this unit's eth0 address (see deploy/x410/provision_network.sh)."
