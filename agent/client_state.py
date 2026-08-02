"""
ClientStateStore — the unit's replica of the PC's plans and schedule.

Plans and their schedule are authored on the PC and are cross-unit, so they are
not executed here; the unit just holds a copy so a replacement PC (knowing only
unit IPs) can pull everything back and rebuild. Persisted as two JSON files with
atomic writes (temp file + replace), loaded once at startup. Nothing here is
FastAPI-aware.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List

from .models import Plan, ScheduledPlan

logger = logging.getLogger(__name__)


class ClientStateStore:
    def __init__(self, plans_file: Path, schedule_file: Path):
        self._plans_file = Path(plans_file)
        self._schedule_file = Path(schedule_file)
        self._plans: List[Plan] = []
        self._schedule: List[ScheduledPlan] = []
        self._load()

    # ── Load / save ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        self._plans = self._read(self._plans_file, "plans", Plan)
        self._schedule = self._read(self._schedule_file, "schedule", ScheduledPlan)
        logger.info("ClientStateStore: %d plan(s), %d scheduled",
                    len(self._plans), len(self._schedule))

    @staticmethod
    def _read(path: Path, key: str, model) -> list:
        if not path.exists() or path.stat().st_size == 0:
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return [model(**item) for item in data.get(key, [])]
        except (OSError, ValueError, TypeError) as exc:
            logger.error("Could not read %s: %s", path, exc)
            return []

    @staticmethod
    def _write(path: Path, key: str, items: list) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({key: [i.model_dump() for i in items]}, indent=2),
                       encoding="utf-8")
        tmp.replace(path)

    # ── Plans ────────────────────────────────────────────────────────────────

    def get_plans(self) -> List[Plan]:
        return list(self._plans)

    def set_plans(self, plans: List[Plan]) -> None:
        self._plans = list(plans)
        self._write(self._plans_file, "plans", self._plans)
        logger.info("ClientStateStore: stored %d plan(s)", len(self._plans))

    # ── Schedule ─────────────────────────────────────────────────────────────

    def get_schedule(self) -> List[ScheduledPlan]:
        return list(self._schedule)

    def set_schedule(self, schedule: List[ScheduledPlan]) -> None:
        self._schedule = list(schedule)
        self._write(self._schedule_file, "schedule", self._schedule)
        logger.info("ClientStateStore: stored %d scheduled plan(s)", len(self._schedule))
