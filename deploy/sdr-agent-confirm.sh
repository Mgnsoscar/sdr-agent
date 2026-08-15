#!/usr/bin/env bash
# sdr-agent-confirm — roll back an agent release that never confirmed itself healthy.
#
# Run periodically by sdr-agent-confirm.timer. A freshly-activated release leaves a
# `pending` marker; the running agent clears it (writes <ver>.ok) once it has served
# cleanly for the confirm delay. If the agent crashed or hung, the marker stays, and
# after the grace period this script reverts the `current` symlink to the previous
# release and restarts the service.
#
# Deliberately self-contained (pure shell, no agent import) so it still works when
# the new release is completely broken. Mirrors Updater.rollback — keep them in sync.
set -euo pipefail

RELEASES="${SDR_RELEASES_DIR:-/opt/sdr-agent-releases}"
LINK="${SDR_CURRENT_LINK:-/opt/sdr-agent}"
GRACE="${SDR_UPDATE_HEALTH_GRACE_S:-90}"
SERVICE="${SDR_SERVICE_NAME:-sdr-agent}"
M="$RELEASES/.markers"

[ -f "$M/pending" ] || exit 0
PENDING="$(cat "$M/pending" 2>/dev/null || true)"
[ -n "$PENDING" ] || exit 0
[ -f "$M/$PENDING.ok" ] && exit 0                      # agent confirmed it healthy

AGE=$(( $(date +%s) - $(stat -c %Y "$M/pending") ))
[ "$AGE" -lt "$GRACE" ] && exit 0                      # still inside the grace window

PREV="$(cat "$M/previous" 2>/dev/null || true)"
if [ -z "$PREV" ] || [ ! -d "$RELEASES/$PREV" ]; then
    echo "sdr-agent-confirm: release $PENDING unhealthy but no previous to roll back to" >&2
    exit 1
fi

echo "sdr-agent-confirm: release $PENDING unhealthy after ${AGE}s — rolling back to $PREV" >&2
ln -sfn "$RELEASES/$PREV" "$LINK.rollback"
mv -T "$LINK.rollback" "$LINK"
rm -f "$M/pending"
systemctl restart "$SERVICE"
