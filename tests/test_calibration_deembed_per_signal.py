"""Per-signal + source-bias measurement DE-EMBED (docs/calibration-v2.md §14, agent 1.14.0,
capability calibration-deembed-per-signal).

Two placements beyond the plane-level de-embed:
  * a signal's OWN measured curve (signals.<id>.curves.<plane>.measurement_deembed) — each signal
    is measured at ONE frequency with possibly a different cable, so it removes its own cable as a
    constant at that signal's measured-at frequency, and a signal added later through a new cable is
    corrected independently while the others keep theirs;
  * the source bias (source_bias.measurement_deembed) — the SDR-flatness sweep is over frequency at
    a fixed gain, so its cable loss is removed frequency-by-frequency (a constant-loss cable cancels
    in the rep-frequency normalization, i.e. only a frequency-dependent cable reshapes the flatness).
"""
import pytest

from agent.calibration import CalibrationError, resolve


def _pts(pairs):
    return [{"gain_db": g, "power_dbm": p} for g, p in pairs]


CABLES = {
    "cable1": {"kind": "cable", "delta_db_by_freq": [[0, -1.0]]},          # constant −1 dB
    "cable2": {"kind": "cable", "delta_db_by_freq": [[0, -2.0]]},          # constant −2 dB
    "cable_fd": {"kind": "cable", "delta_db_by_freq": [[1e9, -1.0], [2e9, -3.0]]},  # freq-dependent
}


def _doc(curves, *, source_bias=None, limit_dbm=4.0):
    """curves: {signal_id: {"points": [...], optional "measurement_deembed", "center_freq_hz"}}."""
    signals = {}
    for sid, c in curves.items():
        curve = {"interp": "linear", "points": _pts(c["points"])}
        if "measurement_deembed" in c:
            curve["measurement_deembed"] = c["measurement_deembed"]
        sig = {"curves": {"sdr_output": curve}}
        if "center_freq_hz" in c:
            sig["center_freq_hz"] = c["center_freq_hz"]
        signals[sid] = sig
    doc = {
        "schema_version": 1, "unit_type": "b",
        "chain": {
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 74.0},
            "operating_plane": "sdr_output",
            "limits": [{"plane": "sdr_output", "max_dbm": limit_dbm, "reason": "amp"}],
            "planes": {"sdr_output": {"type": "measured", "quantity": "power"}},
        },
        "signals": signals,
    }
    if source_bias is not None:
        doc["source_bias"] = source_bias
    return doc


# ── per-signal measured-curve de-embed (a constant at the signal's measured-at frequency) ────────

def test_per_signal_curve_deembed_recovers_true_power():
    # The de-embed lives on the SIGNAL's curve, not the plane. cable1 (−1 dB) made the SA read 1 dB
    # low; removing it recovers the true curve (40,−30)(74,4).
    raw = [(40, -31.0), (74, 3.0)]
    base = resolve(_doc({"sig": {"points": raw}}), None, "sig")
    de = resolve(_doc({"sig": {"points": raw, "measurement_deembed": "cable1"}}),
                 None, "sig", CABLES)
    assert de.power_for_gain(60.0) == pytest.approx(base.power_for_gain(60.0) + 1.0)
    assert de.power_for_gain(60.0) == pytest.approx(-10.0)


def test_per_signal_curve_overrides_plane_level_default():
    # A plane-level default (cable2, −2) is overridden by the signal's own curve-level cable1 (−1),
    # so the recovered curve is +1 (not +2) over raw.
    raw = [(40, -31.0), (74, 3.0)]
    doc = _doc({"sig": {"points": raw, "measurement_deembed": "cable1"}})
    doc["chain"]["planes"]["sdr_output"]["measurement_deembed"] = "cable2"   # plane default
    de = resolve(doc, None, "sig", CABLES)
    assert de.power_for_gain(60.0) == pytest.approx(-10.0)                   # cable1 won (+1), not +2


def test_different_cable_per_signal_each_corrected_independently():
    # The owner's case: sig1 measured through cable1 (−1), sig2 later through cable2 (−2). Each
    # signal names its own cable on its own curve, and each resolves to the SAME true curve.
    doc = _doc({
        "sig1": {"points": [(40, -31.0), (74, 3.0)], "measurement_deembed": "cable1"},
        "sig2": {"points": [(40, -32.0), (74, 2.0)], "measurement_deembed": "cable2"},
    })
    r1 = resolve(doc, None, "sig1", CABLES)
    r2 = resolve(doc, None, "sig2", CABLES)
    for g in (45.0, 60.0, 70.0):
        assert r1.power_for_gain(g) == pytest.approx(r2.power_for_gain(g))
    assert r1.power_for_gain(60.0) == pytest.approx(-10.0)                   # true (40,−30)(74,4)


def test_per_signal_freq_dependent_deembed_uses_the_signal_center_freq():
    # A frequency-dependent cable removes its loss AT the signal's measured-at frequency: at 2 GHz
    # cable_fd is −3 dB, so the true curve is 3 dB above the raw reading.
    raw = [(40, -31.0), (74, 3.0)]
    de = resolve(_doc({"sig": {"points": raw, "measurement_deembed": "cable_fd",
                               "center_freq_hz": 2e9}}), None, "sig", CABLES)
    assert de.power_for_gain(40.0) == pytest.approx(-28.0)                   # −31 − (−3) = −28


def test_per_signal_deembed_not_published_and_curve_is_true():
    art = resolve(_doc({"sig": {"points": [(40, -31.0), (74, 3.0)],
                                "measurement_deembed": "cable1"}}), None, "sig",
                  CABLES).to_public_dict()
    assert "measurement_deembed" not in art
    assert art["curve"][0][1] == pytest.approx(-30.0)                        # already de-embedded


def test_per_signal_deembed_shifts_the_safety_ceiling():
    raw = [(40, -31.0), (74, 3.0)]
    base = resolve(_doc({"sig": {"points": raw}}, limit_dbm=3.0), None, "sig")
    de = resolve(_doc({"sig": {"points": raw, "measurement_deembed": "cable1"}}, limit_dbm=3.0),
                 None, "sig", CABLES)
    assert de.max_gain_db < base.max_gain_db                                 # true 1 dB higher → tighter


def test_unknown_per_signal_deembed_component_refused():
    with pytest.raises(CalibrationError):
        resolve(_doc({"sig": {"points": [(40, -31.0), (74, 3.0)],
                              "measurement_deembed": "nope"}}), None, "sig", CABLES)


# ── source-bias de-embed (frequency-by-frequency across the flatness sweep) ──────────────────────

def _bias_delta(res):
    return {int(f): d for f, d in (res.to_public_dict().get("source_bias_delta_by_freq") or [])}


def test_source_bias_freq_dependent_deembed_reshapes_the_flatness():
    # The SDR was driven to read FLAT (0 dBm) at 1 and 2 GHz through cable_fd (−1 dB @1G, −3 dB
    # @2G). The true output therefore RISES with frequency (+1 @1G, +3 @2G). Normalized to the rep
    # frequency (1 GHz) the flatness is 0 @1G, +2 dB @2G — where a raw (no-de-embed) read would be flat.
    sb = {"power_by_freq": [[1e9, 0.0], [2e9, 0.0]], "measurement_deembed": "cable_fd"}
    sig = {"sig": {"points": [(40, -31.0), (74, 3.0)], "center_freq_hz": 1e9}}
    de = _bias_delta(resolve(_doc(sig, source_bias=sb), None, "sig", CABLES))
    base = _bias_delta(resolve(_doc(sig, source_bias={"power_by_freq": [[1e9, 0.0], [2e9, 0.0]]}),
                               None, "sig", CABLES))
    assert de[int(1e9)] == pytest.approx(0.0)              # rep frequency: normalized to 0
    assert de[int(2e9)] == pytest.approx(2.0, abs=1e-6)    # reshaped by L(2G) − L(1G) = −3 − (−1)
    assert base[int(2e9)] == pytest.approx(0.0)            # without de-embed the sweep stays flat


def test_source_bias_constant_cable_is_a_noop_on_normalized_flatness():
    # A constant-loss cable shifts absolute power but not the SHAPE, and the bias is normalized to
    # the rep frequency, so a constant de-embed leaves the published flatness identical.
    sig = {"sig": {"points": [(40, -31.0), (74, 3.0)], "center_freq_hz": 1e9}}
    pbf = [[1e9, 0.0], [2e9, -1.5]]
    de = _bias_delta(resolve(_doc(sig, source_bias={"power_by_freq": pbf,
                                                    "measurement_deembed": "cable1"}),
                             None, "sig", CABLES))
    base = _bias_delta(resolve(_doc(sig, source_bias={"power_by_freq": pbf}), None, "sig", CABLES))
    assert de == pytest.approx(base)


def test_unknown_source_bias_deembed_component_refused():
    sb = {"power_by_freq": [[1e9, 0.0], [2e9, 0.0]], "measurement_deembed": "nope"}
    with pytest.raises(CalibrationError):
        resolve(_doc({"sig": {"points": [(40, -31.0), (74, 3.0)], "center_freq_hz": 1e9}},
                     source_bias=sb), None, "sig", CABLES)
