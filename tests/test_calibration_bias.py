"""Per-unit source-bias stage + stage bypass (agent/calibration.py).

Source bias: a unit-owned CW power-vs-frequency table (the SDR's output flatness),
normalized to each signal's rep frequency, that shifts the SOURCE anchor — so both the
delivered power AND the safety-limit inversion track the transmit frequency. Bypass:
a stage marked bypass:true resolves as a transparent 0-dB hop with its limits dropped
("as if it weren't there"); every stage but the source can be bypassed.
"""
import pytest

from agent.calibration import CalibrationError, resolve


def _pts(pairs):
    return [{"gain_db": g, "power_dbm": p} for g, p in pairs]


# SDR source curve: slope-1, (40,-30) (60,-10) (74,4).
SDR = [(40, -30.0), (60, -10.0), (74, 4.0)]

# P_cw: -8 @ 1.0 GHz, -10 @ 1.5 GHz, -12 @ 2.0 GHz. Normalized to rep 1.5 GHz →
# bias(1.0 GHz)=+2 (SDR hot), bias(1.5 GHz)=0, bias(2.0 GHz)=-2 (SDR cold).
BIAS = {"power_by_freq": [[1.0e9, -8.0], [1.5e9, -10.0], [2.0e9, -12.0]]}


def _doc(*, operating="sdr_output", planes=None, limits=None, gain_limits=None,
         source_bias=None, center_freq_hz=1.5e9, curves=None):
    if planes is None:
        planes = {"sdr_output": {"type": "measured", "quantity": "power"}}
    if curves is None:
        curves = {"sdr_output": {"interp": "linear", "points": _pts(SDR)}}
    sig = {"curves": curves}
    if center_freq_hz is not None:
        sig["center_freq_hz"] = center_freq_hz
    chain = {
        "gain_limits": gain_limits if gain_limits is not None
                       else {"min_gain_db": 0.0, "max_gain_db": 74.0},
        "operating_plane": operating,
        "limits": limits if limits is not None else [],
        "planes": planes,
    }
    doc = {"schema_version": 1, "unit_type": "broadcaster", "chain": chain,
           "signals": {"sig": sig}}
    if source_bias is not None:
        doc["source_bias"] = source_bias
    return doc


def _pad_chain(pad_bypass=False, op_bypass=False):
    """sdr_output (measured) → pad (-10 dB) → antenna (+6 dB, operating)."""
    planes = {
        "sdr_output": {"type": "measured", "quantity": "power"},
        "pad": {"type": "derived", "from": "sdr_output", "delta_db": -10.0, "quantity": "power"},
        "antenna": {"type": "derived", "from": "pad", "delta_db": 6.0, "quantity": "EIRP"},
    }
    if pad_bypass:
        planes["pad"]["bypass"] = True
    if op_bypass:
        planes["antenna"]["bypass"] = True
    return planes


# ── Source bias ──────────────────────────────────────────────────────────────────

def test_bias_is_zero_at_rep_frequency():
    r = resolve(_doc(source_bias=BIAS), None, "sig")
    assert r.power_for_gain(60) == pytest.approx(-10.0)              # rep (None → 1.5 GHz)
    assert r.power_for_gain(60, freq=1.5e9) == pytest.approx(-10.0)


def test_bias_shifts_power_away_from_rep():
    r = resolve(_doc(source_bias=BIAS), None, "sig")
    assert r.power_for_gain(60, freq=1.0e9) == pytest.approx(-8.0)   # SDR +2 hot
    assert r.power_for_gain(60, freq=2.0e9) == pytest.approx(-12.0)  # SDR -2 cold


def test_bias_inverts_in_gain_for_power():
    r = resolve(_doc(source_bias=BIAS), None, "sig")
    # deliver -10 dBm at 1.0 GHz (SDR +2 hot) ⇒ 2 dB less gain: curve(g)=-12 → g=58.
    assert r.gain_for_power(-10, freq=1.0e9) == pytest.approx(58.0)
    assert r.gain_for_power(-10, freq=1.5e9) == pytest.approx(60.0)


def test_bias_tightens_ceiling_where_source_is_hot():
    # A limit on the source (max 4 dBm). At rep the cap is 74 dB (curve(74)=4); at 1.0 GHz
    # the SDR runs +2 hot, so curve(g)+2=4 → curve(g)=2 → g=72. The cap MUST drop (safety).
    d = _doc(source_bias=BIAS,
             limits=[{"plane": "sdr_output", "max_dbm": 4.0, "reason": "P1dB"}],
             gain_limits={"min_gain_db": 0.0, "max_gain_db": 89.75})
    r = resolve(d, None, "sig")
    assert r.gain_for_power(1e6, freq=1.5e9) == pytest.approx(74.0)   # clamps to the cap
    assert r.gain_for_power(1e6, freq=1.0e9) == pytest.approx(72.0)


def test_bias_artifact_publishes_normalized_delta():
    art = resolve(_doc(source_bias=BIAS), None, "sig").to_public_dict()
    assert art["source_bias_delta_by_freq"] == [[1.0e9, 2.0], [1.5e9, 0.0], [2.0e9, -2.0]]
    assert "anchor_curve" in art and art.get("center_freq_hz") == pytest.approx(1.5e9)
    assert art["curve"] == [[40.0, -30.0], [60.0, -10.0], [74.0, 4.0]]   # v1 curve unchanged


def test_bias_without_center_freq_derives_a_rep():
    # No center_freq_hz: the rep is the bias-sweep midpoint (1.5 GHz here), and it still works.
    r = resolve(_doc(source_bias=BIAS, center_freq_hz=None), None, "sig")
    assert r.power_for_gain(60, freq=1.0e9) == pytest.approx(-8.0)
    assert "source_bias_delta_by_freq" in r.to_public_dict()


def test_constant_bias_is_a_noop():
    r = resolve(_doc(source_bias={"power_by_freq": [[1.5e9, -10.0]]}), None, "sig")
    assert r.power_for_gain(60, freq=1.0e9) == pytest.approx(-10.0)


def test_no_bias_stays_flat_and_v1():
    r = resolve(_doc(), None, "sig")
    assert "source_bias_delta_by_freq" not in r.to_public_dict()
    assert r.power_for_gain(60, freq=1.0e9) == pytest.approx(-10.0)


# ── Bypass ─────────────────────────────────────────────────────────────────────

def test_bypass_component_drops_its_delta():
    r = resolve(_doc(operating="antenna", planes=_pad_chain(pad_bypass=True)), None, "sig")
    assert r.power_for_gain(60) == pytest.approx(-4.0)      # sdr(-10) + 0(pad) + 6 = -4
    r2 = resolve(_doc(operating="antenna", planes=_pad_chain()), None, "sig")
    assert r2.power_for_gain(60) == pytest.approx(-14.0)    # sdr(-10) - 10 + 6 = -14


def test_bypass_omits_hop_from_artifact():
    art = resolve(_doc(operating="antenna", planes=_pad_chain(pad_bypass=True)),
                  None, "sig").to_public_dict()
    hop_planes = [h["plane"] for h in art.get("passive_hops", [])]
    assert "pad" not in hop_planes and "antenna" in hop_planes


def test_bypass_drops_limit_on_that_stage():
    d = _doc(operating="antenna", planes=_pad_chain(pad_bypass=True),
             limits=[{"plane": "pad", "max_dbm": -50.0, "reason": "x"}],
             gain_limits={"min_gain_db": 0.0, "max_gain_db": 74.0})
    r = resolve(d, None, "sig")
    assert r.max_gain_db == pytest.approx(74.0)             # the -50 limit didn't apply


def test_bypass_operating_plane_reanchors_upstream():
    r = resolve(_doc(operating="antenna", planes=_pad_chain(op_bypass=True)), None, "sig")
    assert r.power_for_gain(60) == pytest.approx(-20.0)     # falls through to pad: sdr - 10


def test_source_stage_cannot_be_bypassed():
    planes = {"sdr_output": {"type": "measured", "quantity": "power", "bypass": True}}
    with pytest.raises(CalibrationError):
        resolve(_doc(planes=planes), None, "sig")


def test_bypassed_bias_is_skipped():
    r = resolve(_doc(source_bias={**BIAS, "bypass": True}), None, "sig")
    assert "source_bias_delta_by_freq" not in r.to_public_dict()
    assert r.power_for_gain(60, freq=1.0e9) == pytest.approx(-10.0)
