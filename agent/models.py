"""
Shared Pydantic models used by the API and process manager.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional, Dict, Any
from pydantic import BaseModel


class ProcessState(str, Enum):
    STOPPED  = "stopped"
    STARTING = "starting"
    RUNNING  = "running"
    STOPPING = "stopping"
    CRASHED  = "crashed"


class TaskConfig(BaseModel):
    """One registered task as read from tasks.yaml."""
    name: str                          # Unique identifier, e.g. "rx_flowgraph"
    description: str = ""
    command: list[str]                 # e.g. ["python3", "/opt/sdr-agent/scripts/rx.py"]
    working_dir: str = "/opt/sdr-agent"
    env: Dict[str, str] = {}           # Extra env vars merged into the process environment
    autostart: bool = False            # Start automatically when agent boots
    restart_on_crash: bool = False     # Restart if the process exits non-zero
    restart_delay_s: float = 3.0       # Seconds to wait before restarting
    # Crash-loop circuit breaker: stop auto-restarting if the task crashes more
    # than max_restarts times within restart_window_s. Prevents a permanently
    # broken task from looping (and spamming crash events) forever. 0 = unlimited.
    max_restarts: int = 5
    restart_window_s: float = 60.0

    # Resume support — for time-deterministic tasks like an attenuator ramp.
    # When a sequence is resumed, the agent injects the elapsed-seconds offset
    # so the script can pick up where it (or its peers) currently are.
    resumable: bool = False
    # How to pass the offset to the script. "arg" appends "<flag> <seconds>";
    # "env" sets an environment variable named by resume_offset_env.
    resume_offset_mode: str = "arg"        # "arg" | "env"
    resume_offset_flag: str = "--start-offset"   # used when mode == "arg"
    resume_offset_env: str = "SDR_START_OFFSET"  # used when mode == "env"



class ProcessStatus(BaseModel):
    """Runtime snapshot returned by the API."""
    name: str
    description: str
    state: ProcessState
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    started_at: Optional[str] = None   # ISO-8601
    stopped_at: Optional[str] = None
    restart_count: int = 0
    log_file: str = ""


class StartRequest(BaseModel):
    """Optional per-call overrides when starting a task."""
    env_overrides: Dict[str, str] = {}
    args: list[str] = []               # Extra CLI args appended to the command


class AgentInfo(BaseModel):
    """Returned by GET /info."""
    hostname: str
    unit_id: str                       # From config or hostname fallback
    agent_version: str
    python_version: str
    tasks: list[str]


class CrashEvent(BaseModel):
    """Event emitted on the SSE stream when a task crashes or exits unexpectedly."""
    type: str = "crash"                # Event type discriminator
    unit_id: str
    task_name: str
    task_description: str
    exit_code: Optional[int]
    started_at: Optional[str]
    crashed_at: str                    # ISO-8601
    restart_count: int
    last_log_lines: list[str]          # Last 20 lines from the log at time of crash


class ExitRecord(BaseModel):
    """One historical exit of a task, kept in a per-task ring buffer."""
    started_at: Optional[str]
    exited_at: str
    exit_code: Optional[int]
    was_crash: bool                    # True if exit_code != 0 and not an intentional stop


class SystemHealth(BaseModel):
    """Returned by GET /system — snapshot of the Pi's health."""
    unit_id: str
    cpu_percent: float                 # Overall CPU load %
    cpu_temp_c: Optional[float]        # CPU temperature in °C (None if unreadable)
    cpu_throttled: Optional[bool]      # True if the Pi reports current throttling
    mem_percent: float                 # RAM used %
    mem_used_mb: float
    mem_total_mb: float
    disk_percent: float                # Root filesystem used %
    disk_free_gb: float
    uptime_s: float                    # Seconds since boot
    load_avg: list[float]              # 1, 5, 15-minute load averages
    utc_now: str = ""                  # Agent's current UTC time (ISO-8601) for clock comparison
    clock_synced: Optional[bool] = None  # True if NTP reports the clock is synchronized
    clock_source: str = ""             # e.g. "systemd-timesyncd", "chrony", or "" if unknown


class SdrDevice(BaseModel):
    """One detected SDR device."""
    type: str = ""                     # e.g. "b200"
    serial: str = ""
    name: str = ""
    product: str = ""


class SdrStatus(BaseModel):
    """Returned by GET /sdr — result of probing for UHD/Ettus devices."""
    detected: bool
    device_count: int
    devices: list[SdrDevice]
    raw_output: str = ""               # Raw uhd_find_devices output for debugging
    error: str = ""                    # Populated if the probe command failed


# ── Scheduled events ───────────────────────────────────────────────────────────

class EventState(str, Enum):
    ARMED     = "armed"        # Waiting for start_at
    RUNNING   = "running"      # Started, waiting for stop_at
    COMPLETED = "completed"    # Stopped normally at stop_at
    CANCELLED = "cancelled"    # Cancelled before it started
    ABORTED   = "aborted"      # Stopped early (e.g. agent reboot mid-event) — fail-safe


class ScheduledEvent(BaseModel):
    """
    A timed task event: start the task at start_at, stop it at stop_at.
    All times are UTC ISO-8601. Persisted to events.json so it survives
    an agent restart.
    """
    id: str                            # Short unique id, e.g. "evt_a1b2c3"
    task_name: str
    start_at: str                      # UTC ISO-8601
    stop_at: str                       # UTC ISO-8601 (resolved from duration if given)
    state: EventState = EventState.ARMED
    created_at: str = ""
    started_actual: Optional[str] = None   # When the start actually fired
    stopped_actual: Optional[str] = None   # When the stop actually fired
    already_running: bool = False      # True if task was already running when start fired
    note: str = ""                     # Optional free-text label for the event


class CreateEventRequest(BaseModel):
    """
    Body for POST /events.
    Provide exactly one of stop_at or duration_s to define the end.
    """
    task_name: str
    start_at: str                      # UTC ISO-8601
    stop_at: Optional[str] = None      # UTC ISO-8601
    duration_s: Optional[float] = None # Alternative to stop_at
    note: str = ""


class PatchEventRequest(BaseModel):
    """Body for PATCH /events/{id} — set a new absolute stop time."""
    stop_at: str                       # UTC ISO-8601


class EventWebhook(BaseModel):
    """Event emitted on the SSE stream on scheduled-event lifecycle transitions."""
    type: str                          # "event_started" | "event_stopped" | "event_aborted" | "event_modified"
    unit_id: str
    event_id: str
    task_name: str
    start_at: str
    stop_at: str
    state: str
    at: str                            # ISO-8601 timestamp of this transition
    detail: str = ""                   # e.g. "stop time changed 09:05 → 09:25"


class TaskEvent(BaseModel):
    """
    Emitted on the event stream when a task is started/stopped/restarted by a
    DIRECT/manual action (the /tasks/{name}/... endpoints) — NOT when a task is
    driven by a sequence step (those already produce sequence_step events, so
    emitting here too would double-log). Lets the activity feed reflect that the
    Pi actually performed the action, not merely that a GUI requested it.
    """
    type: str                          # "task_started" | "task_stopped" | "task_restarted"
    unit_id: str
    task_name: str
    state: str                         # the task's process state after the action
    pid: Optional[int] = None
    at: str                            # ISO-8601 timestamp of this transition
    detail: str = ""


# ── Sequences ──────────────────────────────────────────────────────────────────

class StepAction(str, Enum):
    START = "start"
    STOP  = "stop"


class SequenceStep(BaseModel):
    """
    One action in a sequence, timed relative to an anchor.

    anchor = "start": offset_s is measured from on-air START (T0).
                      Warm-up steps use negative offsets; the amplifier-on
                      step is typically offset 0 (the on-air moment itself).
    anchor = "stop":  offset_s is measured from on-air STOP. The amplifier-off
                      step is offset 0 here; cool-down steps use positive
                      offsets and move automatically when the stop is extended.
    """
    anchor: str = "start"              # "start" | "stop"
    offset_s: float                    # relative to the chosen anchor
    action: StepAction
    task_name: str
    # Extra CLI args appended to the base task's command when this step STARTs it,
    # so one registered task (e.g. a set-gain script) can be reused across steps
    # with different values instead of registering one task per value. Ignored for
    # stop actions. Merged after any resume-offset args.
    args: list[str] = []
    inject_resume_offset: bool = False # If true and resuming, pass the offset to this task's start


class Sequence(BaseModel):
    """
    A reusable, relative-timed choreography on ONE unit. Stored on the Pi in
    sequences.json. Defines what happens around a single on-air window:
    warm-up (start-anchored, negative offsets), the on-air start at T0, the
    on-air stop, and cool-down (stop-anchored, positive offsets).
    """
    id: str
    name: str
    description: str = ""
    steps: list[SequenceStep]


class CreateSequenceRequest(BaseModel):
    name: str
    description: str = ""
    steps: list[SequenceStep]


class SequenceState(str, Enum):
    ARMED     = "armed"        # waiting for the first step to fire
    RUNNING   = "running"      # at least one step has fired, not yet finished
    COMPLETED = "completed"    # all steps fired normally
    CANCELLED = "cancelled"    # cancelled before first step
    ABORTED   = "aborted"      # stopped early (reboot mid-run, manual abort, panic)


class StepFire(BaseModel):
    """Record of a single step firing within a run."""
    anchor: str
    offset_s: float
    action: str
    task_name: str
    fire_at: str                       # absolute UTC the step is scheduled to fire
    fired_actual: Optional[str] = None # when it actually fired (None if not yet / aborted before)
    resume_offset_s: Optional[float] = None  # offset injected, if any
    args: list[str] = []               # extra CLI args for this step's start (see SequenceStep.args)


class SequenceRun(BaseModel):
    """
    An armed/executing instance of a Sequence. Persisted to sequence_runs.json
    so it survives an agent restart (fail-safe = abort on reboot).
    """
    id: str                            # "run_xxxxxxxx"
    sequence_id: str
    sequence_name: str
    state: SequenceState = SequenceState.ARMED
    on_air_at: str                     # absolute UTC of on-air START (T0)
    on_air_end: Optional[str] = None   # absolute UTC of on-air STOP; None if open-ended
    open_ended: bool = False           # True = no stop; runs on-air until aborted
    created_at: str = ""
    started_actual: Optional[str] = None
    stopped_actual: Optional[str] = None
    resume_offset_s: float = 0.0       # 0 for a fresh run; >0 if resumed
    note: str = ""
    steps: list[StepFire] = []         # resolved absolute fire times for this run
    # Plan stamp — lets any GUI regroup runs into their plan after a restart/swap.
    plan_id: str = ""
    plan_name: str = ""


class ArmSequenceRequest(BaseModel):
    """
    Body for POST /sequences/{id}/arm.
    on_air_at is the absolute UTC time the on-air window should START (T0).
    For a fixed-window run, provide exactly one of on_air_end or on_air_duration_s.
    For an open-ended run (test/manual), set open_ended=True and omit both — the
    run fires the start-anchored steps and stays on-air until aborted.
    resume_offset_s, if > 0, is injected into resumable steps so they begin
    partway through (e.g. resuming a ramp 40 minutes in).
    plan_id / plan_name stamp the resulting run so a GUI can group it into a plan.
    """
    on_air_at: str                     # UTC ISO-8601 — when RF should go live
    on_air_end: Optional[str] = None   # UTC ISO-8601 — when RF should go off
    on_air_duration_s: Optional[float] = None  # alternative to on_air_end
    open_ended: bool = False           # True = no stop; run until aborted
    resume_offset_s: float = 0.0
    note: str = ""
    plan_id: str = ""
    plan_name: str = ""


class PatchSequenceRunRequest(BaseModel):
    """Body for PATCH /sequence-runs/{id} — move the on-air STOP to a new absolute UTC time."""
    on_air_end: str                    # UTC ISO-8601


class SequenceWebhook(BaseModel):
    """Event emitted on the SSE stream on sequence-run lifecycle transitions."""
    type: str                          # sequence_started | sequence_on_air | sequence_step | sequence_off_air | sequence_stopped | sequence_aborted | sequence_modified
    unit_id: str
    run_id: str
    sequence_name: str
    on_air_at: str
    on_air_end: Optional[str] = None   # None for open-ended runs
    state: str
    at: str
    detail: str = ""


# ── Panic / emergency stop ──────────────────────────────────────────────────────

class PanicResult(BaseModel):
    """Returned by POST /panic — what was stopped/cancelled."""
    unit_id: str
    tasks_stopped: list[str]
    events_cancelled: list[str]
    runs_aborted: list[str]
    at: str