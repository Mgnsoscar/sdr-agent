"""The measurement de-embed and the source-bias ZERO anchor at the signal's MEASURED-AT frequency
(center_freq_hz), whatever frequency resolve() is asked to fold the read-outs at.

Before: an explicit ``freq_hz`` (a task's live carrier) moved both — the bench cable was evaluated
at the carrier and the flatness was re-zeroed there, so the artifact modelled the SDR at the
carrier as if the curve had been measured there: the flatness correction vanished exactly where
the tone was, and the SDR/attenuator split the agent realized at that frequency no longer matched
the one the script realized from an artifact resolved at center_freq_hz.
"""
import pytest

from agent.calibration import resolve
from paramkit.calkit import PowerMap

CABLES = {"cable_fd": {"kind": "cable", "delta_db_by_freq": [[1e9, -1.0], [2e9, -3.0]]}}
BIAS = [[1.0e9, -3.0], [1.5e9, 0.0], [2.0e9, 3.0]]        # the SDR gets hotter with frequency
POINTS = [(0.0, -80.0), (80.0, 0.0)]                      # measured THROUGH cable_fd at 1.5 GHz


def _doc(center=1.5e9, bias=True, deembed="cable_fd", atten=False):
    curve = {"interp": "linear", "points": [{"gain_db": g, "power_dbm": p} for g, p in POINTS]}
    if deembed:
        curve["measurement_deembed"] = deembed
    sig = {"measurement": {"quantity": "power", "unit": "dBm"}, "curves": {"Source": curve}}
    if center is not None:
        sig["center_freq_hz"] = center
    planes = {"Source": {"type": "measured", "quantity": "power"}}
    op = "Source"
    if atten:
        planes["atten_out"] = {"type": "derived", "from": "Source", "delta_db": 0.0,
                               "control": {"task": "atten_set", "param": "attenuation",
                                           "sense": "attenuation", "min_db": 0.0, "max_db": 95.0,
                                           "step_db": 0.25, "engage_pct": 0.0}}
        op = "atten_out"
    doc = {"schema_version": 1, "unit_type": "broadcaster",
           "chain": {"operating_plane": op,
                     "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 80.0, "gain_step_db": 0.25},
                     "limits": [{"plane": "Source", "max_dbm": 10.0, "reason": "amp"}],
                     "planes": planes},
           "signals": {"sig": sig}, "defaults": {"amplitude": 0.5}}
    if bias:
        doc["source_bias"] = {"power_by_freq": BIAS}
    return doc


def _bias_delta(art):
    return {int(f): d for f, d in (art.get("source_bias_delta_by_freq") or [])}


def test_bias_zero_and_deembed_stay_at_center_freq_whatever_the_fold_frequency():
    at_center = resolve(_doc(), None, "sig", CABLES).to_public_dict()
    at_live = resolve(_doc(), None, "sig", CABLES, freq_hz=1.8e9).to_public_dict()
    # The flatness is zeroed where the curve was measured (1.5 GHz) in BOTH artifacts.
    assert _bias_delta(at_center)[int(1.5e9)] == pytest.approx(0.0)
    assert _bias_delta(at_live)[int(1.5e9)] == pytest.approx(0.0)
    assert _bias_delta(at_live)[int(2e9)] == pytest.approx(3.0)
    # The bench cable is removed at 1.5 GHz (−2 dB → +2 dB) in both, so the anchor curves agree.
    assert at_center["anchor_curve"] == at_live["anchor_curve"]
    assert at_live["anchor_curve"][-1][1] == pytest.approx(2.0)
    # Only the scalar read-outs / the v1 curve follow the fold frequency.
    assert at_center["center_freq_hz"] == 1.5e9 and at_live["center_freq_hz"] == 1.8e9
    assert at_center["max_power_dbm"] == pytest.approx(2.0)
    assert at_live["max_power_dbm"] == pytest.approx(2.0 + 1.8)      # hotter by the flatness rise


def test_the_artifact_models_the_sdr_at_a_carrier_from_the_measured_at_point():
    # Truth at 1.8 GHz for gain 20: measured −60 + the cable removed at 1.5 GHz (+2) + the
    # flatness rise 1.5 → 1.8 GHz (+1.8) = −56.2 dBm — from an artifact resolved at EITHER
    # frequency. (The old zero-at-the-carrier gave −57.4: the rise was erased, the cable at 1.8.)
    for kw in ({}, {"freq_hz": 1.8e9}):
        pm = PowerMap.from_artifact(resolve(_doc(), None, "sig", CABLES, **kw).to_public_dict(), 0.5)
        assert pm.power_for_gain(20.0, freq=1.8e9) == pytest.approx(-56.2, abs=1e-6)
        assert pm.gain_for_power(-56.2, freq=1.8e9) == pytest.approx(20.0)


def test_the_realized_split_no_longer_depends_on_the_fold_frequency():
    # With an attenuator the SDR/attenuator split is chosen by closest exact hit; the agent
    # realizes at the carrier it passes as freq_hz, the script from an artifact resolved without
    # one — both must land on the SAME split at that carrier.
    a = resolve(_doc(atten=True), None, "sig", CABLES).realize(-30.0, 1.8e9)
    b = resolve(_doc(atten=True), None, "sig", CABLES, freq_hz=1.8e9).realize(-30.0, 1.8e9)
    assert a["sdr_gain_db"] == b["sdr_gain_db"]
    assert [s["value"] for s in a["settings"]] == [s["value"] for s in b["settings"]]
    assert a["power_dbm"] == pytest.approx(b["power_dbm"])


def test_without_a_center_freq_the_derived_rep_anchors_the_bias_as_before():
    art = resolve(_doc(center=None, deembed=None), None, "sig", CABLES).to_public_dict()
    # a bias-only chain derives its rep from the sweep midpoint (1.5 GHz here) and zeroes there
    assert _bias_delta(art)[int(1.5e9)] == pytest.approx(0.0)
    assert art["center_freq_hz"] == pytest.approx(1.5e9)
