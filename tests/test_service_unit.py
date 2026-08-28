"""Guard the shipped systemd unit's restart behaviour.

An OTA update restarts the agent while a client is still polling it (keep-alive
connections open). uvicorn's graceful shutdown must be BOUNDED, or the old process
hangs "waiting for connections to close" until the stop timeout — past the OTA
health-confirm grace — and the freshly-activated release is rolled back before it can
start. These assertions keep that bound in place (and safely under the grace window)."""
import re
from pathlib import Path

_UNIT = Path(__file__).resolve().parents[1] / "deploy" / "sdr-agent.service"
_GRACE_S = 90.0   # SDR_UPDATE_HEALTH_GRACE_S default in deploy/sdr-agent-confirm.service


def _field(text: str, key: str):
    m = re.search(rf"(?m)^{re.escape(key)}=(.+)$", text)
    return m.group(1).strip() if m else None


def test_uvicorn_graceful_shutdown_is_bounded():
    exec_start = _field(_UNIT.read_text(), "ExecStart")
    assert exec_start and "uvicorn" in exec_start
    m = re.search(r"--timeout-graceful-shutdown\s+(\d+)", exec_start)
    assert m, "ExecStart must bound uvicorn's graceful shutdown"
    assert 0 < int(m.group(1)) < _GRACE_S


def test_stop_timeout_is_under_the_confirm_grace():
    stop = _field(_UNIT.read_text(), "TimeoutStopSec")
    assert stop, "TimeoutStopSec must backstop a stuck restart"
    secs = int(re.match(r"(\d+)", stop).group(1))
    # Comfortably under the grace so a restart never outlasts the rollback window.
    assert 0 < secs < _GRACE_S
