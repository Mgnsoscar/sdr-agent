"""The plan REPLICA round-trips the client's plan-level anchoring fields (1.35.0).

The client's plan editor lets a plan item's on-/off-air hang off another item / a step in another
item, and a step hang off a step / window edge in another item. Plans are client-only but are
replicated to every unit as opaque storage; if the agent's models dropped the fields, every anchored
plan would read as drifted forever and a reconcile would strip its anchors. Storage only — the
client compiles the anchors to absolute times before arming, so the runtime never sees them."""
from agent import config as cfg
from agent.models import Plan, PlanItem, SequenceStep


def test_plan_item_round_trips_every_anchor_field():
    step = {"id": "t1", "anchor": "step", "anchor_item": "pi-a", "anchor_step_id": "up", "anchor_edge": "end",
            "offset_s": 120.0, "action": "tune", "task_name": "tx", "params": {"rf": "on"}}
    item = {"id": "pi-b", "hostname": "u2", "sequence_id": "s", "steps": [step],
            "on_air_anchor": "step", "on_air_anchor_item": "pi-a", "on_air_anchor_edge": "end",
            "on_air_anchor_step": "up", "on_air_offset_s": 60.0,
            "off_air_anchor": "item", "off_air_anchor_item": "", "off_air_anchor_edge": "on",
            "off_air_anchor_step": "", "off_air_offset_s": 450.0, "expanded": False}
    plan = Plan(id="p", name="p", items=[PlanItem(**item)])
    back = Plan(**plan.model_dump(mode="json"))
    it = back.items[0]
    assert it.id == "pi-b" and it.expanded is False
    assert (it.on_air_anchor, it.on_air_anchor_item, it.on_air_anchor_edge, it.on_air_anchor_step) == \
        ("step", "pi-a", "end", "up")
    assert (it.off_air_anchor, it.off_air_anchor_item, it.off_air_anchor_edge) == ("item", "", "on")
    assert it.steps[0].anchor_item == "pi-a" and it.steps[0].anchor_step_id == "up"
    # a pre-1.35 plan (no fields) defaults to the plan's own anchors
    old = PlanItem(hostname="u", sequence_id="s", on_air_offset_s=5.0)
    assert (old.id, old.on_air_anchor, old.off_air_anchor, old.expanded) == ("", "plan", "plan", True)
    assert SequenceStep(anchor="start", offset_s=0, action="start", task_name="t").anchor_item == ""


def test_capability_and_version():
    assert "plan-item-anchors" in cfg.AGENT_CAPABILITIES
    assert tuple(int(x) for x in cfg.AGENT_VERSION.split(".")) >= (1, 35, 0)
