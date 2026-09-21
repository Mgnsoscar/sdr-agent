"""Sustained TX underflows are an RF fault (docs/rf-fault-recovery.md §14m, 1.36.2).

GNU Radio's USRP sink logs `usrp_sink :error: In the last 750 ms, N underflows occurred.` once per
window while the host cannot keep the sample stream full; the flowgraph is alive, the task reads
RUNNING, and the radio emits bursts with gaps (the owner's spectrum analyzer "going crazy" on a
deliberately too-heavy L1P configuration). Nothing matched those lines before. Now the watchdog scan
judges each report by its RATE (GR's "last N ms" is the time since its previous report, not a fixed
window — the first field build summed those windows and faulted healthy tasks on ONE report covering
17–33 s with a handful of underflows, 1.36.2): a report at or above UNDERFLOW_FAULT_RATE (200/s) is
heavy and its window counts, a lighter one is ignored (a long light window ends the streak), and once
an unbroken streak's heavy windows cover UNDERFLOW_FAULT_S (4 s) the task is flagged through the ordinary
`_flag_rf_fault` path: alarm, snapshot, auto-drop RF, run coupling, and — the owner's choice — the
standalone auto-restart, whose budget trips loudly when the relaunch underflows again.

Covers:
  * the GR line parser (the exact field format, singular/plural, foreign lines);
  * a heavy streak covering the threshold faults through the scan (health, detail, hook, auto-drop, event);
  * the owner's false positives — one sparse report over 17–33 s — never fault, however many; light
    reports don't count and a long light window resets the streak; the rate knob at 0 restores the
    window-sum reading (so the knob is what separates the two);
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
        assert proc.health_detail.startswith("sustained TX underflows: 6 heavy reports over 4.5 s")
        assert "at ~10,000 underflows/s (limit 200/s)" in proc.health_detail
        assert proc._resource_snapshot is not None                 # the §6.3 snapshot, as for any fault
        assert hook_calls and hook_calls[0][0] == "tx"             # coupled into an owning run
        assert proc.state != ProcessState.RUNNING                  # RF auto-dropped
        got = q.get_nowait()
        assert got["type"] == "task_health" and got["health"] == "rf_fault"
        assert "underflow" in got["detail"]
    asyncio.run(scenario())


SPARSE = ("usrp_sink :error: In the last 17300 ms, 18 underflows occurred.\n"
          "usrp_sink :error: In the last 32800 ms, 66 underflows occurred.\n"
          "usrp_sink :error: In the last 21300 ms, 60 underflows occurred.\n").encode()


def test_a_sparse_report_over_a_long_window_never_faults(tmp_path):
    """The field false positive (1.36.2): GR's window is the time since its previous report, so ONE
    report covering 17–33 s with a handful of underflows is a healthy stream with a hiccup — the owner's
    "18 underflows in 6 seconds is not worth stopping the task for". Never a fault, however many."""
    async def scenario():
        mgr, proc = _mgr(tmp_path)
        proc.log.current.write_bytes(b"banner\n" + SPARSE * 20)      # 70 s of "sparse" per copy, x20
        await mgr._scan_task_health(proc)
        assert proc.health == TaskHealth.OK.value and proc._fault_alarmed is False
        assert proc.state == ProcessState.RUNNING
        assert proc._uf_ms == 0 and proc._uf_reports == 0           # nothing counted at all
    asyncio.run(scenario())


def test_light_reports_do_not_count_and_a_long_light_window_resets(tmp_path):
    async def scenario():
        mgr, proc = _mgr(tmp_path)
        light = LINE.format(n=5).encode()                             # 5 in 750 ms = 6.7/s: light
        # heavy, light, heavy, light, heavy: the light ones are ignored, 3 heavy = 2.25 s
        proc.log.current.write_bytes(_burst(1) + light + _burst(1) + light + _burst(1))
        await mgr._scan_task_health(proc)
        assert proc.health == TaskHealth.OK.value
        assert proc._uf_reports == 3 and proc._uf_ms == 2250
        # a light report whose window spans the gap proves the stream was fine: the streak is over
        with open(proc.log.current, "ab") as fh:
            fh.write(b"usrp_sink :error: In the last 5000 ms, 40 underflows occurred.\n" + _burst(3))
        await mgr._scan_task_health(proc)
        assert proc.health == TaskHealth.OK.value                    # 3 heavy after the reset = 2.25 s
        assert proc._uf_reports == 3
        with open(proc.log.current, "ab") as fh:
            fh.write(_burst(3))                                       # 6 heavy → 4.5 s
        await mgr._scan_task_health(proc)
        assert proc.health == TaskHealth.RF_FAULT.value
    asyncio.run(scenario())


def test_the_rate_knob_is_what_separates_sparse_from_heavy(tmp_path, monkeypatch):
    """With the rate limit at 0 every report is heavy and the sparse report's 17.3 s window alone
    covers the threshold — the 1.36.2 reading; the default limit is what keeps it from faulting."""
    monkeypatch.setattr(pm._agentcfg, "UNDERFLOW_FAULT_RATE", 0.0)
    async def scenario():
        mgr, proc = _mgr(tmp_path)
        proc.log.current.write_bytes(b"usrp_sink :error: In the last 17300 ms, 18 underflows occurred.\n")
        await mgr._scan_task_health(proc)
        assert proc.health == TaskHealth.RF_FAULT.value
        assert "1 heavy reports over 17.3 s" in proc.health_detail
    asyncio.run(scenario())


def test_a_report_exactly_at_the_rate_limit_is_heavy(tmp_path, monkeypatch):
    monkeypatch.setattr(pm._agentcfg, "UNDERFLOW_FAULT_RATE", 200.0)
    async def scenario():
        mgr, proc = _mgr(tmp_path)
        at = LINE.format(n=150).encode()                              # 150 / 0.75 s = 200/s: heavy
        below = LINE.format(n=149).encode()                           # 198.7/s: light
        proc.log.current.write_bytes(at * 5 + below * 5)
        await mgr._scan_task_health(proc)
        assert proc._uf_reports == 5 and proc._uf_ms == 3750
        assert proc.health == TaskHealth.OK.value
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
        assert "6 heavy reports over 4.5 s" in proc.health_detail
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
