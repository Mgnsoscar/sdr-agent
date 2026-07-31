"""
A task's stdout is redirected to its log file, so Python would block-buffer
print() output — making it appear late or not at all, while logging (stderr)
stays prompt.  The ProcessManager must launch tasks with PYTHONUNBUFFERED so both
streams flush live.  These tests capture the env handed to the subprocess and
assert the flag is set, even when the agent's own environment doesn't have it.
"""
import asyncio
import os
from pathlib import Path
from unittest import mock

import pytest

from agent import process_manager as pm
from agent.log_manager import LogManager
from agent.models import TaskConfig


class _FakeProc:
    pid = 4321

    def __init__(self):
        self.returncode = None

    async def wait(self):
        await asyncio.sleep(0)
        self.returncode = 0
        return 0


def _capture_launch(monkeypatch):
    """Patch create_subprocess_exec to record its kwargs and return a fake proc."""
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr(pm.asyncio, "create_subprocess_exec", fake_exec)
    # Ensure the ambient env does NOT already carry the flag, so we prove the
    # ProcessManager adds it rather than inheriting it.
    monkeypatch.delenv("PYTHONUNBUFFERED", raising=False)
    return captured


def _task(tmp_path: Path) -> TaskConfig:
    return TaskConfig(
        name="t",
        command=["python3", str(tmp_path / "s.py")],
        working_dir=str(tmp_path),
    )


def test_start_sets_pythonunbuffered(tmp_path, monkeypatch):
    captured = _capture_launch(monkeypatch)
    proc = pm.ManagedProcess(_task(tmp_path), LogManager(tmp_path, "t"),
                             pm.EventDispatcher(), unit_id="u")
    asyncio.run(proc.start())
    assert captured["env"]["PYTHONUNBUFFERED"] == "1"


def test_start_respects_explicit_override(tmp_path, monkeypatch):
    captured = _capture_launch(monkeypatch)
    cfg = _task(tmp_path)
    cfg.env = {"PYTHONUNBUFFERED": "0"}   # a task that deliberately wants buffering
    proc = pm.ManagedProcess(cfg, LogManager(tmp_path, "t"),
                             pm.EventDispatcher(), unit_id="u")
    asyncio.run(proc.start())
    assert captured["env"]["PYTHONUNBUFFERED"] == "0"


def test_oneshot_sets_pythonunbuffered(tmp_path, monkeypatch):
    captured = _capture_launch(monkeypatch)
    mgr = pm.ProcessManager({"t": _task(tmp_path)}, tmp_path, unit_id="u")
    asyncio.run(mgr.run_oneshot("t", ["-a", "20"]))
    assert captured["env"]["PYTHONUNBUFFERED"] == "1"
