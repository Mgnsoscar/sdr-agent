"""calkit (the transmit-time fold) honours a measured curve's `extrapolate` setting, so
the SDR gain it commands matches the --power range the agent resolver published. Without
this the client would show/author a wider range than the unit delivers (commanding into
the extrapolated region would silently clamp to a different power). See
docs/calibration.md §7.4 and agent 1.14.0.
"""
import pytest

from agent.calibration import resolve
from paramkit.calkit import PowerMap


def _pts(pairs):
    return [{"gain_db": g, "power_dbm": p} for g, p in pairs]


CURVE = [(30, -20.0), (40, -10.0), (50, 0.0)]      # power == gain - 50, slope 1; device [0, 60]


def _doc(extrapolate=None):
    curve = {"interp": "linear", "points": _pts(CURVE)}
    if extrapolate is not None:
        curve["extrapolate"] = extrapolate
    return {"schema_version": 1, "unit_type": "b",
            "chain": {"gain_limits": {"min_gain_db": 0.0, "max_gain_db": 60.0},
                      "operating_plane": "sdr_output",
                      "planes": {"sdr_output": {"type": "measured", "quantity": "dBm"}}},
            "signals": {"sig": {"curves": {"sdr_output": curve}, "center_freq_hz": 1.5e9}}}


def _resolved_and_map(extrapolate=None, amp=0.8):
    r = resolve(_doc(extrapolate), None, "sig")
    art = r.to_public_dict()
    art["amplitude"] = amp
    return r, PowerMap.from_artifact(art, fallback_amplitude=amp)


def test_v1_artifact_carries_the_flag_and_map_range_matches_resolver():
    r, m = _resolved_and_map("down")
    assert r.to_public_dict()["extrapolate"] == "down"
    assert m.min_power_dbm == pytest.approx(r.min_power_dbm) == pytest.approx(-50.0)
    assert m.max_power_dbm == pytest.approx(r.max_power_dbm) == pytest.approx(0.0)


def test_none_map_stays_clamped():
    _, m = _resolved_and_map(None)
    assert m.min_power_dbm == pytest.approx(-20.0)
    assert m.max_power_dbm == pytest.approx(0.0)


def test_commanding_an_extrapolated_power_gives_the_extrapolated_gain():
    _, m = _resolved_and_map("down")
    # -40 dBm is in the extrapolated region: gain = 30 + (-40 + 20)/1 = 10.
    assert m.gain_for_power(-40.0) == pytest.approx(10.0)
    assert m.power_for_gain(10.0) == pytest.approx(-40.0)
    # A demand below the extrapolated floor clamps to min_gain (0) → its power (-50).
    assert m.power_for_gain(0.0) == pytest.approx(-50.0)


def test_up_matches_resolver_and_respects_ceiling():
    r, m = _resolved_and_map("up")
    assert m.max_power_dbm == pytest.approx(r.max_power_dbm) == pytest.approx(10.0)
    # Commanding above the top delivers at most the ceiling-gain (60) power (+10).
    assert m.gain_for_power(999.0) == pytest.approx(60.0)
    assert m.power_for_gain(60.0) == pytest.approx(10.0)


def test_transmit_gain_agrees_with_resolver_realize():
    r, m = _resolved_and_map("both")
    for p in (-45.0, -20.0, 0.0, 8.0):
        assert m.gain_for_power(p) == pytest.approx(r.realize(p)["sdr_gain_db"], abs=1e-6)
