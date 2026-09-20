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
     .number("-Clock-origin", "--clock-origin", unit="s", min=0.0, default=0.0, is_clock_origin=True)
     .number("--bw", min=1, max=40, default=10, live=True)
     .number("--power", min=-120, max=0, default=-50, live=True)
     .choice("--rf", options=["on", "off"], default="off", live=True, is_rf=True)
     .flag("--restart", live=True, resets_elapsed=True))
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
          "clock_origin": ("-Clock-origin", "--clock-origin"),
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
        got = {k: v for k, v in a.items() if k not in ("elapsed", "clock_origin")}   # (+ what the markers add)
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
        # The operator's hand tunes on the faulted process (spawned by THIS run's launch — the record
        # is merged only for that process): bw (never scheduled) + rf off + power -45 applied BEFORE
        # the schedule's own sets of those dests (so the schedule's position wins for them).
        proc = mgr._procs["tx"]
        proc.started_at = T._iso(T0 + timedelta(seconds=0.5))
        proc._live_applied = {"bw": 33.0, "rf": "off", "power": -45.0}
        proc._live_applied_at = {k: T._iso(T0 + timedelta(seconds=0.7)) for k in ("bw", "rf", "power")}
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


# ── review round: exact numeric text, an integer(...) marker, the --flag=value form ──────────────

INT_SCRIPT = '''\
from paramkit import Script
s = (Script("d")
     .integer("--elapsed", unit="s", min=0, default=0, is_elapsed=True)
     .integer("--seed", min=0, default=1, live=True)
     .number("--power", default=-50, live=True))
'''


def test_overlay_keeps_every_digit_of_a_tuned_value_and_whole_numbers_for_integers():
    """A tuned carrier is relaunched EXACTLY (1602.5625 MHz — a GLONASS channel — not the %g-rounded
    1602.56, 2.5 kHz off); a large whole number never becomes exponent form (an integer param refuses
    it); an integer-kind param gets a whole number even for a float value."""
    spec = extract_params(DRIFT_SCRIPT)
    assert cmdargs.overlay_live_params([], {"freq": 1602.5625}, spec, None) == \
        ["-Start-frequency", "1602.5625"]
    assert cmdargs.overlay_live_params([], {"freq": 1575420123.0}, spec, None) == \
        ["-Start-frequency", "1575420123"]
    ispec = extract_params(INT_SCRIPT)
    assert cmdargs.overlay_live_params([], {"seed": 1234567}, ispec, None) == ["--seed", "1234567"]
    assert cmdargs.overlay_live_params([], {"seed": 12.0}, ispec, None) == ["--seed", "12"]
    Script("x").integer("--seed", min=0, default=1, live=True).parse(["--seed", "1234567"])   # accepted
    assert cmdargs.num_text(0.1) == "0.1" and cmdargs.num_text(60.0) == "60"
    assert cmdargs.num_text(2.5, "integer") == "2" or cmdargs.num_text(2.5, "integer") == "3"
    assert cmdargs.num_text("abc") == "abc"


def test_bake_elapsed_gives_an_integer_marker_a_whole_second_the_parser_accepts():
    ispec = extract_params(INT_SCRIPT)
    ep = cmdargs.elapsed_param(ispec)
    args = cmdargs.bake_elapsed(["--power", "-50"], ep, 45.6)
    assert args == ["--power", "-50", "--elapsed", "46"]
    ns = (Script("d").integer("--elapsed", unit="s", min=0, default=0, is_elapsed=True)
          .number("--power", default=-50, live=True)).parse(args)
    assert ns.elapsed == 46
    # a number(...) marker keeps ms resolution
    ep_f = cmdargs.elapsed_param(extract_params(DRIFT_SCRIPT))
    assert cmdargs.bake_elapsed([], ep_f, 45.6) == ["-Elapsed", "45.6"]


def test_flag_equals_value_form_is_read_and_rewritten():
    ep = {"dest": "elapsed", "flags": ["-Elapsed", "--elapsed"], "kind": "number", "default": 0.0}
    assert cmdargs.elapsed_of_args(["--elapsed=5400", "--x", "1"], ep) == 5400.0
    assert cmdargs.bake_elapsed(["--elapsed=5400", "--x", "1"], ep, 5430.0) == ["--elapsed=5430", "--x", "1"]
    assert cmdargs.set_arg_value(["--p=1", "--p", "2"], ["--p"], 3) == ["--p=3", "--p", "3"]
    assert cmdargs.arg_value(["--p=1", "--p", "2"], ["--p"]) == "2"          # last occurrence wins


# ── the elapsed-RESET trigger (owner workflow: muted pre-roll launch, then `rf on` + `restart` AT on-air) ──

def test_resets_elapsed_marker_surfaces_through_paramkit_argspec_and_cmdargs():
    d = {p["name"]: p for p in (Script("d").flag("--restart", live=True, resets_elapsed=True)
                                .flag("--other", live=True)).describe()["params"]}
    assert d["restart"]["resets_elapsed"] is True and d["other"]["resets_elapsed"] is False
    spec = extract_params(DRIFT_SCRIPT)
    by = {p["dest"]: p for p in spec["params"]}
    assert by["restart"]["resets_elapsed"] is True and by["elapsed"]["resets_elapsed"] is False
    assert cmdargs.resets_elapsed_dests(spec) == {"restart"}
    assert cmdargs.is_reset_fire({"restart": True}, {"restart"})
    assert cmdargs.is_reset_fire({"restart": "on"}, {"restart"})
    assert not cmdargs.is_reset_fire({"restart": False}, {"restart"})
    assert not cmdargs.is_reset_fire({"rf": "on"}, {"restart"})


def _preroll_run(runner, now, *, fault_s=30, launch_elapsed=None, restart_skipped=False):
    """The owner's shape: START 5 s BEFORE on-air with RF muted; AT on-air `rf on` + `restart` (so the
    drift begins at T0); fault `fault_s` after T0."""
    T0 = now - timedelta(seconds=130)
    launch_args = ["--freq", "1600", "--duration", "180", "--power", "-30", "--rf", "off"]
    if launch_elapsed is not None:
        launch_args += ["-Elapsed", str(launch_elapsed)]
    fired_restart = "skipped" if restart_skipped else T._iso(T0)
    steps = [
        T._fire("start", -5.0, T0 - timedelta(seconds=5), fired=T._iso(T0 - timedelta(seconds=5)),
                args=launch_args, replace=True),
        T._fire("tune", 0.0, T0, fired=T._iso(T0), params={"rf": "on"}),
        T._fire("tune", 0.0, T0, fired=fired_restart, params={"restart": True}),
        T._fire("stop", 0.0, now + timedelta(seconds=600), fired="skipped"),
    ]
    return T0, _install(runner, steps, T0=T0, now=now, fault_at=T0 + timedelta(seconds=fault_s),
                        on_air_end=now + timedelta(seconds=600))


def test_restart_counts_the_elapsed_from_the_on_air_restart_trigger_not_the_launch(tmp_path, monkeypatch):
    async def scenario():
        for mode, expect in (("resync", 130.0), ("replay", 30.0)):   # now − T0 / fault − T0, NOT +5 s
            (tmp_path / mode).mkdir()
            mgr, runner = _mk(tmp_path / mode, monkeypatch, ["--freq", "1600", "--power", "-30", "--rf", "off"])
            now = datetime.now(timezone.utc)
            _preroll_run(runner, now)
            out = await runner.restart_run("r1", RestartRequest(mode=mode, restart_at=T._iso(now)))
            a = _argdict(_relaunch_of(out).args)
            assert abs(float(a["elapsed"]) - expect) < 0.01, (mode, a)
            assert a["rf"] == "on" and "--restart" not in _relaunch_of(out).args
    asyncio.run(scenario())


def test_restart_trigger_overrides_a_launch_that_began_part_way(tmp_path, monkeypatch):
    """A launch started 500 s into the drift and then RESTARTED at on-air: the clock began at 0 at the
    trigger — the launch's own --elapsed no longer applies."""
    mgr, runner = _mk(tmp_path, monkeypatch, ["--freq", "1600", "--power", "-30", "--rf", "off"])
    now = datetime.now(timezone.utc)
    _, run = _preroll_run(runner, now, launch_elapsed=500)
    fire = runner._relaunch_start_fire(run, "tx", now, runner._spec_of("tx"), include_skipped=True, elapsed_at=now)
    assert abs(float(_argdict(fire.args)["elapsed"]) - 130.0) < 0.01


def test_a_fault_skipped_restart_trigger_counts_for_resync_only(tmp_path, monkeypatch):
    """The trigger the fault skipped: resync (the schedule's position) counts it — the drift SHOULD have
    restarted at T0; replay (what actually ran) does not — the clock still runs from the launch."""
    mgr, runner = _mk(tmp_path, monkeypatch, ["--freq", "1600", "--power", "-30", "--rf", "off"])
    now = datetime.now(timezone.utc)
    T0, run = _preroll_run(runner, now, fault_s=-2, restart_skipped=True)   # faulted 2 s BEFORE on-air
    spec = runner._spec_of("tx")
    resync = runner._relaunch_start_fire(run, "tx", now, spec, include_skipped=True, elapsed_at=now)
    assert abs(float(_argdict(resync.args)["elapsed"]) - 130.0) < 0.01          # from the (skipped) trigger
    fault_at = T0 - timedelta(seconds=2)
    replay = runner._relaunch_start_fire(run, "tx", now, spec, include_skipped=False, elapsed_at=fault_at)
    assert abs(float(_argdict(replay.args)["elapsed"]) - 3.0) < 0.01            # launch → fault: 5 − 2


def test_standalone_relaunch_counts_from_the_last_applied_restart_trigger(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch, [], auto_restart_on_fault=True, restart_delay_s=0.0)
        proc = mgr._procs["tx"]
        calls = []

        async def fake_start(request=None):
            calls.append(request)
        proc.start = fake_start
        now = datetime.now(timezone.utc)
        proc.started_at = (now - timedelta(seconds=60)).isoformat()
        proc._live_applied = {"rf": "on", "restart": True}
        proc._live_applied_at = {"rf": (now - timedelta(seconds=20)).isoformat(),
                                 "restart": (now - timedelta(seconds=20)).isoformat()}
        await mgr.relaunch("tx", StartRequest(args=["--freq", "1600", "--rf", "off"], replace_args=True))
        a = _argdict(calls[-1].args)
        assert abs(float(a["elapsed"]) - 20.0) < 1.0                     # from the trigger, not the spawn
        assert a["rf"] == "on" and "--restart" not in calls[-1].args     # the trigger is never re-fired
        proc._live_applied, proc._live_applied_at = {"rf": "on"}, {"rf": (now - timedelta(seconds=20)).isoformat()}
        await mgr.relaunch("tx", StartRequest(args=["--freq", "1600", "--rf", "off"], replace_args=True))
        assert abs(float(_argdict(calls[-1].args)["elapsed"]) - 60.0) < 1.0   # no trigger: since the spawn
    asyncio.run(scenario())


# ── the ABSOLUTE clock origin (§14j): exact, whatever the launch latency ────────────────────────

def test_clock_origin_marker_surfaces_and_the_scan_records_the_scripts_report(tmp_path, monkeypatch):
    spec = extract_params(DRIFT_SCRIPT)
    by = {p["dest"]: p for p in spec["params"]}
    assert by["clock_origin"]["is_clock_origin"] is True and by["elapsed"]["is_clock_origin"] is False
    assert cmdargs.clock_origin_param(spec)["dest"] == "clock_origin"
    assert cmdargs.bake_clock_origin(["--x", "1"], cmdargs.clock_origin_param(spec), 1758400000.12345) == \
        ["--x", "1", "-Clock-origin", "1758400000.123"]
    assert pm._last_clock_origin("noise\nCLOCK origin=1758400000.500\nmore\nCLOCK origin=1758400100.250\n") == 1758400100.25
    assert pm._last_clock_origin("no marker here") is None

    async def scenario():
        mgr, _ = _mk(tmp_path, monkeypatch, [])
        proc = mgr._procs["tx"]
        proc.state = pm.ProcessState.RUNNING
        proc._proc = type("P", (), {"returncode": None, "pid": 1})()

        async def read(off, inode):
            return ("HEALTH state=transmitting\nCLOCK origin=1758400000.500\n", 60, 1)
        monkeypatch.setattr(proc.log, "read_since", read)
        await mgr._scan_task_health(proc)
        assert proc.clock_origin == 1758400000.5 and mgr.clock_origin("tx") == 1758400000.5
    asyncio.run(scenario())


def test_run_restart_bakes_the_reported_origin_resync_exact_replay_shifted(tmp_path, monkeypatch):
    """The owner's shape (muted pre-roll, `rf on` + `restart` at T0). The process REPORTED its origin
    when the trigger reset its clock (T0 + a few ms): resync bakes exactly that origin (the launch
    latency of the relaunch no longer matters — the script computes its own elapsed from it);
    replay bakes it shifted by the down-time, like the rest of the profile."""
    mgr, runner = _mk(tmp_path, monkeypatch, ["--freq", "1600", "--power", "-30", "--rf", "off"])
    now = datetime.now(timezone.utc)
    T0, run = _preroll_run(runner, now)                     # launch T0−5, trigger at T0, fault T0+30
    proc = mgr._procs["tx"]
    proc.started_at = T._iso(T0 - timedelta(seconds=4.5))    # this run's process
    proc.clock_origin = (T0 + timedelta(milliseconds=40)).timestamp()   # reported by the script
    spec = runner._spec_of("tx")
    resync = runner._relaunch_start_fire(run, "tx", now, spec, include_skipped=True, elapsed_at=now)
    a = _argdict(resync.args)
    assert abs(float(a["clock_origin"]) - proc.clock_origin) < 0.002          # exact, unshifted
    assert abs(float(a["elapsed"]) - 130.0) < 0.01                             # still baked as a fallback
    fault_at = T0 + timedelta(seconds=30)
    replay = runner._relaunch_start_fire(run, "tx", now, spec, include_skipped=False, elapsed_at=fault_at)
    downtime = (now - fault_at).total_seconds()
    assert abs(float(_argdict(replay.args)["clock_origin"]) - (proc.clock_origin + downtime)) < 0.01


def test_run_restart_origin_falls_back_to_the_schedule(tmp_path, monkeypatch):
    """No report (watchdog off / an older script), a foreign process, or a fault-SKIPPED trigger that
    never fired in the process: the origin is the schedule's — the counted trigger's instant, else
    the launch instant minus the launch's own elapsed."""
    mgr, runner = _mk(tmp_path, monkeypatch, ["--freq", "1600", "--power", "-30", "--rf", "off"])
    now = datetime.now(timezone.utc)
    T0, run = _preroll_run(runner, now)
    spec = runner._spec_of("tx")
    fire = runner._relaunch_start_fire(run, "tx", now, spec, include_skipped=True, elapsed_at=now)
    assert abs(float(_argdict(fire.args)["clock_origin"]) - T0.timestamp()) < 0.002   # the trigger
    # the process reported an origin, but a hand start AFTER the fault owns it → schedule
    proc = mgr._procs["tx"]
    proc.started_at = T._iso(now - timedelta(seconds=1))
    proc.clock_origin = (now - timedelta(seconds=1)).timestamp()
    fire = runner._relaunch_start_fire(run, "tx", now, spec, include_skipped=True, elapsed_at=now)
    assert abs(float(_argdict(fire.args)["clock_origin"]) - T0.timestamp()) < 0.002
    # a fault-SKIPPED trigger (never fired): the process's report is the LAUNCH's clock → resync
    # follows the schedule (the trigger's instant); the launch-only origin = launch − its elapsed
    mgr2, runner2 = _mk(tmp_path / "s", monkeypatch, ["--freq", "1600", "--power", "-30", "--rf", "off"]) \
        if (tmp_path / "s").mkdir() is None else (None, None)
    T0b, run2 = _preroll_run(runner2, now, fault_s=-2, restart_skipped=True, launch_elapsed=500)
    proc2 = mgr2._procs["tx"]
    proc2.started_at = T._iso(T0b - timedelta(seconds=4.5))
    proc2.clock_origin = (T0b - timedelta(seconds=504)).timestamp()      # launch's clock, 500 s in
    spec2 = runner2._spec_of("tx")
    resync = runner2._relaunch_start_fire(run2, "tx", now, spec2, include_skipped=True, elapsed_at=now)
    assert abs(float(_argdict(resync.args)["clock_origin"]) - T0b.timestamp()) < 0.002
    fault_at = T0b - timedelta(seconds=2)
    replay = runner2._relaunch_start_fire(run2, "tx", now, spec2, include_skipped=False, elapsed_at=fault_at)
    downtime = (now - fault_at).total_seconds()
    assert abs(float(_argdict(replay.args)["clock_origin"]) - (proc2.clock_origin + downtime)) < 0.01


def test_standalone_relaunch_bakes_the_reported_origin_else_a_reconstruction(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch, [], auto_restart_on_fault=True, restart_delay_s=0.0)
        proc = mgr._procs["tx"]
        calls = []

        async def fake_start(request=None):
            calls.append(request)
        proc.start = fake_start
        now = datetime.now(timezone.utc)
        proc.started_at = (now - timedelta(seconds=60)).isoformat()
        proc.clock_origin = (now - timedelta(seconds=57.3)).timestamp()   # the script's own report
        req = StartRequest(args=["--freq", "1600", "--rf", "on"], replace_args=True)
        await mgr.relaunch("tx", req)
        a = _argdict(calls[-1].args)
        assert abs(float(a["clock_origin"]) - proc.clock_origin) < 0.002
        # no report: the spawn minus the launch's own elapsed
        proc.clock_origin = None
        await mgr.relaunch("tx", StartRequest(args=["--freq", "1600", "--elapsed", "100"], replace_args=True))
        a = _argdict(calls[-1].args)
        assert abs(float(a["clock_origin"]) - (now - timedelta(seconds=160)).timestamp()) < 1.5
        # no report, a reset trigger applied 20 s ago: the trigger's instant
        proc._live_applied = {"restart": True}
        proc._live_applied_at = {"restart": (now - timedelta(seconds=20)).isoformat()}
        await mgr.relaunch("tx", req)
        assert abs(float(_argdict(calls[-1].args)["clock_origin"]) - (now - timedelta(seconds=20)).timestamp()) < 1.5
    asyncio.run(scenario())
