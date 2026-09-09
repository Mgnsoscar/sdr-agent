"""Tune-step run-log quantity blocks (agent/tune_log.py + RunLog.emit_block).

A calibrated --power tune logs every power quantity it moved (base measured quantity + each
declared law view) as ONE grouped block; a non-power tune logs its value plus any visible
derived readout that tracks it. Each step's whole block is written atomically so two tunes
firing at the same instant never interleave line-by-line.
"""
import math

from agent import tune_log
from agent.sequence_log import RunLog
from agent.log_manager import LogManager


# A self-contained argspec mirroring the GPS C/A mock: a calibrated --power, a sidelobe count
# with a VISIBLE derived passband bandwidth and a HIDDEN enbw the full-power law keys on.
_SPEC = {
    "params": [
        {"dest": "power", "flags": ["-Power", "--power"], "unit": "dBm"},
        {"dest": "sidelobes", "flags": ["-Sidelobes", "--sidelobes"], "unit": ""},
        {"dest": "passband_bw_mhz", "flags": ["-Passband-bandwidth"], "unit": "MHz",
         "formula": {"linear": ["sidelobes", 2.046, 2.046]}},
        {"dest": "enbw_mhz", "flags": ["-Full-power-bandwidth"], "unit": "MHz", "hidden": True,
         "formula": {"table": ["sidelobes", 0.923588, 0.971788, 0.988638, 0.997168,
                               1.002311, 1.005749]}},
        {"dest": "rf", "flags": ["--rf"], "unit": ""},
    ],
    "calibration_power_laws": [
        {"id": "full_power", "name": "Full signal power (filter passband)", "unit": "dBm",
         "in": "density", "out": "abs", "k": 60.0, "param": "enbw_mhz", "coeff": 10.0, "ref": 1.0},
        {"id": "main_lobe_power", "name": "Main-lobe integrated power", "unit": "dBm",
         "in": "density", "out": "abs", "k": 59.654784},
    ],
}
_ART = {"quantity": "spectral density", "operating_unit": "dBm/Hz"}
_EFF = {"power": -99.654784, "sidelobes": 5.0, "rf": "on"}


def test_power_tune_lists_every_quantity():
    txt = tune_log.format_tune_step("mock_prn", {"power": -99.654784}, _EFF, _SPEC, _ART, "07:55:39")
    lines = txt.splitlines()
    # One header, three quantity rows (base + two laws).
    assert lines[0] == "[07:55:39] ◈ mock_prn    •    Power"
    assert "dBm/Hz" in lines[1] and "Spectral density" in lines[1]
    body = "\n".join(lines[1:])
    assert "Main-lobe integrated power" in body and "Full signal power (filter passband)" in body
    # Values are folded from the base at the live operating point.
    assert "-40" in body                                 # main lobe = base + 59.654784
    enbw5 = 1.005749
    full = -99.654784 + 60.0 + 10.0 * math.log10(enbw5)  # ≈ -39.63
    assert f"{full:.4f}".rstrip("0").rstrip(".")[:6] in body


def test_non_power_tune_shows_value_and_derived_readout():
    txt = tune_log.format_tune_step("mock_prn", {"sidelobes": 2},
                                    {**_EFF, "sidelobes": 2.0}, _SPEC, _ART, "07:55:39")
    lines = txt.splitlines()
    assert lines[0] == "[07:55:39] ◈ mock_prn    •    Sidelobes"
    body = "\n".join(lines[1:])
    assert "2" in lines[1]                                # the value itself
    # The visible derived passband bandwidth tracks it: 2.046·2 + 2.046 = 6.138 MHz.
    assert "6.138" in body and "MHz" in body and "Passband bandwidth" in body
    # The HIDDEN enbw is a law-only bridge, never shown as a readout.
    assert "Full-power-bandwidth" not in body and "enbw" not in body


def test_one_step_is_one_grouped_block_even_with_two_params():
    txt = tune_log.format_tune_step("mock_prn", {"power": -99.654784, "sidelobes": 2},
                                    {**_EFF, "sidelobes": 2.0}, _SPEC, _ART, "07:55:39")
    # Both parameters render, each under its own header, all stamped the same instant.
    assert txt.count("◈ mock_prn") == 2
    assert txt.count("[07:55:39]") == 2
    assert "Power" in txt and "Sidelobes" in txt


def test_uncalibrated_power_falls_back_to_the_bare_value():
    txt = tune_log.format_tune_step("mock_prn", {"power": -99.654784}, _EFF, _SPEC, None, "07:55:39")
    # No artifact → no law views; just the header + the raw value (no crash, no guessed quantities).
    assert txt.splitlines()[0].endswith("Power")
    assert "Main-lobe" not in txt and "Full signal" not in txt


def test_empty_change_returns_none():
    assert tune_log.format_tune_step("t", {}, {}, _SPEC, _ART, "00:00:00") is None


def test_simultaneous_tunes_do_not_interleave(tmp_path):
    """Two tune blocks written via RunLog.emit_block land whole, one after the other — never
    line-interleaved (the guarantee the runner relies on for same-instant tunes)."""
    lm = LogManager(tmp_path, "seqrun")
    rl = RunLog(lm, lambda name: None)
    rl.open("test run")
    a = tune_log.format_tune_step("t1", {"power": -99.654784}, _EFF, _SPEC, _ART, "07:55:39")
    b = tune_log.format_tune_step("t2", {"sidelobes": 2}, {**_EFF, "sidelobes": 2.0},
                                  _SPEC, _ART, "07:55:39")
    rl.emit_block(a)
    rl.emit_block(b)
    rl.close()
    text = (lm.current).read_text(encoding="utf-8")
    # Each block appears verbatim and contiguous — b's header comes strictly after all of a.
    assert a in text and b in text
    assert text.index(a) + len(a) <= text.index(b)
