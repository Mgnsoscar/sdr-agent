"""Sustained TX underflows are an RF fault (docs/rf-fault-recovery.md §14m, 1.36.2).

GNU Radio's USRP sink logs `usrp_sink :error: In the last 750 ms, N underflows occurred.` once per
window while the host cannot keep the sample stream full; the flowgraph is alive, the task reads
RUNNING, and the radio emits bursts with gaps (the owner's spectrum analyzer "going crazy" on a
deliberately too-heavy L1P configuration). Nothing matched those lines before. Now the watchdog scan
sums the windows of an unbroken streak and flags the task through the ordinary `_flag_rf_fault` path
once they cover UNDERFLOW_FAULT_S (4 s): alarm, snapshot, auto-drop RF, run coupling, and — the owner's
choice — the standalone auto-restart, whose budget trips loudly when the relaunch underflows again.

Covers:
  * the GR line parser (the exact field format, singular/plural, foreign lines);
  * a streak covering the threshold faults through the scan (health, detail, hook, auto-drop, event);
  * a short burst does not; a streak accumulates across scans; a gap resets it; 0 disables it;
  * start() resets the streak so a relaunch is judged afresh;
  * LIVE: the real health loop over a script printing the GR lines faults + stops it, and an
    auto-restart-on-fault task is relaunched, faults again and trips its budget.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, REPO_ROOT)

from agent import process_manager as pm                       # noqa: E402
from agent.models import ProcessState, StartRequest, TaskConfig, TaskHealth   # noqa: E402

LINE = "usrp_sink :error: In the last 750 ms, {n} underflows occurred.\n"


def _burst(k, n=7500):
    return "".join(LINE.format(n=n) for _ in range(k)).encode()


def _task(tmp_path, **kw):
    return TaskConfig(name="tx", command=["python3", str(tmp_path / "s.py")],
                      working_dir=str(tmp_path), **kw)


def _mgr(tmp_path, **kw):
    mgr = pm.ProcessManager({"tx": _task(tmp_path, **kw)}, tmp_path, unit_id="u")
    proc = mgr._procs["tx"]
    proc.state = ProcessState.RUNNING
    proc.pid = os.getpid()
    return mgr, proc


# ── the parser ─────────────────────────────────────────────────────────────────

def test_parser_reads_the_gr_report_line():
    text = ("── GPS L1 P TX ──\n  analog TX BW   : 56.000 MHz\n"
            "usrp_sink :error: In the last 750 ms, 6927 underflows occurred.\n"
            "usrp_sink :error: In the last 750 ms, 7187 underflows occurred.\n"
            "usrp_sink :error: In the last 1500 ms, 1 underflow occurred.\n"
            "GPS L1 P stopped.\n")
    assert pm._underflow_reports(text) == [(750, 6927), (750, 7187), (1500, 1)]
    assert pm._underflow_reports("tuning to 1575.42 MHz\nRF on\nHEALTH state=transmitting\n") == []


# ── through the scan ───────────────────────────────────────────────────────────

def test_a_sustained_streak_faults_and_drops_rf(tmp_path):
    async def scenario():
        mgr, proc = _mgr(tmp_path)
        hook_calls = []
        async def hook(name, detail):
            hook_calls.append((name, detail))
        mgr.set_fault_hook(hook)
        q = mgr.dispatcher.subscribe()
        proc.log.current.write_bytes(b"banner\n" + _burst(6))      # 6 x 750 ms = 4.5 s >= 4 s
        await mgr._scan_task_health(proc)
        await asyncio.sleep(0.05)
        assert proc.health == TaskHealth.RF_FAULT.value
        assert proc.health_detail.startswith("sustained TX underflows: 6 reports over 4.5 s")
        assert "underflows/s" in proc.health_detail
        assert proc._resource_snapshot is not None                 # the §6.3 snapshot, as for any fault
        assert hook_calls and hook_calls[0][0] == "tx"             # coupled into an owning run
        assert proc.state != ProcessState.RUNNING                  # RF auto-dropped
        got = q.get_nowait()
        assert got["type"] == "task_health" and got["health"] == "rf_fault"
        assert "underflow" in got["detail"]
    asyncio.run(scenario())


def test_a_short_burst_is_not_a_fault(tmp_path):
    async def scenario():
        mgr, proc = _mgr(tmp_path)
        proc.log.current.write_bytes(b"banner\n" + _burst(3))      # 2.25 s < 4 s
        await mgr._scan_task_health(proc)
        assert proc.health == TaskHealth.OK.value and proc._fault_alarmed is False
        assert proc._uf_ms == 2250 and proc._uf_reports == 3
        with open(proc.log.current, "ab") as fh:
            fh.write(b"tuning to 1575.42 MHz\nRF on\n")             # clean text: nothing changes
        await mgr._scan_task_health(proc)
        assert proc.health == TaskHealth.OK.value
        assert proc.state == ProcessState.RUNNING
    asyncio.run(scenario())


def test_the_streak_accumulates_across_scans(tmp_path):
    async def scenario():
        mgr, proc = _mgr(tmp_path)
        proc.log.current.write_bytes(_burst(3))
        await mgr._scan_task_health(proc)
        assert proc.health == TaskHealth.OK.value
        with open(proc.log.current, "ab") as fh:
            fh.write(_burst(3))                                     # 3 + 3 → 4.5 s covered
        await mgr._scan_task_health(proc)
        assert proc.health == TaskHealth.RF_FAULT.value
        assert "6 reports over 4.5 s" in proc.health_detail
    asyncio.run(scenario())


def test_a_gap_resets_the_streak(tmp_path):
    async def scenario():
        mgr, proc = _mgr(tmp_path)
        proc.log.current.write_bytes(_burst(3))
        await mgr._scan_task_health(proc)
        proc._uf_last -= 10.0                                       # 10 s since the last report
        with open(proc.log.current, "ab") as fh:
            fh.write(_burst(3))
        await mgr._scan_task_health(proc)
        assert proc.health == TaskHealth.OK.value                  # a NEW streak of 2.25 s only
        assert proc._uf_reports == 3
        with open(proc.log.current, "ab") as fh:
            fh.write(_burst(3))                                     # …which then does add up
        await mgr._scan_task_health(proc)
        assert proc.health == TaskHealth.RF_FAULT.value
    asyncio.run(scenario())


def test_knob_zero_disables_the_detector(tmp_path, monkeypatch):
    monkeypatch.setattr(pm._agentcfg, "UNDERFLOW_FAULT_S", 0.0)
    async def scenario():
        mgr, proc = _mgr(tmp_path)
        proc.log.current.write_bytes(_burst(40))                    # 30 s of underflow
        await mgr._scan_task_health(proc)
        assert proc.health == TaskHealth.OK.value
        assert proc.state == ProcessState.RUNNING
    asyncio.run(scenario())


def test_a_signature_hit_still_wins_and_a_clean_log_is_untouched(tmp_path):
    async def scenario():
        mgr, proc = _mgr(tmp_path)
        proc.log.current.write_bytes(_burst(1) + b"HEALTH state=faulted reason=\"halt\"\n")
        await mgr._scan_task_health(proc)
        await asyncio.sleep(0.02)
        assert proc.health == TaskHealth.RF_FAULT.value
        assert proc.health_detail.startswith("log signature:")
    asyncio.run(scenario())


def test_start_resets_the_streak(tmp_path):
    """A relaunch is judged afresh — the faulted launch's streak never counts against it."""
    (tmp_path / "s.py").write_text("import time\ntime.sleep(30)\n")
    async def scenario():
        mgr, proc = _mgr(tmp_path)
        proc.state = ProcessState.STOPPED
        proc._uf_last, proc._uf_ms, proc._uf_reports, proc._uf_count = time.monotonic(), 3000, 4, 100
        await mgr.start("tx")
        try:
            assert proc._uf_last is None and proc._uf_ms == 0
            assert proc._uf_reports == 0 and proc._uf_count == 0
        finally:
            await mgr.stop("tx")
    asyncio.run(scenario())


# ── LIVE: the real watchdog loop over a real process ──────────────────────────

UNDERFLOW_SCRIPT = '''\
import sys, time
print("── FAKE TX ──", flush=True)
for i in range(400):
    print("usrp_sink :error: In the last 750 ms, %d underflows occurred." % (7000 + i), flush=True)
    time.sleep(0.15)
'''


async def _wait_for(pred, timeout=8.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return pred()


def test_live_watchdog_faults_an_underflowing_task_and_stops_it(tmp_path, monkeypatch):
    monkeypatch.setattr(pm._agentcfg, "HEALTH_WATCH_ENABLED", True)
    monkeypatch.setattr(pm._agentcfg, "HEALTH_POLL_S", 0.3)
    (tmp_path / "s.py").write_text(UNDERFLOW_SCRIPT)
    async def scenario():
        mgr = pm.ProcessManager({"tx": _task(tmp_path, restart_on_crash=False)}, tmp_path, unit_id="u")
        proc = mgr._procs["tx"]
        q = mgr.dispatcher.subscribe()
        await mgr.startup()
        try:
            await mgr.start("tx")
            assert proc.state == ProcessState.RUNNING
            assert await _wait_for(lambda: proc.health == TaskHealth.RF_FAULT.value)
            assert "sustained TX underflows" in proc.health_detail
            assert await _wait_for(lambda: proc.state != ProcessState.RUNNING)    # auto-dropped
            assert await _wait_for(lambda: proc._proc is not None and proc._proc.returncode is not None)
            events = []
            while not q.empty():
                events.append(q.get_nowait())
            assert any(e.get("type") == "task_health" and e.get("health") == "rf_fault" for e in events)
        finally:
            await mgr.shutdown()
    asyncio.run(scenario())


def test_live_auto_restart_relaunches_then_trips_its_budget(tmp_path, monkeypatch):
    """The owner's choice: an underflow fault auto-restarts like any other; a configuration that can't
    be sustained faults again on the relaunch, and the budget trips loudly instead of looping."""
    monkeypatch.setattr(pm._agentcfg, "HEALTH_WATCH_ENABLED", True)
    monkeypatch.setattr(pm._agentcfg, "HEALTH_POLL_S", 0.3)
    monkeypatch.setattr(pm._agentcfg, "AUTO_RESTART_ENABLED", True)
    (tmp_path / "s.py").write_text(UNDERFLOW_SCRIPT)
    async def scenario():
        cfgt = _task(tmp_path, restart_on_crash=False, auto_restart_on_fault=True,
                     max_fault_restarts=1, restart_window_s=100.0, restart_delay_s=0.0)
        mgr = pm.ProcessManager({"tx": cfgt}, tmp_path, unit_id="u")
        proc = mgr._procs["tx"]
        starts = []
        real_start = proc.start
        async def counting_start(request=None):
            starts.append(request)
            await real_start(request)
        proc.start = counting_start
        await mgr.startup()
        try:
            await mgr.start("tx", StartRequest(args=["--power", "-64"], replace_args=True))
            assert await _wait_for(lambda: len(starts) >= 2, timeout=15.0)          # relaunched once
            assert starts[1] is not None and starts[1].args == ["--power", "-64"]  # the same launch
            assert await _wait_for(lambda: proc.fault_restart_giving_up, timeout=15.0)
            assert len(starts) == 2                                                 # then it stopped
            assert await _wait_for(lambda: proc.state != ProcessState.RUNNING)
        finally:
            await mgr.shutdown()
    asyncio.run(scenario())
