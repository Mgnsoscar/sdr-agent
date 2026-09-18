"""
The fault-time resource snapshot (RF-fault Phase 1, docs/rf-fault-recovery.md §6.3): best-effort,
never raises, reads the Phase-0 launch-env work back, and works with no hardware.
"""
import os

from agent import system as sysmon
from agent.models import FaultSnapshot


def test_snapshot_with_a_live_pid_captures_proc_and_machine_state(tmp_path):
    uhd = tmp_path / "uhd.log"
    uhd.write_text("-- Loading FPGA image...\n-- Setting master clock rate\n-- done\n")
    snap = sysmon.capture_fault_snapshot(
        os.getpid(), uhd_log_path=str(uhd),
        task_dir=str(tmp_path), log_path=str(tmp_path / "current.log"),
    )
    assert isinstance(snap, FaultSnapshot)
    assert snap.map_max is not None and snap.map_max > 0        # /proc/sys/vm/max_map_count
    assert snap.map_count and snap.map_count > 0                # /proc/self/maps line count
    assert snap.rss_bytes and snap.rss_bytes > 0
    assert snap.vmcircbuf_backend_env                           # live env or config fallback
    assert "Loading FPGA image" in "\n".join(snap.uhd_log_tail)  # the §3.7 UHD file sink tail
    assert snap.snapshot_path and os.path.exists(snap.snapshot_path)   # full JSON written beside log
    assert snap.log_path.endswith("current.log")
    assert snap.captured_at


def test_snapshot_with_a_dead_pid_blanks_pid_fields_but_keeps_machine(tmp_path):
    snap = sysmon.capture_fault_snapshot(2147480000, task_dir=str(tmp_path))   # implausible pid
    assert snap.map_count is None                              # pid-scoped read → blank
    assert snap.rss_bytes is None
    assert snap.map_max is not None                            # machine-wide still populates
    assert snap.vmcircbuf_backend_env                          # config fallback
    assert any("config" in n or "pid gone" in n for n in snap.notes)


def test_snapshot_none_pid_never_raises_and_writes_nothing():
    snap = sysmon.capture_fault_snapshot(None)                 # no pid, no task_dir
    assert isinstance(snap, FaultSnapshot)
    assert snap.vmcircbuf_backend_env                          # config fallback
    assert snap.snapshot_path == ""                            # no task_dir → nothing written


def test_count_maps_survives_a_non_utf8_maps_line(monkeypatch):
    # A mapped file whose pathname holds non-UTF-8 bytes appears verbatim in /proc/<pid>/maps. A
    # text-mode read would raise UnicodeDecodeError (a ValueError, not OSError) out of the best-effort
    # snapshot; the binary read must count the lines instead of raising. `open` is unqualified in
    # system.py, so shadowing the module global redirects _count_maps at this crafted content.
    import io
    raw = b"7f0000-7f0100 r-xp /lib/x\xff\xfe.so\n7f0200-7f0300 rw-p /dev/shm/y\n"
    monkeypatch.setattr(sysmon, "open", lambda p, *a, **k: io.BytesIO(raw), raising=False)
    assert sysmon._count_maps(1234) == 2                       # two b"\n", no UnicodeDecodeError

    # And the real path still reads (binary open on this process's own maps).
    monkeypatch.undo()
    assert sysmon._count_maps(os.getpid()) > 0
