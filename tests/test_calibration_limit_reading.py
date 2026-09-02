"""A STAGE safety limit (chain.limits) is gauged through the signal's LIMITING reading
(docs/calibration-v2.md §13; capability calibration-limit-through-reading, agent ≥ 1.13.0).

A limit is a dBm ceiling on a stage boundary; the operating node's LIMITING reading converts
that stage's power into the measured quantity so the ceiling caps the signal correctly whatever
it is measured in. A CONSTANT limiting delta is baked into gain_ceiling_db (C − Δlim); a
PARAMETER-KEYED limiting law is published as a limit the consumer re-folds at the live task
parameter (a via_limiting freq_dependent_limits entry). An OWN limiting reading inverts the
limit against its separate dBm curve. Without a measurement/limiting bridge the resolve is
byte-identical (the old direct inversion against the measured curve)."""
import math

import pytest

from agent.calibration import resolve
from paramkit.calkit import PowerMap


def _pts(pairs):
    return [{"gain_db": g, "power_dbm": p} for g, p in pairs]


SDR = [(40, -30.0), (60, -10.0), (74, 4.0)]      # value == gain − 70 (in the MEASURED quantity)
DENS_TO_DBM = {"id": "fbw", "name": "Full-bandwidth power", "in": "density", "out": "abs",
               "k": 10.0}                          # dBm = density + 10 (constant)
# density → total dBm, keyed on bandwidth: dBm = density + 10·log10(bw); +70 dB at rep 10 MHz.
FBW = {"id": "fbw", "name": "Full-bandwidth power", "in": "density", "out": "abs",
       "param": "bw", "coeff": 10.0, "ref": 1.0, "rep": 1e7}


def _doc(*, measurement=None, limiting=None, quantity="spectral density", max_dbm=4.0):
    sig = {"curves": {"sdr_output": {"interp": "linear", "points": _pts(SDR)}},
           "center_freq_hz": 1.5e9}
    if measurement is not None:
        sig["measurement"] = measurement
    if limiting is not None:
        sig["limiting"] = limiting
    return {
        "schema_version": 1, "unit_type": "broadcaster",
        "chain": {
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 74.0},
            "operating_plane": "sdr_output",
            "limits": [{"plane": "sdr_output", "max_dbm": max_dbm, "reason": "amp"}],
            "planes": {"sdr_output": {"type": "measured", "quantity": quantity}},
        },
        "signals": {"sig": sig},
    }


def _map(art, amp=0.8):
    art = dict(art)
    art["amplitude"] = amp
    return PowerMap.from_artifact(art, fallback_amplitude=amp)


# ── v1 / no bridge: unchanged (direct inversion against the measured curve) ──────

def test_no_reading_is_byte_identical():
    r = resolve(_doc(), None, "sig")
    # stage limit 4.0 inverts against the measured curve → gain 74 (the curve's top point)
    assert r.max_gain_db == pytest.approx(74.0)
    assert r.max_power_dbm == pytest.approx(4.0)
    art = r.to_public_dict()
    # no measurement/limiting bridge → no v2 readings shape, no via flag
    assert "readings" not in art
    assert "freq_dependent_limits" not in art


# ── constant limiting law: the delta is baked into the ceiling (C − Δlim) ────────

def test_constant_law_tightens_the_ceiling():
    # density measured; limiting = density + 10 dBm. A 4 dBm stage ceiling ⇒ density ceiling
    # −6 dBm/Hz ⇒ gain 64 (was 74/4.0 before the fix: 10 dB over the ceiling).
    r = resolve(_doc(measurement={"quantity": "psd", "unit": "dBm/Hz"},
                     limiting={"kind": "law", "law": DENS_TO_DBM}), None, "sig")
    assert r.max_gain_db == pytest.approx(64.0)
    assert r.max_power_dbm == pytest.approx(-6.0)
    art = r.to_public_dict()
    assert art["gain_ceiling_db"] == pytest.approx(64.0)   # baked (constant delta)
    assert art["freq_dependent_limits"] == []              # nothing to re-fold at runtime


def test_constant_law_calkit_parity():
    r = resolve(_doc(measurement={"quantity": "psd", "unit": "dBm/Hz"},
                     limiting={"kind": "law", "law": DENS_TO_DBM}), None, "sig")
    pm = _map(r.to_public_dict())
    assert pm.max_gain_db == pytest.approx(r.max_gain_db, abs=1e-6)
    assert pm.max_power_dbm == pytest.approx(r.max_power_dbm, abs=1e-6)


# ── parameter-keyed limiting law: published as a runtime (via) limit ─────────────

def test_param_keyed_law_is_a_runtime_via_limit():
    r = resolve(_doc(measurement={"quantity": "psd", "unit": "dBm/Hz"},
                     limiting={"kind": "law", "law": FBW}, max_dbm=60.0), None, "sig")
    art = r.to_public_dict()
    # not bakeable (Δlim moves with bw): gain_ceiling_db is the plain gain cap, the limit is a
    # via_limiting entry the consumer re-folds.
    assert art["gain_ceiling_db"] == pytest.approx(74.0)
    fdl = art["freq_dependent_limits"]
    assert len(fdl) == 1 and fdl[0]["via_limiting"] is True and fdl[0]["max_dbm"] == 60.0
    # at rep bw (1e7 → +70): density target 60−70 = −10 → gain 60
    assert r.max_gain_db == pytest.approx(60.0)


def test_param_keyed_law_refolds_at_live_parameter():
    r = resolve(_doc(measurement={"quantity": "psd", "unit": "dBm/Hz"},
                     limiting={"kind": "law", "law": FBW}, max_dbm=60.0), None, "sig")
    pm = _map(r.to_public_dict())
    # narrower bw ⇒ lower total ⇒ a higher gain is allowed; 10× the bandwidth ⇒ 10 dB tighter.
    c_narrow = pm._ceiling(None, {"bw": 1e6})   # target 60−60 = 0 → gain 70
    c_wide = pm._ceiling(None, {"bw": 1e7})     # target 60−70 = −10 → gain 60
    assert c_narrow == pytest.approx(70.0, abs=1e-6)
    assert c_wide == pytest.approx(60.0, abs=1e-6)
    # parity with the resolver at the representative value (bw = rep = 1e7)
    assert pm.max_gain_db == pytest.approx(r.max_gain_db, abs=1e-6)


# ── own limiting reading: the limit inverts against the separate dBm curve ───────

def test_own_limiting_curve_gauges_the_stage_limit():
    own = {"points": _pts([(40, -20.0), (74, 14.0)])}     # slope-1 dBm curve
    r = resolve(_doc(measurement={"quantity": "psd", "unit": "dBm/Hz"},
                     limiting={"kind": "own", "curve": own}), None, "sig")
    # 4 dBm stage ceiling on the OWN curve → gain 40 + (4 − (−20)) = 64
    assert r.max_gain_db == pytest.approx(64.0)
    art = r.to_public_dict()
    assert art["gain_ceiling_db"] == pytest.approx(64.0)
    pm = _map(art)
    assert pm.max_gain_db == pytest.approx(64.0, abs=1e-6)


# ── the ceiling can only tighten: same-quantity dBm measurement is unchanged ─────

def test_dbm_same_measurement_matches_legacy():
    # dBm measurement, limiting == measurement (same): the stage limit inverts against the
    # measured curve exactly as a v1 document would (Δlim = 0).
    r = resolve(_doc(measurement={"quantity": "power", "unit": "dBm"},
                     limiting={"kind": "same"}), None, "sig")
    assert r.max_gain_db == pytest.approx(74.0)
    assert r.max_power_dbm == pytest.approx(4.0)
