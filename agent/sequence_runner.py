"""
SequenceRunner — stores Sequences and executes SequenceRuns on this unit.

A Sequence is a relative-timed choreography around a single on-air window:

    anchor="start" steps   (offset relative to on-air START, T0)
        e.g.  -120s start rx_flowgraph     (warm-up)
                 0s start amplifier         (on-air begins)
                 0s start attenuator_ramp
    anchor="stop"  steps   (offset relative to on-air STOP)
                 0s stop  amplifier         (on-air ends)
                +5s stop  attenuator_ramp   (cool-down)
                +5s stop  rx_flowgraph

Arming resolves these to absolute fire times around two anchors:
    on_air_at  (T0)         — start-anchored steps fire at  on_air_at  + offset
    on_air_end (stop)       — stop-anchored  steps fire at  on_air_end + offset

Extending/shortening moves on_air_end; stop-anchored steps follow automatically
because they are recomputed from on_air_end.

Resume: a resume_offset_s > 0 is injected into resumable steps' start (via
ProcessManager.build_resume_request) so a ramp begins partway through.

Fail-safe: on reboot, any run that was mid-flight is ABORTED — every task the
sequence touches is stopped. We never resume across a crash automatically.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from . import ramp
from .log_manager import LogManager
from .models import (
    ArmSequenceRequest, CreateSequenceRequest, Sequence, SequenceRun,
    SequenceState, SequenceStep, StepFire, StepOverride,
    SequenceWebhook,
)
from .process_manager import ProcessManager
from .sequence_log import RunLog

logger = logging.getLogger(__name__)

_TICK_SECONDS = 0.25


def _utcnow_dt() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow_dt().isoformat()


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _seq_id() -> str:
    return "seq_" + secrets.token_hex(4)


def _run_id() -> str:
    return "run_" + secrets.token_hex(4)


class SequenceRunner:
    def __init__(
        self,
        manager: ProcessManager,
        unit_id: str,
        sequences_path: Path,
        runs_path: Path,
        log_root: Path,
    ):
        self._manager  = manager
        self._unit_id  = unit_id
        self._seq_store = sequences_path
        self._run_store = runs_path
        self._log_root = log_root
        # One LogManager per sequence (logs/_sequences/<seq_id>/); the run log is
        # rotated into it per run, so the Logs view tails the latest run.
        self._seq_logs: Dict[str, LogManager] = {}
        # Active per-run log writers (annotations + collected task output).
        self._run_logs: Dict[str, RunLog] = {}
        self._sequences: Dict[str, Sequence] = {}
        self._runs: Dict[str, SequenceRun] = {}
        self._loop_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        # Run ids for which the on-air (T0) marker event has already been emitted,
        # so we fire "sequence_on_air" exactly once per run when on_air_at is
        # reached. In-memory only: a run mid-flight across a restart is aborted by
        # reconcile, so this never needs to survive one.
        self._on_air_marked: set[str] = set()
        # Same, for the off-air (on_air_end / T_end) marker. Reset when a run's
        # on_air_end is moved (patch), so it re-fires at the new end.
        self._off_air_marked: set[str] = set()

    # ── Run logging ────────────────────────────────────────────────────────────

    def get_sequence_log_manager(self, seq_id: str) -> LogManager:
        """The LogManager for a sequence's run log (created lazily), so the Logs
        view can tail it even between runs."""
        lm = self._seq_logs.get(seq_id)
        if lm is None:
            lm = LogManager(self._log_root / "_sequences", seq_id)
            self._seq_logs[seq_id] = lm
        return lm

    def _task_log_manager(self, name: str) -> Optional[LogManager]:
        try:
            return self._manager.get_log_manager(name)
        except KeyError:
            return None

    def _open_run_log(self, seq: Sequence, run: SequenceRun) -> None:
        try:
            rl = RunLog(self.get_sequence_log_manager(seq.id), self._task_log_manager)
            window = (f"on-air {run.on_air_at} → {run.on_air_end}"
                      if run.on_air_end else f"on-air {run.on_air_at} (open-ended)")
            rl.open(f"sequence '{seq.name}'  run {run.id}  {window}")
            rl.annotate("armed")
            self._run_logs[run.id] = rl
        except Exception:
            logger.exception("Run %s: could not open run log", run.id)

    def _close_run_log(self, run_id: str, footer: str) -> None:
        rl = self._run_logs.pop(run_id, None)
        if rl is not None:
            try:
                rl.close(footer)
            except Exception:
                logger.exception("Run %s: error closing run log", run_id)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        self._load_sequences()
        self._load_runs()
        await self._reconcile_on_startup()
        self._loop_task = asyncio.create_task(self._run_loop(), name="sequence-loop")
        logger.info(
            "SequenceRunner started: %d sequence(s), %d run(s)",
            len(self._sequences), len(self._runs),
        )

    async def shutdown(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass

    # ── Sequence CRUD ─────────────────────────────────────────────────────────

    def _validate_steps(self, steps: List[SequenceStep]) -> None:
        if not steps:
            raise ValueError("sequence must have at least one step")
        # All referenced tasks must exist on this unit
        for s in steps:
            if not self._manager.has_task(s.task_name):
                raise ValueError(f"unknown task in step: '{s.task_name}'")
            action = s.action.value if hasattr(s.action, "value") else str(s.action)
            if s.anchor not in ("start", "stop", "both"):
                raise ValueError(f"step anchor must be 'start', 'stop' or 'both', got '{s.anchor}'")
            if s.anchor == "both" and action != "ramp":
                raise ValueError("only a ramp step can be anchored to both edges")
            if action == "ramp":
                if s.ramp is None:
                    raise ValueError(f"ramp step for '{s.task_name}' has no ramp definition")
                try:
                    if s.anchor == "both":
                        if s.ramp.steps is None and s.ramp.step is None and s.ramp.hold_s is None:
                            raise ValueError("a window-filling ramp needs a step count or hold time")
                    else:
                        ramp.resolve_ramp(s.ramp.start, s.ramp.stop, steps=s.ramp.steps, step=s.ramp.step,
                                          hold_s=s.ramp.hold_s, duration_s=s.ramp.duration_s)
                except ValueError as exc:
                    raise ValueError(f"ramp step for '{s.task_name}': {exc}")
        # Must have an on-air start (a start-anchored action at offset 0 is the
        # conventional T0 action, but we don't force it — we just require that
        # there's at least one start-anchored and one stop-anchored step so the
        # on-air window is well-defined).
        has_start = any(s.anchor == "start" for s in steps)
        has_stop  = any(s.anchor == "stop"  for s in steps)
        if not has_start:
            raise ValueError("sequence needs at least one 'start'-anchored step (on-air start)")
        if not has_stop:
            raise ValueError("sequence needs at least one 'stop'-anchored step (on-air stop)")

    async def create_sequence(self, req: CreateSequenceRequest) -> Sequence:
        self._validate_steps(req.steps)
        seq = Sequence(
            id=_seq_id(), name=req.name, description=req.description, steps=req.steps
        )
        async with self._lock:
            self._sequences[seq.id] = seq
            self._persist_sequences()
        logger.info("Sequence %s created: %s (%d steps)", seq.id, seq.name, len(seq.steps))
        return seq

    async def update_sequence(self, seq_id: str, req: CreateSequenceRequest) -> Sequence:
        if seq_id not in self._sequences:
            raise KeyError(f"Unknown sequence: '{seq_id}'")
        self._validate_steps(req.steps)
        seq = Sequence(
            id=seq_id, name=req.name, description=req.description, steps=req.steps
        )
        async with self._lock:
            self._sequences[seq_id] = seq
            self._persist_sequences()
        logger.info("Sequence %s updated", seq_id)
        return seq

    def list_sequences(self) -> List[Sequence]:
        return list(self._sequences.values())

    def get_sequence(self, seq_id: str) -> Sequence:
        if seq_id not in self._sequences:
            raise KeyError(f"Unknown sequence: '{seq_id}'")
        return self._sequences[seq_id]

    async def delete_sequence(self, seq_id: str) -> None:
        if seq_id not in self._sequences:
            raise KeyError(f"Unknown sequence: '{seq_id}'")
        # Refuse to delete if an armed/running run references it
        for run in self._runs.values():
            if run.sequence_id == seq_id and run.state in (
                SequenceState.ARMED, SequenceState.RUNNING
            ):
                raise ValueError("cannot delete a sequence with an active run")
        async with self._lock:
            del self._sequences[seq_id]
            self._persist_sequences()
        logger.info("Sequence %s deleted", seq_id)

    def _has_active_run(self, seq_id: str) -> bool:
        return any(r.sequence_id == seq_id and r.state in (
            SequenceState.ARMED, SequenceState.RUNNING) for r in self._runs.values())

    async def apply_sequences(self, sequences: List[Sequence], prune: bool):
        """Converge this unit's sequences to `sequences`, PRESERVING their ids so
        every unit shares the same sequence_id a plan references.

        Definitions only: upsert each incoming sequence (by id), and — when prune —
        delete stored ones the library omits, EXCEPT any with an armed/running run
        (those are kept and reported as skipped; an in-flight broadcast captured its
        own steps at arm time, so it is unaffected either way). Returns
        (upserted_ids, deleted_ids, skipped_ids). Raises ValueError if a step
        references a task this unit doesn't have (deploy tasks before sequences)."""
        for seq in sequences:
            self._validate_steps(seq.steps)
        upserted: List[str] = []
        deleted: List[str] = []
        skipped: List[str] = []
        incoming = {s.id for s in sequences}
        async with self._lock:
            for seq in sequences:
                self._sequences[seq.id] = Sequence(
                    id=seq.id, name=seq.name, description=seq.description,
                    steps=list(seq.steps))
                upserted.append(seq.id)
            if prune:
                for seq_id in [s for s in self._sequences if s not in incoming]:
                    if self._has_active_run(seq_id):
                        skipped.append(seq_id)
                        continue
                    del self._sequences[seq_id]
                    deleted.append(seq_id)
            self._persist_sequences()
        logger.info("apply_sequences: upserted %d, deleted %d, skipped %d",
                    len(upserted), len(deleted), len(skipped))
        return upserted, deleted, skipped

    # ── On-air window helpers ─────────────────────────────────────────────────

    @staticmethod
    def _lead_offset(steps: List[SequenceStep]) -> float:
        """
        The most-negative start-anchored offset — the warm-up lead-in. By our model
        the on-air window is [on_air_at, on_air_end] and on_air_end is supplied at
        arm time; this only reports how far before on-air the first step fires.
        """
        return min((s.offset_s for s in steps if s.anchor == "start"), default=0.0)

    @staticmethod
    def _validate_overrides(
        steps: List[SequenceStep], overrides: List[StepOverride],
    ) -> Dict[int, StepOverride]:
        """
        Key step overrides by index and reject any that don't address a real,
        overridable step. A stop step takes no args, so overriding one is an error
        rather than a silent no-op. Raises ValueError on a bad override.
        """
        out: Dict[int, StepOverride] = {}
        n = len(steps)
        for ov in overrides or []:
            if ov.index < 0 or ov.index >= n:
                raise ValueError(
                    f"step override index {ov.index} out of range (sequence has {n} step(s))")
            if steps[ov.index].anchor == "stop" or \
                    str(getattr(steps[ov.index].action, "value",
                                steps[ov.index].action)) == "stop":
                raise ValueError(
                    f"step override index {ov.index} targets a stop step, which takes no args")
            out[ov.index] = ov
        return out

    def _resolve_steps(
        self, steps: List[SequenceStep], on_air_at: datetime,
        on_air_end: Optional[datetime], resume_offset_s: float,
        open_ended: bool = False, overrides: Optional[Dict[int, StepOverride]] = None,
    ) -> List[StepFire]:
        """
        Compute absolute fire times for every step around the two anchors.
        If open_ended is True, stop-anchored steps are skipped entirely — the run
        fires only the start-anchored (warm-up + on-air-start) steps and stays
        on-air until aborted. on_air_end may be None in that case.

        overrides maps a step's index (its position in `steps`) to a StepOverride
        whose args/replace_args replace the step's — so a plan can run a sequence
        with per-task parameters that differ from its saved definition. The stored
        sequence is not mutated; only this run's StepFires carry the new args.
        """
        overrides = overrides or {}
        fires: List[StepFire] = []
        for i, s in enumerate(steps):
            action = s.action.value if hasattr(s.action, "value") else str(s.action)
            if action == "ramp":
                fires.extend(self._resolve_ramp(s, on_air_at, on_air_end, open_ended))
                continue
            if s.anchor == "stop":
                if open_ended:
                    continue   # no stop in an open-ended run; abort handles shutdown
                base = on_air_end
            else:
                base = on_air_at
            fire_at = base + timedelta(seconds=s.offset_s)
            inject = (
                resume_offset_s if (s.action == "start"
                                    and s.inject_resume_offset
                                    and resume_offset_s > 0)
                else None
            )
            ov = overrides.get(i)
            args = list(ov.args) if ov is not None else list(s.args)
            replace_args = ov.replace_args if ov is not None else s.replace_args
            fires.append(StepFire(
                anchor=s.anchor,
                offset_s=s.offset_s,
                action=s.action.value if hasattr(s.action, "value") else str(s.action),
                task_name=s.task_name,
                fire_at=fire_at.isoformat(),
                resume_offset_s=inject,
                args=args,
                replace_args=replace_args,
                params=dict(s.params or {}),
            ))
        # Sort by fire time so the runner fires them in order
        fires.sort(key=lambda f: _parse(f.fire_at))
        return fires

    def _resolve_ramp(self, s: SequenceStep, on_air_at: datetime,
                      on_air_end: Optional[datetime], open_ended: bool) -> List[StepFire]:
        """Expand a RAMP step into a series of `tune` fires. A both-anchored ramp
        fills the on-air window (skipped when the run is open-ended, since there's
        no window). A bad/under-specified ramp is logged and dropped rather than
        sinking the whole run."""
        if s.ramp is None:
            return []
        r = s.ramp
        window_s = None
        if s.anchor == "both":
            if on_air_end is None:
                return []
            # The ramp fills [on-air + offset_s, off-air + offset_end_s]; its
            # duration is the window minus whatever the insets carve off.
            end_inset = s.offset_end_s or 0.0
            window_s = (on_air_end - on_air_at).total_seconds() - (s.offset_s or 0.0) + end_inset
        try:
            resolved = ramp.resolve_ramp(r.start, r.stop, steps=r.steps, step=r.step, hold_s=r.hold_s,
                                         duration_s=r.duration_s, window_s=window_s)
            points = ramp.place_ramp(s.anchor, s.offset_s, resolved)
        except ValueError as exc:
            logger.error("Ramp step for '%s' could not be resolved: %s", s.task_name, exc)
            return []
        out: List[StepFire] = []
        for fire_anchor, off, value in points:
            if fire_anchor == "stop":
                if open_ended or on_air_end is None:
                    continue
                base = on_air_end
            else:
                base = on_air_at
            out.append(StepFire(
                anchor=fire_anchor, offset_s=off, action="tune",
                task_name=s.task_name, fire_at=(base + timedelta(seconds=off)).isoformat(),
                params={r.param: value}))
        return out

    # ── Arming ────────────────────────────────────────────────────────────────

    async def arm(self, seq_id: str, req: ArmSequenceRequest, on_air_end_iso: Optional[str]) -> SequenceRun:
        """
        Arm a sequence. on_air_at = T0 (RF live). on_air_end = on-air stop.
        Both absolute UTC. resume_offset_s injected into resumable start steps.
        If req.open_ended, on_air_end_iso may be None — the run fires only
        start-anchored steps and stays on-air until aborted.
        """
        seq = self.get_sequence(seq_id)

        # A plan may supply a complete, plan-local step list that replaces the
        # stored sequence's steps for this run only (the sequence is untouched).
        if req.steps is not None:
            self._validate_steps(req.steps)
            eff_steps = list(req.steps)
        else:
            eff_steps = seq.steps

        on_air_at  = _parse(req.on_air_at)
        now = _utcnow_dt()

        open_ended = req.open_ended
        on_air_end: Optional[datetime] = None
        if not open_ended:
            if not on_air_end_iso:
                raise ValueError("a fixed-window run requires on_air_end or on_air_duration_s")
            on_air_end = _parse(on_air_end_iso)
            if on_air_end <= on_air_at:
                raise ValueError("on_air_end must be after on_air_at")
            # Hard block: the on-air window must fit the sequence's fixed-duration
            # content (e.g. a 60s ramp-up + a 60s ramp-down ⇒ ≥120s).
            window_s = (on_air_end - on_air_at).total_seconds()
            min_dur = ramp.min_on_air_duration(eff_steps)
            if window_s + 1e-6 < min_dur:
                raise ValueError(
                    f"on-air window is {window_s:.0f}s but this sequence needs at "
                    f"least {min_dur:.0f}s (its ramps don't fit)")

        # The earliest step (most negative start-anchored offset) must be in the future
        lead_in = self._lead_offset(eff_steps)   # most negative offset, e.g. -120
        earliest_fire = on_air_at + timedelta(seconds=lead_in)
        if earliest_fire <= now:
            raise ValueError(
                f"first step would fire in the past "
                f"(on-air start needs {abs(lead_in):.0f}s lead-in; choose a later on_air_at)"
            )

        overrides = self._validate_overrides(eff_steps, req.step_overrides)
        steps = self._resolve_steps(eff_steps, on_air_at, on_air_end, req.resume_offset_s,
                                    open_ended, overrides)

        run = SequenceRun(
            id=_run_id(),
            sequence_id=seq.id,
            sequence_name=seq.name,
            state=SequenceState.ARMED,
            on_air_at=on_air_at.isoformat(),
            on_air_end=on_air_end.isoformat() if on_air_end else None,
            open_ended=open_ended,
            created_at=_utcnow_iso(),
            resume_offset_s=req.resume_offset_s,
            note=req.note,
            steps=steps,
            plan_id=req.plan_id,
            plan_name=req.plan_name,
        )

        async with self._lock:
            self._runs[run.id] = run
            self._persist_runs()

        self._open_run_log(seq, run)

        logger.info(
            "Run %s armed from sequence '%s': on-air %s → %s%s (resume_offset=%.0fs)",
            run.id, seq.name, run.on_air_at,
            run.on_air_end or "(open-ended)",
            f" [plan {req.plan_name}]" if req.plan_name else "",
            req.resume_offset_s,
        )
        return run

    # ── Run management ────────────────────────────────────────────────────────

    def list_runs(self) -> List[SequenceRun]:
        return list(self._runs.values())

    def get_run(self, run_id: str) -> SequenceRun:
        if run_id not in self._runs:
            raise KeyError(f"Unknown run: '{run_id}'")
        return self._runs[run_id]

    async def patch_on_air_end(self, run_id: str, new_end_iso: str) -> SequenceRun:
        """Move the on-air STOP to a new absolute UTC time. Stop-anchored steps follow."""
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise KeyError(f"Unknown run: '{run_id}'")
            if run.state not in (SequenceState.ARMED, SequenceState.RUNNING):
                raise ValueError(f"cannot modify a run in state '{run.state}'")
            if run.open_ended:
                raise ValueError(
                    "cannot extend an open-ended run — it has no on-air stop; "
                    "stop it by aborting instead"
                )

            new_end = _parse(new_end_iso)
            now = _utcnow_dt()
            if new_end <= now:
                raise ValueError("new on-air end must be in the future")
            if new_end <= _parse(run.on_air_at):
                raise ValueError("on-air end must be after on-air start")

            old_end = run.on_air_end
            run.on_air_end = new_end.isoformat()
            # The end moved — allow the off-air marker to fire again at the new end
            # (e.g. a run extended after it had already gone off air).
            self._off_air_marked.discard(run.id)

            # Recompute only stop-anchored steps that haven't fired yet; keep
            # the fired_actual on already-fired steps. Simplest correct approach:
            # rebuild all steps, then re-apply fired_actual for matching steps.
            # Rebuild from THIS RUN's own steps (the choreography it was armed with),
            # not the stored sequence — which may have been edited, or replaced by a
            # plan-local step list, since the run was armed.
            fired_map = {
                (f.anchor, f.offset_s, f.action, f.task_name, tuple(f.args)): f.fired_actual
                for f in run.steps
            }
            armed_steps = [
                SequenceStep(anchor=f.anchor, offset_s=f.offset_s, action=f.action,
                             task_name=f.task_name, args=list(f.args),
                             replace_args=f.replace_args, params=dict(f.params or {}))
                for f in run.steps
            ]
            rebuilt = self._resolve_steps(
                armed_steps, _parse(run.on_air_at), new_end, run.resume_offset_s
            )
            for f in rebuilt:
                key = (f.anchor, f.offset_s, f.action, f.task_name, tuple(f.args))
                f.fired_actual = fired_map.get(key)
            run.steps = rebuilt
            self._persist_runs()

        await self._fire(run, "sequence_modified",
                         detail=f"on-air end {old_end} → {run.on_air_end}")
        logger.info("Run %s on-air end changed %s → %s", run_id, old_end, run.on_air_end)
        return run

    async def cancel_or_abort(self, run_id: str) -> SequenceRun:
        """
        Cancel an ARMED run (never fires), or ABORT a RUNNING run:
        stop every task the sequence touches and halt all remaining steps.
        """
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise KeyError(f"Unknown run: '{run_id}'")
            state = run.state

        if state == SequenceState.ARMED:
            async with self._lock:
                run.state = SequenceState.CANCELLED
                self._persist_runs()
            self._close_run_log(run_id, "cancelled before start")
            logger.info("Run %s cancelled before start", run_id)
            return run

        if state == SequenceState.RUNNING:
            await self._abort_run(run, reason="cancelled by operator")
            return run

        raise ValueError(f"cannot cancel a run in state '{state}'")

    # ── Scheduler loop ──────────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        try:
            while True:
                await self._tick()
                await asyncio.sleep(_TICK_SECONDS)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("SequenceRunner loop crashed")

    async def _tick(self) -> None:
        now = _utcnow_dt()

        # Interleave any new task output into each active run's log.
        for rl in list(self._run_logs.values()):
            rl.collect()

        due: List[tuple[SequenceRun, StepFire]] = []

        async with self._lock:
            for run in self._runs.values():
                if run.state not in (SequenceState.ARMED, SequenceState.RUNNING):
                    continue
                for step in run.steps:
                    if step.fired_actual is None and _parse(step.fire_at) <= now:
                        due.append((run, step))

        # Fire due steps in time order across all runs
        due.sort(key=lambda rs: _parse(rs[1].fire_at))
        for run, step in due:
            await self._fire_step(run, step)

        # Emit the on-air (T0) and off-air (T_end) markers for any run that has
        # reached those moments.
        await self._emit_on_air(now)
        await self._emit_off_air(now)

    # ── On-air / off-air markers ──────────────────────────────────────────────────

    async def _emit_on_air(self, now: datetime) -> None:
        """
        Fire a one-shot "sequence_on_air" event when a run crosses its on_air_at
        (T0). This is distinct from "sequence_started", which fires when the run's
        FIRST step fires — that's the warm-up moment, not on-air. Without this, an
        activity feed can only show T0 via a generic step event; this gives the
        actual RF-live moment its own marker.
        """
        due: List[SequenceRun] = []
        async with self._lock:
            for run in self._runs.values():
                if run.state not in (SequenceState.ARMED, SequenceState.RUNNING):
                    continue
                if run.id in self._on_air_marked:
                    continue
                if _parse(run.on_air_at) <= now:
                    self._on_air_marked.add(run.id)
                    # Record the actual RF-live moment so any GUI can show on-air
                    # state without comparing its own (possibly skewed) clock to T0.
                    run.on_air_actual = now.isoformat()
                    due.append(run)
            if due:
                self._persist_runs()

        for run in due:
            rl = self._run_logs.get(run.id)
            if rl is not None:
                rl.annotate("ON AIR (T0)")
            await self._fire(run, "sequence_on_air", detail="on air")
            logger.info("Run %s on air (T0 reached)", run.id)

    async def _emit_off_air(self, now: datetime) -> None:
        """
        Fire a one-shot "sequence_off_air" event when a run crosses its on_air_end
        (T_end). This is distinct from "sequence_stopped", which fires once EVERY
        step (including cool-down, which is stop-anchored at positive offsets) has
        fired — that's the run finishing, not the RF-off instant. Open-ended runs
        have no on_air_end and never emit this; they end via abort.
        """
        due: List[SequenceRun] = []
        async with self._lock:
            for run in self._runs.values():
                if run.state not in (SequenceState.ARMED, SequenceState.RUNNING):
                    continue
                if run.open_ended or not run.on_air_end:
                    continue
                if run.id in self._off_air_marked:
                    continue
                if _parse(run.on_air_end) <= now:
                    self._off_air_marked.add(run.id)
                    due.append(run)

        for run in due:
            rl = self._run_logs.get(run.id)
            if rl is not None:
                rl.annotate("OFF AIR (T_end)")
            await self._fire(run, "sequence_off_air", detail="off air")
            logger.info("Run %s off air (on_air_end reached)", run.id)

    # ── Step firing ──────────────────────────────────────────────────────────────

    async def _fire_step(self, run: SequenceRun, step: StepFire) -> None:
        # Mark first to avoid double-firing if a step's action is slow
        first_step = run.state == SequenceState.ARMED
        step.fired_actual = _utcnow_iso()
        if first_step:
            run.state = SequenceState.RUNNING
            run.started_actual = run.started_actual or _utcnow_iso()

        rl = self._run_logs.get(run.id)
        if rl is not None:
            glyph = {"start": "▶ start", "run": "⚡ run", "stop": "⏹ stop",
                     "tune": "◈ tune"}.get(step.action, step.action)
            line = f"{glyph} {step.task_name}"
            if step.action == "tune" and step.params:
                line += " " + " ".join(f"{k}={v}" for k, v in step.params.items())
            elif step.args:
                line += " " + " ".join(step.args)
            if step.resume_offset_s:
                line += f"  (resume +{step.resume_offset_s:.0f}s)"
            rl.annotate(line)
            if step.action in ("start", "run"):
                rl.watch_task(step.task_name)   # collect this task's output from here

        try:
            if step.action == "start":
                # Start with any resume-offset injection PLUS this step's own extra
                # args, so a single registered task can be reused with different
                # arguments per step (e.g. a set-gain script at various gains).
                # build_resume_request returns an empty StartRequest for offset 0 /
                # non-resumable tasks, so this covers the no-resume case too.
                sreq = self._manager.build_resume_request(
                    step.task_name, step.resume_offset_s or 0.0)
                if step.args:
                    sreq.args = list(sreq.args) + list(step.args)
                sreq.replace_args = step.replace_args
                await self._manager.start(step.task_name, sreq, source="sequence")
            elif step.action == "run":
                # Fire-and-exit: a transient process, no slot, no stop. Many of the
                # same script (e.g. attenuator sets) can run without colliding.
                await self._manager.run_oneshot(
                    step.task_name, list(step.args), run_id=run.id)
            elif step.action == "tune":
                # Retune a running duration task's live parameters. The task must
                # already be running (started by an earlier step); if it isn't, or
                # exposes no live params, set_params raises and we log it below
                # without derailing the rest of the run.
                result = await self._manager.set_params(
                    step.task_name, dict(step.params), wait=0.0)
                if rl is not None and isinstance(result, dict) and result.get("rejected"):
                    rl.annotate("   ⚠ tune rejected: " + "; ".join(
                        f"{k} ({v})" for k, v in result["rejected"].items()))
            else:  # stop
                await self._manager.stop(step.task_name, source="sequence")
        except Exception as exc:
            logger.error("Run %s step (%s %s) failed: %s",
                         run.id, step.action, step.task_name, exc)

        # Persist progress + fire a per-step webhook (useful for the live feed)
        async with self._lock:
            self._persist_runs()

        await self._fire(
            run, "sequence_step",
            detail=f"{step.action} {step.task_name}"
                   + (f" (resume +{step.resume_offset_s:.0f}s)"
                      if step.resume_offset_s else ""),
        )

        if first_step:
            await self._fire(run, "sequence_started", detail="")

        # If every step has fired, the run is complete — UNLESS it's open-ended,
        # which has only start-anchored steps and must stay on-air until aborted.
        if not run.open_ended and all(s.fired_actual is not None for s in run.steps):
            async with self._lock:
                run.state = SequenceState.COMPLETED
                run.stopped_actual = _utcnow_iso()
                self._persist_runs()
            self._close_run_log(run.id, "completed")
            await self._fire(run, "sequence_stopped", detail="all steps complete")
            logger.info("Run %s completed", run.id)
        elif run.open_ended and all(s.fired_actual is not None for s in run.steps):
            logger.info("Run %s now fully on-air (open-ended; awaiting abort)", run.id)

    # ── Abort ──────────────────────────────────────────────────────────────────

    async def _abort_run(self, run: SequenceRun, reason: str) -> None:
        """Stop EVERY task this run's sequence touches, mark aborted, halt steps."""
        seq = self._sequences.get(run.sequence_id)
        task_names = (
            sorted({s.task_name for s in seq.steps}) if seq
            else sorted({s.task_name for s in run.steps})
        )

        for name in task_names:
            try:
                if self._manager.is_running(name):
                    await self._manager.stop(name)
            except Exception as exc:
                logger.error("Abort run %s: failed to stop '%s': %s", run.id, name, exc)

        # Also sweep any still-running one-shot (run-action) processes for this run.
        try:
            await self._manager.stop_oneshots(run.id)
        except Exception as exc:
            logger.error("Abort run %s: failed to sweep one-shots: %s", run.id, exc)

        async with self._lock:
            run.state = SequenceState.ABORTED
            run.stopped_actual = _utcnow_iso()
            self._persist_runs()

        self._close_run_log(run.id, f"aborted: {reason}")
        await self._fire(run, "sequence_aborted", detail=reason)
        logger.warning("Run %s ABORTED (%s) — stopped tasks: %s", run.id, reason, task_names)

    def tasks_touched_by_active_runs(self) -> List[str]:
        """All task names referenced by armed/running runs (used by panic)."""
        names: set[str] = set()
        for run in self._runs.values():
            if run.state in (SequenceState.ARMED, SequenceState.RUNNING):
                seq = self._sequences.get(run.sequence_id)
                src = seq.steps if seq else run.steps
                names.update(s.task_name for s in src)
        return sorted(names)

    async def abort_all_active(self, reason: str) -> List[str]:
        """Abort every armed/running run. Returns the run ids aborted."""
        active = [r for r in self._runs.values()
                  if r.state in (SequenceState.ARMED, SequenceState.RUNNING)]
        aborted: List[str] = []
        for run in active:
            if run.state == SequenceState.ARMED:
                async with self._lock:
                    run.state = SequenceState.CANCELLED
                    self._persist_runs()
                self._close_run_log(run.id, f"cancelled: {reason}")
            else:
                await self._abort_run(run, reason=reason)
            aborted.append(run.id)
        return aborted

    # ── Startup reconciliation (fail-safe = abort) ─────────────────────────────

    async def _reconcile_on_startup(self) -> None:
        now = _utcnow_dt()
        for run in list(self._runs.values()):
            if run.state not in (SequenceState.ARMED, SequenceState.RUNNING):
                continue

            # Has any step already fired, or is any step's time in the past?
            any_fired = any(s.fired_actual is not None for s in run.steps)
            all_past  = all(_parse(s.fire_at) <= now for s in run.steps)
            any_past  = any(_parse(s.fire_at) <= now for s in run.steps)

            if all_past and not any_fired:
                # Whole thing was scheduled in a window that fully elapsed while
                # we were down and nothing fired — treat as aborted (fail-safe).
                logger.warning("Reconcile: run %s window fully elapsed while down — aborting", run.id)
                await self._abort_run(run, reason="window elapsed during downtime")
            elif any_fired or any_past:
                # We were mid-run (or should have been). Fail safe: abort, stop
                # everything the sequence touches.
                logger.warning("Reconcile: run %s was mid-flight at startup — aborting (fail-safe)", run.id)
                await self._abort_run(run, reason="agent restarted mid-run")
            else:
                # Entirely in the future — keep armed.
                logger.info("Reconcile: run %s re-armed (on-air %s)", run.id, run.on_air_at)

        async with self._lock:
            self._persist_runs()

    # ── Webhook helper ──────────────────────────────────────────────────────────

    async def _fire(self, run: SequenceRun, kind: str, detail: str = "") -> None:
        payload = SequenceWebhook(
            type=kind,
            unit_id=self._unit_id,
            run_id=run.id,
            sequence_name=run.sequence_name,
            on_air_at=run.on_air_at,
            on_air_end=run.on_air_end,
            state=run.state.value,
            at=_utcnow_iso(),
            detail=detail,
        )
        asyncio.create_task(self._manager.dispatcher.fire(payload))

    # ── Persistence ──────────────────────────────────────────────────────────────

    def _persist_sequences(self) -> None:
        try:
            data = {"sequences": [s.model_dump() for s in self._sequences.values()]}
            tmp = self._seq_store.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.rename(self._seq_store)
        except OSError as exc:
            logger.error("Failed to persist sequences.json: %s", exc)

    def _persist_runs(self) -> None:
        try:
            data = {"runs": [r.model_dump() for r in self._runs.values()]}
            tmp = self._run_store.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.rename(self._run_store)
        except OSError as exc:
            logger.error("Failed to persist sequence_runs.json: %s", exc)

    def _load_sequences(self) -> None:
        if not self._seq_store.exists():
            self._sequences = {}
            return
        try:
            data = json.loads(self._seq_store.read_text())
            self._sequences = {s["id"]: Sequence(**s) for s in data.get("sequences", [])}
            logger.info("Loaded %d sequence(s)", len(self._sequences))
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            logger.error("Failed to load sequences.json (%s) — starting empty", exc)
            self._sequences = {}

    def _load_runs(self) -> None:
        if not self._run_store.exists():
            self._runs = {}
            return
        try:
            data = json.loads(self._run_store.read_text())
            self._runs = {r["id"]: SequenceRun(**r) for r in data.get("runs", [])}
            logger.info("Loaded %d run(s)", len(self._runs))
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            logger.error("Failed to load sequence_runs.json (%s) — starting empty", exc)
            self._runs = {}