"""Tabular run-log reconstruction (agent/run_table.py).

One row per state CHANGE (a no-op tune adds none); every power quantity, the realized SDR gain
and attenuation, each live parameter with its derived readout, and the fixed parameters as
constant columns. RF renders 0/1.
"""
from types import SimpleNamespace

from agent import run_table


_SPEC = {
    "params": [
        {"dest": "power", "flags": ["-Power", "--power"], "unit": "dBm", "live": True},
        {"dest": "gain", "flags": ["-Gain", "--gain"], "unit": "dB", "live": True},
        {"dest": "freq", "flags": ["-Center-frequency", "--freq"], "unit": "MHz", "live": False},
        {"dest": "prn", "flags": ["-PRN", "--prn"], "unit": "", "live": False},
        {"dest": "sidelobes", "flags": ["-Sidelobes", "--sidelobes"], "unit": "", "live": True},
        {"dest": "passband_bw_mhz", "flags": ["-Passband-bandwidth"], "unit": "MHz", "live": False,
         "formula": {"linear": ["sidelobes", 2.046, 2.046]}},
        {"dest": "enbw_mhz", "flags": ["-Full-power-bandwidth"], "unit": "MHz", "hidden": True,
         "live": False, "formula": {"table": ["sidelobes", 0.923588, 0.971788, 0.988638,
                                              0.997168, 1.002311, 1.005749]}},
        {"dest": "rf", "flags": ["-RF", "--rf"], "unit": "", "live": True},
    ],
    "calibration_power_laws": [
        {"id": "full_power", "name": "Full signal power (filter passband)", "unit": "dBm",
         "in": "density", "out": "abs", "k": 60.0, "param": "enbw_mhz", "coeff": 10.0, "ref": 1.0},
        {"id": "main_lobe_power", "name": "Main-lobe integrated power", "unit": "dBm",
         "in": "density", "out": "abs", "k": 59.654784},
    ],
}
_ART = {"quantity": "spectral density", "operating_unit": "dBm/Hz",
        "active_components": [{"plane": "atten"}]}


def _realize(power, freq=None):
    return {"sdr_gain_db": 80.25, "atten_db": 29.0}


def _step(action, fired, args=None, params=None, task="mock_prn"):
    return SimpleNamespace(action=action, task_name=task, args=args or [],
                           params=params or {}, fired_actual=fired)


def _launch():
    return _step("start", "2026-09-09T09:00:00+00:00",
                 args=["--prn", "1", "--freq", "1575.42", "--sidelobes", "2",
                       "--power", "-99.654784", "--rf", "off"])


def test_columns_include_quantities_realized_and_fixed_params():
    t = run_table.build_task_table("mock_prn", [_launch()], _SPEC, _ART, _realize)
    cols = t["columns"]
    assert cols[0] == "Time"
    # power quantities (declared order: base, full, main), then realized gain/atten
    assert "Spectral density [dBm/Hz]" in cols
    assert "Full signal power (filter passband) [dBm]" in cols
    assert "Main-lobe integrated power [dBm]" in cols
    assert "SDR gain [dB]" in cols and "Attenuation [dB]" in cols
    # live params + derived, then fixed params trailing
    assert "Sidelobes" in cols and "Passband bandwidth [MHz]" in cols and "RF on" in cols
    assert "Center frequency [MHz]" in cols and "PRN" in cols
    assert cols.index("Sidelobes") < cols.index("Center frequency [MHz]")   # live before fixed
    # --power and --gain never appear as their own columns (covered by quantities / SDR gain)
    assert "power" not in cols and "gain" not in cols and "Gain [dB]" not in cols


def test_row_per_change_and_no_op_tune_is_dropped():
    steps = [
        _launch(),
        _step("tune", "2026-09-09T09:00:02+00:00", params={"rf": "on"}),        # change
        _step("tune", "2026-09-09T09:00:04+00:00", params={"sidelobes": 2}),    # no-op
        _step("tune", "2026-09-09T09:00:06+00:00", params={"sidelobes": 3}),    # change
    ]
    t = run_table.build_task_table("mock_prn", steps, _SPEC, _ART, _realize)
    assert len(t["rows"]) == 3                                                  # the no-op dropped one
    cols = t["columns"]
    ci = {c: i for i, c in enumerate(cols)}
    rf, sl = ci["RF on"], ci["Sidelobes"]
    full = ci["Full signal power (filter passband) [dBm]"]
    # Time is HH:MM:SS.mmm — millisecond precision so two sub-second-apart ramp fires
    # never collapse to one displayed timestamp.
    assert [r[ci["Time"]] for r in t["rows"]] == ["09:00:00.000", "09:00:02.000", "09:00:06.000"]
    assert [r[rf] for r in t["rows"]] == [0, 1, 1]                              # RF 0/1
    assert [r[sl] for r in t["rows"]] == [2, 2, 3]
    # full-signal power tracks the sidelobe count (enbw), so it changes on the last row only
    assert t["rows"][0][full] == t["rows"][1][full]
    assert t["rows"][2][full] != t["rows"][1][full]
    # passband tracks sidelobes: 2 → 6.138, 3 → 8.184
    pb = ci["Passband bandwidth [MHz]"]
    assert t["rows"][0][pb] == 6.138 and t["rows"][2][pb] == 8.184
    # fixed columns are constant
    assert all(r[ci["PRN"]] == 1 and r[ci["Center frequency [MHz]"]] == 1575.42 for r in t["rows"])


def test_subsecond_fires_get_distinct_timestamps():
    # Two ramp tunes < 1 s apart that straddle a second boundary must NOT collapse to one
    # displayed timestamp (the fast-forward report: -120 @ ...43.974 and -160 @ ...44.796
    # both printed "10:39:..." truncated to the second). Millisecond precision keeps them
    # distinct AND ordered.
    steps = [
        _launch(),
        _step("tune", "2026-09-09T09:00:43.974602+00:00", params={"power": -120.0}),
        _step("tune", "2026-09-09T09:00:44.796660+00:00", params={"power": -160.0}),
    ]
    t = run_table.build_task_table("mock_prn", steps, _SPEC, _ART, _realize)
    ci = {c: i for i, c in enumerate(t["columns"])}
    times = [r[ci["Time"]] for r in t["rows"]]
    assert times == ["09:00:00.000", "09:00:43.974", "09:00:44.796"]
    assert len(set(times)) == len(times)              # all distinct — no shared timestamp


def test_uncalibrated_run_has_no_quantity_or_realized_columns():
    t = run_table.build_task_table("mock_prn", [_launch()], _SPEC, artifact=None, realize=None)
    cols = t["columns"]
    assert not any("dBm" in c for c in cols)          # no power quantities
    assert "SDR gain [dB]" not in cols and "Attenuation [dB]" not in cols
    # parameters still tabulate
    assert "Sidelobes" in cols and "RF on" in cols and "PRN" in cols
