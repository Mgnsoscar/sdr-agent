"""
RF-fault prevention (docs/rf-fault-recovery.md §3.7): pre-image the SDR at boot.

A lightweight device OPEN via `uhd_usrp_probe` loads the FPGA image at boot, while nothing holds
the device, so the first transmit task warms up fast instead of paying the one-time image load at
on-air. It uses `uhd_usrp_probe` (opens + loads the image), NOT `uhd_find_devices` (enumerate only),
and is a best-effort no-op with no radio on PATH — so dev / CI / mock units are unaffected.
"""
import asyncio
from unittest import mock

import pytest

from agent import system as sysmon
from agent import main
from agent import config as cfg


def test_preimage_noop_without_uhd_usrp_probe(monkeypatch):
    monkeypatch.setattr(sysmon.shutil, "which", lambda name: None)
    ran = {"called": False}

    def fake_run(*a, **k):
        ran["called"] = True
    monkeypatch.setattr(sysmon.subprocess, "run", fake_run)

    result = sysmon._preimage_sdr(timeout=5)
    assert "not on PATH" in result
    assert ran["called"] is False        # never shells out when the tool is absent


def test_preimage_uses_uhd_usrp_probe_not_find_devices(monkeypatch):
    monkeypatch.setattr(sysmon.shutil, "which",
                        lambda name: "/usr/bin/uhd_usrp_probe" if name == "uhd_usrp_probe" else None)
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        class _R:  # noqa: D401
            stdout = ""; stderr = ""
        return _R()
    monkeypatch.setattr(sysmon.subprocess, "run", fake_run)

    result = sysmon._preimage_sdr(timeout=5)
    assert seen["argv"][0] == "/usr/bin/uhd_usrp_probe"
    assert "uhd_find_devices" not in seen["argv"][0]
    assert "pre-imaged" in result


def test_preimage_swallows_timeout_and_oserror(monkeypatch):
    monkeypatch.setattr(sysmon.shutil, "which", lambda name: "/usr/bin/uhd_usrp_probe")

    def raise_timeout(*a, **k):
        raise sysmon.subprocess.TimeoutExpired(cmd="uhd_usrp_probe", timeout=5)
    monkeypatch.setattr(sysmon.subprocess, "run", raise_timeout)
    assert "timed out" in sysmon._preimage_sdr(timeout=5)   # never raises

    def raise_oserror(*a, **k):
        raise OSError("boom")
    monkeypatch.setattr(sysmon.subprocess, "run", raise_oserror)
    assert "failed" in sysmon._preimage_sdr(timeout=5)


# ── boot wiring ─────────────────────────────────────────────────────────────

class _FakeManager:
    def __init__(self, running):
        self._running = set(running)

    def task_names(self):
        return ["a", "b", "c"]

    def is_running(self, name):
        return name in self._running


def test_boot_sweep_runs_and_never_raises(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_sweep_shm_orphans", lambda: calls.append("sweep"))
    main._boot_sweep()
    assert calls == ["sweep"]

    def boom():
        raise RuntimeError("sweep exploded")
    monkeypatch.setattr(main, "_sweep_shm_orphans", boom)
    main._boot_sweep()                   # swallowed → no exception


def test_preimage_when_idle_preimages_when_device_free(monkeypatch):
    monkeypatch.setattr(main, "_manager", None)      # no manager → device assumed free
    monkeypatch.setattr(cfg, "PREIMAGE_ON_BOOT", True)
    monkeypatch.setattr(cfg, "PREIMAGE_TIMEOUT_S", 7.0)
    seen = {}

    async def fake_preimage(timeout):
        seen["timeout"] = timeout
        return "imaged"
    monkeypatch.setattr(main.sysmon, "pre_image_sdr", fake_preimage)

    asyncio.run(main._preimage_when_idle())
    assert seen["timeout"] == 7.0


def test_preimage_when_idle_skips_when_a_task_holds_the_device(monkeypatch):
    monkeypatch.setattr(main, "_manager", _FakeManager(running={"b"}))
    monkeypatch.setattr(cfg, "PREIMAGE_ON_BOOT", True)
    called = {"n": 0}

    async def fake_preimage(timeout):
        called["n"] += 1
        return "imaged"
    monkeypatch.setattr(main.sysmon, "pre_image_sdr", fake_preimage)

    asyncio.run(main._preimage_when_idle())
    assert called["n"] == 0              # a running task holds the device → never probed


def test_preimage_when_idle_skips_when_disabled(monkeypatch):
    monkeypatch.setattr(main, "_manager", None)
    monkeypatch.setattr(cfg, "PREIMAGE_ON_BOOT", False)
    called = {"n": 0}

    async def fake_preimage(timeout):
        called["n"] += 1
    monkeypatch.setattr(main.sysmon, "pre_image_sdr", fake_preimage)

    asyncio.run(main._preimage_when_idle())
    assert called["n"] == 0


def test_preimage_when_idle_hard_bound_is_caught(monkeypatch):
    """A wedged probe whose reaping never returns must not hang the coroutine: the outer wait_for
    bound raises TimeoutError, which is caught — the agent keeps running."""
    monkeypatch.setattr(main, "_manager", None)
    monkeypatch.setattr(cfg, "PREIMAGE_ON_BOOT", True)

    async def hang(timeout):
        return "never"
    monkeypatch.setattr(main.sysmon, "pre_image_sdr", hang)

    async def fake_wait_for(coro, timeout):
        coro.close()                     # avoid 'coroutine was never awaited'
        raise asyncio.TimeoutError
    monkeypatch.setattr(main.asyncio, "wait_for", fake_wait_for)

    asyncio.run(main._preimage_when_idle())   # TimeoutError swallowed → no exception


def test_preimage_when_idle_never_raises(monkeypatch):
    monkeypatch.setattr(main, "_manager", None)
    monkeypatch.setattr(cfg, "PREIMAGE_ON_BOOT", True)

    async def preimage_boom(timeout):
        raise RuntimeError("probe exploded")
    monkeypatch.setattr(main.sysmon, "pre_image_sdr", preimage_boom)

    asyncio.run(main._preimage_when_idle())   # swallowed → no exception
