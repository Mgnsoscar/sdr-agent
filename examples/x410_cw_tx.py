#!/usr/bin/env python3
"""
x410_cw_tx.py — transmit a continuous-wave (CW) test tone on an Ettus/NI X410.

A minimal "does the radio actually transmit?" check: tune one TX channel to a
frequency and emit a steady tone until stopped (Ctrl-C, or SIGTERM when the SDR
agent stops the task). Uses the UHD Python API, so it must run on the X410's
SYSTEM python3 (the one UHD ships with) — NOT the agent's bundled Python:

    /usr/bin/python3 x410_cw_tx.py --freq 2.4e9 --channel 0 --gain 30

Channel maps to the front-panel RF port: --channel 0 = RF0, 1 = RF1, 2 = RF2,
3 = RF3. The emitted signal sits at (freq + tone_offset); with the default
tone_offset=0 that's a pure carrier exactly at --freq. Use a small offset (e.g.
--tone-offset 1e6) to separate the tone from LO/DC leakage on an analyzer.

⚠️  Transmitting is real RF. Feed a cable + attenuator into a spectrum analyzer or
a dummy load — never a live antenna — and stay within what you're licensed to emit.
Start at low gain.
"""
import argparse
import signal
import sys

import numpy as np
import uhd


def parse_args():
    p = argparse.ArgumentParser(description="Transmit a CW test tone on an X410 TX channel.")
    p.add_argument("--freq", type=float, required=True,
                   help="Center (carrier) frequency in Hz, e.g. 2.4e9")
    p.add_argument("--channel", type=int, default=0,
                   help="TX channel = front RF port: 0=RF0, 1=RF1, 2=RF2, 3=RF3 (default 0)")
    p.add_argument("--gain", type=float, default=30.0,
                   help="TX gain in dB (default 30; start low)")
    p.add_argument("--rate", type=float, default=1e6,
                   help="TX sample rate in Sa/s (default 1e6)")
    p.add_argument("--tone-offset", type=float, default=0.0,
                   help="Baseband tone offset in Hz; emitted freq = freq + offset "
                        "(default 0 = pure carrier at --freq)")
    p.add_argument("--amplitude", type=float, default=0.3,
                   help="Tone amplitude, 0..1 of full scale (default 0.3)")
    p.add_argument("--duration", type=float, default=0.0,
                   help="Seconds to transmit, or 0 = until stopped (default 0)")
    p.add_argument("--antenna", type=str, default="",
                   help="TX antenna/port name; leave blank to use the channel default")
    p.add_argument("--args", type=str, default="",
                   help='UHD device args, e.g. "type=x4xx" (default: auto-detect)')
    return p.parse_args()


def main():
    a = parse_args()
    if not 0.0 <= a.amplitude <= 1.0:
        sys.exit("--amplitude must be between 0 and 1")

    print(f"Opening USRP (args={a.args!r}) ...", flush=True)
    usrp = uhd.usrp.MultiUSRP(a.args)
    chan = a.channel

    usrp.set_tx_rate(a.rate, chan)
    usrp.set_tx_freq(uhd.types.TuneRequest(a.freq - a.tone_offset), chan)
    usrp.set_tx_gain(a.gain, chan)
    if a.antenna:
        usrp.set_tx_antenna(a.antenna, chan)

    # Report what actually got set (UHD clamps/rounds to the hardware's real values).
    act_rate = usrp.get_tx_rate(chan)
    act_freq = usrp.get_tx_freq(chan)
    act_gain = usrp.get_tx_gain(chan)
    try:
        ant = usrp.get_tx_antenna(chan)
        avail = usrp.get_tx_antennas(chan)
    except Exception:
        ant, avail = "?", []
    print(f"  channel   : {chan}  (RF{chan})")
    print(f"  LO freq   : {act_freq/1e6:.6f} MHz")
    print(f"  tone      : +{a.tone_offset/1e3:.3f} kHz  ->  emitted ~{(act_freq+a.tone_offset)/1e6:.6f} MHz")
    print(f"  rate      : {act_rate/1e6:.6f} MSa/s")
    print(f"  gain      : {act_gain:.1f} dB")
    print(f"  antenna   : {ant}   available: {avail}")
    print("  (feed a cable+attenuator into an analyzer/dummy load — not an antenna)", flush=True)

    # TX streamer: 32-bit float complex host samples, sc16 on the wire.
    st_args = uhd.usrp.StreamArgs("fc32", "sc16")
    st_args.channels = [chan]
    tx_streamer = usrp.get_tx_stream(st_args)
    spp = tx_streamer.get_max_num_samps()

    md = uhd.types.TXMetadata()
    md.start_of_burst = True
    md.end_of_burst = False
    md.has_time_spec = False

    # Stop cleanly on Ctrl-C (manual test) and SIGTERM (agent "stop task").
    running = {"go": True}
    def _stop(signum, _frame):
        running["go"] = False
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    # Generate the tone with continuous phase across buffers so there's no jump each
    # packet. A running phase accumulator (wrapped to [0, 2pi)) keeps precision over
    # long runs. tone_offset=0 -> inc=0 -> constant buffer = pure carrier at the LO.
    inc = 2.0 * np.pi * a.tone_offset / act_rate
    ramp = np.arange(spp)
    phase = 0.0
    max_samps = int(a.duration * act_rate) if a.duration > 0 else 0
    sent = 0
    print("Transmitting. Ctrl-C to stop." if a.duration <= 0
          else f"Transmitting for {a.duration:g} s.", flush=True)
    try:
        while running["go"] and (max_samps == 0 or sent < max_samps):
            buff = (a.amplitude * np.exp(1j * (phase + inc * ramp))).astype(np.complex64)
            buff = np.ascontiguousarray(buff.reshape(1, spp))
            tx_streamer.send(buff, md)
            md.start_of_burst = False
            phase = (phase + inc * spp) % (2.0 * np.pi)
            sent += spp
    finally:
        # End-of-burst so the DAC ramps down instead of leaving a stuck carrier.
        md.start_of_burst = False
        md.end_of_burst = True
        tx_streamer.send(np.zeros((1, 1), dtype=np.complex64), md)
        print("\nStopped, end-of-burst sent.", flush=True)


if __name__ == "__main__":
    main()
