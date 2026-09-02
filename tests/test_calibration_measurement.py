"""Per-signal MEASUREMENT quantity + unit (docs/calibration-ui-redesign §5, Phase 2):
a signal declares ``measurement = {quantity, unit}`` — the operator-facing quantity it was
measured in and its display unit. The resolver publishes these as the artifact's operating
quantity/unit (the base --power axis, since Reported is retired), and the unit's FAMILY gauges
the reading bridges: a density measurement feeds the density→dBm laws, and a dBm safety ceiling
can only gauge a dBm limiting reading. Absent ⇒ today's behaviour (plane quantity, dBm)."""
import pytest

from agent.calibration import CalibrationError, resolve


def _pts(pairs):
    return [{"gain_db": g, "power_dbm": p} for g, p in pairs]


SDR = [(40, -30.0), (60, -10.0), (74, 4.0)]      # slope-1: power == gain - 70

# A density→dBm law (constant k) and two deliberately-wrong laws.
DENS_TO_DBM = {"id": "fbw", "name": "Full-bandwidth power", "in": "density", "out": "abs",
               "k": 10.0}
ABS_TO_DBM = {"id": "peak", "name": "Peak→total", "in": "abs", "out": "abs", "k": 3.0}
DENS_TO_DENS = {"id": "restate", "name": "Denominator restate", "in": "density",
                "out": "density", "k": 60.0}


def _doc(*, measurement=None, limiting=None, quantity="spectral density"):
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
            "limits": [{"plane": "sdr_output", "max_dbm": 4.0, "reason": "amp"}],
            "planes": {"sdr_output": {"type": "measured", "quantity": quantity}},
        },
        "signals": {"sig": sig},
    }


def _r(**kw):
    return resolve(_doc(**kw), None, "sig")


# ── publishing ────────────────────────────────────────────────────────────────

def test_measurement_publishes_quantity_and_unit():
    art = _r(measurement={"quantity": "Spectral density at main lobe peak", "unit": "dBm/Hz"},
             limiting={"kind": "law", "law": DENS_TO_DBM}).to_public_dict()
    assert art["quantity"] == "Spectral density at main lobe peak"
    assert art["operating_unit"] == "dBm/Hz"


def test_measurement_quantity_reaches_the_banner():
    r = _r(measurement={"quantity": "Peak spectral density", "unit": "dBm/MHz"},
           limiting={"kind": "law", "law": DENS_TO_DBM})
    assert "Peak spectral density" in r.banner_label()


def test_dbm_measurement_publishes_dbm_unit():
    art = _r(measurement={"quantity": "Full-band power", "unit": "dBm"}).to_public_dict()
    assert art["quantity"] == "Full-band power"
    assert art["operating_unit"] == "dBm"


def test_no_measurement_is_byte_identical():
    art = _r().to_public_dict()
    assert art["quantity"] == "spectral density"        # the plane quantity
    assert "operating_unit" not in art                  # nothing to override the default dBm


def test_measurement_quantity_only_keeps_default_unit():
    # A quantity label without a unit key (dBm implied) publishes the quantity, no unit.
    art = _r(measurement={"quantity": "Total in-band power"}).to_public_dict()
    assert art["quantity"] == "Total in-band power"
    assert "operating_unit" not in art


# ── the unit's family gauges the bridges ───────────────────────────────────────

def test_density_measurement_accepts_a_density_to_dbm_law():
    r = _r(measurement={"quantity": "psd", "unit": "dBm/MHz"},
           limiting={"kind": "law", "law": DENS_TO_DBM})
    assert r.to_public_dict()["operating_unit"] == "dBm/MHz"    # resolves, no raise


def test_density_measurement_rejects_same_limiting():
    with pytest.raises(CalibrationError, match="density"):
        _r(measurement={"quantity": "psd", "unit": "dBm/Hz"}, limiting={"kind": "same"})


def test_density_measurement_rejects_an_absolute_input_law():
    with pytest.raises(CalibrationError, match="expects"):
        _r(measurement={"quantity": "psd", "unit": "dBm/Hz"},
           limiting={"kind": "law", "law": ABS_TO_DBM})


def test_limiting_law_must_return_dbm():
    with pytest.raises(CalibrationError, match="must return dBm"):
        _r(measurement={"quantity": "psd", "unit": "dBm/Hz"},
           limiting={"kind": "law", "law": DENS_TO_DENS})


def test_dbm_measurement_rejects_a_density_input_law():
    with pytest.raises(CalibrationError, match="expects"):
        _r(measurement={"quantity": "power", "unit": "dBm"},
           limiting={"kind": "law", "law": DENS_TO_DBM})


def test_dbm_measurement_allows_same_limiting():
    r = _r(measurement={"quantity": "power", "unit": "dBm"}, limiting={"kind": "same"})
    assert r.to_public_dict()["operating_unit"] == "dBm"        # resolves, no raise


# ── shape validation ───────────────────────────────────────────────────────────

def test_unknown_unit_is_rejected():
    with pytest.raises(CalibrationError, match="not one of"):
        _r(measurement={"quantity": "x", "unit": "dBW"})


def test_measurement_must_be_an_object():
    with pytest.raises(CalibrationError, match="must be an object"):
        _r(measurement=["dBm"])


def test_absent_measurement_skips_family_validation():
    # Without a measurement block, a "same" limiting is not second-guessed (legacy behaviour):
    # the plane quantity stands and nothing raises.
    r = _r(limiting={"kind": "same"})
    assert r.to_public_dict()["quantity"] == "spectral density"
