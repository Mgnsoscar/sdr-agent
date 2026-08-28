#!/usr/bin/env python3
"""
Runnable tests for paramkit (no pytest needed):

    python3 tests/test_paramkit.py

Exits 0 if all pass, 1 otherwise.
"""
import contextlib
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paramkit import Preset, Script, slug  # noqa: E402

_failures = []


def check(cond, msg):
    if cond:
        print(f"  ok  · {msg}")
    else:
        print(f"  FAIL· {msg}")
        _failures.append(msg)


def expect_exit(fn, msg):
    """A bad CLI value should make argparse exit(2). Confirm it does."""
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            fn()
    except SystemExit as e:
        check(e.code != 0, f"{msg} (rejected: {err.getvalue().strip().splitlines()[-1:]})")
        return
    check(False, f"{msg} (was NOT rejected)")


def sample() -> Script:
    return (
        Script("demo")
        .number("-f", "--freq", unit="Hz", min=70e6, max=6e9,
                presets={"WiFi ch1 (2.412 GHz)": 2.412e9, "GPS L1": 1.57542e9},
                required=True)
        .number("-g", "--gain", unit="dB", min=0, max=89, default=40)
        .integer("-n", "--count", min=1, max=10, default=1)
        .choice("--mode", options=["chirp", "tone"], default="chirp")
        .flag("-v", "--verbose")
    )


def main() -> int:
    print("preset resolution:")
    s = sample()
    a = s.parse(["--freq", "wifi_ch1_2_412_ghz", "-g", "30"])
    check(a.freq == 2.412e9, "preset key resolves to value")
    check(a.gain == 30.0, "raw gain parses")
    a = s.parse(["--freq", "WiFi ch1 (2.412 GHz)"])
    check(a.freq == 2.412e9, "preset label (exact) resolves to value")
    a = s.parse(["--freq", "1.9e9"])
    check(a.freq == 1.9e9, "raw frequency parses when not a preset")

    print("defaults & flag:")
    a = s.parse(["-f", "wifi_ch1_2_412_ghz"])
    check(a.gain == 40.0 and a.count == 1 and a.mode == "chirp", "defaults applied")
    check(a.verbose is False, "flag defaults False")
    a = s.parse(["-f", "2.4e9", "-v"])
    check(a.verbose is True, "flag set True when present")
    check(not hasattr(a, "describe_params"), "describe_params stripped from namespace")

    print("range enforcement:")
    expect_exit(lambda: sample().parse(["-f", "50e6"]), "below-min frequency")
    expect_exit(lambda: sample().parse(["-f", "7e9"]), "above-max frequency")
    expect_exit(lambda: sample().parse(["-f", "2.4e9", "-g", "200"]), "above-max gain")
    expect_exit(lambda: sample().parse(["-f", "2.4e9", "-n", "0"]), "below-min integer")

    print("choice & required:")
    expect_exit(lambda: sample().parse(["-f", "2.4e9", "--mode", "bogus"]), "invalid choice")
    expect_exit(lambda: sample().parse(["-g", "30"]), "missing required --freq")
    expect_exit(lambda: sample().parse(["-f", "not_a_number"]), "unparseable frequency")

    print("integer bases:")
    a = sample().parse(["-f", "2.4e9", "-n", "0x0a"])
    check(a.count == 10, "integer accepts hex (base 0)")

    print("schema (describe):")
    schema = sample().describe()
    freq = next(p for p in schema["params"] if p["name"] == "freq")
    check(freq["kind"] == "number", "freq kind=number")
    check(freq["unit"] == "Hz", "freq unit=Hz")
    check(freq["min"] == 70e6 and freq["max"] == 6e9, "freq range in schema")
    check(freq["required"] is True, "freq required in schema")
    labels = [p["label"] for p in freq["presets"]]
    check("WiFi ch1 (2.412 GHz)" in labels, "preset label present in schema")
    keys = [p["key"] for p in freq["presets"]]
    check("wifi_ch1_2_412_ghz" in keys, "preset key slugged in schema")
    mode = next(p for p in schema["params"] if p["name"] == "mode")
    check(mode["choices"] == ["chirp", "tone"], "choice options in schema")
    check(json.loads(sample().to_json())["description"] == "demo", "to_json round-trips")

    print("--describe-params CLI mode:")
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            sample().parse(["--describe-params"])
    except SystemExit as e:
        check(e.code == 0, "--describe-params exits 0")
    doc = json.loads(out.getvalue())
    check(doc["params"][0]["name"] == "freq", "--describe-params prints schema JSON")

    print("labelled choices ({label: value} mapping, like presets):")
    sc = (Script("y")
          .choice("--freq", options={"1575.42 MHz": 1.57542e9, "1227.6 MHz": 1.2276e9},
                  unit="Hz", default=1.57542e9)
          .choice("--otw", options={"8-bit (halves USB)": "sc8", "16-bit": "sc16"},
                  default="sc8")
          .choice("--band", options=["L1", "L2"], default="L1"))  # plain list, unchanged
    schema = {p["name"]: p for p in sc.describe()["params"]}
    check(schema["freq"]["choices"] == ["1575420000.0", "1227600000.0"],
          "dict-choice value tokens become choices")
    check(schema["freq"]["choice_labels"] == {"1575420000.0": "1575.42 MHz",
                                              "1227600000.0": "1227.6 MHz"},
          "dict-choice labels captured in schema")
    check(schema["freq"]["choice_values"] == {"1575420000.0": 1.57542e9,
                                              "1227600000.0": 1.2276e9},
          "dict-choice typed values captured in schema")
    check(schema["freq"]["unit"] == "Hz", "choice supports a unit")
    check(schema["band"]["choice_labels"] is None, "list-choice keeps choice_labels=None")
    check(schema["band"]["choice_values"] is None, "list-choice keeps choice_values=None")
    # The script receives the VALUE in its real type — selectable by token or by label.
    a = sc.parse(["--freq", "1575420000.0"]); check(a.freq == 1.57542e9, "CLI token → typed value")
    a = sc.parse(["--freq", "1227.6 MHz"]); check(a.freq == 1.2276e9, "CLI label → typed value")
    a = sc.parse(["--otw", "sc16"]); check(a.otw == "sc16", "string-valued choice by value")
    a = sc.parse(["--otw", "16-bit"]); check(a.otw == "sc16", "string-valued choice by label")
    a = sc.parse([]); check(a.freq == 1.57542e9, "choice default resolves to the typed value")
    expect_exit(lambda: sc.parse(["--freq", "bogus"]), "invalid choice rejected")
    try:
        Script("z").choice("--w", options={"A": "a", "B": "b"}, default="Q")
        check(False, "bad default (not a value/label) should raise")
    except ValueError:
        check(True, "dict-choice default still validated")

    print("presets as list of Preset / tuples:")
    s2 = Script("x").number("--f", presets=[Preset("a", "Alpha", 1.0), ("Beta", 2.0)])
    a = s2.parse(["--f", "a"]); check(a.f == 1.0, "Preset object key resolves")
    a = s2.parse(["--f", "beta"]); check(a.f == 2.0, "tuple preset label resolves")

    print("definition-time guards:")
    try:
        Script("x").number("--f", min=0, max=10, presets={"too big": 99})
        check(False, "preset out of range should raise")
    except ValueError:
        check(True, "preset out of declared range rejected at definition")
    try:
        Script("x").number("-a", name="dup").number("-b", name="dup")
        check(False, "duplicate name should raise")
    except ValueError:
        check(True, "duplicate parameter name rejected")

    print("slug:")
    check(slug("WiFi ch1 (2.4 GHz)") == "wifi_ch1_2_4_ghz", "slug normalises label")

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S)")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
