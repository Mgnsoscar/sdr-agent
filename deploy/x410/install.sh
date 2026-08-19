#!/usr/bin/env bash
# install.sh (X410) — install the SDR agent onto an Ettus/NI X410 in the OTA
# versioned layout, with NO apt and dependencies from an on-device-built wheelhouse.
# Counterpart to deploy/provision_install.sh (the Debian/Pi version).
#
# Run as root, from inside an unpacked bundle dir:
#   VERSION  agent/  scripts/  paramkit/  requirements.txt  configs/  deploy/  wheels/
#
# ── STATUS: SKELETON ──────────────────────────────────────────────────────────
# Complete once recon (deploy/x410/recon.sh) confirms:
#   * PERSIST_ROOT — a writable path that survives reboot AND an NI OS update.
#   * that `wheels/` was built on THIS device (deploy/x410/build_wheelhouse.sh).
#   * that pip does NOT need --break-system-packages here (adjust PIP_BASE).
# Search this file for TODO(recon).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

# TODO(recon): the persistent partition. On a Pi this is just /opt; on the X410 the
# rootfs is A/B-swapped by Mender, so code + state must live on persistent storage.
PERSIST_ROOT="${PERSIST_ROOT:-/data}"           # <-- confirm on hardware
[ -d "$PERSIST_ROOT" ] && touch "$PERSIST_ROOT/.sdr_w" 2>/dev/null && rm -f "$PERSIST_ROOT/.sdr_w" || {
    echo "PERSIST_ROOT=$PERSIST_ROOT is not writable — set it to the X410's persistent path" >&2
    exit 1
}

BASE="$PERSIST_ROOT/sdr-agent"                  # symlink -> active release
RELEASES="$PERSIST_ROOT/sdr-agent-releases"
SHARED="$PERSIST_ROOT/sdr-agent-shared"         # state: configs/logs/run (survives updates)
DROPIN=/etc/systemd/system/sdr-agent.service.d

HERE="$(cd "$(dirname "$0")/../.." && pwd)"     # bundle root (this script is deploy/x410/)
[ -f "$HERE/VERSION" ] || { echo "no VERSION in bundle at $HERE" >&2; exit 1; }
VERSION="$(tr -d ' \t\r\n' < "$HERE/VERSION")"
[ -n "$VERSION" ] || { echo "empty VERSION" >&2; exit 1; }

echo "==> Provisioning X410 agent $VERSION into $PERSIST_ROOT"

echo "==> Preparing shared state ($SHARED)"
mkdir -p "$SHARED/logs" "$SHARED/run" "$SHARED/configs"
if [ -d "$HERE/configs" ]; then
    for f in "$HERE/configs/."/*; do
        [ -e "$f" ] || continue
        name="$(basename "$f")"
        [ -e "$SHARED/configs/$name" ] || cp -a "$f" "$SHARED/configs/$name"   # never clobber state
    done
fi

echo "==> Laying code down as release $VERSION"
REL="$RELEASES/$VERSION"
mkdir -p "$RELEASES"; rm -rf "$REL"; mkdir -p "$REL"
cp -a "$HERE/agent" "$HERE/scripts" "$HERE/paramkit" "$HERE/requirements.txt" "$REL/"

echo "==> Activating release (symlink $BASE -> $REL)"
ln -sfn "$REL" "$BASE"

echo "==> Installing Python dependencies (wheelhouse-first, NO apt)"
# TODO(recon): drop --break-system-packages if this pip rejects it.
PIP_BASE=(pip3 install --root-user-action=ignore --disable-pip-version-check --no-input)
WHEELS=""; [ -d "$HERE/wheels" ] && WHEELS="$HERE/wheels"
if [ -z "$WHEELS" ]; then
    echo "    !! no wheels/ in the bundle — build it on-device first:" >&2
    echo "       deploy/x410/build_wheelhouse.sh" >&2
fi
# psutil is pip-managed here (no apt), unlike the Pi.
if "${PIP_BASE[@]}" --no-index ${WHEELS:+--find-links "$WHEELS"} -r "$REL/requirements.txt" psutil; then
    echo "    dependencies satisfied offline from the wheelhouse"
else
    echo "    offline install incomplete — trying online (fast fail-out)"
    "${PIP_BASE[@]}" --retries 1 --timeout 15 -r "$REL/requirements.txt" psutil
fi

echo "==> Installing systemd units"
# TODO(recon): if an NI OS update wipes /etc/systemd/system, install these onto the
# persistent partition and symlink them here instead (or accept re-provision).
install -m644 "$HERE/deploy/sdr-agent.service"          /etc/systemd/system/sdr-agent.service
install -m644 "$HERE/deploy/sdr-agent-confirm.service"  /etc/systemd/system/sdr-agent-confirm.service
install -m644 "$HERE/deploy/sdr-agent-confirm.timer"    /etc/systemd/system/sdr-agent-confirm.timer
install -m755 "$HERE/deploy/sdr-agent-confirm.sh"       /usr/local/bin/sdr-agent-confirm

echo "==> Writing service env drop-in ($DROPIN/override.conf)"
# This is where the X410's persistent paths are injected — no code change needed,
# config.py reads all of these from the environment.
mkdir -p "$DROPIN"
{
    echo "[Service]"
    # The packaged sdr-agent.service hardcodes /opt/sdr-agent for WorkingDirectory +
    # PYTHONPATH; a drop-in overrides both so the agent runs from the persistent
    # release without shipping a separate unit file.
    printf 'WorkingDirectory=%s\n'              "$BASE"
    printf 'Environment=PYTHONPATH=%s\n'        "$BASE"
    printf 'Environment=SDR_AGENT_BASE=%s\n'    "$BASE"
    printf 'Environment=SDR_STATE_DIR=%s\n'     "$SHARED"
    printf 'Environment=SDR_RELEASES_DIR=%s\n'  "$RELEASES"
    printf 'Environment=SDR_CURRENT_LINK=%s\n'  "$BASE"
    # Never advertise the internal RFSoC management NIC (169.254.0.1) as a unit
    # address — a client can't reach it and it isn't ours to expose.
    printf 'Environment=SDR_MDNS_EXCLUDE_IFACES=int0\n'
    [ -n "${SDR_UNIT_ID:-}" ] && printf 'Environment=SDR_UNIT_ID=%s\n' "$SDR_UNIT_ID"
    [ -n "${SDR_API_KEY:-}" ] && printf 'Environment=SDR_API_KEY=%s\n' "$SDR_API_KEY"
} > "$DROPIN/override.conf"
chmod 600 "$DROPIN/override.conf"

echo "==> Enabling + starting the agent"
systemctl daemon-reload
systemctl enable --now sdr-agent-confirm.timer
systemctl enable sdr-agent
systemctl restart sdr-agent

echo "==> Done. Layout:"; ls -l "$BASE"
echo "    releases: $(ls "$RELEASES" 2>/dev/null | tr '\n' ' ')"
echo "    state:    $SHARED"
echo "    verify:   curl -s http://127.0.0.1:8765/info"
