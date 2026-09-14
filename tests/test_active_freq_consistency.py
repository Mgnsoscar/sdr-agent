"""The agent realizes a task's active components (an attenuator) at the CARRIER the script folds
its own gain at — the launch command's CAL_FREQ_PARAM scaled to Hz — and re-realizes on a live
retune of it; the run-log export realizes each row at that row's carrier.

Owner report: calibrated power off by "about a cable loss" once the chain carried frequency-
dependent cable tables. Root cause: the agent positioned the attenuator at the calibration's
center_freq_hz while the script set its SDR gain at the carrier; the SDR/attenuator split the
realization picks (closest exact hit) jumps with frequency, so the two belonged to different
splits and the delivered power was off by their difference.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from agent import process_manager as pm
from agent import run_table, tune_log
from agent.calibration import resolve
from agent.models import StartRequest, TaskConfig
from agent.process_manager import ProcessManager, _freq_from_command


# ── the shared frequency helper ─────────────────────────────────────────────────

SPEC = {"calibration_signal": "sig", "calibration_freq_param": "freq", "params": [
    {"dest": "freq", "flags": ["-Center-frequency", "--freq"], "unit": "MHz", "kind": "number",
     "default": 1575.42, "live": True},
    {"dest": "power", "flags": ["-Power", "--power"], "unit": "dBm", "kind": "number", "live": True},
]}


def test_freq_hz_of_scales_by_the_declared_unit():
    assert tune_log.freq_hz_of(SPEC, {"freq": 1200.0}) == pytest.approx(1.2e9)     # MHz → Hz
    assert tune_log.freq_hz_of(SPEC, {"freq": "1227.6"}) == pytest.approx(1.2276e9)
    assert tune_log.freq_hz_of(SPEC, {}) == pytest.approx(1575.42e6)               # schema default
    hz = {**SPEC, "params": [dict(SPEC["params"][0], unit="Hz", default=None)]}
    assert tune_log.freq_hz_of(hz, {"freq": 1575.42e6}) == pytest.approx(1575.42e6)
    assert tune_log.freq_hz_of(hz, {}) is None                                     # no value, no default
    assert tune_log.freq_hz_of({"params": SPEC["params"]}, {"freq": 1.0}) is None  # no freq param
    assert tune_log.freq_hz_of(None, {"freq": 1.0}) is None
    assert tune_log.freq_hz_of(SPEC, {"freq": "abc"}) is None


def test_freq_from_command_reads_the_launch_carrier():
    assert _freq_from_command(["python3", "tx.py", "-Center-frequency", "1200", "--power", "-30"],
                              SPEC) == pytest.approx(1.2e9)
    assert _freq_from_command(["python3", "tx.py", "--freq", "1227.6"], SPEC) == pytest.approx(1.2276e9)
    assert _freq_from_command(["python3", "tx.py", "--power", "-30"], SPEC) == pytest.approx(1575.42e6)
    assert _freq_from_command(["python3", "tx.py", "--freq", "1200"], None) is None
    assert _freq_from_command(["python3", "tx.py"], {"params": SPEC["params"]}) is None


# ── a frequency-dependent unit with an attenuator, driven through the manager ──────

TX_SCRIPT = '''
from paramkit import Script

CAL_SIGNAL_ID = "sig"
CAL_FREQ_PARAM = "freq"


def build_script():
    return (
        Script("test tx")
        .number("-Center-frequency", "--freq", unit="MHz", min=70.0, max=6000.0,
                default=1575.42, required=True, live=True)
        .number("-Power", "--power", unit="dBm", min=-120.0, max=10.0, required=True, live=True)
        .choice("-RF", "--rf", options=["on", "off"], default="on", live=True, is_rf=True)
    )
'''

ATTEN_SCRIPT = (
    "import argparse\n"
    "p = argparse.ArgumentParser()\n"
    "p.add_argument('--attenuation', type=float, default=0.0)\n"
    "p.parse_args()\n")

BIAS = [[1.0e9, -3.0], [1.5e9, 0.0], [2.0e9, 3.0]]            # the SDR gets hotter with frequency


def _doc():
    return {
        "schema_version": 1, "unit_type": "broadcaster",
        "chain": {
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 40.0, "gain_step_db": 1.0},
            "operating_plane": "atten_out",
            "planes": {
                "sdr_output": {"type": "measured", "quantity": "power"},
                "atten_out": {"type": "derived", "from": "sdr_output", "delta_db": 0.0,
                              "control": {"task": "atten_set", "param": "attenuation",
                                          "sense": "attenuation", "min_db": 0.0, "max_db": 95.0,
                                          "step_db": 0.25, "engage_pct": 0.0}},
            },
        },
        "source_bias": {"power_by_freq": BIAS},
        "signals": {"sig": {"center_freq_hz": 1.5e9, "curves": {"sdr_output": {"points": [
            {"gain_db": 0, "power_dbm": -40.0}, {"gain_db": 40, "power_dbm": 0.0}]}}}},
    }


def _mgr(tmp_path, monkeypatch, env_extra=None):
    (tmp_path / "calibration.json").write_text(json.dumps(_doc()))
    (tmp_path / "defaults.yaml").write_text("types: {}\n")
    (tmp_path / "components.yaml").write_text("components: {}\n")
    (tmp_path / "tx.py").write_text(TX_SCRIPT)
    (tmp_path / "atten.py").write_text(ATTEN_SCRIPT)
    monkeypatch.setattr(pm._agentcfg, "CALIBRATION_DOC", tmp_path / "calibration.json")
    monkeypatch.setattr(pm._agentcfg, "CALIBRATION_DEFAULTS", tmp_path / "defaults.yaml")
    monkeypatch.setattr(pm._agentcfg, "CALIBRATION_COMPONENTS", tmp_path / "components.yaml")
    tasks = {
        "tx": TaskConfig(name="tx", command=["python3", "tx.py"], working_dir=str(tmp_path),
                         env={"SDR_CAL_SIGNAL_ID": "sig", **(env_extra or {})}),
        "atten_set": TaskConfig(name="atten_set", command=["python3", str(tmp_path / "atten.py")],
                                working_dir=str(tmp_path), env={}),
    }
    return ProcessManager(tasks, tmp_path, "unit-a")


def _capture(mgr, monkeypatch):
    fired, started = [], []

    async def fake_oneshot(name, args, timeout=pm._ACTIVE_SET_TIMEOUT_S):
        fired.append((name, list(args)))
        return 0

    proc = mgr._get("tx")

    async def noop_start(request=None):
        started.append(request)

    async def noop_set(values, wait=1.0):
        return {"applied": values, "rejected": {}}

    monkeypatch.setattr(mgr, "_launch_oneshot_wait", fake_oneshot)
    monkeypatch.setattr(proc, "start", noop_start)
    monkeypatch.setattr(proc, "set_params", noop_set)
    monkeypatch.setattr(proc, "status", lambda: None)
    return fired, started


def _atten_at(freq_hz, power=-30.0):
    """The attenuation the realization picks for `power` at `freq_hz` (what the script assumes)."""
    r = resolve(_doc(), None, "sig").realize(power, freq_hz)
    return r["settings"][0]["value"]


def test_the_split_genuinely_moves_with_frequency():
    # The premise of the fix: the same −30 dBm is realized with a DIFFERENT attenuation at the
    # carrier than at center_freq_hz, so realizing at the wrong one is a real power error.
    assert _atten_at(1.5e9) != _atten_at(1575.42e6)
    assert _atten_at(1.5e9) != _atten_at(1.2e9)


def test_launch_positions_the_attenuator_at_the_commands_carrier(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    fired, _ = _capture(mgr, monkeypatch)
    asyncio.run(mgr.start("tx", StartRequest(args=["-Center-frequency", "1575.42", "--power", "-30"],
                                             replace_args=True), source="sequence"))
    assert fired == [("atten_set", ["--attenuation", pm._fmt_num(_atten_at(1575.42e6))])]
    assert fired[0][1][1] != pm._fmt_num(_atten_at(1.5e9))       # not the center_freq_hz split


def test_a_carrier_retune_repositions_the_attenuator(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    fired, _ = _capture(mgr, monkeypatch)
    asyncio.run(mgr.start("tx", StartRequest(args=["--freq", "1575.42", "--power", "-30"],
                                             replace_args=True), source="sequence"))
    asyncio.run(mgr.set_params("tx", {"freq": 1200.0}))            # MHz, like the script's param
    assert fired[-1] == ("atten_set", ["--attenuation", pm._fmt_num(_atten_at(1.2e9))])
    # …and a later power tune keeps realizing at the retuned carrier, not the launch one.
    asyncio.run(mgr.set_params("tx", {"power": -35.0}))
    assert fired[-1] == ("atten_set", ["--attenuation", pm._fmt_num(_atten_at(1.2e9, -35.0))])
    # An RF-off tune mutes (attenuator to max); RF-on re-realizes at the carrier in effect.
    asyncio.run(mgr.set_params("tx", {"rf": "off"}))
    assert fired[-1] == ("atten_set", ["--attenuation", "95"])
    asyncio.run(mgr.set_params("tx", {"rf": "on"}))
    assert fired[-1] == ("atten_set", ["--attenuation", pm._fmt_num(_atten_at(1.2e9, -35.0))])


def test_launch_env_carries_the_carrier_for_the_injected_artifact(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    _fired, started = _capture(mgr, monkeypatch)
    asyncio.run(mgr.start("tx", StartRequest(args=["--freq", "1227.6", "--power", "-30"],
                                             replace_args=True), source="sequence"))
    assert started[-1].env_overrides[pm._agentcfg.CAL_FREQ_HZ_ENV] == "1227600000.000000"
    # The launch args themselves are untouched.
    assert started[-1].args == ["--freq", "1227.6", "--power", "-30"] and started[-1].replace_args
    # An explicit value in the task config wins (the derivation never overrides it).
    mgr2 = _mgr(tmp_path, monkeypatch, env_extra={pm._agentcfg.CAL_FREQ_HZ_ENV: "1000000000"})
    _f2, started2 = _capture(mgr2, monkeypatch)
    asyncio.run(mgr2.start("tx", StartRequest(args=["--freq", "1227.6", "--power", "-30"],
                                              replace_args=True), source="sequence"))
    assert pm._agentcfg.CAL_FREQ_HZ_ENV not in (started2[-1].env_overrides or {})


def test_run_table_realizes_each_row_at_its_own_carrier():
    seen = []

    def realize(power, freq=None, rf_on=True):
        seen.append(freq)
        return {"sdr_gain_db": 1.0, "atten_db": 2.0}

    steps = [
        SimpleNamespace(task_name="tx", action="start", fired_actual="2026-09-14T10:00:00+00:00",
                        args=["--freq", "1200", "--power", "-30"], params=None),
        SimpleNamespace(task_name="tx", action="tune", fired_actual="2026-09-14T10:00:05+00:00",
                        args=[], params={"freq": 1575.42}),
        SimpleNamespace(task_name="tx", action="tune", fired_actual="2026-09-14T10:00:09+00:00",
                        args=[], params={"power": -35.0}),
    ]
    run_table.build_task_table("tx", steps, SPEC, None, realize=realize)
    assert seen == [pytest.approx(1.2e9), pytest.approx(1575.42e6), pytest.approx(1575.42e6)]
