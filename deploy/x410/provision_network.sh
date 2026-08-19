#!/usr/bin/env bash
# provision_network.sh (X410) — set hostname + a static eth0 address on an Ettus/NI
# X410, the way the X410's own network stack wants it (NOT Debian netplan/cloud-init,
# which the Pi version uses and which does not exist here).
#
# ── STATUS: STUB — do not run until recon confirms the network stack ──────────
# The Pi's deploy/provision_network.sh writes /etc/netplan + a cloud-init drop-in and
# reapplies via nmcli. None of that applies on Yocto. recon.sh reports which of these
# manages eth0; wire up the matching branch below, and ALWAYS snapshot first so the
# borrowed unit can be reverted at hand-back.
#
# Inputs (env):
#   PROV_HOSTNAME   e.g. x410-1
#   PROV_ETH_IP     e.g. 10.0.0.5
#   PROV_PREFIX     e.g. 24
#   PROV_GATEWAY    e.g. 10.0.0.254   (optional)
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

: "${PROV_HOSTNAME:?set PROV_HOSTNAME}"
SNAP="/data/sdr-agent-netsnapshot"   # revert record; TODO(recon): persistent path

echo "==> Snapshotting current network + hostname to $SNAP (for revert)"
mkdir -p "$SNAP"
hostname > "$SNAP/hostname.orig" 2>/dev/null || true
ip -o addr show eth0 > "$SNAP/eth0.addr.orig" 2>/dev/null || true
command -v networkctl >/dev/null && networkctl status eth0 > "$SNAP/eth0.networkd.orig" 2>/dev/null || true
command -v connmanctl >/dev/null && connmanctl services > "$SNAP/connman.orig" 2>/dev/null || true

echo "==> Setting hostname to $PROV_HOSTNAME"
# hostnamectl is the systemd-native, reboot-persistent way (present on the X410).
if command -v hostnamectl >/dev/null; then
    hostnamectl set-hostname "$PROV_HOSTNAME"
else
    echo "!! hostnamectl not found — TODO(recon): the X410's persistent hostname method"
fi

echo "==> Static eth0 address"
# TODO(recon): implement the branch matching the X410's stack. Sketches:
#
#   systemd-networkd:
#     write /etc/systemd/network/10-eth0.network with [Match] Name=eth0 and
#     [Network] Address=$PROV_ETH_IP/$PROV_PREFIX (+ Gateway=$PROV_GATEWAY), then
#     `networkctl reload` / `systemctl restart systemd-networkd`.
#
#   connman:
#     `connmanctl config <ethernet-service> --ipv4 manual $PROV_ETH_IP <mask> $PROV_GATEWAY`
#
#   NI-managed config:
#     use NI's documented mechanism (usrp_mpm / MPM config), if that's what owns eth0.
echo "!! not implemented — see the sketches above; wire the confirmed stack. NOOP for now."

echo "==> Snapshot saved. Revert with the files in $SNAP."
