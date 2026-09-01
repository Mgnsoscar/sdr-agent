"""calkit.PowerMap (the transmit script's consumer) — v1 flat-curve behaviour, the v2
frequency-aware fold (cable/antenna deltas folded at the transmit frequency), the
frequency-split ceiling, and cross-consistency with the agent resolver: script-side
and agent MUST agree on --power ↔ gain at every frequency (docs/calibration-v2.md)."""
import json

import pytest

from agent import calibration as cal
from paramkit.calkit import PowerMap


SDR_POINTS = [(40, -36.0), (50, -26.0), (60, -16.0), (70, -6.0), (74, -2.5)]
AMP_POINTS = [(40, -6.0), (50, 4.0), (60, 14.0), (70, 22.0), (74, 24.0)]
COMPONENTS = {
    "cable_flat": {"kind": "cable",   "delta_db_by_freq": [[0, -1.8]]},
    "ant_flat":   {"kind": "antenna", "delta_db_by_freq": [[0, 6.0]]},
    "cable_fdep": {"kind": "cable",   "delta_db_by_freq": [[1.0e9, -2.0], [2.0e9, -3.0]]},
    "ant_fdep":   {"kind": "antenna", "delta_db_by_freq": [[1.0e9, 5.0], [2.0e9, 7.0]]},
}


def _pts(pairs):
    return [{"gain_db": g, "power_dbm": p} for g, p in pairs]


def _doc(cable, antenna, center_freq_hz=None, limits=None, amplitude=0.8):
    sig = {"curves": {"sdr_output": {"points": _pts(SDR_POINTS)},
                      "amplifier_output": {"points": _pts(AMP_POINTS)}}, "amplitude": amplitude}
    if center_freq_hz is not None:
        sig["center_freq_hz"] = center_freq_hz
    return {
        "schema_version": 1, "unit_type": "broadcaster",
        "chain": {
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 89.75},
            "operating_plane": "antenna_eirp",
            "limits": limits if limits is not None
                      else [{"plane": "sdr_output", "max_dbm": -2.5, "reason": "amp"}],
            "planes": {
                "sdr_output":       {"type": "measured"},
                "amplifier_output": {"type": "measured"},
                "cable_output":     {"type": "derived", "from": "amplifier_output", "component": cable},
                "antenna_eirp":     {"type": "derived", "from": "cable_output", "component": antenna,
                                     "quantity": "EIRP"},
            }},
        "signals": {"sig": sig},
    }


def _pair(doc):
    """(resolver result, PowerMap built from its artifact)."""
    r = cal.resolve(doc, None, "sig", COMPONENTS)
    pm = PowerMap.from_artifact(r.to_public_dict(), fallback_amplitude=0.5)
    return r, pm


# ── v1 flat curve: frequency is irrelevant, behaviour unchanged ──────────────────

def test_v1_flat_chain_ignores_frequency():
    r, pm = _pair(_doc("cable_flat", "ant_flat"))
    assert not pm.freq_dependent
    assert pm.power_for_gain(74.0) == pytest.approx(28.2)         # amp 24 + 4.2
    assert pm.power_for_gain(74.0, freq=9.9e9) == pytest.approx(28.2)   # freq ignored
    assert pm.max_gain_db == pytest.approx(74.0)
    assert pm.gain_for_power(28.2) == pytest.approx(74.0)


# ── v2: deltas fold at the transmit frequency ────────────────────────────────────

def test_v2_power_folds_at_frequency():
    r, pm = _pair(_doc("cable_fdep", "ant_fdep", center_freq_hz=1.5e9))
    assert pm.freq_dependent
    # EIRP@74 = amp(24) + cable(f) + antenna(f)
    assert pm.power_for_gain(74.0, freq=1.0e9) == pytest.approx(27.0)
    assert pm.power_for_gain(74.0, freq=2.0e9) == pytest.approx(28.0)
    assert pm.power_for_gain(74.0, freq=1.5e9) == pytest.approx(27.5)
    # default (no freq) uses the artifact's representative center frequency
    assert pm.power_for_gain(74.0) == pytest.approx(27.5)


def test_v2_frequency_clamps_outside_table():
    r, pm = _pair(_doc("cable_fdep", "ant_fdep", center_freq_hz=1.5e9))
    assert pm.power_for_gain(74.0, freq=0.4e9) == pytest.approx(27.0)   # clamp to 1 GHz
    assert pm.power_for_gain(74.0, freq=5.0e9) == pytest.approx(28.0)   # clamp to 2 GHz


def test_v2_round_trips_gain_power_at_frequency():
    r, pm = _pair(_doc("cable_fdep", "ant_fdep", center_freq_hz=1.5e9))
    for f in (1.0e9, 1.5e9, 2.0e9):
        for g in (45.0, 60.0, 70.0):
            p = pm.power_for_gain(g, freq=f)
            assert pm.gain_for_power(p, freq=f) == pytest.approx(g, abs=1e-6)


# ── the frequency-split ceiling ──────────────────────────────────────────────────

def test_amp_protection_ceiling_stays_put():
    r, pm = _pair(_doc("cable_fdep", "ant_fdep", center_freq_hz=1.5e9))
    # only the sdr_output (measured) limit → a fixed gain ceiling at every frequency
    assert pm._ceiling(1.0e9) == pytest.approx(74.0)
    assert pm._ceiling(2.0e9) == pytest.approx(74.0)


def test_regulatory_cap_ceiling_moves_with_frequency():
    limits = [{"plane": "sdr_output", "max_dbm": -2.5, "reason": "amp"},
              {"plane": "antenna_eirp", "max_dbm": 26.0, "reason": "EIRP cap"}]
    r, pm = _pair(_doc("cable_fdep", "ant_fdep", center_freq_hz=1.5e9, limits=limits))
    # @1 GHz cap ⇒ gain 72; @2 GHz ⇒ gain 70 (matches the resolver's own ceiling)
    assert pm._ceiling(1.0e9) == pytest.approx(72.0)
    assert pm._ceiling(2.0e9) == pytest.approx(70.0)
    # and a requested power above the cap clamps to that frequency's ceiling
    assert pm.gain_for_power(999.0, freq=2.0e9) == pytest.approx(70.0)


# ── cross-consistency: calkit == the agent resolver at every frequency ───────────

@pytest.mark.parametrize("cable,antenna,center", [
    ("cable_flat", "ant_flat", None),
    ("cable_fdep", "ant_fdep", 1.5e9),
])
def test_powermap_matches_resolver(cable, antenna, center):
    limits = [{"plane": "sdr_output", "max_dbm": -2.5, "reason": "amp"},
              {"plane": "antenna_eirp", "max_dbm": 26.0, "reason": "EIRP cap"}]
    doc = _doc(cable, antenna, center_freq_hz=center, limits=limits)
    r, pm = _pair(doc)
    freqs = [1.0e9, 1.5e9, 2.0e9] if center else [None]
    for f in freqs:
        for g in (45.0, 60.0, 72.0):
            assert pm.power_for_gain(g, freq=f) == pytest.approx(r.power_for_gain(g, freq=f))
        for p in (0.0, 10.0, 26.0):
            assert pm.gain_for_power(p, freq=f) == pytest.approx(r.gain_for_power(p, freq=f))


# ── reported operating plane + freq-dependent limit (per-limit anchor curve) ─────────

def _reported_doc():
    """Source measured full-band (limiting) + main-lobe (reported) at one node, then a
    frequency-dependent amplifier; operating plane reads main-lobe EIRP, the limit caps the
    amp's full-band output. The limit must invert against the full-band curve, not main-lobe."""
    src = [(40, -6.0), (74, 24.0)]        # full-band power vs gain
    rep = [(40, -8.0), (74, 21.0)]        # main-lobe (a few dB lower)
    return {
        "schema_version": 1, "unit_type": "broadcaster",
        "chain": {
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 89.75},
            "operating_plane": "amp_out",
            "limits": [{"plane": "amp_out", "max_dbm": 5.0, "reason": "amp output"}],
            "planes": {
                "source":    {"type": "measured", "role": "limiting", "quantity": "total"},
                "main_lobe": {"type": "measured", "role": "reported", "of": "source",
                              "quantity": "main-lobe"},
                "amp_out":   {"type": "derived", "from": "main_lobe", "component": "amp_fdep",
                              "quantity": "EIRP"},
            }},
        "signals": {"sig": {"amplitude": 0.8, "center_freq_hz": 1.5e9,
                    "curves": {"source": {"points": _pts(src)},
                               "main_lobe": {"points": _pts(rep)}}}},
    }


def test_reported_plane_limit_matches_resolver_at_every_frequency():
    comps = {"amp_fdep": {"kind": "amplifier",
                          "delta_db_by_freq": [[1.0e9, 10.0], [2.0e9, 6.0]]}}
    doc = _reported_doc()
    r = cal.resolve(doc, None, "sig", comps)
    pm = PowerMap.from_artifact(r.to_public_dict(), fallback_amplitude=0.8)
    assert pm.freq_dependent
    for f in (1.0e9, 1.5e9, 2.0e9):
        # the ceiling (inverting the full-band limit through the SEPARATE limiting curve)
        # must match the resolver exactly, and the operating point reads main-lobe EIRP.
        assert pm._ceiling(f) == pytest.approx(r._max_gain_at(f), abs=1e-6)
        for g in (45.0, 60.0, 70.0):
            assert pm.power_for_gain(g, freq=f) == pytest.approx(r.power_for_gain(g, freq=f))


# ── baked fallback + load ────────────────────────────────────────────────────────

def test_from_linear_baked_unchanged():
    pm = PowerMap.from_linear(0.0, 89.75, -50.0, -2.5, amplitude=0.7)
    assert pm.source == "baked defaults"
    assert not pm.freq_dependent
    assert pm.power_for_gain(89.75) == pytest.approx(-2.5)


def test_load_reads_v2_artifact_from_env(tmp_path, monkeypatch):
    # artifact measured at 0.8, script transmits at 0.8 → amplitudes match → calibrated.
    r, _ = _pair(_doc("cable_fdep", "ant_fdep", center_freq_hz=2.0e9, amplitude=0.8))
    art_path = tmp_path / "cal.json"
    art_path.write_text(json.dumps(r.to_public_dict()), encoding="utf-8")
    baked = PowerMap.from_linear(0.0, 89.75, -50.0, -2.5, amplitude=0.8)
    monkeypatch.setenv("SDR_CALIBRATION_FILE", str(art_path))
    pm = PowerMap.load(baked)
    assert pm.source == "calibration file" and pm.freq_dependent
    assert pm.warning is None
    assert pm.power_for_gain(74.0) == pytest.approx(28.0)   # folded at the 2 GHz center
    assert pm.amplitude == pytest.approx(0.8)               # the amplitude the curves used


def test_load_rejects_amplitude_mismatch(tmp_path, monkeypatch):
    # Calibration measured at 0.8, but the script drives 0.5 → the calibrated power scale
    # no longer describes this script. It must fall back to uncalibrated (baked) and warn,
    # never silently transmit on a mismatched scale.
    r, _ = _pair(_doc("cable_flat", "ant_flat", amplitude=0.8))
    art_path = tmp_path / "cal.json"
    art_path.write_text(json.dumps(r.to_public_dict()), encoding="utf-8")
    baked = PowerMap.from_linear(0.0, 89.75, -50.0, -2.5, amplitude=0.5)
    monkeypatch.setenv("SDR_CALIBRATION_FILE", str(art_path))
    pm = PowerMap.load(baked)
    assert pm is baked                                     # rejected → the uncalibrated map
    assert pm.source == "baked defaults"
    assert pm.amplitude == pytest.approx(0.5)              # the script's fixed amplitude
    assert pm.warning and "0.8" in pm.warning and "0.5" in pm.warning


def test_load_accepts_matching_amplitude(tmp_path, monkeypatch):
    # Same curves, but calibrated at the script's own 0.5 → accepted.
    r, _ = _pair(_doc("cable_flat", "ant_flat", amplitude=0.5))
    art_path = tmp_path / "cal.json"
    art_path.write_text(json.dumps(r.to_public_dict()), encoding="utf-8")
    baked = PowerMap.from_linear(0.0, 89.75, -50.0, -2.5, amplitude=0.5)
    monkeypatch.setenv("SDR_CALIBRATION_FILE", str(art_path))
    pm = PowerMap.load(baked)
    assert pm.source == "calibration file"
    assert pm.warning is None
    assert pm.amplitude == pytest.approx(0.5)


def test_load_without_env_returns_baked(monkeypatch):
    monkeypatch.delenv("SDR_CALIBRATION_FILE", raising=False)
    baked = PowerMap.from_linear(0.0, 89.75, -50.0, -2.5, amplitude=0.7)
    assert PowerMap.load(baked) is baked


# ── uncalibrated map: no baked absolute scale (issue #4) ─────────────────────────

def test_uncalibrated_map_refuses_absolute_power():
    from paramkit.calkit import NoAbsoluteScale
    pm = PowerMap.uncalibrated(0.0, 89.75, amplitude=0.5)
    assert pm.has_absolute is False
    assert pm.min_power_dbm is None and pm.max_power_dbm is None
    assert pm.power_field_kwargs() == {}                 # --power field unbounded / absent
    assert pm.max_gain_db == pytest.approx(89.75)        # gain limits still apply
    with pytest.raises(NoAbsoluteScale):
        pm.gain_for_power(-30.0)


def test_load_no_file_returns_uncalibrated(monkeypatch):
    monkeypatch.delenv("SDR_CALIBRATION_FILE", raising=False)
    baked = PowerMap.uncalibrated(0.0, 89.75, amplitude=0.5)
    got = PowerMap.load(baked)
    assert got is baked and got.has_absolute is False


def test_load_amplitude_mismatch_returns_uncalibrated(tmp_path, monkeypatch):
    r, _ = _pair(_doc("cable_flat", "ant_flat", amplitude=0.8))
    art_path = tmp_path / "cal.json"
    art_path.write_text(json.dumps(r.to_public_dict()), encoding="utf-8")
    baked = PowerMap.uncalibrated(0.0, 89.75, amplitude=0.5)   # script transmits at 0.5
    monkeypatch.setenv("SDR_CALIBRATION_FILE", str(art_path))
    pm = PowerMap.load(baked)
    assert pm is baked and pm.has_absolute is False and pm.warning


# ── source bias: calkit folds the SDR flatness identically to the resolver ───────

def _bias_doc():
    """SDR-only chain (operating = source) with a per-unit source bias and a source limit,
    so both the delivered power AND the safety ceiling move with the transmit frequency."""
    return {
        "schema_version": 1, "unit_type": "broadcaster",
        "chain": {
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 89.75},
            "operating_plane": "sdr_output",
            "limits": [{"plane": "sdr_output", "max_dbm": -2.5, "reason": "amp"}],
            "planes": {"sdr_output": {"type": "measured", "quantity": "power"}},
        },
        "signals": {"sig": {"amplitude": 0.8, "center_freq_hz": 1.5e9,
                    "curves": {"sdr_output": {"points": _pts(SDR_POINTS)}}}},
        "source_bias": {"power_by_freq": [[1.0e9, -8.0], [1.5e9, -10.0], [2.0e9, -12.0]]},
    }


def test_source_bias_matches_resolver_at_every_frequency():
    r = cal.resolve(_bias_doc(), None, "sig")
    pm = PowerMap.from_artifact(r.to_public_dict(), fallback_amplitude=0.8)
    assert pm.freq_dependent
    for f in (1.0e9, 1.25e9, 1.5e9, 2.0e9):
        for g in (45.0, 60.0, 70.0):
            assert pm.power_for_gain(g, freq=f) == pytest.approx(r.power_for_gain(g, freq=f))
        for p in (-30.0, -20.0, -6.0):
            assert pm.gain_for_power(p, freq=f) == pytest.approx(r.gain_for_power(p, freq=f))
        assert pm._ceiling(f) == pytest.approx(r._max_gain_at(f))
