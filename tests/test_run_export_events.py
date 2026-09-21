"""The exported log-table shows a run's RF faults and restarts (docs/rf-fault-recovery.md §14n).

Owner report (1.36.3): a plan whose P-code task RF-faulted (sustained underflows during a ramp) and
was restarted + resynced a couple of times exported a spreadsheet that read "fine all along" — the
run's fault fields are cleared by the restart, the synthetic relaunch is an ordinary start fire at
the same level (deduped away), and the table walked only fired start/tune steps.

Now the run keeps a durable `incidents` list (an RF fault, a relaunch-less restart, a give-up), the
synthetic relaunch fire carries a `note`, and `build_task_table` renders both in a trailing Event
column: an incident is a DEAD row at its instant (blank power/device cells, RF 0), a noted fire
always gets a row. The text run log also gains the fault line it never had.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import run_table                                    # noqa: E402
from agent.models import RestartRequest, RunIncident, SequenceRun, SequenceState, StepFire   # noqa: E402
import test_run_table as R                                     # noqa: E402  (_SPEC/_ART/_realize/_step/_launch)
import test_sequence_restart as T                              # noqa: E402  (_mk/_fire/_iso)


def _inc(kind, at, task="mock_prn", detail="sustained TX underflows: 6 heavy reports over 4.5 s"):
    return SimpleNamespace(kind=kind, at=at, task=task, detail=detail)


# ── the table ──────────────────────────────────────────────────────────────────

def test_event_column_is_last_and_blank_on_ordinary_rows():
    t = run_table.build_task_table("mock_prn", [R._launch("on")], R._SPEC, R._ART, R._realize)
    assert t["columns"][-1] == "Event"
    assert t["rows"][0][-1] == ""


def test_a_fault_is_a_dead_row_and_the_relaunch_note_forces_a_row():
    steps = [
        R._launch("on"),                                                          # 09:00:00 at -99.65
        R._step("tune", "2026-09-09T09:00:05+00:00", params={"power": -95.0}),
        # the RESTART relaunch: the SAME level as before the fault, marked by its note
        SimpleNamespace(action="start", task_name="mock_prn", fired_actual="2026-09-09T09:00:20+00:00",
                        args=["--prn", "1", "--freq", "1575.42", "--sidelobes", "2",
                              "--power", "-95", "--rf", "on"],
                        params={}, note="RESTART (resync)"),
        R._step("tune", "2026-09-09T09:00:30+00:00", params={"power": -90.0}),
    ]
    incidents = [_inc("rf_fault", "2026-09-09T09:00:12+00:00")]
    t = run_table.build_task_table("mock_prn", steps, R._SPEC, R._ART, R._realize,
                                   on_air_at="2026-09-09T09:00:00+00:00", incidents=incidents)
    cols = t["columns"]
    times = [r[0] for r in t["rows"]]
    assert times == ["09:00:00.000", "09:00:05.000", "09:00:12.000", "09:00:20.000", "09:00:30.000"]
    fault = t["rows"][2]
    assert fault[-1].startswith("RF FAULT — sustained TX underflows")
    assert fault[1] == 12                                       # the on-air offset is filled too
    # dead: every power quantity + device cell blank, the RF gate reads 0, params kept
    for name in ("Spectral density [dBm/Hz]", "Full signal power (filter passband) [dBm]",
                 "SDR gain [dB]", "Attenuation [dB]"):
        assert fault[cols.index(name)] is None, name
    assert fault[cols.index("RF on")] == 0
    assert fault[cols.index("Sidelobes")] == 2
    relaunch = t["rows"][3]
    assert relaunch[-1] == "RESTART (resync)"
    assert relaunch[cols.index("Spectral density [dBm/Hz]")] == -95   # back at the level it had
    assert relaunch[cols.index("RF on")] == 1
    assert t["rows"][4][-1] == ""                               # the ramp resumes, plain rows again


def test_without_incidents_or_notes_the_rows_are_as_before():
    steps = [R._launch("on"), R._step("tune", "2026-09-09T09:00:05+00:00", params={"power": -95.0}),
             R._step("tune", "2026-09-09T09:00:06+00:00", params={"power": -95.0})]   # a no-op
    t = run_table.build_task_table("mock_prn", steps, R._SPEC, R._ART, R._realize)
    assert len(t["rows"]) == 2                                  # the no-op tune still adds no row


def test_incidents_are_scoped_to_the_task_and_a_give_up_is_shown():
    steps = [R._launch("on")]
    incidents = [_inc("rf_fault", "2026-09-09T09:00:10+00:00", task="other"),     # not this task
                 _inc("rf_fault", "2026-09-09T09:00:12+00:00"),
                 _inc("gave_up", "2026-09-09T09:00:40+00:00", task="", detail="budget exhausted")]
    t = run_table.build_task_table("mock_prn", steps, R._SPEC, R._ART, R._realize, incidents=incidents)
    events = [r[-1] for r in t["rows"]]
    assert events[0] == ""
    assert events[1].startswith("RF FAULT")
    assert events[2] == "AUTO-RESTART GAVE UP — budget exhausted"
    assert len(t["rows"]) == 3                                  # the other task's fault is not here


def test_a_relaunch_less_restart_incident_is_a_dead_row():
    steps = [R._launch("on")]
    incidents = [_inc("restart", "2026-09-09T09:00:12+00:00",
                      detail="resync: mock_prn is scheduled OFF now — no relaunch")]
    t = run_table.build_task_table("mock_prn", steps, R._SPEC, R._ART, R._realize, incidents=incidents)
    row = t["rows"][1]
    assert row[-1].startswith("RESTART — resync: mock_prn is scheduled OFF")
    assert row[t["columns"].index("RF on")] == 0


# ── the model ──────────────────────────────────────────────────────────────────

def test_incidents_and_notes_round_trip_and_default_empty():
    run = SequenceRun(id="r", sequence_id="s", sequence_name="n", on_air_at="2026-09-09T09:00:00+00:00",
                      steps=[StepFire(anchor="start", offset_s=0, action="start", task_name="tx",
                                      fire_at="2026-09-09T09:00:00+00:00", note="RESTART (replay)")],
                      incidents=[RunIncident(kind="rf_fault", at="2026-09-09T09:00:12+00:00",
                                             task="tx", detail="d")])
    again = SequenceRun(**run.model_dump())
    assert again.incidents[0].kind == "rf_fault" and again.steps[0].note == "RESTART (replay)"
    old = run.model_dump(); old.pop("incidents"); old["steps"][0].pop("note")
    loaded = SequenceRun(**old)                                 # a pre-1.36.4 record
    assert loaded.incidents == [] and loaded.steps[0].note == ""


# ── the runner ─────────────────────────────────────────────────────────────────

def _running_run(runner, now, *, rid="r1"):
    T0 = now - timedelta(seconds=10)
    steps = [
        T._fire("start", -1.0, T0, fired=T._iso(T0), args=["--power", "-90", "--rf", "off"], replace=True),
        T._fire("tune", 0.0, T0 + timedelta(seconds=1), fired=T._iso(T0 + timedelta(seconds=1)),
                params={"rf": "on"}),
        T._fire("tune", 5.0, now - timedelta(seconds=5), fired=T._iso(now - timedelta(seconds=5)),
                params={"power": -70}),
        T._fire("tune", 12.0, now + timedelta(seconds=2), fired=None, params={"power": -50}),
        T._fire("stop", 0.0, now + timedelta(seconds=10), fired=None),
    ]
    run = SequenceRun(id=rid, sequence_id="s1", sequence_name="pcode", state=SequenceState.RUNNING,
                      on_air_at=T._iso(T0), on_air_end=T._iso(now + timedelta(seconds=10)), steps=steps)
    runner._runs[rid] = run
    return run


def test_fault_then_restart_leaves_the_incident_the_note_and_an_export_that_shows_them(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = T._mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = _running_run(runner, now)
        lines = []
        runner._run_logs["r1"] = SimpleNamespace(annotate=lines.append)
        await runner.on_task_fault("tx", "sustained TX underflows: 6 heavy reports over 4.5 s")
        assert run.fault_task == "tx" and len(run.incidents) == 1
        inc = run.incidents[0]
        assert inc.kind == "rf_fault" and inc.task == "tx" and inc.at == run.fault_at
        assert inc.detail.startswith("sustained TX underflows")
        assert any(l.startswith("⚠ RF FAULT — tx: sustained TX underflows") for l in lines)   # the text log too

        out = await runner.restart_run("r1", RestartRequest(mode="resync", restart_at=T._iso(now)))
        relaunch = [s for s in out.steps if s.action == "start" and s.note][0]
        assert relaunch.note == "RESTART (resync)"
        assert out.fault == "" and len(out.incidents) == 1          # a relaunch marks the fire, not a 2nd incident
        relaunch.fired_actual = T._iso(now + timedelta(milliseconds=300))   # the tick fires it

        table = runner.build_log_table("r1")["tables"][0]
        cols = table["columns"]
        assert cols[-1] == "Event"
        events = [r[-1] for r in table["rows"]]
        fault_rows = [r for r in table["rows"] if r[-1].startswith("RF FAULT")]
        restart_rows = [r for r in table["rows"] if r[-1] == "RESTART (resync)"]
        assert len(fault_rows) == 1 and len(restart_rows) == 1
        assert events.index(fault_rows[0][-1]) < events.index("RESTART (resync)")
        rf_col = cols.index(next(c for c in cols if c.endswith(" on")))   # the gate column ("Rf on")
        assert fault_rows[0][rf_col] == 0                               # dead
        assert restart_rows[0][rf_col] == 1                             # back on air
    asyncio.run(scenario())


def test_replay_and_auto_restart_notes(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = T._mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = T._faulted_run(runner, now, rid="rr")
        out = await runner.restart_run("rr", RestartRequest(mode="replay", restart_at=T._iso(now)))
        note = [s for s in out.steps if s.action == "start" and s.note][0].note
        assert note.startswith("RESTART (replay), off-air shifted +3s")
        run2 = T._faulted_run(runner, now, rid="ra")
        out2 = await runner.restart_run("ra", RestartRequest(mode="resync", restart_at=T._iso(now)),
                                        reset_budget=False)
        assert [s for s in out2.steps if s.action == "start" and s.note][0].note == "AUTO-RESTART (resync)"
    asyncio.run(scenario())


def test_give_up_records_an_incident(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = T._mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = T._faulted_run(runner, now, rid="rg")
        await runner._auto_restart_gaveup(run, "budget exhausted (2 attempts)")
        assert run.incidents[-1].kind == "gave_up"
        assert run.incidents[-1].task == "tx" and "budget exhausted" in run.incidents[-1].detail
    asyncio.run(scenario())
