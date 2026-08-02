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
TASKS_YAML = Path(os.environ.get("SDR_TASKS_FILE", BASE_DIR / "configs" / "tasks.yaml"))
LOG_DIR    = Path(os.environ.get("SDR_LOG_DIR",   BASE_DIR / "logs"))
EVENTS_FILE = Path(os.environ.get("SDR_EVENTS_FILE", BASE_DIR / "configs" / "events.json"))
SEQUENCES_FILE = Path(os.environ.get("SDR_SEQUENCES_FILE", BASE_DIR / "configs" / "sequences.json"))
SEQUENCE_RUNS_FILE = Path(os.environ.get("SDR_SEQUENCE_RUNS_FILE", BASE_DIR / "configs" / "sequence_runs.json"))
PLANS_FILE = Path(os.environ.get("SDR_PLANS_FILE", BASE_DIR / "configs" / "plans.json"))
SCHEDULE_FILE = Path(os.environ.get("SDR_SCHEDULE_FILE", BASE_DIR / "configs" / "schedule.json"))

# ── Agent identity ────────────────────────────────────────────────────────────

import socket
HOSTNAME   = socket.gethostname()
UNIT_ID    = os.environ.get("SDR_UNIT_ID", HOSTNAME)

# ── HTTP server ───────────────────────────────────────────────────────────────

AGENT_HOST    = os.environ.get("SDR_AGENT_HOST", "0.0.0.0")
AGENT_PORT    = int(os.environ.get("SDR_AGENT_PORT", "8765"))
AGENT_VERSION = "1.0.0"

# ── Auth (optional shared secret) ────────────────────────────────────────────
# Set SDR_API_KEY on both the Pi and your client.  Leave empty to disable auth.

API_KEY = os.environ.get("SDR_API_KEY", "")


# ── Task registry ─────────────────────────────────────────────────────────────

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
            task = TaskConfig(**entry)
            tasks[task.name] = task
        except Exception as exc:
            logger.error("Skipping malformed task entry %s: %s", entry, exc)

    logger.info("Loaded %d task(s) from %s", len(tasks), TASKS_YAML)
    return tasks