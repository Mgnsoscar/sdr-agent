"""calkit: fold the SDR gain with the active components PINNED (``pinned_applied`` +
``gain_for_power(applied_db=…)``) — for a script whose carrier moves without the agent
re-commanding the attenuator (the CW drift). The split is chosen once, at the launch carrier,
by the party that commands the components; re-realizing at every new frequency would hop the
assumed attenuation by whole steps while the physical attenuator stays put."""
import pytest

from agent.calibration import resolve
from paramkit.calkit import PowerMap

BIAS = [[1.0e9, -3.0], [1.5e9, 0.0], [2.0e9, 3.0]]


def _doc(atten=True):
    planes = {"sdr_output": {"type": "measured", "quantity": "power"}}
    op = "sdr_output"
    if atten:
        planes["atten_out"] = {"type": "derived", "from": "sdr_output", "delta_db": 0.0,
                               "control": {"task": "atten_set", "param": "attenuation",
                                           "sense": "attenuation", "min_db": 0.0, "max_db": 95.0,
                                           "step_db": 0.25, "engage_pct": 0.0}}
        op = "atten_out"
    return {
        "schema_version": 1, "unit_type": "broadcaster",
        "chain": {"gain_limits": {"min_gain_db": 0.0, "max_gain_db": 40.0, "gain_step_db": 1.0},
                  "operating_plane": op, "planes": planes},
        "source_bias": {"power_by_freq": BIAS},
        "signals": {"sig": {"center_freq_hz": 1.5e9, "curves": {"sdr_output": {"points": [
            {"gain_db": 0, "power_dbm": -40.0}, {"gain_db": 40, "power_dbm": 0.0}]}}}},
        "defaults": {"amplitude": 0.5},
    }


def _pm(atten=True):
    return PowerMap.from_artifact(resolve(_doc(atten), None, "sig").to_public_dict(), 0.5)


def test_pinned_applied_is_the_realizations_split_at_that_frequency():
    pm = _pm()
    f0 = 1575.42e6
    res = pm.realize(-30.0, freq=f0)
    pin = pm.pinned_applied(-30.0, freq=f0)
    assert pin == pytest.approx(sum(s["applied_db"] for s in res["settings"]))
    assert pin <= 0.0                                              # an attenuator: applied ≤ 0
    # Folding with that pin reproduces the SDR gain of the same realization exactly.
    assert pm.gain_for_power(-30.0, freq=f0, applied_db=pin) == pytest.approx(res["sdr_gain_db"])
    assert pm.power_for_gain(res["sdr_gain_db"], freq=f0, applied_db=pin) == pytest.approx(res["power_dbm"])


def test_a_pinned_fold_moves_only_the_sdr_gain_as_the_carrier_drifts():
    pm = _pm()
    f0, pin = 1575.42e6, None
    pin = pm.pinned_applied(-30.0, freq=f0)
    # As the tone drifts the attenuator stays at `pin`; the SDR gain alone follows the flatness,
    # and the delivered power stays on target to within the SDR's 1 dB grid.
    for f in (1.2e9, 1.4e9, 1.6e9, 1.9e9):
        g = pm.gain_for_power(-30.0, freq=f, applied_db=pin)
        assert abs(pm.power_for_gain(g, freq=f, applied_db=pin) - (-30.0)) <= 0.5 + 1e-9
    # Bias −3 → +3 over the sweep: the pinned gain spans that 6 dB (the attenuator absorbs none).
    g_lo = pm.gain_for_power(-30.0, freq=1.0e9, applied_db=pin)
    g_hi = pm.gain_for_power(-30.0, freq=2.0e9, applied_db=pin)
    assert g_lo - g_hi == pytest.approx(6.0, abs=1.0)
    # …whereas re-realizing at another frequency would pick a DIFFERENT attenuation (the very
    # mismatch pinning prevents).
    assert any(pm.pinned_applied(-30.0, freq=f) != pin for f in (1.2e9, 1.4e9, 1.6e9, 1.9e9))


def test_pinning_is_a_no_op_without_active_components():
    pm = _pm(atten=False)
    assert pm.pinned_applied(-30.0, freq=1.5e9) is None
    g = pm.gain_for_power(-30.0, freq=1.5e9)
    assert pm.gain_for_power(-30.0, freq=1.5e9, applied_db=None) == g
    assert pm.gain_for_power(-30.0, freq=1.5e9, applied_db=-5.0) == g      # ignored: nothing to pin
    assert pm.power_for_gain(g, freq=1.5e9, applied_db=-5.0) == pm.power_for_gain(g, freq=1.5e9)


def test_pinned_applied_refuses_uncalibrated():
    pm = PowerMap.uncalibrated(0.0, 40.0, 0.5)
    assert pm.pinned_applied(-30.0) is None
