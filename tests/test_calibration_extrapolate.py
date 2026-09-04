"""Per-signal measured-curve EXTRAPOLATION (docs/calibration.md §7.4, agent 1.14.0).

A signal's measured curve may set ``extrapolate: down|up|both`` (default ``none``) to
continue the end-segment slope past the measured gain endpoints, so ``--power`` can be
commanded at a gain that wasn't measured (e.g. below a noise-floor-limited low-gain
point). The commanded gain is still clamped to ``[min_gain, ceiling]`` — extrapolation
extends the curve, never the gain limits. Absent/``none`` is byte-identical to today.
"""
import pytest

from agent.calibration import CalibrationError, resolve


def _pts(pairs):
    return [{"gain_db": g, "power_dbm": p} for g, p in pairs]


# power == gain - 50, slope 1.0 dBm/dB. Measured only over gain [30, 50]; the device gain
# range is wider ([0, 60]), so with no extrapolation the range clamps to the measured span.
CURVE = [(30, -20.0), (40, -10.0), (50, 0.0)]


def _doc(extrapolate=None):
    curve = {"interp": "linear", "points": _pts(CURVE)}
    if extrapolate is not None:
        curve["extrapolate"] = extrapolate
    return {
        "schema_version": 1, "unit_type": "broadcaster",
        "chain": {
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 60.0},
            "operating_plane": "sdr_output",
            "planes": {"sdr_output": {"type": "measured", "quantity": "dBm"}},
        },
        "signals": {"sig": {"curves": {"sdr_output": curve}, "center_freq_hz": 1.5e9}},
    }


def _r(extrapolate=None):
    return resolve(_doc(extrapolate), None, "sig")


# ── the default is unchanged (clamp to the measured span) ─────────────────────

def test_none_clamps_to_the_measured_span():
    for ex in (None, "none"):
        r = _r(ex)
        assert r.min_power_dbm == pytest.approx(-20.0)   # power at the lowest measured gain
        assert r.max_power_dbm == pytest.approx(0.0)     # power at the highest measured gain
        assert "extrapolate" not in r.to_public_dict()   # nothing published when none


# ── down / up / both extend the range at the end slope, bounded by the gain limits ─

def test_down_extends_the_low_end_to_min_gain():
    r = _r("down")
    # gain 0 is 30 dB below the lowest measured point; slope 1 → 30 dB less power.
    assert r.min_power_dbm == pytest.approx(-50.0)
    assert r.max_power_dbm == pytest.approx(0.0)         # up end still clamped
    assert r.to_public_dict()["extrapolate"] == "down"


def test_up_extends_the_high_end_to_the_ceiling():
    r = _r("up")
    assert r.min_power_dbm == pytest.approx(-20.0)       # down end still clamped
    # gain 60 is 10 dB above the top measured point; slope 1 → +10 dB more power.
    assert r.max_power_dbm == pytest.approx(10.0)
    assert r.to_public_dict()["extrapolate"] == "up"


def test_both_extends_both_ends():
    r = _r("both")
    assert r.min_power_dbm == pytest.approx(-50.0)
    assert r.max_power_dbm == pytest.approx(10.0)


# ── the extrapolated region is actually realizable at the extrapolated gain ───

def test_realize_maps_an_extrapolated_power_to_its_extrapolated_gain():
    r = _r("down")
    # -40 dBm sits in the extrapolated region (below the measured -20): gain = 30 + (-40+20) = 10.
    res = r.realize(-40.0)
    assert res["power_dbm"] == pytest.approx(-40.0)
    # Without extrapolation the same command clamps up to the measured floor.
    assert _r("none").realize(-40.0)["power_dbm"] == pytest.approx(-20.0)


def test_extrapolation_never_exceeds_the_gain_ceiling():
    # "up" with a demand far past the ceiling still stops at the ceiling gain (60):
    # power there is the extrapolated +10, not unbounded.
    r = _r("up")
    assert r.realize(1e6)["power_dbm"] == pytest.approx(10.0)


# ── validation ────────────────────────────────────────────────────────────────

def test_invalid_extrapolate_is_rejected():
    with pytest.raises(CalibrationError):
        _r("sideways")


def test_bool_true_means_both():
    r = _r(True)
    assert r.min_power_dbm == pytest.approx(-50.0)
    assert r.max_power_dbm == pytest.approx(10.0)
