#!/usr/bin/env bash
# provision_network.sh — set a Pi's hostname, and OPTIONALLY a static IP. Run as root,
# LAST in provisioning.
#
# Two modes, chosen by PROV_STATIC:
#   PROV_STATIC=0 (default) — DHCP mode. Set the hostname only, pin it against
#       cloud-init, re-advertise mDNS under the new name, and DO NOT reboot. The unit
#       keeps its current (DHCP / link-local) address and stays reachable throughout —
#       the right default for units reached by broadcaster-N.local across a WiFi, a
#       transparent bridge, or a direct cable (see docs/connectivity.md).
#   PROV_STATIC=1 — static mode. Also write a static IP on eth0 (+ optional wlan0),
#       detecting the stack (NetworkManager vs dhcpcd), then reboot. This drops the SSH
#       session at the IP change; only for a dedicated fleet subnet the PC also joins.
#
# Inputs via environment:
#   PROV_HOSTNAME     e.g. broadcaster-2                         (required)
#   PROV_STATIC       0 (DHCP, default) or 1 (assign a static IP)
#   --- static mode only (PROV_STATIC=1): ---
#   PROV_ETH_IP       e.g. 10.0.0.2         (address only; PROV_PREFIX is the mask)
#   PROV_WLAN_IP      e.g. 10.0.1.2         (optional — omit to leave wlan as-is)
#   PROV_PREFIX       CIDR prefix, e.g. 24
#   PROV_ETH_GW       e.g. 10.0.0.254
#   PROV_WLAN_GW      e.g. 10.0.1.254       (optional; defaults to PROV_ETH_GW)
#   PROV_DNS          space/comma-separated, e.g. "10.0.0.254 1.1.1.1"
#   PROV_WLAN_SSID    WiFi SSID             (optional — only if configuring wlan)
#   PROV_WLAN_PSK     WiFi passphrase       (optional)
#   PROV_NO_REBOOT    set to 1 to skip the reboot (testing)
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

: "${PROV_HOSTNAME:?PROV_HOSTNAME required}"
PROV_STATIC="${PROV_STATIC:-0}"
STAMP="$(date +%Y%m%d-%H%M%S)"

echo "==> Setting hostname to $PROV_HOSTNAME"
hostnamectl set-hostname "$PROV_HOSTNAME"
# Keep /etc/hosts consistent so `sudo` and local name lookups don't hang.
if grep -qE "^127\.0\.1\.1" /etc/hosts; then
    sed -i.bak-"$STAMP" -E "s/^(127\.0\.1\.1\s+).*/\1$PROV_HOSTNAME/" /etc/hosts
else
    cp -a /etc/hosts /etc/hosts.bak-"$STAMP"
    printf '127.0.1.1\t%s\n' "$PROV_HOSTNAME" >> /etc/hosts
fi

# cloud-init (Ubuntu Server for Pi, and some Raspberry Pi OS images) re-applies the
# hostname from its datasource on every boot unless told not to — the classic
# "hostname resets after a reboot". Pin it in BOTH modes. Only in static mode do we
# also disable cloud-init's network management (we own the network then); in DHCP mode
# we must leave it alone or we'd tear down the very DHCP the unit relies on.
if [ -d /etc/cloud ] || command -v cloud-init >/dev/null 2>&1; then
    echo "==> cloud-init detected — pinning hostname"
    mkdir -p /etc/cloud/cloud.cfg.d
    {
        echo "# Written by sdr provision_network.sh ($STAMP)."
        echo "preserve_hostname: true"
        [ "$PROV_STATIC" = "1" ] && echo "network: {config: disabled}"
    } > /etc/cloud/cloud.cfg.d/99-sdr-provision.cfg
fi

# ── DHCP mode: hostname only, no static IP, no reboot ────────────────────────────
if [ "$PROV_STATIC" != "1" ]; then
    echo "==> DHCP mode — no static IP. Re-advertising mDNS under the new hostname."
    systemctl restart avahi-daemon 2>/dev/null || true
    # The agent reads its hostname at startup; restart it so it re-announces as
    # broadcaster-N.local (mDNS) without a reboot.
    systemctl restart sdr-agent 2>/dev/null || true
    echo "==> Done (DHCP). The unit stays reachable at its current address and now"
    echo "    also answers to $PROV_HOSTNAME.local — no reboot needed."
    exit 0
fi

# ── Static mode: write a static IP, then reboot ──────────────────────────────────
: "${PROV_ETH_IP:?PROV_ETH_IP required in static mode}"
: "${PROV_PREFIX:=24}"
: "${PROV_WLAN_GW:=${PROV_ETH_GW:-}}"
DNS_LIST="$(echo "${PROV_DNS:-}" | tr ',' ' ')"

nm_writer() {
    # NetworkManager (Bookworm and later). One connection profile per interface.
    local iface="$1" ip="$2" gw="$3"
    local con
    con="$(nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null | awk -F: -v d="$iface" '$2==d{print $1; exit}')"
    if [ -z "$con" ]; then
        con="prov-$iface"
        nmcli connection add type "${4:-ethernet}" ifname "$iface" con-name "$con" >/dev/null 2>&1 || true
    fi
    echo "    nmcli: $iface -> $ip/$PROV_PREFIX via $gw (profile '$con')"
    nmcli connection modify "$con" \
        ipv4.method manual \
        ipv4.addresses "$ip/$PROV_PREFIX" \
        ${gw:+ipv4.gateway "$gw"} \
        ${DNS_LIST:+ipv4.dns "$(echo "$DNS_LIST" | tr ' ' ',')"}
}

dhcpcd_writer() {
    # dhcpcd (Bullseye and earlier). Append a static block per interface.
    local iface="$1" ip="$2" gw="$3"
    echo "    dhcpcd: $iface -> $ip/$PROV_PREFIX via $gw"
    {
        echo ""
        echo "# --- sdr provisioning ($STAMP) $iface ---"
        echo "interface $iface"
        echo "static ip_address=$ip/$PROV_PREFIX"
        [ -n "$gw" ] && echo "static routers=$gw"
        [ -n "$DNS_LIST" ] && echo "static domain_name_servers=$DNS_LIST"
    } >> /etc/dhcpcd.conf
}

if command -v nmcli >/dev/null 2>&1 && systemctl is-active --quiet NetworkManager; then
    echo "==> Network stack: NetworkManager"
    nm_writer eth0 "$PROV_ETH_IP" "${PROV_ETH_GW:-}" ethernet
    if [ -n "${PROV_WLAN_IP:-}" ]; then
        if [ -n "${PROV_WLAN_SSID:-}" ]; then
            echo "    nmcli: joining WiFi '$PROV_WLAN_SSID'"
            nmcli device wifi connect "$PROV_WLAN_SSID" password "${PROV_WLAN_PSK:-}" ifname wlan0 >/dev/null 2>&1 || true
        fi
        nm_writer wlan0 "$PROV_WLAN_IP" "$PROV_WLAN_GW" wifi
    fi
elif [ -f /etc/dhcpcd.conf ]; then
    echo "==> Network stack: dhcpcd"
    cp -a /etc/dhcpcd.conf /etc/dhcpcd.conf.bak-"$STAMP"
    # For dhcpcd, WiFi credentials live in wpa_supplicant.
    if [ -n "${PROV_WLAN_SSID:-}" ] && [ -n "${PROV_WLAN_IP:-}" ]; then
        WPA=/etc/wpa_supplicant/wpa_supplicant.conf
        if [ -f "$WPA" ] && ! grep -q "ssid=\"$PROV_WLAN_SSID\"" "$WPA"; then
            cp -a "$WPA" "$WPA.bak-$STAMP"
            wpa_passphrase "$PROV_WLAN_SSID" "${PROV_WLAN_PSK:-}" >> "$WPA" 2>/dev/null || true
        fi
    fi
    dhcpcd_writer eth0 "$PROV_ETH_IP" "${PROV_ETH_GW:-}"
    [ -n "${PROV_WLAN_IP:-}" ] && dhcpcd_writer wlan0 "$PROV_WLAN_IP" "$PROV_WLAN_GW"
else
    echo "!! No supported network stack (nmcli/NetworkManager or dhcpcd) found." >&2
    echo "   Hostname was set; static IPs were NOT written. Configure manually." >&2
    exit 2
fi

echo "==> Network config written (backups tagged .bak-$STAMP). Rebooting."
if [ "${PROV_NO_REBOOT:-0}" = "1" ]; then
    echo "   PROV_NO_REBOOT=1 — skipping reboot."
else
    ( sleep 2; systemctl reboot ) &
fi
