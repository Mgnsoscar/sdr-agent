"""
System health monitoring and SDR device probing.

Health uses psutil plus Raspberry-Pi-specific reads for temperature and
throttle state.  SDR probing shells out to `uhd_find_devices`.

All blocking calls are dispatched to a thread pool so they never stall
the asyncio event loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psutil

from .models import FaultSnapshot, SdrDevice, SdrStatus, SystemHealth

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

# Set by _set_clock when the operator sets the clock by hand (e.g. "sync to PC
# clock" on a no-internet rig). It means "this clock was last disciplined manually
# and NTP has not since taken over" — cleared the moment we observe NTPSynchronized,
# so once real internet time returns we report that instead. Uptime-scoped: a reboot
# clears it (the flag, not the clock), which is correct — we no longer know the
# clock was hand-set.
_manual_clock_set = False


def _read_clock_sync() -> tuple[Optional[bool], str]:
    """
    Determine whether the system clock is trustworthy and how it's disciplined.

    Returns (ntp_synced, source):
      - ntp_synced: True if `timedatectl` reports NTPSynchronized (real NTP/internet
        time), False if not, None if it can't be determined.
      - source: "chrony" / "systemd-timesyncd" when NTP-synced; "manual" when the
        clock was last set by hand (synced to the PC clock and NTP hasn't taken
        over); "" otherwise.

    The client renders the pair together: NTP-synced → "internet time", manual →
    "PC clock", neither → "unsynced". A manual set deliberately leaves NTP enabled
    (see _set_clock), so NTPSynchronized stays "no" until real internet time
    returns — which is exactly why the manual flag, not NTPSynchronized, is what
    tells the operator their PC-clock sync took.
    """
    global _manual_clock_set
    if shutil.which("timedatectl") is None:
        # Can't read NTP state. If we set the clock by hand we still know that.
        return (False, "manual") if _manual_clock_set else (None, "")
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
        if synced:
            _manual_clock_set = False   # NTP is the authority now; forget the hand-set
            source = "chrony" if shutil.which("chronyc") else "systemd-timesyncd"
            return True, source
        if _manual_clock_set:
            return False, "manual"
        return synced, ""   # False (running free) or None (couldn't parse)
    except (subprocess.SubprocessError, OSError):
        return (False, "manual") if _manual_clock_set else (None, "")


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


# ── Clock setting ──────────────────────────────────────────────────────────────

def _set_clock(epoch: float) -> tuple[bool, str]:
    """Set the system clock to `epoch` (UTC seconds since 1970). Runs as root via
    the systemd service, so `date` succeeds without extra privilege.

    We use `date -u -s @<epoch>` rather than `timedatectl set-time` because the
    latter refuses while NTP is enabled — and we deliberately DON'T disable NTP:
    on a Pi with no internet (a direct-ethernet test rig) timesyncd has nothing to
    correct against, so the manual time sticks; once the Pi is back online it
    re-syncs to real time on its own. Returns (ok, detail-or-new-utc)."""
    if shutil.which("date") is None:
        return False, "`date` command not found"
    try:
        proc = subprocess.run(
            ["date", "-u", "-s", f"@{epoch:.3f}"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"could not run date: {exc}"
    if proc.returncode != 0:
        # Most likely cause: the agent isn't running as root.
        return False, (proc.stderr.strip()
                       or f"date exited {proc.returncode} (is the agent root?)")
    # Remember we disciplined the clock by hand, so health reports "PC clock" rather
    # than "unsynced" until NTP (real internet time) takes over.
    global _manual_clock_set
    _manual_clock_set = True
    return True, datetime.now(timezone.utc).isoformat()


async def set_clock(epoch: float) -> tuple[bool, str]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _set_clock, epoch)


# ── SDR probing ───────────────────────────────────────────────────────────────

def _parse_uhd_output(text: str) -> list[dict]:
    """
    Parse `uhd_find_devices` output into raw per-device dicts (every Device Address
    key), so the caller can tell a locally-attached device from a networked USRP
    merely discovered on the LAN.

    The output groups devices in blocks like:
        --------------------------------------------------
        -- UHD Device 0
        --------------------------------------------------
        Device Address:
            serial: 30ABCDE
            name: MyB206
            product: B200
            type: b200
    A network-discovered USRP (e.g. an X4xx seen from another host) additionally
    carries an `addr:` line with its IP; a USB or on-board device has none.
    """
    devices: list[dict] = []
    current: dict[str, str] = {}

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("-- UHD Device"):
            if current:
                devices.append(current)
                current = {}
        elif ":" in line:
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()
            if key and val:
                current[key] = val

    if current:
        devices.append(current)

    return devices


def _is_local_device(raw: dict) -> bool:
    """True if the device is physically attached to THIS host (USB or on-board),
    rather than a networked USRP discovered over the LAN. UHD gives a
    network-discovered device an `addr` (its IP); a USB/on-board device has none
    (the X410's own device reports mgmt_addr 127.0.0.1 and no addr). So: local iff
    there's no `addr`, or it's loopback."""
    addr = (raw.get("addr") or "").strip()
    return (not addr) or addr.startswith("127.")


def _to_sdr_device(raw: dict) -> SdrDevice:
    return SdrDevice(
        type=raw.get("type", ""), serial=raw.get("serial", ""),
        name=raw.get("name", ""), product=raw.get("product", ""),
    )


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

    # Keep only devices physically attached to THIS host — drop networked USRPs
    # merely discovered on the LAN (e.g. an X4xx seen by every other unit on the
    # same subnet), so the SDR field reflects what's actually connected here.
    raw = _parse_uhd_output(combined)
    local = [_to_sdr_device(d) for d in raw if _is_local_device(d)]
    return SdrStatus(
        detected=len(local) > 0,
        device_count=len(local),
        devices=local,
        raw_output=combined.strip(),
    )


async def get_sdr_status() -> SdrStatus:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _probe_sdr)


def _preimage_sdr(timeout: float) -> str:
    """Open the SDR once so UHD loads the FPGA image — the slow, variable first-open cost that
    otherwise lands inside the first transmit task's flowgraph construction (and has once made a
    signal start after its on-air time). Uses ``uhd_usrp_probe``, which OPENS the device and loads
    the image, NOT ``uhd_find_devices`` (enumerate-only, the /sdr path — it never loads the image).
    Best-effort and never raises. A no-op when ``uhd_usrp_probe`` is not on PATH (a no-radio box, so
    it costs nothing in dev/CI). MUST be called only while no task holds the single TX channel —
    the caller guarantees that (boot, before any task launches). See docs/rf-fault-recovery.md §3.7."""
    exe = shutil.which("uhd_usrp_probe")
    if exe is None:
        return "uhd_usrp_probe not on PATH — pre-image skipped"
    try:
        subprocess.run([exe], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # The image load happens early in device init, so the probe very likely loaded it before
        # timing out on the (slower) full property enumeration — not a failure, just bounded.
        return f"uhd_usrp_probe timed out after {timeout}s (image likely loaded)"
    except OSError as exc:
        return f"SDR pre-image failed: {exc}"
    return "SDR pre-imaged (FPGA image loaded)"


async def pre_image_sdr(timeout: float = 45.0) -> str:
    """Async wrapper: run the blocking device open in the thread pool so it never stalls the loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _preimage_sdr, timeout)


# ── Fault-time resource snapshot (docs/rf-fault-recovery.md §6.3) ──────────────
# Captured when a task is detected dead-but-alive, so the NEXT vmcircbuf is self-diagnosing. Reads
# the Phase-0 launch-env work (the pinned backend, HOME, the UHD file log) BACK. Every field is
# independently guarded and blanks on failure — the whole capture never raises and works with no
# hardware. Dispatched off-thread by the async wrapper so it never stalls the event loop.

def _read_int_file(path: str) -> Optional[int]:
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def _count_maps(pid: int) -> Optional[int]:
    """Number of VMAs = lines in /proc/<pid>/maps — what vm.max_map_count actually caps (cheaper
    and more accurate than psutil.memory_maps, which groups by path). Read in BINARY and count b'\\n'
    so a mapped file with a non-UTF-8 pathname (which appears verbatim in the maps line) can't raise a
    UnicodeDecodeError out of the best-effort snapshot — the sibling helpers guard the same way
    (_read_int_file catches ValueError, _tail_file decodes with errors='replace')."""
    try:
        with open(f"/proc/{pid}/maps", "rb") as fh:
            return sum(chunk.count(b"\n") for chunk in iter(lambda: fh.read(65536), b""))
    except OSError:
        return None


def _tail_file(path, n: int) -> list:
    try:
        p = Path(path)
        if not p.exists():
            return []
        with p.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 8192))     # last ~8 KB is plenty for n lines
            text = fh.read().decode("utf-8", errors="replace")
        return text.splitlines()[-n:]
    except OSError:
        return []


def _read_vmcircbuf_pref(home: str, gr_prefs_path: str = "") -> str:
    """The backend GR ACTUALLY selects: the content of its `vmcircbuf_default_factory` pref file —
    3.10 reads userconf()/prefs/ (userconf = $GR_PREFS_PATH | $HOME/.config/gnuradio | the legacy
    $HOME/.gnuradio), 3.8 reads $HOME/.gnuradio/prefs/. Returns the first non-empty one found, else
    "" (GR then probes and persists the first working factory — sysv_shm on Linux)."""
    from . import config as cfg
    candidates = []
    if gr_prefs_path:
        candidates.append(Path(gr_prefs_path) / "prefs" / cfg.GR_VMCIRCBUF_PREF_KEY)
    if home:
        candidates.append(Path(home) / ".config" / "gnuradio" / "prefs" / cfg.GR_VMCIRCBUF_PREF_KEY)
        candidates.append(Path(home) / ".gnuradio" / "prefs" / cfg.GR_VMCIRCBUF_PREF_KEY)
    for p in candidates:
        try:
            val = p.read_text(errors="replace").strip()
        except OSError:
            continue
        if val:
            return val
    return ""


def _gnuradio_default_factory() -> str:
    """The COMPILED-in vmcircbuf default from `gnuradio-config-info --prefs` ([vmcircbuf]
    default_factory) — what GR uses under GR_DONT_LOAD_PREFS=1 when no GR_CONF_* env pin is set.
    Blank when the tool is absent (no-GR box) or the section isn't present."""
    exe = shutil.which("gnuradio-config-info")
    if exe is None:
        return ""
    try:
        out = subprocess.run([exe, "--prefs"], capture_output=True, text=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    section = None
    for line in (out.stdout or "").splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1].strip().lower()
        elif section == "vmcircbuf" and s.lower().startswith("default_factory"):
            _, _, val = s.partition("=")
            return val.strip()
    return ""


def _ipcs_summary() -> str:
    exe = shutil.which("ipcs")
    if exe is None:
        return ""
    try:
        out = subprocess.run([exe, "-m"], capture_output=True, text=True, timeout=3)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return (out.stdout or "")[:2048].strip()


def capture_fault_snapshot(pid: Optional[int], uhd_log_path=None, task_dir=None,
                           log_path=None) -> FaultSnapshot:
    """Build the §6.3 snapshot, write the full JSON beside the run log, and return it. Best-effort:
    never raises. Capture BEFORE the auto-drop-RF stop so a halted-but-alive flowgraph still has a
    readable /proc/<pid>; a reaped pid (the Layer-1 clean-exit path) blanks only the PID-scoped
    fields while the machine-wide ones still populate."""
    from . import config as cfg
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%SZ")
    notes: list = []
    snap = FaultSnapshot(captured_at=now.isoformat().replace("+00:00", "Z"),
                         log_path=str(log_path) if log_path else "")

    try:
        du = shutil.disk_usage("/dev/shm")
        snap.shm_used_bytes, snap.shm_total_bytes = du.used, du.total
    except OSError:
        notes.append("/dev/shm unavailable")

    snap.map_max = _read_int_file("/proc/sys/vm/max_map_count")

    proc = None
    if pid:
        try:
            proc = psutil.Process(pid)
        except Exception:      # noqa: BLE001 — NoSuchProcess etc.
            notes.append("pid gone (fields from config)")
    if proc is not None:
        snap.map_count = _count_maps(pid)
        try:
            snap.rss_bytes = proc.memory_info().rss
        except Exception:      # noqa: BLE001
            pass
        try:
            snap.nofile_soft, snap.nofile_hard = proc.rlimit(psutil.RLIMIT_NOFILE)
        except Exception:      # noqa: BLE001
            pass
        try:
            env = proc.environ()
            snap.vmcircbuf_backend_env = env.get(cfg.GR_VMCIRCBUF_ENV, "")
            snap.task_home = env.get("HOME", "")
            snap.vmcircbuf_backend_pref = _read_vmcircbuf_pref(
                snap.task_home, env.get(cfg.GR_PREFS_PATH_ENV, ""))
        except Exception:      # noqa: BLE001 — AccessDenied / Zombie
            notes.append("task env unreadable")

    # Fall back to the INTENDED config values (not necessarily what the task ran with) when the
    # live env wasn't readable — and say so, so the diagnostic isn't misread.
    if not snap.vmcircbuf_backend_env:
        snap.vmcircbuf_backend_env = cfg.GR_VMCIRCBUF_FACTORY
        notes.append("backend from config, not live env")
    if not snap.task_home:
        snap.task_home = str(cfg.TASK_HOME)
    if not snap.vmcircbuf_backend_pref:
        # The pref file GR reads (review fix #1) — from the pinned task HOME when the live env was
        # unreadable. Blank = GR probed for itself (sysv_shm first on Linux) — treat as SysV-suspect.
        snap.vmcircbuf_backend_pref = _read_vmcircbuf_pref(snap.task_home)
        if not snap.vmcircbuf_backend_pref:
            notes.append("no vmcircbuf pref file — GR chose its own backend (sysv_shm first on Linux)")

    snap.vmcircbuf_backend_compiled = _gnuradio_default_factory()

    backends = (snap.vmcircbuf_backend_pref + " " + snap.vmcircbuf_backend_compiled).lower()
    if "sysv" in backends or not snap.vmcircbuf_backend_pref:
        snap.ipcs_summary = _ipcs_summary()

    if uhd_log_path:
        snap.uhd_log_tail = _tail_file(uhd_log_path, 40)

    snap.notes = notes

    if task_dir is not None:
        try:
            out = Path(task_dir) / f"snapshot_{ts}.json"
            snap.snapshot_path = str(out)          # record it BEFORE dumping so the file self-references
            out.write_text(json.dumps(snap.model_dump(), indent=2))
        except OSError:
            snap.snapshot_path = ""
    return snap


async def fault_snapshot(pid: Optional[int], uhd_log_path=None, task_dir=None,
                         log_path=None) -> FaultSnapshot:
    """Async wrapper — run the blocking capture in the thread pool (never stalls the event loop)."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, capture_fault_snapshot, pid, uhd_log_path, task_dir,
                                      log_path)