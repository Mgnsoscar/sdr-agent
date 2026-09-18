"""
RF-fault prevention (docs/rf-fault-recovery.md §3.5): the /dev/shm staging-hygiene contract.

paramkit.txstage tags each staged dir 'sdrtx-<pid>-…' so the agent can sweep ONLY our own
orphans left by a hard-killed task — never a live sibling, a foreign object, or GNU Radio's own
vmcircbuf buffers. The agent runs the sweep at boot, before every managed launch, and after each
task ends. These tests pin the sweep's safety contract and its wiring into the launch path.
"""
import asyncio
import os
from pathlib import Path

import pytest

from paramkit import txstage
from agent import process_manager as pm
from agent import config as cfg
from agent.log_manager import LogManager
from agent.models import TaskConfig


def test_staging_dir_is_tagged_with_the_pid(tmp_path):
    d = txstage.staging_dir("gal_e5", base=str(tmp_path))
    name = os.path.basename(d)
    assert name.startswith(f"sdrtx-{os.getpid()}-")
    assert "gal_e5" in name
    assert os.path.isdir(d)


def test_sweep_removes_only_dead_pid_tagged_orphans(tmp_path):
    base = str(tmp_path)
    live = txstage.staging_dir("live", base=base)                 # our pid → alive
    dead = os.path.join(base, "sdrtx-2147480000-chirp-xyz")       # implausible pid → dead
    os.makedirs(dead)
    foreign = os.path.join(base, "gr-buffer-abc")                 # not our prefix
    os.makedirs(foreign)
    unparsed = os.path.join(base, "sdrtx-notapid-zzz")            # tagged but no numeric pid
    os.makedirs(unparsed)

    removed = txstage.sweep_orphans(base=base)

    assert removed == 1
    assert os.path.isdir(live)          # live pid → kept
    assert not os.path.exists(dead)     # dead pid → reclaimed
    assert os.path.isdir(foreign)       # foreign object → never touched
    assert os.path.isdir(unparsed)      # unparseable pid → left alone (safe)


def test_sweep_no_base_is_a_noop(tmp_path, monkeypatch):
    # Absent /dev/shm (and no base) → 0, never raises.
    monkeypatch.setattr(txstage, "_SHM_ROOT", str(tmp_path / "does-not-exist"))
    assert txstage.sweep_orphans() == 0


def test_pid_alive_classification():
    assert txstage._pid_alive(os.getpid()) is True
    assert txstage._pid_alive(0) is False
    assert txstage._pid_alive(-1) is False
    assert txstage._pid_alive(2147480000) is False   # implausibly high → not running


# ── agent wiring ──────────────────────────────────────────────────────────────

def test_agent_sweep_honours_the_disable_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(txstage, "_SHM_ROOT", str(tmp_path))
    dead = os.path.join(str(tmp_path), "sdrtx-2147480000-x-y")
    os.makedirs(dead)
    monkeypatch.setattr(cfg, "SHM_SWEEP_ENABLED", False)
    assert pm._sweep_shm_orphans() == 0
    assert os.path.exists(dead)                       # disabled → left in place
    monkeypatch.setattr(cfg, "SHM_SWEEP_ENABLED", True)
    assert pm._sweep_shm_orphans() == 1
    assert not os.path.exists(dead)


class _FakeProc:
    pid = 4321

    def __init__(self):
        self.returncode = None

    async def wait(self):
        await asyncio.sleep(0)
        self.returncode = 0
        return 0


def test_launch_sweeps_a_prior_hard_kill_orphan(tmp_path, monkeypatch):
    """§12 P0 acceptance: a launch after a simulated hard kill reclaims the dead task's staged
    /dev/shm dir (so it can't starve the new flowgraph's buffers), and never touches a live one."""
    shm = tmp_path / "shm"
    shm.mkdir()
    monkeypatch.setattr(txstage, "_SHM_ROOT", str(shm))
    monkeypatch.setattr(cfg, "SHM_SWEEP_ENABLED", True)

    dead = shm / "sdrtx-2147480000-gal_e5-abc"       # a SIGKILLed prior task's orphan
    dead.mkdir()
    live = txstage.staging_dir("other", base=str(shm))

    async def fake_exec(*args, **kwargs):
        return _FakeProc()
    monkeypatch.setattr(pm.asyncio, "create_subprocess_exec", fake_exec)

    task = TaskConfig(name="t", command=["python3", str(tmp_path / "s.py")],
                      working_dir=str(tmp_path))
    proc = pm.ManagedProcess(task, LogManager(tmp_path, "t"), pm.EventDispatcher(), unit_id="u")
    asyncio.run(proc.start())

    assert not dead.exists()            # the orphan was reclaimed before the launch
    assert os.path.isdir(live)          # a live task's staging is untouched
