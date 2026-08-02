"""
ClientStateStore: a unit's replica of the PC's plans + schedule — persisted,
reloaded, and replaced wholesale. Opaque storage (never executed here).
"""
from agent.client_state import ClientStateStore
from agent.models import Plan, PlanItem, ScheduledPlan, SequenceStep, StepAction


def _plan(pid: str) -> Plan:
    return Plan(id=pid, name=pid.upper(), items=[
        PlanItem(hostname="unit-a", sequence_id="seq_1", sequence_name="beacon",
                 steps=[SequenceStep(anchor="start", offset_s=0,
                                     action=StepAction.START, task_name="tx")],
                 on_air_offset_s=5.0)])


def _sched(sid: str, pid: str) -> ScheduledPlan:
    return ScheduledPlan(id=sid, plan_id=pid, plan_name=pid.upper(),
                         start="2026-08-02T10:00:00", stop="2026-08-02T10:30:00")


def test_roundtrip_and_persist(tmp_path):
    pf, sf = tmp_path / "plans.json", tmp_path / "schedule.json"
    store = ClientStateStore(pf, sf)
    assert store.get_plans() == [] and store.get_schedule() == []

    store.set_plans([_plan("p1"), _plan("p2")])
    store.set_schedule([_sched("s1", "p1")])

    # A fresh store over the same files reloads what was written.
    store2 = ClientStateStore(pf, sf)
    assert [p.id for p in store2.get_plans()] == ["p1", "p2"]
    assert store2.get_plans()[0].items[0].on_air_offset_s == 5.0
    assert [s.id for s in store2.get_schedule()] == ["s1"]
    assert store2.get_schedule()[0].plan_id == "p1"


def test_replace_is_wholesale(tmp_path):
    store = ClientStateStore(tmp_path / "p.json", tmp_path / "s.json")
    store.set_plans([_plan("p1"), _plan("p2")])
    store.set_plans([_plan("p3")])          # replaces, not merges
    assert [p.id for p in store.get_plans()] == ["p3"]


def test_corrupt_files_degrade_to_empty(tmp_path):
    pf, sf = tmp_path / "p.json", tmp_path / "s.json"
    pf.write_text("{ not json")
    sf.write_text("]]garbage")
    store = ClientStateStore(pf, sf)
    assert store.get_plans() == [] and store.get_schedule() == []
