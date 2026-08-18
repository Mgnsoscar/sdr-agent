#!/usr/bin/env python3
"""
Runnable tests for the static paramkit introspection in agent/argspec.py:

    python3 tests/test_argspec_paramkit.py

Confirms the agent can extract paramkit's rich schema (kind/unit/min/max/presets)
from a script's SOURCE without executing it, and still falls back to argparse.
Exits 0 if all pass, 1 otherwise.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.argspec import extract_params, extract_paramkit_spec  # noqa: E402

_failures = []


def check(cond, msg):
    print(("  ok  · " if cond else "  FAIL· ") + msg)
    if not cond:
        _failures.append(msg)


PARAMKIT_SRC = '''
from paramkit import Script

FREQS = {
    "WiFi ch1 (2.412 GHz)": 2.412e9,
    "GPS L1": 1.57542e9,
}

script = (
    Script("demo tx")
    .number("-f", "--freq", unit="Hz", min=70e6, max=6e9, presets=FREQS,
            required=True, help="Center frequency.")
    .number("-g", "--gain", unit="dB", min=0, max=89, default=40)
    .choice("--antenna", options=["TX/RX", "RX2"], default="TX/RX")
    .choice("--otw", options={"sc8": "8-bit (halves USB)", "sc16": "16-bit"}, default="sc8")
    .flag("-v", "--verbose")
)
args = script.parse()
'''

ARGPARSE_SRC = '''
import argparse
p = argparse.ArgumentParser()
p.add_argument("-f", "--freq", type=float, required=True)
p.add_argument("-v", "--verbose", action="store_true")
'''


def main() -> int:
    print("paramkit source → rich schema (no execution):")
    spec = extract_params(PARAMKIT_SRC)
    check(spec.get("format") == "paramkit", "detected as paramkit")
    check(spec.get("description") == "demo tx", "description extracted")
    by = {p["name"]: p for p in spec["params"]}
    check(set(by) == {"freq", "gain", "antenna", "otw", "verbose"}, "all params found")

    f = by["freq"]
    check(f["kind"] == "number", "freq kind=number")
    check(f["unit"] == "Hz", "freq unit=Hz")
    check(f["min"] == 70e6 and f["max"] == 6e9, "freq min/max")
    check(f["required"] is True, "freq required")
    check(f["flags"] == ["-f", "--freq"], "freq flags")
    labels = [p["label"] for p in f["presets"]]
    keys = [p["key"] for p in f["presets"]]
    vals = [p["value"] for p in f["presets"]]
    check("WiFi ch1 (2.412 GHz)" in labels, "preset label resolved from module dict")
    check("wifi_ch1_2_412_ghz" in keys, "preset key slugged")
    check(2.412e9 in vals, "preset value resolved")
    # backward-compatible fields still present
    check(f["dest"] == "freq" and f["type"] == "float" and f["is_flag"] is False,
          "classic argparse fields present on paramkit param")

    g = by["gain"]
    check(g["default"] == 40 and g["unit"] == "dB", "gain default+unit")

    ant = by["antenna"]
    check(ant["kind"] == "choice" and ant["choices"] == ["TX/RX", "RX2"], "choice options")
    check(ant["choice_labels"] is None, "list-choice has no labels")

    otw = by["otw"]
    check(otw["choices"] == ["sc8", "sc16"], "dict-choice values extracted statically")
    check(otw["choice_labels"] == {"sc8": "8-bit (halves USB)", "sc16": "16-bit"},
          "dict-choice labels extracted statically")

    v = by["verbose"]
    check(v["kind"] == "flag" and v["is_flag"] is True and v["default"] is False,
          "flag mapped correctly")

    print("argparse source → classic schema (fallback):")
    spec2 = extract_params(ARGPARSE_SRC)
    check(spec2.get("format") is None, "not tagged paramkit")
    names = {p["dest"] for p in spec2["params"]}
    check(names == {"freq", "verbose"}, "argparse params still extracted")

    print("computed min/max bounds resolve statically:")
    csrc = (
        "from paramkit import Script\n"
        "A, B = 30.0, 61.44\n"          # tuple-unpacked source vars
        "C = 10\n"
        "MAX_VALUE = A + B\n"           # named computed const
        "script = (Script('d')\n"
        "  .number('--x', min=0, max=MAX_VALUE)\n"
        "  .number('--y', min=0, max=A + B)\n"   # inline expression
        "  .integer('--n', min=1, max=C * 2 + 1))\n"
    )
    cby = {p["name"]: p for p in extract_params(csrc)["params"]}
    check(cby["x"]["max"] == 91.44, "named computed const (A+B) resolves in schema")
    check(cby["y"]["max"] == 91.44, "inline A+B resolves in schema")
    check(cby["n"]["max"] == 21, "integer computed bound (C*2+1) resolves in schema")

    print("empty paramkit (no builder calls) falls back gracefully:")
    spec3 = extract_params("import paramkit\n")
    check(spec3.get("params") == [], "no params, no crash")

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S)")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
