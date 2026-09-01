"""calkit consumes the artifact's `readings` block and applies the reported/limiting
power-quantity bridges at the LIVE parameter value (docs/calibration-v2.md §13) — so
--power stays accurate as a keyed parameter (e.g. sweep bandwidth) is tuned, and the map
agrees with the agent resolver at the representative value."""
import pytest

from agent.calibration import resolve
from paramkit.calkit import PowerMap


def _pts(pairs):
    return [{"gain_db": g, "power_dbm": p} for g, p in pairs]


SDR = [(40, -30.0), (60, -10.0), (74, 4.0)]      # slope-1: power == gain - 70
FBW = {"id": "fbw", "name": "Full-bandwidth power", "in": "density", "out": "abs",
       "param": "bw", "coeff": 10.0, "ref": 1.0, "rep": 1e7}    # rep 10 MHz → +70 dB


def _doc(reported=None, limiting=None):
    plane = {"type": "measured", "quantity": "spectral density"}
    if reported is not None:
        plane["reported"] = reported
    if limiting is not None:
        plane["limiting"] = limiting
    chain = {"gain_limits": {"min_gain_db": 0.0, "max_gain_db": 74.0},
             "operating_plane": "sdr_output",
             "limits": [{"plane": "sdr_output", "max_dbm": 4.0, "reason": "amp"}],
             "planes": {"sdr_output": plane}}
    return {"schema_version": 1, "unit_type": "b", "chain": chain,
            "signals": {"sig": {"curves": {
                "sdr_output": {"interp": "linear", "points": _pts(SDR)}},
                "center_freq_hz": 1.5e9}}}


def _resolved_and_map(reported=None, limiting=None, amp=0.8):
    r = resolve(_doc(reported, limiting), None, "sig")
    art = r.to_public_dict()
    art["amplitude"] = amp
    return r, PowerMap.from_artifact(art, fallback_amplitude=amp)


def test_reported_law_parity_with_agent_at_rep():
    r, pm = _resolved_and_map(reported={"kind": "law", "unit": "dBm", "law": FBW})
    assert pm.max_power_dbm == pytest.approx(r.max_power_dbm, abs=1e-6)
    assert pm.min_power_dbm == pytest.approx(r.min_power_dbm, abs=1e-6)
    # a mid-range reported power maps to the same gain in both, at rep bw
    p = r.max_power_dbm - 6.0
    assert pm.gain_for_power(p, params={"bw": 1e7}) == pytest.approx(
        r.gain_for_power(p), abs=1e-6)


def test_reported_law_refolds_at_live_bandwidth():
    _, pm = _resolved_and_map(reported={"kind": "law", "unit": "dBm", "law": FBW})
    # the SAME gain reads 10 dB higher at 10x the bandwidth (10*log10(10) = 10 dB)
    hi = pm.power_for_gain(60.0, params={"bw": 1e7})
    lo = pm.power_for_gain(60.0, params={"bw": 1e6})
    assert hi - lo == pytest.approx(10.0, abs=1e-6)


def test_reported_roundtrip_at_live_value():
    _, pm = _resolved_and_map(reported={"kind": "law", "unit": "dBm", "law": FBW})
    params = {"bw": 5e6}
    p = pm.min_power_dbm + 20.0          # comfortably inside the range at rep
    g = pm.gain_for_power(p + 0.0, params=params)
    back = pm.power_for_gain(g, params=params)
    # round-trips up to hardware clamp; the gain we get back re-reports consistently
    assert back == pytest.approx(pm.power_for_gain(g, params=params), abs=1e-9)
    assert pm._reported_applies


def test_no_readings_behaves_like_v1():
    _, pm = _resolved_and_map()          # no bridge
    assert pm._reported is None
    assert pm._reported_shift({"bw": 1e9}) == 0.0


def test_limiting_cap_tightens_with_parameter():
    # limiting = full-band total (density + 10log10 bw), capped at 50 dBm total. The measured
    # DENSITY spans -30..4 dBm/Hz, so total spans 30..64 (bw=1e6) — the cap binds mid-curve.
    # As bw grows the total rises, so the gain ceiling must drop.
    _, pm = _resolved_and_map(
        reported={"kind": "same", "unit": "dBm/Hz"},
        limiting={"kind": "law", "law": FBW, "max_dbm": 50.0})
    c_narrow = pm._ceiling(None, {"bw": 1e6})   # target density 50-60 = -10 → gain 60
    c_wide = pm._ceiling(None, {"bw": 1e7})     # target density 50-70 = -20 → gain 50
    assert c_wide < c_narrow
    assert c_narrow - c_wide == pytest.approx(10.0, abs=1e-6)
