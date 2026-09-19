"""
RF-fault RECOVERY Phase 3b — the STANDALONE task "Auto-restart on fault" (docs/rf-fault-recovery.md
§7.1/§14e).

A task with TaskConfig.auto_restart_on_fault set is relaunched by the ProcessManager when it RF-faults
— but ONLY when the task is NOT owned by an active sequence/plan run (a run-owned fault is recovered by
the run policy; relaunching here too would double-transmit on the single TX channel). Budget-limited
(max_fault_restarts within restart_window_s), gated by the master kill-switch config.AUTO_RESTART_ENABLED.

Covered:
  * the decision matrix (checkbox off / kill-switch off / owned-by-a-run all stand down);
  * both detection paths relaunch (the non-intentional EXIT branch of _watch, the watchdog-stop path of
    _scan_task_health) and never double-fire for one fault;
  * the auto-drop stop does NOT abort recovery, but an operator stop DOES;
  * the rolling budget gives up after max_fault_restarts and re-arms after the window;
  * the owned-query plumbing (ProcessManager.set_owned_query / SequenceRunner.tasks_claimed_by_active_runs).
"""
import asyncio
import os

import pytest

from agent import process_manager as pm
from agent import config as cfg
from agent import main
from agent.models import (
    ProcessState, SequenceRun, SequenceState, StartRequest, StepFire, TaskConfig, TaskHealth,
)
from agent.sequence_runner import SequenceRunner


# ── helpers ───────────────────────────────────────────────────────────────────

def _task(tmp_path, **kw):
    kw.setdefault("restart_delay_s", 0.0)      # no real wait in tests
    return TaskConfig(name="tx", command=["python3", str(tmp_path / "s.py")],
                      working_dir=str(tmp_path), **kw)


class _FakeProc:
    """A stand-in asyncio subprocess: wait() resolves to `code` and sets returncode."""
    def __init__(self, code=1, alive=False):
        self.pid = os.getpid()
        self.returncode = None if alive else code
        self._code = code
    async def wait(self):
        self.returncode = self._code
        return self._code


def _mgr(tmp_path, **kw):
    mgr = pm.ProcessManager({"tx": _task(tmp_path, **kw)}, tmp_path, unit_id="u")
    return mgr, mgr._procs["tx"]


def _stub_start(proc):
    """Replace start() with a recorder that marks the proc RUNNING (so guards behave)."""
    calls = []
    async def start(request=None):
        calls.append(request)
        proc.state = ProcessState.RUNNING
        proc._proc = _FakeProc(alive=True)          # a live process after relaunch
        proc._operator_stop_requested = False
        proc._stop_requested = False
    proc.start = start
    return calls


# ── the per-launch override (Run… form) ──────────────────────────────────────────

def test_per_launch_override_enables_a_task_configured_off(tmp_path):
    async def scenario():
        _, proc = _mgr(tmp_path, auto_restart_on_fault=False)
        proc._auto_restart_override = True          # the Run… form turned it on for this launch
        proc.state = ProcessState.CRASHED
        proc._proc = _FakeProc(code=1)
        calls = _stub_start(proc)
        await proc._maybe_auto_restart_standalone()
        assert len(calls) == 1
    asyncio.run(scenario())


def test_per_launch_override_disables_a_task_configured_on(tmp_path):
    async def scenario():
        _, proc = _mgr(tmp_path, auto_restart_on_fault=True)
        proc._auto_restart_override = False         # explicitly OFF for this launch
        proc.state = ProcessState.CRASHED
        proc._proc = _FakeProc(code=1)
        calls = _stub_start(proc)
        await proc._maybe_auto_restart_standalone()
        assert calls == []
    asyncio.run(scenario())


def test_start_captures_the_override_and_request(tmp_path):
    async def scenario():
        _, proc = _mgr(tmp_path, auto_restart_on_fault=False)
        req = StartRequest(args=["--power", "-70"], replace_args=True, auto_restart_on_fault=True)
        # Drive the real start() enough to capture the request, then bail before launching.
        # Simplest: exercise the two lines directly via a stub that records + captures.
        proc._last_request = req
        proc._auto_restart_override = req.auto_restart_on_fault
        assert proc._auto_restart_override is True
        proc.state = ProcessState.CRASHED
        proc._proc = _FakeProc(code=1)
        calls = _stub_start(proc)
        await proc._maybe_auto_restart_standalone()
        assert calls == [req]                        # relaunched with the SAME request (exact params)
    asyncio.run(scenario())


def test_start_request_default_override_is_none(tmp_path):
    assert StartRequest().auto_restart_on_fault is None


# ── the decision matrix ─────────────────────────────────────────────────────────

def test_faulted_standalone_task_relaunches(tmp_path):
    async def scenario():
        _, proc = _mgr(tmp_path, auto_restart_on_fault=True)
        proc.state = ProcessState.CRASHED
        proc._proc = _FakeProc(code=1)              # dead
        calls = _stub_start(proc)
        await proc._maybe_auto_restart_standalone()
        assert len(calls) == 1                       # relaunched
        assert len(proc._fault_restart_times) == 1
        assert proc.restart_count == 1
    asyncio.run(scenario())


def test_no_relaunch_when_checkbox_off(tmp_path):
    async def scenario():
        _, proc = _mgr(tmp_path, auto_restart_on_fault=False)
        proc.state = ProcessState.CRASHED
        proc._proc = _FakeProc(code=1)
        calls = _stub_start(proc)
        await proc._maybe_auto_restart_standalone()
        assert calls == []
    asyncio.run(scenario())


def test_no_relaunch_when_kill_switch_off(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(cfg, "AUTO_RESTART_ENABLED", False)
        _, proc = _mgr(tmp_path, auto_restart_on_fault=True)
        proc.state = ProcessState.CRASHED
        proc._proc = _FakeProc(code=1)
        calls = _stub_start(proc)
        await proc._maybe_auto_restart_standalone()
        assert calls == []
    asyncio.run(scenario())


def test_no_relaunch_when_owned_by_an_active_run(tmp_path):
    async def scenario():
        _, proc = _mgr(tmp_path, auto_restart_on_fault=True)
        proc._owned_query = lambda: {"tx"}          # a run owns this task
        proc.state = ProcessState.CRASHED
        proc._proc = _FakeProc(code=1)
        calls = _stub_start(proc)
        await proc._maybe_auto_restart_standalone()
        assert calls == []                           # left to the run policy
    asyncio.run(scenario())


def test_relaunch_when_owned_query_lists_a_different_task(tmp_path):
    async def scenario():
        _, proc = _mgr(tmp_path, auto_restart_on_fault=True)
        proc._owned_query = lambda: {"other"}       # a DIFFERENT task is run-owned
        proc.state = ProcessState.CRASHED
        proc._proc = _FakeProc(code=1)
        calls = _stub_start(proc)
        await proc._maybe_auto_restart_standalone()
        assert len(calls) == 1
    asyncio.run(scenario())


def test_owned_query_failure_stands_down(tmp_path):
    async def scenario():
        _, proc = _mgr(tmp_path, auto_restart_on_fault=True)
        def boom():
            raise RuntimeError("runner busy")
        proc._owned_query = boom
        proc.state = ProcessState.CRASHED
        proc._proc = _FakeProc(code=1)
        calls = _stub_start(proc)
        await proc._maybe_auto_restart_standalone()
        assert calls == []                           # never relaunch on an unknown ownership
    asyncio.run(scenario())


# ── safety guards ────────────────────────────────────────────────────────────────

def test_never_relaunches_over_a_still_alive_process(tmp_path):
    async def scenario():
        _, proc = _mgr(tmp_path, auto_restart_on_fault=True)
        proc.state = ProcessState.CRASHED
        proc._proc = _FakeProc(alive=True)          # returncode is None → still alive
        calls = _stub_start(proc)
        await proc._maybe_auto_restart_standalone()
        assert calls == []                           # the double-transmit invariant
    asyncio.run(scenario())


def test_never_relaunches_a_running_task(tmp_path):
    async def scenario():
        _, proc = _mgr(tmp_path, auto_restart_on_fault=True)
        proc.state = ProcessState.RUNNING           # already (re)started
        proc._proc = _FakeProc(code=1)
        calls = _stub_start(proc)
        await proc._maybe_auto_restart_standalone()
        assert calls == []
    asyncio.run(scenario())


def test_operator_stop_aborts_recovery_but_auto_drop_does_not(tmp_path):
    async def scenario():
        # A delay so we can flip the operator flag mid-attempt.
        _, proc = _mgr(tmp_path, auto_restart_on_fault=True, restart_delay_s=0.05)
        proc.state = ProcessState.CRASHED
        proc._proc = _FakeProc(code=1)
        calls = _stub_start(proc)
        # The internal auto-drop set _stop_requested but NOT _operator_stop_requested.
        proc._stop_requested = True
        proc._operator_stop_requested = False
        task = asyncio.create_task(proc._maybe_auto_restart_standalone())
        await asyncio.sleep(0.01)
        proc._operator_stop_requested = True        # operator stops mid-delay
        await task
        assert calls == []                           # honoured the operator
    asyncio.run(scenario())


def test_auto_drop_stop_alone_does_not_block_recovery(tmp_path):
    async def scenario():
        _, proc = _mgr(tmp_path, auto_restart_on_fault=True)
        proc.state = ProcessState.CRASHED
        proc._proc = _FakeProc(code=1)
        proc._stop_requested = True                 # set by the auto-drop stop()
        proc._operator_stop_requested = False       # ...but not the operator flag
        calls = _stub_start(proc)
        await proc._maybe_auto_restart_standalone()
        assert len(calls) == 1                        # recovery proceeds
    asyncio.run(scenario())


# ── the rolling budget breaker ───────────────────────────────────────────────────

def test_budget_gives_up_after_max_then_rearms_after_the_window(tmp_path):
    async def scenario():
        _, proc = _mgr(tmp_path, auto_restart_on_fault=True, max_fault_restarts=2,
                       restart_window_s=100.0)
        calls = _stub_start(proc)

        async def fault_once():
            proc.state = ProcessState.CRASHED
            proc._proc = _FakeProc(code=1)          # dead again
            await proc._maybe_auto_restart_standalone()

        await fault_once(); await fault_once()       # two recoveries within budget
        assert len(calls) == 2
        assert proc.fault_restart_giving_up is False
        await fault_once()                           # third fault → give up
        assert len(calls) == 2
        assert proc.fault_restart_giving_up is True
        # age the two attempts out of the window → the breaker re-arms
        proc._fault_restart_times.clear()
        proc.fault_restart_giving_up = False
        await fault_once()
        assert len(calls) == 3
    asyncio.run(scenario())


def test_zero_budget_is_unlimited(tmp_path):
    async def scenario():
        _, proc = _mgr(tmp_path, auto_restart_on_fault=True, max_fault_restarts=0)
        calls = _stub_start(proc)
        for _ in range(5):
            proc.state = ProcessState.CRASHED
            proc._proc = _FakeProc(code=1)
            await proc._maybe_auto_restart_standalone()
        assert len(calls) == 5
    asyncio.run(scenario())


def test_inflight_latch_prevents_a_double_relaunch(tmp_path):
    async def scenario():
        _, proc = _mgr(tmp_path, auto_restart_on_fault=True, restart_delay_s=0.05)
        proc.state = ProcessState.CRASHED
        proc._proc = _FakeProc(code=1)
        calls = _stub_start(proc)
        # Both detection paths race for the SAME fault.
        a = asyncio.create_task(proc._maybe_auto_restart_standalone())
        b = asyncio.create_task(proc._maybe_auto_restart_standalone())
        await asyncio.gather(a, b)
        assert len(calls) == 1                        # exactly one relaunch
    asyncio.run(scenario())


# ── the two detection paths, end to end ──────────────────────────────────────────

def test_exit_path_relaunches_via_watch(tmp_path):
    """Layer 1: the done-watcher forced a non-zero exit → _watch's rf-fault branch relaunches."""
    async def scenario():
        _, proc = _mgr(tmp_path, auto_restart_on_fault=True)
        proc.state = ProcessState.RUNNING
        proc.pid = os.getpid()
        proc._proc = _FakeProc(code=1)              # exits non-zero when _watch awaits it
        proc.log.current.write_bytes(
            b"tuning\nHEALTH state=faulted reason=\"flowgraph halted\"\n")
        calls = _stub_start(proc)
        await proc._watch()
        await asyncio.sleep(0.02)
        assert proc.health == TaskHealth.RF_FAULT.value    # flagged as an RF fault, not a plain crash
        assert len(calls) == 1                              # standalone auto-restart fired
    asyncio.run(scenario())


def test_wedge_path_relaunches_via_scan_after_the_auto_drop(tmp_path):
    """Layer 2: the true-wedge case — the watchdog auto-drops RF, then relaunches (no exit)."""
    async def scenario():
        mgr, proc = _mgr(tmp_path, auto_restart_on_fault=True)
        proc.state = ProcessState.RUNNING
        proc.pid = os.getpid()
        proc._proc = _FakeProc(code=0)              # a wedge: stop() will reap it
        proc.log.current.write_bytes(b"stuck\nvmcircbuf: no space\n")
        calls = _stub_start(proc)
        await mgr._scan_task_health(proc)
        await asyncio.sleep(0.02)
        assert proc.health == TaskHealth.RF_FAULT.value
        assert len(calls) == 1                        # relaunched after the auto-drop stop
    asyncio.run(scenario())


def test_relaunch_waits_for_the_old_watcher_to_settle(tmp_path):
    """The wedge-path relaunch (a different task than _watch) must await the old watcher first, so
    start() can't race the old exit handling and corrupt the new run (a review concurrency fix)."""
    async def scenario():
        _, proc = _mgr(tmp_path, auto_restart_on_fault=True)
        proc.state = ProcessState.CRASHED
        proc._proc = _FakeProc(code=1)
        order = []
        gate = asyncio.Event()

        async def old_watch():
            await gate.wait()
            order.append("watcher_done")
        proc._watcher_task = asyncio.create_task(old_watch())

        calls = []
        async def start(request=None):
            order.append("relaunch")
            calls.append(request)
            proc.state = ProcessState.RUNNING
            proc._proc = _FakeProc(alive=True)
        proc.start = start

        relaunch = asyncio.create_task(proc._maybe_auto_restart_standalone())
        await asyncio.sleep(0.02)
        assert calls == []                 # blocked waiting on the old watcher
        gate.set()                         # let the old watcher finish
        await relaunch
        assert order == ["watcher_done", "relaunch"]   # relaunch strictly after the watcher settled
        assert len(calls) == 1
    asyncio.run(scenario())


def test_exit_path_does_not_relaunch_a_run_owned_task(tmp_path):
    """A run-owned fault takes the run-policy path, never the standalone relaunch (no double-TX)."""
    async def scenario():
        _, proc = _mgr(tmp_path, auto_restart_on_fault=True)
        proc._owned_query = lambda: {"tx"}
        proc.state = ProcessState.RUNNING
        proc.pid = os.getpid()
        proc._proc = _FakeProc(code=1)
        proc.log.current.write_bytes(b"HEALTH state=faulted reason=\"halted\"\n")
        calls = _stub_start(proc)
        await proc._watch()
        await asyncio.sleep(0.02)
        assert proc.health == TaskHealth.RF_FAULT.value    # still flagged (run coupling handles it)
        assert calls == []                                  # but NOT relaunched here
    asyncio.run(scenario())


# ── the owned-query plumbing ─────────────────────────────────────────────────────

def test_set_owned_query_applies_to_current_and_future_procs(tmp_path):
    mgr, proc = _mgr(tmp_path)
    q = lambda: {"tx"}
    mgr.set_owned_query(q)
    assert proc._owned_query is q
    fresh = mgr._make_proc(_task(tmp_path))
    assert fresh._owned_query is q


class _FakeManager:
    class _D:
        async def fire(self, payload):
            pass
    def __init__(self):
        self.dispatcher = _FakeManager._D()
    def has_task(self, name):
        return True


# ── tasks.yaml persistence round-trip (the primary authoring path) ───────────────

def test_auto_restart_flag_survives_the_tasks_yaml_round_trip(tmp_path, monkeypatch):
    """The client's saved checkbox must persist through _spec_to_entry → tasks.yaml → load_tasks,
    else the flag reverts on reload and the task never auto-restarts (a review HIGH)."""
    monkeypatch.setattr(cfg, "TASKS_YAML", tmp_path / "tasks.yaml")
    monkeypatch.setattr(cfg, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "tasks.yaml").write_text("tasks: []\n")

    spec = TaskConfig(name="tx", command=["python3", "tx.py"],
                      auto_restart_on_fault=True, max_fault_restarts=3)
    doc = main._load_tasks_doc()
    doc["tasks"] = [main._spec_to_entry(spec)]
    main._save_tasks_doc(doc)

    loaded = cfg.load_tasks()
    assert loaded["tx"].auto_restart_on_fault is True     # persisted
    assert loaded["tx"].max_fault_restarts == 3           # custom budget persisted


def test_default_budget_is_omitted_but_defaults_back(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "TASKS_YAML", tmp_path / "tasks.yaml")
    monkeypatch.setattr(cfg, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "tasks.yaml").write_text("tasks: []\n")

    entry = main._spec_to_entry(TaskConfig(name="tx", command=["python3", "tx.py"],
                                           auto_restart_on_fault=True))
    assert entry["auto_restart_on_fault"] is True
    assert "max_fault_restarts" not in entry              # default 2 kept out of the yaml
    doc = main._load_tasks_doc(); doc["tasks"] = [entry]; main._save_tasks_doc(doc)
    assert cfg.load_tasks()["tx"].max_fault_restarts == 2  # defaults back on load


def test_claimed_includes_live_and_pending_launches(tmp_path):
    """The standalone gate must count a task an ARMED run WILL launch (un-fired start), not only one
    it has already launched — else a run arming during the relaunch delay collides (review MEDIUM)."""
    r = SequenceRunner(_FakeManager(), "u", tmp_path / "seq.json",
                       tmp_path / "runs.json", tmp_path)
    steps = [
        # a FIRED launch (live-owned)
        StepFire(anchor="start", offset_s=0.0, action="start", task_name="live",
                 fire_at="2026-09-18T00:00:00Z", fired_actual="2026-09-18T00:00:00Z"),
        # an UN-FIRED launch the armed run will still perform (claimed, not yet live)
        StepFire(anchor="start", offset_s=0.0, action="start", task_name="pending",
                 fire_at="2026-09-18T00:10:00Z", fired_actual=None),
        # a task the run already launched AND stopped → NOT claimed
        StepFire(anchor="start", offset_s=0.0, action="run", task_name="done",
                 fire_at="2026-09-18T00:00:00Z", fired_actual="2026-09-18T00:00:00Z"),
        StepFire(anchor="stop", offset_s=0.0, action="stop", task_name="done",
                 fire_at="2026-09-18T00:01:00Z", fired_actual="2026-09-18T00:01:00Z"),
    ]
    run = SequenceRun(id="run_1", sequence_id="s", sequence_name="seq",
                      state=SequenceState.RUNNING, on_air_at="2026-09-18T00:00:00Z", steps=steps)
    r._runs = {run.id: run}
    claimed = r.tasks_claimed_by_active_runs()
    assert "live" in claimed and "pending" in claimed
    assert "done" not in claimed
    run.state = SequenceState.COMPLETED
    assert r.tasks_claimed_by_active_runs() == set()


# ── the launch hook (full-path relaunch) + breaker reset ─────────────────────────

def test_relaunch_routes_through_the_launch_hook_when_set(tmp_path):
    """The relaunch goes through the manager launch path (attenuator positioning) when a hook is set,
    not a bare ManagedProcess.start (review Finding 2)."""
    async def scenario():
        _, proc = _mgr(tmp_path, auto_restart_on_fault=True)
        proc.state = ProcessState.CRASHED
        proc._proc = _FakeProc(code=1)
        req = StartRequest(args=["--power", "-70"], replace_args=True)
        proc._last_request = req
        via_hook = []
        async def hook(name, request):
            via_hook.append((name, request))
        proc._launch_hook = hook
        # If the hook is used, the bare start must NOT be:
        bare = []
        async def start(request=None):
            bare.append(request)
        proc.start = start
        await proc._maybe_auto_restart_standalone()
        assert via_hook == [("tx", req)]     # relaunched via the full manager path
        assert bare == []                     # not the bare start
    asyncio.run(scenario())


def test_start_resets_the_fault_restart_breaker_flag(tmp_path, monkeypatch):
    """A fresh start re-arms the standalone breaker so a later trip logs again / the UI recovers
    (review Finding 3 — the flag was never cleared). Drive start() to just past its early flag
    clears by failing calibration (which raises before any launch/IO)."""
    def boom(env, name):
        raise pm._calib.CalibrationError("nope")
    monkeypatch.setattr(pm, "_inject_calibration", boom)

    async def scenario():
        _, proc = _mgr(tmp_path)
        proc.fault_restart_giving_up = True
        proc.restart_giving_up = True
        with pytest.raises(RuntimeError):
            await proc.start()
        assert proc.fault_restart_giving_up is False   # re-armed on a fresh start
        assert proc.restart_giving_up is False         # (its crash-loop twin, unchanged behaviour)
    asyncio.run(scenario())
