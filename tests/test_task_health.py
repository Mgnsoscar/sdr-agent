"""
RF-fault DETECTION Phase 1 — the agent health watchdog, health model, and run coupling.

  * LogManager.read_since — the incremental log-scan primitive (new bytes only; survives rotation).
  * ProcessManager._scan_task_health — a seeded fault line flips a RUNNING task to rf_fault, fires a
    TaskHealthEvent over SSE, invokes the run-coupling hook, and auto-drops RF — WITHOUT a process exit.
  * SequenceRunner.on_task_fault — stamps the owning run + skips the dead task's steps, leaving a
    co-running healthy task untouched.
  * ProcessStatus carries health.
"""
import asyncio
import os

import pytest

from agent import process_manager as pm
from agent import config as cfg
from agent.log_manager import LogManager
from agent.models import (
    SequenceRun, SequenceState, StepFire, TaskConfig, TaskHealth,
)
from agent.sequence_runner import SequenceRunner


# ── LogManager.read_since ─────────────────────────────────────────────────────

def test_read_since_returns_only_new_bytes_and_survives_rotation(tmp_path):
    async def scenario():
        lm = LogManager(tmp_path, "t")
        lm.current.write_bytes(b"line one\n")
        text, off, inode = await lm.read_since(0, None)
        assert text == "line one\n"
        # nothing new → empty, offset unchanged
        text2, off2, inode2 = await lm.read_since(off, inode)
        assert text2 == "" and off2 == off and inode2 == inode
        # append → only the new bytes
        with lm.current.open("ab") as fh:
            fh.write(b"line two\n")
        text3, off3, inode3 = await lm.read_since(off2, inode2)
        assert text3 == "line two\n"
        # rotate (rename + fresh file, new inode) → read from the top of the new file
        lm.rotate()
        lm.current.write_bytes(b"fresh run\n")
        text4, off4, inode4 = await lm.read_since(off3, inode3)
        assert text4 == "fresh run\n"
        assert inode4 != inode3
    asyncio.run(scenario())


# ── the watchdog ──────────────────────────────────────────────────────────────

def _task(tmp_path):
    return TaskConfig(name="tx", command=["python3", str(tmp_path / "s.py")],
                      working_dir=str(tmp_path))


def test_watchdog_flags_rf_fault_from_a_log_line_without_an_exit(tmp_path):
    async def scenario():
        mgr = pm.ProcessManager({"tx": _task(tmp_path)}, tmp_path, unit_id="u")
        proc = mgr._procs["tx"]
        proc.state = pm.ProcessState.RUNNING
        proc.pid = os.getpid()               # a live pid → the snapshot reads a real /proc
        proc.log.current.write_bytes(b"starting up\nHEALTH state=faulted reason=\"flowgraph halted\"\n")

        hook_calls = []
        async def hook(name, detail):
            hook_calls.append((name, detail))
        mgr.set_fault_hook(hook)

        q = mgr.dispatcher.subscribe()
        await mgr._scan_task_health(proc)
        await asyncio.sleep(0.05)             # let the fire-and-forget dispatch run

        assert proc.health == TaskHealth.RF_FAULT.value
        assert proc._resource_snapshot is not None          # §6.3 snapshot captured
        assert hook_calls and hook_calls[0][0] == "tx"      # run coupling invoked
        assert proc.state != pm.ProcessState.RUNNING        # RF auto-dropped (task stopped)
        # a TaskHealthEvent reached the SSE subscriber
        got = q.get_nowait()
        assert got["type"] == "task_health" and got["health"] == "rf_fault"
    asyncio.run(scenario())


def test_watchdog_ignores_a_clean_log_and_advances_the_cursor(tmp_path):
    async def scenario():
        mgr = pm.ProcessManager({"tx": _task(tmp_path)}, tmp_path, unit_id="u")
        proc = mgr._procs["tx"]
        proc.state = pm.ProcessState.RUNNING
        proc.pid = os.getpid()
        proc.log.current.write_bytes(b"tuning to 1575.42 MHz\nRF on\n")
        await mgr._scan_task_health(proc)
        assert proc.health == TaskHealth.OK.value
        assert proc._fault_alarmed is False
        assert proc._log_offset > 0          # cursor advanced past the scanned bytes
    asyncio.run(scenario())


def test_flag_rf_fault_is_idempotent(tmp_path):
    async def scenario():
        mgr = pm.ProcessManager({"tx": _task(tmp_path)}, tmp_path, unit_id="u")
        proc = mgr._procs["tx"]
        proc.pid = os.getpid()
        fires = []
        async def hook(name, detail):
            fires.append(name)
        proc._fault_hook = hook
        await proc._flag_rf_fault("first")
        await proc._flag_rf_fault("second")   # latched → no-op
        assert proc.health_detail == "first"
        assert fires == ["tx"]                 # coupled exactly once
    asyncio.run(scenario())


def test_status_carries_health(tmp_path):
    mgr = pm.ProcessManager({"tx": _task(tmp_path)}, tmp_path, unit_id="u")
    proc = mgr._procs["tx"]
    proc.health = TaskHealth.RF_FAULT.value
    proc.health_detail = "log signature: vmcircbuf"
    st = proc.status()
    assert st.health == TaskHealth.RF_FAULT.value
    assert st.health_detail == "log signature: vmcircbuf"


# ── run coupling ───────────────────────────────────────────────────────────────

class _Dispatcher:
    def __init__(self):
        self.events = []
    async def fire(self, payload):
        self.events.append(payload.model_dump() if hasattr(payload, "model_dump") else payload)


class _FakeManager:
    def __init__(self):
        self.dispatcher = _Dispatcher()
    def has_task(self, name):
        return True


def _run_with(fault_task="tx", peer=None):
    steps = [
        StepFire(anchor="start", offset_s=0.0, action="start", task_name=fault_task,
                 fire_at="2026-09-18T00:00:00Z", fired_actual="2026-09-18T00:00:00Z"),
        StepFire(anchor="start", offset_s=5.0, action="tune", task_name=fault_task,
                 fire_at="2026-09-18T00:00:05Z", fired_actual=None),        # pending → must skip
        StepFire(anchor="stop", offset_s=0.0, action="stop", task_name=fault_task,
                 fire_at="2026-09-18T00:10:00Z", fired_actual=None),        # pending → skip
    ]
    if peer:
        steps += [
            StepFire(anchor="start", offset_s=0.0, action="start", task_name=peer,
                     fire_at="2026-09-18T00:00:00Z", fired_actual="2026-09-18T00:00:00Z"),
            StepFire(anchor="start", offset_s=5.0, action="tune", task_name=peer,
                     fire_at="2026-09-18T00:00:05Z", fired_actual=None),    # healthy → keep
        ]
    return SequenceRun(id="run_1", sequence_id="s", sequence_name="seq",
                       state=SequenceState.RUNNING, on_air_at="2026-09-18T00:00:00Z",
                       steps=steps)


def test_on_task_fault_couples_the_owning_run_and_spares_a_peer(tmp_path):
    async def scenario():
        r = SequenceRunner(_FakeManager(), "u", tmp_path / "seq.json",
                           tmp_path / "runs.json", tmp_path)
        run = _run_with("tx", peer="amp")
        r._runs = {run.id: run}
        await r.on_task_fault("tx", "log signature: vmcircbuf")
        await asyncio.sleep(0.01)             # let the fire-and-forget event dispatch run

        assert run.fault and run.fault_task == "tx" and run.fault_at
        # tx's pending steps skipped; the started step keeps its real timestamp
        tx_steps = [s for s in run.steps if s.task_name == "tx"]
        assert tx_steps[0].fired_actual not in (None, "skipped")   # the launch
        assert tx_steps[1].fired_actual == "skipped"               # the pending tune
        assert tx_steps[2].fired_actual == "skipped"               # the pending stop
        # the healthy peer is untouched
        amp_pending = [s for s in run.steps if s.task_name == "amp" and s.action == "tune"]
        assert amp_pending[0].fired_actual is None
        # a sequence_rf_fault event fired
        assert any(e["type"] == "sequence_rf_fault" for e in r._manager.dispatcher.events)
    asyncio.run(scenario())


def test_on_task_fault_noop_when_no_active_run_owns_the_task(tmp_path):
    async def scenario():
        r = SequenceRunner(_FakeManager(), "u", tmp_path / "seq.json",
                           tmp_path / "runs.json", tmp_path)
        run = _run_with("tx")
        run.state = SequenceState.COMPLETED           # not active
        r._runs = {run.id: run}
        await r.on_task_fault("tx", "detail")
        assert run.fault == ""                         # nothing coupled
        assert not r._manager.dispatcher.events
    asyncio.run(scenario())
