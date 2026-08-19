#!/usr/bin/env bash
# provision_network.sh (X410) — achieve the SAME provisioning goals as the Pi's
# deploy/provision_network.sh, but via the X410's own network stack (NOT Debian
# netplan/NetworkManager/cloud-init, none of which exist here).
#
# Goals carried over from the Pi (see docs/x410-agent-port.md G4):
#   1. Persistent hostname that survives reboot          -> hostnamectl (native)
#   2. Reachable at <hostname>.local with no reboot       -> avahi + agent restart
#   3. Direct-cable reachability: stable 169.254.1.N/16 on eth0 with no DHCP server
#   4. (opt-in) Static site IP on eth0 + gateway/DNS
#
# ── STATUS: STUB — hostname (goal 1/2) is wired; the IP parts (3/4) NOOP until
#    recon confirms the stack. Most likely systemd-networkd, in which case goals
#    3 and 4 are a single .network file (sketches below). ALWAYS snapshots first.
#
# Inputs (env):
#   PROV_HOSTNAME   e.g. x410-1                (required; trailing -N gives the link-local)
#   PROV_STATIC     0 (default, goal 3 only) | 1 (also goal 4, static site IP)
#   PROV_ETH_IP     e.g. 10.0.0.5             (static mode)
#   PROV_PREFIX     e.g. 24                   (static mode)
#   PROV_GATEWAY    e.g. 10.0.0.254           (static mode, optional)
#   PROV_DNS        e.g. "10.0.0.254 1.1.1.1" (static mode, optional)
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

N="${PROV_HOSTNAME##*-}"            # trailing number → the .N of the link-local
PROV_STATIC="${PROV_STATIC:-0}"

echo "==> eth0 addressing (goal 3 always; goal 4 if PROV_STATIC=1)"
# TODO(recon): implement ONE branch, matching the stack recon reports.
#
# ── systemd-networkd (most likely) — goals 3 AND 4 are a single .network file ──
#   Goal 3 (default): keep DHCP for the WiFi-bridge case, add the stable link-local
#   for the direct-cable case. networkd, unlike NetworkManager, does NOT tear the
#   link down when there's no DHCP server, so no `optional:true` equivalent is
#   needed — this is strictly simpler than the Pi.
#
#     cat > /etc/systemd/network/10-sdr-eth0.network <<NET
#     [Match]
#     Name=eth0
#     [Network]
#     DHCP=yes
#     LinkLocalAddressing=ipv4            # first-class here (was a no-op under netplan)
#     Address=169.254.1.$N/16            # stable per-unit direct-cable address
#     NET
#
#   Goal 4 (PROV_STATIC=1): instead of DHCP, pin the site IP + gateway + DNS:
#     [Network]
#     Address=$PROV_ETH_IP/${PROV_PREFIX:-24}
#     Gateway=$PROV_GATEWAY
#     DNS=$PROV_DNS
#     Address=169.254.1.$N/16            # keep direct-cable reachable too
#
#   Apply surgically (no reboot): `networkctl reload` then `networkctl reconfigure eth0`.
#
# ── connman ── goal 3: manual link-local; goal 4:
#     connmanctl config <eth-service> --ipv4 manual $PROV_ETH_IP <mask> $PROV_GATEWAY
# ── NI-managed ── use NI's documented mechanism (usrp_mpm / MPM), if it owns eth0.
echo "!! eth0 addressing not implemented — wire the confirmed stack above. NOOP for now."

echo "==> Re-announce mDNS under the new hostname (goal 2, no reboot)"
systemctl restart avahi-daemon 2>/dev/null || true
systemctl restart sdr-agent 2>/dev/null || true

echo "==> Snapshot saved to $SNAP. Revert hostname/network from those files at hand-back."
