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
