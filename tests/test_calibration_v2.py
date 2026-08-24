"""Unit tests for calibration v2 (docs/calibration-v2.md): passive planes reference a
shared component catalog whose loss/gain is a frequency table, evaluated at the
signal's representative frequency. Covers component resolution, frequency
interpolation + endpoint clamping, the frequency-split ceiling (amp protection stays
put; a regulatory cap moves), the v2 artifact, validation, back-compat with inline
delta_db, and the catalog loader."""
import json

import pytest

from agent import calibration as cal
from agent.calibration import CalibrationError, resolve, validate_document


# Reuse the v1 fixture shape: an amp curve that tops out at 24 dBm at gain 74.
SDR_POINTS = [(40, -36.0), (50, -26.0), (60, -16.0), (70, -6.0), (74, -2.5)]
AMP_POINTS = [(40, -6.0), (50, 4.0), (60, 14.0), (70, 22.0), (74, 24.0)]

# Component catalog: flat parts (1-point = constant) and frequency-dependent ones.
COMPONENTS = {
    "cable_flat": {"kind": "cable",   "delta_db_by_freq": [[0, -1.8]]},
    "ant_flat":   {"kind": "antenna", "delta_db_by_freq": [[0, 6.0]]},
    "cable_fdep": {"kind": "cable",   "delta_db_by_freq": [[1.0e9, -2.0], [2.0e9, -3.0]]},
    "ant_fdep":   {"kind": "antenna", "delta_db_by_freq": [[1.0e9, 5.0], [2.0e9, 7.0]]},
}


def _pts(pairs):
    return [{"gain_db": g, "power_dbm": p} for g, p in pairs]


def _doc(cable="cable_flat", antenna="ant_flat", center_freq_hz=None,
         operating="antenna_eirp", limits=None):
    cable_plane = ({"type": "derived", "from": "amplifier_output", "component": cable}
                   if cable else None)
    antenna_plane = {"type": "derived", "from": "cable_output", "component": antenna,
                     "quantity": "EIRP"}
    sig = {"curves": {
        "sdr_output":       {"points": _pts(SDR_POINTS)},
        "amplifier_output": {"points": _pts(AMP_POINTS)},
    }, "amplitude": 0.8}
    if center_freq_hz is not None:
        sig["center_freq_hz"] = center_freq_hz
    return {
        "schema_version": 1,
        "unit_type": "broadcaster",
        "chain": {
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 89.75},
            "operating_plane": operating,
            "limits": limits if limits is not None
                      else [{"plane": "sdr_output", "max_dbm": -2.5, "reason": "amp P1dB"}],
            "planes": {
                "sdr_output":       {"type": "measured", "quantity": "total in-band power"},
                "amplifier_output": {"type": "measured", "quantity": "main-lobe power"},
                "cable_output":     cable_plane,
                "antenna_eirp":     antenna_plane,
            },
        },
        "signals": {"sig": sig},
    }


def _resolve(doc, freq=None):
    return resolve(doc, None, "sig", COMPONENTS, freq_hz=freq)


# ── component refs behave like inline deltas (back-compat) ───────────────────────

def test_flat_component_matches_inline_delta():
    r = _resolve(_doc(cable="cable_flat", antenna="ant_flat"))
    # amp tops at 24 dBm @ gain 74; cable −1.8 + antenna +6.0 = +4.2 → 28.2 EIRP.
    assert r.max_gain_db == pytest.approx(74.0)          # amp-protection limit, unchanged
    assert r.power_for_gain(74.0) == pytest.approx(28.2)
    # a flat component needs no center_freq_hz
    assert r.operating_quantity == "EIRP"


# ── frequency dependence ─────────────────────────────────────────────────────────

def test_delta_interpolates_at_center_frequency():
    at1 = _resolve(_doc(cable="cable_fdep", antenna="ant_fdep", center_freq_hz=1.0e9))
    at2 = _resolve(_doc(cable="cable_fdep", antenna="ant_fdep", center_freq_hz=2.0e9))
    mid = _resolve(_doc(cable="cable_fdep", antenna="ant_fdep", center_freq_hz=1.5e9))
    # EIRP @74 = amp(24) + cable(f) + antenna(f)
    assert at1.power_for_gain(74.0) == pytest.approx(24 + (-2.0) + 5.0)   # 27.0
    assert at2.power_for_gain(74.0) == pytest.approx(24 + (-3.0) + 7.0)   # 28.0
    assert mid.power_for_gain(74.0) == pytest.approx(24 + (-2.5) + 6.0)   # 27.5


def test_explicit_freq_arg_overrides_center_frequency():
    r = _resolve(_doc(cable="cable_fdep", antenna="ant_fdep", center_freq_hz=1.0e9))
    assert r.power_for_gain(74.0, freq=2.0e9) == pytest.approx(28.0)


def test_frequency_outside_table_clamps_to_the_end():
    r = _resolve(_doc(cable="cable_fdep", antenna="ant_fdep", center_freq_hz=1.0e9))
    below = r.power_for_gain(74.0, freq=0.5e9)     # clamps to 1.0 GHz values
    assert below == pytest.approx(27.0)
    above = r.power_for_gain(74.0, freq=9.0e9)     # clamps to 2.0 GHz values
    assert above == pytest.approx(28.0)


def test_freq_dependent_chain_without_center_freq_refuses():
    with pytest.raises(CalibrationError, match="center_freq_hz"):
        _resolve(_doc(cable="cable_fdep", antenna="ant_fdep", center_freq_hz=None))


def test_amp_protection_ceiling_is_frequency_independent():
    # The mandatory sdr_output limit sits on a MEASURED plane, so the gain ceiling it
    # implies must not move even though the antenna is frequency-dependent.
    lo = _resolve(_doc(cable="cable_fdep", antenna="ant_fdep", center_freq_hz=1.0e9))
    hi = _resolve(_doc(cable="cable_fdep", antenna="ant_fdep", center_freq_hz=2.0e9))
    assert lo.max_gain_db == pytest.approx(74.0)
    assert hi.max_gain_db == pytest.approx(74.0)


def test_regulatory_cap_on_passive_plane_moves_with_frequency():
    # An EIRP cap of 26 dBm at the (frequency-dependent) antenna plane resolves to a
    # DIFFERENT gain ceiling per frequency; the amp limit (74) is the const floor.
    limits = [{"plane": "sdr_output", "max_dbm": -2.5, "reason": "amp"},
              {"plane": "antenna_eirp", "max_dbm": 26.0, "reason": "EIRP cap"}]
    lo = _resolve(_doc(cable="cable_fdep", antenna="ant_fdep",
                       center_freq_hz=1.0e9, limits=limits))
    hi = _resolve(_doc(cable="cable_fdep", antenna="ant_fdep",
                       center_freq_hz=2.0e9, limits=limits))
    # @1 GHz: EIRP=amp+3.0, 26 ⇒ amp 23 ⇒ gain 72.  @2 GHz: EIRP=amp+4.0, 26 ⇒ amp 22 ⇒ gain 70.
    assert lo.max_gain_db == pytest.approx(72.0)
    assert hi.max_gain_db == pytest.approx(70.0)
    assert lo.max_gain_db < 74.0 and hi.max_gain_db < lo.max_gain_db


# ── artifact ─────────────────────────────────────────────────────────────────────

def test_artifact_carries_v1_curve_and_v2_fields():
    r = _resolve(_doc(cable="cable_fdep", antenna="ant_fdep", center_freq_hz=1.5e9))
    art = r.to_public_dict()
    # v1-compat: a folded curve at the representative frequency, gain clamps, amplitude.
    assert art["curve"][0][0] == 40.0 and art["curve"][-1][0] == 74.0
    assert art["curve"][-1][1] == pytest.approx(27.5)   # folded at 1.5 GHz
    assert art["max_gain_db"] == pytest.approx(74.0) and art["amplitude"] == 0.8
    # v2: the unfolded anchor curve + the passive hop tables + the split ceiling.
    assert art["anchor_curve"][-1] == [74.0, 24.0]      # amplifier_output, offset 0
    hops = {h["plane"]: h for h in art["passive_hops"]}
    assert hops["cable_output"]["component"] == "cable_fdep"
    assert hops["cable_output"]["delta_db_by_freq"] == [[1.0e9, -2.0], [2.0e9, -3.0]]
    assert hops["antenna_eirp"]["delta_db_by_freq"] == [[1.0e9, 5.0], [2.0e9, 7.0]]
    assert art["gain_ceiling_db"] == pytest.approx(74.0)
    assert art["center_freq_hz"] == 1.5e9
    assert art["freq_dependent_limits"] == []           # only the sdr limit, which is const


def test_measured_operating_plane_emits_no_v2_fields():
    # Operating directly on sdr_output (a measured plane): identical to v1, no hops.
    r = _resolve(_doc(cable="cable_flat", antenna="ant_flat", operating="sdr_output"))
    art = r.to_public_dict()
    assert "passive_hops" not in art and "anchor_curve" not in art
    assert art["curve"][-1] == [74.0, -2.5]


# ── validation & refusals ────────────────────────────────────────────────────────

def test_unknown_component_refuses():
    with pytest.raises(CalibrationError, match="unknown component"):
        _resolve(_doc(cable="nope", antenna="ant_flat"))


def test_component_and_delta_together_refuses():
    doc = _doc(cable="cable_flat")
    doc["chain"]["planes"]["cable_output"]["delta_db"] = -1.0   # both → error
    with pytest.raises(CalibrationError, match="both"):
        _resolve(doc)


def test_derived_plane_with_neither_refuses():
    doc = _doc(cable="cable_flat")
    doc["chain"]["planes"]["cable_output"] = {"type": "derived", "from": "amplifier_output"}
    with pytest.raises(CalibrationError, match="neither"):
        _resolve(doc)


def test_non_increasing_frequency_table_refuses():
    comps = dict(COMPONENTS)
    comps["bad"] = {"kind": "cable", "delta_db_by_freq": [[2.0e9, -2.0], [1.0e9, -3.0], [2.0e9, -4.0]]}
    doc = _doc(cable="bad", antenna="ant_flat", center_freq_hz=1.5e9)
    with pytest.raises(CalibrationError, match="not strictly increasing"):
        resolve(doc, None, "sig", comps, freq_hz=1.5e9)


def test_validate_document_reports_per_signal_summary():
    summary = validate_document(_doc(cable="cable_fdep", antenna="ant_fdep",
                                     center_freq_hz=1.0e9), None, COMPONENTS)
    assert summary["sig"]["operating_plane"] == "antenna_eirp"
    assert summary["sig"]["max_power_dbm"] == pytest.approx(27.0)


def test_validate_document_flags_unknown_component():
    with pytest.raises(CalibrationError, match="unknown component"):
        validate_document(_doc(cable="nope", antenna="ant_flat"), None, COMPONENTS)


# ── catalog loader ───────────────────────────────────────────────────────────────

def test_load_components_json(tmp_path):
    p = tmp_path / "components.json"
    p.write_text(json.dumps({"components": COMPONENTS}), encoding="utf-8")
    got = cal.load_components(p)
    assert got["cable_fdep"]["delta_db_by_freq"] == [[1.0e9, -2.0], [2.0e9, -3.0]]


def test_load_components_absent_is_empty(tmp_path):
    assert cal.load_components(tmp_path / "nope.yaml") == {}


def test_load_components_malformed_raises(tmp_path):
    p = tmp_path / "components.json"
    p.write_text("{ not json", encoding="utf-8")
    with pytest.raises(CalibrationError, match="not valid"):
        cal.load_components(p)


# ── partial measured stages (a signal measured only at an earlier stage) ─────────

def _two_measured_doc(sig_curves, operating="amplifier_output", cable=None, antenna=None):
    """A chain with two measured stages (sdr_output → amplifier_output), optionally a
    passive cable/antenna after them. `sig_curves` is the one signal's curves dict."""
    planes = {
        "sdr_output":       {"type": "measured", "quantity": "total in-band power"},
        "amplifier_output": {"type": "measured", "quantity": "main-lobe power"},
    }
    if cable:
        planes["cable_output"] = {"type": "derived", "from": "amplifier_output",
                                  "component": cable}
    if antenna:
        planes["antenna_eirp"] = {"type": "derived", "from": "cable_output",
                                  "component": antenna, "quantity": "EIRP"}
    return {
        "schema_version": 1, "unit_type": "broadcaster",
        "chain": {
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 89.75},
            "operating_plane": operating,
            "limits": [{"plane": "sdr_output", "max_dbm": -2.5, "reason": "amp P1dB"}],
            "planes": planes,
        },
        "signals": {"sig": {"amplitude": 0.8, "curves": sig_curves}},
    }


def test_signal_measured_at_both_stages_uses_its_own_downstream_curve():
    doc = _two_measured_doc({"sdr_output": {"points": _pts(SDR_POINTS)},
                             "amplifier_output": {"points": _pts(AMP_POINTS)}})
    r = _resolve(doc)
    assert r.power_for_gain(74.0) == pytest.approx(24.0)   # amp curve, unchanged


def test_signal_measured_only_upstream_falls_back_to_that_curve():
    # amplifier_output is the operating plane but this signal was measured only at
    # sdr_output → it inherits the sdr curve instead of failing.
    doc = _two_measured_doc({"sdr_output": {"points": _pts(SDR_POINTS)}})
    r = _resolve(doc)
    assert r.power_for_gain(74.0) == pytest.approx(-2.5)   # sdr curve, inherited
    assert r.max_gain_db == pytest.approx(74.0)            # amp-protection limit still binds


def test_fallback_through_passive_hops_uses_upstream_anchor():
    # Operating at the antenna, but the signal is measured only at sdr_output. EIRP folds
    # cable+antenna onto the inherited sdr curve; the synthetic amp hop is invisible.
    doc = _two_measured_doc({"sdr_output": {"points": _pts(SDR_POINTS)}},
                            operating="antenna_eirp", cable="cable_flat", antenna="ant_flat")
    r = _resolve(doc)
    # EIRP @74 = sdr(-2.5) + cable(-1.8) + antenna(+6.0) = 1.7
    assert r.power_for_gain(74.0) == pytest.approx(1.7)
    art = r.to_public_dict()
    assert art["anchor_curve"][-1] == [74.0, -2.5]         # the inherited sdr curve
    hops = [h["plane"] for h in art["passive_hops"]]
    assert hops == ["cable_output", "antenna_eirp"]        # no fallback amp hop listed


def test_latent_source_stage_still_refuses():
    # The FIRST stage has nothing upstream to inherit, so a signal with no source curve
    # is a hard error (not silently resolved).
    doc = _two_measured_doc({"amplifier_output": {"points": _pts(AMP_POINTS)}},
                            operating="sdr_output")
    with pytest.raises(CalibrationError, match="no measured curve"):
        _resolve(doc)


def test_resolve_public_with_catalog_file(tmp_path):
    unit = tmp_path / "calibration.json"
    unit.write_text(json.dumps(_doc(cable="cable_fdep", antenna="ant_fdep",
                                    center_freq_hz=2.0e9)), encoding="utf-8")
    comps = tmp_path / "components.yaml"
    comps.write_text(json.dumps({"components": COMPONENTS}), encoding="utf-8")
    art = cal.resolve_public(unit, tmp_path / "no_defaults.yaml", "sig",
                             unit_type="broadcaster", components_path=comps)
    assert art["max_power_dbm"] == pytest.approx(28.0)
    assert art["passive_hops"][0]["plane"] == "cable_output"
