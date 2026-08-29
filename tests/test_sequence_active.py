"""Agent-side 'auto-command both' for sequences/plans (docs/calibration-v2.md): a
sequence step that sets an absolute --power on a calibrated transmit task also drives the
task's linked active components (e.g. a step attenuator) — the SequenceRunner resolves the
active components at fire time and retunes each linked control task to the SDR-first value.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from agent import process_manager as pm
from agent.models import TaskConfig
from agent.process_manager import ProcessManager
from agent.sequence_runner import SequenceRunner, _step_power


# ── the step → power extraction ────────────────────────────────────────────────────

def test_step_power_reads_start_run_args_and_tune_params():
    run = SimpleNamespace(action="run", args=["--power", "-100", "--prn", "3"], params={})
    assert _step_power(run) == -100.0
    cap = SimpleNamespace(action="start", args=["-Power", "-42.5"], params={})
    assert _step_power(cap) == -42.5
    tune = SimpleNamespace(action="tune", args=[], params={"power": -60.0})
    assert _step_power(tune) == -60.0
    none = SimpleNamespace(action="run", args=["--gain", "20"], params={})
    assert _step_power(none) is None
    stop = SimpleNamespace(action="stop", args=[], params={})
    assert _step_power(stop) is None


# ── ProcessManager.active_settings resolves the real chain ──────────────────────────

def _write_cal(tmp_path):
    doc = {
        "schema_version": 1, "unit_type": "broadcaster",
        "chain": {
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 40.0, "gain_step_db": 1.0},
            "operating_plane": "atten_out",
            "planes": {
                "sdr_output": {"type": "measured", "quantity": "power"},
                "atten_out": {"type": "derived", "from": "sdr_output", "delta_db": 0.0,
                              "control": {"task": "atten_set", "param": "attenuation",
                                          "sense": "attenuation", "min_db": 0.0,
                                          "max_db": 95.0, "step_db": 0.25, "engage_pct": 0.0}},
            },
        },
        "signals": {"sig": {"curves": {"sdr_output": {"points": [
            {"gain_db": 0, "power_dbm": -40.0}, {"gain_db": 40, "power_dbm": 0.0}]}}}},
    }
    p = tmp_path / "calibration.json"
    p.write_text(json.dumps(doc))
    (tmp_path / "defaults.yaml").write_text("types: {}\n")
    (tmp_path / "components.yaml").write_text("components: {}\n")
    return p


def _mgr_with_cal(tmp_path, monkeypatch, signal="sig"):
    _write_cal(tmp_path)
    monkeypatch.setattr(pm._agentcfg, "CALIBRATION_DOC", tmp_path / "calibration.json")
    monkeypatch.setattr(pm._agentcfg, "CALIBRATION_DEFAULTS", tmp_path / "defaults.yaml")
    monkeypatch.setattr(pm._agentcfg, "CALIBRATION_COMPONENTS", tmp_path / "components.yaml")
    tasks = {
        "tx": TaskConfig(name="tx", command=["python3", "tx.py"],
                         working_dir=str(tmp_path),
                         env={"SDR_CAL_SIGNAL_ID": signal} if signal else {}),
        "atten_set": TaskConfig(name="atten_set", command=["python3", "atten.py"],
                                working_dir=str(tmp_path), env={}),
    }
    return ProcessManager(tasks, tmp_path, "unit-a")


def test_active_settings_realizes_the_attenuator_value(tmp_path, monkeypatch):
    mgr = _mgr_with_cal(tmp_path, monkeypatch)
    # −100 dBm: SDR pinned at min gain (−40) and the attenuator supplies the other 60 dB.
    s = mgr.active_settings("tx", -100.0)
    assert s == [{"plane": "atten_out", "task": "atten_set", "param": "attenuation",
                  "applied_db": -60.0, "value": 60.0}]
    # −20 dBm is in the SDR's own range → the attenuator stays at rest (0 dB).
    assert mgr.active_settings("tx", -20.0)[0]["value"] == 0.0


def test_active_settings_empty_without_power_or_optin(tmp_path, monkeypatch):
    mgr = _mgr_with_cal(tmp_path, monkeypatch)
    assert mgr.active_settings("tx", None) == []            # relative-gain mode
    assert mgr.active_settings("atten_set", -100.0) == []   # task didn't opt into calibration
    # a signal the unit isn't calibrated for → resolve raises SignalNotCalibrated → []
    mgr2 = _mgr_with_cal(tmp_path, monkeypatch, signal="missing")
    assert mgr2.active_settings("tx", -100.0) == []


# ── the runner fires the component (best-effort) ────────────────────────────────────

class _FakeMgr:
    def __init__(self, settings, fail=False):
        self._settings, self.fail = settings, fail
        self.calls = []

    def active_settings(self, task, power, freq=None):
        return list(self._settings)

    async def set_params(self, name, values, wait=1.0):
        self.calls.append((name, dict(values), wait))
        if self.fail:
            raise RuntimeError("task not running")
        return {"applied": values, "rejected": {}}


def _runner(mgr, tmp_path):
    return SequenceRunner(mgr, "u", tmp_path / "s.json", tmp_path / "r.json", tmp_path)


def test_command_active_retunes_each_linked_task(tmp_path):
    mgr = _FakeMgr([{"plane": "atten_out", "task": "atten_set",
                     "param": "attenuation", "applied_db": -60.0, "value": 60.0}])
    runner = _runner(mgr, tmp_path)
    step = SimpleNamespace(action="run", task_name="tx",
                           args=["--power", "-100"], params={})
    asyncio.run(runner._command_active(step, None))
    assert mgr.calls == [("atten_set", {"attenuation": 60.0}, 0.0)]


def test_command_active_noop_without_power(tmp_path):
    mgr = _FakeMgr([{"task": "atten_set", "param": "attenuation", "value": 1.0}])
    runner = _runner(mgr, tmp_path)
    step = SimpleNamespace(action="run", task_name="tx", args=["--gain", "20"], params={})
    asyncio.run(runner._command_active(step, None))
    assert mgr.calls == []                                  # no --power → nothing commanded


def test_command_active_tolerates_a_component_that_isnt_running(tmp_path):
    # A failing set_params (component not running) is logged, never fatal.
    mgr = _FakeMgr([{"task": "atten_set", "param": "attenuation", "value": 60.0}], fail=True)
    runner = _runner(mgr, tmp_path)
    step = SimpleNamespace(action="run", task_name="tx", args=["--power", "-100"], params={})
    asyncio.run(runner._command_active(step, None))        # must not raise
    assert mgr.calls == [("atten_set", {"attenuation": 60.0}, 0.0)]
