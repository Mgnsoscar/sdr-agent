"""Unit tests for the per-unit power calibration resolver (agent/calibration.py).

Covers the happy path (interpolated curve + derived-plane walk-back), the safety
ceiling (limits inverted against the signal's own curve, tightest wins), the layered
type→unit merge, and every fail-safe refusal in docs/calibration.md §8.
"""
import json

import pytest

from agent import calibration as cal
from agent.calibration import (
    CalibrationError,
    SignalNotCalibrated,
    resolve,
    resolve_from_files,
)


# ── Fixtures: a realistic chain with a deliberate kink near the top ──────────────
#
# sdr_output (amp disconnected): slope-1 from 40→70 dB, then compresses 70→74.
#   (40,-36) (50,-26) (60,-16) (70,-6) (74,-2.5)
# amplifier_output (amp connected): monotonic, compresses near the top.
#   (40,-6) (50,4) (60,14) (70,22) (74,24)
# cable_output   = amplifier_output − 1.8
# antenna_eirp   = cable_output + 6.0   →  amplifier_output + 4.2

SDR_POINTS = [(40, -36.0), (50, -26.0), (60, -16.0), (70, -6.0), (74, -2.5)]
AMP_POINTS = [(40, -6.0), (50, 4.0), (60, 14.0), (70, 22.0), (74, 24.0)]
NET_ANTENNA_OFFSET = -1.8 + 6.0                      # amplifier_output → antenna_eirp


def _pts(pairs):
    return [{"gain_db": g, "power_dbm": p} for g, p in pairs]


def _chain(operating="antenna_eirp", limits=None, gain_limits=None):
    return {
        "gain_limits": gain_limits if gain_limits is not None
                       else {"min_gain_db": 0.0, "max_gain_db": 89.75},
        "operating_plane": operating,
        "limits": limits if limits is not None
                  else [{"plane": "sdr_output", "max_dbm": -2.5, "reason": "amp P1dB"}],
        "planes": {
            "sdr_output":       {"type": "measured", "quantity": "total in-band power"},
            "amplifier_output": {"type": "measured", "quantity": "main-lobe power"},
            "cable_output":     {"type": "derived", "from": "amplifier_output", "delta_db": -1.8},
            "antenna_eirp":     {"type": "derived", "from": "cable_output", "delta_db": 6.0,
                                 "quantity": "EIRP"},
        },
    }


def _doc(operating="antenna_eirp", limits=None, gain_limits=None,
         amp_points=AMP_POINTS, signal_amplitude=0.8, curves=None):
    if curves is None:
        curves = {
            "sdr_output":       {"interp": "linear", "points": _pts(SDR_POINTS)},
            "amplifier_output": {"interp": "linear", "points": _pts(amp_points)},
        }
    sig = {"occupied_bw_hz": 40_920_000, "curves": curves}
    if signal_amplitude is not None:
        sig["amplitude"] = signal_amplitude
    return {
        "schema_version": 1,
        "unit_id": "unit_test",
        "unit_type": "broadcaster",
        "chain": _chain(operating, limits, gain_limits),
        "signals": {"gps_l1_mcode": sig},
    }


def _resolve(doc=None, type_defaults=None, signal="gps_l1_mcode"):
    return resolve(doc if doc is not None else _doc(), type_defaults, signal)


# ── Happy path ───────────────────────────────────────────────────────────────────

def test_resolves_and_reports_operating_plane():
    r = _resolve()
    assert r.operating_plane == "antenna_eirp"
    assert r.operating_quantity == "EIRP"
    assert r.banner_label() == "EIRP, at antenna_eirp"
    assert r.amplitude == 0.8


def test_ceiling_from_drive_limit_is_top_of_sdr_curve():
    # sdr_output hits -2.5 dBm at 74 dB → ceiling 74 (tighter than hw max 89.75).
    r = _resolve()
    assert r.max_gain_db == pytest.approx(74.0)
    assert r.min_gain_db == 0.0


def test_power_for_gain_walks_derived_hops():
    # antenna_eirp = amplifier_output + 4.2 ; at 60 dB amp=14 → EIRP 18.2
    r = _resolve()
    assert r.power_for_gain(60) == pytest.approx(14.0 + NET_ANTENNA_OFFSET)
    assert r.max_power_dbm == pytest.approx(24.0 + NET_ANTENNA_OFFSET)   # at 74 dB


def test_interpolation_is_exact_at_measured_points():
    # the whole point of the curve: zero error at every measured gain.
    r = _resolve()
    for g, p in AMP_POINTS:
        assert r.power_for_gain(g) == pytest.approx(p + NET_ANTENNA_OFFSET)


def test_interpolation_captures_the_kink():
    # amp between 70→74 has slope 0.5 (22→24 over 4 dB): at 72 dB amp=23.0.
    r = _resolve()
    assert r.power_for_gain(72) == pytest.approx(23.0 + NET_ANTENNA_OFFSET)


def test_inversion_round_trips_within_range():
    r = _resolve()
    for eirp in (10.0, 18.2, 23.0, 27.0):
        g = r.gain_for_power(eirp)
        assert r.power_for_gain(g) == pytest.approx(eirp, abs=1e-9)


def test_gain_for_power_hits_expected_gain():
    r = _resolve()
    # EIRP 18.2 → amp target 14.0 → exactly the 60 dB point.
    assert r.gain_for_power(14.0 + NET_ANTENNA_OFFSET) == pytest.approx(60.0)


# ── Clamping (never extrapolate up past the ceiling) ─────────────────────────────

def test_request_above_max_clamps_to_ceiling():
    r = _resolve()
    g = r.gain_for_power(100.0)
    assert g == pytest.approx(r.max_gain_db)             # 74
    assert r.power_for_gain(g) < 100.0                   # honest report of the clamp


def test_request_below_floor_clamps_to_lowest_gain():
    r = _resolve()
    g = r.gain_for_power(-100.0)
    assert g == pytest.approx(40.0)                      # lowest measured gain


# ── Ceiling: multiple limits, tightest wins ──────────────────────────────────────

def test_regulatory_eirp_limit_tightens_ceiling():
    # add EIRP cap 20 dBm: amp target 15.8 → gain 62.25, tighter than the 74 drive cap.
    limits = [
        {"plane": "sdr_output", "max_dbm": -2.5},
        {"plane": "antenna_eirp", "max_dbm": 20.0, "reason": "regulatory"},
    ]
    r = _resolve(_doc(limits=limits))
    # invert amp curve 60(14)→70(22): 60 + (15.8-14)/(22-14)*10 = 62.25
    assert r.max_gain_db == pytest.approx(62.25)


def test_explicit_max_gain_can_be_the_tightest():
    r = _resolve(_doc(gain_limits={"min_gain_db": 0.0, "max_gain_db": 55.0}))
    assert r.max_gain_db == pytest.approx(55.0)


# ── Operating on the SDR plane directly (pre-amp-pass staging) ────────────────────

def test_operating_on_sdr_plane():
    r = _resolve(_doc(operating="sdr_output"))
    assert r.operating_plane == "sdr_output"
    assert r.power_for_gain(60) == pytest.approx(-16.0)  # no derived hops
    assert r.max_gain_db == pytest.approx(74.0)


# ── offset_db shifts a plane ─────────────────────────────────────────────────────

def test_offset_db_shifts_the_curve():
    curves = {
        "sdr_output":       {"points": _pts(SDR_POINTS)},
        "amplifier_output": {"offset_db": -10.0, "points": _pts(AMP_POINTS)},
    }
    r = _resolve(_doc(curves=curves))
    # amp shifted down 10 dB → antenna at 60 dB = 14 - 10 + 4.2
    assert r.power_for_gain(60) == pytest.approx(14.0 - 10.0 + NET_ANTENNA_OFFSET)


# ── Layered merge: type defaults + per-unit overrides ────────────────────────────

def test_type_defaults_supply_chain_skeleton_and_amplitude():
    type_defaults = {
        "chain": {
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 89.75},
            "operating_plane": "sdr_output",            # conservative default
            "limits": [{"plane": "sdr_output", "max_dbm": -2.5}],
            "planes": {
                "sdr_output":       {"type": "measured", "quantity": "total in-band power"},
                "amplifier_output": {"type": "measured", "quantity": "main-lobe power"},
            },
        },
        "defaults": {"amplitude": 0.8},
    }
    # unit doc: no amplitude on the signal; adds the passive planes; flips operating.
    unit = {
        "schema_version": 1,
        "unit_type": "broadcaster",
        "chain": {
            "operating_plane": "antenna_eirp",          # unit overrides type default
            "planes": {
                "cable_output": {"type": "derived", "from": "amplifier_output", "delta_db": -1.8},
                "antenna_eirp": {"type": "derived", "from": "cable_output", "delta_db": 6.0,
                                 "quantity": "EIRP"},
            },
        },
        "signals": {"gps_l1_mcode": {"curves": {
            "sdr_output":       {"points": _pts(SDR_POINTS)},
            "amplifier_output": {"points": _pts(AMP_POINTS)},
        }}},
    }
    r = resolve(unit, type_defaults, "gps_l1_mcode")
    assert r.operating_plane == "antenna_eirp"           # unit won the merge
    assert r.amplitude == 0.8                            # inherited from type defaults
    assert r.power_for_gain(60) == pytest.approx(14.0 + NET_ANTENNA_OFFSET)


# ── Fail-safe: soft (fall back) vs hard (refuse) ─────────────────────────────────

def test_missing_signal_is_soft_error():
    with pytest.raises(SignalNotCalibrated):
        _resolve(signal="does_not_exist")


def test_non_invertible_curve_refuses():
    # power not strictly increasing with gain → not invertible → hard error.
    bad = [(40, -6.0), (50, 4.0), (60, 4.0), (70, 22.0), (74, 24.0)]
    with pytest.raises(CalibrationError):
        _resolve(_doc(amp_points=bad))


def test_non_increasing_gain_refuses():
    curves = {"sdr_output": {"points": _pts(SDR_POINTS)},
              "amplifier_output": {"points": _pts([(40, -6.0), (40, 4.0)])}}
    with pytest.raises(CalibrationError):
        _resolve(_doc(curves=curves))


def test_no_ceiling_refuses():
    with pytest.raises(CalibrationError):
        _resolve(_doc(operating="sdr_output", limits=[],
                      gain_limits={"min_gain_db": 0.0}))     # no max, no limits


def test_operating_plane_without_curve_refuses():
    # operate on amplifier_output but only measure sdr_output → latent → refuse.
    curves = {"sdr_output": {"points": _pts(SDR_POINTS)}}
    with pytest.raises(CalibrationError):
        _resolve(_doc(operating="amplifier_output", curves=curves,
                      limits=[{"plane": "sdr_output", "max_dbm": -2.5}]))


def test_curve_for_derived_plane_refuses():
    curves = {"sdr_output": {"points": _pts(SDR_POINTS)},
              "amplifier_output": {"points": _pts(AMP_POINTS)},
              "cable_output": {"points": _pts(AMP_POINTS)}}      # derived — illegal
    with pytest.raises(CalibrationError):
        _resolve(_doc(curves=curves))


def test_curve_for_unknown_plane_refuses():
    curves = {"sdr_output": {"points": _pts(SDR_POINTS)},
              "amplifier_output": {"points": _pts(AMP_POINTS)},
              "ghost": {"points": _pts(AMP_POINTS)}}
    with pytest.raises(CalibrationError):
        _resolve(_doc(curves=curves))


def test_dangling_from_refuses():
    doc = _doc()
    doc["chain"]["planes"]["cable_output"]["from"] = "nowhere"
    with pytest.raises(CalibrationError):
        _resolve(doc)


def test_derived_cycle_refuses():
    doc = _doc(operating="cable_output")
    # make cable_output point back through antenna to itself
    doc["chain"]["planes"]["cable_output"]["from"] = "antenna_eirp"
    with pytest.raises(CalibrationError):
        _resolve(doc)


def test_bad_schema_version_refuses():
    doc = _doc()
    doc["schema_version"] = 999
    with pytest.raises(CalibrationError):
        _resolve(doc)


def test_single_point_curve_uses_slope_one_fallback():
    curves = {"sdr_output": {"points": _pts([(60, -16.0)])}}
    r = _resolve(_doc(operating="sdr_output", curves=curves,
                      limits=[{"plane": "sdr_output", "max_dbm": -2.5}],
                      gain_limits={"min_gain_db": 0.0, "max_gain_db": 89.75}))
    # slope-1: 65 dB → -16 + 5 = -11 ; and -2.5 limit → gain 60 + 13.5 = 73.5
    assert r.power_for_gain(65) == pytest.approx(-11.0)
    assert r.max_gain_db == pytest.approx(73.5)


# ── Public artifact (what the agent injects) ─────────────────────────────────────

def test_to_public_dict_flattens_operating_curve():
    r = _resolve()
    d = r.to_public_dict()
    assert d["operating_plane"] == "antenna_eirp"
    assert d["quantity"] == "EIRP"
    assert d["max_gain_db"] == pytest.approx(74.0)
    # curve is the operating-plane transfer at the anchor breakpoints, derived
    # offset (+4.2) folded in: gains from AMP_POINTS, powers shifted.
    gains = [g for g, _ in d["curve"]]
    assert gains == [40.0, 50.0, 60.0, 70.0, 74.0]
    assert d["curve"][2] == [60.0, pytest.approx(14.0 + NET_ANTENNA_OFFSET)]
    assert d["max_power_dbm"] == pytest.approx(24.0 + NET_ANTENNA_OFFSET)


# ── Whole-document validation (validate-on-upload) ───────────────────────────────

def test_validate_document_reports_all_signals():
    from agent.calibration import validate_document
    doc = _doc()
    doc["signals"]["cw"] = {"occupied_bw_hz": 1000, "curves": {
        "sdr_output": {"points": _pts(SDR_POINTS)},
        "amplifier_output": {"points": _pts(AMP_POINTS)},
    }}
    summary = validate_document(doc, None)
    assert set(summary) == {"gps_l1_mcode", "cw"}
    assert summary["gps_l1_mcode"]["operating_plane"] == "antenna_eirp"
    assert summary["gps_l1_mcode"]["max_gain_db"] == pytest.approx(74.0)


def test_validate_document_rejects_a_bad_signal():
    from agent.calibration import validate_document
    doc = _doc()
    # add a second, broken signal → whole upload should be rejected, naming it
    doc["signals"]["cw"] = {"curves": {
        "sdr_output": {"points": _pts([(40, -36.0), (50, -36.0)])},   # not invertible
    }}
    with pytest.raises(CalibrationError) as exc:
        validate_document(doc, None)
    assert "cw" in str(exc.value)


def test_validate_document_reports_doc_level_defect_once():
    from agent.calibration import validate_document
    # No safety ceiling (chain-level defect) with TWO signals present.
    doc = _doc(operating="sdr_output", limits=[], gain_limits={"min_gain_db": 0.0})
    doc["signals"]["cw"] = {"curves": {"sdr_output": {"points": _pts(SDR_POINTS)}}}
    with pytest.raises(CalibrationError) as exc:
        validate_document(doc, None)
    msg = str(exc.value)
    # A single structural defect must not be reported as N per-signal failures.
    assert "invalid signal(s)" not in msg
    assert "ceiling" in msg


def test_validate_document_requires_signals():
    from agent.calibration import validate_document
    doc = _doc()
    doc["signals"] = {}
    with pytest.raises(CalibrationError):
        validate_document(doc, None)


def test_validate_document_rejects_bad_schema_version():
    from agent.calibration import validate_document
    doc = _doc()
    doc["schema_version"] = 2
    with pytest.raises(CalibrationError):
        validate_document(doc, None)


# ── File loaders ─────────────────────────────────────────────────────────────────

def test_resolve_from_files_json_unit_and_yaml_defaults(tmp_path):
    unit_path = tmp_path / "calibration.json"
    defaults_path = tmp_path / "calibration_defaults.yaml"
    unit_path.write_text(json.dumps(_doc()))
    defaults_path.write_text(
        "schema_version: 1\n"
        "types:\n"
        "  broadcaster:\n"
        "    defaults: { amplitude: 0.8 }\n"
    )
    r = resolve_from_files(unit_path, defaults_path, "gps_l1_mcode")
    assert r is not None
    assert r.operating_plane == "antenna_eirp"


def test_resolve_from_files_missing_unit_returns_none(tmp_path):
    r = resolve_from_files(tmp_path / "nope.json", tmp_path / "defs.yaml", "gps_l1_mcode")
    assert r is None


def test_broken_unit_json_refuses(tmp_path):
    p = tmp_path / "calibration.json"
    p.write_text("{ not valid json ")
    with pytest.raises(CalibrationError):
        resolve_from_files(p, tmp_path / "defs.yaml", "gps_l1_mcode")
