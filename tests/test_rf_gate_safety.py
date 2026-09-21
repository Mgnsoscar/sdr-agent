"""RF safety around the active components (docs/rf-fault-recovery.md §14q, 1.36.8).

Owner report: an auto-restarted L1C run came back "waaaay" hotter than its −70 dBm target, and after
the crash the dead task's LO leakage stayed on the air until a manual stop. Both are the attenuator:
the agent positions it before every RF-on launch/tune BEST-EFFORT (a failed set was a log line, the
launch went ahead at a calibrated SDR gain over an attenuator of unknown position), and nothing ever
drove it back to max when a task faulted, crashed or stopped — the killed process leaves the radio's
TX LO on, leaking through the attenuator left at the transmit setting.

Covers:
  * STRICT: a launch / tune that opens the RF gate is refused when its attenuator set fails or times
    out (nothing spawned, the tune RPC not sent); a failing MUTE is not refused; the knob restores
    best-effort;
  * every attenuator command is annotated into the run log with its outcome;
  * the chain is MUTED on a watchdog fault, on a run-driven crash and on a stop — unless another task
    is live on the unit; the mute is skipped with the knob off;
  * the post-fault radio reset runs detached, holds the device gate, is skipped while a task is live
    and with the knob off.
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import process_manager as pm                              # noqa: E402
from agent.models import ProcessState, SequenceRun, SequenceState, StartRequest, TaskConfig, TaskHealth   # noqa: E402
import test_active_freq_consistency as A                              # noqa: E402  (_mgr / _capture / _atten_at)
import test_sequence_restart as T                                     # noqa: E402  (_mk / _fire / _iso)


class _FakeProc:
    """A stand-in subprocess. `alive=False` (a wedge the watchdog reaps) reports its exit code up
    front so a real stop() never signals — its pid is THIS test process; `alive=True` is for the
    exit watcher, which awaits wait() first and never signals."""
    def __init__(self, code, alive=False):
        self.pid = os.getpid()
        self.returncode = None if alive else code
        self._code = code
    async def wait(self):
        self.returncode = self._code
        return self._code


def _capture_codes(mgr, monkeypatch, codes):
    """Like A._capture, but each one-shot returns the next code of `codes` (None = timed out)."""
    fired, started = A._capture(mgr, monkeypatch)
    it = iter(codes)
    async def fake_oneshot(name, args, timeout=pm._ACTIVE_SET_TIMEOUT_S):
        fired.append((name, list(args)))
        return next(it)
    monkeypatch.setattr(mgr, "_launch_oneshot_wait", fake_oneshot)
    return fired, started


# ── strict: never open the gate over an unconfirmed attenuator ──────────────────

def test_a_failed_set_refuses_an_rf_on_launch(tmp_path, monkeypatch):
    mgr = A._mgr(tmp_path, monkeypatch)
    fired, started = _capture_codes(mgr, monkeypatch, [1])
    with pytest.raises(RuntimeError, match="RF gate refused"):
        asyncio.run(mgr.start("tx", StartRequest(args=["--freq", "1575.42", "--power", "-30", "--rf", "on"],
                                                 replace_args=True), source="sequence"))
    assert len(fired) == 1 and started == []                          # the set was tried; nothing launched


def test_a_timed_out_set_refuses_too_and_the_knob_restores_best_effort(tmp_path, monkeypatch):
    mgr = A._mgr(tmp_path, monkeypatch)
    fired, started = _capture_codes(mgr, monkeypatch, [None, None])
    with pytest.raises(RuntimeError, match="timed out"):
        asyncio.run(mgr.start("tx", StartRequest(args=["--freq", "1575.42", "--power", "-30"],
                                                 replace_args=True), source="sequence"))
    assert started == []
    monkeypatch.setattr(pm._agentcfg, "ACTIVE_SET_STRICT", False)
    asyncio.run(mgr.start("tx", StartRequest(args=["--freq", "1575.42", "--power", "-30"],
                                             replace_args=True), source="sequence"))
    assert len(started) == 1                                          # the old best-effort launch


def test_a_failing_mute_is_not_refused_but_an_rf_on_tune_is(tmp_path, monkeypatch):
    mgr = A._mgr(tmp_path, monkeypatch)
    fired, started = _capture_codes(mgr, monkeypatch, [1, 1, 0])
    # an RF-OFF launch mutes; the mute fails → logged, the (muted) launch proceeds
    asyncio.run(mgr.start("tx", StartRequest(args=["--freq", "1575.42", "--power", "-30", "--rf", "off"],
                                             replace_args=True), source="sequence"))
    assert len(started) == 1 and fired[-1] == ("atten_set", ["--attenuation", "95"])
    tunes = []
    async def noop_set(values, wait=1.0):
        tunes.append(values)
        return {"applied": values, "rejected": {}}
    monkeypatch.setattr(mgr._get("tx"), "set_params", noop_set)
    with pytest.raises(RuntimeError, match="RF gate refused"):        # opening the gate: set fails → refused
        asyncio.run(mgr.set_params("tx", {"rf": "on"}))
    assert tunes == []                                                # the RPC was never sent
    asyncio.run(mgr.set_params("tx", {"rf": "on"}))                   # the set works → the tune goes out
    assert tunes == [{"rf": "on"}]


# ── the run log knows what the attenuator was told ─────────────────────────────

def test_every_set_is_annotated_with_its_outcome(tmp_path, monkeypatch):
    mgr = A._mgr(tmp_path, monkeypatch)
    fired, _ = _capture_codes(mgr, monkeypatch, [0, 1, 0])
    notes = []
    mgr.set_active_hook(lambda task, line: notes.append((task, line)))
    asyncio.run(mgr.start("tx", StartRequest(args=["--freq", "1575.42", "--power", "-30"],
                                             replace_args=True), source="sequence"))
    with pytest.raises(RuntimeError):
        asyncio.run(mgr.set_params("tx", {"power": -60.0}))
    asyncio.run(mgr.set_params("tx", {"power": -60.0}))
    assert [t for t, _ in notes] == ["tx", "tx", "tx"]
    a30 = pm._fmt_num(A._atten_at(1575.42e6)); a60 = pm._fmt_num(A._atten_at(1575.42e6, -60.0))
    assert notes[0][1] == f"⚙ atten_set --attenuation {a30} → ok"
    assert notes[1][1] == f"⚠ atten_set --attenuation {a60} FAILED (exit 1)"
    assert notes[2][1] == f"⚙ atten_set --attenuation {a60} → ok"


def test_runner_annotates_the_run_driving_the_task(tmp_path, monkeypatch):
    mgr, runner = T._mk(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    run = SequenceRun(id="ra", sequence_id="s", sequence_name="n", state=SequenceState.RUNNING,
                      on_air_at=T._iso(now), steps=[T._fire("start", 0, now, fired=T._iso(now))])
    other = SequenceRun(id="rb", sequence_id="s", sequence_name="n", state=SequenceState.RUNNING,
                        on_air_at=T._iso(now), steps=[T._fire("start", 0, now, fired=T._iso(now), task="other")])
    runner._runs.update({"ra": run, "rb": other})
    lines = {"ra": [], "rb": []}
    runner._run_logs["ra"] = SimpleNamespace(annotate=lines["ra"].append)
    runner._run_logs["rb"] = SimpleNamespace(annotate=lines["rb"].append)
    runner.on_active_set("tx", "⚙ atten_set --attenuation 31.75 → ok")
    assert lines["ra"] == ["   ⚙ atten_set --attenuation 31.75 → ok"] and lines["rb"] == []


# ── mute on fault / crash / stop ───────────────────────────────────────────────

def _faulting(tmp_path, monkeypatch, codes=(0, 0, 0, 0)):
    mgr = A._mgr(tmp_path, monkeypatch)
    fired, started = _capture_codes(mgr, monkeypatch, list(codes))
    monkeypatch.setattr(pm._agentcfg, "RESET_SDR_ON_FAULT", False)   # the reset is tested on its own
    proc = mgr._get("tx")
    proc.state = ProcessState.RUNNING
    proc.pid = os.getpid()
    proc._proc = _FakeProc(code=0)
    return mgr, proc, fired


def test_watchdog_fault_mutes_the_chain(tmp_path, monkeypatch):
    mgr, proc, fired = _faulting(tmp_path, monkeypatch)
    proc.log.current.write_bytes(b"stuck\nvmcircbuf: no space\n")
    asyncio.run(mgr._scan_task_health(proc))
    assert proc.health == TaskHealth.RF_FAULT.value
    assert fired[-1] == ("atten_set", ["--attenuation", "95"])       # attenuators to max


def test_run_driven_crash_mutes_the_chain(tmp_path, monkeypatch):
    mgr, proc, fired = _faulting(tmp_path, monkeypatch)
    mgr.set_owned_query(lambda: {"tx"})
    mgr.set_pending_query(lambda: set())
    proc._proc = _FakeProc(code=-6, alive=True)
    proc.log.current.write_bytes(b"EnvironmentError: IOError: usb tx2 transfer status: LIBUSB_TRANSFER_ERROR\n")
    asyncio.run(proc._watch())
    assert proc.health == TaskHealth.RF_FAULT.value
    assert fired[-1] == ("atten_set", ["--attenuation", "95"])


def test_stop_mutes_unless_another_task_is_live_or_the_knob_is_off(tmp_path, monkeypatch):
    mgr, proc, fired = _faulting(tmp_path, monkeypatch)
    async def fake_stop(*a, **k):
        proc.state = ProcessState.STOPPED
    monkeypatch.setattr(proc, "stop", fake_stop)
    monkeypatch.setattr(proc, "status", types.MethodType(pm.ManagedProcess.status, proc))   # the real one
    asyncio.run(mgr.stop("tx"))
    assert fired[-1] == ("atten_set", ["--attenuation", "95"])       # stopped ⇒ muted
    n = len(fired)
    mgr._procs["atten_set"].state = ProcessState.RUNNING              # another task holds the chain
    proc.state = ProcessState.RUNNING
    asyncio.run(mgr.stop("tx"))
    assert len(fired) == n                                            # not muted under its feet
    mgr._procs["atten_set"].state = ProcessState.STOPPED
    monkeypatch.setattr(pm._agentcfg, "MUTE_ON_FAULT", False)
    proc.state = ProcessState.RUNNING
    asyncio.run(mgr.stop("tx"))
    assert len(fired) == n


# ── the post-fault radio reset ─────────────────────────────────────────────────

def test_post_fault_reset_holds_the_device_gate_and_is_skipped_while_live(tmp_path, monkeypatch):
    mgr = A._mgr(tmp_path, monkeypatch)
    fired, _ = _capture_codes(mgr, monkeypatch, [0] * 6)
    monkeypatch.setattr(pm._agentcfg, "RESET_SDR_ON_FAULT", True)
    monkeypatch.setattr(pm._agentcfg, "RESET_SDR_TIMEOUT_S", 5.0)
    calls = []
    async def fake_probe(timeout):
        calls.append(mgr.device_free.is_set())                        # False while the probe holds it
        return "SDR pre-imaged (FPGA image loaded)"
    monkeypatch.setattr(pm._sysmon, "pre_image_sdr", fake_probe)
    async def scenario():
        await mgr._after_fault("tx")
        await asyncio.sleep(0.05)
        assert calls == [False] and mgr.device_free.is_set()
        mgr._procs["atten_set"].state = ProcessState.RUNNING          # someone live → skipped
        await mgr._after_fault("tx")
        await asyncio.sleep(0.05)
        assert calls == [False] and mgr.device_free.is_set()
        mgr._procs["atten_set"].state = ProcessState.STOPPED
        monkeypatch.setattr(pm._agentcfg, "RESET_SDR_ON_FAULT", False)
        await mgr._after_fault("tx")
        await asyncio.sleep(0.05)
        assert calls == [False]
    asyncio.run(scenario())
