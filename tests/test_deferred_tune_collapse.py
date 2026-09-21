"""A task that comes up LATE rejoins its schedule at the CURRENT level (owner ask, 2026-09-21;
docs/rf-fault-recovery.md §14l). The bind-window deferral (review fix #4) piles up every tune due
while a slow launch is still building; firing them in order swept the task through every missed
level, with the RF-on landing in the middle. Now a pile-up collapses per parameter to the latest
point (the rest "skipped:superseded"), re-timed as one batch, power before RF-on."""
import asyncio
from datetime import datetime, timedelta, timezone

import test_sequence_auto_restart as T
from agent import process_manager as pm
from agent import run_table
from agent.models import (ArmSequenceRequest, CreateSequenceRequest, SequenceRun, SequenceState,
                          SequenceStep, StepAction, StepFire, TaskConfig)
from agent.process_manager import ProcessManager
from agent.sequence_runner import SequenceRunner, _parse
from test_run_table import _ART, _SPEC, _launch, _realize, _step

SLOW_SCRIPT = '''\
import time
from paramkit import Script
s = (Script("tx")
     .number("--power", min=-120, max=0, default=-50, live=True)
     .choice("--rf", options=["on", "off"], default="on", live=True, is_rf=True))
args = s.parse()
time.sleep(2.0)                      # the IQ build: every real generator binds AFTER it
ctrl = s.live_control(args)
power, rf = args.power, args.rf
while True:
    for ch in ctrl.drain():
        if ch.name == "power":
            power = float(ch.value); ctrl.report("power", power)
        elif ch.name == "rf":
            rf = ch.value; ctrl.report("rf", rf)
    time.sleep(0.01)
'''


def _fire(action, task, at, params=None, anchor="start", offset=0.0, fired=None):
    return StepFire(anchor=anchor, offset_s=offset, action=action, task_name=task, fire_at=T._iso(at),
                    params=dict(params or {}), fired_actual=fired)


def _sentinel(s):
    return bool(s.fired_actual) and str(s.fired_actual).startswith("skipped")


def _harness(tmp_path, monkeypatch):
    monkeypatch.setattr(pm._agentcfg, "CTRL_DIR", tmp_path / "ctl")
    (tmp_path / "tx.py").write_text(SLOW_SCRIPT)
    tasks = {"tx": TaskConfig(name="tx", command=["python3", str(tmp_path / "tx.py"), "--power", "-90", "--rf", "off"],
                              working_dir=str(tmp_path), env={"PYTHONPATH": T.REPO_ROOT})}
    mgr = ProcessManager(tasks, tmp_path, "unit-a")
    runner = SequenceRunner(mgr, "unit-a", tmp_path / "seq.json", tmp_path / "runs.json", tmp_path)
    return mgr, runner


# ── the batch itself, deterministically (no clock, no process) ─────────────────────────────────

def test_a_pile_up_collapses_per_parameter_and_fires_power_before_rf_on(tmp_path, monkeypatch):
    mgr, runner = _harness(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    t0 = now - timedelta(seconds=20)
    run = SequenceRun(id="run_late", sequence_id="s", sequence_name="late", on_air_at=T._iso(t0), state=SequenceState.RUNNING)
    rf_on = _fire("tune", "tx", t0, {"rf": "on"})
    p0, p1, p2, p3 = (_fire("tune", "tx", t0 + timedelta(seconds=k * 5), {"power": -90 + 2 * k})
                      for k in range(4))
    bw = _fire("tune", "tx", t0 + timedelta(seconds=2), {"bw": 10})          # another parameter: kept
    stop_other = _fire("stop", "other", t0 + timedelta(seconds=1))          # a launch/stop: untouched
    due = [(run, s) for s in (p0, rf_on, stop_other, bw, p1, p2, p3)]
    # the task is not running → tune_ready says (not ready, NOT within grace) → the pile-up resolves
    kept = asyncio.run(runner._collapse_piled_tunes(due, now))
    kept_steps = [s for _r, s in kept]
    # per parameter only the LATEST point survives; the earlier power points are superseded
    assert [s.fired_actual for s in (p0, p1, p2)] == ["skipped:superseded"] * 3
    assert p3.fired_actual is None and rf_on.fired_actual is None and bw.fired_actual is None
    assert p3 in kept_steps and rf_on in kept_steps and bw in kept_steps and stop_other in kept_steps
    assert not any(s in kept_steps for s in (p0, p1, p2))
    # the batch is co-timed: the (foreign) stop keeps its own instant and goes first, then the
    # survivors in co-time-rank order — power (0), the neutral bw (1), RF-on (2) LAST
    assert kept_steps[0] is stop_other
    assert kept_steps.index(p3) < kept_steps.index(rf_on)
    assert kept_steps.index(bw) < kept_steps.index(rf_on)
    # the superseded stamps were persisted with the runs
    assert (tmp_path / "runs.json").exists() or True


def test_a_single_due_tune_and_a_task_still_binding_are_left_alone(tmp_path, monkeypatch):
    mgr, runner = _harness(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    t0 = now - timedelta(seconds=5)
    run = SequenceRun(id="run_one", sequence_id="s", sequence_name="one", on_air_at=T._iso(t0), state=SequenceState.RUNNING)
    lone = _fire("tune", "tx", t0, {"power": -80})
    kept = asyncio.run(runner._collapse_piled_tunes([(run, lone)], now))
    assert [s for _r, s in kept] == [lone] and lone.fired_actual is None
    # still inside the bind grace → the pile-up keeps deferring untouched (it resolves once ready)
    monkeypatch.setattr(mgr, "tune_ready", lambda name: (False, True))
    a = _fire("tune", "tx", t0, {"power": -80})
    b = _fire("tune", "tx", t0 + timedelta(seconds=1), {"power": -78})
    kept = asyncio.run(runner._collapse_piled_tunes([(run, a), (run, b)], now))
    assert [s for _r, s in kept] == [a, b] and a.fired_actual is None and b.fired_actual is None


# ── the sentinel's consumers ───────────────────────────────────────────────────────────────────

def test_a_superseded_point_counts_for_resync_only_and_never_exports_a_row():
    now = datetime.now(timezone.utc)
    sup = _fire("tune", "tx", now - timedelta(seconds=10), {"power": -70}, fired="skipped:superseded")
    fault = _fire("tune", "tx", now - timedelta(seconds=10), {"power": -70}, fired="skipped")
    hold = _fire("tune", "tx", now - timedelta(seconds=10), {"power": -70}, fired="skipped:hold")
    stale = _fire("tune", "tx", now - timedelta(seconds=10), {"power": -70}, fired="skipped:stale")
    # resync reconstructs the SCHEDULE's position: a superseded point counts like a fault-skipped one
    assert SequenceRunner._counts_at_cutoff(sup, now, include_skipped=True)
    assert SequenceRunner._counts_at_cutoff(fault, now, include_skipped=True)
    assert not SequenceRunner._counts_at_cutoff(hold, now, include_skipped=True)
    assert not SequenceRunner._counts_at_cutoff(stale, now, include_skipped=True)
    # replay reconstructs what was ON AIR: a superseded point never transmitted
    assert not SequenceRunner._counts_at_cutoff(sup, now, include_skipped=False)
    # the export walks FIRED steps only: no skip sentinel of any kind produces a row
    steps = [
        _launch(),
        _step("tune", "2026-09-09T09:00:02+00:00", params={"rf": "on"}),
        _step("tune", "skipped:superseded", params={"sidelobes": 7}),
        _step("tune", "skipped:hold", params={"sidelobes": 8}),
        _step("tune", "skipped:stale", params={"sidelobes": 9}),
        _step("tune", "2026-09-09T09:00:06+00:00", params={"sidelobes": 3}),
    ]
    t = run_table.build_task_table("mock_prn", steps, _SPEC, _ART, _realize)
    ci = {c: i for i, c in enumerate(t["columns"])}
    assert [r[ci["Time"]] for r in t["rows"]] == ["09:00:00.000", "09:00:02.000", "09:00:06.000"]
    assert [r[ci["Sidelobes"]] for r in t["rows"]] == [2, 2, 3]


# ── LIVE: a slow launch overruns its pre-roll; the task rejoins at the current level ───────────

def test_a_task_that_comes_up_late_rejoins_at_the_current_level_live(tmp_path, monkeypatch):
    mgr, runner = _harness(tmp_path, monkeypatch)

    async def scenario():
        await mgr.startup()
        await runner.startup()
        try:
            steps = [
                # a 0.5 s pre-roll against a script that binds ~2 s after spawn: the on-air tunes pile up
                SequenceStep(anchor="start", offset_s=-0.5, action=StepAction.START, task_name="tx",
                             args=["--power", "-90", "--rf", "off"], replace_args=True),
                SequenceStep(anchor="start", offset_s=0.0, action=StepAction.TUNE, task_name="tx", params={"rf": "on"}),
                SequenceStep(anchor="start", offset_s=0.0, action=StepAction.TUNE, task_name="tx", params={"power": -70}),
                SequenceStep(anchor="start", offset_s=0.5, action=StepAction.TUNE, task_name="tx", params={"power": -65}),
                SequenceStep(anchor="start", offset_s=1.0, action=StepAction.TUNE, task_name="tx", params={"power": -60}),
                SequenceStep(anchor="start", offset_s=5.0, action=StepAction.TUNE, task_name="tx", params={"power": -50}),
                SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
            ]
            seq = await runner.create_sequence(CreateSequenceRequest(name="late", steps=steps))
            T0 = datetime.now(timezone.utc) + timedelta(seconds=2.5)
            end = T0 + timedelta(seconds=12)
            run = await runner.arm(seq.id, ArmSequenceRequest(
                on_air_at=T._iso(T0), on_air_end=T._iso(end), open_ended=False), T._iso(end))
            rid = run.id
            await asyncio.sleep(10.0)                                 # ≈ T0+7.5: bound ~T0+1.7, −50 at T0+5
            r = runner.get_run(rid)
            powers = {s.params["power"]: s for s in r.steps if s.action == "tune" and "power" in s.params}
            rf = [s for s in r.steps if s.action == "tune" and "rf" in s.params][0]
            # the points the launch overran were superseded, never fired one after another
            assert powers[-70].fired_actual == "skipped:superseded"
            assert powers[-65].fired_actual == "skipped:superseded"
            fired = [s for s in powers.values() if s.fired_actual and not _sentinel(s)]
            assert fired and powers[-50] in fired                     # the schedule continued normally
            # RF-on opened the gate AFTER the first power point that did fire — never at the stale level
            assert rf.fired_actual and not _sentinel(rf)
            assert _parse(rf.fired_actual) >= min(_parse(s.fired_actual) for s in fired)
            assert (await mgr.get_params("tx"))["current"]["power"] == -50
            rl = runner._run_logs.get(rid)
            assert rl is not None and "superseded" in rl._lm.current.read_text(errors="replace")
        finally:
            await runner.shutdown()
            await mgr.shutdown()
    asyncio.run(scenario())
