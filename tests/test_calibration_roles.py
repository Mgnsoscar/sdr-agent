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


# ── Frequency-dependent limit with a reported operating plane (1.7.4) ────────────────

_FREQ_COMP = {"amp_fdep": {"kind": "amplifier",
                           "delta_db_by_freq": [[1.0e9, 10.0], [2.0e9, 6.0]]}}


def _fdep_reported_doc():
    """Source measured two ways (full-band 'source' limiting + main-lobe 'main_lobe'
    reported), then a FREQUENCY-DEPENDENT amplifier; the operating plane is the amp output
    (reads main-lobe EIRP) and the safety limit is the amp's full-band output power."""
    return {
        "schema_version": 1, "unit_type": "broadcaster",
        "chain": {
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 89.75},
            "operating_plane": "amp_out",
            "limits": [{"plane": "amp_out", "max_dbm": 5.0, "reason": "amp output"}],
            "planes": {
                "source":    {"type": "measured", "role": "limiting",
                              "quantity": "total in-band power"},
                "main_lobe": {"type": "measured", "role": "reported", "of": "source",
                              "quantity": "main-lobe power"},
                "amp_out":   {"type": "derived", "from": "main_lobe",
                              "component": "amp_fdep", "quantity": "EIRP"},
            }},
        "signals": {"gps": {"amplitude": 0.5, "center_freq_hz": 1.5e9,
                    "curves": {"source": {"points": _pts(SOURCE_PTS)},
                               "main_lobe": {"points": _pts(MAINLOBE_PTS)}}}},
    }


def test_reported_operating_plane_with_freq_limit_resolves():
    # Previously refused ("frequency-dependent limits combined with a 'reported' operating
    # plane aren't supported yet"). Now it resolves.
    r = resolve(_fdep_reported_doc(), None, "gps", _FREQ_COMP, freq_hz=1.5e9)
    assert r.operating_quantity == "EIRP"


def test_freq_limit_publishes_its_own_limiting_curve():
    art = resolve(_fdep_reported_doc(), None, "gps", _FREQ_COMP, freq_hz=1.5e9).to_public_dict()
    # the operating anchor is the reported (main-lobe) curve …
    assert art["anchor_curve"][0][1] == pytest.approx(-13.5)       # main_lobe @70
    fdl = art["freq_dependent_limits"][0]
    assert fdl["plane"] == "amp_out" and fdl["max_dbm"] == 5.0
    # … while the limit carries its OWN limiting (full-band source) curve to invert against
    assert fdl["anchor_curve"][0][1] == pytest.approx(-12.5)       # source @70
    assert fdl["delta_db_by_freq"] == [[1.0e9, 10.0], [2.0e9, 6.0]]


def test_reported_operating_plane_freq_ceiling_moves_with_frequency():
    lo = resolve(_fdep_reported_doc(), None, "gps", _FREQ_COMP, freq_hz=1.0e9)
    hi = resolve(_fdep_reported_doc(), None, "gps", _FREQ_COMP, freq_hz=2.0e9)
    # amp gain 10 dB @1 GHz vs 6 dB @2 GHz → the 5 dBm full-band limit maps to a lower
    # full-band target (hence more allowed gain) at the higher frequency.
    assert hi.max_gain_db > lo.max_gain_db


def test_freq_limit_through_a_different_measured_plane_still_refuses():
    # A genuinely different measured base (not the reported/limiting pair of one node) has no
    # shared delta base, so it stays unsupported.
    doc = _fdep_reported_doc()
    doc["chain"]["planes"]["other"] = {"type": "measured", "quantity": "x"}
    doc["chain"]["planes"]["branch"] = {"type": "derived", "from": "other",
                                        "component": "amp_fdep"}
    doc["chain"]["limits"].append({"plane": "branch", "max_dbm": 5.0, "reason": "x"})
    doc["signals"]["gps"]["curves"]["other"] = {"points": _pts(SOURCE_PTS)}
    with pytest.raises(CalibrationError, match="different measured plane"):
        resolve(doc, None, "gps", _FREQ_COMP, freq_hz=1.5e9)


# ── The core fix: the amp limit gauges on full-band, --power reports main-lobe ───────

def test_amp_limit_gauges_on_fullband_not_mainlobe():
    r = resolve(_doc(), None, "gps")
    # Amp-input limit -2.5 dBm (full-band) inverts through the SOURCE curve → 80 dB, NOT
    # through the main-lobe curve (which would reach -2.5 only at ~81 dB, over-driving).
    assert r.max_gain_db == pytest.approx(80.0, abs=1e-6)
    # Operating plane is the reported (main-lobe) curve, so the reported ceiling is -3.5.
    assert r.max_power_dbm == pytest.approx(-3.5, abs=1e-6)
    assert r.operating_quantity == "main-lobe power"


def test_signal_not_measured_at_reported_stage_passes_through():
    # A signal measured only on the source (full-band) — not on the reported area-of-interest
    # stage — passes straight through: the operating point inherits the source curve, and the
    # amp limit still gauges on the source. (This is the partial-measured-stage fallback,
    # extended to reported stages.)
    doc = _doc()
    doc["signals"]["gps"]["curves"].pop("main_lobe")     # measured on source only
    r = resolve(doc, None, "gps")
    assert r.max_gain_db == pytest.approx(80.0, abs=1e-6)      # limit still on full-band
    assert r.max_power_dbm == pytest.approx(-2.5, abs=1e-6)    # reports the source curve
    # A sibling signal that IS measured at the reported stage still reports area-of-interest.
    doc["signals"]["gps2"] = {"amplitude": 0.5, "curves": {
        "source": {"points": _pts(SOURCE_PTS)},
        "main_lobe": {"points": _pts(MAINLOBE_PTS)}}}
    r2 = resolve(doc, None, "gps2")
    assert r2.max_power_dbm == pytest.approx(-3.5, abs=1e-6)


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


def test_freq_dependent_limit_with_reported_operating_plane_is_supported():
    # A frequency-dependent limit downstream of a REPORTED operating plane now resolves: the
    # limit and the operating plane share the same limiting curve (via the reported→limiting
    # punch-through), which the artifact publishes per-limit so the limit inverts against it.
    components = {"ant": {"delta_db_by_freq": [[1.5e9, 3.0], [1.6e9, 6.0]]}}
    doc = _doc(operating="main_lobe", planes_extra={
        "antenna": {"type": "derived", "from": "cable", "component": "ant",
                    "quantity": "EIRP"}})
    doc["chain"]["limits"] = [
        {"plane": "amplifier", "side": "input", "max_dbm": -2.5},
        {"plane": "antenna", "side": "output", "max_dbm": 50.0}]
    doc["signals"]["gps"]["center_freq_hz"] = 1.5754e9
    r = resolve(doc, None, "gps", components)
    art = r.to_public_dict()
    # operating anchor is the reported (main-lobe) curve; the antenna limit publishes its own
    # limiting (full-band source) curve to invert against.
    assert art["anchor_curve"][0][1] == pytest.approx(-13.5)       # main_lobe @70
    ant = next(l for l in art["freq_dependent_limits"] if l["plane"] == "antenna")
    assert ant["anchor_curve"][0][1] == pytest.approx(-12.5)       # source @70


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
