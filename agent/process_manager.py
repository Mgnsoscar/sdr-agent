"""
ProcessManager — owns the lifecycle of every registered task.

Each task runs as a real OS subprocess.  stdout and stderr are merged
and written to the task's log file.  Crash-restart logic runs inside
an asyncio task so it never blocks the HTTP server.

Event support: when a task crashes the manager fires a CrashEvent to all
connected SSE subscribers (best-effort, non-blocking, stdlib-only).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic as _monotonic
from typing import Deque, Dict, List, Optional

from .log_manager import LogManager
from .models import (
    CrashEvent, ExitRecord, ProcessState, ProcessStatus,
    StartRequest, TaskConfig, TaskEvent,
)

logger = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Event dispatcher (SSE fan-out) ────────────────────────────────────────────

class EventDispatcher:
    """
    Fans events out to connected SSE subscribers.

    Instead of POSTing to registered webhook URLs (which required the Pi to make
    inbound connections to laptops — blocked by laptop firewalls without admin),
    this holds an in-memory asyncio.Queue per connected SSE client. fire() puts
    the event on every queue; each /events/stream connection drains its own queue
    and writes the events down its long-lived HTTP response.

    The connection is laptop-initiated and outbound (laptop GETs the Pi), so it
    needs no inbound firewall rule on the laptop. No registration, no stored URLs.

    fire(event) keeps the same signature as before, so callers (process manager,
    scheduler, sequence runner) are unchanged.
    """

    def __init__(self, max_queue: int = 1000):
        self._subscribers: set[asyncio.Queue] = set()
        self._max_queue = max_queue

    def subscribe(self) -> asyncio.Queue:
        """Register a new SSE client; returns its private event queue."""
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(q)
        logger.info("SSE subscriber connected (%d total)", len(self._subscribers))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)
        logger.info("SSE subscriber disconnected (%d remaining)", len(self._subscribers))

    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def fire(self, event) -> None:
        """
        Put the event (any Pydantic model) on every subscriber's queue. Non-blocking
        and best-effort: if a subscriber's queue is full (a stuck/slow client), the
        event is dropped for that client rather than blocking the agent.
        """
        if not self._subscribers:
            return
        payload = event.model_dump()
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                logger.warning("SSE subscriber queue full — dropping event for one client")


# ── Managed process ───────────────────────────────────────────────────────────

class ManagedProcess:
    """Wraps a single asyncio subprocess and its state."""

    def __init__(
        self,
        config: TaskConfig,
        log_manager: LogManager,
        dispatcher: EventDispatcher,
        unit_id: str,
    ):
        self.config      = config
        self.log         = log_manager
        self.state       = ProcessState.STOPPED
        self.pid: Optional[int] = None
        self.exit_code: Optional[int] = None
        self.started_at: Optional[str] = None
        self.stopped_at: Optional[str] = None
        self.restart_count: int = 0

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._watcher_task: Optional[asyncio.Task] = None
        self._log_fh = None
        self._dispatcher = dispatcher
        self._unit_id = unit_id

        # Set when a manual stop is requested, so an in-progress restart-delay in
        # the watcher aborts instead of relaunching (lets you stop a crash-looping
        # task). Cleared on an intentional start.
        self._stop_requested = False
        # Timestamps (monotonic) of recent auto-restarts, for the crash-loop
        # circuit breaker.
        self._restart_times: Deque[float] = deque(maxlen=50)
        # True once the breaker has tripped; surfaced so the UI can show it.
        self.restart_giving_up = False

        # Ring buffer of recent exits (newest appended last)
        self.history: Deque[ExitRecord] = deque(maxlen=10)

    # ── Public interface ──────────────────────────────────────────────────────

    async def start(self, request: Optional[StartRequest] = None) -> None:
        if self.state in (ProcessState.RUNNING, ProcessState.STARTING):
            raise RuntimeError(f"Task '{self.config.name}' is already {self.state}")

        # An explicit start clears any prior stop request and breaker trip.
        self._stop_requested = False
        self.restart_giving_up = False
        self.state = ProcessState.STARTING
        req = request or StartRequest()

        cmd = list(self.config.command) + list(req.args)
        env = {**os.environ, **self.config.env, **req.env_overrides}

        self.log.rotate()
        self.log.cleanup()   # prune old archives so the SD card never fills
        self._log_fh = self.log.open_for_write()

        logger.info("Starting task '%s': %s", self.config.name, cmd)

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=self._log_fh,
            stderr=self._log_fh,
            cwd=self.config.working_dir,
            env=env,
            start_new_session=True,
        )

        self.pid        = self._proc.pid
        self.state      = ProcessState.RUNNING
        self.started_at = _utcnow()
        self.stopped_at = None
        self.exit_code  = None

        self._watcher_task = asyncio.create_task(
            self._watch(), name=f"watch-{self.config.name}"
        )

    async def stop(self, timeout: float = 10.0) -> None:
        # Always record the stop request first — this breaks an in-progress
        # restart-delay in the watcher (the crash-loop case), even if there's no
        # live process to signal right now.
        self._stop_requested = True

        if self.state not in (ProcessState.RUNNING, ProcessState.STARTING):
            # Task isn't running. It may be mid-crash-loop (state CRASHED, watcher
            # sleeping before a restart). Cancel that watcher so it doesn't relaunch,
            # and settle the state to STOPPED.
            if self.state == ProcessState.CRASHED:
                if self._watcher_task and not self._watcher_task.done():
                    self._watcher_task.cancel()
                self.state = ProcessState.STOPPED
                logger.info("Task '%s' crash-restart cancelled by stop", self.config.name)
            return

        self.state = ProcessState.STOPPING
        logger.info("Stopping task '%s' (pid=%s)", self.config.name, self.pid)

        if self._proc and self._proc.returncode is None:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass

            try:
                await asyncio.wait_for(self._proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("Task '%s' did not stop; sending SIGKILL", self.config.name)
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await self._proc.wait()

        await self._cleanup()

    def status(self) -> ProcessStatus:
        return ProcessStatus(
            name          = self.config.name,
            description   = self.config.description,
            state         = self.state,
            pid           = self.pid,
            exit_code     = self.exit_code,
            started_at    = self.started_at,
            stopped_at    = self.stopped_at,
            restart_count = self.restart_count,
            log_file      = str(self.log.current),
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _watch(self) -> None:
        """Wait for the process to exit, then handle restart and crash notification."""
        assert self._proc is not None
        await self._proc.wait()
        code = self._proc.returncode
        self.exit_code  = code
        self.stopped_at = _utcnow()

        await self._cleanup(set_state=False)

        intentional = self.state == ProcessState.STOPPING
        was_crash = (code != 0) and not intentional

        # Record this exit in the ring buffer
        self.history.append(ExitRecord(
            started_at = self.started_at,
            exited_at  = self.stopped_at,
            exit_code  = code,
            was_crash  = was_crash,
        ))

        if intentional:
            # Intentional stop — no crash event
            self.state = ProcessState.STOPPED
            logger.info("Task '%s' stopped (exit=%s)", self.config.name, code)

        elif code != 0:
            # Unexpected exit — fire crash event before deciding on restart
            self.state = ProcessState.CRASHED
            logger.warning("Task '%s' crashed (exit=%s)", self.config.name, code)
            await self._fire_crash_event(code)

            if not self.config.restart_on_crash:
                return

            # Crash-loop circuit breaker: count restarts within the rolling window.
            now = _monotonic()
            window = self.config.restart_window_s
            self._restart_times.append(now)
            recent = [t for t in self._restart_times if now - t <= window]
            limit = self.config.max_restarts
            if limit and len(recent) > limit:
                self.restart_giving_up = True
                logger.error(
                    "Task '%s' crashed %d times within %.0fs — giving up auto-restart "
                    "(manual start required)",
                    self.config.name, len(recent), window,
                )
                # Fire one more crash event flagged as the final give-up so the GUI
                # can show it stopped looping.
                await self._fire_crash_event(code, gave_up=True)
                return

            logger.info(
                "Restarting '%s' in %.1fs (restart #%d) ...",
                self.config.name, self.config.restart_delay_s, self.restart_count + 1
            )
            try:
                await asyncio.sleep(self.config.restart_delay_s)
            except asyncio.CancelledError:
                # stop() cancelled us during the delay — do not relaunch.
                logger.info("Task '%s' restart aborted (stop requested)", self.config.name)
                self.state = ProcessState.STOPPED
                raise
            # A stop requested during the delay also aborts the relaunch.
            if self._stop_requested:
                logger.info("Task '%s' restart aborted (stop requested)", self.config.name)
                self.state = ProcessState.STOPPED
                return
            self.restart_count += 1
            await self.start()
        else:
            # Clean exit (code 0)
            self.state = ProcessState.STOPPED
            logger.info("Task '%s' exited cleanly", self.config.name)

    async def _fire_crash_event(self, exit_code: Optional[int], gave_up: bool = False) -> None:
        """Collect last log lines and dispatch the crash event to subscribers."""
        try:
            last_lines = await self.log.tail(20)
        except Exception:
            last_lines = []

        detail_lines = list(last_lines)
        if gave_up:
            detail_lines = [
                f"[auto-restart disabled after crash loop]"
            ] + detail_lines

        event = CrashEvent(
            unit_id          = self._unit_id,
            task_name        = self.config.name,
            task_description = self.config.description,
            exit_code        = exit_code,
            started_at       = self.started_at,
            crashed_at       = self.stopped_at or _utcnow(),
            restart_count    = self.restart_count,
            last_log_lines   = detail_lines,
        )
        # Fire and forget — don't let a slow subscriber delay crash handling
        asyncio.create_task(self._dispatcher.fire(event))

    async def _cleanup(self, set_state: bool = True) -> None:
        if self._log_fh:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None

        if set_state:
            self.state = ProcessState.STOPPED


# ── Manager ───────────────────────────────────────────────────────────────────

class ProcessManager:
    """Holds all ManagedProcess instances; called by the HTTP layer."""

    def __init__(self, tasks: Dict[str, TaskConfig], log_root: Path, unit_id: str):
        self._log_root   = log_root
        self._unit_id    = unit_id
        self._dispatcher = EventDispatcher()
        self._procs: Dict[str, ManagedProcess] = {
            name: ManagedProcess(cfg, LogManager(log_root, name), self._dispatcher, unit_id)
            for name, cfg in tasks.items()
        }

    def _make_proc(self, cfg: TaskConfig) -> ManagedProcess:
        return ManagedProcess(
            cfg, LogManager(self._log_root, cfg.name), self._dispatcher, self._unit_id
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        for name, proc in self._procs.items():
            if proc.config.autostart:
                logger.info("Autostarting task '%s'", name)
                try:
                    await proc.start()
                except Exception as exc:
                    logger.error("Failed to autostart '%s': %s", name, exc)

    async def shutdown(self) -> None:
        running = [p for p in self._procs.values() if p.state == ProcessState.RUNNING]
        if running:
            logger.info("Stopping %d task(s) on shutdown ...", len(running))
            await asyncio.gather(*[p.stop() for p in running], return_exceptions=True)

    # ── Event stream (SSE) ────────────────────────────────────────────────────

    @property
    def dispatcher(self) -> "EventDispatcher":
        """Exposed so the scheduler/sequence-runner can fire lifecycle events and
        so the /events/stream endpoint can subscribe/unsubscribe SSE clients."""
        return self._dispatcher

    def is_running(self, name: str) -> bool:
        """True if the named task is currently running. False if unknown."""
        proc = self._procs.get(name)
        return proc is not None and proc.state == ProcessState.RUNNING

    def has_task(self, name: str) -> bool:
        return name in self._procs

    def get_config(self, name: str) -> TaskConfig:
        """Return the TaskConfig for a task (raises KeyError if unknown)."""
        return self._get(name).config

    def build_resume_request(self, name: str, offset_s: float) -> StartRequest:
        """
        Build a StartRequest that injects a resume offset into a resumable task,
        according to its resume_offset_mode. For non-resumable tasks or offset 0,
        returns an empty StartRequest (normal start).
        """
        req = StartRequest()
        if offset_s <= 0:
            return req
        cfg = self._get(name).config
        if not cfg.resumable:
            return req
        if cfg.resume_offset_mode == "env":
            req.env_overrides = {cfg.resume_offset_env: str(offset_s)}
        else:  # "arg"
            req.args = [cfg.resume_offset_flag, str(offset_s)]
        return req

    # ── Per-task operations ───────────────────────────────────────────────────

    def _get(self, name: str) -> ManagedProcess:
        if name not in self._procs:
            raise KeyError(f"Unknown task: '{name}'")
        return self._procs[name]

    async def start(self, name: str, request: Optional[StartRequest] = None,
                    source: str = "manual") -> ProcessStatus:
        proc = self._get(name)
        await proc.start(request)
        status = proc.status()
        if source == "manual":
            await self._fire_task_event("task_started", status)
        return status

    async def stop(self, name: str, source: str = "manual") -> ProcessStatus:
        proc = self._get(name)
        await proc.stop()
        status = proc.status()
        if source == "manual":
            await self._fire_task_event("task_stopped", status)
        return status

    async def restart(self, name: str, request: Optional[StartRequest] = None,
                      source: str = "manual") -> ProcessStatus:
        proc = self._get(name)
        if proc.state == ProcessState.RUNNING:
            await proc.stop()
        await proc.start(request)
        status = proc.status()
        if source == "manual":
            await self._fire_task_event("task_restarted", status)
        return status

    async def _fire_task_event(self, kind: str, status: ProcessStatus) -> None:
        """Emit a manual task lifecycle event to the stream subscribers."""
        event = TaskEvent(
            type=kind,
            unit_id=self._unit_id,
            task_name=status.name,
            state=status.state.value,
            pid=status.pid,
            at=_utcnow(),
        )
        asyncio.create_task(self._dispatcher.fire(event))

    def status(self, name: str) -> ProcessStatus:
        return self._get(name).status()

    def all_statuses(self) -> List[ProcessStatus]:
        return [p.status() for p in self._procs.values()]

    def get_log_manager(self, name: str) -> LogManager:
        return self._get(name).log

    def get_history(self, name: str) -> List[ExitRecord]:
        """Return the recent-exit ring buffer for a task (newest last)."""
        return list(self._get(name).history)

    def task_names(self) -> List[str]:
        return list(self._procs.keys())

    async def reload(self, new_tasks: Dict[str, TaskConfig]) -> dict:
        current  = set(self._procs.keys())
        incoming = set(new_tasks.keys())

        added: List[str]     = []
        removed: List[str]   = []
        skipped: List[str]   = []
        unchanged: List[str] = []

        for name in incoming - current:
            self._procs[name] = self._make_proc(new_tasks[name])
            logger.info("Reload: registered new task '%s'", name)
            added.append(name)

        for name in current - incoming:
            proc = self._procs[name]
            if proc.state == ProcessState.RUNNING:
                logger.warning(
                    "Reload: task '%s' removed from tasks.yaml but still running — skipping", name
                )
                skipped.append(name)
            else:
                del self._procs[name]
                logger.info("Reload: unregistered task '%s'", name)
                removed.append(name)

        for name in current & incoming:
            self._procs[name].config = new_tasks[name]
            unchanged.append(name)

        return {
            "added": added,
            "removed": removed,
            "skipped": skipped,
            "unchanged": unchanged,
        }