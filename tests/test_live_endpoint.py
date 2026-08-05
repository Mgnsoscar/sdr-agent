"""End-to-end: a real subprocess using paramkit.live is launched by the
ProcessManager, and manager.set_params() retunes it over the control socket the
agent provisioned via SDR_CTRL_SOCK."""
import asyncio
import time
from pathlib import Path

import pytest

from agent import process_manager as pm
from agent.log_manager import LogManager
from agent.models import TaskConfig

REPO_ROOT = str(Path(__file__).resolve().parents[1])

SCRIPT = '''\
import time
from paramkit import Script

s = (Script("live test script")
     .integer("--gain", unit="dB", min=0, max=49, default=30, live=True)
     .number("--freq", unit="Hz", min=1.0, max=1e12, default=100.0, live=True))
args = s.parse()
ctrl = s.live_control(args)

gain, freq = args.gain, args.freq
while True:
    for ch in ctrl.drain():
        if ch.name == "gain":
            gain = (int(ch.value) // 2) * 2      # mimic a device with even steps
            ctrl.report("gain", gain)
        elif ch.name == "freq":
            freq = ch.value
            ctrl.report("freq", freq)
    time.sleep(0.01)
'''


def _wait_for(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_set_params_end_to_end(tmp_path, monkeypatch):
    # Control sockets under a writable dir (default is /opt/sdr-agent/run/ctl).
    monkeypatch.setattr(pm._agentcfg, "CTRL_DIR", tmp_path / "ctl")

    script = tmp_path / "live_script.py"
    script.write_text(SCRIPT)

    task = TaskConfig(
        name="livetask",
        command=["python3", str(script)],
        working_dir=str(tmp_path),
        env={"PYTHONPATH": REPO_ROOT},        # so the subprocess can import paramkit
    )
    proc = pm.ManagedProcess(task, LogManager(tmp_path, "livetask"),
                             pm.EventDispatcher(), unit_id="u")

    async def scenario():
        await proc.start()
        sock = Path(proc._ctrl_sock)
        # The script binds the socket shortly after launch.
        assert _wait_for(sock.exists), "control socket never appeared"

        # Retune: gain 41 → device quantises to 40; freq passes through.
        resp = await proc.set_params({"gain": 41, "freq": 250e6}, wait=3.0)
        assert resp["ok"] is True, resp
        assert resp["rejected"] == {}
        assert resp["applied"]["gain"] == 40
        assert resp["applied"]["freq"] == 250e6
        assert resp["pending"] == []

        # Out-of-range is rejected, not applied.
        bad = await proc.set_params({"gain": 999}, wait=0.5)
        assert "gain" in bad["rejected"]

        # get reflects the settled state.
        got = await proc.get_params()
        assert got["applied"]["gain"] == 40

        await proc.stop()
        # Socket is cleaned up on stop.
        assert _wait_for(lambda: not sock.exists(), timeout=3.0)

    asyncio.run(scenario())


def test_set_params_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr(pm._agentcfg, "CTRL_DIR", tmp_path / "ctl")
    task = TaskConfig(name="idle", command=["python3", "-c", "pass"],
                      working_dir=str(tmp_path))
    proc = pm.ManagedProcess(task, LogManager(tmp_path, "idle"),
                             pm.EventDispatcher(), unit_id="u")
    with pytest.raises(RuntimeError):
        asyncio.run(proc.set_params({"gain": 1}))
