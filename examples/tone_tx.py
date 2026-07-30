#!/usr/bin/env python3
"""
Example paramkit script: a single-tone transmitter (mock).

This shows the frequency-with-named-presets pattern the GUI can turn into a
dropdown. It doesn't touch real hardware — it just resolves and prints the
parameters, so you can run it anywhere to see paramkit in action:

    python3 examples/tone_tx.py --freq wifi_ch1 --gain 40
    python3 examples/tone_tx.py -f 2.412e9 -g 40 --duration 5
    python3 examples/tone_tx.py --describe-params     # JSON schema for a GUI
    python3 examples/tone_tx.py --help

Note how --freq accepts EITHER a preset key (wifi_ch1) OR a raw number (2.412e9),
and how out-of-range values are rejected with a clear message.
"""
import os
import sys

# Let the example run straight from the repo without installing paramkit.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paramkit import Script


# Named frequencies: the label is what a GUI shows in its dropdown; the value is
# what the script actually receives. Operators can still pass any raw frequency.
FREQUENCIES = {
    "WiFi ch1 (2.412 GHz)": 2.412e9,
    "WiFi ch6 (2.437 GHz)": 2.437e9,
    "ISM 2.4 GHz":          2.4e9,
    "ISM 915 MHz":          915e6,
    "GPS L1 (1.575 GHz)":   1.57542e9,
}


def build_script() -> Script:
    return (
        Script(
            "Transmit a single tone at a chosen frequency (mock — prints only).",
            epilog="Frequencies can be a preset key or a raw value in Hz.",
        )
        .number("-f", "--freq", unit="Hz", min=70e6, max=6e9,
                presets=FREQUENCIES, required=True,
                help="Center frequency.")
        .number("-g", "--gain", unit="dB", min=0, max=89, default=40,
                help="TX gain.")
        .number("-d", "--duration", unit="s", min=0.0, max=3600.0, default=10.0,
                help="How long to transmit.")
        .choice("--antenna", options=["TX/RX", "RX2"], default="TX/RX",
                help="Which antenna port to use.")
        .flag("-v", "--verbose", help="Print extra detail.")
    )


def main() -> int:
    script = build_script()
    args = script.parse()

    print("── tone_tx (mock) ─────────────────────────────")
    print(f"  frequency : {args.freq:,.0f} Hz")
    print(f"  gain      : {args.gain:g} dB")
    print(f"  duration  : {args.duration:g} s")
    print(f"  antenna   : {args.antenna}")
    if args.verbose:
        print(f"  (verbose) : would key the radio for {args.duration:g}s now")
    print("───────────────────────────────────────────────")
    return 0


if __name__ == "__main__":
    sys.exit(main())
