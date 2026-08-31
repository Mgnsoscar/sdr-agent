"""Auto-command both (docs/calibration-v2.md §12.3): launching or tuning a calibrated
transmit task with an absolute --power also positions each linked active component (e.g. a
step attenuator) — fired by the agent as a ONE-SHOT (no long-running control task needed),
before the transmit emits. Centralized in ProcessManager so Run, quick-play, sequences,
ramps and the API all get it uniformly."""
import asyncio
import json

import pytest

from agent import process_manager as pm
from agent.models import StartRequest, TaskConfig
from agent.process_manager import (
    ProcessManager, _power_from_command, _fmt_num,
)


# ── pure helpers ────────────────────────────────────────────────────────────────────

def test_power_from_command_reads_either_flag():
    assert _power_from_command(["python3", "tx.py", "--power", "-100"]) == -100.0
    assert _power_from_command(["python3", "tx.py", "-Power", "-42.5"]) == -42.5
    assert _power_from_command(["python3", "tx.py", "--gain", "20"]) is None


def test_fmt_num_drops_trailing_zero():
    assert _fmt_num(60.0) == "60" and _fmt_num(0.25) == "0.25"


# ── a unit with an SDR + a step-attenuator active component ──────────────────────────

ATTEN_SCRIPT = (
    "import argparse\n"
    "p = argparse.ArgumentParser()\n"
    "p.add_argument('--attenuation', type=float, default=0.0)\n"
    "p.add_argument('--port', default='/dev/ttyACM0')\n"
    "p.parse_args()\n")


def _write_cal(tmp_path, consts=None):
    control = {"task": "atten_set", "param": "attenuation", "sense": "attenuation",
               "min_db": 0.0, "max_db": 95.0, "step_db": 0.25, "engage_pct": 0.0}
    if consts is not None:
        control["consts"] = consts
    doc = {
        "schema_version": 1, "unit_type": "broadcaster",
        "chain": {
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 40.0, "gain_step_db": 1.0},
            "operating_plane": "atten_out",
            "planes": {
                "sdr_output": {"type": "measured", "quantity": "power"},
                "atten_out": {"type": "derived", "from": "sdr_output", "delta_db": 0.0,
                              "control": control},
            },
        },
        "signals": {"sig": {"curves": {"sdr_output": {"points": [
            {"gain_db": 0, "power_dbm": -40.0}, {"gain_db": 40, "power_dbm": 0.0}]}}}},
    }
    (tmp_path / "calibration.json").write_text(json.dumps(doc))
    (tmp_path / "defaults.yaml").write_text("types: {}\n")
    (tmp_path / "components.yaml").write_text("components: {}\n")
    (tmp_path / "atten.py").write_text(ATTEN_SCRIPT)


def _mgr(tmp_path, monkeypatch, signal="sig", consts=None):
    _write_cal(tmp_path, consts=consts)
    monkeypatch.setattr(pm._agentcfg, "CALIBRATION_DOC", tmp_path / "calibration.json")
    monkeypatch.setattr(pm._agentcfg, "CALIBRATION_DEFAULTS", tmp_path / "defaults.yaml")
    monkeypatch.setattr(pm._agentcfg, "CALIBRATION_COMPONENTS", tmp_path / "components.yaml")
    tasks = {
        "tx": TaskConfig(name="tx", command=["python3", "tx.py"], working_dir=str(tmp_path),
                         env={"SDR_CAL_SIGNAL_ID": signal} if signal else {}),
        "atten_set": TaskConfig(name="atten_set",
                                command=["python3", str(tmp_path / "atten.py")],
                                working_dir=str(tmp_path), env={}),
    }
    return ProcessManager(tasks, tmp_path, "unit-a")


# ── active_settings (resolve → SDR-first realization) ────────────────────────────────

def test_active_settings_realizes_the_attenuator_value(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    s = mgr.active_settings("tx", -100.0)
    assert s == [{"plane": "atten_out", "task": "atten_set", "param": "attenuation",
                  "applied_db": -60.0, "value": 60.0, "consts": {}}]
    assert mgr.active_settings("tx", -20.0)[0]["value"] == 0.0     # SDR carries it, atten at rest
    assert mgr.active_settings("tx", None) == []                   # relative-gain mode
    assert mgr.active_settings("atten_set", -100.0) == []          # not a calibrated transmit task


# ── flag resolution from the control task's argspec ──────────────────────────────────

def test_active_flag_resolves_from_the_scripts_argspec(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    assert mgr._active_flag("atten_set", "attenuation") == "--attenuation"
    assert mgr._active_flag("atten_set", "nonesuch") == "--nonesuch"   # fallback


# ── constant params (e.g. a serial port) ride along on every set ─────────────────────

def test_active_settings_carry_the_constant_params(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch, consts={"port": "/dev/ttyACM0"})
    s = mgr.active_settings("tx", -100.0)
    assert s[0]["consts"] == {"port": "/dev/ttyACM0"}


def test_oneshot_passes_constant_params_with_the_driver(tmp_path, monkeypatch):
    # The port isn't the driving param, but the attenuator script needs it every time — the
    # one-shot must carry --port alongside the computed --attenuation.
    mgr = _mgr(tmp_path, monkeypatch, consts={"port": "/dev/ttyACM0"})
    fired = _capture_oneshots(mgr, monkeypatch)
    _stub_transmit(mgr, monkeypatch)
    asyncio.run(mgr.start("tx", StartRequest(args=["--power", "-100"], replace_args=True),
                          source="sequence"))
    assert fired == [("atten_set",
                      ["--attenuation", "60", "--port", "/dev/ttyACM0"])]


def test_control_const_cannot_shadow_the_driving_param():
    # A const that duplicates the driving param is a config error — reject it at parse.
    from agent import calibration as _calib
    good = {"task": "atten_set", "param": "attenuation", "sense": "attenuation",
            "min_db": 0.0, "max_db": 95.0, "step_db": 0.25, "consts": {"port": "/dev/ttyACM0"}}
    ctrl = _calib._parse_control(good, "atten_out")
    assert ctrl.consts == {"port": "/dev/ttyACM0"}
    bad = dict(good, consts={"attenuation": "10"})
    with pytest.raises(_calib.CalibrationError):
        _calib._parse_control(bad, "atten_out")


# ── the one-shot is fired before the transmit, on every launch/tune path ─────────────

def _capture_oneshots(mgr, monkeypatch):
    fired = []

    async def fake(name, args, timeout=pm._ACTIVE_SET_TIMEOUT_S):
        fired.append((name, list(args)))
        return 0

    monkeypatch.setattr(mgr, "_launch_oneshot_wait", fake)
    return fired


def _stub_transmit(mgr, monkeypatch):
    """Stub the real transmit launch/tune so the test drives only the coupling."""
    proc = mgr._get("tx")

    async def noop_start(request=None):
        return None

    async def noop_set(values, wait=1.0):
        return {"applied": values, "rejected": {}}

    monkeypatch.setattr(proc, "start", noop_start)
    monkeypatch.setattr(proc, "set_params", noop_set)
    monkeypatch.setattr(proc, "status", lambda: None)


def test_start_fires_the_attenuator_oneshot_first(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    fired = _capture_oneshots(mgr, monkeypatch)
    _stub_transmit(mgr, monkeypatch)
    asyncio.run(mgr.start("tx", StartRequest(args=["--power", "-100"], replace_args=True),
                          source="sequence"))
    assert fired == [("atten_set", ["--attenuation", "60"])]


def test_run_oneshot_fires_the_attenuator(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    fired = _capture_oneshots(mgr, monkeypatch)

    class _FakeProc:
        pid = 1234
        returncode = 0
        async def wait(self):
            return 0

    async def fake_exec(*a, **k):                        # don't launch a real transmit process
        return _FakeProc()

    monkeypatch.setattr(pm.asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(mgr.run_oneshot("tx", ["--power", "-100"]))
    assert ("atten_set", ["--attenuation", "60"]) in fired


def test_set_params_fires_the_attenuator(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    fired = _capture_oneshots(mgr, monkeypatch)
    _stub_transmit(mgr, monkeypatch)
    asyncio.run(mgr.set_params("tx", {"power": -60.0}))
    assert fired == [("atten_set", ["--attenuation", "20"])]      # −60 → SDR floor −40 + 20 dB atten


def test_relative_gain_launch_fires_nothing(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    fired = _capture_oneshots(mgr, monkeypatch)
    _stub_transmit(mgr, monkeypatch)
    asyncio.run(mgr.start("tx", StartRequest(args=["--gain", "20"], replace_args=True),
                          source="sequence"))
    assert fired == []                                            # no absolute power → nothing
