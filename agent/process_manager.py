"""
ProcessManager — owns the lifecycle of every registered task.

Each task runs as a real OS subprocess.  stdout and stderr are merged
and written to the task's log file, with PYTHONUNBUFFERED set so print()
output flushes live instead of block-buffering.  Crash-restart logic runs
inside an asyncio task so it never blocks the HTTP server.

Event support: when a task crashes the manager fires a CrashEvent to all
connected SSE subscribers (best-effort, non-blocking, stdlib-only).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import socket
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic as _monotonic
from typing import Deque, Dict, List, Optional

from . import config as _agentcfg   # module import; container methods use a local `cfg`
from . import calibration as _calib
from . import system as _sysmon
from . import tune_log as _tune_log
from . import cmdargs as _cmdargs
from .argspec import extract_params
from paramkit import rf as _rf
from paramkit import txstage as _txstage
from paramkit import txhealth as _txhealth
from .log_manager import LogManager
from .models import (
    CrashEvent, ExitRecord, ProcessState, ProcessStatus,
    StartRequest, TaskConfig, TaskEvent, TaskHealth, TaskHealthEvent,
)

try:
    from paramkit.live import CTRL_SOCK_ENV
except Exception:   # noqa: BLE001 — paramkit always present on a real unit
    CTRL_SOCK_ENV = "SDR_CTRL_SOCK"

logger = logging.getLogger(__name__)


def _ctrl_sock_path(name: str) -> str:
    """A short, per-task Unix-socket path for live-parameter control. The name is
    sanitised (it may contain '/'), length-capped so the full path stays under the
    AF_UNIX ~108-byte limit, and — when sanitising or the cap changed anything —
    suffixed with a short hash of the FULL name so two long or slash-bearing names
    can't collide onto the same socket (which would cross-wire their live tuning)."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if safe == name and len(safe) <= 40:
        stem = safe or "task"
    else:
        import hashlib
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        stem = f"{safe[:40] or 'task'}-{digest}"
    return str(_agentcfg.CTRL_DIR / f"{stem}.sock")


def _inject_calibration(env: dict, task_name: str) -> None:
    """If a task opted into power calibration — its env sets SDR_CAL_SIGNAL_ID to
    the script's CAL_SIGNAL_ID — resolve the unit's calibration for that signal and
    point the task at a per-run resolved artifact via SDR_CALIBRATION_FILE. Mutates
    ``env`` in place.

    Fail-safe (docs/calibration.md §8): a task that didn't opt in, or a unit with no
    calibration document, is a no-op (the script uses its baked defaults). A document
    that lacks THIS signal is a soft miss — logged, then fall back. A broken or unsafe
    document raises CalibrationError, which the caller turns into an aborted start
    ('refuse to transmit')."""
    signal_id = env.get(_agentcfg.CAL_SIGNAL_ID_ENV)
    if not signal_id:
        return                                       # task didn't opt in
    # Optional transmit frequency (Hz) for folding the representative curve/bounds; a
    # bad value is ignored (the doc's center_freq_hz still applies).
    freq_hz = None
    raw_freq = env.get(_agentcfg.CAL_FREQ_HZ_ENV)
    if raw_freq:
        try:
            freq_hz = float(raw_freq)
        except (TypeError, ValueError):
            logger.warning("Task '%s': ignoring non-numeric %s=%r",
                           task_name, _agentcfg.CAL_FREQ_HZ_ENV, raw_freq)
    try:
        artifact = _calib.resolve_public(
            _agentcfg.CALIBRATION_DOC, _agentcfg.CALIBRATION_DEFAULTS,
            signal_id, unit_type=_agentcfg.UNIT_TYPE,
            components_path=_agentcfg.CALIBRATION_COMPONENTS, freq_hz=freq_hz)
    except _calib.SignalNotCalibrated as exc:
        logger.warning("Task '%s': %s — using the script's baked-in calibration "
                       "defaults", task_name, exc)
        return
    if artifact is None:
        return                                       # no per-unit calibration doc
    # Sanitised, length-capped name for readability, plus a short hash of the FULL
    # task name so two tasks that sanitise/truncate to the same string never share one
    # artifact file (which would point a task at another task's resolved curve).
    import hashlib
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", task_name)[:60] or "task"
    digest = hashlib.sha1(task_name.encode("utf-8")).hexdigest()[:8]
    _agentcfg.CAL_RUN_DIR.mkdir(parents=True, exist_ok=True)
    path = _agentcfg.CAL_RUN_DIR / f"{safe}-{digest}.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    env[_agentcfg.CALIBRATION_FILE_ENV] = str(path)
    logger.info("Task '%s': resolved calibration for signal '%s' → %s",
                task_name, signal_id, path)


def _ctrl_rpc(path: str, req: dict, timeout: float) -> dict:
    """Blocking one-shot request/response against a script's control socket.
    Raises RuntimeError with a friendly message when the socket isn't there (the
    task doesn't use paramkit.live, or hasn't bound yet)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
    except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
        raise RuntimeError(
            "task does not expose live parameters (or isn't ready yet)") from exc
    try:
        f = s.makefile("rwb")
        f.write((json.dumps(req) + "\n").encode("utf-8"))
        f.flush()
        line = f.readline()
        if not line:
            raise RuntimeError("no response from the task's control socket")
        return json.loads(line.decode("utf-8"))
    finally:
        s.close()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _script_prefix(command: list) -> list:
    """The [interpreter, …, script] prefix of a command — up to and including the
    first argument ending in .py. Falls back to the first element (the interpreter)
    if there's no .py. Used to replace a task's trailing args with a step's."""
    for i, a in enumerate(command):
        if isinstance(a, str) and a.endswith(".py"):
            return list(command[: i + 1])
    return list(command[:1])


def _resolve_script_path(cmd: list) -> list:
    """If a task's script argument no longer sits directly at its command path, find the file by
    basename. Two cases: (1) it was filed into an organizational subfolder (search under the
    command path's own dir); (2) the library moved to the persistent SCRIPTS_DIR (STATE_DIR/scripts)
    but the task was baked with the OLD release-local path — after an update that path resolves into
    the new release (bundled defaults only), so fall back to searching SCRIPTS_DIR. Scripts keep
    their basename identity, so a launch command needn't change when a script moves."""
    out = list(cmd)
    for i, a in enumerate(out):
        if isinstance(a, str) and a.endswith(".py"):
            if not os.path.isfile(a):
                base = os.path.basename(a)
                found = None
                for root in (os.path.dirname(a) or ".", str(_agentcfg.SCRIPTS_DIR)):
                    if root and os.path.isdir(root):
                        for dirpath, _dirs, files in os.walk(root):
                            if base in files:
                                found = os.path.join(dirpath, base)
                                break
                    if found:
                        break
                if found:
                    out[i] = found
            break
    return out


def _ensure_paramkit_on_path(env: dict) -> dict:
    """Prepend BASE_DIR to the launch env's PYTHONPATH so a transmit script can ``import paramkit``
    (which ships inside the release, next to the agent) NO MATTER where the script file lives. The
    deployed library now sits in the persistent SCRIPTS_DIR, no longer beside paramkit, so a script's
    own 'look next to me' sys.path guess would miss it — this makes the import robust either way."""
    base = str(_agentcfg.BASE_DIR)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = base + (os.pathsep + existing if existing else "")
    return env


def _launch_env_pins(task_dir) -> dict:
    """RF-fault prevention (Phase 0, docs/rf-fault-recovery.md §3.4/§3.6/§3.7): the launch-env PINS
    applied to every transmit task. Merged ABOVE ambient os.environ but BELOW the task's cfg.env /
    request env_overrides, so each is a default the task/operator can still override:
      * HOME           — a stable, writable home so GNU Radio's ~/.gnuradio handling is deterministic.
      * GR vmcircbuf   — pin the buffer backend the way GR ACTUALLY reads it (review fix #1): write its
                         `vmcircbuf_default_factory` pref FILE (a factory name) under the task HOME at
                         both the 3.8 (`~/.gnuradio/prefs`) and 3.10 (`~/.config/gnuradio/prefs`)
                         locations, and pin GR_PREFS_PATH so 3.10 can't be redirected by an ambient
                         XDG_CONFIG_HOME. The GR_CONF_* env var is exported too, as documentation only —
                         GR's vmcircbuf code never consults it (see config.GR_VMCIRCBUF_FACTORY).
      * UHD file log   — capture the FPGA image load / UHD errors to a PER-TASK file while the console
                         stays off (UHD_LOG_CONSOLE_LEVEL is left untouched). Needs the task's log dir.
    Each key is omitted when its config value is blank (so nothing is forced when unconfigured)."""
    pins: dict = {}
    if _agentcfg.TASK_HOME:
        pins["HOME"] = str(_agentcfg.TASK_HOME)
    if _agentcfg.GR_VMCIRCBUF_FACTORY:
        pins[_agentcfg.GR_VMCIRCBUF_ENV] = _agentcfg.GR_VMCIRCBUF_FACTORY
        home = pins.get("HOME") or os.environ.get("HOME", "")
        if home:
            _pin_gr_vmcircbuf_pref(home, _agentcfg.GR_VMCIRCBUF_FACTORY)
            pins[_agentcfg.GR_PREFS_PATH_ENV] = str(Path(home) / ".config" / "gnuradio")
    if _agentcfg.UHD_LOG_FILE_LEVEL and task_dir is not None:
        pins[_agentcfg.UHD_LOG_FILE_ENV] = str(Path(task_dir) / _agentcfg.UHD_LOG_FILE_NAME)
        pins[_agentcfg.UHD_LOG_FILE_LEVEL_ENV] = _agentcfg.UHD_LOG_FILE_LEVEL
    return pins


def gr_vmcircbuf_factory_name(token: str) -> str:
    """The GR factory NAME for a config token: 'mmap_shm_open' → 'gr::vmcircbuf_mmap_shm_open_factory'
    (a full name is passed through). That name string is what GR matches the pref file against."""
    t = (token or "").strip()
    return t if t.startswith("gr::") else f"gr::vmcircbuf_{t}_factory"


def _pin_gr_vmcircbuf_pref(home: str, token: str) -> str:
    """Write GR's `vmcircbuf_default_factory` pref file under `home` at BOTH the locations the two
    shipped GR generations read (3.8: ~/.gnuradio/prefs; 3.10: ~/.config/gnuradio/prefs, which is
    also what the pinned GR_PREFS_PATH points at). Idempotent (rewritten only when the content
    differs), best-effort (a failure is logged, never raised — the launch proceeds). Returns the
    factory name written. Review fix #1."""
    name = gr_vmcircbuf_factory_name(token)
    for d in (Path(home) / ".config" / "gnuradio" / "prefs", Path(home) / ".gnuradio" / "prefs"):
        try:
            d.mkdir(parents=True, exist_ok=True)
            p = d / _agentcfg.GR_VMCIRCBUF_PREF_KEY
            # GR 3.10 matches the file's bytes EXACTLY (a trailing newline = no match → it probes and
            # persists sysv_shm), so compare bytes, and write atomically (tmp + replace) so a task
            # allocating its first buffer never reads a truncated file (re-review findings O3/O4).
            want = name.encode()
            if not p.exists() or p.read_bytes() != want:
                tmp = p.with_name(p.name + ".tmp")
                tmp.write_bytes(want)
                os.replace(tmp, p)
        except OSError as exc:
            logger.warning("could not pin the GR vmcircbuf backend at %s: %s", d, exc)
    return name


def _sweep_shm_orphans() -> int:
    """Reclaim dead-PID /dev/shm staging orphans (paramkit.txstage 'sdrtx-' dirs left by a task the
    agent SIGKILLed / that crashed). Best-effort — hygiene must never break a launch or teardown;
    disabled with SDR_SHM_SWEEP=0. Called at boot, before each managed launch, and after each task
    ends (docs/rf-fault-recovery.md §3.5)."""
    if not _agentcfg.SHM_SWEEP_ENABLED:
        return 0
    try:
        n = _txstage.sweep_orphans()
        if n:
            logger.info("Swept %d orphaned /dev/shm staging dir(s)", n)
        return n
    except Exception as exc:   # noqa: BLE001 — never let cleanup break the caller
        logger.debug("shm sweep skipped: %s", exc)
        return 0


def _build_command(command: list, args: list, replace: bool) -> list:
    """Build the launch command. replace=True → [interpreter, script, *args]
    (args are the complete set); replace=False → command + args (append)."""
    if replace and args:
        cmd = _script_prefix(command) + list(args)
    else:
        cmd = list(command) + list(args)
    return _resolve_script_path(_resolve_exe(cmd))


# How long a stop() that lands during a launch waits for the spawn before giving up on it. A spawn
# takes milliseconds; the bound only ever measures a launch stranded by an exception (finding C4,
# now settled by start() itself), so it can be short.
_STARTING_STOP_WAIT_S = 10.0

# Absolute-power flags the fleet's transmit scripts use (mirror of the client's
# ui.param_form._POWER_FLAGS); a launch/tune setting one drives the active components too.
_POWER_FLAGS = ("--power", "-Power")
# How long to wait for a one-shot active-component set (e.g. an attenuator) to finish before
# letting the transmit proceed — so the component is in position first, without a hung set
# blocking a whole test campaign.
_ACTIVE_SET_TIMEOUT_S = 5.0


def _power_from_command(cmd) -> Optional[float]:
    """The absolute --power (dBm) a launch command sets, or None."""
    val = None
    for i, a in enumerate(cmd or []):
        if a in _POWER_FLAGS and i + 1 < len(cmd):
            try:
                val = float(cmd[i + 1])
            except (TypeError, ValueError):
                pass
    return val


def _rf_on_from_command(cmd, gate: dict) -> bool:
    """Whether a launch command leaves the RF output gate ON: the gate flag's value on the command
    line if present, else the gate's schema default, else on (a launch that never set --rf)."""
    flags = {str(f) for f in (gate.get("flags") or [])}
    last = None
    for i, a in enumerate(cmd or []):
        if str(a) in flags and i + 1 < len(cmd):
            last = cmd[i + 1]
    if last is not None:
        return _rf.is_on(last)
    default = gate.get("default")
    return _rf.is_on(default) if default is not None else True


def _freq_from_command(cmd, spec: Optional[dict]) -> Optional[float]:
    """The transmit frequency (Hz) a launch command sets: the script's CAL_FREQ_PARAM value on the
    command line (else its schema default), scaled by the unit the param is declared in — i.e.
    the frequency the script will fold its own calibration at. None when the script declares no
    frequency param. The agent realizes everything it commands for the task (the attenuator) at
    THIS frequency, so the SDR gain the script sets and the attenuation the agent sets belong to
    the same realization."""
    if not spec:
        return None
    dest = spec.get("calibration_freq_param")
    if not dest:
        return None
    param = next((p for p in (spec.get("params") or []) if p.get("dest") == dest), None)
    flags = {str(f) for f in ((param or {}).get("flags") or [])}
    last = None
    for i, a in enumerate(cmd or []):
        if str(a) in flags and i + 1 < len(cmd):
            last = cmd[i + 1]
    return _tune_log.freq_hz_of(spec, {dest: last} if last is not None else {})


# GNU Radio's USRP sink underflow report (gr-uhd `usrp_sink_impl`, one line per report window):
#   usrp_sink :error: In the last 750 ms, 7187 underflows occurred.
# Captures (window_ms, underflow_count). Matched on the scanned log text (stdout+stderr merged).
_UNDERFLOW_RE = re.compile(r"in the last\s+(\d+)\s*ms,\s*(\d+)\s+underflows?\s+occurred", re.IGNORECASE)


def _underflow_reports(text: str) -> List[tuple]:
    """Every GR underflow report in `text`, in order, as (window_ms, count) pairs."""
    return [(int(m.group(1)), int(m.group(2))) for m in _UNDERFLOW_RE.finditer(text)]


def _last_clock_origin(text: str) -> Optional[float]:
    """The value of the LAST `CLOCK origin=<unix seconds>` marker in `text`, or None."""
    m = None
    for m in re.finditer(re.escape(_txhealth.CLOCK_MARKER) + r"\s*([0-9]+(?:\.[0-9]+)?)", text or ""):
        pass
    if m is None:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return None


def _fmt_num(v: float) -> str:
    """A numeric CLI argument value: whole numbers without a trailing .0 so int-typed
    argparse params accept them (e.g. 60.0 → '60', 0.25 → '0.25')."""
    return f"{float(v):g}"


def _resolve_exe(cmd: list) -> list:
    """Resolve a bare executable name (no slash) to an absolute path via PATH.

    Default asyncio searches PATH for argv[0], but uvloop/libuv does NOT — so a
    task command like ['python3', ...] launches fine under plain asyncio yet fails
    with FileNotFoundError once uvicorn[standard] pulls in uvloop (as on the X410).
    Resolving here makes both event loops behave identically. A command that
    already gives a path (contains '/') is left untouched; an unresolved bare name
    is left as-is so the existing FileNotFoundError still surfaces a clear error."""
    if not cmd:
        return cmd
    exe = cmd[0]
    if exe and "/" not in exe:
        resolved = shutil.which(exe)
        if resolved:
            return [resolved] + list(cmd[1:])
    return cmd


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
        # Path of this run's live-parameter control socket (set on start).
        self._ctrl_sock: Optional[str] = None

        # Set when a manual stop is requested, so an in-progress restart-delay in
        # the watcher aborts instead of relaunching (lets you stop a crash-looping
        # task). Cleared on an intentional start.
        self._stop_requested = False
        # Set ONLY by an operator/external stop (not the internal RF auto-drop), so a standalone
        # auto-restart-on-fault relaunch honours an operator stopping the faulted task, while the
        # auto-drop stop() that frees the channel does NOT abort its own recovery. Cleared on start.
        self._operator_stop_requested = False
        # Timestamps (monotonic) of recent auto-restarts, for the crash-loop
        # circuit breaker.
        self._restart_times: Deque[float] = deque(maxlen=50)
        # True once the breaker has tripped; surfaced so the UI can show it.
        self.restart_giving_up = False

        # Ring buffer of recent exits (newest appended last)
        self.history: Deque[ExitRecord] = deque(maxlen=10)

        # ── RF-fault health (Phase 1, docs/rf-fault-recovery.md §5.2/§5.3) ────────
        # A SEPARATE axis from `state` — a halted GR flowgraph is still process-RUNNING. Plain
        # attributes (the ManagedProcess no-lock convention); the health watchdog writes them,
        # status() reads them. `health` is a TaskHealth value string.
        self.health: str = TaskHealth.OK.value
        self.health_detail: str = ""
        self.last_output_at: Optional[str] = None
        self._resource_snapshot = None          # FaultSnapshot captured at the fault
        # Per-task incremental log-scan cursor for read_since (reset each start; a rotation is
        # detected by the inode so a stale offset can't read the wrong run).
        self._log_offset: int = 0
        self._log_inode = None
        # Fire the health event + couple the run ONCE per run (a benign single log mention can't
        # re-alarm); cleared on the next start so a relaunch re-arms detection.
        self._fault_alarmed: bool = False
        # Async run-coupling hook (task_name, detail) -> None, set by the ProcessManager so a
        # detected fault can reach the SequenceRunner (stamp run.fault, stop tuning the dead task).
        self._fault_hook = None

        # ── STANDALONE auto-restart-on-fault (Phase 3b, §7.1/§14e) ────────────────
        # Owned-query () -> set[task_name]: task names currently owned by an ACTIVE run, so a
        # run-owned fault is left to the run policy (never also relaunched here → no double-transmit).
        # Set by the ProcessManager (from the SequenceRunner) via set_owned_query.
        self._owned_query = None
        # Launch hook (name, request) -> awaitable — the FULL manager launch path (positions the
        # attenuator via _gate_precommand + carries the launch carrier), so an auto-restart relaunch
        # is a faithful reproduction, not a bare ManagedProcess.start that skips those. Set by the
        # ProcessManager; None in isolation → fall back to a direct self.start.
        self._launch_hook = None
        # The last StartRequest this task was launched with, so a standalone auto-restart reproduces
        # the EXACT parameters it faulted with (the Run… form may launch with custom args). None = a
        # bare start (the task's configured command). Its per-launch auto_restart_on_fault override
        # (None = fall back to the TaskConfig default) is captured on each start.
        self._last_request: Optional[StartRequest] = None
        self._auto_restart_override: Optional[bool] = None
        # Monotonic timestamps of recent standalone fault-restarts, for the rolling budget breaker.
        self._fault_restart_times: Deque[float] = deque(maxlen=50)
        # True once the fault-restart budget has tripped (surfaced for the UI / logs).
        self.fault_restart_giving_up = False
        # Guards a standalone relaunch in flight (delay window), so the two detection paths (the
        # non-intentional EXIT in _watch and the watchdog stop() in _scan_task_health) can never both
        # relaunch one fault. Cleared once the attempt finishes (a genuinely new fault re-arms it).
        self._fault_restart_inflight = False
        # Holds the detached wedge-path relaunch task (Layer 2) so it isn't garbage-collected mid-flight.
        self._relaunch_task: Optional[asyncio.Task] = None
        # Set the instant create_subprocess_exec returns, so a stop() that lands while the launch is in
        # flight (state STARTING) can wait for the process and actually kill it (review fix #10).
        self._spawned = asyncio.Event()
        # True while the watcher/relaunch task is sleeping a restart or settle DELAY — the only phase a
        # stop() may cancel it in; never mid-_flag_rf_fault (review fix #13).
        self._in_restart_delay = False
        # The manager's shutdown flag: a relaunch that wakes after the agent began tearing down stands
        # down instead of spawning a transmitter into the teardown (review fix #12).
        self._shutdown_flag: Optional[asyncio.Event] = None
        # () -> set[task_name]: tasks claimed ONLY by a not-yet-fired launch of an active run (a subset
        # of the owned-query). A standalone relaunch WAITS such a claim out (bounded) rather than
        # standing down for good (review fix #28). Set by the ProcessManager.
        self._pending_query = None
        # When the script reported HEALTH state=transmitting (paramkit.txhealth) — the radio is up.
        # A recovery breaker's healthy-settle clock starts here, not at spawn (review fix #19).
        self.transmitting_at: Optional[str] = None
        # The ABSOLUTE instant (Unix seconds) the script's own timeline last (re)started, as the script
        # REPORTED it (txhealth.CLOCK_MARKER, read by the watchdog scan). A restart bakes it back so a
        # time-dependent script resumes exactly, whatever the launch latency (§14j). None = not reported.
        self.clock_origin: Optional[float] = None
        # Sustained-underflow streak (§14m): the last scan instant that carried a GR underflow report,
        # and the streak's covered window / report / underflow totals. Reset on every start.
        self._uf_last: Optional[float] = None
        self._uf_ms: int = 0
        self._uf_reports: int = 0
        self._uf_count: int = 0
        # Live-parameter values applied to THIS run by set_params ({dest: value}), so a standalone
        # auto-restart relaunches at the LIVE state (a muted / lowered task comes back muted / lowered),
        # not the launch request (review fix #2). Cleared on start.
        self._live_applied: dict = {}
        self._live_applied_at: dict = {}      # {dest: ISO instant the value was applied}

    # ── Public interface ──────────────────────────────────────────────────────

    async def start(self, request: Optional[StartRequest] = None) -> None:
        if self.state in (ProcessState.RUNNING, ProcessState.STARTING):
            raise RuntimeError(f"Task '{self.config.name}' is already {self.state}")
        # A slot whose previous process is STILL ALIVE (state STOPPING — a SIGTERM in its ≤10 s grace,
        # or any not-yet-reaped process) must not take a second launch: the two would transmit at once
        # and stop()'s continuation would then SIGKILL/clean up the WRONG one (re-review finding C1).
        # The caller waits it out (ProcessManager.restart) or reports "still stopping".
        if self.state == ProcessState.STOPPING or (
                self._proc is not None and self._proc.returncode is None):
            raise RuntimeError(f"Task '{self.config.name}' is still stopping — retry once it has stopped")

        # An explicit start clears any prior stop request and breaker trip.
        self._stop_requested = False
        self._operator_stop_requested = False
        self.restart_giving_up = False
        self.fault_restart_giving_up = False   # a fresh start re-arms the standalone auto-restart breaker
        # A fresh run re-arms fault detection: clear health + the alarm latch, and reset the
        # log-scan cursor (start() rotates current.log below, so the new run reads from a new inode).
        self.health = TaskHealth.OK.value
        self.health_detail = ""
        self._resource_snapshot = None
        self._fault_alarmed = False
        self._log_offset = 0
        self._log_inode = None
        self._spawned.clear()
        self.transmitting_at = None
        self.clock_origin = None
        self._reset_underflow_streak()
        self._live_applied = {}
        self._live_applied_at = {}
        self.state = ProcessState.STARTING
        req = request or StartRequest()
        # Remember this launch so a standalone auto-restart-on-fault can reproduce it exactly, and
        # capture its per-launch override of the auto-restart flag (None = use the TaskConfig default).
        self._last_request = request
        self._auto_restart_override = req.auto_restart_on_fault

        try:
            await self._launch(req)
        except BaseException:
            # ANY failure/cancel inside the launch window (a missing cwd/interpreter, ENOMEM, a log
            # OSError, a cancelled relaunch task mid-exec) used to leave the slot STARTING forever:
            # every later stop() waited 30 s on _spawned, abort/PANIC/shutdown sweeps stalled on it and
            # the task could never be started again (re-review finding C4). Settle it: kill a child
            # that did get spawned, release the waiters, clean up, and re-raise.
            await self._settle_failed_launch()
            raise

    async def _settle_failed_launch(self) -> None:
        p = self._proc
        if p is not None and p.returncode is None:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                await asyncio.wait_for(p.wait(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):   # noqa: BLE001
                pass
        self._spawned.set()
        try:
            await self._cleanup()
        except Exception:                          # noqa: BLE001
            self.state = ProcessState.STOPPED

    async def _launch(self, req: StartRequest) -> None:
        cmd = _build_command(self.config.command, req.args, req.replace_args)
        # RF-fault prevention pins sit ABOVE ambient os.environ but BELOW the task's own cfg.env /
        # request env_overrides — deterministic defaults the task can still override.
        env = {**os.environ, **_launch_env_pins(self.log.task_dir),
               **self.config.env, **req.env_overrides}
        _ensure_paramkit_on_path(env)   # scripts live in the persistent dir now — keep `import paramkit` working
        # stdout is redirected to a file, so Python would block-buffer print()
        # output (appearing only in ~8 KB bursts or at exit) while stderr/logging
        # stays prompt — the "prints sometimes show up, sometimes not" symptom.
        # Force unbuffered output so both streams flush live. A task that really
        # wants buffering can still override this in its env.
        env.setdefault("PYTHONUNBUFFERED", "1")

        # Resolve per-unit power calibration for this task (if it opted in). A hard,
        # unsafe calibration error aborts the start — refuse to transmit rather than
        # risk over-driving; a soft miss falls back to the script's baked defaults.
        try:
            _inject_calibration(env, self.config.name)
        except _calib.CalibrationError as exc:
            self.state = ProcessState.STOPPED
            self._spawned.set()      # release a stop() waiting on the launch (nothing was spawned)
            raise RuntimeError(
                f"Refusing to start '{self.config.name}': calibration error: {exc}"
            ) from exc

        # Provision a control socket for live-parameter tuning. paramkit.live binds
        # it iff the script declares live params and calls script.live_control();
        # otherwise nothing listens and set-params calls report cleanly that the
        # task exposes none. The path is per-task and ephemeral.
        try:
            _agentcfg.CTRL_DIR.mkdir(parents=True, exist_ok=True)
            self._ctrl_sock = _ctrl_sock_path(self.config.name)
            env[CTRL_SOCK_ENV] = self._ctrl_sock
        except OSError as exc:
            logger.warning("Could not prepare control socket dir for '%s': %s",
                           self.config.name, exc)
            self._ctrl_sock = None

        self.log.rotate()
        self.log.rotate_uhd(_agentcfg.UHD_LOG_FILE_NAME)   # per-run UHD file log (review fix #23)
        self.log.cleanup()   # prune old archives so the SD card never fills
        self._log_fh = self.log.open_for_write()

        # Reclaim any dead-PID /dev/shm staging orphan before we build a fresh flowgraph, so a
        # prior hard-killed task can't starve this launch's GR buffers (docs/rf-fault-recovery §3.5).
        _sweep_shm_orphans()

        logger.info("Starting task '%s': %s", self.config.name, cmd)

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=self._log_fh,
            stderr=self._log_fh,
            cwd=self.config.working_dir,
            env=env,
            start_new_session=True,
        )

        self._spawned.set()
        if self._stop_requested:
            # A stop() landed while the exec was in flight (review fix #10): it could not signal a
            # process that did not exist yet, so honour it now — kill what we just spawned, clean up,
            # and refuse to report a start. Without this the process ran on with _stop_requested set,
            # its log fh closed and its control socket unlinked.
            logger.info("Task '%s' stopped during its launch — killing pid %s", self.config.name, self._proc.pid)
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await self._proc.wait()
            await self._cleanup()
            raise RuntimeError(f"Task '{self.config.name}' was stopped during its launch")

        self.pid        = self._proc.pid
        self.state      = ProcessState.RUNNING
        self.started_at = _utcnow()
        self.stopped_at = None
        self.exit_code  = None

        self._watcher_task = asyncio.create_task(
            self._watch(), name=f"watch-{self.config.name}"
        )

    async def stop(self, timeout: float = 10.0, *, operator: bool = True) -> None:
        # Always record the stop request first — this breaks an in-progress
        # restart-delay in the watcher (the crash-loop case), even if there's no
        # live process to signal right now.
        self._stop_requested = True
        # An operator/external stop also blocks a standalone auto-restart-on-fault relaunch; the
        # internal RF auto-drop passes operator=False so it doesn't cancel its own recovery.
        if operator:
            self._operator_stop_requested = True
            # An operator stop also cancels a DETACHED (wedge-path) relaunch still waiting out its
            # settle delay — otherwise the relaunch fired anyway once the delay passed (review fix #11).
            t = self._relaunch_task
            if t is not None and not t.done():
                t.cancel()

        if self.state == ProcessState.STARTING:
            # The launch is in flight (start() is awaiting create_subprocess_exec). Wait for the spawn
            # so there is a process to signal — start() itself kills the child it just spawned when it
            # sees _stop_requested, so after the wait either the task is RUNNING (signal it below) or
            # start() already cleaned up / failed (nothing to do). Review fix #10.
            try:
                await asyncio.wait_for(self._spawned.wait(), timeout=_STARTING_STOP_WAIT_S)
            except asyncio.TimeoutError:
                logger.warning("Task '%s': launch did not spawn within %.0f s of a stop",
                               self.config.name, _STARTING_STOP_WAIT_S)
                if self.state == ProcessState.STARTING and (
                        self._proc is None or self._proc.returncode is not None):
                    self.state = ProcessState.STOPPED    # a stranded launch: settle it (finding C4)
            if self.state != ProcessState.RUNNING:
                return

        if self.state != ProcessState.RUNNING:
            # Task isn't running. It may be mid-crash-loop (state CRASHED, watcher sleeping before a
            # restart / settle delay): cancel that watcher so it doesn't relaunch, and settle the state
            # to STOPPED. A watcher that is NOT in its delay is inside the exit handling itself
            # (_flag_rf_fault: snapshot + alarm + run coupling) — cancelling it there silently lost the
            # alarm and the coupling (review fix #13); it honours _stop_requested on its own afterwards.
            if self.state == ProcessState.CRASHED:
                w = self._watcher_task
                if w is not None and not w.done() and self._in_restart_delay:
                    w.cancel()
                    self.state = ProcessState.STOPPED
                    logger.info("Task '%s' crash-restart cancelled by stop", self.config.name)
                elif w is None or w.done():
                    self.state = ProcessState.STOPPED
                else:
                    logger.info("Task '%s' stop noted (exit handling in progress; no relaunch)",
                                self.config.name)
            return

        self.state = ProcessState.STOPPING
        logger.info("Stopping task '%s' (pid=%s)", self.config.name, self.pid)

        # Bind the process ONCE: the escalation + wait must act on the process this stop began on,
        # never on one a later launch put in the slot (start() refuses a STOPPING slot, but this keeps
        # the continuation honest even so — finding C1).
        p = self._proc
        if p and p.returncode is None:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass

            try:
                await asyncio.wait_for(p.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("Task '%s' did not stop; sending SIGKILL", self.config.name)
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await p.wait()

        await self._cleanup()

    async def wait_stopped(self, timeout: float = 20.0) -> bool:
        """Wait until this slot holds no live process (a stop in flight has finished). True when it
        settled within `timeout`."""
        deadline = _monotonic() + timeout
        while _monotonic() < deadline:
            alive = self._proc is not None and self._proc.returncode is None
            if self.state not in (ProcessState.STOPPING, ProcessState.STARTING) and not alive:
                return True
            await asyncio.sleep(0.05)
        return False

    def status(self) -> ProcessStatus:
        return ProcessStatus(
            name           = self.config.name,
            description    = self.config.description,
            state          = self.state,
            pid            = self.pid,
            exit_code      = self.exit_code,
            started_at     = self.started_at,
            stopped_at     = self.stopped_at,
            restart_count  = self.restart_count,
            log_file       = str(self.log.current),
            health         = self.health,
            health_detail  = self.health_detail,
            last_output_at = self.last_output_at,
        )

    # ── Live parameters (retune a running task) ────────────────────────────────

    async def set_params(self, values: dict, wait: float = 1.0) -> dict:
        """Push live-parameter updates to the running script over its control
        socket and return {ok, accepted, rejected, applied, pending}. Raises
        RuntimeError if the task isn't running or exposes no live params."""
        if self.state != ProcessState.RUNNING or not self._ctrl_sock:
            raise RuntimeError(f"Task '{self.config.name}' is not running")
        # The socket read must outlast the script-side wait for applied values.
        timeout = max(0.0, float(wait)) + 5.0
        return await asyncio.to_thread(
            _ctrl_rpc, self._ctrl_sock,
            {"op": "set", "values": values, "wait": wait}, timeout)

    async def get_params(self) -> dict:
        """Read the running script's current + applied live-parameter values."""
        if self.state != ProcessState.RUNNING or not self._ctrl_sock:
            raise RuntimeError(f"Task '{self.config.name}' is not running")
        return await asyncio.to_thread(
            _ctrl_rpc, self._ctrl_sock, {"op": "get"}, 5.0)

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
            # Unexpected exit — fire an event before deciding on restart.
            self.state = ProcessState.CRASHED
            logger.warning("Task '%s' crashed (exit=%s)", self.config.name, code)
            # An RF fault (the Layer-1 done-watcher forced this non-zero exit, or the watchdog
            # already flagged it) surfaces as the LOUD TaskHealthEvent + run coupling + snapshot,
            # NOT a plain crash — _flag_rf_fault is idempotent, so a prior watchdog alarm no-ops it
            # (no double-alarm). An ordinary crash keeps the existing CrashEvent path.
            if await self._is_rf_fault_exit():
                await self._flag_rf_fault(self.health_detail or "flowgraph halted (non-zero exit)")
                # An RF fault does NOT go through the generic crash-restart supervisor: recovery is
                # owned by the run's recovery policy (SequenceRunner.restart_run / the Phase-3 unattended
                # trigger), which reconstructs the crash-time level and re-instates the schedule. Letting
                # the raw supervisor ALSO relaunch here (config.restart_on_crash) would put two processes
                # on the single TX channel at the wrong level — a double-transmit. A STANDALONE task
                # (not owned by any run) with its own "Auto-restart on fault" set relaunches here
                # instead; otherwise this stands down and the fault is left for the operator/run policy.
                # Only a NATURAL exit (the done-watcher forced it) relaunches from here; a WEDGE the
                # watchdog auto-dropped set _stop_requested, and _scan_task_health owns that relaunch
                # (so the two paths can't both fire — one exit, one relaunch).
                if not self._stop_requested:
                    await self._maybe_auto_restart_standalone()
                return
            else:
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
            self._in_restart_delay = True
            try:
                await asyncio.sleep(self.config.restart_delay_s)
            except asyncio.CancelledError:
                # stop() cancelled us during the delay — do not relaunch.
                logger.info("Task '%s' restart aborted (stop requested)", self.config.name)
                self.state = ProcessState.STOPPED
                raise
            finally:
                self._in_restart_delay = False
            # A stop requested during the delay — or the agent tearing down (review fix #12) — also
            # aborts the relaunch.
            if self._stop_requested or self._shutting_down():
                logger.info("Task '%s' restart aborted (stop requested / shutting down)", self.config.name)
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

    async def _fire_health_event(self, detail: str, snapshot) -> None:
        """Dispatch the loud RF-fault TaskHealthEvent (mirrors _fire_crash_event's shape/dispatch)."""
        try:
            last_lines = await self.log.tail(20)
        except Exception:      # noqa: BLE001
            last_lines = []
        event = TaskHealthEvent(
            unit_id          = self._unit_id,
            task_name        = self.config.name,
            task_description = self.config.description,
            health           = TaskHealth.RF_FAULT,
            detail           = detail,
            at               = _utcnow(),
            last_log_lines   = list(last_lines),
            snapshot         = snapshot,
        )
        asyncio.create_task(self._dispatcher.fire(event))

    def _reset_underflow_streak(self) -> None:
        self._uf_last = None
        self._uf_ms = self._uf_reports = self._uf_count = 0

    def _underflow_fault_detail(self, text: str) -> Optional[str]:
        """Feed one scan's new log text to the sustained-underflow detector (§14m).

        A GR underflow report continues the current streak when the previous one was seen within the
        gap (UNDERFLOW_GAP_S, floored to 1.5 polls), else it starts a new streak. The streak's length is
        the SUM of the reported windows (each line covers "the last N ms" — real transmit time, whatever
        the poll cadence). Returns the fault detail once an unbroken streak covers UNDERFLOW_FAULT_S
        (0 = never), else None. Lines are the only timing source (GR stamps none), so two bursts that
        land in ONE scan read as one streak — a scan spans ~one poll, so that is a bounded error."""
        reports = _underflow_reports(text)
        if not reports:
            return None
        now = _monotonic()
        gap = max(_agentcfg.UNDERFLOW_GAP_S, 1.5 * _agentcfg.HEALTH_POLL_S)
        if self._uf_last is None or now - self._uf_last > gap:
            self._uf_ms = self._uf_reports = self._uf_count = 0     # a new streak
        self._uf_last = now
        for window_ms, count in reports:
            self._uf_ms += max(0, window_ms)
            self._uf_reports += 1
            self._uf_count += max(0, count)
        limit_s = _agentcfg.UNDERFLOW_FAULT_S
        if limit_s <= 0 or self._uf_ms < limit_s * 1000.0:
            return None
        covered = self._uf_ms / 1000.0
        rate = self._uf_count / covered if covered > 0 else 0.0
        return (f"sustained TX underflows: {self._uf_reports} reports over {covered:.1f} s "
                f"(~{rate:,.0f} underflows/s) — the host cannot keep the sample stream full at this "
                f"configuration (sample rate / generator load), so the radio is emitting bursts with "
                f"gaps between them")

    async def _flag_rf_fault(self, detail: str) -> None:
        """Mark this task RF-faulted (dead-but-alive, docs/rf-fault-recovery.md §5.3): set health,
        capture the §6.3 resource snapshot, fire the loud TaskHealthEvent, and invoke the run-coupling
        hook. Idempotent per run via `_fault_alarmed`, so both detection layers / a repeated log
        mention can't double-alarm. Does NOT stop the task — the watchdog auto-drops RF after."""
        if self._fault_alarmed:
            return
        self._fault_alarmed = True
        self.health = TaskHealth.RF_FAULT.value
        self.health_detail = detail
        # Capture BEFORE any auto-drop stop, so a halted-but-alive flowgraph still has a live /proc.
        snap = None
        try:
            uhd_log = str(Path(self.log.task_dir) / _agentcfg.UHD_LOG_FILE_NAME)
            snap = await _sysmon.fault_snapshot(
                self.pid, uhd_log_path=uhd_log,
                task_dir=str(self.log.task_dir), log_path=str(self.log.current),
            )
            self._resource_snapshot = snap
        except Exception as exc:   # noqa: BLE001 — a snapshot failure never blocks the alarm
            logger.debug("fault snapshot failed for '%s': %s", self.config.name, exc)
        try:
            await self._fire_health_event(detail, snap)
        except Exception as exc:   # noqa: BLE001
            logger.warning("could not fire health event for '%s': %s", self.config.name, exc)
        if self._fault_hook is not None:
            try:
                await self._fault_hook(self.config.name, detail)
            except Exception as exc:   # noqa: BLE001 — coupling failure never blocks the alarm
                logger.warning("fault hook failed for '%s': %s", self.config.name, exc)

    async def _maybe_auto_restart_standalone(self) -> None:
        """STANDALONE auto-restart-on-fault (Phase 3b, docs/rf-fault-recovery.md §7.1/§14e).

        Relaunch a task that RF-faulted with the SAME parameters it had — but ONLY when it is the
        task's OWN business: `auto_restart_on_fault` is set, the master kill-switch is on, and the
        task is NOT owned by an active sequence/plan run (a run-owned fault is recovered by the run
        policy — the unattended trigger or an operator Restart; relaunching here too would put two
        processes on the single TX channel = a double-transmit). Budget-limited (max_fault_restarts
        within restart_window_s), then it stands down and leaves the task faulted for a manual start.

        Called from BOTH detection paths once the process is DEAD: the non-intentional EXIT branch of
        _watch (the done-watcher forced a non-zero exit) and _scan_task_health after its auto-drop
        stop() (the true-wedge case). An in-flight latch makes the two mutually exclusive per fault."""
        # A per-launch override (Run… form) wins over the stored TaskConfig default.
        enabled = (self._auto_restart_override if self._auto_restart_override is not None
                   else self.config.auto_restart_on_fault)
        if not enabled:
            return
        if not _agentcfg.AUTO_RESTART_ENABLED:
            return
        # A run DRIVES this task → the run's recovery policy handles it (never double-relaunch). A
        # claim that is only a not-yet-fired launch is re-examined after the settle delay
        # (_wait_out_run_claim, review fix #28) rather than standing down for good here.
        kind = self._run_claim_kind()
        if kind in ("driven", "error"):
            return
        # Only one attempt in flight (the EXIT path and the watchdog-stop path can race for one fault).
        if self._fault_restart_inflight:
            return
        # Rolling-window budget: give up on a fast fault loop (a permanently broken task), keep
        # recovering an intermittent startup fault (old attempts age out of the window).
        now = _monotonic()
        window = self.config.restart_window_s
        recent = [t for t in self._fault_restart_times if now - t <= window]
        budget = self.config.max_fault_restarts
        if budget and len(recent) >= budget:
            if not self.fault_restart_giving_up:
                self.fault_restart_giving_up = True
                logger.error(
                    "Task '%s' RF-faulted %d time(s) within %.0fs — giving up auto-restart "
                    "(manual start required)", self.config.name, len(recent), window)
            return
        self._fault_restart_inflight = True
        try:
            # Let the OLD watcher fully settle before relaunching, so its exit handling (state/history/
            # _cleanup of the old run's log fh + control socket) can't run concurrently with — and
            # corrupt — the new run start() creates. In the EXIT path we ARE the watcher (awaiting self
            # would deadlock), so skip; in the wedge path this is the health loop, so the old _watch is
            # a different task we wait out.
            watcher = self._watcher_task
            if (watcher is not None and not watcher.done()
                    and watcher is not asyncio.current_task()):
                try:
                    await asyncio.shield(watcher)   # a cancel of THIS task must not cancel the watcher
                except Exception:      # noqa: BLE001 — the watcher's own errors are its business
                    pass
            # Wait out a settle delay (also lets the /dev/shm sweep in _cleanup finish). Abort if a
            # stop is requested meanwhile (an operator stopping the faulted task must not be overridden).
            # The delay (and the claim wait below) is the ONLY phase a stop() may cancel this task in.
            self._in_restart_delay = True
            try:
                await asyncio.sleep(self.config.restart_delay_s)
                if self._operator_stop_requested or self._shutting_down():
                    logger.info("Task '%s' auto-restart aborted (operator stop / shutdown)", self.config.name)
                    return
                # Re-check ownership after the delay — a run may have (re-)armed this task meanwhile. A
                # claim that is ONLY a not-yet-fired launch is waited out (bounded), review fix #28.
                if not await self._wait_out_run_claim():
                    return
            except asyncio.CancelledError:
                logger.info("Task '%s' auto-restart aborted (cancelled)", self.config.name)
                raise
            finally:
                self._in_restart_delay = False
            if self._operator_stop_requested or self._shutting_down():
                logger.info("Task '%s' auto-restart aborted (operator stop / shutdown)", self.config.name)
                return
            # Ground-truth safety gate: never relaunch over a process that has not actually exited
            # (the single-TX-channel double-transmit invariant), nor over one already (re)started.
            if self.state in (ProcessState.RUNNING, ProcessState.STARTING):
                return
            if self._proc is not None and self._proc.returncode is None:
                logger.warning("Task '%s' still alive — deferring auto-restart", self.config.name)
                return
            self._fault_restart_times.append(now)
            self.restart_count += 1
            logger.info("Auto-restarting faulted task '%s' (attempt #%d) ...",
                        self.config.name, len(self._fault_restart_times))
            # Relaunch with the SAME request it faulted under (custom args / env / the override) so the
            # recovered task transmits at the exact parameters. Through the manager launch hook when set
            # (the full path: repositions the attenuator via _gate_precommand, carries the carrier), else
            # a bare direct start (isolation / tests).
            try:
                if self._launch_hook is not None:
                    await self._launch_hook(self.config.name, self._last_request)
                else:
                    await self.start(self._last_request)
            except asyncio.CancelledError:
                raise
            except Exception as exc:       # noqa: BLE001 — a failed relaunch is logged, never silent
                logger.error("Auto-restart of '%s' failed: %s", self.config.name, exc)
                # The task is DOWN and nothing will retry it (the budget was consumed): raise the loud
                # alarm again so an operator learns the recovery failed (re-review finding W4).
                self.health = TaskHealth.RF_FAULT.value
                self.health_detail = f"auto-restart relaunch failed: {exc}"
                try:
                    await self._fire_health_event(self.health_detail, self._resource_snapshot)
                except Exception:          # noqa: BLE001 — alarm is best effort
                    pass
        finally:
            self._fault_restart_inflight = False

    def _shutting_down(self) -> bool:
        return self._shutdown_flag is not None and self._shutdown_flag.is_set()

    def _run_claim_kind(self) -> str:
        """How an active run claims this task right now: "none" (relaunchable), "pending" (only a
        not-yet-fired launch — waited out), "driven" (a run is driving it, or its faulted task — the
        run policy owns the fault), or "error" (a query failed: never relaunch blindly)."""
        if self._owned_query is None:
            return "none"
        try:
            if self.config.name not in self._owned_query():
                return "none"
        except Exception as exc:       # noqa: BLE001 — a query failure must not relaunch blindly
            logger.warning("owned-query failed for '%s' — skipping auto-restart: %s",
                           self.config.name, exc)
            return "error"
        try:
            if self._pending_query is not None and self.config.name in self._pending_query():
                return "pending"
        except Exception:              # noqa: BLE001
            pass
        return "driven"

    async def _wait_out_run_claim(self) -> bool:
        """After the settle delay: True when NO active run claims this task (relaunch it); False when a
        run DRIVES it (its recovery policy owns the fault — stand down for good), or an operator stop /
        shutdown arrives, or a launch-only claim outlasts restart_window_s. A claim that is ONLY a
        not-yet-fired launch of an armed run is waited out (a short-lived one — the run about to start
        it, or one being re-armed) instead of standing down permanently, so a task in claimed∖live no
        longer falls between the two recovery paths (review fix #28). Never raises; a query failure
        reads as claimed (never relaunch blindly)."""
        deadline = _monotonic() + max(0.0, float(self.config.restart_window_s or 0.0))
        pause = min(5.0, max(0.5, float(self.config.restart_delay_s or 1.0)))
        while True:
            kind = self._run_claim_kind()
            if kind == "none":
                return True
            if kind != "pending":
                return False          # a run is driving it → the run's policy recovers it
            if _monotonic() >= deadline or self._operator_stop_requested or self._shutting_down():
                logger.info("Task '%s' auto-restart stood down: a run still claims it (pending launch)",
                            self.config.name)
                return False
            await asyncio.sleep(pause)

    def ctrl_ready(self) -> bool:
        """True once the running script has bound its live-parameter control socket."""
        return bool(self._ctrl_sock) and os.path.exists(self._ctrl_sock)

    def age_s(self) -> Optional[float]:
        """Seconds since this run's spawn (None when not started)."""
        if not self.started_at:
            return None
        try:
            from datetime import datetime, timezone
            t = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
            return max(0.0, (datetime.now(timezone.utc) - t).total_seconds())
        except (TypeError, ValueError):
            return None

    async def _is_rf_fault_exit(self) -> bool:
        """True if this task's exit is (or corroborates) an RF fault: health already flagged, or the
        log tail carries a fault signature (the Layer-1 done-watcher marker / a GR buffer error)."""
        if self.health == TaskHealth.RF_FAULT.value:
            return True
        try:
            lines = await self.log.tail(40)
        except Exception:      # noqa: BLE001
            return False
        hay = "\n".join(lines).lower()
        return any(p.lower() in hay for p in _agentcfg.HEALTH_FAULT_PATTERNS)

    async def _cleanup(self, set_state: bool = True) -> None:
        if self._log_fh:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None

        # Remove this run's control socket. The script unlinks its own on a clean
        # exit; this also clears a stale file left by a crash so it can't fool a
        # later set-params into connecting to nothing.
        if self._ctrl_sock:
            try:
                os.unlink(self._ctrl_sock)
            except OSError:
                pass
            self._ctrl_sock = None

        # Reclaim this task's /dev/shm staging if it was SIGKILLed (a wedged tb.wait() that the
        # 10 s grace escalated to SIGKILL never ran the script's atexit). The sweep touches only
        # dead-PID 'sdrtx-' orphans, so a live sibling task is never harmed (§3.5).
        _sweep_shm_orphans()

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
        # Transient fire-and-exit ("run") processes — not tied to a task's single
        # slot, so a sequence can fire many (e.g. attenuator sets) without the
        # "already running" collision. Keyed by a monotonic id → (proc, fh, run_id).
        self._oneshots: Dict[int, tuple] = {}
        self._oneshot_seq = 0
        # Active-component control-param → CLI flag, cached per script (see _active_flag).
        self._active_flags: Dict[str, dict] = {}
        # Full extracted argspec, cached per script (see tune_log_context).
        self._script_specs: Dict[str, Optional[dict]] = {}
        # Per-task RF-gate bookkeeping: the last-known {power, rf_on} so a live tune that toggles
        # only one of them still positions the attenuators correctly (see _gate_precommand).
        self._gate_state: Dict[str, dict] = {}
        # RF-fault DETECTION (Phase 1): the health-watchdog coroutine + the run-coupling hook it
        # (and the exit path) invoke on a confirmed fault (set by the SequenceRunner via lifespan).
        self._health_task: Optional[asyncio.Task] = None
        self._fault_hook = None
        # STANDALONE auto-restart-on-fault (Phase 3b): the owned-query the SequenceRunner supplies so a
        # run-owned fault is never also relaunched by the task's own policy (see _maybe_auto_restart_standalone).
        self._owned_query = None
        # The launch hook every proc uses for a standalone auto-restart relaunch (the full manager
        # launch path, so the attenuator is repositioned). Wired to self.relaunch in main.py lifespan.
        self._launch_hook = None
        # Tasks claimed ONLY by a not-yet-fired launch (see ManagedProcess._wait_out_run_claim).
        self._pending_query = None
        # Set FIRST in shutdown(): every sleeping relaunch checks it before spawning (review fix #12).
        self._shutdown_flag = asyncio.Event()
        # CLEARED while the boot pre-image (uhd_usrp_probe) holds the SDR; a task launch waits on it
        # instead of colliding on the device (review fix #9). Set = device free (the default).
        self.device_free = asyncio.Event()
        # Bumped by every PANIC/shutdown (cancel_pending_relaunches): a launch parked before its
        # proc.start() — on the boot pre-image gate or the attenuator pre-command — compares it after
        # the gates and abandons itself if a panic happened meanwhile (re-review finding C3).
        self._panic_epoch = 0
        self.device_free.set()
        # Per-script cache: does the script report HEALTH state=transmitting (watch_flowgraph)?
        self._tx_marker_scripts: Dict[str, bool] = {}
        for proc in self._procs.values():
            proc._shutdown_flag = self._shutdown_flag

    def _make_proc(self, cfg: TaskConfig) -> ManagedProcess:
        proc = ManagedProcess(
            cfg, LogManager(self._log_root, cfg.name), self._dispatcher, self._unit_id
        )
        proc._fault_hook = self._fault_hook
        proc._owned_query = self._owned_query
        proc._pending_query = self._pending_query
        proc._launch_hook = self._launch_hook
        proc._shutdown_flag = self._shutdown_flag
        return proc

    def set_fault_hook(self, hook) -> None:
        """Register the run-coupling callback (task_name, detail) -> awaitable, invoked when a task
        is confirmed RF-faulted. Applied to every current and future ManagedProcess."""
        self._fault_hook = hook
        for proc in self._procs.values():
            proc._fault_hook = hook

    def set_owned_query(self, query) -> None:
        """Register the owned-query () -> set[task_name] (from the SequenceRunner) naming the tasks
        currently owned by an ACTIVE run. A standalone task's Auto-restart-on-fault consults it so a
        run-owned fault is left to the run's recovery policy — never double-relaunched here (which
        would double-transmit on the single TX channel). Applied to every current + future proc."""
        self._owned_query = query
        for proc in self._procs.values():
            proc._owned_query = query

    def set_pending_query(self, query) -> None:
        """Register the pending-launch query () -> set[task_name]: tasks claimed ONLY by a not-yet-fired
        launch of an active run (see ManagedProcess._wait_out_run_claim). Applied to every proc."""
        self._pending_query = query
        for proc in self._procs.values():
            proc._pending_query = query

    def set_launch_hook(self, hook) -> None:
        """Register the launch callback (name, request) -> awaitable a standalone auto-restart uses to
        relaunch a faulted task through the FULL manager path (attenuator positioning + launch carrier),
        rather than a bare ManagedProcess.start. Applied to every current + future proc. Wired to
        self.relaunch."""
        self._launch_hook = hook
        for proc in self._procs.values():
            proc._launch_hook = hook

    async def relaunch(self, name: str, request: Optional[StartRequest] = None) -> None:
        """Relaunch a task through the full launch path (used by a standalone auto-restart-on-fault).
        source='auto-restart' so it is not mistaken for an operator start (no task_started event).

        The relaunch reproduces the task's LIVE state, not just its launch request: every value a
        set_params tune applied to the faulted run (a lowered --power, the RF gate turned off, a moved
        carrier) is baked onto the launch args by the script's argspec flags — so a task the operator
        muted or lowered comes back muted or lowered, never RF-on at the launch level (review fix #2).
        If it WAS live-tuned but the argspec is unreadable, it stands down (logged) rather than
        relaunch hot; an un-tuned task relaunches with the request untouched."""
        proc = self._get(name)
        live = dict(proc._live_applied)
        req = request or StartRequest()
        spec = self._script_spec(name)
        if live and spec is None:
            logger.error("Auto-restart of '%s' STOOD DOWN: it was live-tuned (%s) but its argspec is "
                         "unreadable — refusing to relaunch at the launch parameters",
                         name, sorted(live))
            return
        base = _cmdargs.post_script_args(_build_command(proc.config.command, req.args, req.replace_args))
        args = list(base)
        if live:
            args = _cmdargs.overlay_live_params(args, live, spec, self._rf_gate(name))
        # A time-dependent script (one declaring an `is_elapsed` parameter) resumes its OWN
        # timeline where it stands NOW: the elapsed the faulted launch carried + the wall-clock
        # seconds since it was spawned (resync semantics — a standalone drift rejoins its own
        # clock, it has no schedule to shift). Unknown spawn time ⇒ left as launched.
        ep = _cmdargs.elapsed_param(spec)
        age = proc.age_s() if ep is not None else None
        reset_at = self._last_reset_applied_at(proc, spec) if ep is not None else None
        launch_elapsed = _cmdargs.elapsed_of_args(args, ep) if ep is not None else 0.0
        if ep is not None and reset_at is not None:
            # A live elapsed-RESET trigger (`restart`, declared resets_elapsed) was applied to the
            # faulted run: its clock started THERE (§14i), so resume from now − that instant.
            args = _cmdargs.bake_elapsed(args, ep, max(0.0, reset_at))
        elif ep is not None and age is not None:
            args = _cmdargs.bake_elapsed(args, ep, launch_elapsed + age)
        # The ABSOLUTE origin (§14j): the script's own report when it gave one (exact — it is the
        # instant its clock actually started, a reset trigger included), else the best reconstruction:
        # the applied reset trigger's instant, else the spawn minus the launch's own elapsed. The
        # script prefers the origin over --elapsed, so the relaunch lands on the never-faulted position
        # whatever the launch latency.
        cp = _cmdargs.clock_origin_param(spec)
        if cp is not None:
            import time as _time
            origin = proc.clock_origin
            if origin is None and reset_at is not None:
                origin = _time.time() - reset_at
            if origin is None and age is not None:
                origin = _time.time() - age - launch_elapsed
            if origin is not None:
                args = _cmdargs.bake_clock_origin(args, cp, origin)
        if args != base or live:
            req = req.model_copy(update={"args": args, "replace_args": True})
        await self.start(name, req, source="auto-restart")

    @staticmethod
    def _last_reset_applied_at(proc: "ManagedProcess", spec: Optional[dict]) -> Optional[float]:
        """Seconds since the LAST applied elapsed-reset trigger on this process (None when none)."""
        best = None
        for d in _cmdargs.resets_elapsed_dests(spec):
            if d in proc._live_applied and _cmdargs.is_reset_fire({d: proc._live_applied[d]}, {d}):
                ts = proc._live_applied_at.get(d)
                if not ts:
                    continue
                try:
                    from datetime import datetime, timezone
                    t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                    age = (datetime.now(timezone.utc) - t).total_seconds()
                except (TypeError, ValueError):
                    continue
                best = age if best is None else min(best, age)
        return best

    def live_applied(self, name: str) -> dict:
        """The live-parameter values applied to `name`'s CURRENT process by any set_params source
        (a sequence tune or an operator's Tune…), {dest: value}; reset on every launch. A restart
        reconstruction carries these for the dests its schedule never drives (review follow-up:
        every parameter, not only a swept level)."""
        return dict(self._get(name)._live_applied)

    def clock_origin(self, name: str) -> Optional[float]:
        """The script-reported absolute clock origin (Unix seconds) of `name`'s CURRENT process, or None."""
        return self._get(name).clock_origin

    def live_applied_at(self, name: str) -> dict:
        """{dest: ISO instant} of when each live_applied value was applied (see live_applied)."""
        return dict(self._get(name)._live_applied_at)

    def cancel_pending_relaunches(self) -> int:
        """Abort every PENDING auto-restart relaunch (a faulted task waiting out its settle / claim
        delay on either detection path) and block any that has not started deciding yet: PANIC and
        shutdown must guarantee no transmitter comes up afterwards (review fixes #12/#33). Returns
        how many were cancelled. Sync (no await) so a caller holding no loop turn still gets it."""
        n = 0
        self._panic_epoch += 1
        for proc in self._procs.values():
            proc._operator_stop_requested = True
            t = proc._relaunch_task
            if t is not None and not t.done():
                t.cancel()
                n += 1
            w = proc._watcher_task
            if (proc.state == ProcessState.CRASHED and w is not None and not w.done()
                    and proc._in_restart_delay):
                proc._stop_requested = True
                w.cancel()
                proc.state = ProcessState.STOPPED
                n += 1
        return n

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        for name, proc in self._procs.items():
            if proc.config.autostart:
                logger.info("Autostarting task '%s'", name)
                try:
                    await proc.start()
                except Exception as exc:
                    logger.error("Failed to autostart '%s': %s", name, exc)
        # RF-fault DETECTION (Phase 1): the periodic health watchdog (§5.2). A poll interval <= 0
        # disables it (review fix #25 — it used to spin).
        if (_agentcfg.HEALTH_WATCH_ENABLED and _agentcfg.HEALTH_POLL_S > 0
                and self._health_task is None):
            self._health_task = asyncio.create_task(self._health_loop(), name="task-health")

    async def shutdown(self) -> None:
        # FIRST: every relaunch that wakes from now on stands down (review fix #12).
        self._shutdown_flag.set()
        if self._health_task is not None:
            self._health_task.cancel()
            self._health_task = None
        # Cancel every pending standalone auto-restart on BOTH detection paths (the detached wedge
        # relaunch AND the exit-path watcher sleeping its settle delay), so no relaunch can spawn a
        # fresh transmitter while the agent is tearing down.
        self.cancel_pending_relaunches()
        live = [p for p in self._procs.values()
                if p.state in (ProcessState.RUNNING, ProcessState.STARTING)]
        if live:
            logger.info("Stopping %d task(s) on shutdown ...", len(live))
            await asyncio.gather(*[p.stop() for p in live], return_exceptions=True)
        # A slot whose stop was interrupted mid-grace (the health task cancelled above, or the runner
        # loop cancelled mid-STOP-step) reads STOPPING with its process ALIVE: reap it, so the agent
        # never exits with a transmitter running (re-review finding C6).
        for proc in self._procs.values():
            pr = proc._proc
            if pr is not None and pr.returncode is None:
                try:
                    os.killpg(os.getpgid(pr.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                try:
                    await asyncio.wait_for(pr.wait(), timeout=5.0)
                except (asyncio.TimeoutError, Exception):   # noqa: BLE001
                    pass
                proc.state = ProcessState.STOPPED

    async def _health_loop(self) -> None:
        """Periodic RF-fault watchdog (docs/rf-fault-recovery.md §5.2). Every HEALTH_POLL_S it scans
        each RUNNING task's NEW log bytes for a fault signature (the Layer-1 done-watcher marker, or a
        GR buffer error for the TRUE-WEDGE case where the flowgraph never exits). On a hit for a
        not-yet-alarmed task it flags the RF fault (health event + snapshot + run coupling) and
        auto-drops RF by stopping the dead task. Best-effort; one task's error never stops the loop."""
        while True:
            try:
                await asyncio.sleep(_agentcfg.HEALTH_POLL_S)
                for proc in list(self._procs.values()):
                    if proc.state != ProcessState.RUNNING or proc._fault_alarmed:
                        continue
                    try:
                        await self._scan_task_health(proc)
                    except Exception as exc:   # noqa: BLE001 — never let one task stall the loop
                        logger.debug("health scan failed for '%s': %s", proc.config.name, exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:           # noqa: BLE001 — the loop must survive anything
                logger.warning("health watchdog loop error: %s", exc)

    @staticmethod
    def _scan_stale(proc: "ManagedProcess", p0) -> bool:
        """The process the scanned bytes came from is no longer the RUNNING one: it exited (its exit
        path owns the alarm, the run coupling and any relaunch) or was already replaced by a relaunch.
        Acting on the stale read cancelled the exit path's alarm mid-flight (#13) or SIGTERMed a healthy
        relaunch and relaunched it again (#14)."""
        return proc._proc is not p0 or proc.state != ProcessState.RUNNING

    async def _scan_task_health(self, proc: "ManagedProcess") -> None:
        """Read a task's new log bytes; on a fault signature, flag the fault and auto-drop RF."""
        p0 = proc._proc
        text, new_off, new_inode = await proc.log.read_since(proc._log_offset, proc._log_inode)
        proc._log_offset, proc._log_inode = new_off, new_inode
        if not text:
            return
        if self._scan_stale(proc, p0):
            return                     # the bytes belong to an exited/replaced process (finding W10)
        proc.last_output_at = _utcnow()
        low = text.lower()
        # The script's "flowgraph up" report (paramkit.txhealth): the radio is on air from here — the
        # recovery breaker's healthy-settle clock keys on it, not on the spawn (review fix #19).
        if proc.transmitting_at is None and _txhealth.TRANSMITTING_MARKER.lower() in low:
            proc.transmitting_at = _utcnow()
        # The script's reported clock origin (`CLOCK origin=<unix>`): the LAST one wins — a reset
        # trigger prints a new one when it restarts the timeline (§14j).
        origin = _last_clock_origin(text)
        if origin is not None:
            proc.clock_origin = origin
        hit = next((p for p in _agentcfg.HEALTH_FAULT_PATTERNS if p.lower() in low), None)
        # A sustained streak of GR underflow reports is a fault too (§14m): the flowgraph is alive but
        # the radio is emitting bursts with gaps — as useless (and worse for the band) as silence.
        detail = (f"log signature: {hit}" if hit is not None
                  else proc._underflow_fault_detail(text))
        if detail is None:
            return
        if self._scan_stale(proc, p0):
            return
        logger.warning("Task '%s' RF fault detected (%s)", proc.config.name, detail)
        await proc._flag_rf_fault(detail)
        # Re-check after the (slow: snapshot + event + run coupling) flag: if the process exited
        # meanwhile, its exit path owns the rest — stop()ing here cancelled that path mid-flight and
        # lost the alarm/coupling (review fixes #13/#14).
        if self._scan_stale(proc, p0):
            return
        # Auto-drop RF: free the single TX channel (SIGTERM→grace→SIGKILL — a halted flowgraph may
        # not honour SIGTERM). Idempotent, so it can't collide with a concurrent abort/deadman.
        # operator=False: this is the internal auto-drop, so it must NOT block the task's own
        # standalone auto-restart recovery below (only an operator/API stop does).
        try:
            # Shielded: a cancel of the health task (shutdown) must not interrupt the SIGTERM→SIGKILL
            # escalation and leave the faulted process alive in a STOPPING slot (finding C6).
            await asyncio.shield(proc.stop(operator=False))
        except asyncio.CancelledError:
            raise
        except Exception as exc:   # noqa: BLE001
            logger.warning("auto-drop-RF stop failed for '%s': %s", proc.config.name, exc)
            return
        # True-wedge path: the process is now DEAD (stop() awaited its exit). A standalone
        # auto-restart-on-fault task relaunches — DETACHED, so the ~restart_delay_s settle can't stall
        # the watchdog from scanning other tasks. _maybe_auto_restart_standalone awaits the old watcher
        # first, so start() can't race the exit-path cleanup. The EXIT-path _watch does NOT relaunch a
        # wedge (its _stop_requested is set by the auto-drop), so this is the sole wedge relaunch.
        proc._relaunch_task = asyncio.create_task(
            proc._maybe_auto_restart_standalone(), name=f"relaunch-{proc.config.name}")

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

    def is_live(self, name: str) -> bool:
        """True while the task is RUNNING, STARTING (launch in flight) or its OS process still exists —
        everything an abort must stop, not only is_running() (review fix #6)."""
        proc = self._procs.get(name)
        if proc is None:
            return False
        return proc.state in (ProcessState.RUNNING, ProcessState.STARTING) or self.is_process_alive(name)

    def tune_ready(self, name: str) -> tuple:
        """(ready, within_grace): whether the running task has bound its live-parameter control socket,
        and whether it is still young enough (< CTRL_BIND_GRACE_S since spawn) that a tune should be
        DEFERRED rather than fired into nothing (review fix #4)."""
        proc = self._procs.get(name)
        if proc is None or proc.state != ProcessState.RUNNING:
            return False, False
        if proc.ctrl_ready():
            return True, False
        age = proc.age_s()
        return False, (age is not None and age < _agentcfg.CTRL_BIND_GRACE_S)

    def expects_tx_marker(self, name: str) -> bool:
        """Whether the task's script reports HEALTH state=transmitting (it uses paramkit.txhealth's
        watch_flowgraph). Read off the script source once, cached per script."""
        try:
            proc = self._get(name)
            script = _script_prefix(list(proc.config.command))[-1]
        except Exception:      # noqa: BLE001
            return False
        key = f"{script}|{proc.config.working_dir}"
        if key not in self._tx_marker_scripts:
            src = self._read_script_source(script, proc.config.working_dir)
            if src is None:
                return False           # unreadable right now: never memoise a miss (finding W9)
            self._tx_marker_scripts[key] = "watch_flowgraph" in src
        return self._tx_marker_scripts[key]

    def task_transmitting_confirmed(self, name: str) -> bool:
        """For the recovery breaker's healthy-settle: a script that reports the transmitting marker
        counts as healthy only once it has (the warm-up before it is NOT healthy time, review fix #19);
        a script without the marker (FIFO stagers, mocks, x410) keeps the running-and-OK rule."""
        proc = self._procs.get(name)
        if proc is None:
            return False
        if not (_agentcfg.HEALTH_WATCH_ENABLED and _agentcfg.HEALTH_POLL_S > 0):
            return True                # nothing reads the marker: running-and-OK is the rule (O2)
        if not self.expects_tx_marker(name):
            return True
        return proc.transmitting_at is not None

    def is_process_alive(self, name: str) -> bool:
        """True if the named task's OS process exists and has NOT exited (returncode is None).

        Ground truth for 'is it safe to relaunch over this task yet', independent of the state
        field's asynchronous settling: a wedged flowgraph the watchdog is mid-stopping reads state
        STOPPING (so is_running() is already False) yet its process is still ALIVE until SIGKILL, and
        after a stop the state field only flips to STOPPED once the watcher task runs. returncode is
        the reliable signal — set by asyncio exactly when the process has exited. False if unknown."""
        proc = self._procs.get(name)
        p = getattr(proc, "_proc", None) if proc is not None else None
        return p is not None and p.returncode is None

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
            # A script that DECLARES its elapsed-time parameter (paramkit `is_elapsed`) is
            # resumable by contract, without the operator configuring the flag by hand.
            ep = _cmdargs.elapsed_param(self._script_spec(name))
            if ep is not None:
                req.args = _cmdargs.bake_elapsed([], ep, offset_s)
            return req
        if cfg.resume_offset_mode == "env":
            req.env_overrides = {cfg.resume_offset_env: str(offset_s)}
        else:  # "arg"
            req.args = [cfg.resume_offset_flag, str(offset_s)]
        return req

    # ── One-shot (fire-and-exit) runs ─────────────────────────────────────────

    async def run_oneshot(self, name: str, args: List[str], run_id: str = "") -> None:
        """
        Launch a task's command as a transient, self-terminating process — NOT the
        task's single managed slot — so a sequence can fire many (e.g. attenuator
        sets at different values) without an "already running" collision, and
        without needing a stop. Output is appended to the task's current.log — the
        same log the Logs tab tails — so a one-shot's output is visible there
        instead of a separate file the UI never reads. Tracked so abort/panic can
        sweep any still-running one-shot.
        """
        mp = self._get(name)
        cfg = mp.config
        cmd = _build_command(cfg.command, list(args), replace=True)
        await self.device_free.wait()          # never open the SDR under the boot pre-image (#9)
        # Auto-command both: a one-shot transmit run that sets an absolute --power also drives
        # its linked active components (attenuator, …) first — muted (attenuators at max) when the
        # command leaves the RF output gate off.
        await self._gate_precommand(name, cmd=cmd)
        env = {**os.environ, **_launch_env_pins(mp.log.task_dir), **cfg.env}
        _ensure_paramkit_on_path(env)   # scripts live in the persistent dir now — keep `import paramkit` working
        env.setdefault("PYTHONUNBUFFERED", "1")   # flush print()/stdout live, like logging
        try:
            fh = mp.log.current.open("ab")   # append into the task's single log
        except OSError as exc:
            logger.error("One-shot '%s': could not open log: %s", name, exc)
            fh = None

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=fh, stderr=fh, cwd=cfg.working_dir, env=env,
            start_new_session=True,
        )
        self._oneshot_seq += 1
        oid = self._oneshot_seq
        self._oneshots[oid] = (proc, fh, run_id)
        logger.info("One-shot '%s' (pid=%s): %s", name, proc.pid, cmd)
        asyncio.create_task(self._watch_oneshot(oid, name))

    async def _watch_oneshot(self, oid: int, name: str) -> None:
        entry = self._oneshots.get(oid)
        if entry is None:
            return
        proc, fh, _run_id = entry
        code = await proc.wait()
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass
        self._oneshots.pop(oid, None)
        if code not in (0, None):
            logger.warning("One-shot '%s' exited with code %s", name, code)
        else:
            logger.info("One-shot '%s' completed", name)

    async def stop_oneshots(self, run_id: Optional[str] = None) -> int:
        """SIGTERM still-running one-shots (all, or just one run's). Returns the
        number signalled. Their watchers clean up as they exit."""
        signalled = 0
        for _oid, (proc, _fh, rid) in list(self._oneshots.items()):
            if run_id is not None and rid != run_id:
                continue
            if proc.returncode is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    signalled += 1
                except (ProcessLookupError, OSError):
                    pass
        return signalled

    # ── Per-task operations ───────────────────────────────────────────────────

    def _get(self, name: str) -> ManagedProcess:
        if name not in self._procs:
            raise KeyError(f"Unknown task: '{name}'")
        return self._procs[name]

    async def start(self, name: str, request: Optional[StartRequest] = None,
                    source: str = "manual") -> ProcessStatus:
        proc = self._get(name)
        # Auto-command both: if this launch sets an absolute --power on a calibrated transmit
        # task with active components, position each linked component (attenuator, …) first —
        # as a one-shot, so nothing has to be running and the operator never sees it.
        req = request or StartRequest()
        cmd = _build_command(proc.config.command, req.args, req.replace_args)
        # While a launch is parked below (the boot pre-image gate, the attenuator pre-command) its slot
        # still reads STOPPED, so a Stop / PANIC landing meanwhile cannot signal a process: they leave
        # their INTENT on the slot (stop() latches _operator_stop_requested even when nothing runs;
        # PANIC bumps _panic_epoch) and the launch honours it after the gates instead of coming up on
        # air after the operator stopped it (re-review findings C3/O1). A fresh operator/sequence
        # launch supersedes any OLDER stop intent (cleared here); an auto-restart keeps the #11 rule
        # (an operator stop BEFORE the relaunch's launch also stands it down).
        if source != "auto-restart":
            proc._operator_stop_requested = False
        epoch0 = self._panic_epoch
        epoch0 = self._panic_epoch
        await self.device_free.wait()          # never open the SDR under the boot pre-image (#9)
        await self._gate_precommand(name, cmd=cmd)
        if self._panic_epoch != epoch0 or self._shutdown_flag.is_set():
            raise RuntimeError(f"one-shot '{name}' abandoned: panic / shutdown while waiting to launch")
        if proc._operator_stop_requested or self._shutdown_flag.is_set() or self._panic_epoch != epoch0:
            if source == "auto-restart":
                logger.info("Auto-restart of '%s' abandoned: operator stop / shutdown during its pre-command", name)
                return proc.status()
            raise RuntimeError(f"launch of '{name}' abandoned: it was stopped / panic-stopped while "
                               f"waiting to launch")
        await proc.start(self._with_launch_freq(name, req, cmd))
        status = proc.status()
        if source == "manual":
            await self._fire_task_event("task_started", status)
        return status

    def _with_launch_freq(self, name: str, req: StartRequest, cmd) -> StartRequest:
        """Carry the launch's transmit frequency (the command's CAL_FREQ_PARAM, in Hz) into the
        task env as SDR_CAL_FREQ_HZ, so the injected artifact's v1 curve and --power bounds fold
        at the carrier the script actually transmits at — the same frequency the frequency-aware
        fold and the attenuator use. An explicit value (task config or request) wins."""
        key = _agentcfg.CAL_FREQ_HZ_ENV
        if key in self._get(name).config.env or key in (req.env_overrides or {}):
            return req
        f = self._freq_of_launch(name, cmd)
        if f is None:
            return req
        return req.model_copy(update={"env_overrides": {**(req.env_overrides or {}), key: f"{f:.6f}"}})

    def _freq_of_launch(self, name: str, cmd) -> Optional[float]:
        """The transmit frequency (Hz) a launch command sets for `name` (see _freq_from_command)."""
        return _freq_from_command(cmd, self._script_spec(name))

    def _freq_dest(self, name: str) -> Optional[str]:
        """The dest of `name`'s CAL_FREQ_PARAM (the live param whose tune moves the carrier)."""
        return (self._script_spec(name) or {}).get("calibration_freq_param") or None

    async def stop(self, name: str, source: str = "manual") -> ProcessStatus:
        proc = self._get(name)
        await proc.stop()
        status = proc.status()
        if source == "manual":
            await self._fire_task_event("task_stopped", status)
        return status

    async def set_params(self, name: str, values: dict, wait: float = 1.0) -> dict:
        """Retune a running task's live parameters. Raises KeyError (unknown task)
        or RuntimeError (not running / no live params)."""
        # A live retune of an absolute --power (or a toggle of the RF gate) on a calibrated
        # transmit task also repositions its linked active components (attenuator, …) first —
        # muted (attenuators at max) when the gate is now off, else set for the effective --power.
        gate = self._rf_gate(name)
        gd = (gate.get("dest") or gate.get("name")) if gate else None
        fd = self._freq_dest(name)
        # A retune of the CARRIER repositions them too: on a frequency-dependent chain the
        # SDR/attenuator split the script re-folds at the new carrier differs from the one the
        # agent commanded at the old — the two must be realized at the same frequency.
        if "power" in values or (gd and gd in values) or (fd and fd in values):
            await self._gate_precommand(name, values=values)
        result = await self._get(name).set_params(values, wait)
        # Remember what this run has been tuned to, so a standalone auto-restart relaunches at the
        # LIVE state rather than the launch request (review fix #2). A rejected value is not applied.
        try:
            rejected = set((result or {}).get("rejected") or {}) if isinstance(result, dict) else set()
            applied = {k: v for k, v in dict(values).items() if k not in rejected}
            proc_ = self._get(name)
            proc_._live_applied.update(applied)
            now_iso = _utcnow()
            proc_._live_applied_at.update({k: now_iso for k in applied})
        except Exception:      # noqa: BLE001 — bookkeeping only
            pass
        return result

    async def get_params(self, name: str) -> dict:
        """Read a running task's current + applied live-parameter values."""
        return await self._get(name).get_params()

    def _resolve_active(self, task_name: str, freq_hz: Optional[float] = None):
        """``(resolved_calibration, freq_hz)`` for a task IF it opted into calibration AND its
        chain has active components, else ``(None, freq_hz)`` (freq resolved from the task env when
        not supplied). Never raises — a resolution problem yields None."""
        try:
            cfg = self._get(task_name).config
        except KeyError:
            return None, freq_hz
        signal_id = cfg.env.get(_agentcfg.CAL_SIGNAL_ID_ENV)
        if not signal_id:
            return None, freq_hz
        if freq_hz is None:
            raw = cfg.env.get(_agentcfg.CAL_FREQ_HZ_ENV)
            if raw:
                try:
                    freq_hz = float(raw)
                except (TypeError, ValueError):
                    freq_hz = None
        try:
            resolved = _calib.resolve_from_files(
                _agentcfg.CALIBRATION_DOC, _agentcfg.CALIBRATION_DEFAULTS, signal_id,
                components_path=_agentcfg.CALIBRATION_COMPONENTS, freq_hz=freq_hz)
        except _calib.CalibrationError as exc:
            logger.warning("Active components for '%s': %s", task_name, exc)
            return None, freq_hz
        if resolved is None or not resolved.has_active:
            return None, freq_hz
        return resolved, freq_hz

    def active_settings(self, task_name: str, power: Optional[float],
                        freq_hz: Optional[float] = None) -> List[dict]:
        """The active-component commands to issue alongside an absolute ``power`` (dBm) on
        ``task_name`` — driven automatically whenever the task is launched/tuned (Run, quick
        play, sequences, ramps, the API). Each is ``{plane, task, param, applied_db, value}``
        from the SDR-first realization, naming
        a linked control task (e.g. a step attenuator), its parameter, and the value to set so
        the SDR + the component together deliver ``power``. Empty when the task didn't opt into
        calibration, the unit isn't calibrated for its signal, the chain has no active
        components, or ``power`` is None. Never raises — a resolution problem yields []."""
        if power is None:
            return []
        resolved, freq_hz = self._resolve_active(task_name, freq_hz)
        if resolved is None:
            return []
        return resolved.realize(float(power), freq_hz)["settings"]

    def _mute_settings(self, task_name: str, freq_hz: Optional[float] = None) -> List[dict]:
        """The active-component commands that MUTE ``task_name``'s chain — every programmable
        attenuator driven to max (see ``ResolvedCalibration.mute``). Same shape as
        ``active_settings`` so the same one-shot command path positions them. Empty when the task
        isn't calibrated or has no active components. Never raises."""
        resolved, _ = self._resolve_active(task_name, freq_hz)
        return resolved.mute()["settings"] if resolved is not None else []

    def _active_flag(self, task_name: str, param: str) -> str:
        """The CLI flag for an active component's control ``param``, read from its task's
        script argspec (cached per script). Falls back to ``--<param>`` when the script can't
        be read or declares no such parameter."""
        fallback = f"--{param}"
        try:
            cfg = self._get(task_name).config
        except KeyError:
            return fallback
        script = next((a for a in cfg.command
                       if isinstance(a, str) and a.endswith(".py")), None)
        if not script:
            return fallback
        cached = self._active_flags.get(script)
        if cached is None:
            flags: dict = {}
            # Read via the same subfolder/SCRIPTS_DIR-aware resolution as _script_spec, so a script
            # filed into a subfolder or relocated to the persistent SCRIPTS_DIR still yields its flags.
            source = self._read_script_source(script, cfg.working_dir)
            if source is not None:
                for s in (extract_params(source) or {}).get("params", []):
                    opts = [f for f in (s.get("flags") or []) if f.startswith("--")] \
                        or (s.get("flags") or [])
                    if s.get("dest") and opts:
                        flags[s["dest"]] = opts[0]
                self._active_flags[script] = flags     # memoise only a real read — never a miss
            cached = flags
        return cached.get(param, fallback)

    def _script_spec(self, task_name: str) -> Optional[dict]:
        """The full extracted argspec (params + calibration laws) for a task's script, cached
        per script path. None when the task is unknown or the script can't be read/parsed.

        A MISS is NEVER cached. If the script can't be read/parsed (e.g. it hasn't been
        deployed to this unit yet — an agent update wipes the release-local scripts dir), the
        next call retries. Caching the None would leave the run log / spreadsheet export degraded
        for the LIFE of the process even after the library is re-deployed, since the cache is only
        dropped on reload() and a script re-upload need not change tasks.yaml. So only a real spec
        is memoised."""
        try:
            cfg = self._get(task_name).config
        except KeyError:
            return None
        script = next((a for a in cfg.command
                       if isinstance(a, str) and a.endswith(".py")), None)
        if not script:
            return None
        cached = self._script_specs.get(script)
        if cached is not None:
            return cached
        source = self._read_script_source(script, cfg.working_dir)
        spec = (extract_params(source) or None) if source is not None else None
        if spec is not None:
            self._script_specs[script] = spec        # memoise only a hit — never a miss
        return spec

    @staticmethod
    def _read_script_source(script: str, working_dir: Optional[str]) -> Optional[str]:
        """The source text of a task's transmit script — resolved the SAME way the launch does
        (`_build_command` → `_resolve_script_path`). A script filed into an organizational
        subfolder keeps its basename, so the task command's path may not be the file's real
        location; reading only that naive path would fail (returning None), degrading the run
        log / spreadsheet export to the uncalibrated one-line fallback EVEN THOUGH the task
        launches fine and the client authored against a valid spec (its `/scripts/{name}/params`
        search finds the subfolder file). So: try the command path first (absolute, or relative
        to working_dir), then fall back to locating the basename under its directory. None when
        no readable source is found."""
        p = Path(script)
        if not p.is_absolute() and working_dir:
            p = Path(working_dir) / script
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        # Not at the command path — find it by basename (subfolders), as the launch does.
        for cand in _resolve_script_path([str(p)]):
            if isinstance(cand, str) and cand.endswith(".py") and cand != str(p):
                try:
                    return Path(cand).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    break
        return None

    def tune_log_context(self, task_name: str):
        """(argspec, resolved public calibration artifact) for a task — the inputs the sequence
        run log's tune-step quantity blocks fold from. Either may be None (script unreadable /
        task didn't opt into calibration / unit uncalibrated for its signal). Never raises."""
        spec = self._script_spec(task_name)
        artifact = None
        try:
            cfg = self._get(task_name).config
        except KeyError:
            return spec, None
        signal_id = cfg.env.get(_agentcfg.CAL_SIGNAL_ID_ENV)
        if signal_id:
            freq_hz = None
            raw = cfg.env.get(_agentcfg.CAL_FREQ_HZ_ENV)
            if raw:
                try:
                    freq_hz = float(raw)
                except (TypeError, ValueError):
                    freq_hz = None
            try:
                artifact = _calib.resolve_public(
                    _agentcfg.CALIBRATION_DOC, _agentcfg.CALIBRATION_DEFAULTS, signal_id,
                    unit_type=_agentcfg.UNIT_TYPE,
                    components_path=_agentcfg.CALIBRATION_COMPONENTS, freq_hz=freq_hz)
            except Exception:                        # noqa: BLE001 — the log never breaks a run
                artifact = None
        return spec, artifact

    def power_realizer(self, task_name: str):
        """A closure ``power_dbm -> {'sdr_gain_db', 'atten_db'}`` for a task's calibrated chain
        (the calibration is resolved ONCE, then reused for every row), or None when the task
        isn't calibrated. Fills the run-log table's realized SDR gain / attenuation columns."""
        try:
            cfg = self._get(task_name).config
        except KeyError:
            return None
        signal_id = cfg.env.get(_agentcfg.CAL_SIGNAL_ID_ENV)
        if not signal_id:
            return None
        freq_env = None
        raw = cfg.env.get(_agentcfg.CAL_FREQ_HZ_ENV)
        if raw:
            try:
                freq_env = float(raw)
            except (TypeError, ValueError):
                freq_env = None
        try:
            resolved = _calib.resolve_from_files(
                _agentcfg.CALIBRATION_DOC, _agentcfg.CALIBRATION_DEFAULTS, signal_id,
                components_path=_agentcfg.CALIBRATION_COMPONENTS, freq_hz=freq_env)
        except Exception:                            # noqa: BLE001 — uncalibrated / bad doc → no columns
            return None
        if resolved is None:
            return None
        muted = resolved.mute()                      # resolved ONCE: gain 0 + attenuators at max
        muted_atten = next((s.get("value") for s in muted.get("settings", []) or []), None)

        def realize(power, freq=None, rf_on=True):
            if not rf_on:                            # muted: RF gate off → no emission
                return {"sdr_gain_db": muted.get("sdr_gain_db", 0.0), "atten_db": muted_atten}
            try:
                res = resolved.realize(float(power), freq if freq is not None else freq_env)
            except Exception:                        # noqa: BLE001
                return None
            atten = next((s.get("value") for s in res.get("settings", []) or []), None)
            return {"sdr_gain_db": res.get("sdr_gain_db"), "atten_db": atten}

        return realize

    async def _launch_oneshot_wait(self, name: str, args: List[str],
                                   timeout: float = _ACTIVE_SET_TIMEOUT_S) -> Optional[int]:
        """Launch a task's command as a transient process and AWAIT its exit (with a timeout),
        appending its output to the task's log. Used to set an active component (e.g. an
        attenuator) to a value and have it exit — no long-running control task needed."""
        mp = self._get(name)
        cfg = mp.config
        cmd = _build_command(cfg.command, list(args), replace=True)
        env = {**os.environ, **_launch_env_pins(mp.log.task_dir), **cfg.env}
        _ensure_paramkit_on_path(env)   # scripts live in the persistent dir now — keep `import paramkit` working
        env.setdefault("PYTHONUNBUFFERED", "1")
        try:
            fh = mp.log.current.open("ab")
        except OSError:
            fh = None
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=fh, stderr=fh, cwd=cfg.working_dir, env=env, start_new_session=True)
        logger.info("Active-set '%s' (pid=%s): %s", name, proc.pid, cmd)
        code: Optional[int] = None
        try:
            code = await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Active-set '%s' did not finish within %ss — proceeding",
                           name, timeout)
        finally:
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
        if code not in (0, None):
            logger.warning("Active-set '%s' exited with code %s", name, code)
        return code

    async def _apply_active_settings(self, settings: List[dict]) -> None:
        """Fire each active-component set (a one-shot, awaited) so the components are physically in
        position before the transmit emits. Best-effort: a failed/timed-out set is logged, not
        fatal (the transmit script still clamps its own SDR gain to a safe range)."""
        for s in settings or []:
            atask, param, value = s.get("task"), s.get("param"), s.get("value")
            if not atask or param is None or value is None:
                continue
            args = [self._active_flag(atask, param), _fmt_num(value)]
            # Constant params (e.g. the attenuator's serial port) travel on every set — the
            # driving param alone isn't enough for the script to run.
            for cdest, cval in (s.get("consts") or {}).items():
                args += [self._active_flag(atask, cdest), str(cval)]
            try:
                await self._launch_oneshot_wait(atask, args)
            except Exception as exc:                     # never let a set derail the transmit
                logger.warning("Active component '%s' set failed: %s", atask, exc)

    async def _precommand_active(self, name: str, power: Optional[float],
                                 freq_hz: Optional[float] = None) -> None:
        """Set each linked active component (e.g. a step attenuator) for an absolute ``power``
        BEFORE the transmit task ``name`` emits — as a one-shot, so no long-running control
        task is needed and the operator never has to think about it. Awaited so the component
        is physically in position first. Best-effort: a failed/timed-out set is logged, not
        fatal (the transmit script still clamps its own SDR gain to a safe range). A no-op for
        a task without active components or without an absolute power (relative-gain mode)."""
        await self._apply_active_settings(self.active_settings(name, power, freq_hz))

    def _rf_gate(self, name: str) -> Optional[dict]:
        """The task's RF output-gate param dict (or None) from its cached argspec."""
        spec = self._script_spec(name)
        return _rf.gate((spec or {}).get("params")) if spec else None

    async def _gate_precommand(self, name: str, *, cmd: Optional[list] = None,
                               values: Optional[dict] = None) -> None:
        """Position a task's active components for a launch (``cmd``) or a live tune (``values``),
        honouring the RF output gate: OFF ⇒ MUTE the chain (every attenuator to max); ON ⇒ set it
        for the effective absolute ``--power``. The per-task ``{power, rf_on}`` is tracked so a
        tune that toggles only one of them still positions the attenuators correctly. A task with
        no RF gate behaves exactly as before (the gate is always 'on' ⇒ set for --power)."""
        gate = self._rf_gate(name)
        st = self._gate_state.setdefault(name, {"power": None, "rf_on": True, "freq_hz": None})
        if cmd is not None:                              # a launch: (re)seed from the command line
            st["power"] = _power_from_command(cmd)
            st["rf_on"] = _rf_on_from_command(cmd, gate) if gate is not None else True
            # The carrier the script folds at (its CAL_FREQ_PARAM, scaled to Hz): the components
            # are realized at the SAME frequency as the SDR gain the script sets, else on a
            # frequency-dependent chain the two belong to different SDR/attenuator splits.
            st["freq_hz"] = self._freq_of_launch(name, cmd)
        else:                                            # a live tune: update only what changed
            vals = values or {}
            if "power" in vals:
                p = vals.get("power")
                st["power"] = float(p) if isinstance(p, (int, float)) else None
            if gate is not None:
                gd = gate.get("dest") or gate.get("name")
                if gd in vals:
                    st["rf_on"] = _rf.is_on(vals.get(gd))
            fd = self._freq_dest(name)
            if fd and fd in vals:
                f = _tune_log.freq_hz_of(self._script_spec(name), {fd: vals.get(fd)})
                if f is not None:
                    st["freq_hz"] = f
        if gate is not None and not st["rf_on"]:
            await self._apply_active_settings(self._mute_settings(name, st.get("freq_hz")))
        else:
            await self._apply_active_settings(
                self.active_settings(name, st["power"], st.get("freq_hz")))

    async def restart(self, name: str, request: Optional[StartRequest] = None,
                      source: str = "manual") -> ProcessStatus:
        proc = self._get(name)
        req = request or StartRequest()
        cmd = _build_command(proc.config.command, req.args, req.replace_args)
        await self.device_free.wait()          # never open the SDR under the boot pre-image (#9 / O6)
        await self._gate_precommand(name, cmd=cmd)
        if proc.state == ProcessState.RUNNING:
            await proc.stop()
        elif proc.state == ProcessState.STOPPING or self.is_process_alive(name):
            # A stop is in flight (its SIGTERM grace): wait it out rather than launch over the live
            # process — start() refuses that (finding C1).
            if not await proc.wait_stopped():
                raise RuntimeError(f"Task '{name}' is still stopping — retry once it has stopped")
        await proc.start(self._with_launch_freq(name, req, cmd))
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
        # A deploy re-registers tasks AND may have re-uploaded scripts (same tasks.yaml, changed
        # or newly-present script files). Drop the per-script argspec caches so a changed/restored
        # script is re-read — otherwise a stale (or a previously-missing) spec would persist for
        # the life of the process. See _script_spec.
        self._script_specs.clear()
        self._active_flags.clear()
        self._tx_marker_scripts.clear()   # a re-deployed script may have adopted the marker (W9)
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