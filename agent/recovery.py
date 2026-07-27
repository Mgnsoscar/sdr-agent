"""
Recovery & panic operations.

Two concerns live here:

1. PANIC / EMERGENCY STOP
   Immediately stop all running tasks on this unit and cancel/abort every
   armed-or-running event and sequence run, so nothing re-fires. This is the
   "RF off NOW" action (e.g. the airport calls).

2. RESUME-OFFSET COMPUTATION
   Helpers the GUI uses to propose a resume offset for crash recovery:
     • resume-all-from-offset: re-arm a plan synchronized at a new on-air time
       with a chosen elapsed offset.
     • rejoin-single-unit: relaunch one crashed unit so it rejoins peers that
       never stopped, accounting for this unit's warm-up lead-in.

   The actual offset math is simple and deterministic; we expose it as a pure
   function so both the GUI and tests can use it. Re-arming is then just a
   normal sequence arm with resume_offset_s set.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from .models import PanicResult
from .process_manager import ProcessManager
from .scheduler import Scheduler
from .sequence_runner import SequenceRunner

logger = logging.getLogger(__name__)


def _utcnow_dt() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow_dt().isoformat()


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ── Panic ──────────────────────────────────────────────────────────────────────

async def panic_stop(
    manager: ProcessManager,
    scheduler: Scheduler,
    runner: SequenceRunner,
    unit_id: str,
) -> PanicResult:
    """
    Stop everything on this unit immediately and clear all schedules so nothing
    re-fires. Order matters: abort runs/events FIRST (so their schedulers stop
    issuing starts), then stop any remaining running tasks.
    """
    logger.warning("PANIC stop requested on unit %s", unit_id)

    # 1. Abort/cancel sequence runs (stops their tasks + halts remaining steps)
    runs_aborted = await runner.abort_all_active(reason="panic stop")

    # 2. Cancel/stop scheduled events
    events_cancelled = await scheduler.cancel_all_active(reason="panic stop")

    # 3. Stop any tasks still running (manual starts, or anything left over)
    tasks_stopped = []
    for status in manager.all_statuses():
        if status.state == "running":
            try:
                await manager.stop(status.name, source="recovery")
                tasks_stopped.append(status.name)
            except Exception as exc:
                logger.error("Panic: failed to stop task '%s': %s", status.name, exc)

    result = PanicResult(
        unit_id=unit_id,
        tasks_stopped=tasks_stopped,
        events_cancelled=events_cancelled,
        runs_aborted=runs_aborted,
        at=_utcnow_iso(),
    )
    logger.warning("PANIC complete on %s: %s", unit_id, result.model_dump())
    return result


# ── Resume-offset math (pure, deterministic) ─────────────────────────────────────

def elapsed_on_air_s(original_on_air_at: str, now: Optional[datetime] = None) -> float:
    """
    How many seconds of on-air time have elapsed since the original on-air start.
    This is the offset the surviving (never-stopped) units are currently at.
    """
    now = now or _utcnow_dt()
    return max(0.0, (now - _parse(original_on_air_at)).total_seconds())


def rejoin_offset_s(
    original_on_air_at: str,
    warmup_lead_in_s: float,
    now: Optional[datetime] = None,
) -> float:
    """
    Offset to inject when REJOINING a single crashed unit to peers that never
    stopped. The rejoining unit cannot be on-air instantly — it needs its
    warm-up lead-in first. So the offset must match where peers will be at the
    moment THIS unit's RF actually comes on, i.e. now + lead_in.

    warmup_lead_in_s should be the absolute value of the most-negative
    start-anchored step offset (e.g. 120 for a step at -120s).
    """
    now = now or _utcnow_dt()
    rf_live_at = now + timedelta(seconds=max(0.0, warmup_lead_in_s))
    return max(0.0, (rf_live_at - _parse(original_on_air_at)).total_seconds())


def rejoin_on_air_at(
    warmup_lead_in_s: float,
    now: Optional[datetime] = None,
) -> str:
    """
    The on-air (T0) time to pass when rejoining: now + warm-up lead-in, so the
    sequence's warm-up has time to run before RF goes live. Returned as UTC ISO.
    """
    now = now or _utcnow_dt()
    return (now + timedelta(seconds=max(0.0, warmup_lead_in_s))).isoformat()