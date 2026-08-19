#!/usr/bin/env bash
# recon.sh — Phase 0 fact-finding for porting the SDR agent to an Ettus/NI X410.
#
# READ-ONLY: this changes nothing. Run it on the X410 (root@<host>) and paste the
# output back. It answers the open questions in docs/x410-agent-port.md
# (G1 wheels, G2 persistent storage, G4 networking, G6 thermal).
#
#   ssh root@<x410-host> 'bash -s' < deploy/x410/recon.sh
# or copy it over and run `bash recon.sh`.
set -u

line() { printf '\n=== %s ===\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

line "OS / kernel"
uname -a
[ -r /etc/os-release ] && cat /etc/os-release || echo "(no /etc/os-release)"

line "Python + platform tags (G1 — must match the wheelhouse)"
if have python3; then
    python3 --version
    python3 - <<'PY'
import sysconfig, platform, sys
print("machine       :", platform.machine())
print("platform tag  :", sysconfig.get_platform())
print("impl/abi      :", f"cp{sys.version_info.major}{sys.version_info.minor}")
try:
    import pip; print("pip           :", pip.__version__)
except Exception as e:
    print("pip           : NOT importable:", e)
PY
else
    echo "!! python3 NOT found — this is a blocker"
fi
have pip3 && { echo "--- pip3 marker test (does it need --break-system-packages?)"; pip3 install --help 2>&1 | grep -c break-system-packages; }

line "UHD (G — SDR probe uses this)"
have uhd_find_devices && { echo "uhd_find_devices: $(command -v uhd_find_devices)"; uhd_find_devices 2>&1 | head -30; } || echo "!! uhd_find_devices NOT on PATH"
have gnuradio-config-info && gnuradio-config-info --version || echo "gnuradio: not found (only needed if IQ replay uses a flowgraph)"

line "systemd (OTA restart + service depend on this)"
have systemctl && systemctl --version | head -1 || echo "!! systemctl NOT found — OTA/service model needs it"

line "Identity"
echo "hostname     : $(hostname 2>/dev/null)"
echo -n "machine-id   : "; cat /etc/machine-id 2>/dev/null || echo "(missing)"

line "Writable / persistent storage (G2 — must survive reboot AND OS update)"
mount | grep -Ei 'data|persist|overlay|mmcblk|ubi' || true
echo "--- df:"; df -h 2>/dev/null | grep -Ev 'tmpfs|devtmpfs' || df -h
for c in /data /mnt/data /var/lib /home/root /opt; do
    if [ -d "$c" ]; then
        if touch "$c/.sdr_write_test" 2>/dev/null; then
            echo "writable: $c   (rm test file)"; rm -f "$c/.sdr_write_test"
        else
            echo "read-only or denied: $c"
        fi
    fi
done

line "Thermal (G6 — find the SoC zone)"
for z in /sys/class/thermal/thermal_zone*/type; do
    [ -r "$z" ] && printf '%s -> %s (%s)\n' "$z" "$(cat "$z")" "$(cat "${z%type}temp" 2>/dev/null)"
done

line "Networking (G4 — which stack manages eth0; snapshot for revert)"
if have networkctl; then echo "-- systemd-networkd:"; networkctl status eth0 2>/dev/null | head -20; fi
if have connmanctl; then echo "-- connman services:"; connmanctl services 2>/dev/null; fi
echo "-- addresses:"; ip -o addr show 2>/dev/null || ifconfig -a 2>/dev/null
echo "-- default route:"; ip route show default 2>/dev/null

line "Internet reachability (offline-first install?)"
curl -sSI --max-time 8 https://pypi.org >/dev/null 2>&1 && echo "pypi reachable" || echo "pypi NOT reachable (offline — wheelhouse is mandatory)"

line "DONE — paste everything above back."
