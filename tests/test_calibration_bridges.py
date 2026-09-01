"""Power-quantity BRIDGES on the operating node (docs/calibration-v2.md §13): the node is
measured once, and the REPORTED reading (operator --power) and LIMITING reading (safety
gauge) derive from that measurement by a bridge (same+k / declared law / own curve).

The reported delta shifts the operator power axis; the artifact carries the bridges so a
runtime consumer re-folds at the live parameter value. A document with no bridge resolves
byte-identically to before."""
import pytest

from agent.calibration import resolve


def _pts(pairs):
    return [{"gain_db": g, "power_dbm": p} for g, p in pairs]


SDR = [(40, -30.0), (60, -10.0), (74, 4.0)]      # slope-1: power == gain - 70


def _doc(*, reported=None, limiting=None, quantity="spectral density"):
    plane = {"type": "measured", "quantity": quantity}
    if reported is not None:
        plane["reported"] = reported
    if limiting is not None:
        plane["limiting"] = limiting
    chain = {
        "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 74.0},
        "operating_plane": "sdr_output",
        "limits": [{"plane": "sdr_output", "max_dbm": 4.0, "reason": "amp"}],
        "planes": {"sdr_output": plane},
    }
    return {"schema_version": 1, "unit_type": "broadcaster", "chain": chain,
            "signals": {"sig": {"curves": {
                "sdr_output": {"interp": "linear", "points": _pts(SDR)}},
                "center_freq_hz": 1.5e9}}}


def _r(**kw):
    return resolve(_doc(**kw), None, "sig")


# ── default: no bridge ⇒ unchanged ──────────────────────────────────────────────

def test_no_bridge_is_byte_identical():
    art = _r().to_public_dict()
    assert "readings" not in art
    assert "operating_unit" not in art
    assert art["quantity"] == "spectral density"


# ── reported: same + constant offset (denominator restatement) ──────────────────

def test_reported_same_k_shifts_operator_axis():
    base = _r()
    off = _r(reported={"kind": "same", "k": 30.0, "unit": "dBm/MHz",
                       "quantity": "spectral density"})
    assert off.max_power_dbm == pytest.approx(base.max_power_dbm + 30.0)
    assert off.min_power_dbm == pytest.approx(base.min_power_dbm + 30.0)
    # gain_for_power: an operator number 30 dB higher lands on the same gain
    p = base.max_power_dbm - 5.0
    assert off.gain_for_power(p + 30.0) == pytest.approx(base.gain_for_power(p))
    # power_for_gain reports 30 dB higher for the same gain
    assert off.power_for_gain(60.0) == pytest.approx(base.power_for_gain(60.0) + 30.0)


def test_reported_unit_and_quantity_surface():
    off = _r(reported={"kind": "same", "k": 30.0, "unit": "dBm/MHz",
                       "quantity": "PSD"})
    art = off.to_public_dict()
    assert art["operating_unit"] == "dBm/MHz"
    assert art["quantity"] == "PSD"
    assert "PSD" in off.banner_label()
    assert art["readings"]["reported"]["kind"] == "same"
    assert art["readings"]["reported"]["k"] == 30.0


# ── reported: declared law (density -> total power, keyed on bw) ─────────────────

FBW = {"id": "fbw", "name": "Full-bandwidth power", "in": "density", "out": "abs",
       "param": "bw", "coeff": 10.0, "ref": 1.0, "rep": 1e7}     # rep 10 MHz → +70 dB


def test_reported_law_shifts_by_rep_delta():
    base = _r()
    law = _r(reported={"kind": "law", "unit": "dBm", "quantity": "full-bandwidth power",
                       "law": FBW})
    assert law.max_power_dbm == pytest.approx(base.max_power_dbm + 70.0)
    assert law.min_power_dbm == pytest.approx(base.min_power_dbm + 70.0)


def test_reported_law_embedded_in_artifact():
    art = _r(reported={"kind": "law", "unit": "dBm", "law": FBW}).to_public_dict()
    r = art["readings"]["reported"]
    assert r["kind"] == "law"
    assert r["law"]["id"] == "fbw"
    assert r["law"]["terms"][0]["param"] == "bw"
    assert art["readings"]["reported_delta_db"] == pytest.approx(70.0)


def test_reported_curve_is_reported_space():
    # the v1-compat curve carries the reported delta so an old script shows the reported number
    base = _r()
    law = _r(reported={"kind": "law", "unit": "dBm", "law": FBW})
    b0 = base.to_public_dict()["curve"]
    l0 = law.to_public_dict()["curve"]
    for (g0, p0), (g1, p1) in zip(b0, l0):
        assert g0 == g1
        assert p1 == pytest.approx(p0 + 70.0)


# ── limiting: carried for runtime enforcement ───────────────────────────────────

def test_limiting_bridge_and_cap_in_artifact():
    art = _r(limiting={"kind": "law", "law": FBW, "max_dbm": 20.0}).to_public_dict()
    lim = art["readings"]["limiting"]
    assert lim["kind"] == "law"
    assert lim["law"]["id"] == "fbw"
    assert lim["max_dbm"] == 20.0


def test_reported_and_limiting_independent():
    # reported per-tooth (total - 10log10 N), limiting total power — two methods, same node
    art = _r(
        reported={"kind": "law", "unit": "dBm", "quantity": "per-tooth",
                  "law": {"id": "tooth", "name": "per-tooth", "param": "n",
                          "coeff": -10.0, "ref": 1.0, "rep": 100}},
        limiting={"kind": "same", "unit": "dBm", "max_dbm": 30.0},
    ).to_public_dict()
    assert art["readings"]["reported"]["law"]["id"] == "tooth"
    assert art["readings"]["limiting"]["kind"] == "same"
    assert art["readings"]["limiting"]["max_dbm"] == 30.0
    # per-tooth at rep N=100 is 20 dB below total
    assert art["readings"]["reported_delta_db"] == pytest.approx(-20.0)


def test_bad_bridge_refused():
    from agent.calibration import CalibrationError
    with pytest.raises(CalibrationError):
        _r(reported={"kind": "law", "law": "not-a-dict-or-known-id"})


def test_per_signal_reading_overrides_plane():
    # plane sets a +10 dB reported default; the signal overrides with +30 dB — signal wins.
    doc = _doc(reported={"kind": "same", "k": 10.0, "unit": "dBm"})
    doc["signals"]["sig"]["reported"] = {"kind": "same", "k": 30.0, "unit": "dBm/MHz"}
    base = _r()
    r = resolve(doc, None, "sig")
    assert r.max_power_dbm == pytest.approx(base.max_power_dbm + 30.0)
    assert r.to_public_dict()["operating_unit"] == "dBm/MHz"


def test_plane_reading_is_the_default_when_signal_has_none():
    doc = _doc(reported={"kind": "same", "k": 10.0, "unit": "dBm"})
    base = _r()
    r = resolve(doc, None, "sig")
    assert r.max_power_dbm == pytest.approx(base.max_power_dbm + 10.0)


# ── own-measurement readings (a separate curve, not derived) ────────────────────

def test_reported_own_curve_drives_the_operator_axis():
    # primary measures density; reported is its OWN separately-measured curve (gain→power)
    own = {"points": _pts([(40, 0.0), (74, 34.0)])}    # slope 1
    r = _r(reported={"kind": "own", "unit": "dBm", "quantity": "main-lobe", "curve": own})
    assert r.power_for_gain(60.0) == pytest.approx(20.0)   # interp(60, own)
    # gain for a reported power inverts the OWN curve
    assert r.gain_for_power(20.0) == pytest.approx(60.0)
    art = r.to_public_dict()
    assert art["readings"]["reported"]["kind"] == "own"
    assert art["readings"]["reported"]["anchor_curve"] == [[40.0, 0.0], [74.0, 34.0]]


def test_limiting_own_curve_sets_the_ceiling():
    # limiting is its OWN main-lobe curve; a cap on it sets the gain ceiling (first-class)
    own = {"points": _pts([(40, -20.0), (74, 14.0)])}  # slope 1
    r = _r(limiting={"kind": "own", "curve": own, "max_dbm": 4.0})
    # cap 4 dBm on the own curve → gain where own == 4 → 40 + (4 − (−20)) = 64
    assert r.max_gain_db == pytest.approx(64.0)
    art = r.to_public_dict()
    assert art["readings"]["limiting"]["kind"] == "own"
    assert art["readings"]["limiting"]["max_dbm"] == 4.0
    assert art["readings"]["limiting"]["anchor_curve"] == [[40.0, -20.0], [74.0, 14.0]]
