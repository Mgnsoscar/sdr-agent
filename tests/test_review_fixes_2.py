"""Second adversarial review of the RF-fault arc (docs/rf-fault-recovery.md §14h): one regression per
confirmed finding, plus the mechanisms the tests critic found unpinned. Real subprocesses where the
finding was reproduced over one; constructed runs / fakes elsewhere (the fakes from test_review_fixes).
"""
import asyncio
import os
import signal as _signal
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import test_sequence_auto_restart as T          # LIVE_SCRIPT / _mk / _fire / _iso / _faulted_auto_run
import test_restart_all_params as R             # DRIFT_SCRIPT / _mk / _install / _argdict
from test_review_fixes import _FakeProc, _stub_start, _relaunchable, PEER
from agent import cmdargs
from agent import config as agentcfg
from agent import process_manager as pm
from agent.models import (ProcessState, RestartRequest, SequenceRun, SequenceState, StartRequest,
                          StepFire, TaskConfig, TaskHealth, CreateSequenceRequest, SequenceStep,
                          StepAction, ArmSequenceRequest)
from agent.process_manager import ProcessManager
from agent.sequence_runner import SequenceRunner, _RestartDeferred

REPO_ROOT = str(Path(__file__).resolve().parents[1])
_now = lambda: datetime.now(timezone.utc)


def _mgr_with(tmp_path, monkeypatch, script_src, name="tx", **cfg):
    monkeypatch.setattr(pm._agentcfg, "CTRL_DIR", tmp_path / "ctl")
    script = tmp_path / f"{name}.py"
    script.write_text(script_src)
    tasks = {name: TaskConfig(name=name, command=["python3", str(script)], working_dir=str(tmp_path),
                              env={"PYTHONPATH": REPO_ROOT}, **cfg)}
    mgr = ProcessManager(tasks, tmp_path, "unit-a")
    runner = SequenceRunner(mgr, "unit-a", tmp_path / "seq.json", tmp_path / "runs.json", tmp_path)
    return mgr, runner


# ── C1 (HIGH): a slot whose process is still stopping takes no second launch ────────────────────

def test_start_over_a_stopping_slot_is_refused_and_restart_waits_it_out(tmp_path, monkeypatch):
    async def scenario():
        mgr, _ = _mgr_with(tmp_path, monkeypatch, PEER)          # ignores SIGTERM: a 1 s SIGKILL grace
        proc = mgr._procs["tx"]
        await mgr.start("tx")
        pid1 = proc.pid
        await asyncio.sleep(0.6)                                  # let the script install SIG_IGN
        stopper = asyncio.create_task(proc.stop(timeout=1.0))
        await asyncio.sleep(0.2)
        assert proc.state == ProcessState.STOPPING and mgr.is_process_alive("tx")
        with pytest.raises(RuntimeError, match="still stopping"):
            await proc.start()                                    # no second process on the channel
        await stopper
        assert proc.state == ProcessState.STOPPED and not mgr.is_process_alive("tx")
        # restart() while a stop is in flight WAITS for it, then launches exactly one process
        await mgr.start("tx")
        pid2 = proc.pid
        await asyncio.sleep(0.6)
        stopper = asyncio.create_task(proc.stop(timeout=1.0))
        await asyncio.sleep(0.2)
        st = await mgr.restart("tx")
        assert st.state == ProcessState.RUNNING and proc.pid not in (pid1, pid2)
        await stopper
        try:
            os.kill(pid2, 0)
            alive2 = True
        except ProcessLookupError:
            alive2 = False
        assert not alive2                                         # the old one really died
        await mgr.shutdown()
    asyncio.run(scenario())


# ── C4: a launch that fails inside its STARTING window settles the slot ─────────────────────────

def test_failed_launch_settles_the_slot_and_a_stop_does_not_hang(tmp_path, monkeypatch):
    async def scenario():
        mgr, _ = _mgr_with(tmp_path, monkeypatch, T.LIVE_SCRIPT)
        proc = mgr._procs["tx"]
        proc.config.working_dir = str(tmp_path / "missing")       # spawn raises FileNotFoundError
        with pytest.raises(FileNotFoundError):
            await proc.start()
        assert proc.state == ProcessState.STOPPED and proc._spawned.is_set()
        t0 = asyncio.get_event_loop().time()
        await proc.stop()                                         # instant, no 30 s wait
        assert asyncio.get_event_loop().time() - t0 < 1.0
        with pytest.raises(FileNotFoundError):                    # startable again (not "already starting")
            await proc.start()
    asyncio.run(scenario())


# ── C3/O1: a stop / PANIC landing while a launch is parked abandons it ──────────────────────────

def test_stop_or_panic_during_a_parked_launch_abandons_it(tmp_path, monkeypatch):
    async def scenario():
        mgr, _ = _mgr_with(tmp_path, monkeypatch, T.LIVE_SCRIPT)
        mgr.device_free.clear()                                   # the boot pre-image holds the SDR
        t = asyncio.create_task(mgr.start("tx"))
        await asyncio.sleep(0.1)
        await mgr.stop("tx")                                      # nothing runs yet: latches the intent
        mgr.device_free.set()
        with pytest.raises(RuntimeError, match="abandoned"):
            await t
        assert not mgr.is_running("tx") and not mgr.is_process_alive("tx")
        # PANIC (cancel_pending_relaunches bumps the epoch) during the park: same
        mgr.device_free.clear()
        t = asyncio.create_task(mgr.start("tx"))
        await asyncio.sleep(0.1)
        mgr.cancel_pending_relaunches()
        mgr.device_free.set()
        with pytest.raises(RuntimeError, match="abandoned"):
            await t
        assert not mgr.is_running("tx")
        # a FRESH launch afterwards supersedes the old intent
        await mgr.start("tx")
        assert mgr.is_running("tx")
        await mgr.shutdown()
    asyncio.run(scenario())


# ── G8: run_oneshot waits on the device gate too ────────────────────────────────────────────────

def test_run_oneshot_waits_on_the_device_gate(tmp_path, monkeypatch):
    async def scenario():
        mgr, _ = _mgr_with(tmp_path, monkeypatch, "print('one-shot')\n")
        mgr.device_free.clear()
        t = asyncio.create_task(mgr.run_oneshot("tx", [], run_id="r"))
        await asyncio.sleep(0.3)
        assert not t.done()
        mgr.device_free.set()
        await asyncio.wait_for(t, 10)
    asyncio.run(scenario())


# ── C2: a STOP precedes a co-timed START of the same task; the successor run really launches ────

def test_stop_sorts_before_a_co_timed_start_of_the_same_task(tmp_path, monkeypatch):
    monkeypatch.setattr(pm._agentcfg, "CTRL_DIR", tmp_path / "ctl")
    script = tmp_path / "tx.py"
    script.write_text(T.LIVE_SCRIPT)
    tasks = {"tx": TaskConfig(name="tx", command=["python3", str(script)], working_dir=str(tmp_path),
                              env={"PYTHONPATH": REPO_ROOT})}
    mgr = ProcessManager(tasks, tmp_path, "unit-a")
    runner = SequenceRunner(mgr, "unit-a", tmp_path / "seq.json", tmp_path / "runs.json", tmp_path)

    async def scenario():
        await mgr.startup()
        await runner.startup()
        try:
            def mk(power):
                return [SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx",
                                     args=["--power", power, "--rf", "on"], replace_args=True),
                        SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx")]
            s1 = await runner.create_sequence(CreateSequenceRequest(name="one", steps=mk("-90")))
            s2 = await runner.create_sequence(CreateSequenceRequest(name="two", steps=mk("-40")))
            now = _now()
            T1, E1 = now + timedelta(seconds=1.5), now + timedelta(seconds=4.0)
            r1 = await runner.arm(s1.id, ArmSequenceRequest(on_air_at=T._iso(T1), on_air_end=T._iso(E1)), T._iso(E1))
            E2 = E1 + timedelta(seconds=4.0)
            r2 = await runner.arm(s2.id, ArmSequenceRequest(on_air_at=T._iso(E1), on_air_end=T._iso(E2)), T._iso(E2))
            await asyncio.sleep(6.0)
            assert runner.get_run(r1.id).state == SequenceState.COMPLETED
            assert runner.get_run(r2.id).state == SequenceState.RUNNING
            assert mgr.is_running("tx")
            assert (await mgr.get_params("tx"))["current"]["power"] == -40   # run 2's launch, on air
        finally:
            await runner.shutdown()
            await mgr.shutdown()
    asyncio.run(scenario())


# ── C5: a launch completing into HOLDING is kept ───────────────────────────────────────────────

def test_launch_completing_into_holding_is_not_stopped(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = T._mk(tmp_path, monkeypatch)
        now = _now()
        T0 = now - timedelta(seconds=1)
        start = T._fire("start", 0.0, T0, fired=None, args=["--power", "-50", "--rf", "on"], replace=True)
        run = SequenceRun(id="h", sequence_id="s", sequence_name="hold", state=SequenceState.RUNNING,
                          on_air_at=T._iso(T0), on_air_end=None, open_ended=True, hold_aware=True,
                          hold_at_offset_s=100.0, steps=[start])
        runner._runs["h"] = run
        gate, entered = asyncio.Event(), asyncio.Event()
        orig = mgr._gate_precommand

        async def slow_gate(name, **kw):
            entered.set()
            await gate.wait()
            return await orig(name, **kw)
        monkeypatch.setattr(mgr, "_gate_precommand", slow_gate)
        fire = asyncio.create_task(runner._fire_step(run, start))
        await entered.wait()
        async with runner._lock:
            run.state = SequenceState.HOLDING              # hold_now landed mid-launch
        gate.set()
        await fire
        await asyncio.sleep(0.3)
        try:
            assert mgr.is_running("tx")                    # kept: the Hold freezes it, RF stays
        finally:
            await mgr.shutdown()
    asyncio.run(scenario())


# ── G2: _fire_step on a dead run stamps nothing and starts nothing ──────────────────────────────

def test_fire_step_on_an_aborted_run_does_nothing(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = T._mk(tmp_path, monkeypatch)
        calls = []

        async def fake_start(name, req=None, source="manual"):
            calls.append(name)
        monkeypatch.setattr(mgr, "start", fake_start)
        now = _now()
        start = T._fire("start", 0.0, now, fired=None, args=["--power", "-50"], replace=True)
        run = SequenceRun(id="a", sequence_id="s", sequence_name="x", state=SequenceState.ABORTED,
                          on_air_at=T._iso(now), steps=[start])
        runner._runs["a"] = run
        await runner._fire_step(run, start)
        assert start.fired_actual is None and calls == []
    asyncio.run(scenario())


# ── W1 (HIGH): a resync inside a scheduled OFF gap does not relaunch ────────────────────────────

def _two_epoch_run(runner, now):
    T0 = now - timedelta(seconds=90)
    steps = [
        T._fire("start", 0.0, T0, fired=T._iso(T0), args=["--power", "-60", "--rf", "on"], replace=True),
        T._fire("stop", 60.0, T0 + timedelta(seconds=60), fired="skipped"),             # epoch 1 ends (gap)
        T._fire("start", 120.0, T0 + timedelta(seconds=120), fired="skipped",
                args=["--power", "-90", "--rf", "on"], replace=True),                  # epoch 2
        T._fire("stop", 180.0, T0 + timedelta(seconds=180), fired="skipped"),
    ]
    run = SequenceRun(id="e", sequence_id="s", sequence_name="two-epochs", state=SequenceState.RUNNING,
                      on_air_at=T._iso(T0), on_air_end=T._iso(T0 + timedelta(seconds=180)), steps=steps,
                      fault="tx: halt", fault_task="tx", fault_at=T._iso(T0 + timedelta(seconds=50)))
    runner._runs["e"] = run
    return run


def test_resync_in_a_scheduled_off_gap_inserts_no_relaunch(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = T._mk(tmp_path, monkeypatch)
        now = _now()
        run = _two_epoch_run(runner, now)
        out = await runner.restart_run("e", RestartRequest(mode="resync", restart_at=T._iso(now)))
        assert out.fault == ""
        assert not [s for s in out.steps if s.action == "start" and s.fired_actual is None
                    and abs((datetime.fromisoformat(s.fire_at) - now).total_seconds()) < 1]
        assert not mgr.is_running("tx")                       # silent through the gap
        ep2 = [s for s in out.steps if s.action == "start" and s.args[1] == "-90"][0]
        assert ep2.fired_actual is None                       # epoch 2 re-instated on its schedule
        # replay resumes from the crash point (10 s of epoch 1 were left): a relaunch IS inserted
        mgr2, runner2 = T._mk(tmp_path / "b", monkeypatch) if False else (None, None)
    asyncio.run(scenario())


def test_replay_in_the_gap_still_resumes_epoch_one(tmp_path, monkeypatch):
    mgr, runner = T._mk(tmp_path, monkeypatch)
    now = _now()
    run = _two_epoch_run(runner, now)
    fire = runner._relaunch_start_fire(run, "tx", now, runner._spec_of("tx"), include_skipped=False,
                                       elapsed_at=now)
    assert fire is not None and R._argdict(fire.args)["power"] == "-60"


# ── W2: a tune the run no longer owns is dropped, never deferred into the successor ─────────────

def test_stale_tune_is_dropped_not_fired_or_deferred(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = T._mk(tmp_path, monkeypatch)
        tuned = []

        async def fake_set(name, values, wait=1.0):
            tuned.append(values)
            return {}
        monkeypatch.setattr(mgr, "set_params", fake_set)
        now = _now()
        r1 = SequenceRun(id="r1", sequence_id="s", sequence_name="one", state=SequenceState.RUNNING,
                         on_air_at=T._iso(now - timedelta(seconds=10)), steps=[
            T._fire("start", 0.0, now - timedelta(seconds=10), fired=T._iso(now - timedelta(seconds=10))),
            T._fire("tune", 8.0, now - timedelta(seconds=2), fired=None, params={"rf": "off"}),  # deferred
            T._fire("stop", 9.0, now - timedelta(seconds=1), fired=T._iso(now - timedelta(seconds=1)))])
        r2 = SequenceRun(id="r2", sequence_id="s", sequence_name="two", state=SequenceState.RUNNING,
                         on_air_at=T._iso(now), steps=[
            T._fire("start", 0.0, now - timedelta(seconds=0.5), fired=T._iso(now - timedelta(seconds=0.5))),
            T._fire("stop", 0.0, now + timedelta(seconds=60), fired=None)])
        runner._runs.update({"r1": r1, "r2": r2})
        tune = r1.steps[1]
        assert runner._tune_target_stale(r1, tune)            # stopped by r1 AND launched later by r2
        await runner._fire_step(r1, tune)
        assert tune.fired_actual == "skipped:stale" and tuned == []
        assert not runner._counts_at_cutoff(tune, now + timedelta(seconds=5), True)
        # the plain case: launched, not stopped, no successor → not stale
        r3 = SequenceRun(id="r3", sequence_id="s", sequence_name="three", state=SequenceState.RUNNING,
                         on_air_at=T._iso(now), steps=[
            T._fire("start", 0.0, now - timedelta(seconds=5), fired=T._iso(now - timedelta(seconds=5)), task="peer"),
            T._fire("tune", 1.0, now, fired=None, params={"rf": "off"}, task="peer")])
        runner._runs.clear(); runner._runs["r3"] = r3
        assert not runner._tune_target_stale(r3, r3.steps[1])
    asyncio.run(scenario())


# ── W4: a failed START couples an RF fault (run) / alarms (standalone) ─────────────────────────

def test_failed_start_couples_an_rf_fault_into_the_run(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = T._mk(tmp_path, monkeypatch)
        events = []

        async def fake_fire(run, kind, detail=""):
            events.append(kind)
        monkeypatch.setattr(runner, "_fire", fake_fire)

        async def bad_start(name, req=None, source="manual"):
            raise RuntimeError("Refusing to start 'tx': calibration error")
        monkeypatch.setattr(mgr, "start", bad_start)
        now = _now()
        start = T._fire("start", 0.0, now, fired=None, args=["--power", "-50"], replace=True)
        tune = T._fire("tune", 5.0, now + timedelta(seconds=5), fired=None, params={"power": -40})
        run = SequenceRun(id="f", sequence_id="s", sequence_name="x", state=SequenceState.RUNNING,
                          on_air_at=T._iso(now), on_air_end=T._iso(now + timedelta(seconds=60)),
                          steps=[start, tune, T._fire("stop", 0.0, now + timedelta(seconds=60), fired=None)])
        runner._runs["f"] = run
        await runner._fire_step(run, start)
        assert run.fault.startswith("launch failed") and run.fault_task == "tx"
        assert tune.fired_actual == "skipped" and "sequence_rf_fault" in events
    asyncio.run(scenario())


def test_failed_standalone_relaunch_raises_the_alarm_again(tmp_path):
    async def scenario():
        mgr, proc = _relaunchable(tmp_path, delay=0.0)
        fired = []

        async def fake_event(detail, snapshot):
            fired.append(detail)
        proc._fire_health_event = fake_event

        async def bad_hook(name, req):
            raise RuntimeError("boom")
        proc._launch_hook = bad_hook
        proc.state = ProcessState.CRASHED
        proc._proc = _FakeProc(code=1)
        await proc._maybe_auto_restart_standalone()
        assert proc.health == TaskHealth.RF_FAULT.value and fired and "relaunch failed" in fired[0]
    asyncio.run(scenario())


# ── W5: a second fault in a faulted run is coupled and released to its own checkbox ────────────

def test_second_task_fault_is_coupled_and_not_claimed(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = T._mk(tmp_path, monkeypatch)
        now = _now()
        T0 = now - timedelta(seconds=20)
        steps = [
            T._fire("start", 0.0, T0, fired=T._iso(T0)),
            T._fire("start", 0.0, T0, fired=T._iso(T0), task="peer"),
            T._fire("tune", 30.0, now + timedelta(seconds=10), fired="skipped", params={"power": -40}),
            T._fire("tune", 30.0, now + timedelta(seconds=10), fired=None, params={"power": -40}, task="peer"),
            T._fire("stop", 0.0, now + timedelta(seconds=60), fired="skipped"),
            T._fire("stop", 0.0, now + timedelta(seconds=60), fired=None, task="peer"),
        ]
        run = SequenceRun(id="m", sequence_id="s", sequence_name="multi", state=SequenceState.RUNNING,
                          on_air_at=T._iso(T0), on_air_end=T._iso(now + timedelta(seconds=60)), steps=steps,
                          fault="tx: halt", fault_task="tx", fault_at=T._iso(now - timedelta(seconds=3)))
        runner._runs["m"] = run
        await runner.on_task_fault("peer", "vmcircbuf")
        assert run.fault_task == "tx" and "peer: vmcircbuf" in run.fault
        assert all(s.fired_actual == "skipped" for s in steps if s.task_name == "peer" and s.action != "start")
        claimed = runner.tasks_claimed_by_active_runs()
        assert "tx" in claimed and "peer" not in claimed        # peer's own auto-restart may act
    asyncio.run(scenario())


# ── W6: a never-clearing deferral trips loudly after AUTO_RESTART_DEFER_TICKS ──────────────────

def test_restart_deferral_is_bounded_and_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(agentcfg, "AUTO_RESTART_ENABLED", True)
    monkeypatch.setattr(agentcfg, "AUTO_RESTART_DEFER_TICKS", 3)

    async def scenario():
        mgr, runner = T._mk(tmp_path, monkeypatch)
        now = _now()
        run = T._faulted_auto_run(runner, now)
        run.steps[2].params = {"bw": 25}                     # a non-fallback dest was tuned…
        monkeypatch.setattr(runner, "_spec_of", lambda task: None)   # …and the schema is unreadable
        tripped = []

        async def fake_gaveup(r, reason):
            tripped.append(reason)
        monkeypatch.setattr(runner, "_auto_restart_gaveup", fake_gaveup)
        for _ in range(2):
            await runner._service_auto_restart(_now())
        assert tripped == [] and runner._auto_deferred["ar"] == 2
        await runner._service_auto_restart(_now())
        assert len(tripped) == 1 and "deferred 3 ticks" in tripped[0]
        await runner._service_auto_restart(_now())
        assert len(tripped) == 1                              # trips once
    asyncio.run(scenario())


# ── W7: a faulted run whose window is over completes and releases its claim ────────────────────

def test_faulted_run_past_its_window_completes_and_unclaims(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = T._mk(tmp_path, monkeypatch)
        now = _now()
        T0 = now - timedelta(seconds=7200)
        steps = [T._fire("start", 0.0, T0, fired=T._iso(T0)),
                 T._fire("stop", 0.0, T0 + timedelta(seconds=600), fired="skipped")]
        run = SequenceRun(id="old", sequence_id="s", sequence_name="old", state=SequenceState.RUNNING,
                          on_air_at=T._iso(T0), on_air_end=T._iso(T0 + timedelta(seconds=600)), steps=steps,
                          fault="tx: halt", fault_task="tx", fault_at=T._iso(T0 + timedelta(seconds=300)))
        runner._runs["old"] = run
        assert "tx" not in runner.tasks_claimed_by_active_runs()
        await runner._tick()
        assert run.state == SequenceState.COMPLETED
        # a LIVE faulted run (window ahead) keeps its claim + stays RUNNING
        live = T._faulted_auto_run(runner, _now(), rid="live", policy="manual")
        assert "tx" in runner.tasks_claimed_by_active_runs()
        await runner._tick()
        assert live.state == SequenceState.RUNNING
    asyncio.run(scenario())


# ── W8: hold_now refuses a faulted run ─────────────────────────────────────────────────────────

def test_hold_now_refuses_a_faulted_run(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = T._mk(tmp_path, monkeypatch)
        run = T._faulted_auto_run(runner, _now(), policy="manual")
        run.hold_aware, run.hold_at_offset_s = True, 300.0
        with pytest.raises(ValueError, match="RF fault"):
            await runner.hold_now(run.id)
        assert run.state == SequenceState.RUNNING
    asyncio.run(scenario())


# ── W9/W10/O2: the marker cache, the stale stamp, the disabled watchdog ────────────────────────

def test_expects_tx_marker_never_memoises_a_miss_and_reload_clears_it(tmp_path, monkeypatch):
    async def scenario():
        mgr, _ = _mgr_with(tmp_path, monkeypatch, "print('x')\n")
        script = tmp_path / "tx.py"
        script.unlink()
        assert mgr.expects_tx_marker("tx") is False               # unreadable now
        script.write_text("from paramkit.txhealth import watch_flowgraph\n")
        assert mgr.expects_tx_marker("tx") is True                # not memoised as a miss
        script.write_text("print('no marker')\n")
        assert mgr.expects_tx_marker("tx") is True                # cached hit…
        await mgr.reload({"tx": mgr.get_config("tx")})
        assert mgr.expects_tx_marker("tx") is False               # …dropped by a re-deploy
    asyncio.run(scenario())


def test_stale_read_does_not_stamp_the_replacement_process(tmp_path, monkeypatch):
    async def scenario():
        mgr, _ = _mgr_with(tmp_path, monkeypatch, "print('x')\n")
        proc = mgr._procs["tx"]
        proc.state = ProcessState.RUNNING
        proc._proc = _FakeProc(alive=True)

        async def swapping_read(off, inode):
            proc._proc = _FakeProc(alive=True)                    # the process was replaced mid-read
            return ("HEALTH state=transmitting\n", 30, 1)
        monkeypatch.setattr(proc.log, "read_since", swapping_read)
        await mgr._scan_task_health(proc)
        assert proc.transmitting_at is None
    asyncio.run(scenario())


def test_disabled_watchdog_keeps_the_settle_on_running_and_ok(tmp_path, monkeypatch):
    mgr, _ = _mgr_with(tmp_path, monkeypatch, "from paramkit.txhealth import watch_flowgraph\n")
    monkeypatch.setattr(agentcfg, "HEALTH_POLL_S", 0.0)
    assert mgr.expects_tx_marker("tx") is True
    assert mgr.task_transmitting_confirmed("tx") is True         # nothing reads the marker
    monkeypatch.setattr(agentcfg, "HEALTH_POLL_S", 2.0)
    assert mgr.task_transmitting_confirmed("tx") is False


# ── O3/G14: the pref file is matched byte-exact, written atomically, and left alone when equal ─

def test_pref_file_exact_bytes_and_idempotent(tmp_path):
    home = tmp_path / "home"
    d = home / ".config" / "gnuradio" / "prefs"
    d.mkdir(parents=True)
    f = d / agentcfg.GR_VMCIRCBUF_PREF_KEY
    f.write_text("gr::vmcircbuf_mmap_shm_open_factory\n")           # a hand-seeded trailing newline
    name = pm._pin_gr_vmcircbuf_pref(str(home), "mmap_shm_open")
    assert f.read_bytes() == name.encode()                          # exact bytes GR 3.10 matches
    assert not list(d.glob("*.tmp"))
    m0 = f.stat().st_mtime_ns
    pm._pin_gr_vmcircbuf_pref(str(home), "mmap_shm_open")
    assert f.stat().st_mtime_ns == m0                               # untouched when equal


# ── R1/R2: the hand-tune merge is time-ordered and process-scoped ──────────────────────────────

def _r_run(runner, now):
    T0 = now - timedelta(seconds=30)
    steps = [
        R.T._fire("start", 0.0, T0, fired=R.T._iso(T0), args=["--power", "-50", "--rf", "off"], replace=True),
        R.T._fire("tune", 1.0, T0 + timedelta(seconds=1), fired=R.T._iso(T0 + timedelta(seconds=1)), params={"rf": "on"}),
        R.T._fire("tune", 5.0, T0 + timedelta(seconds=5), fired=R.T._iso(T0 + timedelta(seconds=5)), params={"power": -40}),
        R.T._fire("stop", 0.0, now + timedelta(seconds=60), fired="skipped"),
    ]
    return R._install(runner, steps, T0=T0, now=now, fault_at=now - timedelta(seconds=2))


def test_hand_tune_after_the_schedules_last_set_wins_before_it_the_schedule_wins(tmp_path, monkeypatch):
    mgr, runner = R._mk(tmp_path, monkeypatch, ["--power", "-50", "--rf", "off"])
    now = _now()
    run = _r_run(runner, now)
    proc = mgr._procs["tx"]
    proc.started_at = R.T._iso(now - timedelta(seconds=29))        # spawned by this run's launch
    proc._live_applied = {"power": -70.0, "rf": "off"}
    proc._live_applied_at = {"power": R.T._iso(now - timedelta(seconds=10)),   # after the −40 tune
                             "rf": R.T._iso(now - timedelta(seconds=8))}       # after the rf-on tune
    a = R._argdict(runner._relaunch_start_fire(run, "tx", now, runner._spec_of("tx"),
                                               include_skipped=False, elapsed_at=now).args)
    assert float(a["power"]) == -70.0 and a["rf"] == "off"        # the operator's later mute + level
    proc._live_applied_at = {"power": R.T._iso(now - timedelta(seconds=29.9)),  # BEFORE the schedule's sets
                             "rf": R.T._iso(now - timedelta(seconds=29.9))}
    a = R._argdict(runner._relaunch_start_fire(run, "tx", now, runner._spec_of("tx"),
                                               include_skipped=False, elapsed_at=now).args)
    assert float(a["power"]) == -40.0 and a["rf"] == "on"         # the schedule's position stands


def test_live_record_of_another_process_is_never_merged(tmp_path, monkeypatch):
    mgr, runner = R._mk(tmp_path, monkeypatch, ["--power", "-50", "--rf", "off"])
    now = _now()
    run = _r_run(runner, now)
    proc = mgr._procs["tx"]
    proc._live_applied = {"power": -30.0}
    proc._live_applied_at = {"power": R.T._iso(now - timedelta(seconds=1))}
    proc.started_at = R.T._iso(now - timedelta(seconds=1))         # a hand start AFTER the fault
    a = R._argdict(runner._relaunch_start_fire(run, "tx", now, runner._spec_of("tx"),
                                               include_skipped=False, elapsed_at=now).args)
    assert float(a["power"]) == -40.0                             # the run's own crash state
    proc.started_at = R.T._iso(now - timedelta(seconds=29))        # ours → merged
    a = R._argdict(runner._relaunch_start_fire(run, "tx", now, runner._spec_of("tx"),
                                               include_skipped=False, elapsed_at=now).args)
    assert float(a["power"]) == -30.0


# ── R3: an arm-time resume injection survives the restart ──────────────────────────────────────

def test_resume_injection_is_carried_by_the_reconstruction(tmp_path, monkeypatch):
    # (a) the script-declared marker
    mgr, runner = R._mk(tmp_path, monkeypatch, ["--freq", "1600", "--power", "-30", "--rf", "on"])
    now = _now()
    T0 = now - timedelta(seconds=100)
    launch = R.T._fire("start", 0.0, T0, fired=R.T._iso(T0), args=["--freq", "1600", "--rf", "on"], replace=True)
    launch.resume_offset_s = 600.0
    run = R._install(runner, [launch, R.T._fire("stop", 0.0, now + timedelta(seconds=60), fired="skipped")],
                     T0=T0, now=now, fault_at=now - timedelta(seconds=30))
    fire = runner._relaunch_start_fire(run, "tx", now, runner._spec_of("tx"), include_skipped=True, elapsed_at=now)
    assert abs(float(R._argdict(fire.args)["elapsed"]) - 700.0) < 0.01 and fire.resume_offset_s is None
    # (b) the operator-configured arg mode
    (tmp_path / "b").mkdir(); (tmp_path / "c").mkdir()
    mgr2, runner2 = R._mk(tmp_path / "b", monkeypatch, ["--power", "-30"], script_src=R.T.LIVE_SCRIPT,
                          resumable=True, resume_offset_flag="--start-offset")
    launch2 = R.T._fire("start", 0.0, T0, fired=R.T._iso(T0), args=["--power", "-30"], replace=True)
    launch2.resume_offset_s = 600.0
    run2 = R._install(runner2, [launch2, R.T._fire("stop", 0.0, now + timedelta(seconds=60), fired="skipped")],
                      T0=T0, now=now, fault_at=now - timedelta(seconds=30))
    fire2 = runner2._relaunch_start_fire(run2, "tx", now, runner2._spec_of("tx"), include_skipped=True, elapsed_at=now)
    assert abs(float(cmdargs.arg_value(fire2.args, ["--start-offset"])) - 700.0) < 0.01
    # (c) env mode rides the synthetic fire's resume_offset_s (re-injected by _fire_step)
    mgr3, runner3 = R._mk(tmp_path / "c", monkeypatch, ["--power", "-30"], script_src=R.T.LIVE_SCRIPT,
                          resumable=True, resume_offset_mode="env")
    launch3 = R.T._fire("start", 0.0, T0, fired=R.T._iso(T0), args=["--power", "-30"], replace=True)
    launch3.resume_offset_s = 600.0
    run3 = R._install(runner3, [launch3, R.T._fire("stop", 0.0, now + timedelta(seconds=60), fired="skipped")],
                      T0=T0, now=now, fault_at=now - timedelta(seconds=30))
    fire3 = runner3._relaunch_start_fire(run3, "tx", now, runner3._spec_of("tx"), include_skipped=True, elapsed_at=now)
    assert abs(fire3.resume_offset_s - 700.0) < 0.01


# ── E4 / R6: injection-only replace launches append; a dangling flag gets its value ────────────

def test_injection_only_start_keeps_the_configured_command(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = R._mk(tmp_path, monkeypatch, ["--freq", "1600", "--power", "-30"])
        seen = []

        async def fake_start(name, req=None, source="manual"):
            seen.append(req)
        monkeypatch.setattr(mgr, "start", fake_start)
        now = _now()
        step = R.T._fire("start", 0.0, now, fired=None, args=[], replace=True)
        step.resume_offset_s = 12.0
        run = SequenceRun(id="i", sequence_id="s", sequence_name="x", state=SequenceState.RUNNING,
                          on_air_at=R.T._iso(now), steps=[step])
        runner._runs["i"] = run
        await runner._fire_step(run, step)
        assert seen and seen[0].args == ["-Elapsed", "12"] and seen[0].replace_args is False
    asyncio.run(scenario())
    assert cmdargs.set_arg_value(["--rf", "on", "--power"], ["--power"], "-50") == ["--rf", "on", "--power", "-50"]


# ── G3 / G7 / G4 / G6 / G10 / G13: mechanisms the tests critic found unpinned ─────────────────

def test_real_start_clears_the_live_record(tmp_path, monkeypatch):
    async def scenario():
        mgr, _ = _mgr_with(tmp_path, monkeypatch, T.LIVE_SCRIPT)
        proc = mgr._procs["tx"]
        proc._live_applied, proc._live_applied_at = {"power": -100.0}, {"power": "x"}
        await proc.start()
        try:
            assert proc._live_applied == {} and proc._live_applied_at == {}
        finally:
            await mgr.shutdown()
    asyncio.run(scenario())


def test_operator_stop_cancels_a_pending_relaunch_but_the_auto_drop_does_not(tmp_path):
    """#11: an OPERATOR stop cancels the detached relaunch (no relaunch); the internal auto-drop
    (operator=False) must NOT cancel the task's own recovery — the relaunch still happens."""
    async def scenario():
        for operator, expect_cancelled, expect_calls in ((True, True, 0), (False, False, 1)):
            (tmp_path / str(operator)).mkdir(exist_ok=True)
            mgr, proc = _relaunchable(tmp_path / str(operator), delay=1.0)
            calls = _stub_start(proc)
            proc.state = ProcessState.STOPPED
            proc._proc = _FakeProc(code=1)
            proc._relaunch_task = asyncio.create_task(proc._maybe_auto_restart_standalone())
            await asyncio.sleep(0.1)
            await proc.stop(operator=operator)
            await asyncio.sleep(1.3)
            assert proc._relaunch_task.cancelled() is expect_cancelled
            assert len(calls) == expect_calls
    asyncio.run(scenario())


def test_shutdown_flag_alone_stands_a_crash_restart_down(tmp_path, monkeypatch):
    async def scenario():
        mgr, _ = _mgr_with(tmp_path, monkeypatch, "import sys; sys.exit(1)\n",
                           restart_on_crash=True, restart_delay_s=0.8, max_restarts=5)
        proc = mgr._procs["tx"]
        await mgr.startup()
        await mgr.start("tx")
        await asyncio.sleep(0.4)                                  # exited; the watcher sleeps its delay
        mgr._shutdown_flag.set()                                  # the flag ALONE, no cancel
        await asyncio.sleep(1.2)
        assert proc.restart_count == 0 and not mgr.is_running("tx")
        await mgr.shutdown()
    asyncio.run(scenario())


def test_stop_never_cancels_a_crashed_watcher_outside_its_delay(tmp_path, monkeypatch):
    async def scenario():
        mgr, _ = _mgr_with(tmp_path, monkeypatch, "print('x')\n")
        proc = mgr._procs["tx"]
        gate = asyncio.Event()
        proc.state = ProcessState.CRASHED
        proc._in_restart_delay = False                            # inside _flag_rf_fault
        proc._watcher_task = asyncio.create_task(gate.wait())
        await proc.stop()
        await asyncio.sleep(0)
        assert not proc._watcher_task.cancelled() and proc.state == ProcessState.CRASHED
        proc._in_restart_delay = True                             # sleeping its delay
        await proc.stop()
        await asyncio.sleep(0)
        assert proc._watcher_task.cancelled() and proc.state == ProcessState.STOPPED
        gate.set()
    asyncio.run(scenario())


def test_wait_out_run_claim_gives_up_after_the_window(tmp_path):
    async def scenario():
        mgr, proc = _relaunchable(tmp_path, delay=0.0)
        proc.config.restart_window_s = 0.4
        proc._pending_query = lambda: {"tx"}                      # a claim that never clears
        mgr.set_owned_query(lambda: {"tx"})
        t0 = asyncio.get_event_loop().time()
        assert await proc._wait_out_run_claim() is False
        assert asyncio.get_event_loop().time() - t0 < 2.0
    asyncio.run(scenario())


def test_unreadable_schema_relaunch_proceeds_without_an_elapsed(tmp_path, monkeypatch):
    mgr, runner = R._mk(tmp_path, monkeypatch, ["--freq", "1600", "--power", "-30", "--rf", "on"])
    now = _now()
    T0 = now - timedelta(seconds=100)
    run = R._install(runner, [R.T._fire("start", 0.0, T0, fired=R.T._iso(T0), args=["--freq", "1600"], replace=True),
                              R.T._fire("stop", 0.0, now + timedelta(seconds=60), fired="skipped")],
                     T0=T0, now=now, fault_at=now - timedelta(seconds=30))
    fire = runner._relaunch_start_fire(run, "tx", now, None, include_skipped=True, elapsed_at=now)
    assert fire is not None and "elapsed" not in R._argdict(fire.args)


# ── G1 / G9: replay never re-instates a hold-skipped fire; a due-but-unfired fire is floored ──

def test_replay_plan_skips_hold_skipped_fires_and_floors_past_now(tmp_path, monkeypatch):
    mgr, runner = T._mk(tmp_path, monkeypatch)
    now = _now()
    run = T._faulted_auto_run(runner, now, policy="manual")
    run.steps.insert(3, T._fire("tune", 6.0, now - timedelta(seconds=4), fired="skipped:hold", params={"power": -30}))
    run.steps.insert(3, T._fire("tune", 7.0, now - timedelta(seconds=3.5), fired="skipped", params={"power": -60}))
    plan, _end = runner._plan_restart(run, "tx", now, "replay", 3.0)
    planned = [s for s, _ in plan]
    assert not [s for s in planned if s.fired_actual == "skipped:hold"]
    due = [nf for s, nf in plan if s.params.get("power") == -60]
    assert due and due[0] > now                                   # floored past the relaunch START
