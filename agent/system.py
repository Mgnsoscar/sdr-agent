"""
System health monitoring and SDR device probing.

Health uses psutil plus Raspberry-Pi-specific reads for temperature and
throttle state.  SDR probing shells out to `uhd_find_devices`.

All blocking calls are dispatched to a thread pool so they never stall
the asyncio event loop.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psutil

from .models import SdrDevice, SdrStatus, SystemHealth

logger = logging.getLogger(__name__)

# Boot time is constant for the life of the process
_BOOT_TIME = psutil.boot_time()

# Pi thermal zone — works on Raspberry Pi OS
_THERMAL_PATH = Path("/sys/class/thermal/thermal_zone0/temp")


# ── CPU temperature ───────────────────────────────────────────────────────────

def _read_cpu_temp() -> Optional[float]:
    """Read CPU temp in °C from the Pi thermal zone. Returns None if unavailable."""
    try:
        raw = _THERMAL_PATH.read_text().strip()
        return round(int(raw) / 1000.0, 1)   # millidegrees → °C
    except (OSError, ValueError):
        # Fall back to psutil if the thermal zone isn't present
        try:
            temps = psutil.sensors_temperatures()
            for entries in temps.values():
                if entries:
                    return round(entries[0].current, 1)
        except Exception:
            pass
        return None


# ── Throttle state ────────────────────────────────────────────────────────────

def _read_throttled() -> Optional[bool]:
    """
    Use vcgencmd to check if the Pi is currently throttled.
    Bit 0 of the throttled flag = under-voltage now.
    Bit 2 = currently throttled.  Returns None if vcgencmd is unavailable.
    """
    if shutil.which("vcgencmd") is None:
        return None
    try:
        out = subprocess.run(
            ["vcgencmd", "get_throttled"],
            capture_output=True, text=True, timeout=3,
        )
        # Output looks like: "throttled=0x50000"
        m = re.search(r"throttled=0x([0-9a-fA-F]+)", out.stdout)
        if not m:
            return None
        flags = int(m.group(1), 16)
        # Currently-throttled (bit 2) OR currently under-voltage (bit 0)
        return bool(flags & 0x1) or bool(flags & 0x4)
    except (subprocess.SubprocessError, OSError):
        return None


# ── Clock / NTP sync ──────────────────────────────────────────────────────────

def _read_clock_sync() -> tuple[Optional[bool], str]:
    """
    Determine whether the system clock is NTP-synchronized.

    Tries `timedatectl` (systemd-timesyncd / chrony) first since that's the
    Raspberry Pi OS default. Returns (synced, source). synced is None if it
    can't be determined.
    """
    if shutil.which("timedatectl") is None:
        return None, ""
    try:
        out = subprocess.run(
            ["timedatectl", "show",
             "--property=NTPSynchronized", "--property=NTP"],
            capture_output=True, text=True, timeout=3,
        )
        synced: Optional[bool] = None
        for line in out.stdout.splitlines():
            if line.startswith("NTPSynchronized="):
                synced = line.split("=", 1)[1].strip().lower() == "yes"
        # Best-effort source detection
        source = ""
        if shutil.which("chronyc"):
            source = "chrony"
        elif Path("/run/systemd/timesync").exists() or shutil.which("timedatectl"):
            source = "systemd-timesyncd"
        return synced, source
    except (subprocess.SubprocessError, OSError):
        return None, ""


# ── Health snapshot ───────────────────────────────────────────────────────────

def _collect_health(unit_id: str) -> SystemHealth:
    """Synchronous health collection (runs in thread pool)."""
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    try:
        load = list(psutil.getloadavg())
    except (OSError, AttributeError):
        load = [0.0, 0.0, 0.0]

    clock_synced, clock_source = _read_clock_sync()

    return SystemHealth(
        unit_id       = unit_id,
        cpu_percent   = psutil.cpu_percent(interval=0.3),
        cpu_temp_c    = _read_cpu_temp(),
        cpu_throttled = _read_throttled(),
        mem_percent   = vm.percent,
        mem_used_mb   = round(vm.used / 1024 / 1024, 1),
        mem_total_mb  = round(vm.total / 1024 / 1024, 1),
        disk_percent  = disk.percent,
        disk_free_gb  = round(disk.free / 1024 / 1024 / 1024, 2),
        uptime_s      = round(time.time() - _BOOT_TIME, 1),
        load_avg      = [round(x, 2) for x in load],
        utc_now       = datetime.now(timezone.utc).isoformat(),
        clock_synced  = clock_synced,
        clock_source  = clock_source,
    )


async def get_health(unit_id: str) -> SystemHealth:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _collect_health, unit_id)


# ── SDR probing ───────────────────────────────────────────────────────────────

def _parse_uhd_output(text: str) -> list[SdrDevice]:
    """
    Parse `uhd_find_devices` output into SdrDevice objects.

    The output groups devices in blocks like:
        --------------------------------------------------
        -- UHD Device 0
        --------------------------------------------------
        Device Address:
            serial: 30ABCDE
            name: MyB206
            product: B200
            type: b200
    """
    devices: list[SdrDevice] = []
    current: dict[str, str] = {}

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("-- UHD Device"):
            if current:
                devices.append(SdrDevice(**current))
                current = {}
        elif ":" in line:
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()
            if key in ("type", "serial", "name", "product"):
                current[key] = val

    if current:
        devices.append(SdrDevice(**current))

    return devices


def _probe_sdr() -> SdrStatus:
    """Synchronous SDR probe (runs in thread pool)."""
    if shutil.which("uhd_find_devices") is None:
        return SdrStatus(
            detected=False, device_count=0, devices=[],
            error="uhd_find_devices not found on PATH",
        )

    try:
        out = subprocess.run(
            ["uhd_find_devices"],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        return SdrStatus(
            detected=False, device_count=0, devices=[],
            error="uhd_find_devices timed out",
        )
    except OSError as exc:
        return SdrStatus(
            detected=False, device_count=0, devices=[],
            error=f"probe failed: {exc}",
        )

    combined = (out.stdout or "") + (out.stderr or "")

    # "No UHD Devices Found" is the canonical empty result
    if "No UHD Devices Found" in combined:
        return SdrStatus(
            detected=False, device_count=0, devices=[],
            raw_output=combined.strip(),
        )

    devices = _parse_uhd_output(combined)
    return SdrStatus(
        detected=len(devices) > 0,
        device_count=len(devices),
        devices=devices,
        raw_output=combined.strip(),
    )


async def get_sdr_status() -> SdrStatus:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _probe_sdr)