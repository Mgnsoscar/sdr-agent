#!/usr/bin/env bash
# install.sh — run once on each Pi as root (or with sudo)
set -euo pipefail

INSTALL_DIR="/opt/sdr-agent"
SERVICE_FILE="/etc/systemd/system/sdr-agent.service"

echo "==> Wiping any previous install"
systemctl stop sdr-agent 2>/dev/null || true
rm -rf "$INSTALL_DIR"

echo "==> Creating directory layout"
mkdir -p "$INSTALL_DIR"/logs

echo "==> Copying files"
# Copy directories to parent so they land as /opt/sdr-agent/agent, not /opt/sdr-agent/agent/agent
cp -r agent       "$INSTALL_DIR/"
cp -r scripts     "$INSTALL_DIR/"
cp -r configs     "$INSTALL_DIR/"
cp -r paramkit    "$INSTALL_DIR/"   # importable by scripts via PYTHONPATH (see service)
cp requirements.txt "$INSTALL_DIR/"

echo "==> Installing system packages (apt)"
# psutil ships as a Debian package on Raspberry Pi OS; installing it via apt
# avoids pip trying (and failing) to uninstall the apt-managed copy.
apt-get update -qq
apt-get install -y python3-psutil

echo "==> Installing Python dependencies (pip)"
pip3 install --break-system-packages --root-user-action=ignore \
    --upgrade --upgrade-strategy only-if-needed \
    -r "$INSTALL_DIR/requirements.txt"

echo "==> Installing systemd service"
cp sdr-agent.service "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable sdr-agent
systemctl restart sdr-agent

echo "==> Done. Check status with: systemctl status sdr-agent"