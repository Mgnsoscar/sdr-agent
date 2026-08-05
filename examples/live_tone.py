#!/usr/bin/env python3
"""
Example paramkit script with LIVE parameters (mock — no real hardware).

A long-running "transmitter" whose gain and frequency can be retuned while it
runs, from the GUI's Tune… button (or `POST /tasks/{name}/params`). It doesn't
touch an SDR — it just prints the values it's "transmitting" once a second, and
applies live changes the way a real script would: draining them at the top of
its loop and reporting back the value the "device" actually took (here, gain is
quantised to even dB to mimic a hardware step).

Try it standalone (live tuning is simply inert without a host):

    python3 examples/live_tone.py --freq 100e6 --gain 30

Deploy it as a task and press Tune… on the running task to change --freq/--gain
on the fly.
"""
import os
import sys
import time

# Let the example run straight from the repo without installing paramkit.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paramkit import Script


def build_script() -> Script:
    return (
        Script(
            "Continuously transmit a tone (mock); gain and frequency are live.",
            epilog="Press Tune… on the running task to change --freq/--gain live.",
        )
        .number("-f", "--freq", unit="Hz", min=70e6, max=6e9, default=100e6,
                live=True, help="Center frequency (live).")
        .integer("-g", "--gain", unit="dB", min=0, max=49, default=30,
                 live=True, help="TX gain (live; device steps in 2 dB).")
        .number("-i", "--interval", unit="s", min=0.1, max=10.0, default=1.0,
                help="How often to print the current state.")
    )


def main() -> int:
    script = build_script()
    args = script.parse()
    ctrl = script.live_control(args)          # opens the control socket, if any

    # "Apply" the initial settings to our mock device.
    freq = args.freq
    gain = (int(args.gain) // 2) * 2          # device quantises to even dB
    print(f"── live_tone (mock) ── on air @ {freq/1e6:.3f} MHz, gain {gain} dB")

    try:
        while True:
            # Apply any live changes on THIS thread (device access stays here),
            # and report the value the device actually took so the UI is honest.
            for change in ctrl.drain():
                if change.name == "freq":
                    freq = change.value
                    ctrl.report("freq", freq)
                    print(f"   retuned → {freq/1e6:.3f} MHz")
                elif change.name == "gain":
                    gain = (int(change.value) // 2) * 2
                    ctrl.report("gain", gain)     # e.g. asked 41 → took 40
                    print(f"   gain    → {gain} dB")

            print(f"transmitting @ {freq/1e6:.3f} MHz, gain {gain} dB")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("── live_tone stopped")
        return 0
    finally:
        ctrl.close()


if __name__ == "__main__":
    raise SystemExit(main())
