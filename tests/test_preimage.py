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

def test_boot_prevention_sweeps_then_preimages(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_sweep_shm_orphans", lambda: calls.append("sweep"))

    async def fake_preimage(timeout):
        calls.append(("preimage", timeout))
        return "ok"
    monkeypatch.setattr(main.sysmon, "pre_image_sdr", fake_preimage)
    monkeypatch.setattr(cfg, "PREIMAGE_ON_BOOT", True)
    monkeypatch.setattr(cfg, "PREIMAGE_TIMEOUT_S", 7.0)

    asyncio.run(main._boot_prevention())
    assert calls == ["sweep", ("preimage", 7.0)]


def test_boot_prevention_skips_preimage_when_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_sweep_shm_orphans", lambda: calls.append("sweep"))

    async def fake_preimage(timeout):
        calls.append("preimage")
        return "ok"
    monkeypatch.setattr(main.sysmon, "pre_image_sdr", fake_preimage)
    monkeypatch.setattr(cfg, "PREIMAGE_ON_BOOT", False)

    asyncio.run(main._boot_prevention())
    assert calls == ["sweep"]            # sweep still runs; pre-image skipped


def test_boot_prevention_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("sweep exploded")
    monkeypatch.setattr(main, "_sweep_shm_orphans", boom)

    async def preimage_boom(timeout):
        raise RuntimeError("probe exploded")
    monkeypatch.setattr(main.sysmon, "pre_image_sdr", preimage_boom)
    monkeypatch.setattr(cfg, "PREIMAGE_ON_BOOT", True)

    asyncio.run(main._boot_prevention())   # both failures swallowed → no exception
