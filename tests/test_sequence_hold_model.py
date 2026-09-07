"""The Hold step DATA MODEL + structural validation (docs/sequence-hold-step.md §5.1).

Covers the vocabulary (StepAction.HOLD, SequenceState.HOLDING, the SequenceRun/
ArmSequenceRequest fields, ProceedRequest) and the _validate_steps rules. The arm-time
GATE lives here too: a Hold-bearing sequence must be armed hold_aware, else it is refused
(the scheduled path compiles the Hold out client-side). The holding RUNTIME itself — park
at the hold, proceed, the deadman, restart-abort — is exercised in test_sequence_hold_runtime.py.

The validation rules (SequenceRunner._validate_steps) are pure/synchronous, so most tests
call them directly; the arm gate and the non-Hold regression run through the async arm().
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.models import (
    ArmSequenceRequest, CreateSequenceRequest, ProceedRequest, SequenceRun,
    SequenceState, SequenceStep, StepAction, TaskConfig,
)
from agent.process_manager import ProcessManager
from agent.sequence_runner import SequenceRunner

REPO_ROOT = str(Path(__file__).resolve().parents[1])


def _runner(tmp_path):
    """A runner whose unit knows one task 'tx' (never started — validation/arm only)."""
    tasks = {
        "tx": TaskConfig(name="tx", command=["python3", "-c", "pass"],
                         working_dir=str(tmp_path), env={"PYTHONPATH": REPO_ROOT}),
    }
    mgr = ProcessManager(tasks, tmp_path, "unit-a")
    return SequenceRunner(mgr, "unit-a", tmp_path / "seq.json",
                          tmp_path / "runs.json", tmp_path)


def _hold(offset_s=30.0, **kw):
    """A HOLD boundary marker (window-A end). No task, no work."""
    return SequenceStep(anchor="start", offset_s=offset_s, action=StepAction.HOLD,
                        task_name="", **kw)


def _valid_hold_steps():
    """START @0 → HOLD @30 → (window B: TUNE anchored to the hold) → STOP @0."""
    return [
        SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx"),
        _hold(30.0),
        SequenceStep(anchor="hold", offset_s=0.0, action=StepAction.TUNE,
                     task_name="tx", params={"gain": 10}),
        SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
    ]


def _normal_steps():
    """A plain two-anchor sequence (no Hold), for the regression check."""
    return [
        SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx"),
        SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
    ]


# ── Model shape ──────────────────────────────────────────────────────────────

def test_new_vocabulary_exists():
    assert StepAction.HOLD.value == "hold"
    assert SequenceState.HOLDING.value == "holding"


def test_sequence_run_hold_fields_default_off():
    # Defaulted so a run persisted before this feature deserializes unchanged.
    run = SequenceRun(id="r1", sequence_id="s", sequence_name="n",
                      on_air_at="2026-01-01T00:00:00+00:00")
    assert run.hold_at_offset_s is None
    assert run.held_actual is None
    assert run.resumed_actual is None
    assert run.hold_aware is False
    assert run.max_hold_s == 1800.0            # 30-min deadman default


def test_arm_request_hold_fields_default_off():
    req = ArmSequenceRequest(on_air_at="2026-01-01T00:00:00+00:00")
    assert req.hold_aware is False
    assert req.max_hold_s == 1800.0


def test_proceed_request_model():
    p = ProceedRequest(proceed_at="2026-01-01T00:00:00+00:00")
    assert p.proceed_at == "2026-01-01T00:00:00+00:00"
    assert p.steps is None
    # It carries an optional edited window-B step list (edit-while-holding, Phase 1).
    p2 = ProceedRequest(proceed_at="2026-01-01T00:00:00+00:00", steps=_valid_hold_steps())
    assert len(p2.steps) == 4


def test_hold_step_round_trips_through_json():
    step = _hold(45.0)
    again = SequenceStep(**step.model_dump())
    assert again.action == StepAction.HOLD
    assert again.anchor == "start"
    assert again.offset_s == 45.0
    assert again.args == [] and again.params == {} and again.ramp is None


# ── Validation: the good case ────────────────────────────────────────────────

def test_validate_accepts_one_hold_with_window_b(tmp_path):
    # Exactly one HOLD in a legal position, with an anchor="hold" step after it.
    _runner(tmp_path)._validate_steps(_valid_hold_steps())


def test_validate_accepts_hold_with_stop_only_window_b(tmp_path):
    # Window B need not contain anchor="hold" steps — a bare hold + stop is legal.
    steps = [
        SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx"),
        _hold(10.0),
        SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
    ]
    _runner(tmp_path)._validate_steps(steps)


def test_normal_sequence_still_validates(tmp_path):
    _runner(tmp_path)._validate_steps(_normal_steps())


# ── Validation: the rejections (Appendix A.2.5) ──────────────────────────────

def test_validate_rejects_two_holds(tmp_path):
    steps = [
        SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx"),
        _hold(10.0),
        _hold(20.0),
        SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
    ]
    with pytest.raises(ValueError, match="at most one HOLD"):
        _runner(tmp_path)._validate_steps(steps)


def test_validate_rejects_hold_with_args(tmp_path):
    steps = [
        SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx"),
        SequenceStep(anchor="start", offset_s=10.0, action=StepAction.HOLD,
                     task_name="tx", args=["--power", "-40"]),
        SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
    ]
    with pytest.raises(ValueError, match="no args/params/ramp"):
        _runner(tmp_path)._validate_steps(steps)


def test_validate_rejects_hold_with_params(tmp_path):
    steps = [
        SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx"),
        SequenceStep(anchor="start", offset_s=10.0, action=StepAction.HOLD,
                     task_name="tx", params={"gain": 5}),
        SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
    ]
    with pytest.raises(ValueError, match="no args/params/ramp"):
        _runner(tmp_path)._validate_steps(steps)


def test_validate_rejects_start_step_after_the_hold(tmp_path):
    # The "ordering" rejection: window A (start-anchored work) must complete before the
    # hold. A start-anchored step with a LARGER offset than the HOLD sits on the wrong
    # side of the boundary (a stop/window-B item ordered before the hold).
    steps = [
        SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx"),
        _hold(10.0),
        SequenceStep(anchor="start", offset_s=20.0, action=StepAction.TUNE,
                     task_name="tx", params={"gain": 3}),
        SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
    ]
    with pytest.raises(ValueError, match="after the HOLD"):
        _runner(tmp_path)._validate_steps(steps)


def test_validate_rejects_hold_marker_not_anchored_to_start(tmp_path):
    # A HOLD anchored to 'stop' would sit in the stop window — mis-ordered.
    steps = [
        SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx"),
        SequenceStep(anchor="stop", offset_s=-5.0, action=StepAction.HOLD, task_name=""),
        SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
    ]
    with pytest.raises(ValueError, match="anchored to 'start'"):
        _runner(tmp_path)._validate_steps(steps)


def test_validate_rejects_stray_hold_anchor_without_a_hold(tmp_path):
    steps = [
        SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx"),
        SequenceStep(anchor="hold", offset_s=5.0, action=StepAction.TUNE,
                     task_name="tx", params={"gain": 3}),
        SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
    ]
    with pytest.raises(ValueError, match="requires a HOLD marker"):
        _runner(tmp_path)._validate_steps(steps)


# ── The arm-time gate (a Hold must be armed hold_aware), and the non-Hold regression ─

def test_non_hold_aware_arm_of_a_hold_sequence_is_refused(tmp_path):
    # The safety gate (docs §7): the agent refuses to run a Hold un-held. The scheduled
    # path compiles the Hold out client-side; a raw non-hold-aware arm is rejected.
    async def scenario():
        runner = _runner(tmp_path)
        seq = await runner.create_sequence(CreateSequenceRequest(
            name="hold-run", steps=_valid_hold_steps()))
        now = datetime.now(timezone.utc)
        with pytest.raises(ValueError, match="contains a Hold"):
            await runner.arm(
                seq.id,
                ArmSequenceRequest(on_air_at=(now + timedelta(seconds=60)).isoformat(),
                                   open_ended=True),           # hold_aware defaults False
                None,
            )
        assert runner.list_runs() == []                        # nothing armed

    asyncio.run(scenario())


def test_arm_gate_also_covers_per_run_step_lists(tmp_path):
    # A plan may pass a Hold-bearing step list via ArmSequenceRequest.steps; the gate
    # must fire on eff_steps, not just the stored sequence.
    async def scenario():
        runner = _runner(tmp_path)
        seq = await runner.create_sequence(CreateSequenceRequest(
            name="plain", steps=_normal_steps()))
        now = datetime.now(timezone.utc)
        with pytest.raises(ValueError, match="contains a Hold"):
            await runner.arm(
                seq.id,
                ArmSequenceRequest(on_air_at=(now + timedelta(seconds=60)).isoformat(),
                                   open_ended=True, steps=_valid_hold_steps()),
                None,
            )

    asyncio.run(scenario())


def test_non_hold_sequence_still_arms(tmp_path):
    # Regression: a Hold-free sequence arms exactly as before.
    async def scenario():
        runner = _runner(tmp_path)
        seq = await runner.create_sequence(CreateSequenceRequest(
            name="plain", steps=_normal_steps()))
        now = datetime.now(timezone.utc)
        run = await runner.arm(
            seq.id,
            ArmSequenceRequest(on_air_at=(now + timedelta(seconds=3600)).isoformat(),
                               open_ended=True),
            None,
        )
        assert run.state == SequenceState.ARMED
        assert run.hold_aware is False           # defaulted off on a normal arm
        assert any(s.action == "start" for s in run.steps)

    asyncio.run(scenario())
