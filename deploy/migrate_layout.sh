#!/usr/bin/env bash
# migrate_layout.sh — one-time migration of a classic /opt/sdr-agent install to the
# OTA versioned layout. Run once per Pi as root, from a checkout of this repo.
#
#   Before:  /opt/sdr-agent/{agent,scripts,paramkit,configs,logs,run}   (plain dir)
#   After:   /opt/sdr-agent            -> symlink to the active release
#            /opt/sdr-agent-releases/<version>/{agent,scripts,paramkit,requirements.txt}
#            /opt/sdr-agent-shared/{configs,logs,run}                    (state; survives updates)
#
# Idempotent: if /opt/sdr-agent is already a symlink, only the service + timer are
# refreshed. Safe to re-run.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

BASE=/opt/sdr-agent
RELEASES=/opt/sdr-agent-releases
SHARED=/opt/sdr-agent-shared
HERE="$(cd "$(dirname "$0")/.." && pwd)"   # repo root

VERSION="$(python3 - <<'PY'
import re
print(re.search(r'AGENT_VERSION\s*=\s*"([^"]+)"', open("/opt/sdr-agent/agent/config.py").read()).group(1))
PY
)"

systemctl stop sdr-agent 2>/dev/null || true
mkdir -p "$RELEASES" "$SHARED"

if [ ! -L "$BASE" ]; then
    echo "==> Moving state into $SHARED"
    for d in configs logs run; do
        if [ -e "$BASE/$d" ]; then
            mkdir -p "$SHARED/$d"
            cp -a "$BASE/$d/." "$SHARED/$d/" 2>/dev/null || true
        fi
    done

    echo "==> Laying current code down as release $VERSION"
    REL="$RELEASES/$VERSION"
    rm -rf "$REL"; mkdir -p "$REL"
    cp -a "$BASE/agent" "$BASE/scripts" "$BASE/paramkit" "$BASE/requirements.txt" "$REL/"

    echo "==> Replacing $BASE with a symlink to the release"
    rm -rf "$BASE"
    ln -sfn "$REL" "$BASE"
else
    echo "==> $BASE is already a symlink; refreshing units only"
fi

echo "==> Installing OTA service + confirm timer"
install -m644 "$HERE/deploy/sdr-agent.service"          /etc/systemd/system/sdr-agent.service
install -m644 "$HERE/deploy/sdr-agent-confirm.service"  /etc/systemd/system/sdr-agent-confirm.service
install -m644 "$HERE/deploy/sdr-agent-confirm.timer"    /etc/systemd/system/sdr-agent-confirm.timer
install -m755 "$HERE/deploy/sdr-agent-confirm.sh"       /usr/local/bin/sdr-agent-confirm

systemctl daemon-reload
systemctl enable --now sdr-agent-confirm.timer
systemctl restart sdr-agent

echo "==> Done. Layout:"
ls -l "$BASE"
echo "    releases: $(ls "$RELEASES" 2>/dev/null | tr '\n' ' ')"
echo "    state:    $SHARED"
