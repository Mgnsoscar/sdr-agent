"""Measurement DE-EMBED (docs/calibration-v2.md §14): a measured plane names the
measurement-path cable/pad between it and the analyzer; resolve() removes that loss,
recovering the TRUE power at the plane. It's a bench artifact — never in the transmit path
or the artifact — and it's applied before limit inversion so safety gauges true power."""
import pytest

from agent.calibration import resolve


def _pts(pairs):
    return [{"gain_db": g, "power_dbm": p} for g, p in pairs]


CABLES = {
    "cable1": {"kind": "cable", "delta_db_by_freq": [[0, -1.0]]},   # constant −1 dB
    "cable2": {"kind": "cable", "delta_db_by_freq": [[0, -2.0]]},   # constant −2 dB
}


def _doc(raw_curve, deembed=None, limit_dbm=4.0):
    plane = {"type": "measured", "quantity": "power"}
    if deembed is not None:
        plane["measurement_deembed"] = deembed
    return {
        "schema_version": 1, "unit_type": "b",
        "chain": {
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 74.0},
            "operating_plane": "sdr_output",
            "limits": [{"plane": "sdr_output", "max_dbm": limit_dbm, "reason": "amp"}],
            "planes": {"sdr_output": plane},
        },
        "signals": {"sig": {"curves": {
            "sdr_output": {"interp": "linear", "points": _pts(raw_curve)}}}},
    }


def test_deembed_recovers_true_power():
    # raw SA readings are 1 dB low through cable1; de-embedding recovers the true curve.
    raw = [(40, -31.0), (74, 3.0)]
    base = resolve(_doc(raw), None, "sig")                       # no de-embed → raw
    de = resolve(_doc(raw, deembed="cable1"), None, "sig", CABLES)
    assert de.power_for_gain(60.0) == pytest.approx(base.power_for_gain(60.0) + 1.0)
    assert de.power_for_gain(60.0) == pytest.approx(-10.0)       # true: (40,-30)(74,4) slope 1


def test_cable_swap_gives_the_same_true_power():
    # measure through cable1 (−1): SA reads 1 dB low; through cable2 (−2): 2 dB low. Each
    # de-embed removes its own cable, so the resolved TRUE curve is identical.
    r1 = resolve(_doc([(40, -31.0), (74, 3.0)], deembed="cable1"), None, "sig", CABLES)
    r2 = resolve(_doc([(40, -32.0), (74, 2.0)], deembed="cable2"), None, "sig", CABLES)
    for g in (45.0, 60.0, 70.0):
        assert r1.power_for_gain(g) == pytest.approx(r2.power_for_gain(g))


def test_deembed_shifts_the_safety_ceiling():
    # the amp P1dB limit is a TRUE-power cap, so removing the cable moves the gain ceiling.
    raw = [(40, -31.0), (74, 3.0)]
    base = resolve(_doc(raw, limit_dbm=3.0), None, "sig")
    de = resolve(_doc(raw, deembed="cable1", limit_dbm=3.0), None, "sig", CABLES)
    # true is 1 dB higher, so the same dBm cap is reached at a LOWER gain (tighter ceiling)
    assert de.max_gain_db < base.max_gain_db


def test_deembed_not_published_to_artifact():
    art = resolve(_doc([(40, -31.0), (74, 3.0)], deembed="cable1"), None, "sig",
                  CABLES).to_public_dict()
    assert "measurement_deembed" not in art
    # the published curve is already de-embedded (true) power
    assert art["curve"][0][1] == pytest.approx(-30.0)


def test_inline_deembed_table():
    de = resolve(_doc([(40, -31.0), (74, 3.0)], deembed=[[0, -1.0]]), None, "sig")
    assert de.power_for_gain(60.0) == pytest.approx(-10.0)


def test_unknown_deembed_component_refused():
    from agent.calibration import CalibrationError
    with pytest.raises(CalibrationError):
        resolve(_doc([(40, -31.0), (74, 3.0)], deembed="nope"), None, "sig", CABLES)
