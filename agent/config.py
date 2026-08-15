"""
Loads agent configuration from environment variables and tasks.yaml.
"""
from __future__ import annotations

import os
import yaml
import logging
from pathlib import Path
from typing import Dict

from .models import TaskConfig

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────

BASE_DIR   = Path(os.environ.get("SDR_AGENT_BASE", "/opt/sdr-agent"))
# State (configs, logs, runtime) lives in STATE_DIR, decoupled from the code so an
# OTA update — which replaces the code dir — never touches tasks/sequences/plans/
# logs. Defaults to BASE_DIR, so a classic single-dir install is unchanged; a
# versioned OTA install sets SDR_STATE_DIR to a shared dir outside the release.
STATE_DIR  = Path(os.environ.get("SDR_STATE_DIR", BASE_DIR))
TASKS_YAML = Path(os.environ.get("SDR_TASKS_FILE", STATE_DIR / "configs" / "tasks.yaml"))
LOG_DIR    = Path(os.environ.get("SDR_LOG_DIR",   STATE_DIR / "logs"))
EVENTS_FILE = Path(os.environ.get("SDR_EVENTS_FILE", STATE_DIR / "configs" / "events.json"))
SEQUENCES_FILE = Path(os.environ.get("SDR_SEQUENCES_FILE", STATE_DIR / "configs" / "sequences.json"))
SEQUENCE_RUNS_FILE = Path(os.environ.get("SDR_SEQUENCE_RUNS_FILE", STATE_DIR / "configs" / "sequence_runs.json"))
PLANS_FILE = Path(os.environ.get("SDR_PLANS_FILE", STATE_DIR / "configs" / "plans.json"))
SCHEDULE_FILE = Path(os.environ.get("SDR_SCHEDULE_FILE", STATE_DIR / "configs" / "schedule.json"))
# Per-run control sockets for live-parameter tuning (paramkit.live). Kept short —
# AF_UNIX paths are capped at ~108 bytes — and outside configs/ since they're
# ephemeral runtime state, not saved config.
CTRL_DIR   = Path(os.environ.get("SDR_CTRL_DIR", STATE_DIR / "run" / "ctl"))

# ── OTA update layout ─────────────────────────────────────────────────────────
# Release dirs live under RELEASES_DIR as <version>/ (the code), and BASE_DIR is a
# symlink to the active one that the updater flips atomically. OTA markers live in
# RELEASES_DIR/.markers (never inside a replaced release). All three are outside
# the code dir, so they survive an update. A classic install leaves BASE_DIR a
# plain dir and simply never uses these.
RELEASES_DIR = Path(os.environ.get("SDR_RELEASES_DIR", str(BASE_DIR) + "-releases"))
CURRENT_LINK = Path(os.environ.get("SDR_CURRENT_LINK", BASE_DIR))
# The systemd unit the agent restarts to load a freshly-activated release.
SERVICE_NAME = os.environ.get("SDR_SERVICE_NAME", "sdr-agent")
# The agent marks a freshly-activated release healthy after serving this long…
UPDATE_CONFIRM_DELAY_S = float(os.environ.get("SDR_UPDATE_CONFIRM_DELAY_S", "30"))
# …and the external confirm timer rolls it back if it's still unconfirmed after
# this long (larger than the confirm delay, so a healthy agent always wins the race).
UPDATE_HEALTH_GRACE_S = float(os.environ.get("SDR_UPDATE_HEALTH_GRACE_S", "90"))

# ── Agent identity ────────────────────────────────────────────────────────────

import socket
HOSTNAME   = socket.gethostname()
UNIT_ID    = os.environ.get("SDR_UNIT_ID", HOSTNAME)


def machine_id() -> str:
    """A stable, unique identifier for this physical machine, from
    /etc/machine-id (generated once at OS install; survives hostname changes and
    reboots). Empty string if it can't be read. This is the client's reliable
    fingerprint for 'the same Pi', independent of hostname/IP/label."""
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            v = Path(p).read_text(encoding="utf-8").strip()
            if v:
                return v
        except OSError:
            continue
    return ""


MACHINE_ID = machine_id()

# ── HTTP server ───────────────────────────────────────────────────────────────

AGENT_HOST    = os.environ.get("SDR_AGENT_HOST", "0.0.0.0")
AGENT_PORT    = int(os.environ.get("SDR_AGENT_PORT", "8765"))
AGENT_VERSION = "1.0.0"

# ── Auth (optional shared secret) ────────────────────────────────────────────
# Set SDR_API_KEY on both the Pi and your client.  Leave empty to disable auth.

API_KEY = os.environ.get("SDR_API_KEY", "")


# ── Task registry ─────────────────────────────────────────────────────────────

def _env_str(v) -> str:
    """Coerce a YAML-parsed env value back to the string env always is. Bools and
    null get conventional forms rather than Python's 'True'/'None'."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return ""
    return str(v)


def load_tasks() -> Dict[str, TaskConfig]:
    """Parse tasks.yaml and return a dict keyed by task name."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if not TASKS_YAML.exists():
        logger.warning("tasks.yaml not found at %s — no tasks registered", TASKS_YAML)
        return {}

    try:
        with TASKS_YAML.open() as fh:
            raw = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        logger.error("tasks.yaml is not valid YAML (%s) — registering no tasks", exc)
        return {}

    tasks: Dict[str, TaskConfig] = {}
    for entry in raw.get("tasks", []):
        try:
            if isinstance(entry, dict) and isinstance(entry.get("env"), dict):
                # Env is all strings, but YAML may have parsed a value like `on` or
                # `8080` as a bool/int (e.g. a hand-edited or legacy file). Coerce
                # so a stray type never drops the whole task.
                entry = {**entry, "env": {str(k): _env_str(v)
                                          for k, v in entry["env"].items()}}
            task = TaskConfig(**entry)
            tasks[task.name] = task
        except Exception as exc:
            logger.error("Skipping malformed task entry %s: %s", entry, exc)

    logger.info("Loaded %d task(s) from %s", len(tasks), TASKS_YAML)
    return tasks