#!/usr/bin/env bash
# uninstall.sh (X410) — remove the SDR agent and leave the borrowed unit as it was.
# Run as root. Reverts everything install.sh created. The eth0/hostname change (if
# you ran provision_network.sh) is separate — revert it from /data/sdr-netsnapshot.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

PERSIST_ROOT="${PERSIST_ROOT:-/data}"
PYROOT="$PERSIST_ROOT/python"
BASE="$PERSIST_ROOT/sdr-agent"
SHARED="$PERSIST_ROOT/sdr-agent-shared"
KEEP_STATE="${KEEP_STATE:-0}"          # set 1 to preserve $SHARED (configs/logs)

echo "==> Stopping + disabling the service"
systemctl disable --now sdr-agent 2>/dev/null || true
rm -f /etc/systemd/system/sdr-agent.service
systemctl daemon-reload

echo "==> Removing code + bundled Python"
rm -rf "$BASE" "$PYROOT"
if [ "$KEEP_STATE" = "1" ]; then
    echo "    keeping state at $SHARED (KEEP_STATE=1)"
else
    rm -rf "$SHARED"
fi

echo "==> Footprint check (should be nothing):"
ls -d "$PYROOT" "$BASE" "$SHARED" 2>/dev/null || echo "    (clean)"
echo "==> Done. If you changed eth0/hostname, revert from ${PERSIST_ROOT}/sdr-netsnapshot"
echo "    (rm /etc/systemd/network/10-sdr-eth0.network ; systemctl restart systemd-networkd)."
