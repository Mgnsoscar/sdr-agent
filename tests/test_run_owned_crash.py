"""A CRASH of a task a run is driving is the run's fault (docs/rf-fault-recovery.md §14p, 1.36.6).

Owner report: an L1C sequence started fine, then a couple of seconds after RF-on the script died on a
USB error (`usb tx2 transfer status: LIBUSB_TRANSFER_NO_DEVICE`, a std::runtime_error terminate).
The task read CRASHED, but the sequence kept reading RUNNING with no alert and no Restart: the exit
watcher coupled only an exit whose log tail matched a halt signature into the run; every other
non-zero exit fired a plain CrashEvent the run never hears about. To the run a crashed task is
exactly as silent as a halted one, so a non-zero exit of a task an active run is DRIVING now goes
through `_flag_rf_fault` with a crash detail (exit code + the last log line) — alarm, snapshot, run
coupling (Restart / the auto policy), the export's Event row — and the crash-restart supervisor
stands down (the run policy relaunches at the reconstructed level). A standalone task, or a task a
run has merely not yet launched, keeps the crash path exactly as before.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import process_manager as pm                              # noqa: E402
from agent.models import ProcessState, RestartRequest, SequenceState, TaskConfig, TaskHealth   # noqa: E402
import test_sequence_restart as T                                     # noqa: E402  (_mk)
import test_run_export_events as E                                    # noqa: E402  (_running_run)

USB_TAIL = (b"  GPS L1C:   RF             : OFF (muted)\n"
            b"terminate reached from thread id: 7fff09fbf160Got std::runtime_error\n"
            b"EnvironmentError: IOError: usb tx2 transfer status: LIBUSB_TRANSFER_NO_DEVICE\n")


class _FakeProc:
    def __init__(self, code):
        self.pid = os.getpid()
        self.returncode = None
        self._code = code
    async def wait(self):
        self.returncode = self._code
        return self._code


def _crashed(tmp_path, *, owned, pending=frozenset(), restart_on_crash=True):
    cfg = TaskConfig(name="tx", command=["python3", str(tmp_path / "s.py")], working_dir=str(tmp_path),
                     restart_on_crash=restart_on_crash, restart_delay_s=0.0)
    mgr = pm.ProcessManager({"tx": cfg}, tmp_path, unit_id="u")
    mgr.set_owned_query(owned)
    mgr.set_pending_query(lambda: set(pending))
    proc = mgr._procs["tx"]
    proc.state = ProcessState.RUNNING
    proc.pid = os.getpid()
    proc._proc = _FakeProc(code=-6)                                  # SIGABRT: the std::terminate
    proc.log.current.write_bytes(b"banner\n" + USB_TAIL)
    hook_calls, starts = [], []
    async def hook(name, detail):
        hook_calls.append((name, detail))
    mgr.set_fault_hook(hook)
    async def start(request=None):
        starts.append(request)
        proc.state = ProcessState.RUNNING
    proc.start = start
    return mgr, proc, hook_calls, starts


def _events(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_a_crash_of_a_run_driven_task_is_the_runs_fault(tmp_path):
    async def scenario():
        mgr, proc, hook_calls, starts = _crashed(tmp_path, owned=lambda: {"tx"})
        q = mgr.dispatcher.subscribe()
        await proc._watch()
        await asyncio.sleep(0.05)
        assert proc.state == ProcessState.CRASHED
        assert proc.health == TaskHealth.RF_FAULT.value
        assert proc.health_detail == ("process crashed (exit -6) — EnvironmentError: IOError: "
                                      "usb tx2 transfer status: LIBUSB_TRANSFER_NO_DEVICE")
        assert hook_calls == [("tx", proc.health_detail)]            # coupled into the run
        assert starts == []                                           # the crash supervisor stood down
        kinds = [e.get("type") for e in _events(q)]
        assert "task_health" in kinds and "crash" not in kinds        # the loud alarm, not a plain crash
    asyncio.run(scenario())


def test_a_standalone_crash_keeps_the_crash_path(tmp_path):
    async def scenario():
        mgr, proc, hook_calls, starts = _crashed(tmp_path, owned=lambda: set())
        q = mgr.dispatcher.subscribe()
        await proc._watch()
        await asyncio.sleep(0.05)
        assert proc.health == TaskHealth.OK.value and hook_calls == []
        assert [e.get("type") for e in _events(q)] == ["crash"]
        assert len(starts) == 1                                       # restart_on_crash relaunched it
    asyncio.run(scenario())


def test_a_pending_only_claim_or_a_failing_query_keeps_the_crash_path(tmp_path):
    async def scenario():
        # a run has a not-yet-fired launch of 'tx': the crashed process is not the run's
        mgr, proc, hook_calls, starts = _crashed(tmp_path, owned=lambda: {"tx"}, pending={"tx"})
        await proc._watch()
        assert proc.health == TaskHealth.OK.value and hook_calls == [] and len(starts) == 1
        # the owned-query failing: never couple blindly
        def boom():
            raise RuntimeError("no runner")
        mgr2, proc2, hook2, starts2 = _crashed(tmp_path / "b", owned=boom)
        await proc2._watch()
        assert proc2.health == TaskHealth.OK.value and hook2 == [] and len(starts2) == 1
    asyncio.run(scenario())


def test_crash_detail_without_a_log_is_just_the_code(tmp_path):
    async def scenario():
        mgr, proc, _, _ = _crashed(tmp_path, owned=lambda: {"tx"})
        proc.log.current.write_bytes(b"")
        assert await proc._crash_detail(1) == "process crashed (exit 1)"
        proc.log.current.write_bytes(b"x" * 300 + b"\n")
        assert (await proc._crash_detail(1)).endswith("…")
    asyncio.run(scenario())


# ── LIVE: a real run's real process is killed; the run faults and can be restarted ──────────

async def _wait_for(pred, timeout=8.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return pred()


def test_live_killed_run_task_faults_the_run_and_restart_recovers_it(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = T._mk(tmp_path, monkeypatch)
        mgr.set_fault_hook(runner.on_task_fault)                      # as main.py wires them
        mgr.set_owned_query(runner.tasks_claimed_by_active_runs)
        mgr.set_pending_query(runner.tasks_pending_launch_by_active_runs)
        now = datetime.now(timezone.utc)
        run = E._running_run(runner, now)                             # a RUNNING run driving 'tx'
        await mgr.start("tx")                                         # the real script, as the run launched it
        proc = mgr._procs["tx"]
        assert proc.state == ProcessState.RUNNING
        try:
            os.kill(proc._proc.pid, signal.SIGKILL)                   # the USB error, in effect
            assert await _wait_for(lambda: bool(run.fault))
            assert run.fault_task == "tx" and run.state == SequenceState.RUNNING
            assert run.fault.startswith("process crashed (exit -9)")
            assert run.incidents and run.incidents[-1].kind == "rf_fault"
            assert proc.health == TaskHealth.RF_FAULT.value
            # …and the recovery the owner missed: a resync restart relaunches it
            out = await runner.restart_run("r1", RestartRequest(mode="resync"))
            assert out.fault == ""
            relaunch = [s for s in out.steps if s.action == "start" and s.note]
            assert relaunch and relaunch[0].note == "RESTART (resync)"
        finally:
            await mgr.stop("tx")
            await mgr.shutdown()
    asyncio.run(scenario())
