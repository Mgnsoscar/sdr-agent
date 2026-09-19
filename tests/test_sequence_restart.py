"""RF-fault RECOVERY Phase 2 (docs/rf-fault-recovery.md §7): SequenceRunner.restart_run.

Deterministic unit tests over a constructed faulted run (the crash-time state Phase-1 on_task_fault
leaves: run.fault stamped, the faulted task's un-fired steps 'skipped'), plus a LIVE end-to-end that
faults a real mock task mid-ramp and restarts it back onto the schedule.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent import process_manager as pm
from agent.models import (
    ArmSequenceRequest, CreateSequenceRequest, RestartRequest, SequenceRun, SequenceState,
    SequenceStep, StepAction, StepFire, TaskConfig,
)
from agent.process_manager import ProcessManager
from agent.sequence_runner import SequenceRunner

REPO_ROOT = str(Path(__file__).resolve().parents[1])

# A live duration task with a calibrated-style --power and an RF gate, live-tunable.
LIVE_SCRIPT = '''\
import time
from paramkit import Script
s = (Script("tx")
     .number("--power", min=-120, max=0, default=-50, live=True)
     .choice("--rf", options=["on", "off"], default="on", live=True, is_rf=True))
args = s.parse()
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


def _mk(tmp_path, monkeypatch):
    monkeypatch.setattr(pm._agentcfg, "CTRL_DIR", tmp_path / "ctl")
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
    """Build one StepFire. `fired`: an ISO string (fired), 'skipped', or None (pending)."""
    return StepFire(anchor="start", offset_s=off, action=action, task_name=task,
                    fire_at=_iso(when), fired_actual=fired,
                    params=params or {}, args=args or [], replace_args=replace)


def _faulted_run(runner, now, *, rid="r1"):
    """Install a RUNNING run whose `tx` ramp fired -90→-70, then faulted at now-3s with a still
    -50 point and the STOP ahead (both 'skipped' by on_task_fault). Returns the run."""
    T0 = now - timedelta(seconds=10)
    steps = [
        _fire("start", -1.0, T0, fired=_iso(T0),
              args=["--power", "-90", "--rf", "off"], replace=True),
        _fire("tune", 0.0, T0 + timedelta(seconds=1), fired=_iso(T0 + timedelta(seconds=1)),
              params={"rf": "on"}),
        _fire("tune", 0.0, T0 + timedelta(seconds=1), fired=_iso(T0 + timedelta(seconds=1)),
              params={"power": -90}),
        _fire("tune", 5.0, now - timedelta(seconds=5), fired=_iso(now - timedelta(seconds=5)),
              params={"power": -70}),                       # last FIRED level → L_now
        _fire("tune", 12.0, now + timedelta(seconds=2), fired="skipped",
              params={"power": -50}),                       # future ramp point (fault-skipped)
        _fire("stop", 0.0, now + timedelta(seconds=10), fired="skipped"),   # STOP (fault-skipped)
    ]
    run = SequenceRun(id=rid, sequence_id="s1", sequence_name="chirp sweep",
                      state=SequenceState.RUNNING,
                      on_air_at=_iso(T0), on_air_end=_iso(now + timedelta(seconds=10)),
                      steps=steps, fault="tx: vmcircbuf", fault_task="tx",
                      fault_at=_iso(now - timedelta(seconds=3)))
    runner._runs[rid] = run
    return run


# ── L_now reconstruction + resync re-instatement + the relaunch fire ─────────────

def test_restart_resync_reconstructs_level_reinstates_future_and_relaunches(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = _faulted_run(runner, now)
        out = await runner.restart_run("r1", RestartRequest(mode="resync", restart_at=_iso(now)))

        # The fault is cleared and the run is still RUNNING.
        assert out.fault == "" and out.fault_task == "" and out.state == SequenceState.RUNNING
        # The future ramp point + the STOP were re-instated (no longer 'skipped'), fire_at unchanged.
        future = [s for s in out.steps if s.action == "tune" and s.params.get("power") == -50]
        assert future and future[0].fired_actual is None
        stop = [s for s in out.steps if s.action == "stop"][0]
        assert stop.fired_actual is None and stop.fire_at == _iso(now + timedelta(seconds=10))
        # Off-air is UNCHANGED (resync).
        assert out.on_air_end == _iso(now + timedelta(seconds=10))
        # A synthetic relaunch START at `now`, born at L_now=-70 with RF on, replace_args.
        relaunch = [s for s in out.steps
                    if s.action == "start" and s.fired_actual is None][0]
        assert relaunch.fire_at == _iso(now) and relaunch.replace_args
        assert "--power" in relaunch.args
        pi = relaunch.args.index("--power")
        assert float(relaunch.args[pi + 1]) == -70.0
        ri = relaunch.args.index("--rf")
        assert relaunch.args[ri + 1] == "on"                # RF forced ON (was 'off' at launch)

    asyncio.run(scenario())


def test_restart_replay_shifts_future_fires_and_off_air(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        _faulted_run(runner, now)                            # fault_at = now-3s → downtime 3s
        out = await runner.restart_run("r1", RestartRequest(mode="replay", restart_at=_iso(now)))
        # The future ramp point + STOP shifted LATER by the 3 s downtime.
        future = [s for s in out.steps if s.action == "tune" and s.params.get("power") == -50][0]
        assert future.fire_at == _iso(now + timedelta(seconds=2 + 3))
        stop = [s for s in out.steps if s.action == "stop"][0]
        assert stop.fire_at == _iso(now + timedelta(seconds=10 + 3))
        # Off-air floated +3 s.
        assert out.on_air_end == _iso(now + timedelta(seconds=10 + 3))

    asyncio.run(scenario())


def test_restart_reconstructs_launch_level_when_no_tune_fired(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        T0 = now - timedelta(seconds=5)
        # A fixed-power task: only a launch (--power -45), no ramp; faulted.
        steps = [
            _fire("start", 0.0, T0, fired=_iso(T0), args=["--power", "-45", "--rf", "on"],
                  replace=True),
            _fire("stop", 0.0, now + timedelta(seconds=10), fired="skipped"),
        ]
        run = SequenceRun(id="r2", sequence_id="s", sequence_name="cw", state=SequenceState.RUNNING,
                          on_air_at=_iso(T0), on_air_end=_iso(now + timedelta(seconds=10)),
                          steps=steps, fault="tx: halt", fault_task="tx", fault_at=_iso(now))
        runner._runs["r2"] = run
        out = await runner.restart_run("r2", RestartRequest(restart_at=_iso(now)))
        relaunch = [s for s in out.steps if s.action == "start" and s.fired_actual is None][0]
        pi = relaunch.args.index("--power")
        assert float(relaunch.args[pi + 1]) == -45.0         # the launch level

    asyncio.run(scenario())


def test_restart_replay_refuses_a_channel_collision(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        _faulted_run(runner, now)                            # off-air now+10, replay → now+13
        # A peer run occupying the channel at now+11..now+20 → the +3 s replay end (now+13) collides.
        peer = SequenceRun(id="peer", sequence_id="s2", sequence_name="next",
                           state=SequenceState.ARMED,
                           on_air_at=_iso(now + timedelta(seconds=11)),
                           on_air_end=_iso(now + timedelta(seconds=20)),
                           steps=[_fire("start", 0.0, now + timedelta(seconds=11), fired=None)])
        runner._runs["peer"] = peer
        try:
            await runner.restart_run("r1", RestartRequest(mode="replay", restart_at=_iso(now)))
            assert False, "expected a collision refusal"
        except ValueError as exc:
            assert "overlapping" in str(exc) and "resync" in str(exc)
        # resync (off-air unchanged) is still allowed alongside the peer.
        out = await runner.restart_run("r1", RestartRequest(mode="resync", restart_at=_iso(now)))
        assert out.fault == ""

    asyncio.run(scenario())


def test_restart_guards(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        # Unknown run → KeyError (→404).
        try:
            await runner.restart_run("nope", RestartRequest())
            assert False
        except KeyError:
            pass
        # A run with no fault → ValueError (→409).
        healthy = SequenceRun(id="ok", sequence_id="s", sequence_name="x",
                              state=SequenceState.RUNNING, on_air_at=_iso(now), steps=[])
        runner._runs["ok"] = healthy
        try:
            await runner.restart_run("ok", RestartRequest())
            assert False
        except ValueError as exc:
            assert "no RF fault" in str(exc)
        # A faulted run in a non-RUNNING state → ValueError.
        done = SequenceRun(id="done", sequence_id="s", sequence_name="x",
                           state=SequenceState.COMPLETED, on_air_at=_iso(now),
                           steps=[], fault="tx: x", fault_task="tx", fault_at=_iso(now))
        runner._runs["done"] = done
        try:
            await runner.restart_run("done", RestartRequest())
            assert False
        except ValueError as exc:
            assert "not running" in str(exc)
        # A bad mode → ValueError.
        _faulted_run(runner, now, rid="r3")
        try:
            await runner.restart_run("r3", RestartRequest(mode="sideways"))
            assert False
        except ValueError as exc:
            assert "unknown restart mode" in str(exc)

    asyncio.run(scenario())


# ── Review-finding regressions (docs/rf-fault-recovery.md §14c) ───────────────────

# A task whose ramp swept --gain (not --power), for the gain-reconstruction regression.
GAIN_SCRIPT = '''\
import time
from paramkit import Script
s = (Script("tx")
     .number("--gain", min=0, max=90, default=30, live=True)
     .choice("--rf", options=["on", "off"], default="on", live=True, is_rf=True))
args = s.parse()
ctrl = s.live_control(args)
while True:
    for ch in ctrl.drain():
        pass
    time.sleep(0.01)
'''

# A task whose tune/ramp swept a NON-power/non-gain bridge param (--bw), for the bridge-param
# reconstruction regression: --bw has no _LEVEL_FALLBACK_FLAGS entry, so it is reconstructed ONLY
# through _dest_flag_map(spec) (the argspec path) — pinning finding B's generalisation.
BW_SCRIPT = '''\
import time
from paramkit import Script
s = (Script("tx")
     .number("--bw", min=1, max=40, default=10, live=True)
     .choice("--rf", options=["on", "off"], default="off", live=True, is_rf=True))
args = s.parse()
ctrl = s.live_control(args)
while True:
    for ch in ctrl.drain():
        pass
    time.sleep(0.01)
'''


def _faulted_multi(runner, now, *, rid="rm"):
    """A faulted run whose --power ramp fired up to -70 (the crash level at fault_at=now-5s),
    then the down-time (5 s) SPANNED two more scheduled points -65 (now-4) and -60 (now-2), both
    fault-skipped, before the future -50 (now+3) and the STOP (now+10). The SCHEDULE'S level at
    `now` is -60, not the last-fired -70 — the resync/replay divergence."""
    T0 = now - timedelta(seconds=20)
    steps = [
        _fire("start", 0.0, T0, fired=_iso(T0), args=["--power", "-90", "--rf", "off"], replace=True),
        _fire("tune", 1.0, T0 + timedelta(seconds=1), fired=_iso(T0 + timedelta(seconds=1)),
              params={"rf": "on"}),
        _fire("tune", 1.0, T0 + timedelta(seconds=1), fired=_iso(T0 + timedelta(seconds=1)),
              params={"power": -90}),
        _fire("tune", 14.0, now - timedelta(seconds=6), fired=_iso(now - timedelta(seconds=6)),
              params={"power": -70}),                       # last FIRED (crash level)
        _fire("tune", 16.0, now - timedelta(seconds=4), fired="skipped",
              params={"power": -65}),                       # skipped DURING down-time
        _fire("tune", 18.0, now - timedelta(seconds=2), fired="skipped",
              params={"power": -60}),                       # skipped DURING down-time → schedule-now
        _fire("tune", 23.0, now + timedelta(seconds=3), fired="skipped",
              params={"power": -50}),                       # future ramp point
        _fire("stop", 0.0, now + timedelta(seconds=10), fired="skipped"),
    ]
    run = SequenceRun(id=rid, sequence_id="s1", sequence_name="sweep",
                      state=SequenceState.RUNNING,
                      on_air_at=_iso(T0), on_air_end=_iso(now + timedelta(seconds=10)),
                      steps=steps, fault="tx: vmcircbuf", fault_task="tx",
                      fault_at=_iso(now - timedelta(seconds=5)))
    runner._runs[rid] = run
    return run


def test_restart_resync_uses_the_schedule_level_at_now_not_the_last_fired(tmp_path, monkeypatch):
    """Finding D: resync must rejoin the SCHEDULE — born at the last SCHEDULED (fired-or-skipped)
    point ≤ now (-60), not the last actually-fired point (-70) from before the down-time."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        _faulted_multi(runner, now)
        out = await runner.restart_run("rm", RestartRequest(mode="resync", restart_at=_iso(now)))
        relaunch = [s for s in out.steps if s.action == "start" and s.fired_actual is None][0]
        pi = relaunch.args.index("--power")
        assert float(relaunch.args[pi + 1]) == -60.0          # the schedule's level at now
        # The two down-time points stay MISSED (resync doesn't replay the past); only the future
        # -50 and the STOP are re-instated on their ORIGINAL clock.
        by_pwr = {s.params.get("power"): s for s in out.steps if s.action == "tune"}
        assert by_pwr[-65].fired_actual == "skipped" and by_pwr[-60].fired_actual == "skipped"
        assert by_pwr[-50].fired_actual is None and by_pwr[-50].fire_at == _iso(now + timedelta(seconds=3))
        stop = [s for s in out.steps if s.action == "stop"][0]
        assert stop.fired_actual is None and stop.fire_at == _iso(now + timedelta(seconds=10))

    asyncio.run(scenario())


def test_restart_replay_from_crash_level_reinstates_the_whole_remainder(tmp_path, monkeypatch):
    """Findings E + D: replay resumes from the CRASH level (-70, last actually-fired) and
    re-instates EVERY fault-skipped point — including the ones scheduled DURING the down-time —
    shifted later by the down-time, so the whole remaining profile plays (none are dropped)."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        _faulted_multi(runner, now)                          # fault_at now-5 → downtime 5 s
        out = await runner.restart_run("rm", RestartRequest(mode="replay", restart_at=_iso(now)))
        relaunch = [s for s in out.steps if s.action == "start" and s.fired_actual is None][0]
        pi = relaunch.args.index("--power")
        assert float(relaunch.args[pi + 1]) == -70.0          # the crash level, not the schedule
        by_pwr = {s.params.get("power"): s for s in out.steps if s.action == "tune"}
        # The down-time points are NOT dropped — re-instated, shifted +5 s into the future.
        assert by_pwr[-65].fired_actual is None and by_pwr[-65].fire_at == _iso(now + timedelta(seconds=-4 + 5))
        assert by_pwr[-60].fired_actual is None and by_pwr[-60].fire_at == _iso(now + timedelta(seconds=-2 + 5))
        assert by_pwr[-50].fired_actual is None and by_pwr[-50].fire_at == _iso(now + timedelta(seconds=3 + 5))
        stop = [s for s in out.steps if s.action == "stop"][0]
        assert stop.fire_at == _iso(now + timedelta(seconds=10 + 5))
        assert out.on_air_end == _iso(now + timedelta(seconds=10 + 5))

    asyncio.run(scenario())


def test_restart_bakes_the_swept_param_not_just_power(tmp_path, monkeypatch):
    """Finding B: a task whose ramp swept --GAIN must relaunch at the reconstructed gain, not the
    stale launch gain (an over-power hazard). The full changed live state rides the launch command."""
    async def scenario():
        monkeypatch.setattr(pm._agentcfg, "CTRL_DIR", tmp_path / "ctl")
        script = tmp_path / "gtx.py"
        script.write_text(GAIN_SCRIPT)
        tasks = {"tx": TaskConfig(name="tx", command=["python3", str(script), "--gain", "20",
                                                      "--rf", "off"],
                                  working_dir=str(tmp_path), env={"PYTHONPATH": REPO_ROOT})}
        mgr = ProcessManager(tasks, tmp_path, "unit-a")
        runner = SequenceRunner(mgr, "unit-a", tmp_path / "seq.json",
                                tmp_path / "runs.json", tmp_path)
        now = datetime.now(timezone.utc)
        T0 = now - timedelta(seconds=6)
        steps = [
            _fire("start", 0.0, T0, fired=_iso(T0), args=["--gain", "20", "--rf", "off"], replace=True),
            _fire("tune", 1.0, T0 + timedelta(seconds=1), fired=_iso(T0 + timedelta(seconds=1)),
                  params={"rf": "on"}),
            _fire("tune", 2.0, now - timedelta(seconds=2), fired=_iso(now - timedelta(seconds=2)),
                  params={"gain": 55}),                       # last gain level
            _fire("stop", 0.0, now + timedelta(seconds=10), fired="skipped"),
        ]
        run = SequenceRun(id="rg", sequence_id="s", sequence_name="gramp",
                          state=SequenceState.RUNNING, on_air_at=_iso(T0),
                          on_air_end=_iso(now + timedelta(seconds=10)), steps=steps,
                          fault="tx: halt", fault_task="tx", fault_at=_iso(now - timedelta(seconds=1)))
        runner._runs["rg"] = run
        out = await runner.restart_run("rg", RestartRequest(mode="resync", restart_at=_iso(now)))
        relaunch = [s for s in out.steps if s.action == "start" and s.fired_actual is None][0]
        gi = relaunch.args.index("--gain")
        assert float(relaunch.args[gi + 1]) == 55.0           # reconstructed gain, not the launch 20
        assert "--power" not in relaunch.args                 # never invents a --power the task lacks
        ri = relaunch.args.index("--rf")
        assert relaunch.args[ri + 1] == "on"

    asyncio.run(scenario())


def test_restart_does_not_skip_a_healthy_peer_task_step(tmp_path, monkeypatch):
    """Finding C: the recovery must touch ONLY the faulted task. A healthy PEER task's past-due
    un-fired step must stay pending (the tick fires it), not be swept to 'skipped'."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        T0 = now - timedelta(seconds=10)
        steps = [
            _fire("start", 0.0, T0, fired=_iso(T0), args=["--power", "-90", "--rf", "off"],
                  replace=True),
            _fire("tune", 1.0, T0 + timedelta(seconds=1), fired=_iso(T0 + timedelta(seconds=1)),
                  params={"rf": "on"}),
            _fire("tune", 5.0, now - timedelta(seconds=5), fired=_iso(now - timedelta(seconds=5)),
                  params={"power": -70}),
            _fire("stop", 0.0, now + timedelta(seconds=10), fired="skipped"),
            # A HEALTHY peer task with a past-due un-fired tune (the tick just hasn't fired it yet).
            _fire("tune", 9.0, now - timedelta(seconds=1), fired=None, task="tx2",
                  params={"power": -40}),
        ]
        run = SequenceRun(id="rp", sequence_id="s", sequence_name="two", state=SequenceState.RUNNING,
                          on_air_at=_iso(T0), on_air_end=_iso(now + timedelta(seconds=10)),
                          steps=steps, fault="tx: vmcircbuf", fault_task="tx",
                          fault_at=_iso(now - timedelta(seconds=3)))
        runner._runs["rp"] = run
        out = await runner.restart_run("rp", RestartRequest(restart_at=_iso(now)))
        peer = [s for s in out.steps if s.task_name == "tx2"][0]
        assert peer.fired_actual is None                      # still pending — NOT skipped

    asyncio.run(scenario())


def test_restart_resync_refuses_past_off_air_but_replay_recovers(tmp_path, monkeypatch):
    """Finding A: resync must refuse a run whose on-air window has already ended — there is no
    remaining STOP to rejoin, so relaunching would leave RF ON forever. replay (which shifts the
    whole profile) still recovers it."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        T0 = now - timedelta(seconds=20)

        def _late():
            return [
                _fire("start", 0.0, T0, fired=_iso(T0), args=["--power", "-50", "--rf", "off"],
                      replace=True),
                _fire("tune", 1.0, T0 + timedelta(seconds=1), fired=_iso(T0 + timedelta(seconds=1)),
                      params={"rf": "on"}),
                _fire("stop", 0.0, now - timedelta(seconds=2), fired="skipped"),   # STOP already past
            ]
        run = SequenceRun(id="rl", sequence_id="s", sequence_name="late", state=SequenceState.RUNNING,
                          on_air_at=_iso(T0), on_air_end=_iso(now - timedelta(seconds=2)),
                          steps=_late(), fault="tx: halt", fault_task="tx",
                          fault_at=_iso(now - timedelta(seconds=8)))
        runner._runs["rl"] = run
        try:
            await runner.restart_run("rl", RestartRequest(mode="resync", restart_at=_iso(now)))
            assert False, "expected resync to refuse a window that has ended"
        except ValueError as exc:
            assert "already ended" in str(exc) and "replay" in str(exc)
        # The refusal was atomic — nothing mutated.
        assert run.fault == "tx: halt" and [s for s in run.steps if s.action == "stop"][0].fired_actual == "skipped"

        # replay shifts the STOP into the future (down-time 8 s) → recoverable.
        out = await runner.restart_run("rl", RestartRequest(mode="replay", restart_at=_iso(now)))
        assert out.fault == ""
        stop = [s for s in out.steps if s.action == "stop"][0]
        assert stop.fired_actual is None and _parse_ok(stop.fire_at) > now

    asyncio.run(scenario())


def test_restart_open_ended_run_recovers_without_a_stop(tmp_path, monkeypatch):
    """The no-future-STOP guard is skipped for an OPEN-ENDED run (it legitimately has no STOP —
    it runs until aborted), so resync recovers it."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        T0 = now - timedelta(seconds=6)
        steps = [
            _fire("start", 0.0, T0, fired=_iso(T0), args=["--power", "-55", "--rf", "off"],
                  replace=True),
            _fire("tune", 1.0, T0 + timedelta(seconds=1), fired=_iso(T0 + timedelta(seconds=1)),
                  params={"rf": "on"}),
        ]
        run = SequenceRun(id="ro", sequence_id="s", sequence_name="beacon",
                          state=SequenceState.RUNNING, on_air_at=_iso(T0), on_air_end=None,
                          open_ended=True, steps=steps, fault="tx: halt", fault_task="tx",
                          fault_at=_iso(now - timedelta(seconds=1)))
        runner._runs["ro"] = run
        out = await runner.restart_run("ro", RestartRequest(restart_at=_iso(now)))
        assert out.fault == ""
        relaunch = [s for s in out.steps if s.action == "start" and s.fired_actual is None][0]
        assert relaunch.args[relaunch.args.index("--rf") + 1] == "on"

    asyncio.run(scenario())


def test_restart_replay_collision_uses_the_stop_tail_and_is_atomic(tmp_path, monkeypatch):
    """Findings F + G: the replay collision guard uses the CHANNEL end (the stop tail after off-air),
    not off-air alone — a peer starting AT the shifted off-air but before the shifted stop tail is
    caught — and refuses BEFORE any mutation (the run is left untouched)."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        T0 = now - timedelta(seconds=10)
        # on_air_end now+8, but the STOP tail lands at now+10 (off-air + 2 s). downtime 2 s.
        steps = [
            _fire("start", 0.0, T0, fired=_iso(T0), args=["--power", "-60", "--rf", "off"],
                  replace=True),
            _fire("tune", 1.0, T0 + timedelta(seconds=1), fired=_iso(T0 + timedelta(seconds=1)),
                  params={"rf": "on"}),
            _fire("stop", 0.0, now + timedelta(seconds=10), fired="skipped"),
        ]
        run = SequenceRun(id="rc", sequence_id="s", sequence_name="tail", state=SequenceState.RUNNING,
                          on_air_at=_iso(T0), on_air_end=_iso(now + timedelta(seconds=8)),
                          steps=steps, fault="tx: halt", fault_task="tx",
                          fault_at=_iso(now - timedelta(seconds=2)))
        runner._runs["rc"] = run
        snapshot = [(s.fired_actual, s.fire_at) for s in run.steps]
        # replay shifts off-air → now+10 and the STOP tail → now+12. A peer at [now+10, now+11]
        # does NOT overlap off-air (now+10) but DOES overlap the stop tail (now+12).
        peer = SequenceRun(id="peer", sequence_id="s2", sequence_name="next",
                           state=SequenceState.ARMED,
                           on_air_at=_iso(now + timedelta(seconds=10)),
                           on_air_end=_iso(now + timedelta(seconds=11)),
                           steps=[_fire("start", 0.0, now + timedelta(seconds=10), fired=None)])
        runner._runs["peer"] = peer
        try:
            await runner.restart_run("rc", RestartRequest(mode="replay", restart_at=_iso(now)))
            assert False, "expected the stop-tail collision to be refused"
        except ValueError as exc:
            assert "overlapping" in str(exc)
        # Atomic: the run is byte-for-byte what it was — no re-instatement, off-air unchanged, fault kept.
        assert [(s.fired_actual, s.fire_at) for s in run.steps] == snapshot
        assert run.on_air_end == _iso(now + timedelta(seconds=8)) and run.fault == "tx: halt"

    asyncio.run(scenario())


def test_restart_revalidates_after_the_unlocked_pre_stop(tmp_path, monkeypatch):
    """Finding H: the lock is released to stop the faulted process; if the run's fault CHANGES in
    that gap (aborted, recovered, or re-faulted on another task), the restart must refuse rather
    than mutate a stale plan."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = _faulted_run(runner, now)

        real_stop = mgr.stop

        async def racing_stop(name, source="manual"):
            run.fault_task = "someone_else"               # a concurrent change during the await
            return await real_stop(name, source=source)

        monkeypatch.setattr(mgr, "stop", racing_stop)
        try:
            await runner.restart_run("r1", RestartRequest(restart_at=_iso(now)))
            assert False, "expected a refusal after the fault changed under us"
        except ValueError as exc:
            assert "changed" in str(exc)
        # Nothing recovered — the run keeps its (now-different) fault, un-mutated.
        assert run.fault == "tx: vmcircbuf"

    asyncio.run(scenario())


def test_restart_preserves_the_level_when_the_argspec_is_unreadable(tmp_path, monkeypatch):
    """Re-review finding 1: the level reconstruction must NOT revert to the launch --power when the
    argspec is momentarily unreadable (spec=None → the per-dest flag map is empty). A task launched
    at the ramp TOP (-50) that faulted mid-sweep at -70 must relaunch at -70, not the hot -50."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        T0 = now - timedelta(seconds=10)
        steps = [
            _fire("start", 0.0, T0, fired=_iso(T0), args=["--power", "-50", "--rf", "on"],
                  replace=True),                                  # launched at the ramp TOP
            _fire("tune", 5.0, now - timedelta(seconds=5), fired=_iso(now - timedelta(seconds=5)),
                  params={"power": -70}),                         # a down-ramp; crash level -70
            _fire("stop", 0.0, now + timedelta(seconds=10), fired="skipped"),
        ]
        run = SequenceRun(id="rn", sequence_id="s", sequence_name="down", state=SequenceState.RUNNING,
                          on_air_at=_iso(T0), on_air_end=_iso(now + timedelta(seconds=10)),
                          steps=steps, fault="tx: halt", fault_task="tx",
                          fault_at=_iso(now - timedelta(seconds=3)))
        runner._runs["rn"] = run
        # The argspec is unreadable at restart (transient miss) → tune_log_context yields spec=None.
        monkeypatch.setattr(mgr, "tune_log_context", lambda t: (None, None))
        out = await runner.restart_run("rn", RestartRequest(restart_at=_iso(now)))
        relaunch = [s for s in out.steps if s.action == "start" and s.fired_actual is None][0]
        pi = relaunch.args.index("--power")
        assert float(relaunch.args[pi + 1]) == -70.0            # the crash level, spec-independent

    asyncio.run(scenario())


def test_restart_muted_pre_roll_relaunches_muted_not_forced_on(tmp_path, monkeypatch):
    """Re-review finding 2 (pre-roll): a fault during the muted pre-roll (launch --rf off, RF-on
    tune at T0 still in the future) must relaunch MUTED — the re-instated RF-on tune drives the
    gate at T0. Forcing RF on at relaunch would transmit before on-air (a hot pre-on-air blip)."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        T0 = now + timedelta(seconds=5)                          # on-air is in the FUTURE (pre-roll)
        steps = [
            _fire("start", -1.0, now - timedelta(seconds=1), fired=_iso(now - timedelta(seconds=1)),
                  args=["--power", "-60", "--rf", "off"], replace=True),      # muted launch, fired
            _fire("tune", 0.0, T0, fired="skipped", params={"rf": "on"}),     # RF-on at T0 (skipped)
            _fire("tune", 0.0, T0, fired="skipped", params={"power": -90}),   # ramp start (skipped)
            _fire("stop", 0.0, now + timedelta(seconds=20), fired="skipped"),
        ]
        run = SequenceRun(id="rpr", sequence_id="s", sequence_name="preroll",
                          state=SequenceState.RUNNING, on_air_at=_iso(T0),
                          on_air_end=_iso(now + timedelta(seconds=20)), steps=steps,
                          fault="tx: halt", fault_task="tx", fault_at=_iso(now - timedelta(seconds=0.5)))
        runner._runs["rpr"] = run
        out = await runner.restart_run("rpr", RestartRequest(restart_at=_iso(now)))
        relaunch = [s for s in out.steps if s.action == "start" and s.fired_actual is None][0]
        ri = relaunch.args.index("--rf")
        assert relaunch.args[ri + 1] == "off"                   # relaunched MUTED, not forced on
        # The RF-on tune (future) is re-instated so it un-mutes at T0.
        rf_on = [s for s in out.steps if s.action == "tune" and s.params.get("rf") == "on"][0]
        assert rf_on.fired_actual is None

    asyncio.run(scenario())


def test_restart_cool_down_tail_relaunches_muted(tmp_path, monkeypatch):
    """Re-review finding 2 (cool-down): a restart in the cool-down tail — past the off-air RF-off
    tune but before the STOP — must relaunch MUTED (the schedule holds RF off there), then the
    future STOP fires. Forcing RF on would re-transmit until the STOP."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        T0 = now - timedelta(seconds=20)
        steps = [
            _fire("start", 0.0, T0, fired=_iso(T0), args=["--power", "-60", "--rf", "off"],
                  replace=True),
            _fire("tune", 1.0, T0 + timedelta(seconds=1), fired=_iso(T0 + timedelta(seconds=1)),
                  params={"rf": "on"}),
            _fire("tune", 2.0, T0 + timedelta(seconds=2), fired=_iso(T0 + timedelta(seconds=2)),
                  params={"power": -50}),
            _fire("tune", 0.0, now - timedelta(seconds=1), fired="skipped",   # off-air RF-off (past)
                  params={"rf": "off"}),
            _fire("stop", 0.0, now + timedelta(seconds=2), fired="skipped"),  # STOP (future)
        ]
        run = SequenceRun(id="rcd", sequence_id="s", sequence_name="cooldown",
                          state=SequenceState.RUNNING, on_air_at=_iso(T0),
                          on_air_end=_iso(now - timedelta(seconds=1)), steps=steps,
                          fault="tx: halt", fault_task="tx", fault_at=_iso(now - timedelta(seconds=5)))
        runner._runs["rcd"] = run
        out = await runner.restart_run("rcd", RestartRequest(restart_at=_iso(now)))
        relaunch = [s for s in out.steps if s.action == "start" and s.fired_actual is None][0]
        ri = relaunch.args.index("--rf")
        assert relaunch.args[ri + 1] == "off"                   # the schedule holds RF off at now
        stop = [s for s in out.steps if s.action == "stop"][0]
        assert stop.fired_actual is None                        # the future STOP still fires

    asyncio.run(scenario())


def test_restart_reconstructs_a_bridge_param_via_the_argspec(tmp_path, monkeypatch):
    """Re-review coverage (finding B generalisation): a swept NON-power/non-gain bridge param (--bw)
    has no _LEVEL_FALLBACK_FLAGS entry, so it is reconstructed ONLY via _dest_flag_map(spec). A --bw
    sweep must be baked onto the relaunch, not left at the launch default (calibration folds --power
    at the live bandwidth, so a wrong bw is an over-power/spectrum hazard)."""
    async def scenario():
        monkeypatch.setattr(pm._agentcfg, "CTRL_DIR", tmp_path / "ctl")
        script = tmp_path / "btx.py"
        script.write_text(BW_SCRIPT)
        tasks = {"tx": TaskConfig(name="tx", command=["python3", str(script), "--bw", "10",
                                                      "--rf", "off"],
                                  working_dir=str(tmp_path), env={"PYTHONPATH": REPO_ROOT})}
        mgr = ProcessManager(tasks, tmp_path, "unit-a")
        runner = SequenceRunner(mgr, "unit-a", tmp_path / "seq.json",
                                tmp_path / "runs.json", tmp_path)
        now = datetime.now(timezone.utc)
        T0 = now - timedelta(seconds=6)
        steps = [
            _fire("start", 0.0, T0, fired=_iso(T0), args=["--bw", "10", "--rf", "off"], replace=True),
            _fire("tune", 1.0, T0 + timedelta(seconds=1), fired=_iso(T0 + timedelta(seconds=1)),
                  params={"rf": "on"}),
            _fire("tune", 2.0, now - timedelta(seconds=2), fired=_iso(now - timedelta(seconds=2)),
                  params={"bw": 25}),                             # last bridge value swept
            _fire("stop", 0.0, now + timedelta(seconds=10), fired="skipped"),
        ]
        run = SequenceRun(id="rb", sequence_id="s", sequence_name="bwsweep",
                          state=SequenceState.RUNNING, on_air_at=_iso(T0),
                          on_air_end=_iso(now + timedelta(seconds=10)), steps=steps,
                          fault="tx: halt", fault_task="tx", fault_at=_iso(now - timedelta(seconds=1)))
        runner._runs["rb"] = run
        out = await runner.restart_run("rb", RestartRequest(mode="resync", restart_at=_iso(now)))
        relaunch = [s for s in out.steps if s.action == "start" and s.fired_actual is None][0]
        bi = relaunch.args.index("--bw")
        assert float(relaunch.args[bi + 1]) == 25.0            # reconstructed via the argspec, not 10
        assert relaunch.args[relaunch.args.index("--rf") + 1] == "on"

    asyncio.run(scenario())


def test_faulted_multi_task_run_does_not_auto_complete_when_a_peer_finishes(tmp_path, monkeypatch):
    """Re-review finding (recovery-robustness): a faulted task's un-fired steps are 'skipped' (which
    counts as fired_actual-not-None). In a MULTI-task run, when the HEALTHY peer finishes, the
    completion check would flip the run COMPLETED with the fault unrecovered — and restart_run refuses
    a non-RUNNING run. The `not run.fault` guard keeps a faulted run RUNNING (restartable)."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        T0 = now - timedelta(seconds=10)
        peer_stop = _fire("stop", 0.0, now + timedelta(seconds=1), fired=None, task="tx2")
        steps = [
            _fire("start", 0.0, T0, fired=_iso(T0), args=["--power", "-60", "--rf", "on"],
                  replace=True),                                  # faulted task tx
            _fire("tune", 5.0, now - timedelta(seconds=5), fired="skipped", params={"power": -50}),
            _fire("stop", 0.0, now + timedelta(seconds=10), fired="skipped"),   # tx STOP (skipped)
            _fire("start", 0.0, T0, fired=_iso(T0), args=["--power", "-40", "--rf", "on"],
                  replace=True, task="tx2"),                      # healthy peer tx2
            peer_stop,                                            # tx2 STOP (pending)
        ]
        run = SequenceRun(id="rmt", sequence_id="s", sequence_name="two", state=SequenceState.RUNNING,
                          on_air_at=_iso(T0), on_air_end=_iso(now + timedelta(seconds=10)), steps=steps,
                          fault="tx: vmcircbuf", fault_task="tx",
                          fault_at=_iso(now - timedelta(seconds=3)))
        runner._runs["rmt"] = run
        # Fire the peer's final step → every step is now fired-or-skipped; the completion check runs.
        await runner._fire_step(run, peer_stop)
        assert run.state == SequenceState.RUNNING and run.fault == "tx: vmcircbuf"   # NOT auto-completed
        # And the faulted task is still recoverable.
        out = await runner.restart_run("rmt", RestartRequest(restart_at=_iso(now)))
        assert out.fault == "" and out.state == SequenceState.RUNNING

    asyncio.run(scenario())


def _parse_ok(iso):
    from agent.sequence_runner import _parse
    return _parse(iso)


# ── Live end-to-end: fault a real ramping task mid-run, restart it back on schedule ──

def test_restart_live_recovers_a_faulted_ramp(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        await mgr.startup()
        await runner.startup()
        try:
            steps = [
                SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx",
                             args=["--power", "-90", "--rf", "off"], replace_args=True),
                SequenceStep(anchor="start", offset_s=1.0, action=StepAction.TUNE, task_name="tx",
                             params={"rf": "on"}),
                SequenceStep(anchor="start", offset_s=1.0, action=StepAction.TUNE, task_name="tx",
                             params={"power": -90}),
                SequenceStep(anchor="start", offset_s=2.0, action=StepAction.TUNE, task_name="tx",
                             params={"power": -70}),
                SequenceStep(anchor="start", offset_s=6.0, action=StepAction.TUNE, task_name="tx",
                             params={"power": -50}),
                SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
            ]
            seq = await runner.create_sequence(CreateSequenceRequest(name="sweep", steps=steps))
            now = datetime.now(timezone.utc)
            run = await runner.arm(seq.id, ArmSequenceRequest(
                on_air_at=_iso(now + timedelta(seconds=0.5)),
                on_air_end=_iso(now + timedelta(seconds=9)),
                open_ended=False), _iso(now + timedelta(seconds=9)))
            rid = run.id

            # Let it launch, un-mute, and ramp to -70 (the -50 point is at ~+6.5s, still ahead).
            await asyncio.sleep(3.5)
            assert mgr.is_running("tx")
            # Simulate the Phase-1 fault coupling at -70: stamp the run + kill the task.
            await runner.on_task_fault("tx", "vmcircbuf")
            await mgr.stop("tx", source="sequence")
            await asyncio.sleep(0.5)
            faulted = runner.get_run(rid)
            assert faulted.fault and not mgr.is_running("tx")

            # Restart (resync): relaunch at the -70 it faulted from, RF on, still on the -50/STOP.
            await runner.restart_run(rid, RestartRequest(mode="resync"))
            await asyncio.sleep(2.5)
            assert mgr.is_running("tx")                       # the task is back
            recovered = runner.get_run(rid)
            assert recovered.fault == ""                       # cleared

            # The -50 point + STOP still fire on schedule → the run completes.
            await asyncio.sleep(5.0)
            done = runner.get_run(rid)
            assert done.state == SequenceState.COMPLETED
            assert not mgr.is_running("tx")
        finally:
            await runner.shutdown()
            await mgr.shutdown()

    asyncio.run(scenario())
