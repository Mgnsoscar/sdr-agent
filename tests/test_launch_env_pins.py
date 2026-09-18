"""
RF-fault prevention (docs/rf-fault-recovery.md §3.4/§3.6/§3.7): the launch-env PINS.

Every transmit task launches with HOME (a stable, writable home), the GNU Radio vmcircbuf backend
pin (GR_CONF_VMCIRCBUF_DEFAULT_FACTORY — the only pin GR reads, since the scripts set
GR_DONT_LOAD_PREFS=1), and UHD file logging (UHD_LOG_FILE + level, capturing the FPGA image load
while the console stays off). The pins sit ABOVE ambient os.environ but BELOW the task's own cfg.env
and per-request env_overrides, so a task can still override any of them.
"""
import asyncio
import os
from pathlib import Path

import pytest

from agent import process_manager as pm
from agent import config as cfg
from agent.log_manager import LogManager
from agent.models import TaskConfig, StartRequest


class _FakeProc:
    pid = 4321

    def __init__(self):
        self.returncode = None

    async def wait(self):
        await asyncio.sleep(0)
        self.returncode = 0
        return 0


def _capture(monkeypatch):
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr(pm.asyncio, "create_subprocess_exec", fake_exec)
    return captured


def _task(tmp_path) -> TaskConfig:
    return TaskConfig(name="t", command=["python3", str(tmp_path / "s.py")],
                      working_dir=str(tmp_path))


def _start(tmp_path, monkeypatch, cfg_env=None, req=None):
    captured = _capture(monkeypatch)
    task = _task(tmp_path)
    if cfg_env is not None:
        task.env = cfg_env
    proc = pm.ManagedProcess(task, LogManager(tmp_path, "t"), pm.EventDispatcher(), unit_id="u")
    asyncio.run(proc.start(req))
    return captured["env"]


def test_start_pins_home_gr_backend_and_uhd_log(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "TASK_HOME", "/writable/home")
    monkeypatch.setattr(cfg, "GR_VMCIRCBUF_FACTORY", "mmap_shm_open")
    monkeypatch.setattr(cfg, "UHD_LOG_FILE_LEVEL", "info")
    env = _start(tmp_path, monkeypatch)
    assert env["HOME"] == "/writable/home"
    assert env["GR_CONF_VMCIRCBUF_DEFAULT_FACTORY"] == "mmap_shm_open"
    assert env["UHD_LOG_FILE"] == str(LogManager(tmp_path, "t").task_dir / "uhd.log")
    assert env["UHD_LOG_FILE_LEVEL"] == "info"


def test_home_pin_overrides_ambient(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/ambient/home")       # ambient os.environ HOME
    monkeypatch.setattr(cfg, "TASK_HOME", "/writable/home")
    env = _start(tmp_path, monkeypatch)
    assert env["HOME"] == "/writable/home"            # the pin beats ambient


def test_task_cfg_env_overrides_the_pins(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "TASK_HOME", "/writable/home")
    monkeypatch.setattr(cfg, "GR_VMCIRCBUF_FACTORY", "mmap_shm_open")
    env = _start(tmp_path, monkeypatch,
                 cfg_env={"HOME": "/task/home",
                          "GR_CONF_VMCIRCBUF_DEFAULT_FACTORY": "mmap_tmpfile"})
    assert env["HOME"] == "/task/home"
    assert env["GR_CONF_VMCIRCBUF_DEFAULT_FACTORY"] == "mmap_tmpfile"


def test_request_env_overrides_the_pins(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "TASK_HOME", "/writable/home")
    env = _start(tmp_path, monkeypatch, req=StartRequest(env_overrides={"HOME": "/req/home"}))
    assert env["HOME"] == "/req/home"


def test_blank_config_omits_the_key(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "GR_VMCIRCBUF_FACTORY", "")       # unconfigured → no pin
    monkeypatch.setattr(cfg, "UHD_LOG_FILE_LEVEL", "")
    monkeypatch.setattr(cfg, "TASK_HOME", "")
    env = _start(tmp_path, monkeypatch)
    assert "GR_CONF_VMCIRCBUF_DEFAULT_FACTORY" not in env
    assert "UHD_LOG_FILE" not in env
    # HOME falls back to ambient (not pinned) — never a stray empty HOME
    assert env.get("HOME", None) != ""


def test_oneshot_also_pins_home_and_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "TASK_HOME", "/writable/home")
    monkeypatch.setattr(cfg, "GR_VMCIRCBUF_FACTORY", "mmap_shm_open")
    captured = _capture(monkeypatch)
    mgr = pm.ProcessManager({"t": _task(tmp_path)}, tmp_path, unit_id="u")
    asyncio.run(mgr.run_oneshot("t", ["-a", "20"]))
    assert captured["env"]["HOME"] == "/writable/home"
    assert captured["env"]["GR_CONF_VMCIRCBUF_DEFAULT_FACTORY"] == "mmap_shm_open"
