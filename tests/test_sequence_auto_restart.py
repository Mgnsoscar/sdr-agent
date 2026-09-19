"""RF-fault RECOVERY Phase 3 (docs/rf-fault-recovery.md §7.1/§14d): UNATTENDED auto-restart.

The agent's own tick (SequenceRunner._service_auto_restart) auto-fires restart_run for a faulted run
whose recovery policy is "auto", with NO operator/client present. A budget (config.AUTO_RESTART_BUDGET)
caps attempts and RESETS after the relaunched task transmits healthy for a settle window; on breaker
trip (budget exhausted, or restart_run refuses) the LOUD sequence_rf_fault alarm re-fires and the run
is left RUNNING-faulted for the operator's manual Restart.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent import process_manager as pm
from agent import config as agentcfg
from agent.models import (
    ArmSequenceRequest, CreateSequenceRequest, SequenceRun, SequenceState, SequenceStep,
    StepAction, StepFire, TaskConfig, TaskHealth,
)
from agent.models import RestartRequest
from agent.process_manager import ProcessManager
from agent.sequence_runner import SequenceRunner, _RestartInProgress

REPO_ROOT = str(Path(__file__).resolve().parents[1])

LIVE_SCRIPT = '''\
import time
from paramkit import Script
s = (Script("tx")
     .number("--power", min=-120, max=0, default=-50, live=True)
     .choice("--rf", options=["on", "off"], default="on", live=True, is_rf=True))
args = s.parse()
ctrl = s.live_control(args)
while True:
    for ch in ctrl.drain():
        pass
    time.sleep(0.01)
'''


def _mk(tmp_path, monkeypatch):
    monkeypatch.setattr(pm._agentcfg, "CTRL_DIR", tmp_path / "ctl")
    # Deterministic knobs for the tests (real defaults are enabled / budget 2 / reset 60 s).
    monkeypatch.setattr(agentcfg, "AUTO_RESTART_ENABLED", True)
    monkeypatch.setattr(agentcfg, "AUTO_RESTART_BUDGET", 2)
    monkeypatch.setattr(agentcfg, "AUTO_RESTART_HEALTHY_RESET_S", 30.0)
    script = tmp_path / "tx.py"
    script.write_text(LIVE_SCRIPT)
    tasks = {"tx": TaskConfig(name="tx", command=["python3", str(script), "--power", "-90",
                                                  "--rf", "off"],
                              working_dir=str(tmp_path), env={"PYTHONPATH": REPO_ROOT})}
    mgr = ProcessManager(tasks, tmp_path, "unit-a")
    runner = SequenceRunner(mgr, "unit-a", tmp_path / "seq.json",
                            tmp_path / "runs.json", tmp_path)
    return mgr, runner


def _iso(dt):
    return dt.isoformat()


def _fire(action, off, when, *, fired, task="tx", params=None, args=None, replace=False):
    return StepFire(anchor="start", offset_s=off, action=action, task_name=task,
                    fire_at=_iso(when), fired_actual=fired,
                    params=params or {}, args=args or [], replace_args=replace)


def _recorder(monkeypatch, runner):
    """Replace runner._fire with an async recorder; returns the (kind, detail) list."""
    fired = []

    async def rec(r, kind, detail=""):
        fired.append((kind, detail))

    monkeypatch.setattr(runner, "_fire", rec)
    return fired


def _faulted_auto_run(runner, now, *, rid="ar", policy="auto", mode="resync", count=0):
    """A RUNNING run whose tx faulted mid-ramp (fired to -70), STOP ahead, under the given policy."""
    T0 = now - timedelta(seconds=10)
    steps = [
        _fire("start", -1.0, T0, fired=_iso(T0), args=["--power", "-90", "--rf", "off"], replace=True),
        _fire("tune", 1.0, T0 + timedelta(seconds=1), fired=_iso(T0 + timedelta(seconds=1)),
              params={"rf": "on"}),
        _fire("tune", 5.0, now - timedelta(seconds=5), fired=_iso(now - timedelta(seconds=5)),
              params={"power": -70}),
        _fire("stop", 0.0, now + timedelta(seconds=10), fired="skipped"),
    ]
    run = SequenceRun(id=rid, sequence_id="s1", sequence_name="sweep", state=SequenceState.RUNNING,
                      on_air_at=_iso(T0), on_air_end=_iso(now + timedelta(seconds=10)), steps=steps,
                      fault="tx: vmcircbuf", fault_task="tx", fault_at=_iso(now - timedelta(seconds=3)),
                      restart_policy=policy, restart_mode=mode, auto_restart_count=count)
    runner._runs[rid] = run
    return run


# ── the auto trigger ──────────────────────────────────────────────────────────

def test_auto_policy_run_is_auto_restarted_by_the_tick(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = _faulted_auto_run(runner, now)
        fired = _recorder(monkeypatch, runner)
        await runner._service_auto_restart(now)
        # The fault is recovered, the budget counter advanced, a QUIET notice fired (not the alarm).
        assert run.fault == "" and run.auto_restart_count == 1 and run.auto_restart_task == "tx"
        assert any(k == "sequence_auto_restart" for k, _ in fired)
        # A clean self-recovery must NOT re-fire the LOUD operator alarm — the quiet-vs-loud
        # distinction is the whole UX contract of Phase 3.
        assert not any(k == "sequence_rf_fault" for k, _ in fired)
        # A relaunch start fire was inserted at now, born at the reconstructed -70 (RF on).
        relaunch = [s for s in run.steps if s.action == "start" and s.fired_actual is None]
        assert relaunch and float(relaunch[0].args[relaunch[0].args.index("--power") + 1]) == -70.0

    asyncio.run(scenario())


def test_budget_exhaustion_trips_the_breaker_loudly_once(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = _faulted_auto_run(runner, now, count=2)          # budget (2) already consumed
        fired = _recorder(monkeypatch, runner)
        await runner._service_auto_restart(now)
        # No further restart; the LOUD alarm re-fires; the run is left RUNNING-faulted for the operator.
        assert run.fault == "tx: vmcircbuf" and run.state == SequenceState.RUNNING
        assert run.auto_restart_count == 2
        assert any(k == "sequence_rf_fault" and "gave up" in d for k, d in fired)
        assert "ar" in runner._auto_gaveup
        # A second tick does NOT re-alarm (fired once).
        fired.clear()
        await runner._service_auto_restart(now)
        assert not fired

    asyncio.run(scenario())


def test_a_restart_refusal_trips_the_breaker_not_a_retry_loop(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        T0 = now - timedelta(seconds=20)
        # STOP already past → a resync restart refuses (no future STOP to recover into).
        steps = [
            _fire("start", 0.0, T0, fired=_iso(T0), args=["--power", "-50", "--rf", "on"], replace=True),
            _fire("stop", 0.0, now - timedelta(seconds=2), fired="skipped"),
        ]
        run = SequenceRun(id="ar", sequence_id="s", sequence_name="late", state=SequenceState.RUNNING,
                          on_air_at=_iso(T0), on_air_end=_iso(now - timedelta(seconds=2)), steps=steps,
                          fault="tx: halt", fault_task="tx", fault_at=_iso(now - timedelta(seconds=8)),
                          restart_policy="auto", restart_mode="resync")
        runner._runs["ar"] = run
        fired = _recorder(monkeypatch, runner)
        await runner._service_auto_restart(now)
        # The refusal is a breaker trip: loud alarm, no relaunch, marked given-up. The give-up is
        # made DURABLE (count bumped to the budget) so the doomed restart isn't re-selected every tick.
        assert any(k == "sequence_rf_fault" and "gave up" in d for k, d in fired)
        assert run.auto_restart_count == 2                    # == budget: durable give-up
        assert not [s for s in run.steps if s.action == "start" and s.fired_actual is None]
        assert "ar" in runner._auto_gaveup
        # Next tick does not retry the doomed restart (no re-selection, no re-alarm).
        fired.clear()
        await runner._service_auto_restart(now)
        assert not fired and run.auto_restart_count == 2

    asyncio.run(scenario())


def test_holding_faulted_run_is_not_auto_restarted(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = _faulted_auto_run(runner, now)
        run.state = SequenceState.HOLDING                      # a fault during a Hold
        fired = _recorder(monkeypatch, runner)
        await runner._service_auto_restart(now)
        assert run.fault == "tx: vmcircbuf" and run.auto_restart_count == 0 and not fired

    asyncio.run(scenario())


def test_confirm_and_manual_policy_runs_are_left_for_the_operator(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        for policy in ("confirm", "manual"):
            run = _faulted_auto_run(runner, now, rid="r_" + policy, policy=policy)
            fired = _recorder(monkeypatch, runner)
            await runner._service_auto_restart(now)
            assert run.fault == "tx: vmcircbuf" and run.auto_restart_count == 0 and not fired

    asyncio.run(scenario())


def test_disabled_globally_leaves_a_faulted_auto_run_alone(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        monkeypatch.setattr(agentcfg, "AUTO_RESTART_ENABLED", False)
        now = datetime.now(timezone.utc)
        run = _faulted_auto_run(runner, now)
        fired = _recorder(monkeypatch, runner)
        await runner._service_auto_restart(now)
        assert run.fault == "tx: vmcircbuf" and run.auto_restart_count == 0 and not fired

    asyncio.run(scenario())


def test_healthy_settle_resets_the_budget(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)      # reset window 30 s
        now = datetime.now(timezone.utc)
        # A RECOVERED run (no fault) that consumed 1 auto-restart of tx.
        run = SequenceRun(id="ar", sequence_id="s", sequence_name="x", state=SequenceState.RUNNING,
                          on_air_at=_iso(now - timedelta(seconds=60)), steps=[],
                          restart_policy="auto", auto_restart_count=1, auto_restart_task="tx")
        runner._runs["ar"] = run
        monkeypatch.setattr(runner, "_task_healthy", lambda t: True)
        # First tick: stamps the settle marker, no reset yet.
        await runner._service_auto_restart(now)
        assert run.auto_restart_count == 1 and run.auto_restart_healthy_since
        # A tick past the settle window: the budget resets.
        await runner._service_auto_restart(now + timedelta(seconds=31))
        assert run.auto_restart_count == 0 and run.auto_restart_task == ""
        assert not run.auto_restart_healthy_since

    asyncio.run(scenario())


def test_settle_marker_restarts_if_the_task_goes_unhealthy(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = SequenceRun(id="ar", sequence_id="s", sequence_name="x", state=SequenceState.RUNNING,
                          on_air_at=_iso(now - timedelta(seconds=60)), steps=[],
                          restart_policy="auto", auto_restart_count=1, auto_restart_task="tx")
        runner._runs["ar"] = run
        healthy = {"v": True}
        monkeypatch.setattr(runner, "_task_healthy", lambda t: healthy["v"])
        await runner._service_auto_restart(now)
        assert run.auto_restart_healthy_since                   # started settling
        healthy["v"] = False                                   # task dips unhealthy
        await runner._service_auto_restart(now + timedelta(seconds=5))
        assert not run.auto_restart_healthy_since               # settle timer cleared
        assert run.auto_restart_count == 1                      # budget NOT reset

    asyncio.run(scenario())


# ── process-manager suppression (run-owned rf-fault must not raw-relaunch) ──────

class _ExitedProc:
    """A stand-in for the asyncio subprocess: exits immediately with a non-zero code."""
    returncode = 1

    async def wait(self):
        return 1


def test_rf_fault_exit_does_not_crash_restart_even_with_restart_on_crash(tmp_path, monkeypatch):
    """The core suppression: an RF-fault EXIT (Layer-1 done-watcher) must NOT go through the raw
    crash-restart supervisor, even when restart_on_crash is set — recovery is owned by the run's
    policy (restart_run), and a raw relaunch would double-transmit on the single TX channel."""
    async def scenario():
        task = TaskConfig(name="tx", command=["python3", str(tmp_path / "s.py")],
                          working_dir=str(tmp_path), restart_on_crash=True, max_restarts=5)
        mgr = pm.ProcessManager({"tx": task}, tmp_path, unit_id="u")
        proc = mgr._procs["tx"]
        proc._proc = _ExitedProc()
        proc.state = pm.ProcessState.RUNNING                 # an UNINTENDED exit (not a stop)

        async def yes():
            return True

        async def noop(*a, **k):
            return None

        started = []
        flagged = []

        async def fake_start(*a, **k):
            started.append(1)

        async def rec_flag(*a, **k):
            flagged.append(1)

        monkeypatch.setattr(proc, "_is_rf_fault_exit", yes)
        monkeypatch.setattr(proc, "_flag_rf_fault", rec_flag)
        monkeypatch.setattr(proc, "_cleanup", noop)
        monkeypatch.setattr(proc, "start", fake_start)

        await proc._watch()
        # The fault MUST still be flagged (health/run coupling/snapshot — the sole precondition for
        # the Phase-3 auto-trigger) BEFORE the raw relaunch is suppressed. A regression that returned
        # before _flag_rf_fault would silently lose the fault AND the recovery — invisible RF loss.
        assert flagged == [1]                                # detected + coupled
        assert started == []                                 # SUPPRESSED — no raw relaunch

    asyncio.run(scenario())


def test_ordinary_crash_still_restarts_with_restart_on_crash(tmp_path, monkeypatch):
    """The contrast: a plain (non-rf-fault) crash with restart_on_crash still relaunches — the
    suppression narrows ONLY the rf-fault branch, it does not disable ordinary crash-restart."""
    async def scenario():
        task = TaskConfig(name="tx", command=["python3", str(tmp_path / "s.py")],
                          working_dir=str(tmp_path), restart_on_crash=True, max_restarts=5,
                          restart_delay_s=0.0)
        mgr = pm.ProcessManager({"tx": task}, tmp_path, unit_id="u")
        proc = mgr._procs["tx"]
        proc._proc = _ExitedProc()
        proc.state = pm.ProcessState.RUNNING

        async def no():
            return False

        async def noop(*a, **k):
            return None

        started = []

        async def fake_start(*a, **k):
            started.append(1)

        monkeypatch.setattr(proc, "_is_rf_fault_exit", no)   # NOT an rf-fault → ordinary crash
        monkeypatch.setattr(proc, "_fire_crash_event", noop)
        monkeypatch.setattr(proc, "_cleanup", noop)
        monkeypatch.setattr(proc, "start", fake_start)

        await proc._watch()
        assert started == [1]                                # ordinary crash-restart still fires

    asyncio.run(scenario())


# ── arm plumbing ───────────────────────────────────────────────────────────────

# ── concurrency: manual Restart racing the auto trigger (review finding, HIGH) ──────────

def test_restart_run_refuses_a_concurrent_restart_of_the_same_run(tmp_path, monkeypatch):
    """The in-flight guard: while one caller is inside restart_run's pre-stop→relaunch window
    (its run_id sits in _restart_inflight), a second caller for the SAME run refuses cleanly with
    _RestartInProgress instead of issuing its own pre-stop — so manual + auto never both stop+relaunch."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = _faulted_auto_run(runner, now)
        runner._restart_inflight.add("ar")                    # simulate a caller already in the window
        try:
            await runner.restart_run("ar", RestartRequest(mode="resync", restart_at=_iso(now)))
            assert False, "expected _RestartInProgress"
        except _RestartInProgress:
            pass
        # The refused caller did NOT touch the run (no relaunch, fault intact).
        assert run.fault == "tx: vmcircbuf"
        assert not [s for s in run.steps if s.action == "start" and s.fired_actual is None]

    asyncio.run(scenario())


def test_restart_run_releases_the_inflight_marker_on_success_and_on_error(tmp_path, monkeypatch):
    """The marker is a per-run lock held only across ONE call — a successful restart clears it (so a
    later legitimate restart can run), and a refused restart clears it too (so a doomed run isn't
    wedged un-restartable)."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        # Success path.
        run = _faulted_auto_run(runner, now, rid="ok")
        _recorder(monkeypatch, runner)
        await runner.restart_run("ok", RestartRequest(mode="resync", restart_at=_iso(now)))
        assert "ok" not in runner._restart_inflight
        # Refusal path (resync past off-air): the marker is still released.
        T0 = now - timedelta(seconds=20)
        run2 = SequenceRun(id="late", sequence_id="s", sequence_name="late", state=SequenceState.RUNNING,
                           on_air_at=_iso(T0), on_air_end=_iso(now - timedelta(seconds=2)),
                           steps=[_fire("start", 0.0, T0, fired=_iso(T0),
                                        args=["--power", "-50", "--rf", "on"], replace=True),
                                  _fire("stop", 0.0, now - timedelta(seconds=2), fired="skipped")],
                           fault="tx: halt", fault_task="tx", fault_at=_iso(now - timedelta(seconds=8)),
                           restart_policy="auto")
        runner._runs["late"] = run2
        try:
            await runner.restart_run("late", RestartRequest(mode="resync", restart_at=_iso(now)))
        except ValueError:
            pass
        assert "late" not in runner._restart_inflight

    asyncio.run(scenario())


def test_auto_restart_does_not_false_alarm_a_run_recovered_by_a_concurrent_caller(tmp_path, monkeypatch):
    """Symptom 1 of the race, fixed: if restart_run raises but the run's fault is already CLEARED
    (a concurrent manual Restart won and recovered it), the auto path must NOT trip the breaker or
    fire the LOUD give-up alarm on a healthy, recovered run."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = _faulted_auto_run(runner, now)
        fired = _recorder(monkeypatch, runner)

        async def recovered_then_raise(run_id, req, **kw):
            runner._runs[run_id].fault = ""                   # the other caller already recovered it
            raise ValueError("the run's fault changed during restart; retry")

        monkeypatch.setattr(runner, "restart_run", recovered_then_raise)
        await runner._service_auto_restart(now)
        # No trip: no loud alarm, not marked given-up, inflight cleaned up.
        assert not any(k == "sequence_rf_fault" for k, _ in fired)
        assert "ar" not in runner._auto_gaveup and "ar" not in runner._auto_inflight

    asyncio.run(scenario())


def test_auto_restart_stands_down_quietly_when_a_restart_is_already_in_progress(tmp_path, monkeypatch):
    """The _RestartInProgress path: when restart_run reports another caller holds the run, the auto
    path stands down quietly (no trip, no alarm) — the other caller will resolve the fault."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = _faulted_auto_run(runner, now)
        fired = _recorder(monkeypatch, runner)

        async def in_progress(run_id, req, **kw):
            raise _RestartInProgress("busy")

        monkeypatch.setattr(runner, "restart_run", in_progress)
        await runner._service_auto_restart(now)
        assert not fired and "ar" not in runner._auto_gaveup and "ar" not in runner._auto_inflight
        assert run.fault == "tx: vmcircbuf"                   # untouched — the other caller owns it

    asyncio.run(scenario())


# ── the breaker's middle rung + config edges (review test-coverage findings) ─────────────

def test_budget_middle_rung_restarts_then_the_third_fault_trips(tmp_path, monkeypatch):
    """The count=1 → still-restart rung (what makes the budget 2, not 1) and the full flap→trip
    staircase: fault→restart(0→1)→re-fault→restart(1→2)→re-fault→TRIP."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = _faulted_auto_run(runner, now, count=1)         # one attempt already spent
        fired = _recorder(monkeypatch, runner)
        await runner._service_auto_restart(now)
        # Middle rung: restarted again (not tripped), counter to 2, quiet notice, not given-up.
        assert run.fault == "" and run.auto_restart_count == 2
        assert any(k == "sequence_auto_restart" for k, _ in fired)
        assert not any(k == "sequence_rf_fault" for k, _ in fired) and "ar" not in runner._auto_gaveup
        # Re-fault at the budget: the next tick trips loudly.
        run.fault = "tx: vmcircbuf again"; run.fault_task = "tx"
        run.fault_at = _iso(now); run.steps.append(
            _fire("stop", 0.0, now + timedelta(seconds=10), fired="skipped"))
        fired.clear()
        await runner._service_auto_restart(now + timedelta(seconds=1))
        assert any(k == "sequence_rf_fault" and "gave up" in d for k, d in fired)
        assert run.auto_restart_count == 2 and "ar" in runner._auto_gaveup

    asyncio.run(scenario())


def test_budget_zero_is_unlimited_and_never_trips(tmp_path, monkeypatch):
    """AUTO_RESTART_BUDGET=0 is the documented 'unlimited' opt-in: a persistently-faulting auto run
    restarts on every fault and NEVER enters the give-up path."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        monkeypatch.setattr(agentcfg, "AUTO_RESTART_BUDGET", 0)
        now = datetime.now(timezone.utc)
        run = _faulted_auto_run(runner, now, count=9)         # already way past the default budget
        fired = _recorder(monkeypatch, runner)
        await runner._service_auto_restart(now)
        assert run.fault == "" and run.auto_restart_count == 10   # restarted, not tripped
        assert any(k == "sequence_auto_restart" for k, _ in fired)
        assert not any(k == "sequence_rf_fault" for k, _ in fired) and "ar" not in runner._auto_gaveup

    asyncio.run(scenario())


def test_replay_mode_reaches_restart_run_through_the_auto_path(tmp_path, monkeypatch):
    """The auto trigger threads run.restart_mode into restart_run: a replay-policy run recovers
    replay-shaped (off-air floated later by the downtime), distinguishable from resync."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = _faulted_auto_run(runner, now, mode="replay")   # fault_at = now-3s → downtime 3 s
        orig_end = _parse_end(run.on_air_end)
        _recorder(monkeypatch, runner)
        await runner._service_auto_restart(now)
        # Replay floats off-air later by the downtime (resync would leave it at the original end).
        new_end = _parse_end(run.on_air_end)
        assert (new_end - orig_end).total_seconds() >= 2.0

    asyncio.run(scenario())


def test_peer_fault_shares_the_per_run_budget(tmp_path, monkeypatch):
    """auto_restart_count/task are per-RUN: a peer task's fault after a recovery consumes the SAME
    budget and re-points the settle watch — so a persistently-flapping multi-task run still trips."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        # A run that already auto-restarted task A once; now task B faults (fault_task="txb").
        T0 = now - timedelta(seconds=10)
        steps = [
            _fire("start", 0.0, T0, fired=_iso(T0), task="txb",
                  args=["--power", "-90", "--rf", "on"], replace=True),
            _fire("stop", 0.0, now + timedelta(seconds=10), fired="skipped", task="txb"),
        ]
        run = SequenceRun(id="ar", sequence_id="s", sequence_name="x", state=SequenceState.RUNNING,
                          on_air_at=_iso(T0), on_air_end=_iso(now + timedelta(seconds=10)), steps=steps,
                          fault="txb: halt", fault_task="txb", fault_at=_iso(now - timedelta(seconds=2)),
                          restart_policy="auto", auto_restart_count=1, auto_restart_task="txa")
        runner._runs["ar"] = run
        _recorder(monkeypatch, runner)
        await runner._service_auto_restart(now)
        # B consumed the shared budget (1→2) and is now the watched task.
        assert run.auto_restart_count == 2 and run.auto_restart_task == "txb" and run.fault == ""

    asyncio.run(scenario())


# ── the settle-reset guards (review breaker-logic findings) ─────────────────────────────

def test_settle_reset_is_floored_above_the_fault_detection_latency(tmp_path, monkeypatch):
    """A settle window shorter than the watchdog's re-detection latency would let a freshly-relaunched
    flapper bank a full 'healthy' window and reset the breaker before the imminent re-fault. The
    effective window is floored to 3x HEALTH_POLL_S, so a tiny reset_s can't defeat the breaker."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        monkeypatch.setattr(agentcfg, "AUTO_RESTART_HEALTHY_RESET_S", 1.0)   # below the floor
        monkeypatch.setattr(agentcfg, "HEALTH_POLL_S", 2.0)                  # floor = 6 s
        now = datetime.now(timezone.utc)
        run = SequenceRun(id="ar", sequence_id="s", sequence_name="x", state=SequenceState.RUNNING,
                          on_air_at=_iso(now - timedelta(seconds=60)), steps=[],
                          restart_policy="auto", auto_restart_count=1, auto_restart_task="tx")
        runner._runs["ar"] = run
        monkeypatch.setattr(runner, "_task_healthy", lambda t: True)
        await runner._service_auto_restart(now)                             # stamps the marker
        # 1.5 s later (> configured 1 s, < the 6 s floor): NOT reset yet.
        await runner._service_auto_restart(now + timedelta(seconds=1.5))
        assert run.auto_restart_count == 1
        # Past the floor: now it resets.
        await runner._service_auto_restart(now + timedelta(seconds=7))
        assert run.auto_restart_count == 0

    asyncio.run(scenario())


def test_reset_zero_is_a_lifetime_cap_never_resets(tmp_path, monkeypatch):
    """AUTO_RESTART_HEALTHY_RESET_S=0 means 'never reset' (a lifetime cap) — the OPPOSITE of
    budget=0. A recovered, healthy run's counter stays put however long it stays healthy."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        monkeypatch.setattr(agentcfg, "AUTO_RESTART_HEALTHY_RESET_S", 0.0)
        now = datetime.now(timezone.utc)
        run = SequenceRun(id="ar", sequence_id="s", sequence_name="x", state=SequenceState.RUNNING,
                          on_air_at=_iso(now - timedelta(seconds=60)), steps=[],
                          restart_policy="auto", auto_restart_count=1, auto_restart_task="tx")
        runner._runs["ar"] = run
        monkeypatch.setattr(runner, "_task_healthy", lambda t: True)
        await runner._service_auto_restart(now)
        await runner._service_auto_restart(now + timedelta(seconds=600))
        assert run.auto_restart_count == 1 and not run.auto_restart_healthy_since

    asyncio.run(scenario())


def test_task_healthy_reads_running_and_health_ok(tmp_path, monkeypatch):
    """The real _task_healthy backs the 'no spurious reset' guard: True only when the task is running
    AND its reported health is OK; False for a stopped task, RF_FAULT, an empty name, or a lookup error."""
    mgr, runner = _mk(tmp_path, monkeypatch)

    class _St:
        def __init__(self, h): self.health = h

    state = {"running": True, "health": TaskHealth.OK, "raise": False}

    def fake_is_running(t):
        if state["raise"]:
            raise RuntimeError("boom")
        return state["running"]

    monkeypatch.setattr(runner._manager, "is_running", fake_is_running)
    monkeypatch.setattr(runner._manager, "status", lambda t: _St(state["health"]))

    assert runner._task_healthy("tx") is True
    state["health"] = TaskHealth.RF_FAULT
    assert runner._task_healthy("tx") is False
    state["health"] = TaskHealth.OK; state["running"] = False
    assert runner._task_healthy("tx") is False
    state["running"] = True; state["raise"] = True
    assert runner._task_healthy("tx") is False                # best-effort: a lookup error → not healthy
    assert runner._task_healthy("") is False                  # empty name → not healthy


def _parse_end(iso):
    from datetime import datetime as _dt
    return _dt.fromisoformat(iso)


# ── RF-safety: never relaunch OVER a not-yet-dead process (review HIGH) ──────────────────

def test_auto_restart_defers_while_the_faulted_process_is_still_alive(tmp_path, monkeypatch):
    """For the true-wedge fault, the Phase-1 watchdog's auto-drop stop takes ~10 s to SIGKILL a
    SIGTERM-ignoring flowgraph. The auto trigger must DEFER (not relaunch a second process on the
    channel) until that process is confirmed dead."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = _faulted_auto_run(runner, now)
        fired = _recorder(monkeypatch, runner)
        alive = {"v": True}
        monkeypatch.setattr(runner._manager, "is_process_alive", lambda t: alive["v"])
        # Process still alive (watchdog mid-stop) → deferred: no restart, no trip, counter untouched.
        await runner._service_auto_restart(now)
        assert run.fault == "tx: vmcircbuf" and run.auto_restart_count == 0 and not fired
        # Once the auto-drop has killed it → the deferred restart fires.
        alive["v"] = False
        await runner._service_auto_restart(now)
        assert run.fault == "" and run.auto_restart_count == 1

    asyncio.run(scenario())


def test_restart_run_refuses_to_relaunch_over_a_live_process(tmp_path, monkeypatch):
    """restart_run's RF-safety backstop (protects a manual Restart racing the auto-drop): refuse
    while the OS process is still alive rather than double-transmit."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = _faulted_auto_run(runner, now)
        monkeypatch.setattr(runner._manager, "is_process_alive", lambda t: True)

        async def noop_stop(*a, **k):
            return None

        monkeypatch.setattr(runner._manager, "stop", noop_stop)
        _recorder(monkeypatch, runner)
        try:
            await runner.restart_run("ar", RestartRequest(mode="resync", restart_at=_iso(now)))
            assert False, "expected a refusal while the process is alive"
        except ValueError as exc:
            assert "still stopping" in str(exc)
        assert run.fault == "tx: vmcircbuf"                   # untouched — no relaunch committed

    asyncio.run(scenario())


# ── a manual Restart resets the breaker (review MEDIUM) ──────────────────────────────────

def test_manual_restart_resets_the_breaker_and_clears_the_giveup_latch(tmp_path, monkeypatch):
    """A tripped run recovered by an operator's manual Restart must get a FRESH unattended budget:
    the counter resets and the give-up latch clears, so a later independent fault is auto-restarted
    again (not left permanently un-auto-restartable after one operator touch)."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = _faulted_auto_run(runner, now, count=2)         # exhausted + tripped
        runner._auto_gaveup.add("ar")
        _recorder(monkeypatch, runner)
        # A MANUAL restart (default reset_budget=True) recovers AND resets the breaker.
        await runner.restart_run("ar", RestartRequest(mode="resync", restart_at=_iso(now)))
        assert run.fault == "" and run.auto_restart_count == 0 and run.auto_restart_task == ""
        assert "ar" not in runner._auto_gaveup
        # A fresh fault is auto-restartable again (the budget was handed back).
        run.fault = "tx: vmcircbuf 2"; run.fault_task = "tx"; run.fault_at = _iso(now)
        run.steps.append(_fire("stop", 0.0, now + timedelta(seconds=10), fired="skipped"))
        fired = _recorder(monkeypatch, runner)
        await runner._service_auto_restart(now + timedelta(seconds=1))
        assert run.fault == "" and run.auto_restart_count == 1
        assert any(k == "sequence_auto_restart" for k, _ in fired)

    asyncio.run(scenario())


# ── the authored policy survives the agent round-trip (feature-defeating gap) ────────────

def test_agent_persists_and_round_trips_the_recovery_policy(tmp_path, monkeypatch):
    """The agent's Sequence/CreateSequenceRequest must carry recovery_policy/recovery_mode — the
    client reads the stored policy back to resolve what to arm (a plan/schedule item inherits it).
    Dropping it here silently degrades every armed run to 'manual'."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        seq = await runner.create_sequence(CreateSequenceRequest(name="s", steps=[
            SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx",
                         args=["--power", "-50", "--rf", "on"], replace_args=True),
            SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
        ], recovery_policy="auto", recovery_mode="replay"))
        assert seq.recovery_policy == "auto" and seq.recovery_mode == "replay"
        # Read back (what the client's inherit path fetches).
        assert runner.get_sequence(seq.id).recovery_policy == "auto"
        # Survives a persist + reload (a fresh runner loads sequences from disk).
        runner2 = SequenceRunner(mgr, "unit-a", tmp_path / "seq.json",
                                 tmp_path / "runs2.json", tmp_path)
        runner2._load_sequences()
        assert runner2.get_sequence(seq.id).recovery_policy == "auto"
        assert runner2.get_sequence(seq.id).recovery_mode == "replay"

    asyncio.run(scenario())


def test_arm_stamps_the_recovery_policy_onto_the_run(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        seq = await runner.create_sequence(CreateSequenceRequest(name="s", steps=[
            SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx",
                         args=["--power", "-50", "--rf", "on"], replace_args=True),
            SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
        ]))
        now = datetime.now(timezone.utc)
        run = await runner.arm(seq.id, ArmSequenceRequest(
            on_air_at=_iso(now + timedelta(seconds=1)),
            on_air_end=_iso(now + timedelta(seconds=30)),
            restart_policy="auto", restart_mode="replay"),
            _iso(now + timedelta(seconds=30)))
        assert run.restart_policy == "auto" and run.restart_mode == "replay"
        # Default arm (no policy) stays conservative "manual".
        run2 = await runner.arm(seq.id, ArmSequenceRequest(
            on_air_at=_iso(now + timedelta(seconds=40)),
            on_air_end=_iso(now + timedelta(seconds=60))),
            _iso(now + timedelta(seconds=60)))
        assert run2.restart_policy == "manual" and run2.restart_mode == "resync"

    asyncio.run(scenario())
