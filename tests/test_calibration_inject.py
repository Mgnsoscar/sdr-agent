"""The agent-side calibration injection: process_manager._inject_calibration wires a
task's opt-in (SDR_CAL_SIGNAL_ID) to a resolved artifact at SDR_CALIBRATION_FILE,
with the documented fail-safe behaviour."""
import json

import pytest

from agent import config as cfg
from agent import calibration as calib
from agent import process_manager as pm


SDR_POINTS = [(40, -36.0), (50, -26.0), (60, -16.0), (70, -6.0), (74, -2.5)]


def _pts(pairs):
    return [{"gain_db": g, "power_dbm": p} for g, p in pairs]


def _valid_doc(unit_type="broadcaster"):
    return {
        "schema_version": 1,
        "unit_id": "u1",
        "unit_type": unit_type,
        "chain": {
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 89.75},
            "operating_plane": "sdr_output",
            "limits": [{"plane": "sdr_output", "max_dbm": -2.5}],
            "planes": {"sdr_output": {"type": "measured", "quantity": "total in-band power"}},
        },
        "signals": {"gps_l1_mcode": {"amplitude": 0.8, "curves": {
            "sdr_output": {"points": _pts(SDR_POINTS)},
        }}},
    }


def _wire(tmp_path, monkeypatch, doc=None, unit_type="broadcaster"):
    doc_path = tmp_path / "calibration.json"
    if doc is not None:
        doc_path.write_text(json.dumps(doc))
    monkeypatch.setattr(cfg, "CALIBRATION_DOC", doc_path)
    monkeypatch.setattr(cfg, "CALIBRATION_DEFAULTS", tmp_path / "calibration_defaults.yaml")
    monkeypatch.setattr(cfg, "CAL_RUN_DIR", tmp_path / "run" / "cal")
    monkeypatch.setattr(cfg, "UNIT_TYPE", unit_type)


def test_opt_in_injects_resolved_artifact(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, _valid_doc())
    env = {cfg.CAL_SIGNAL_ID_ENV: "gps_l1_mcode"}
    pm._inject_calibration(env, "mcode task")

    assert cfg.CALIBRATION_FILE_ENV in env
    art = json.loads((tmp_path / "run" / "cal" / "mcode_task.json").read_text())
    assert art["signal_id"] == "gps_l1_mcode"
    assert art["operating_plane"] == "sdr_output"
    assert art["max_gain_db"] == pytest.approx(74.0)
    assert art["amplitude"] == 0.8
    assert art["curve"][0] == [40.0, -36.0] and art["curve"][-1] == [74.0, -2.5]


def test_no_opt_in_is_noop(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, _valid_doc())
    env = {}
    pm._inject_calibration(env, "t")
    assert cfg.CALIBRATION_FILE_ENV not in env


def test_no_calibration_doc_is_noop(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, doc=None)               # file absent
    env = {cfg.CAL_SIGNAL_ID_ENV: "gps_l1_mcode"}
    pm._inject_calibration(env, "t")
    assert cfg.CALIBRATION_FILE_ENV not in env           # script falls back to baked


def test_missing_signal_is_soft_fallback(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, _valid_doc())
    env = {cfg.CAL_SIGNAL_ID_ENV: "unknown_signal"}
    pm._inject_calibration(env, "t")                      # logs, does not raise
    assert cfg.CALIBRATION_FILE_ENV not in env


def test_broken_doc_raises_hard(tmp_path, monkeypatch):
    doc = _valid_doc()
    # power not strictly increasing with gain → not invertible → hard error
    doc["signals"]["gps_l1_mcode"]["curves"]["sdr_output"]["points"] = _pts(
        [(40, -36.0), (50, -36.0)])
    _wire(tmp_path, monkeypatch, doc)
    env = {cfg.CAL_SIGNAL_ID_ENV: "gps_l1_mcode"}
    with pytest.raises(calib.CalibrationError):
        pm._inject_calibration(env, "t")


def test_agent_unit_type_selects_defaults(tmp_path, monkeypatch):
    # doc omits unit_type; the agent's UNIT_TYPE supplies it, and the type defaults
    # provide the amplitude the signal doesn't set.
    doc = _valid_doc(unit_type=None)
    doc.pop("unit_type")
    doc["signals"]["gps_l1_mcode"].pop("amplitude")
    _wire(tmp_path, monkeypatch, doc, unit_type="broadcaster")
    (tmp_path / "calibration_defaults.yaml").write_text(
        "schema_version: 1\n"
        "types:\n"
        "  broadcaster:\n"
        "    defaults: { amplitude: 0.8 }\n"
    )
    env = {cfg.CAL_SIGNAL_ID_ENV: "gps_l1_mcode"}
    pm._inject_calibration(env, "t")
    art = json.loads((tmp_path / "run" / "cal" / "t.json").read_text())
    assert art["unit_type"] == "broadcaster"
    assert art["amplitude"] == 0.8
