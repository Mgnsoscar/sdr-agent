"""Measured-plane roles (docs/calibration.md §4.1): a plane is either ``limiting``
(safety limits invert through it) or ``reported`` (a re-measurement of the same node
in a different quantity, shown to the operator but INVISIBLE to limits — the limit
walk punches through it, via ``of``, to the limiting curve).

The scenario throughout is the real one this feature is for: a source measured two ways
at the same node — full-band integrated power (``source``, limiting) and main-lobe power
(``main_lobe``, reported) — feeding an amplifier with a full-band input limit. The user
sees main-lobe power; the amp is protected on full-band.
"""
import pytest

from agent.calibration import CalibrationError, resolve, validate_document


def _pts(pairs):
    return [{"gain_db": g, "power_dbm": p} for g, p in pairs]


# Slope-1 curves; at 80 dB gain full-band = -2.5 dBm, main-lobe = -3.5 dBm (1 dB lower).
SOURCE_PTS = [(70, -12.5), (80, -2.5), (89.75, 7.25)]
MAINLOBE_PTS = [(70, -13.5), (80, -3.5), (89.75, 6.25)]


def _doc(operating="main_lobe", planes_extra=None, limits=None, curves_extra=None):
    planes = {
        "source":    {"type": "measured", "role": "limiting",
                      "quantity": "total in-band power"},
        "main_lobe": {"type": "measured", "role": "reported", "of": "source",
                      "quantity": "main-lobe power"},
        "amplifier": {"type": "derived", "from": "main_lobe", "delta_db": 20.0,
                      "quantity": "amp out"},
        "cable":     {"type": "derived", "from": "amplifier", "delta_db": -1.8},
        "antenna":   {"type": "derived", "from": "cable", "delta_db": 6.0,
                      "quantity": "EIRP"},
    }
    if planes_extra:
        planes.update(planes_extra)
    curves = {
        "source":    {"interp": "linear", "points": _pts(SOURCE_PTS)},
        "main_lobe": {"interp": "linear", "points": _pts(MAINLOBE_PTS)},
    }
    if curves_extra:
        curves.update(curves_extra)
    return {
        "schema_version": 1,
        "unit_id": "unit_test",
        "unit_type": "broadcaster",
        "chain": {
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 89.75},
            "operating_plane": operating,
            "limits": limits if limits is not None else
                      [{"plane": "amplifier", "side": "input", "max_dbm": -2.5,
                        "reason": "amp P1dB input"}],
            "planes": planes,
        },
        "signals": {"gps": {"amplitude": 0.5, "curves": curves}},
    }


# ── The core fix: the amp limit gauges on full-band, --power reports main-lobe ───────

def test_amp_limit_gauges_on_fullband_not_mainlobe():
    r = resolve(_doc(), None, "gps")
    # Amp-input limit -2.5 dBm (full-band) inverts through the SOURCE curve → 80 dB, NOT
    # through the main-lobe curve (which would reach -2.5 only at ~81 dB, over-driving).
    assert r.max_gain_db == pytest.approx(80.0, abs=1e-6)
    # Operating plane is the reported (main-lobe) curve, so the reported ceiling is -3.5.
    assert r.max_power_dbm == pytest.approx(-3.5, abs=1e-6)
    assert r.operating_quantity == "main-lobe power"


def test_reported_quantity_propagates_downstream():
    # "the rest of the chain reflects" main-lobe: observing at the antenna anchors on the
    # main-lobe curve, so the EIRP read-out is main-lobe EIRP (main_lobe + 20 - 1.8 + 6).
    r = resolve(_doc(operating="antenna"), None, "gps")
    assert r.max_gain_db == pytest.approx(80.0, abs=1e-6)         # same full-band ceiling
    assert r.max_power_dbm == pytest.approx(-3.5 + 24.2, abs=1e-6)


def test_attenuator_in_front_of_amp_relaxes_the_source():
    # Drop a -6 dB pad between the source node and the amp. The amp-input limit still
    # gauges on full-band AND follows the stage: the source may now climb 6 dB higher
    # (ceiling 86 dB, full-band = +3.5) while the amp still sees exactly -2.5 dBm.
    doc = _doc(planes_extra={
        "pad":       {"type": "derived", "from": "main_lobe", "delta_db": -6.0},
        "amplifier": {"type": "derived", "from": "pad", "delta_db": 20.0,
                      "quantity": "amp out"},
    })
    r = resolve(doc, None, "gps")
    assert r.max_gain_db == pytest.approx(86.0, abs=1e-6)


# ── Transparency: the resolved gauge plane + quantity are reported ──────────────────

def test_validate_summary_reports_limit_gauge_plane_and_quantity():
    summary = validate_document(_doc())
    gauges = summary["gps"]["limit_gauges"]
    assert len(gauges) == 1
    g = gauges[0]
    # The boundary the limit protects is the amp's input node (the main-lobe/source
    # node); the curve it actually inverts on is the LIMITING one it punches through to.
    assert g["at_plane"] == "main_lobe"
    assert g["gauge_plane"] == "source"
    assert g["gauge_quantity"] == "total in-band power"
    assert g["max_dbm"] == pytest.approx(-2.5)


# ── Guards: a reported plane can never silently back a limit ────────────────────────

def test_reported_without_of_is_refused():
    doc = _doc()
    doc["chain"]["planes"]["main_lobe"].pop("of")
    with pytest.raises(CalibrationError, match="must set 'of'"):
        resolve(doc, None, "gps")


def test_reported_of_unknown_plane_is_refused():
    doc = _doc()
    doc["chain"]["planes"]["main_lobe"]["of"] = "nope"
    with pytest.raises(CalibrationError, match="unknown plane"):
        resolve(doc, None, "gps")


def test_reported_of_a_derived_plane_is_refused():
    doc = _doc()
    doc["chain"]["planes"]["main_lobe"]["of"] = "cable"      # derived, not limiting
    with pytest.raises(CalibrationError, match="must re-measure a 'limiting' plane"):
        resolve(doc, None, "gps")


def test_reported_of_another_reported_plane_is_refused():
    doc = _doc()
    doc["chain"]["planes"]["second"] = {
        "type": "measured", "role": "reported", "of": "main_lobe",
        "quantity": "x"}
    doc["signals"]["gps"]["curves"]["second"] = {
        "interp": "linear", "points": _pts(MAINLOBE_PTS)}
    with pytest.raises(CalibrationError, match="must re-measure a 'limiting' plane"):
        resolve(doc, None, "gps")


def test_limiting_plane_with_of_is_refused():
    doc = _doc()
    doc["chain"]["planes"]["source"]["of"] = "main_lobe"
    with pytest.raises(CalibrationError, match="not 'reported'"):
        resolve(doc, None, "gps")


def test_invalid_role_is_refused():
    doc = _doc()
    doc["chain"]["planes"]["source"]["role"] = "bogus"
    with pytest.raises(CalibrationError, match="invalid role"):
        resolve(doc, None, "gps")


def test_freq_dependent_limit_with_reported_operating_plane_is_refused():
    # A frequency-dependent passive hop feeds a v2 re-fold against the operating plane's
    # published anchor_curve; that path isn't taught the reported→limiting punch-through,
    # so the combination is refused (clearly) rather than mis-gauged.
    components = {"ant": {"delta_db_by_freq": [[1.5e9, 3.0], [1.6e9, 6.0]]}}
    doc = _doc(operating="main_lobe", planes_extra={
        "antenna": {"type": "derived", "from": "cable", "component": "ant",
                    "quantity": "EIRP"}})
    doc["chain"]["limits"] = [
        {"plane": "amplifier", "side": "input", "max_dbm": -2.5},
        {"plane": "antenna", "side": "output", "max_dbm": 50.0}]
    doc["signals"]["gps"]["center_freq_hz"] = 1.5754e9
    with pytest.raises(CalibrationError, match="reported"):
        resolve(doc, None, "gps", components)


# ── Back-compat: a document with no roles resolves exactly as before ────────────────

def test_no_roles_defaults_to_limiting():
    doc = _doc()
    for name in ("source", "main_lobe"):
        doc["chain"]["planes"][name].pop("role", None)
    doc["chain"]["planes"]["main_lobe"].pop("of", None)
    # Now main_lobe is a plain limiting plane in the amp's input path, so the amp limit
    # gauges on the MAIN-LOBE curve — the old (mismatched) behaviour, proving roles are
    # what change it. -2.5 on the main-lobe curve is 1 dB hotter → 81 dB.
    r = resolve(doc, None, "gps")
    assert r.max_gain_db == pytest.approx(81.0, abs=1e-6)
