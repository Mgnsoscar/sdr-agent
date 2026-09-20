"""Restart reconstructs EVERY parameter + the script's elapsed time (docs/rf-fault-recovery.md §14g).

Owner ask after the P0–P3b review: (a) a crashed-and-restarted process must come back with ALL its
parameters correct — not only the power level of a ramp, and also for a run with no ramp at all;
(b) a time-dependent script (cw_drift) declares its elapsed-time parameter so the restart resumes
its own timeline at the right point.

Unit tests over constructed faulted runs (the Phase-1 crash-time state), the standalone relaunch,
build_resume_request, and the paramkit/argspec marker.
"""
import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import test_sequence_restart as T                 # _fire / _iso / LIVE_SCRIPT / _mk / _faulted_run
from agent import cmdargs
from agent import process_manager as pm
from agent.argspec import extract_params
from agent.models import RestartRequest, SequenceRun, SequenceState, StartRequest, TaskConfig
from agent.process_manager import ProcessManager
from agent.sequence_runner import SequenceRunner
from paramkit import Script

REPO_ROOT = str(Path(__file__).resolve().parents[1])

# A cw_drift-shaped script: a choice (the drift mode), numbers (carrier, bandwidth, duration), a
# store_true trigger, the RF gate, and the ELAPSED-TIME parameter (is_elapsed).
DRIFT_SCRIPT = '''\
import time
from paramkit import Script
s = (Script("drift")
     .number("-Start-frequency", "--freq", unit="MHz", min=70, max=6000, default=1575.42, live=True)
     .number("-Duration", "--duration", unit="min", min=0.1, default=10.0)
     .choice("-Drift", "--drift", options=["once", "loop", "pingpong"], default="once", live=True)
     .number("-Elapsed", "--elapsed", unit="s", min=0.0, default=0.0, is_elapsed=True)
     .number("--bw", min=1, max=40, default=10, live=True)
     .number("--power", min=-120, max=0, default=-50, live=True)
     .choice("--rf", options=["on", "off"], default="off", live=True, is_rf=True)
     .flag("--restart", live=True))
args = s.parse()
ctrl = s.live_control(args)
while True:
    for ch in ctrl.drain():
        ctrl.report(ch.name, ch.value)
    time.sleep(0.01)
'''


def _mk(tmp_path, monkeypatch, command_args, *, script_src=DRIFT_SCRIPT, **cfg):
    monkeypatch.setattr(pm._agentcfg, "CTRL_DIR", tmp_path / "ctl")
    script = tmp_path / "drift.py"
    script.write_text(script_src)
    tasks = {"tx": TaskConfig(name="tx", command=["python3", str(script), *command_args],
                              working_dir=str(tmp_path), env={"PYTHONPATH": REPO_ROOT}, **cfg)}
    mgr = ProcessManager(tasks, tmp_path, "unit-a")
    runner = SequenceRunner(mgr, "unit-a", tmp_path / "seq.json", tmp_path / "runs.json", tmp_path)
    return mgr, runner


def _install(runner, steps, *, T0, now, fault_at, rid="r1", on_air_end=None):
    run = SequenceRun(id=rid, sequence_id="s1", sequence_name="drift", state=SequenceState.RUNNING,
                      on_air_at=T._iso(T0),
                      on_air_end=T._iso(on_air_end or (now + timedelta(seconds=60))),
                      steps=steps, fault="tx: vmcircbuf", fault_task="tx", fault_at=T._iso(fault_at))
    runner._runs[rid] = run
    return run


# The flags the DRIFT_SCRIPT params answer to (either spelling reaches the same dest).
_FLAGS = {"freq": ("-Start-frequency", "--freq"), "duration": ("-Duration", "--duration"),
          "drift": ("-Drift", "--drift"), "elapsed": ("-Elapsed", "--elapsed"), "bw": ("--bw",),
          "power": ("--power",), "rf": ("--rf",), "restart": ("--restart",)}


def _is_flag(tok):
    return str(tok).startswith("-") and not re.match(r"^-\d", str(tok))   # "-40" is a value


def _argdict(args):
    """{dest: value} of a post-script arg list (last occurrence wins, like argparse), keyed by the
    DEST whichever flag spelling set it; an unknown flag keeps its own name."""
    by_flag = {f: d for d, fs in _FLAGS.items() for f in fs}
    out = {}
    for i, a in enumerate(args):
        if _is_flag(a) and i + 1 < len(args) and not _is_flag(args[i + 1]):
            out[by_flag.get(str(a), str(a))] = args[i + 1]
    return out


def _relaunch_of(run):
    return [s for s in run.steps if s.action == "start" and s.fired_actual is None][0]


# ── (a) every parameter, not only a swept level ─────────────────────────────────────────────────

def test_restart_bakes_a_tuned_choice_and_carrier_not_only_numerics(tmp_path, monkeypatch):
    """A tuned STRING/choice (--drift once→loop) and a tuned carrier are reconstructed onto the
    relaunch by their argspec flags, alongside the level and the gate. (The run-owned path used to
    bake numerics only — a tuned choice silently reverted to the launch value.)"""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch, ["--drift", "once", "--power", "-50", "--rf", "off"])
        now = datetime.now(timezone.utc)
        T0 = now - timedelta(seconds=20)
        steps = [
            T._fire("start", 0.0, T0, fired=T._iso(T0),
                    args=["--drift", "once", "--power", "-50", "--rf", "off"], replace=True),
            T._fire("tune", 1.0, T0 + timedelta(seconds=1), fired=T._iso(T0 + timedelta(seconds=1)),
                    params={"rf": "on"}),
            T._fire("tune", 5.0, T0 + timedelta(seconds=5), fired=T._iso(T0 + timedelta(seconds=5)),
                    params={"drift": "loop", "freq": 1300.0}),
            T._fire("tune", 8.0, T0 + timedelta(seconds=8), fired=T._iso(T0 + timedelta(seconds=8)),
                    params={"restart": True}),                    # a trigger: an event, not state
            T._fire("stop", 0.0, now + timedelta(seconds=60), fired="skipped"),
        ]
        _install(runner, steps, T0=T0, now=now, fault_at=now - timedelta(seconds=2))
        out = await runner.restart_run("r1", RestartRequest(mode="resync", restart_at=T._iso(now)))
        a = _argdict(_relaunch_of(out).args)
        assert a["drift"] == "loop"                    # the choice, not the launch "once"
        assert float(a["freq"]) == 1300.0              # the moved carrier
        assert a["rf"] == "on" and float(a["power"]) == -50.0
        assert "--restart" not in _relaunch_of(out).args  # a store_true trigger is never re-fired
    asyncio.run(scenario())


def test_restart_with_no_ramp_reproduces_every_launch_and_tuned_param(tmp_path, monkeypatch):
    """A run that ramps NOTHING: its launch args are its state. Every launch parameter (duration,
    bandwidth, mode, carrier, level, gate) survives the relaunch verbatim and the one tune it made
    (a lowered power) is applied on top — nothing reverts to the script default."""
    async def scenario():
        launch = ["--freq", "1600", "--duration", "180", "--drift", "pingpong", "--bw", "20",
                  "--power", "-30", "--rf", "on"]
        mgr, runner = _mk(tmp_path, monkeypatch, launch)
        now = datetime.now(timezone.utc)
        T0 = now - timedelta(seconds=30)
        steps = [
            T._fire("start", 0.0, T0, fired=T._iso(T0), args=list(launch), replace=True),
            T._fire("tune", 10.0, T0 + timedelta(seconds=10), fired=T._iso(T0 + timedelta(seconds=10)),
                    params={"power": -40}),
            T._fire("stop", 0.0, now + timedelta(seconds=60), fired="skipped"),
        ]
        _install(runner, steps, T0=T0, now=now, fault_at=now - timedelta(seconds=3))
        out = await runner.restart_run("r1", RestartRequest(mode="replay", restart_at=T._iso(now)))
        a = _argdict(_relaunch_of(out).args)
        expect = dict(_argdict(launch), power="-40")
        got = {k: v for k, v in a.items() if k != "elapsed"}   # (+ the elapsed the marker adds)
        norm = lambda d: {k: (v if k in ("drift", "rf") else float(v)) for k, v in d.items()}
        assert norm(got) == norm(expect)
    asyncio.run(scenario())


def test_restart_carries_an_operator_hand_tune_the_schedule_never_drove(tmp_path, monkeypatch):
    """A parameter the operator tuned BY HAND during the run (the Tune… dialog: recorded on the
    process, not in run.steps) is carried by the relaunch when the schedule never drives that dest —
    the crash-time live state is the truth there. A dest the schedule DOES drive follows the
    schedule's position at the cutoff (the hand-tune of it is superseded)."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch, ["--bw", "10", "--power", "-50", "--rf", "off"])
        now = datetime.now(timezone.utc)
        T0 = now - timedelta(seconds=20)
        steps = [
            T._fire("start", 0.0, T0, fired=T._iso(T0),
                    args=["--bw", "10", "--power", "-50", "--rf", "off"], replace=True),
            T._fire("tune", 1.0, T0 + timedelta(seconds=1), fired=T._iso(T0 + timedelta(seconds=1)),
                    params={"rf": "on"}),
            T._fire("tune", 5.0, T0 + timedelta(seconds=5), fired=T._iso(T0 + timedelta(seconds=5)),
                    params={"power": -70}),
            T._fire("tune", 30.0, now + timedelta(seconds=10), fired="skipped", params={"power": -60}),
            T._fire("stop", 0.0, now + timedelta(seconds=60), fired="skipped"),
        ]
        _install(runner, steps, T0=T0, now=now, fault_at=now - timedelta(seconds=2))
        # The operator's hand tunes on the faulted process: bw (never scheduled) + rf off + power -45
        # (both scheduled → the schedule wins).
        mgr._procs["tx"]._live_applied = {"bw": 33.0, "rf": "off", "power": -45.0}
        out = await runner.restart_run("r1", RestartRequest(mode="resync", restart_at=T._iso(now)))
        a = _argdict(_relaunch_of(out).args)
        assert float(a["bw"]) == 33.0                  # the hand tune, carried
        assert a["rf"] == "on"                         # the schedule's gate at the cutoff
        assert float(a["power"]) == -70.0              # the schedule's level (last counted ≤ now)
    asyncio.run(scenario())


def test_restart_ignores_the_live_record_when_this_run_never_launched_the_task(tmp_path, monkeypatch):
    """No counted launch fire (the task was launched outside this run's counted epoch) ⇒ the live
    record is NOT merged (it belongs to a process this run doesn't own) and nothing is invented."""
    mgr, runner = _mk(tmp_path, monkeypatch, ["--bw", "10", "--power", "-50", "--rf", "off"])
    now = datetime.now(timezone.utc)
    T0 = now - timedelta(seconds=20)
    steps = [T._fire("stop", 0.0, now + timedelta(seconds=60), fired="skipped")]
    run = _install(runner, steps, T0=T0, now=now, fault_at=now - timedelta(seconds=2))
    mgr._procs["tx"]._live_applied = {"bw": 33.0}
    fire = runner._relaunch_start_fire(run, "tx", now, runner._spec_of("tx"), include_skipped=True,
                                       elapsed_at=now)
    a = _argdict(fire.args)
    assert float(a["bw"]) == 10.0 and "--elapsed" not in fire.args


# ── (b) the script-declared elapsed time ─────────────────────────────────────────────────────────

def test_restart_bakes_the_elapsed_time_resync_now_replay_crash_point(tmp_path, monkeypatch):
    """A script declaring an is_elapsed parameter is relaunched with it = the seconds its timeline
    has reached: resync → since the launch up to NOW (rejoin the schedule); replay → up to the FAULT
    instant (the rest of the profile is shifted by the down-time, so is the clock)."""
    async def scenario():
        launch = ["--freq", "1600", "--duration", "180", "--power", "-30", "--rf", "on"]
        for mode, expect in (("resync", 100.0), ("replay", 70.0)):
            mgr, runner = _mk(tmp_path, monkeypatch, launch)
            now = datetime.now(timezone.utc)
            T0 = now - timedelta(seconds=100)
            steps = [
                T._fire("start", 0.0, T0, fired=T._iso(T0), args=list(launch), replace=True),
                T._fire("stop", 0.0, now + timedelta(seconds=600), fired="skipped"),
            ]
            _install(runner, steps, T0=T0, now=now, fault_at=now - timedelta(seconds=30), rid=mode,
                     on_air_end=now + timedelta(seconds=600))
            out = await runner.restart_run(mode, RestartRequest(mode=mode, restart_at=T._iso(now)))
            a = _argdict(_relaunch_of(out).args)
            assert abs(float(a["elapsed"]) - expect) < 0.01, (mode, a)
            assert float(a["freq"]) == 1600.0 and a["rf"] == "on"      # the rest untouched
    asyncio.run(scenario())


def test_restart_elapsed_adds_to_what_the_launch_already_carried(tmp_path, monkeypatch):
    """A launch that already began part-way (--elapsed 500, e.g. the relaunch of an EARLIER restart,
    or an operator starting mid-drift) accumulates: 500 + the seconds it then ran. So a second fault
    chains correctly off the synthetic relaunch fire."""
    mgr, runner = _mk(tmp_path, monkeypatch, ["--freq", "1600", "--power", "-30", "--rf", "on"])
    now = datetime.now(timezone.utc)
    T0 = now - timedelta(seconds=200)
    T1 = now - timedelta(seconds=40)                     # an earlier restart's relaunch, fired at T1
    steps = [
        T._fire("start", 0.0, T0, fired=T._iso(T0), args=["--freq", "1600", "--rf", "on"], replace=True),
        T._fire("start", 0.0, T1, fired=T._iso(T1),
                args=["--freq", "1600", "--rf", "on", "-Elapsed", "500"], replace=True),
        T._fire("stop", 0.0, now + timedelta(seconds=60), fired="skipped"),
    ]
    run = _install(runner, steps, T0=T0, now=now, fault_at=now - timedelta(seconds=10))
    spec = runner._spec_of("tx")
    fire = runner._relaunch_start_fire(run, "tx", now, spec, include_skipped=True, elapsed_at=now)
    a = _argdict(fire.args)
    assert abs(float(a["elapsed"]) - 540.0) < 0.01     # 500 carried + 40 s run since T1
    assert "--elapsed" not in fire.args and "-Elapsed" in fire.args   # the launch's own spelling
    # replay: up to the fault instant
    fire = runner._relaunch_start_fire(run, "tx", now, spec, include_skipped=False,
                                       elapsed_at=now - timedelta(seconds=10))
    assert abs(float(_argdict(fire.args)["elapsed"]) - 530.0) < 0.01


def test_restart_resync_counts_a_fault_skipped_launch_from_its_scheduled_instant(tmp_path, monkeypatch):
    """resync counts a launch the fault SKIPPED (fired_actual='skipped'): the schedule says the task
    should have been launched at fire_at, so the elapsed runs from THAT scheduled instant."""
    mgr, runner = _mk(tmp_path, monkeypatch, ["--freq", "1600", "--power", "-30", "--rf", "on"])
    now = datetime.now(timezone.utc)
    T0 = now - timedelta(seconds=200)
    steps = [
        T._fire("start", 0.0, T0, fired=T._iso(T0), args=["--freq", "1600"], replace=True),
        T._fire("stop", 0.0, T0 + timedelta(seconds=100), fired=T._iso(T0 + timedelta(seconds=100))),
        T._fire("start", 150.0, T0 + timedelta(seconds=150), fired="skipped",
                args=["--freq", "1300"], replace=True),
        T._fire("stop", 0.0, now + timedelta(seconds=60), fired="skipped"),
    ]
    run = _install(runner, steps, T0=T0, now=now, fault_at=T0 + timedelta(seconds=120))
    fire = runner._relaunch_start_fire(run, "tx", now, runner._spec_of("tx"), include_skipped=True,
                                       elapsed_at=now)
    a = _argdict(fire.args)
    assert float(a["freq"]) == 1300.0                  # the second (counted) launch's args
    assert abs(float(a["elapsed"]) - 50.0) < 0.01       # now − (T0 + 150 s)


def test_restart_bakes_no_elapsed_for_a_script_without_the_marker(tmp_path, monkeypatch):
    """A script that declares no is_elapsed param gets nothing invented (byte-identical args to
    before the marker existed)."""
    async def scenario():
        mgr, runner = T._mk(tmp_path, monkeypatch)       # LIVE_SCRIPT: --power/--rf only
        now = datetime.now(timezone.utc)
        T._faulted_run(runner, now)
        out = await runner.restart_run("r1", RestartRequest(mode="resync", restart_at=T._iso(now)))
        args = _relaunch_of(out).args
        assert not any(str(a).lower().lstrip("-") == "elapsed" for a in args)
        assert _argdict(args) == {"power": "-70", "rf": "on"}
    asyncio.run(scenario())


def test_standalone_relaunch_bakes_the_elapsed_since_the_spawn(tmp_path, monkeypatch):
    """The standalone auto-restart relaunch (ProcessManager.relaunch) resumes a time-dependent script
    at (its launch --elapsed) + the wall-clock seconds since the faulted process was spawned, on top
    of every live-tuned value it already bakes; the launch request is otherwise untouched."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch, [], auto_restart_on_fault=True, restart_delay_s=0.0)
        proc = mgr._procs["tx"]
        calls = []

        async def fake_start(request=None):
            calls.append(request)
        proc.start = fake_start
        proc.started_at = (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat()
        proc._live_applied = {"drift": "loop", "power": -80.0}
        await mgr.relaunch("tx", StartRequest(args=["--freq", "1600", "--elapsed", "20", "--power",
                                                    "-30", "--rf", "on"], replace_args=True,
                                              env_overrides={"X": "1"}))
        assert len(calls) == 1
        req = calls[0]
        a = _argdict(req.args)
        assert abs(float(a["elapsed"]) - 65.0) < 1.0   # 20 carried + ~45 s since the spawn
        assert a["drift"] == "loop" and float(a["power"]) == -80.0   # the live state too
        assert float(a["freq"]) == 1600.0 and a["rf"] == "on"
        assert req.replace_args is True and req.env_overrides == {"X": "1"}

        # Un-tuned + no spawn time known ⇒ the request goes through untouched.
        calls.clear()
        proc._live_applied = {}
        proc.started_at = None
        orig = StartRequest(args=["--freq", "1600"], replace_args=True)
        await mgr.relaunch("tx", orig)
        assert calls == [orig]
    asyncio.run(scenario())


def test_build_resume_request_honours_the_declared_elapsed_param(tmp_path, monkeypatch):
    """A non-`resumable` task whose script declares is_elapsed is resumable by contract: the
    arm-time resume offset is injected via that flag. The operator-configured resumable flag keeps
    its own mechanism; a script with no marker gets an empty request as before."""
    mgr, _ = _mk(tmp_path, monkeypatch, [])
    assert mgr.build_resume_request("tx", 12.0).args == ["-Elapsed", "12"]
    assert mgr.build_resume_request("tx", 0.0).args == []
    mgr2, _ = _mk(tmp_path, monkeypatch, [], resumable=True, resume_offset_flag="--start-offset")
    assert mgr2.build_resume_request("tx", 12.0).args == ["--start-offset", "12.0"]
    mgr3, _ = _mk(tmp_path, monkeypatch, [], script_src=T.LIVE_SCRIPT)
    assert mgr3.build_resume_request("tx", 12.0).args == []


# ── the marker itself: paramkit → argspec (drift-guarded) → cmdargs ─────────────────────────────

def test_is_elapsed_marker_surfaces_through_paramkit_and_the_static_argspec():
    sm = (Script("d")
          .number("--elapsed", unit="s", min=0, default=0.0, is_elapsed=True)
          .integer("--ticks", is_elapsed=False)
          .number("--power", default=-50))
    d = {p["name"]: p for p in sm.describe()["params"]}
    assert d["elapsed"]["is_elapsed"] is True
    assert d["ticks"]["is_elapsed"] is False and d["power"]["is_elapsed"] is False
    spec = extract_params(DRIFT_SCRIPT)
    by = {p["dest"]: p for p in spec["params"]}
    assert by["elapsed"]["is_elapsed"] is True and by["elapsed"]["flags"] == ["-Elapsed", "--elapsed"]
    assert all(by[k]["is_elapsed"] is False for k in by if k != "elapsed")
    assert cmdargs.elapsed_param(spec)["dest"] == "elapsed"
    assert cmdargs.elapsed_param(extract_params(T.LIVE_SCRIPT)) is None
    assert cmdargs.elapsed_param(None) is None


def test_cmdargs_elapsed_helpers():
    ep = {"dest": "elapsed", "flags": ["-Elapsed", "--elapsed"], "is_elapsed": True, "default": 0.0}
    assert cmdargs.elapsed_of_args(["--x", "1"], ep) == 0.0
    assert cmdargs.elapsed_of_args(["--elapsed", "12.5", "-Elapsed", "7"], ep) == 7.0   # last wins
    assert cmdargs.elapsed_of_args(["--elapsed", "junk"], ep) == 0.0
    assert cmdargs.elapsed_of_args(["--elapsed", "-4"], ep) == 0.0
    assert cmdargs.bake_elapsed(["--x", "1"], ep, 30.0) == ["--x", "1", "-Elapsed", "30"]
    assert cmdargs.bake_elapsed(["--elapsed", "5"], ep, 12.3456) == ["--elapsed", "12.346"]
    assert cmdargs.bake_elapsed([], ep, -3) == ["-Elapsed", "0"]
    assert cmdargs.bake_elapsed(["--x", "1"], {"dest": "e", "flags": []}, 3) == ["--x", "1"]
