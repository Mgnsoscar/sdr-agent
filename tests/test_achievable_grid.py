"""The shared achievable-power resolver (paramkit/achievable.py) must offer only genuinely
achievable levels — for snap, realize AND quantize — even when the SDR gain step and the
attenuator step are different and NOT multiples of one another (a vernier grid finer than
either step). Mirrored verbatim in the client (state/achievable.py)."""
import pytest

from paramkit.achievable import AchievableGrid, Active


# SDR: a 1:1 curve so delivered power == gain, over 0..40 dB → −40..0 dBm.
def _grid(gain_step, atten_step, atten_span=30.0, engage_pct=0.0):
    return AchievableGrid(
        power_for_gain=lambda g: g - 40.0,
        gain_for_power=lambda p: p + 40.0,
        min_gain=0.0, ceiling=40.0, gain_step=gain_step,
        actives=[Active(0.0, -atten_span, atten_step, engage_pct)])


def _is_achievable(g, x, bases, atten_step, span):
    for b in bases:
        r = b - x
        if -1e-9 <= r <= span + 1e-9 and abs(round(r / atten_step) - r / atten_step) < 1e-6:
            return True
    return False


# ── non-commensurate steps (SDR 1.0 dB, attenuator 0.3 dB → 0.1 dB vernier) ──────────

def test_snap_finds_exact_level_reachable_at_a_higher_sdr_gain():
    g = _grid(1.0, 0.3)
    # −50.0 is exactly reachable (SDR base −38 dBm − 12.0 dB atten); a naive 2-gain search
    # that only tried the floor gain would miss it and land 0.1 dB off.
    assert g.snap(-50.0) == pytest.approx(-50.0)


def test_quantize_steps_through_the_true_vernier_not_the_attenuator_step():
    g = _grid(1.0, 0.3)
    # The true neighbours of −50.0 are ±0.1 (the 1.0/0.3 vernier), NOT ±0.3.
    assert g.quantize_down(-50.0) == pytest.approx(-50.1)
    assert g.quantize_up(-50.0) == pytest.approx(-49.9)
    assert g.quantize_up(-50.2) == pytest.approx(-50.1)


def test_every_offered_level_is_genuinely_achievable():
    g = _grid(1.0, 0.3)
    bases = [b for b in range(-40, 1)]                 # SDR grid delivers integer dBm here
    vals = [g.snap(-50.15), g.quantize_down(-50.0), g.quantize_up(-50.2),
            g.quantize_up(-50.0), g.snap(-63.37)]
    for v in vals:
        assert _is_achievable(None, v, bases, 0.3, 30.0), v


def test_range_and_sdr_first_hold_with_non_commensurate_steps():
    g = _grid(1.0, 0.3)
    assert g.bounds() == (pytest.approx(-70.0), pytest.approx(0.0))   # −40 −30 .. 0
    # In the SDR's own range the attenuator stays at rest (SDR-first).
    r = g.realize(-20.0)
    assert r["sdr_gain_db"] == pytest.approx(20.0) and r["applied"][0] == pytest.approx(0.0)


# ── commensurate + degenerate cases still behave ─────────────────────────────────────

def test_commensurate_steps_still_correct():
    g = _grid(1.0, 0.25)
    assert g.quantize_down(0.0) == pytest.approx(-0.25)
    assert g.snap(-55.3) == pytest.approx(-55.25)


def test_no_active_component_snaps_to_the_sdr_gain_grid():
    g = AchievableGrid(lambda x: x - 40.0, lambda p: p + 40.0,
                       0.0, 40.0, 1.0, [])            # SDR-only
    assert g.bounds() == (pytest.approx(-40.0), pytest.approx(0.0))
    assert g.snap(-19.6) == pytest.approx(-20.0)
    assert g.quantize_down(-20.0) == pytest.approx(-21.0)   # the real 1 dB gain grid


def test_no_active_threshold_gain_robust_to_inverse_float_noise():
    # The threshold/float-overshoot fix must apply to a plain SDR-only (no-active) chain too:
    # a fractional operating-plane floor whose inverse carries float noise must not drop the
    # minimum SDR gain from the achievable set.
    pfg = lambda x: x - 63.7                           # min-gain power = −63.7 (fractional)
    gfp = lambda p: (p + 63.7) + 1e-7                 # inverse with +float noise
    g = AchievableGrid(pfg, gfp, 0.0, 89.75, 0.25, [])   # NO active components
    assert g.bounds()[0] == pytest.approx(pfg(0.0))   # floor is the min-gain level, not one up
    assert g.realize(pfg(0.0))["sdr_gain_db"] == pytest.approx(0.0)
    assert g.quantize_up(pfg(0.0)) == pytest.approx(pfg(0.25))   # next real gain-grid level


def test_engage_threshold_keeps_sdr_higher_with_odd_steps():
    g = _grid(1.0, 0.3, engage_pct=50.0)              # threshold at −20 dBm
    assert g.bounds()[0] == pytest.approx(-50.0)      # −20 − 30
    r = g.realize(-35.0)
    assert r["sdr_gain_db"] == pytest.approx(20.0)    # SDR pinned at the threshold gain


def test_threshold_gain_is_robust_to_inverse_float_noise():
    # A frequency-dependent baseline can make the SDR floor power invert (gain_for_power) with
    # a hair of float noise; a naive ceil-snap of the threshold gain then overshoots by one
    # step and DROPS the minimum gain — reporting the floor one SDR step too high. The
    # threshold-gain walk must be immune to that.
    pfg = lambda x: x - 64.225                         # min-gain power = −64.225 (fractional)
    gfp = lambda p: (p + 64.225) + 1e-7               # inverse with deliberate +float noise
    g = AchievableGrid(pfg, gfp, 0.0, 89.75, 0.25, [Active(0.0, -95.0, 0.2, 0.0)])
    floor = pfg(0.0) - 95.0                            # min gain + full attenuation
    assert g.bounds()[0] == pytest.approx(floor)      # not one 0.25 dB step too high
    assert g.realize(floor)["sdr_gain_db"] == pytest.approx(0.0)   # gain 0 is usable
