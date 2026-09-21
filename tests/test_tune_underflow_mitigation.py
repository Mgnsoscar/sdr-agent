"""Live-tune underflow mitigations (docs/rf-fault-recovery.md §14o, 1.36.5).

Owner report: the P-code task at 61.38 MS/s is stable until a parameter is tuned; a `--power` tune
(each point of a ramp) occasionally underflows hard enough to RF-fault. Two agent-side causes: every
power tune spawned the attenuator one-shot (a fresh interpreter importing paramkit + a serial port)
at the transmitter's own priority, even when the realization landed on the SAME attenuation as
before; and nothing favoured the transmitter's producer thread over that spawn or the agent itself.

Covers:
  * a live tune whose realization repeats the component's last successful setting spawns NOTHING;
    a different setting does; a launch always re-sends; a failed set forgets the position;
  * `_set_nice` sets the niceness right after a spawn, ignores 0, and swallows a PermissionError;
  * LIVE: a launched task runs at TASK_NICE and an active-set one-shot at ONESHOT_NICE (as root).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest                                                        # noqa: E402
from agent import process_manager as pm                              # noqa: E402
from agent.models import ProcessState, StartRequest, TaskConfig        # noqa: E402
import test_active_freq_consistency as A                              # noqa: E402  (_mgr/_capture/_atten_at)


# ── the identical-set skip ───────────────────────────────────────────────────────

def test_a_repeated_identical_tune_does_not_respawn_the_attenuator(tmp_path, monkeypatch):
    mgr = A._mgr(tmp_path, monkeypatch)
    fired, _ = A._capture(mgr, monkeypatch)
    asyncio.run(mgr.start("tx", StartRequest(args=["--freq", "1575.42", "--power", "-30"],
                                             replace_args=True), source="sequence"))
    assert len(fired) == 1                                            # the launch positions it
    asyncio.run(mgr.set_params("tx", {"power": -30.0}))               # the same level again
    assert len(fired) == 1                                            # nothing to re-send
    asyncio.run(mgr.set_params("tx", {"rf": "on"}))                   # gate already on → same setting
    assert len(fired) == 1
    asyncio.run(mgr.set_params("tx", {"power": -60.0}))               # a level that moves the attenuator
    assert len(fired) == 2
    assert fired[-1] == ("atten_set", ["--attenuation", pm._fmt_num(A._atten_at(1575.42e6, -60.0))])
    asyncio.run(mgr.set_params("tx", {"power": -60.0}))
    assert len(fired) == 2                                            # …and only once


def test_mute_and_unmute_still_resend_and_a_launch_always_does(tmp_path, monkeypatch):
    mgr = A._mgr(tmp_path, monkeypatch)
    fired, _ = A._capture(mgr, monkeypatch)
    asyncio.run(mgr.start("tx", StartRequest(args=["--freq", "1575.42", "--power", "-30"],
                                             replace_args=True), source="sequence"))
    asyncio.run(mgr.set_params("tx", {"rf": "off"}))
    assert fired[-1] == ("atten_set", ["--attenuation", "95"]) and len(fired) == 2
    asyncio.run(mgr.set_params("tx", {"rf": "off"}))                  # already muted
    assert len(fired) == 2
    asyncio.run(mgr.set_params("tx", {"rf": "on"}))                   # back to the level's setting
    assert len(fired) == 3
    # A (re)launch at the very same setting re-sends regardless — the position may have changed
    # under the agent's feet (another task, a power cycle) and a launch is not the hot path.
    asyncio.run(mgr.start("tx", StartRequest(args=["--freq", "1575.42", "--power", "-30"],
                                             replace_args=True), source="sequence"))
    assert len(fired) == 4 and fired[-1] == fired[0]


def test_a_failed_set_is_not_remembered(tmp_path, monkeypatch):
    mgr = A._mgr(tmp_path, monkeypatch)
    fired, _ = A._capture(mgr, monkeypatch)
    codes = iter([0, 1, 0])
    async def flaky(name, args, timeout=pm._ACTIVE_SET_TIMEOUT_S):
        fired.append((name, list(args)))
        return next(codes)
    monkeypatch.setattr(mgr, "_launch_oneshot_wait", flaky)
    asyncio.run(mgr.start("tx", StartRequest(args=["--freq", "1575.42", "--power", "-30"],
                                             replace_args=True), source="sequence"))
    with pytest.raises(RuntimeError):                                 # exits 1 → the RF-on tune is REFUSED (§14q)
        asyncio.run(mgr.set_params("tx", {"power": -60.0}))
    asyncio.run(mgr.set_params("tx", {"power": -60.0}))               # position unknown → sent again, ok
    assert len(fired) == 3 and fired[-1] == fired[-2]
    asyncio.run(mgr.set_params("tx", {"power": -60.0}))               # now remembered
    assert len(fired) == 3


# ── _set_nice ───────────────────────────────────────────────────────────────────

def test_set_nice_calls_setpriority_and_swallows_permission_errors(monkeypatch):
    calls = []
    monkeypatch.setattr(pm.os, "setpriority", lambda which, who, prio: calls.append((which, who, prio)))
    assert pm._set_nice(4242, -5, "tx") is True
    assert calls == [(os.PRIO_PROCESS, 4242, -5)]
    assert pm._set_nice(4242, 0, "tx") is False and len(calls) == 1   # 0 = untouched
    assert pm._set_nice(None, 10, "tx") is False and len(calls) == 1
    def denied(which, who, prio):
        raise PermissionError("Operation not permitted")
    monkeypatch.setattr(pm.os, "setpriority", denied)
    assert pm._set_nice(4242, -5, "tx") is False                       # never raises


# ── LIVE: the real spawns ───────────────────────────────────────────────────────

def test_live_task_and_oneshot_run_at_their_niceness(tmp_path, monkeypatch):
    monkeypatch.setattr(pm._agentcfg, "TASK_NICE", -5)
    monkeypatch.setattr(pm._agentcfg, "ONESHOT_NICE", 10)
    (tmp_path / "s.py").write_text("import time\ntime.sleep(30)\n")
    (tmp_path / "one.py").write_text("import os\nprint('nice=%d' % os.nice(0), flush=True)\n")
    tasks = {"tx": TaskConfig(name="tx", command=["python3", str(tmp_path / "s.py")], working_dir=str(tmp_path)),
             "one": TaskConfig(name="one", command=["python3", str(tmp_path / "one.py")], working_dir=str(tmp_path))}
    mgr = pm.ProcessManager(tasks, tmp_path, unit_id="u")
    root = os.geteuid() == 0
    async def scenario():
        proc = mgr._procs["tx"]
        await mgr.start("tx")
        try:
            assert proc.state == ProcessState.RUNNING
            if root:                                                   # lowering niceness needs root
                assert os.getpriority(os.PRIO_PROCESS, proc._proc.pid) == -5
        finally:
            await mgr.stop("tx")
        code = await mgr._launch_oneshot_wait("one", [])
        assert code == 0
        assert "nice=10" in mgr._procs["one"].log.current.read_text()   # raising is always allowed
    asyncio.run(scenario())
