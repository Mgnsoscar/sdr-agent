"""
SDR Agent — FastAPI application.

Routes
──────
GET  /health                          → {"status": "ok"}
GET  /info                            → AgentInfo
POST /reload                          → {"added": [...], "removed": [...], ...}

GET  /tasks                           → list[ProcessStatus]
GET  /tasks/{name}                    → ProcessStatus
POST /tasks/{name}/start              → ProcessStatus   body: StartRequest (optional)
POST /tasks/{name}/stop               → ProcessStatus
POST /tasks/{name}/restart            → ProcessStatus   body: StartRequest (optional)
GET  /tasks/{name}/logs?lines=100     → list[str]
WS   /tasks/{name}/logs/stream        → streaming log

GET  /events/stream                   → SSE stream of crash + lifecycle events

POST /scripts/upload                  → {"saved": filename}   multipart file upload
GET  /scripts                         → list[str]  (filenames in scripts dir)
GET  /scripts/{name}                  → {"name","content","size"}  (read a script)
DELETE /scripts/{name}                → {"deleted": name}          (delete a script)

GET  /config/tasks-yaml               → raw YAML text
PUT  /config/tasks-yaml               → write new YAML + auto-reload

GET  /tasks/{name}/history            → list[ExitRecord]  (recent exits)
GET  /system                          → SystemHealth      (CPU, temp, mem, disk, clock)
GET  /sdr                             → SdrStatus         (UHD device probe)

POST   /events                        → arm a timed event   body: CreateEventRequest
GET    /events                        → list[ScheduledEvent]
GET    /events/{id}                   → ScheduledEvent
PATCH  /events/{id}                   → change stop time     body: PatchEventRequest
DELETE /events/{id}                   → cancel (armed) or stop-now (running)

POST   /sequences                     → store a sequence     body: CreateSequenceRequest
GET    /sequences                     → list[Sequence]
GET    /sequences/{id}                → Sequence
PUT    /sequences/{id}                → update a sequence
DELETE /sequences/{id}                → delete a sequence
POST   /sequences/{id}/arm            → arm → SequenceRun    body: ArmSequenceRequest

GET    /sequence-runs                 → list[SequenceRun]
GET    /sequence-runs/{id}            → SequenceRun
PATCH  /sequence-runs/{id}            → move on-air stop     body: PatchSequenceRunRequest
DELETE /sequence-runs/{id}            → cancel (armed) or abort (running)

POST   /panic                         → emergency stop everything → PanicResult
"""
from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import (
    Depends, FastAPI, File, HTTPException, Request,
    Query, UploadFile, WebSocket, WebSocketDisconnect, status,
)
from fastapi.responses import StreamingResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

import yaml
from ruamel.yaml import YAML

from . import config as cfg
from . import system as sysmon
from .models import (
    AgentInfo, ArmSequenceRequest, CreateEventRequest, CreateSequenceRequest,
    ExitRecord, PanicResult, PatchEventRequest, PatchSequenceRunRequest,
    ProcessStatus, ScheduledEvent, SdrStatus, Sequence, SequenceRun,
    StartRequest, SystemHealth, TaskConfig,
)
from .argspec import extract_params
from .process_manager import ProcessManager
from .scheduler import Scheduler
from .sequence_runner import SequenceRunner
from .mdns import MdnsAdvertiser
from . import recovery

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ── App lifecycle ─────────────────────────────────────────────────────────────

_manager: ProcessManager | None = None
_scheduler: Scheduler | None = None
_runner: SequenceRunner | None = None
_mdns: MdnsAdvertiser | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _manager, _scheduler, _runner, _mdns
    tasks = cfg.load_tasks()
    _manager = ProcessManager(tasks, cfg.LOG_DIR, cfg.UNIT_ID)
    await _manager.startup()

    _scheduler = Scheduler(_manager, cfg.UNIT_ID, cfg.EVENTS_FILE)
    await _scheduler.startup()

    _runner = SequenceRunner(
        _manager, cfg.UNIT_ID, cfg.SEQUENCES_FILE, cfg.SEQUENCE_RUNS_FILE
    )
    await _runner.startup()

    # mDNS advertisement is best-effort; failure here never blocks startup.
    _mdns = MdnsAdvertiser(cfg.UNIT_ID, cfg.AGENT_PORT, cfg.AGENT_VERSION)
    _mdns.start()

    yield

    if _mdns:
        _mdns.stop()
    await _runner.shutdown()
    await _scheduler.shutdown()
    await _manager.shutdown()


app = FastAPI(
    title="SDR Agent",
    version=cfg.AGENT_VERSION,
    lifespan=lifespan,
)


def get_manager() -> ProcessManager:
    assert _manager is not None, "ProcessManager not initialised"
    return _manager


def get_scheduler() -> Scheduler:
    assert _scheduler is not None, "Scheduler not initialised"
    return _scheduler


def get_runner() -> SequenceRunner:
    assert _runner is not None, "SequenceRunner not initialised"
    return _runner


# ── Auth ──────────────────────────────────────────────────────────────────────

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_key(key: str | None = Depends(_api_key_header)):
    if cfg.API_KEY and key != cfg.API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok"}


# ── Reload ────────────────────────────────────────────────────────────────────

@app.post("/reload", tags=["meta"], dependencies=[Depends(verify_key)])
async def reload_tasks(manager: ProcessManager = Depends(get_manager)):
    """Re-read tasks.yaml without restarting the agent."""
    new_tasks = cfg.load_tasks()
    result = await manager.reload(new_tasks)
    logger.info("Reload complete: %s", result)
    return result


# ── Info ──────────────────────────────────────────────────────────────────────

@app.get("/info", response_model=AgentInfo, tags=["meta"],
         dependencies=[Depends(verify_key)])
async def info(manager: ProcessManager = Depends(get_manager)):
    import platform
    return AgentInfo(
        hostname       = cfg.HOSTNAME,
        unit_id        = cfg.UNIT_ID,
        agent_version  = cfg.AGENT_VERSION,
        python_version = platform.python_version(),
        tasks          = manager.task_names(),
    )


# ── Task list ─────────────────────────────────────────────────────────────────

@app.get("/tasks", response_model=list[ProcessStatus], tags=["tasks"],
         dependencies=[Depends(verify_key)])
async def list_tasks(manager: ProcessManager = Depends(get_manager)):
    return manager.all_statuses()


# ── Single task status ────────────────────────────────────────────────────────

@app.get("/tasks/{name}", response_model=ProcessStatus, tags=["tasks"],
         dependencies=[Depends(verify_key)])
async def task_status(name: str, manager: ProcessManager = Depends(get_manager)):
    try:
        return manager.status(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ── Start ─────────────────────────────────────────────────────────────────────

@app.post("/tasks/{name}/start", response_model=ProcessStatus, tags=["tasks"],
          dependencies=[Depends(verify_key)])
async def start_task(
    name: str,
    request: StartRequest | None = None,
    manager: ProcessManager = Depends(get_manager),
):
    try:
        return await manager.start(name, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ── Stop ──────────────────────────────────────────────────────────────────────

@app.post("/tasks/{name}/stop", response_model=ProcessStatus, tags=["tasks"],
          dependencies=[Depends(verify_key)])
async def stop_task(name: str, manager: ProcessManager = Depends(get_manager)):
    try:
        return await manager.stop(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ── Restart ───────────────────────────────────────────────────────────────────

@app.post("/tasks/{name}/restart", response_model=ProcessStatus, tags=["tasks"],
          dependencies=[Depends(verify_key)])
async def restart_task(
    name: str,
    request: StartRequest | None = None,
    manager: ProcessManager = Depends(get_manager),
):
    try:
        return await manager.restart(name, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ── Log fetch (HTTP) ──────────────────────────────────────────────────────────

@app.get("/tasks/{name}/logs", response_model=list[str], tags=["logs"],
         dependencies=[Depends(verify_key)])
async def get_logs(
    name: str,
    lines: int = Query(default=100, ge=1, le=10_000),
    manager: ProcessManager = Depends(get_manager),
):
    try:
        lm = manager.get_log_manager(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return await lm.tail(lines)


# ── Task exit history ─────────────────────────────────────────────────────────

@app.get("/tasks/{name}/history", response_model=list[ExitRecord], tags=["tasks"],
         dependencies=[Depends(verify_key)])
async def task_history(name: str, manager: ProcessManager = Depends(get_manager)):
    """Recent exits for a task (newest last) — useful for spotting crash loops."""
    try:
        return manager.get_history(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ── Log stream (WebSocket) ────────────────────────────────────────────────────

@app.websocket("/tasks/{name}/logs/stream")
async def stream_logs(
    name: str,
    websocket: WebSocket,
    api_key: str | None = Query(default=None, alias="api_key"),
    lines: int = Query(default=50, ge=1, le=1_000),
):
    if cfg.API_KEY and api_key != cfg.API_KEY:
        await websocket.close(code=1008, reason="Unauthorized")
        return

    try:
        manager = get_manager()
        lm = manager.get_log_manager(name)
    except (AssertionError, KeyError) as exc:
        await websocket.close(code=1008, reason=str(exc))
        return

    await websocket.accept()
    try:
        await lm.stream(websocket, lines=lines)
    except WebSocketDisconnect:
        pass


# ── Event stream (SSE) ────────────────────────────────────────────────────────

@app.get("/events/stream", tags=["events"])
async def events_stream(
    request: Request,
    api_key: str | None = Query(default=None, alias="api_key"),
    manager: ProcessManager = Depends(get_manager),
):
    """
    Server-Sent Events stream of crash + lifecycle events.

    The laptop GUI opens this with a plain GET and holds it open; the agent
    writes each event as it occurs. Because the laptop initiates the connection
    (outbound), no inbound firewall rule is needed on the laptop — this is the
    firewall-friendly replacement for outbound webhooks.

    Auth: pass ?api_key=... if the agent has a key configured (WebSocket-style,
    since EventSource can't set headers).
    """
    if cfg.API_KEY and api_key != cfg.API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    dispatcher = manager.dispatcher
    queue = dispatcher.subscribe()

    async def event_generator():
        # Initial comment line opens the stream promptly in clients.
        yield ": connected\n\n"
        import json as _json
        try:
            while True:
                try:
                    # Wait for an event, but wake periodically to send a heartbeat.
                    # The heartbeat also lets us detect a dropped client: when the
                    # client has gone, the write fails and the generator is closed
                    # by the server, triggering the finally below.
                    payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                data = _json.dumps(payload)
                etype = payload.get("type", "message")
                yield f"event: {etype}\ndata: {data}\n\n"
        except asyncio.CancelledError:
            # Normal when the client disconnects — let it propagate after cleanup.
            raise
        finally:
            dispatcher.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # disable proxy buffering if any
        },
    )


# ── Script upload ─────────────────────────────────────────────────────────────

SCRIPTS_DIR = cfg.BASE_DIR / "scripts"


@app.post("/scripts/upload", tags=["scripts"], dependencies=[Depends(verify_key)])
async def upload_script(file: UploadFile = File(...)):
    """
    Upload a .py script to /opt/sdr-agent/scripts/.
    After uploading, edit tasks.yaml and call POST /reload to register it.
    """
    if not file.filename or not file.filename.endswith(".py"):
        raise HTTPException(status_code=400, detail="Only .py files are accepted")

    if "/" in file.filename or "\\" in file.filename or file.filename in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid filename")

    dest = SCRIPTS_DIR / file.filename
    content = await file.read()

    try:
        dest.write_bytes(content)
        dest.chmod(0o755)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {exc}")

    logger.info("Script uploaded: %s (%d bytes)", file.filename, len(content))
    return {"saved": file.filename, "path": str(dest), "size": len(content)}


@app.get("/scripts", tags=["scripts"], dependencies=[Depends(verify_key)])
async def list_scripts():
    """List all .py files currently in the scripts directory."""
    return sorted(p.name for p in SCRIPTS_DIR.glob("*.py"))


def _safe_script_path(name: str):
    """Resolve a script name to a path inside SCRIPTS_DIR, rejecting traversal."""
    if "/" in name or "\\" in name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not name.endswith(".py"):
        raise HTTPException(status_code=400, detail="Only .py files are accepted")
    return SCRIPTS_DIR / name


@app.get("/scripts/{name}", tags=["scripts"], dependencies=[Depends(verify_key)])
async def get_script(name: str):
    """Return the contents of a script in the scripts directory."""
    path = _safe_script_path(name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"No such script: {name}")
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read file: {exc}")
    return {"name": name, "content": content, "size": path.stat().st_size}


@app.delete("/scripts/{name}", tags=["scripts"], dependencies=[Depends(verify_key)])
async def delete_script(name: str):
    """Delete a script from the scripts directory."""
    path = _safe_script_path(name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"No such script: {name}")
    try:
        path.unlink()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to delete file: {exc}")
    logger.info("Script deleted: %s", name)
    return {"deleted": name}


@app.get("/scripts/{name}/params", tags=["scripts"], dependencies=[Depends(verify_key)])
async def script_params(name: str):
    """Statically extract a script's parameters (no code execution).

    Returns the rich paramkit schema (kind/unit/min/max/presets) when the script
    uses paramkit, otherwise the classic argparse schema."""
    path = _safe_script_path(name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"No such script: {name}")
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read file: {exc}")
    return extract_params(source)


# ── Task registry editing (create / update / delete, with live reload) ────────
# Tasks live in tasks.yaml. We edit it with ruamel.yaml (round-trip) so its
# comments and formatting survive, then call manager.reload() so the change
# takes effect immediately — no agent restart or Pi reboot.

_yaml_rt = YAML()
_yaml_rt.preserve_quotes = True
_yaml_rt.indent(mapping=2, sequence=4, offset=2)


def _plain(obj):
    """Recursively convert ruamel types to plain python (for a safe fallback dump)."""
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_plain(v) for v in obj]
    if isinstance(obj, bool):
        return bool(obj)
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        return float(obj)
    if isinstance(obj, str):
        return str(obj)
    return obj


def _load_tasks_doc():
    doc = None
    if cfg.TASKS_YAML.exists():
        try:
            with cfg.TASKS_YAML.open() as fh:
                doc = _yaml_rt.load(fh)
        except Exception as exc:
            # A previously-corrupted file shouldn't wedge every edit — start from a
            # clean doc so the next save rewrites a valid file.
            logger.error("tasks.yaml could not be parsed (%s); starting fresh", exc)
            doc = None
    if not isinstance(doc, dict):
        doc = {}
    if doc.get("tasks") is None:
        doc["tasks"] = []
    return doc


def _save_tasks_doc(doc) -> None:
    import io
    buf = io.StringIO()
    _yaml_rt.dump(doc, buf)
    text = buf.getvalue()
    # ruamel's comment handling can emit YAML that PyYAML (used by load_tasks)
    # can't parse — when a commented entry is removed or the list empties, orphaned
    # comments end up before a `[]`. Verify the result loads; if not, fall back to a
    # plain, comment-free dump so the file on disk is always valid.
    try:
        yaml.safe_load(text)
    except yaml.YAMLError:
        text = yaml.safe_dump(
            {"tasks": _plain(doc.get("tasks") or [])},
            sort_keys=False, default_flow_style=False, allow_unicode=True,
        )
    tmp = cfg.TASKS_YAML.with_name(cfg.TASKS_YAML.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(cfg.TASKS_YAML)   # atomic swap


def _task_index(doc, name: str) -> int:
    for i, entry in enumerate(doc["tasks"]):
        if entry.get("name") == name:
            return i
    return -1


def _spec_to_entry(spec: TaskConfig) -> dict:
    """A clean tasks.yaml entry — the commonly-edited fields only."""
    return {
        "name": spec.name,
        "description": spec.description,
        "command": list(spec.command),
        "working_dir": spec.working_dir or str(SCRIPTS_DIR),
        "env": dict(spec.env),
        "autostart": spec.autostart,
        "restart_on_crash": spec.restart_on_crash,
    }


@app.post("/tasks", tags=["tasks"], dependencies=[Depends(verify_key)])
async def create_task(spec: TaskConfig, manager: ProcessManager = Depends(get_manager)):
    """Add a task to tasks.yaml and reload live (no restart needed)."""
    doc = _load_tasks_doc()
    if _task_index(doc, spec.name) >= 0:
        raise HTTPException(status_code=409, detail=f"A task named '{spec.name}' already exists")
    doc["tasks"].append(_spec_to_entry(spec))
    _save_tasks_doc(doc)
    result = await manager.reload(cfg.load_tasks())
    logger.info("Task created: %s (%s)", spec.name, result)
    return {"created": spec.name, "reload": result}


@app.put("/tasks/{name}", tags=["tasks"], dependencies=[Depends(verify_key)])
async def update_task(name: str, spec: TaskConfig,
                      manager: ProcessManager = Depends(get_manager)):
    """
    Replace a task's definition in tasks.yaml and reload live.

    If the task is currently running it keeps running with its old command; the
    new definition takes effect on the next start/restart.
    """
    doc = _load_tasks_doc()
    idx = _task_index(doc, name)
    if idx < 0:
        raise HTTPException(status_code=404, detail=f"No such task: {name}")
    if spec.name != name and _task_index(doc, spec.name) >= 0:
        raise HTTPException(status_code=409, detail=f"A task named '{spec.name}' already exists")
    doc["tasks"][idx] = _spec_to_entry(spec)
    _save_tasks_doc(doc)
    result = await manager.reload(cfg.load_tasks())
    logger.info("Task updated: %s -> %s (%s)", name, spec.name, result)
    return {"updated": name, "reload": result}


@app.delete("/tasks/{name}", tags=["tasks"], dependencies=[Depends(verify_key)])
async def delete_task(name: str, manager: ProcessManager = Depends(get_manager)):
    """Remove a task from tasks.yaml and reload live. Refuses if it's running."""
    if manager.is_running(name):
        raise HTTPException(status_code=409, detail=f"Task '{name}' is running — stop it first")
    doc = _load_tasks_doc()
    idx = _task_index(doc, name)
    if idx < 0:
        raise HTTPException(status_code=404, detail=f"No such task: {name}")
    del doc["tasks"][idx]
    _save_tasks_doc(doc)
    result = await manager.reload(cfg.load_tasks())
    logger.info("Task deleted: %s (%s)", name, result)
    return {"deleted": name, "reload": result}


# ── System health ─────────────────────────────────────────────────────────────

@app.get("/system", response_model=SystemHealth, tags=["health"],
         dependencies=[Depends(verify_key)])
async def system_health():
    """CPU load + temp + throttle state, memory, disk, uptime, load average."""
    return await sysmon.get_health(cfg.UNIT_ID)


# ── SDR device probe ──────────────────────────────────────────────────────────

@app.get("/sdr", response_model=SdrStatus, tags=["health"],
         dependencies=[Depends(verify_key)])
async def sdr_status():
    """Probe for connected UHD/Ettus devices via uhd_find_devices."""
    return await sysmon.get_sdr_status()


# ── Scheduled events ──────────────────────────────────────────────────────────

@app.post("/events", response_model=ScheduledEvent, tags=["events"],
          dependencies=[Depends(verify_key)])
async def create_event(
    req: CreateEventRequest,
    scheduler: Scheduler = Depends(get_scheduler),
):
    """
    Arm a timed event: start a task at start_at, stop it at stop_at (or after
    duration_s). Times are UTC ISO-8601. Rejects past start times and stop
    times that aren't after the start.
    """
    try:
        return await scheduler.create(req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/events", response_model=list[ScheduledEvent], tags=["events"],
         dependencies=[Depends(verify_key)])
async def list_events(scheduler: Scheduler = Depends(get_scheduler)):
    """All events and their current states (armed/running/completed/cancelled/aborted)."""
    return scheduler.list_events()


@app.get("/events/{event_id}", response_model=ScheduledEvent, tags=["events"],
         dependencies=[Depends(verify_key)])
async def get_event(event_id: str, scheduler: Scheduler = Depends(get_scheduler)):
    try:
        return scheduler.get_event(event_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.patch("/events/{event_id}", response_model=ScheduledEvent, tags=["events"],
           dependencies=[Depends(verify_key)])
async def patch_event(
    event_id: str,
    req: PatchEventRequest,
    scheduler: Scheduler = Depends(get_scheduler),
):
    """
    Change the stop time of an armed or running event to a new absolute UTC time.
    Used to extend or shorten a live broadcasting event. Rejects stop times in the past.
    """
    try:
        return await scheduler.patch_stop(event_id, req.stop_at)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/events/{event_id}", response_model=ScheduledEvent, tags=["events"],
            dependencies=[Depends(verify_key)])
async def delete_event(event_id: str, scheduler: Scheduler = Depends(get_scheduler)):
    """
    Cancel an armed event (it never fires) or stop a running event immediately.
    """
    try:
        return await scheduler.cancel(event_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ── tasks.yaml read / write ───────────────────────────────────────────────────

class YamlBody(BaseModel):
    content: str   # Raw YAML text


@app.get("/config/tasks-yaml", tags=["config"], dependencies=[Depends(verify_key)])
async def get_tasks_yaml():
    """Return the raw contents of tasks.yaml so the GUI can display and edit it."""
    if not cfg.TASKS_YAML.exists():
        return {"content": ""}
    return {"content": cfg.TASKS_YAML.read_text()}


@app.put("/config/tasks-yaml", tags=["config"], dependencies=[Depends(verify_key)])
async def put_tasks_yaml(
    body: YamlBody,
    manager: ProcessManager = Depends(get_manager),
):
    """
    Overwrite tasks.yaml with new content and immediately reload the task registry.
    The YAML is validated before writing — a bad payload is rejected without
    touching the existing file.
    """
    import yaml as _yaml

    # Validate first — don't write a broken file
    try:
        parsed = _yaml.safe_load(body.content)
    except _yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {exc}")

    if not isinstance(parsed, dict) or "tasks" not in parsed:
        raise HTTPException(
            status_code=400,
            detail="YAML must be a mapping with a top-level 'tasks' key"
        )

    # Write atomically: write to a temp file then rename so a crash mid-write
    # never leaves a corrupt tasks.yaml on disk
    tmp = cfg.TASKS_YAML.with_suffix(".yaml.tmp")
    try:
        tmp.write_text(body.content)
        tmp.rename(cfg.TASKS_YAML)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to write tasks.yaml: {exc}")

    # Reload the task registry
    new_tasks = cfg.load_tasks()
    result = await manager.reload(new_tasks)
    logger.info("tasks.yaml updated and reloaded: %s", result)

    return {"saved": True, "reload": result}

# ── Sequences (definitions, stored on this unit) ──────────────────────────────

@app.post("/sequences", response_model=Sequence, tags=["sequences"],
          dependencies=[Depends(verify_key)])
async def create_sequence(
    req: CreateSequenceRequest,
    runner: SequenceRunner = Depends(get_runner),
):
    """Store a new sequence. Validated: every referenced task must exist, and the
    sequence needs at least one start-anchored and one stop-anchored step."""
    try:
        return await runner.create_sequence(req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/sequences", response_model=list[Sequence], tags=["sequences"],
         dependencies=[Depends(verify_key)])
async def list_sequences(runner: SequenceRunner = Depends(get_runner)):
    return runner.list_sequences()


@app.get("/sequences/{seq_id}", response_model=Sequence, tags=["sequences"],
         dependencies=[Depends(verify_key)])
async def get_sequence(seq_id: str, runner: SequenceRunner = Depends(get_runner)):
    try:
        return runner.get_sequence(seq_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.put("/sequences/{seq_id}", response_model=Sequence, tags=["sequences"],
         dependencies=[Depends(verify_key)])
async def update_sequence(
    seq_id: str,
    req: CreateSequenceRequest,
    runner: SequenceRunner = Depends(get_runner),
):
    try:
        return await runner.update_sequence(seq_id, req)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/sequences/{seq_id}", tags=["sequences"],
            dependencies=[Depends(verify_key)])
async def delete_sequence(seq_id: str, runner: SequenceRunner = Depends(get_runner)):
    try:
        await runner.delete_sequence(seq_id)
        return {"deleted": seq_id}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ── Arm a sequence → creates a run ────────────────────────────────────────────

def _resolve_on_air_end(req: ArmSequenceRequest) -> str | None:
    """Resolve the on-air end from either on_air_end or on_air_duration_s.
    Returns None for an open-ended run (no stop)."""
    from datetime import datetime, timedelta, timezone

    def _parse(ts: str) -> datetime:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    if req.open_ended:
        if req.on_air_end or req.on_air_duration_s is not None:
            raise ValueError("open_ended run must not specify on_air_end or on_air_duration_s")
        return None
    if req.on_air_end and req.on_air_duration_s is not None:
        raise ValueError("provide either on_air_end or on_air_duration_s, not both")
    if req.on_air_end:
        return _parse(req.on_air_end).isoformat()
    if req.on_air_duration_s is not None:
        if req.on_air_duration_s <= 0:
            raise ValueError("on_air_duration_s must be positive")
        start = _parse(req.on_air_at)
        return (start + timedelta(seconds=req.on_air_duration_s)).isoformat()
    raise ValueError("must provide on_air_end, on_air_duration_s, or open_ended=true")


@app.post("/sequences/{seq_id}/arm", response_model=SequenceRun, tags=["sequence-runs"],
          dependencies=[Depends(verify_key)])
async def arm_sequence(
    seq_id: str,
    req: ArmSequenceRequest,
    runner: SequenceRunner = Depends(get_runner),
):
    """
    Arm a sequence at an on-air time. on_air_at is when RF goes live (T0); the
    warm-up steps fire before it. Define the end with on_air_end or
    on_air_duration_s. resume_offset_s > 0 injects a resume offset into
    resumable steps (e.g. to resume a ramp partway through).
    """
    try:
        on_air_end = _resolve_on_air_end(req)
        return await runner.arm(seq_id, req, on_air_end)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ── Sequence runs (armed/executing instances) ────────────────────────────────

@app.get("/sequence-runs", response_model=list[SequenceRun], tags=["sequence-runs"],
         dependencies=[Depends(verify_key)])
async def list_sequence_runs(runner: SequenceRunner = Depends(get_runner)):
    return runner.list_runs()


@app.get("/sequence-runs/{run_id}", response_model=SequenceRun, tags=["sequence-runs"],
         dependencies=[Depends(verify_key)])
async def get_sequence_run(run_id: str, runner: SequenceRunner = Depends(get_runner)):
    try:
        return runner.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.patch("/sequence-runs/{run_id}", response_model=SequenceRun, tags=["sequence-runs"],
           dependencies=[Depends(verify_key)])
async def patch_sequence_run(
    run_id: str,
    req: PatchSequenceRunRequest,
    runner: SequenceRunner = Depends(get_runner),
):
    """Move the on-air STOP to a new absolute UTC time. Stop-anchored steps
    (amp-off, cool-down) all follow automatically."""
    try:
        return await runner.patch_on_air_end(run_id, req.on_air_end)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/sequence-runs/{run_id}", response_model=SequenceRun, tags=["sequence-runs"],
            dependencies=[Depends(verify_key)])
async def cancel_sequence_run(run_id: str, runner: SequenceRunner = Depends(get_runner)):
    """Cancel an armed run, or abort a running run (stops every task it touches,
    halts all remaining steps so nothing re-fires)."""
    try:
        return await runner.cancel_or_abort(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ── Panic / emergency stop ────────────────────────────────────────────────────

@app.post("/panic", response_model=PanicResult, tags=["panic"],
          dependencies=[Depends(verify_key)])
async def panic(
    manager: ProcessManager = Depends(get_manager),
    scheduler: Scheduler = Depends(get_scheduler),
    runner: SequenceRunner = Depends(get_runner),
):
    """
    EMERGENCY STOP. Immediately stop all running tasks on this unit and
    cancel/abort every armed-or-running event and sequence run so nothing
    re-fires. This is the "RF off NOW" action.
    """
    return await recovery.panic_stop(manager, scheduler, runner, cfg.UNIT_ID)