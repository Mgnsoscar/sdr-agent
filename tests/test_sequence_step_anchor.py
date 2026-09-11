"""Step-to-step anchoring (Phase 1): a step timed relative to ANOTHER step's edge.

The agent resolves fire times topologically (roots first, then step-anchored steps once
their target's edges are known), and _validate_steps rejects unknown targets, cycles,
self-anchors, and (Phase 1) mixing a step anchor with a Hold. A ramp's "end" edge is its
last point; a point step's start==end==its single fire.
"""
from datetime import datetime, timedelta, timezone

import pytest

from agent.models import RampSpec, SequenceStep, StepAction, TaskConfig
from agent.process_manager import ProcessManager
from agent.sequence_runner import SequenceRunner

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
END = T0 + timedelta(seconds=60)


def _runner(tmp_path):
    tasks = {"tx": TaskConfig(name="tx", command=["python3", "/x/tx.py"], working_dir="/x")}
    mgr = ProcessManager(tasks, tmp_path, "unit-a")
    return SequenceRunner(mgr, "unit-a", tmp_path / "seq.json", tmp_path / "runs.json", tmp_path)


def _off(fire_iso):
    return round((datetime.fromisoformat(fire_iso) - T0).total_seconds(), 6)


def _tune(sid, gain, anchor="start", offset=0.0, ref="", edge="end"):
    return SequenceStep(id=sid, anchor=anchor, anchor_step_id=ref, anchor_edge=edge,
                        offset_s=offset, action=StepAction.TUNE, task_name="tx",
                        params={"gain": gain})


# ── Resolution ───────────────────────────────────────────────────────────────

def test_point_step_anchor_end_start_and_chain(tmp_path):
    r = _runner(tmp_path)
    steps = [
        SequenceStep(id="s0", anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx"),
        _tune("a", 10, anchor="start", offset=5.0),                     # fires T0+5
        _tune("b", 20, anchor="step", ref="a", edge="end", offset=2.0),  # a.end(5)+2 = 7
        _tune("c", 30, anchor="step", ref="b", edge="end", offset=1.0),  # b(7)+1 = 8 (chain)
        _tune("d", 40, anchor="step", ref="a", edge="start", offset=-1.0),  # a.start(5)-1 = 4
        SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
    ]
    fires = r._resolve_steps(steps, T0, END, 0.0)
    by = {f.params.get("gain"): _off(f.fire_at) for f in fires if f.action == "tune"}
    assert by == {10: 5.0, 20: 7.0, 30: 8.0, 40: 4.0}
    # Fires are globally time-ordered.
    times = [_off(f.fire_at) for f in fires]
    assert times == sorted(times)


def test_ramp_end_edge_is_its_last_point(tmp_path):
    r = _runner(tmp_path)
    ramp = RampSpec(start=0.0, stop=9.0, steps=3, duration_s=6.0, param="gain")
    steps = [
        SequenceStep(id="s0", anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx"),
        SequenceStep(id="rmp", anchor="start", offset_s=2.0, action=StepAction.RAMP,
                     task_name="tx", ramp=ramp),
        _tune("after", 99, anchor="step", ref="rmp", edge="end", offset=1.0),
        SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
    ]
    fires = r._resolve_steps(steps, T0, END, 0.0)
    ramp_pts = sorted(_off(f.fire_at) for f in fires if f.anchor == "start" and f.action == "tune")
    after = next(_off(f.fire_at) for f in fires if f.params.get("gain") == 99)
    assert after == round(ramp_pts[-1] + 1.0, 6)     # the tune hangs off the ramp's LAST point


def test_no_step_anchor_is_unchanged(tmp_path):
    """A sequence with no step anchor resolves exactly as before (roots only)."""
    r = _runner(tmp_path)
    steps = [
        SequenceStep(anchor="start", offset_s=-1.0, action=StepAction.START, task_name="tx"),
        _tune("x", 5, anchor="start", offset=0.0),
        SequenceStep(anchor="stop", offset_s=2.0, action=StepAction.STOP, task_name="tx"),
    ]
    fires = r._resolve_steps(steps, T0, END, 0.0)
    offs = sorted(_off(f.fire_at) for f in fires)
    assert offs == [-1.0, 0.0, 62.0]                 # start-1, tune@0, stop = on_air_end(60)+2


# ── Validation ───────────────────────────────────────────────────────────────

def _valid_base():
    return [
        SequenceStep(id="s0", anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx"),
        SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
    ]


def test_valid_step_anchor_passes(tmp_path):
    r = _runner(tmp_path)
    steps = _valid_base() + [_tune("a", 1, anchor="step", ref="s0", edge="end", offset=1.0)]
    r._validate_steps(steps)             # no raise


@pytest.mark.parametrize("bad, msg", [
    (lambda: _tune("a", 1, anchor="step", ref="ghost", edge="end"), "unknown step id"),
    (lambda: _tune("a", 1, anchor="step", ref="a", edge="end"), "cannot anchor to itself"),
    (lambda: _tune("a", 1, anchor="step", ref="s0", edge="middle"), "anchor_edge"),
    (lambda: _tune("a", 1, anchor="step", ref="", edge="end"), "needs anchor_step_id"),
])
def test_bad_step_anchor_rejected(tmp_path, bad, msg):
    r = _runner(tmp_path)
    with pytest.raises(ValueError, match=msg):
        r._validate_steps(_valid_base() + [bad()])


def test_cycle_rejected(tmp_path):
    r = _runner(tmp_path)
    steps = _valid_base() + [
        _tune("a", 1, anchor="step", ref="b", edge="end"),
        _tune("b", 2, anchor="step", ref="a", edge="end"),
    ]
    with pytest.raises(ValueError, match="cycle"):
        r._validate_steps(steps)


def test_step_anchor_with_hold_rejected_in_phase1(tmp_path):
    r = _runner(tmp_path)
    steps = [
        SequenceStep(id="s0", anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx"),
        SequenceStep(anchor="start", offset_s=1.0, action=StepAction.HOLD, task_name=""),
        _tune("a", 1, anchor="step", ref="s0", edge="end", offset=0.5),
        SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
    ]
    with pytest.raises(ValueError, match="Hold"):
        r._validate_steps(steps)
