#!/usr/bin/env python3
"""
Runnable tests for scripts/gps_prn_tx.py (no pytest, no NumPy, no GNU Radio):

    python3 tests/test_gps_prn.py

Covers the two pure-logic pieces that must be correct: the GPS C/A Gold-code
generator (against the ICD reference) and the seamless-loop buffer sizing. The
GNU Radio flowgraph itself needs hardware and isn't unit-tested here.

Exits 0 if all pass, 1 otherwise.
"""
import importlib.util
import os
import sys
from fractions import Fraction

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Load the script as a module without executing main() (heavy imports in it are
# lazy, so this stays NumPy/GNU-Radio-free).
_spec = importlib.util.spec_from_file_location(
    "gps_prn_tx", os.path.join(ROOT, "scripts", "gps_prn_tx.py"))
gps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gps)

_failures = []


def check(cond, msg):
    print(f"  {'ok  ' if cond else 'FAIL'}· {msg}")
    if not cond:
        _failures.append(msg)


# ── Gold code correctness (all 32 PRNs vs ICD first-10-chips) ──────────────────
check(gps._self_test() == 0, "C/A Gold codes match ICD reference for all 32 PRNs")

# Distinct PRNs give distinct codes.
codes = {prn: tuple(gps.ca_code(prn)) for prn in range(1, 33)}
check(len(set(codes.values())) == 32, "all 32 PRN codes are distinct")

# Length and balance for a couple of PRNs.
for prn in (1, 7, 19, 32):
    c = gps.ca_code(prn)
    check(len(c) == 1023 and sum(c) == 512,
          f"PRN {prn}: length 1023, 512 ones / 511 zeros")

# Out-of-range PRN is rejected.
try:
    gps.ca_code(0)
    check(False, "PRN 0 rejected")
except ValueError:
    check(True, "PRN 0 rejected")


# ── Seamless-loop sizing (mirrors build_iq_buffer's Fraction math) ─────────────
def sizing(chip_mcps, samp_mhz):
    sr = int(round(samp_mhz * 1e6))
    cr = int(round(chip_mcps * 1e6))
    spp = Fraction(sr * gps.CODE_LEN, cr)
    return spp.numerator, spp.denominator   # (n_samples, n_periods)


n1, p1 = sizing(1.023, 40.0)
check((n1, p1) == (40000, 1), f"1.023 Mcps @ 40 MHz → 40000 samples / 1 period (got {n1}/{p1})")
n10, p10 = sizing(10.23, 40.0)
check((n10, p10) == (4000, 1), f"10.23 Mcps @ 40 MHz → 4000 samples / 1 period (got {n10}/{p10})")

# For any rate, the buffer must span EXACTLY n_periods whole code periods and be
# an integer number of samples: n_samples · chip_rate == n_periods · 1023 · samp_rate.
# (samples-per-period is generally fractional, so n_samples need not divide by n_periods.)
for cr in (1.023, 10.23, 7.0, 5.0, 2.5):
    n, p = sizing(cr, 40.0)
    cr_hz, sr_hz = int(round(cr * 1e6)), int(40e6)
    check(n * cr_hz == p * gps.CODE_LEN * sr_hz,
          f"{cr} Mcps @ 40 MHz → exactly {p} whole code period(s) in {n} samples")

# Both selectable presets are present in the constant the live-swap relies on.
check(set(round(v, 6) for v in gps.CODE_RATES_MCPS.values()) == {1.023, 10.23},
      "CODE_RATES_MCPS holds exactly the two selectable rates")


print("\nALL PASSED" if not _failures else f"\n{len(_failures)} FAILURE(S)")
sys.exit(1 if _failures else 0)
