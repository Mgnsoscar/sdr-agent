"""Active components (docs/calibration-v2.md): a derived plane carrying a ``control``
block is a programmable gain/attenuation stage on top of its passive baseline. Phase 1
here covers the data model + validation; the achievable-level resolver math is exercised
by later tests in this file as it lands."""
import pytest

from agent import calibration as cal
from agent.calibration import CalibrationError, resolve


# A clean SDR whose curve is 1 dB gain ⇒ 1 dB power over 0..40 dB gain (−40..0 dBm),
# then a programmable attenuator (0..95 dB, 0.25 dB step) as the operating plane.
SDR_POINTS = [(0, -40.0), (40, 0.0)]


def _pts(pairs):
    return [{"gain_db": g, "power_dbm": p} for g, p in pairs]


def _control(**over):
    c = {"task": "atten_set", "param": "attenuation", "sense": "attenuation",
         "min_db": 0.0, "max_db": 95.0, "step_db": 0.25, "engage_pct": 0.0}
    c.update(over)
    return c


def _doc(control=None, gain_step_db=1.0):
    atten = {"type": "derived", "from": "sdr_output", "delta_db": 0.0}
    if control is not None:
        atten["control"] = control
    gl = {"min_gain_db": 0.0, "max_gain_db": 40.0}
    if gain_step_db is not None:
        gl["gain_step_db"] = gain_step_db
    return {
        "schema_version": 1, "unit_type": "broadcaster",
        "chain": {
            "gain_limits": gl,
            "operating_plane": "atten_out",
            "planes": {
                "sdr_output": {"type": "measured", "quantity": "power"},
                "atten_out": {"type": "derived", "from": "sdr_output", "delta_db": 0.0,
                              **({"control": control} if control else {})},
            },
        },
        "signals": {"sig": {"curves": {"sdr_output": {"points": _pts(SDR_POINTS)}}}},
    }


def _resolve(doc):
    return resolve(doc, None, "sig", {})


# ── Phase 1: model + validation ──────────────────────────────────────────────────

def test_active_plane_parses_into_control():
    r = _resolve(_doc(_control()))
    p = r._planes["atten_out"]
    assert p.is_active
    assert p.control.task == "atten_set" and p.control.param == "attenuation"
    assert p.control.applied_hi == 0.0 and p.control.applied_lo == -95.0
    assert p.control.span_db == 95.0


def test_applied_gain_and_param_inverse():
    c = cal._ActiveControl("t", "p", "attenuation", 0.0, 95.0, 0.25, 0.0)
    assert c.param_for_applied(-30.0) == 30.0          # 30 dB attenuation
    assert c.param_for_applied(0.0) == 0.0
    assert c.param_for_applied(-999.0) == 95.0         # clamped to the max
    g = cal._ActiveControl("t", "p", "gain", 10.0, 40.0, 0.5, 0.0)
    assert g.applied_hi == 40.0 and g.applied_lo == 10.0
    assert g.param_for_applied(25.0) == 25.0


def test_a_derived_plane_without_control_stays_passive():
    r = _resolve(_doc(control=None))
    assert r._planes["atten_out"].is_active is False


@pytest.mark.parametrize("bad,frag", [
    (_control(task=""), "task"),
    (_control(param=""), "param"),
    (_control(sense="weird"), "sense"),
    (_control(min_db=5.0, max_db=5.0), "max_db"),
    (_control(step_db=0.0), "step_db"),
    (_control(step_db=-1.0), "step_db"),
    (_control(engage_pct=-1.0), "engage_pct"),
    (_control(engage_pct=101.0), "engage_pct"),
])
def test_bad_control_is_rejected(bad, frag):
    with pytest.raises(CalibrationError) as exc:
        _resolve(_doc(bad))
    assert frag in str(exc.value)


def test_control_missing_numeric_field_is_rejected():
    c = _control(); del c["max_db"]
    with pytest.raises(CalibrationError):
        _resolve(_doc(c))


# ── Phase 2: achievable-level resolver ───────────────────────────────────────────

def test_extended_range_matches_the_spec_example():
    # SDR −40..0 dBm + a 0..95 dB attenuator ⇒ effective −135..0 dBm.
    r = _resolve(_doc(_control()))
    assert r.min_power_dbm == pytest.approx(-135.0)
    assert r.max_power_dbm == pytest.approx(0.0)


def test_sdr_first_keeps_attenuator_at_rest_above_the_floor():
    r = _resolve(_doc(_control(engage_pct=0.0)))
    # From 0 down to −40 the SDR carries it and the attenuator stays at rest.
    for p, g in [(0.0, 40.0), (-20.0, 20.0), (-40.0, 0.0)]:
        out = r.realize(p)
        assert out["sdr_gain_db"] == pytest.approx(g)
        assert out["settings"][0]["value"] == pytest.approx(0.0)
    # Below the floor the SDR is pinned and the attenuator fills the rest.
    out = r.realize(-100.0)
    assert out["sdr_gain_db"] == pytest.approx(0.0)
    assert out["settings"][0]["value"] == pytest.approx(60.0)   # 60 dB attenuation


def test_engage_threshold_keeps_sdr_higher():
    r = _resolve(_doc(_control(engage_pct=50.0)))
    # Threshold at the midpoint (−20): the SDR never drops below gain 20.
    assert r.min_power_dbm == pytest.approx(-115.0)             # −20 − 95
    out = r.realize(-60.0)
    assert out["sdr_gain_db"] == pytest.approx(20.0)
    assert out["settings"][0]["value"] == pytest.approx(40.0)


def test_snap_only_returns_achievable_levels():
    r = _resolve(_doc(_control()))
    # SDR grid is 1 dB; attenuator is 0.25 dB. Mid-range values snap to the combined grid.
    assert r.snap_power(-19.5) == pytest.approx(-19.5)          # SDR −19 + 0.5 dB atten
    assert r.snap_power(-19.1) == pytest.approx(-19.0)          # nearest achievable
    assert r.snap_power(-55.3) == pytest.approx(-55.25)         # attenuator-only region
    # everything realize() offers is reproducible by realize() itself (idempotent)
    for p in (-3.3, -41.7, -88.9, -134.4):
        sp = r.snap_power(p)
        assert r.snap_power(sp) == pytest.approx(sp)


def test_quantize_uses_the_finest_achievable_step():
    r = _resolve(_doc(_control()))
    # A fine 0.25 dB attenuator trims the fraction between the SDR's 1 dB grid points, so
    # the achievable resolution is the 0.25 dB attenuator step across the whole range —
    # even at the top, where the SDR sits at max gain and the attenuator adds the trim.
    assert r.quantize_down(-55.0) == pytest.approx(-55.25)
    assert r.quantize_up(-55.0) == pytest.approx(-54.75)
    assert r.quantize_down(0.0) == pytest.approx(-0.25)
    # With NO active component the step follows the SDR's own 1 dB gain grid.
    passive = _resolve(_doc(control=None))
    assert passive.quantize_down(0.0) == pytest.approx(-1.0)


def test_no_active_component_leaves_range_unchanged():
    # A plain SDR+attenuator-shaped chain but with NO control resolves exactly as a
    # passive chain: min/max power come straight from the gain grid.
    r = _resolve(_doc(control=None))
    assert r.min_power_dbm == pytest.approx(-40.0)
    assert r.max_power_dbm == pytest.approx(0.0)
    assert r.has_active is False
    assert "active_components" not in r.to_public_dict()


def test_artifact_carries_active_components():
    art = _resolve(_doc(_control(engage_pct=10.0))).to_public_dict()
    acs = art["active_components"]
    assert len(acs) == 1
    ac = acs[0]
    assert ac["task"] == "atten_set" and ac["param"] == "attenuation"
    assert ac["sense"] == "attenuation" and ac["step_db"] == 0.25
    assert ac["min_db"] == 0.0 and ac["max_db"] == 95.0 and ac["engage_pct"] == 10.0
    assert art["min_power_dbm"] == pytest.approx(-131.0)        # −36 − 95, T at 10%


def test_two_active_components_with_different_steps_combine():
    # A coarse 1 dB attenuator (0..30) in series with a fine 0.1 dB one (0..5): the
    # combined reduction budget is 35 dB and both steps are realizable.
    doc = _doc(None)
    doc["chain"]["planes"]["atten_out"] = {
        "type": "derived", "from": "sdr_output", "delta_db": 0.0,
        "control": _control(max_db=30.0, step_db=1.0)}
    doc["chain"]["planes"]["fine"] = {
        "type": "derived", "from": "atten_out", "delta_db": 0.0,
        "control": {"task": "fine_set", "param": "att", "sense": "attenuation",
                    "min_db": 0.0, "max_db": 5.0, "step_db": 0.1, "engage_pct": 0.0}}
    doc["chain"]["operating_plane"] = "fine"
    r = resolve(doc, None, "sig", {})
    assert r.min_power_dbm == pytest.approx(-75.0)              # −40 − 35
    out = r.realize(-52.3)                                      # needs 12.3 dB below −40
    assert out["power_dbm"] == pytest.approx(-52.3)
    assert len(out["settings"]) == 2


# ── Phase 3: the transmit-script consumer (calkit) agrees via the artifact ────────

def test_calkit_matches_resolver_via_artifact():
    from paramkit.calkit import PowerMap
    doc = _doc(_control(engage_pct=0.0))
    doc["defaults"] = {"amplitude": 0.5}
    r = resolve(doc, None, "sig", {})
    pm = PowerMap.from_artifact(r.to_public_dict(), fallback_amplitude=0.5)
    assert pm.min_power_dbm == pytest.approx(r.min_power_dbm)
    assert pm.max_power_dbm == pytest.approx(r.max_power_dbm)
    for p in (0.0, -13.3, -20.0, -41.7, -88.9, -134.9, -200.0):
        rr, pk = r.realize(p), pm.realize(p)
        # The SDR gain the script sets matches the resolver…
        assert pm.gain_for_power(p) == pytest.approx(rr["sdr_gain_db"])
        assert pk["sdr_gain_db"] == pytest.approx(rr["sdr_gain_db"])
        # …and the attenuator value the host will command matches too.
        assert pk["settings"][0]["value"] == pytest.approx(rr["settings"][0]["value"])
        assert pk["power_dbm"] == pytest.approx(rr["power_dbm"])


def test_calkit_power_field_kwargs_uses_extended_range():
    from paramkit.calkit import PowerMap
    doc = _doc(_control()); doc["defaults"] = {"amplitude": 0.5}
    pm = PowerMap.from_artifact(resolve(doc, None, "sig", {}).to_public_dict(),
                                fallback_amplitude=0.5)
    kw = pm.power_field_kwargs()
    assert kw["min"] == pytest.approx(-135.0) and kw["max"] == pytest.approx(0.0)
