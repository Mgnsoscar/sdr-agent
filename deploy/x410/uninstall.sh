#!/usr/bin/env bash
# uninstall.sh (X410) — remove the SDR agent and leave the borrowed unit as it was.
# Run as root. Reverts everything install.sh created. Networking/hostname changes are
# NOT touched here — revert those with the snapshot recon.sh recorded (see G4/G7).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

PERSIST_ROOT="${PERSIST_ROOT:-/data}"           # must match install.sh
BASE="$PERSIST_ROOT/sdr-agent"
RELEASES="$PERSIST_ROOT/sdr-agent-releases"
SHARED="$PERSIST_ROOT/sdr-agent-shared"

KEEP_STATE="${KEEP_STATE:-0}"                    # set 1 to preserve $SHARED (configs/logs)

echo "==> Stopping + disabling services"
systemctl disable --now sdr-agent 2>/dev/null || true
systemctl disable --now sdr-agent-confirm.timer 2>/dev/null || true
systemctl stop sdr-agent-confirm.service 2>/dev/null || true

echo "==> Removing systemd units + drop-in"
rm -f /etc/systemd/system/sdr-agent.service
rm -f /etc/systemd/system/sdr-agent-confirm.service
rm -f /etc/systemd/system/sdr-agent-confirm.timer
rm -f /usr/local/bin/sdr-agent-confirm
rm -rf /etc/systemd/system/sdr-agent.service.d
systemctl daemon-reload

echo "==> Removing code + releases"
rm -rf "$BASE" "$RELEASES"          # $BASE is a symlink; -rf removes the link only
if [ "$KEEP_STATE" = "1" ]; then
    echo "    keeping state at $SHARED (KEEP_STATE=1)"
else
    rm -rf "$SHARED"
fi

echo "==> Footprint check — anything left below should be nothing:"
ls -d "$BASE" "$RELEASES" "$SHARED" 2>/dev/null || echo "    (clean)"
echo "==> Done. NOTE: pip-installed deps remain in the system Python (harmless);"
echo "    remove with: pip3 uninstall -y -r $PERSIST_ROOT/... (only if required for hand-back)."
echo "    If you changed hostname/eth0, revert them from the recon snapshot."
