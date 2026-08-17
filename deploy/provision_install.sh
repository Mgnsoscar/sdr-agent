#!/usr/bin/env bash
# provision_install.sh — install the agent onto a FRESH Pi in the OTA versioned
# layout, straight from an unpacked bundle. Run as root, from inside an unpacked
# bundle directory (VERSION + agent/ scripts/ paramkit/ requirements.txt + configs/
# + deploy/). This is the from-scratch counterpart to migrate_layout.sh (which
# converts an existing classic install); the client's "Provision unit" flow uploads
# the bundle, unpacks it, and runs this.
#
# Lays down:
#   /opt/sdr-agent            -> symlink to the active release
#   /opt/sdr-agent-releases/<version>/{agent,scripts,paramkit,requirements.txt}
#   /opt/sdr-agent-shared/{configs,logs,run}          (state; survives updates)
#   /etc/systemd/system/sdr-agent.service.d/override.conf   (SDR_UNIT_ID/SDR_API_KEY)
#
# Non-destructive on re-run: existing shared state (configs/logs) is kept; only the
# release for this version is (re)written and activated.
#
# Inputs via environment:
#   SDR_UNIT_ID   unit id baked into the service env (e.g. broadcaster-2). Optional.
#   SDR_API_KEY   fleet API key baked into the service env. Optional (but recommended).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

HERE="$(cd "$(dirname "$0")/.." && pwd)"   # bundle root (this script is in deploy/)
BASE=/opt/sdr-agent
RELEASES=/opt/sdr-agent-releases
SHARED=/opt/sdr-agent-shared
DROPIN=/etc/systemd/system/sdr-agent.service.d

[ -f "$HERE/VERSION" ] || { echo "no VERSION in bundle at $HERE" >&2; exit 1; }
VERSION="$(tr -d ' \t\r\n' < "$HERE/VERSION")"
[ -n "$VERSION" ] || { echo "empty VERSION" >&2; exit 1; }

echo "==> Provisioning agent $VERSION"

# Bootstrap an existing CLASSIC install (a real dir at $BASE, not the versioned
# symlink) — e.g. a unit still running a pre-OTA agent, which is why the client's
# Update button 404s (no /admin/update route yet). Preserve its state into $SHARED
# before we replace it with the symlink, so the unit's sequences/plans/library
# survive the switch to the OTA layout. A fresh Pi has no $BASE and skips this.
if [ -e "$BASE" ] && [ ! -L "$BASE" ]; then
    echo "==> Existing classic install found at $BASE — migrating its state to $SHARED"
    systemctl stop sdr-agent 2>/dev/null || true
    mkdir -p "$SHARED"
    for d in configs logs run; do
        if [ -d "$BASE/$d" ] && [ ! -e "$SHARED/$d" ]; then
            cp -a "$BASE/$d" "$SHARED/$d"
        fi
    done
    rm -rf "$BASE"
fi

echo "==> Preparing shared state ($SHARED)"
mkdir -p "$SHARED/logs" "$SHARED/run" "$SHARED/configs"
# Seed default configs only where absent — never clobber a unit's existing state.
if [ -d "$HERE/configs" ]; then
    for f in "$HERE/configs/."/*; do
        [ -e "$f" ] || continue
        name="$(basename "$f")"
        if [ ! -e "$SHARED/configs/$name" ]; then
            cp -a "$f" "$SHARED/configs/$name"
        fi
    done
fi

echo "==> Laying code down as release $VERSION"
REL="$RELEASES/$VERSION"
mkdir -p "$RELEASES"
rm -rf "$REL"; mkdir -p "$REL"
cp -a "$HERE/agent" "$HERE/scripts" "$HERE/paramkit" "$HERE/requirements.txt" "$REL/"

echo "==> Activating release (symlink $BASE -> $REL)"
ln -sfn "$REL" "$BASE"

echo "==> Installing system packages (apt)"
# psutil ships as a Debian package; only reach for apt (needs internet) if it isn't
# already importable — so re-provisioning an offline unit doesn't hang on apt.
if python3 -c "import psutil" 2>/dev/null; then
    echo "    psutil already present — skipping apt"
else
    apt-get update -qq && apt-get install -y python3-psutil >/dev/null || \
        echo "    !! could not install python3-psutil (no internet?) — will try a pip wheel"
fi

echo "==> Installing Python dependencies (pip, offline-first)"
# Offline-first: an install that never touches the network finishes instantly when
# the deps are already satisfied, and fails fast (instead of pip retrying an
# unreachable PyPI for every package) when they aren't. Only then fall back online.
PIP_BASE=(pip3 install --break-system-packages --root-user-action=ignore
          --disable-pip-version-check --no-input)
WHEELS=""; [ -d "$HERE/wheels" ] && WHEELS="$HERE/wheels"
if "${PIP_BASE[@]}" --no-index ${WHEELS:+--find-links "$WHEELS"} -r "$REL/requirements.txt"; then
    echo "    dependencies satisfied offline"
else
    echo "    offline install incomplete — falling back to online (fast fail-out)"
    "${PIP_BASE[@]}" --retries 1 --timeout 15 \
        --upgrade --upgrade-strategy only-if-needed -r "$REL/requirements.txt"
fi

echo "==> Installing systemd units"
install -m644 "$HERE/deploy/sdr-agent.service"          /etc/systemd/system/sdr-agent.service
install -m644 "$HERE/deploy/sdr-agent-confirm.service"  /etc/systemd/system/sdr-agent-confirm.service
install -m644 "$HERE/deploy/sdr-agent-confirm.timer"    /etc/systemd/system/sdr-agent-confirm.timer
install -m755 "$HERE/deploy/sdr-agent-confirm.sh"       /usr/local/bin/sdr-agent-confirm

echo "==> Writing service env drop-in ($DROPIN/override.conf)"
mkdir -p "$DROPIN"
{
    echo "[Service]"
    [ -n "${SDR_UNIT_ID:-}" ] && printf 'Environment=SDR_UNIT_ID=%s\n' "$SDR_UNIT_ID"
    [ -n "${SDR_API_KEY:-}" ] && printf 'Environment=SDR_API_KEY=%s\n' "$SDR_API_KEY"
} > "$DROPIN/override.conf"
chmod 600 "$DROPIN/override.conf"   # holds the API key

echo "==> Enabling + starting the agent"
systemctl daemon-reload
systemctl enable --now sdr-agent-confirm.timer
systemctl enable sdr-agent
systemctl restart sdr-agent

echo "==> Done. Layout:"
ls -l "$BASE"
echo "    releases: $(ls "$RELEASES" 2>/dev/null | tr '\n' ' ')"
echo "    state:    $SHARED"
