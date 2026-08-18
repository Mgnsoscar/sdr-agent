"""
Scheduler — fires timed task events at wall-clock (UTC) times.

A ScheduledEvent says: start <task> at <start_at>, stop it at <stop_at>.
The scheduler runs an async loop that arms tasks at their start time and
stops them at their stop time, firing webhooks at each transition.

Persistence & fail-safe
────────────────────────
Events are persisted to events.json on every change.  On startup the
scheduler reconciles persisted events against the current time:

  • stop_at already passed        → ensure task stopped, mark COMPLETED
  • now between start and stop     → ABORT (stop task), mark ABORTED
                                     (a crash mid-event means we can't trust
                                      the units's state — fail safe = off)
  • start_at still in the future   → re-arm normally

All times are UTC ISO-8601 strings at the API boundary; internally we
parse them to aware datetimes for comparison.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .models import (
    CreateEventRequest, EventState, EventWebhook, ScheduledEvent,
)
from .process_manager import ProcessManager

logger = logging.getLogger(__name__)

# How often the scheduler loop wakes to check for due transitions.
# 0.25s keeps us comfortably inside the "within a second" requirement.
_TICK_SECONDS = 0.25


def _utcnow_dt() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow_dt().isoformat()


def _parse(ts: str) -> datetime:
    """Parse an ISO-8601 string to an aware UTC datetime."""
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _new_event_id() -> str:
    return "evt_" + secrets.token_hex(4)


class Scheduler:
    def __init__(self, manager: ProcessManager, unit_id: str, store_path: Path):
        self._manager   = manager
        self._unit_id   = unit_id
        self._store     = store_path
        self._events: Dict[str, ScheduledEvent] = {}
        self._loop_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()   # guards _events during transitions/edits

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        """Load persisted events, reconcile against now, then start the loop."""
        self._load()
        await self._reconcile_on_startup()
        self._loop_task = asyncio.create_task(self._run(), name="scheduler-loop")
        logger.info("Scheduler started with %d event(s)", len(self._events))

    async def shutdown(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass

    # ── Public API (called by HTTP layer) ─────────────────────────────────────

    async def create(self, req: CreateEventRequest) -> ScheduledEvent:
        """Validate and arm a new event."""
        if not self._manager.has_task(req.task_name):
            raise ValueError(f"Unknown task: '{req.task_name}'")

        start_dt = _parse(req.start_at)

        # Resolve the stop time from either stop_at or duration_s
        if req.stop_at and req.duration_s is not None:
            raise ValueError("Provide either stop_at or duration_s, not both")
        if req.stop_at:
            stop_dt = _parse(req.stop_at)
        elif req.duration_s is not None:
            if req.duration_s <= 0:
                raise ValueError("duration_s must be positive")
            from datetime import timedelta
            stop_dt = start_dt + timedelta(seconds=req.duration_s)
        else:
            raise ValueError("Must provide either stop_at or duration_s")

        now = _utcnow_dt()
        if start_dt <= now:
            raise ValueError("start_at must be in the future")
        if stop_dt <= start_dt:
            raise ValueError("stop time must be after start_at")

        event = ScheduledEvent(
            id         = _new_event_id(),
            task_name  = req.task_name,
            start_at   = start_dt.isoformat(),
            stop_at    = stop_dt.isoformat(),
            state      = EventState.ARMED,
            created_at = _utcnow_iso(),
            note       = req.note,
        )

        async with self._lock:
            self._events[event.id] = event
            self._persist()

        logger.info(
            "Event %s armed: %s  %s → %s",
            event.id, event.task_name, event.start_at, event.stop_at
        )
        return event

    def list_events(self) -> List[ScheduledEvent]:
        return list(self._events.values())

    def get_event(self, event_id: str) -> ScheduledEvent:
        if event_id not in self._events:
            raise KeyError(f"Unknown event: '{event_id}'")
        return self._events[event_id]

    async def patch_stop(self, event_id: str, new_stop_at: str) -> ScheduledEvent:
        """Change the stop time of an ARMED or RUNNING event to a new absolute UTC time."""
        async with self._lock:
            event = self._events.get(event_id)
            if event is None:
                raise KeyError(f"Unknown event: '{event_id}'")

            if event.state not in (EventState.ARMED, EventState.RUNNING):
                raise ValueError(f"Cannot modify an event in state '{event.state}'")

            new_stop_dt = _parse(new_stop_at)
            now = _utcnow_dt()

            if new_stop_dt <= now:
                raise ValueError("new stop time must be in the future")
            # Stop must still be after the start
            if new_stop_dt <= _parse(event.start_at):
                raise ValueError("stop time must be after start_at")

            old_stop = event.stop_at
            event.stop_at = new_stop_dt.isoformat()
            self._persist()

        await self._fire(event, "event_modified",
                         detail=f"stop time changed {old_stop} → {event.stop_at}")
        logger.info("Event %s stop time changed %s → %s", event_id, old_stop, event.stop_at)
        return event

    async def cancel(self, event_id: str) -> ScheduledEvent:
        """
        Cancel an ARMED event, or stop-now a RUNNING event.
        - ARMED   → mark CANCELLED, never fires.
        - RUNNING → stop the task immediately, mark COMPLETED.
        """
        async with self._lock:
            event = self._events.get(event_id)
            if event is None:
                raise KeyError(f"Unknown event: '{event_id}'")

            if event.state == EventState.ARMED:
                event.state = EventState.CANCELLED
                self._persist()
                logger.info("Event %s cancelled before start", event_id)
                return event

            if event.state == EventState.RUNNING:
                # Stop now
                running_state = event
            else:
                raise ValueError(f"Cannot cancel an event in state '{event.state}'")

        # Outside the lock: perform the stop
        await self._stop_task(running_state, completed=True, manual=True)
        return running_state

    async def cancel_all_active(self, reason: str = "panic stop") -> List[str]:
        """
        Cancel/stop every armed-or-running event. Armed events are marked
        cancelled; running events have their task stopped. Returns the list of
        affected event ids. Used by the panic / emergency-stop path.
        """
        # Snapshot under the lock; act outside it for the running ones
        armed: List[ScheduledEvent] = []
        running: List[ScheduledEvent] = []
        async with self._lock:
            for event in self._events.values():
                if event.state == EventState.ARMED:
                    event.state = EventState.CANCELLED
                    armed.append(event)
                elif event.state == EventState.RUNNING:
                    running.append(event)
            if armed:
                self._persist()

        affected = [e.id for e in armed]
        for event in running:
            await self._stop_task(event, completed=True, manual=True)
            affected.append(event.id)

        if affected:
            logger.warning("Scheduler: cancelled/stopped %d event(s) (%s)", len(affected), reason)
        return affected

    # ── Scheduler loop ─────────────────────────────────────────────────────────

    async def _run(self) -> None:
        try:
            while True:
                await self._tick()
                await asyncio.sleep(_TICK_SECONDS)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Scheduler loop crashed")

    async def _tick(self) -> None:
        now = _utcnow_dt()

        # Snapshot due transitions under the lock, act outside it
        to_start: List[ScheduledEvent] = []
        to_stop:  List[ScheduledEvent] = []

        async with self._lock:
            for event in self._events.values():
                if event.state == EventState.ARMED and _parse(event.start_at) <= now:
                    to_start.append(event)
                elif event.state == EventState.RUNNING and _parse(event.stop_at) <= now:
                    to_stop.append(event)

        for event in to_start:
            await self._start_task(event)
        for event in to_stop:
            await self._stop_task(event, completed=True)

    # ── Transitions ─────────────────────────────────────────────────────────────

    async def _start_task(self, event: ScheduledEvent) -> None:
        already = self._manager.is_running(event.task_name)
        event.already_running = already
        event.started_actual  = _utcnow_iso()
        event.state           = EventState.RUNNING

        if not already:
            try:
                await self._manager.start(event.task_name, source="scheduler")
            except Exception as exc:
                logger.error("Event %s failed to start task '%s': %s",
                             event.id, event.task_name, exc)
        else:
            logger.info(
                "Event %s: task '%s' already running — scheduling stop only",
                event.id, event.task_name
            )

        async with self._lock:
            self._persist()

        await self._fire(
            event, "event_started",
            detail="task already running; will stop at stop_at" if already else "",
        )

    async def _stop_task(self, event: ScheduledEvent, completed: bool, manual: bool = False) -> None:
        event.stopped_actual = _utcnow_iso()
        event.state = EventState.COMPLETED if completed else EventState.ABORTED

        try:
            await self._manager.stop(event.task_name, source="scheduler")
        except Exception as exc:
            logger.error("Event %s failed to stop task '%s': %s",
                         event.id, event.task_name, exc)

        async with self._lock:
            self._persist()

        kind = "event_stopped" if completed else "event_aborted"
        detail = "stopped manually" if manual else ""
        await self._fire(event, kind, detail=detail)
        logger.info("Event %s %s (task '%s')", event.id, event.state, event.task_name)

    # ── Startup reconciliation (fail-safe) ──────────────────────────────────────

    async def _reconcile_on_startup(self) -> None:
        now = _utcnow_dt()
        for event in list(self._events.values()):
            if event.state not in (EventState.ARMED, EventState.RUNNING):
                continue   # terminal states need no action

            start_dt = _parse(event.start_at)
            stop_dt  = _parse(event.stop_at)

            if stop_dt <= now:
                # The whole window passed while we were down.
                # Make sure the task isn't somehow still running, mark completed.
                if self._manager.is_running(event.task_name):
                    logger.warning(
                        "Reconcile: event %s window passed but task '%s' still running — stopping",
                        event.id, event.task_name
                    )
                    await self._stop_task(event, completed=True)
                else:
                    event.state = EventState.COMPLETED
                    event.stopped_actual = event.stopped_actual or _utcnow_iso()
                    logger.info("Reconcile: event %s already complete", event.id)

            elif start_dt <= now < stop_dt:
                # We rebooted mid-event. Fail safe: abort (stop the broadcast).
                logger.warning(
                    "Reconcile: event %s was mid-run at startup — aborting (fail-safe stop)",
                    event.id
                )
                await self._stop_task(event, completed=False)

            else:
                # start still in the future — leave armed
                event.state = EventState.ARMED
                logger.info("Reconcile: event %s re-armed for %s", event.id, event.start_at)

        async with self._lock:
            self._persist()

    # ── Webhook helper ──────────────────────────────────────────────────────────

    async def _fire(self, event: ScheduledEvent, kind: str, detail: str = "") -> None:
        payload = EventWebhook(
            type      = kind,
            unit_id   = self._unit_id,
            event_id  = event.id,
            task_name = event.task_name,
            start_at  = event.start_at,
            stop_at   = event.stop_at,
            state     = event.state.value,
            at        = _utcnow_iso(),
            detail    = detail,
        )
        # Fire and forget so a slow webhook never delays a transition
        asyncio.create_task(self._manager.dispatcher.fire(payload))

    # ── Persistence ──────────────────────────────────────────────────────────────

    def _persist(self) -> None:
        """Write all events to events.json atomically. Caller holds the lock."""
        try:
            data = {"events": [e.model_dump() for e in self._events.values()]}
            tmp = self._store.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.rename(self._store)
        except OSError as exc:
            logger.error("Failed to persist events.json: %s", exc)

    def _load(self) -> None:
        if not self._store.exists():
            self._events = {}
            return
        try:
            data = json.loads(self._store.read_text())
            self._events = {
                e["id"]: ScheduledEvent(**e) for e in data.get("events") or []
            }
            logger.info("Loaded %d persisted event(s)", len(self._events))
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            logger.error("Failed to load events.json (%s) — starting empty", exc)
            self._events = {}