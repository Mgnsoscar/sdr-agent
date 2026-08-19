#!/usr/bin/env bash
# provision_network.sh (X410) — set the hostname and eth0 addressing on an Ettus/NI
# X410 via systemd-networkd (the stack the NI Alchemy/Zeus image uses — NOT Debian
# netplan/NetworkManager/cloud-init like the Pi version). Achieves the same goals as
# the Pi's deploy/provision_network.sh:
#   1. Persistent hostname (survives reboot)                 -> hostnamectl
#   2. Reachable at <hostname>.local, no reboot               -> avahi + agent restart
#   3. Direct-cable reachability: a stable 169.254.1.N/16 link-local on eth0 that
#      works with no DHCP server (a Windows PC on the other end auto-assigns a
#      169.254.x/16 APIPA address, so no PC IP change is needed)
#   4. (opt-in) A static site IP on eth0 + gateway/DNS
#
# Validated: eth0 is managed by /lib/systemd/network/40-eth0.network; a higher-
# priority /etc/systemd/network/10-sdr-eth0.network overrides it, applied with
# `systemctl restart systemd-networkd` (systemd 243 has no `networkctl reload`).
# The serial/JTAG console is independent of eth0, so this can't lock you out.
#
# Inputs (env):
#   PROV_HOSTNAME   e.g. x410-1   (required; trailing -N sets the .N of the link-local)
#   PROV_STATIC     0 (default, goal 3 only) | 1 (also goal 4)
#   PROV_ETH_IP PROV_PREFIX PROV_GATEWAY PROV_DNS   (static mode)
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
: "${PROV_HOSTNAME:?set PROV_HOSTNAME}"

PROV_STATIC="${PROV_STATIC:-0}"
PERSIST_ROOT="${PERSIST_ROOT:-/data}"
SNAP="$PERSIST_ROOT/sdr-netsnapshot"
NETFILE=/etc/systemd/network/10-sdr-eth0.network

# Link-local host part from the trailing hostname number (x410-1 -> .1); fallback .2.
N="${PROV_HOSTNAME##*-}"
{ [ "$N" -ge 1 ] && [ "$N" -le 254 ]; } 2>/dev/null || N=2
LINKLOCAL="169.254.1.$N/16"

echo "==> Snapshotting current config to $SNAP (for revert)"
mkdir -p "$SNAP"
hostname > "$SNAP/hostname.orig" 2>/dev/null || true
ip -o addr show eth0 > "$SNAP/eth0.addr.orig" 2>/dev/null || true
cp -a /lib/systemd/network/40-eth0.network "$SNAP/" 2>/dev/null || true
[ -f "$NETFILE" ] && cp -a "$NETFILE" "$SNAP/10-sdr-eth0.network.prev" 2>/dev/null || true

echo "==> Setting hostname to $PROV_HOSTNAME"
hostnamectl set-hostname "$PROV_HOSTNAME"

echo "==> Writing $NETFILE"
if [ "$PROV_STATIC" = "1" ]; then
    : "${PROV_ETH_IP:?PROV_ETH_IP required in static mode}"
    : "${PROV_PREFIX:=24}"
    {
        echo "[Match]"
        echo "Name=eth0"
        echo
        echo "[Network]"
        echo "Address=$PROV_ETH_IP/$PROV_PREFIX"
        [ -n "${PROV_GATEWAY:-}" ] && echo "Gateway=$PROV_GATEWAY"
        for d in ${PROV_DNS:-}; do echo "DNS=$d"; done
        echo "Address=$LINKLOCAL           # keep direct-cable reachability too"
    } > "$NETFILE"
    echo "    static $PROV_ETH_IP/$PROV_PREFIX  (+ link-local $LINKLOCAL)"
else
    # Goal 3: DHCP for a real network (WiFi bridge) PLUS a stable link-local for a
    # direct cable. Unlike NetworkManager, networkd doesn't tear the link down when
    # no DHCP server answers, so no optional/`optional:true` equivalent is needed.
    {
        echo "[Match]"
        echo "Name=eth0"
        echo
        echo "[Network]"
        echo "DHCP=yes"
        echo "Address=$LINKLOCAL"
    } > "$NETFILE"
    echo "    dhcp + link-local $LINKLOCAL"
fi

echo "==> Applying (systemctl restart systemd-networkd — 243 has no networkctl reload)"
systemctl restart systemd-networkd
sleep 2
ip -o addr show eth0 | sed 's/^/    /'

echo "==> Re-announcing mDNS under the new hostname (no reboot)"
systemctl restart avahi-daemon 2>/dev/null || true
systemctl restart sdr-agent 2>/dev/null || true

echo "==> Done. Revert: rm $NETFILE ; restore hostname from $SNAP ; systemctl restart systemd-networkd"
