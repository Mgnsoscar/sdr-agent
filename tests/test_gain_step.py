"""Hardware gain step (chain.gain_limits.gain_step_db): the SDR only settles on a
discrete gain grid, so the resolver and the script-side PowerMap both snap the commanded
gain to that grid — never above the safety ceiling — and report the power at the snapped
gain. Resolver and calkit must agree exactly."""
import pytest

from agent import calibration as cal
from agent.calibration import CalibrationError, resolve, validate_document
from paramkit.calkit import PowerMap


def _pts(pairs):
    return [{"gain_db": g, "power_dbm": p} for g, p in pairs]


def _doc(step=0.25, max_gain=74.1):
    # A single measured plane, power = -40 + 0.5*gain (slope 0.5), ceiling from gain_limits.
    gl = {"min_gain_db": 0.0, "max_gain_db": max_gain}
    if step is not None:
        gl["gain_step_db"] = step
    return {
        "schema_version": 1, "unit_type": "broadcaster",
        "chain": {
            "gain_limits": gl,
            "operating_plane": "source",
            "planes": {"source": {"type": "measured", "quantity": "power"}},
        },
        "signals": {"sig": {"amplitude": 0.5, "curves": {
            "source": {"points": _pts([(0, -40.0), (80, 0.0)])}}}},
    }


def _pair(doc):
    r = resolve(doc, None, "sig")
    pm = PowerMap.from_artifact(r.to_public_dict(), fallback_amplitude=0.5)
    return r, pm


def test_gain_is_snapped_to_the_grid():
    r, pm = _pair(_doc(step=0.25))
    # ideal gain 40.1 → nearest 0.25 grid point 40.0; ideal 40.2 → 40.25.
    assert r.gain_for_power(-19.95) == pytest.approx(40.0, abs=1e-6)
    assert r.gain_for_power(-19.90) == pytest.approx(40.25, abs=1e-6)
    assert pm.gain_for_power(-19.95) == pytest.approx(40.0, abs=1e-6)
    assert pm.gain_for_power(-19.90) == pytest.approx(40.25, abs=1e-6)


def test_power_is_reported_at_the_snapped_gain():
    r, pm = _pair(_doc(step=0.25))
    # a commanded 40.1 really settles at 40.0 → power = -40 + 0.5*40 = -20.0
    assert r.power_for_gain(40.1) == pytest.approx(-20.0, abs=1e-6)
    assert pm.power_for_gain(40.1) == pytest.approx(-20.0, abs=1e-6)


def test_snapping_never_exceeds_the_ceiling():
    # ceiling 74.1 is off-grid; the max settable gain floors to 74.0, and a request past
    # the ceiling clamps there — snapping must not round UP past a safety limit.
    r, pm = _pair(_doc(step=0.25, max_gain=74.1))
    assert r.max_gain_db == pytest.approx(74.0, abs=1e-6)
    assert pm.max_gain_db == pytest.approx(74.0, abs=1e-6)
    assert r.gain_for_power(1000.0) == pytest.approx(74.0, abs=1e-6)   # clamped + floored
    assert pm.gain_for_power(1000.0) == pytest.approx(74.0, abs=1e-6)
    assert r.gain_for_power(1000.0) <= 74.1
    assert pm.gain_for_power(1000.0) <= 74.1


def test_artifact_carries_the_gain_step():
    r = resolve(_doc(step=0.25), None, "sig")
    assert r.to_public_dict()["gain_step_db"] == 0.25


def test_no_step_leaves_gain_continuous():
    r, pm = _pair(_doc(step=None))
    assert r.gain_for_power(-19.95) == pytest.approx(40.1, abs=1e-6)   # not snapped
    assert pm.gain_for_power(-19.95) == pytest.approx(40.1, abs=1e-6)
    assert "gain_step_db" not in r.to_public_dict()


def test_resolver_and_calkit_agree_across_the_range():
    r, pm = _pair(_doc(step=0.25))
    for p in (-39.0, -30.0, -20.05, -19.95, -10.3, -2.5):
        assert r.gain_for_power(p) == pytest.approx(pm.gain_for_power(p), abs=1e-9)


def test_non_positive_step_is_refused():
    with pytest.raises(CalibrationError, match="gain_step_db must be positive"):
        resolve(_doc(step=0.0), None, "sig")
    with pytest.raises(CalibrationError, match="gain_step_db must be positive"):
        resolve(_doc(step=-0.25), None, "sig")


def test_non_positive_step_refused_in_signal_less_document():
    doc = _doc(step=-1.0)
    doc["signals"] = {}
    with pytest.raises(CalibrationError, match="gain_step_db must be positive"):
        validate_document(doc)
