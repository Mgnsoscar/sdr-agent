#!/usr/bin/env python3
"""
Linear chirp transmitter for GNU Radio + UHD.

Generates a repeating linear FM chirp sweeping from (center - bw/2) to
(center + bw/2) at a repetition rate of `chirp_freq` Hz.

The sample rate is chosen automatically: oversample x chirp bandwidth is
requested from UHD, and the chirp vector is regenerated using whatever
rate the hardware actually coerces to.

Several whole chirp periods are tiled into one large streaming buffer so
the scheduler emits big chunks per work() call -- this avoids underflows
at high sample rates. Tiling stays phase-continuous because each period
begins and ends at phase 0.

Note on the spectrum: repeating the waveform at `chirp_freq` produces a
comb of spectral lines spaced `chirp_freq` apart under the BW envelope.
Number of visible lines ~= bw / chirp_freq. Lower chirp_freq to pack in
more lines (RBW must be < chirp_freq to resolve them).

Example:
    ./chirp_tx.py -f 2.4e9 -b 30e6 -c 200e3 -g 40
"""

import argparse
import math
import signal
import sys

import numpy as np
from gnuradio import gr, blocks, uhd

import threading


DEFAULT_OVERSAMPLE = 1.25      # fs = oversample * bandwidth
MIN_SAMP_RATE = 200e3          # floor so narrow chirps still get a sane rate
MIN_BUFFER_SAMPS = 1 << 24     # ~1M samples: enough to avoid scheduler thrash


def make_chirp_period(samp_rate: float, bw: float, chirp_freq: float,
                      amplitude: float = 0.7) -> np.ndarray:
    """One period of a baseband linear chirp, -bw/2 -> +bw/2.

    Phase is exactly 0 at both ends of the period, so tiling / looping
    the vector is phase-continuous.
    """
    n = int(round(samp_rate / chirp_freq))
    if n < 2:
        raise ValueError(
            f"chirp_freq {chirp_freq:g} Hz too high for sample rate "
            f"{samp_rate:g} S/s ({n} samples/period)"
        )
    t = np.arange(n) / samp_rate
    period = n / samp_rate
    k = bw / period  # sweep rate [Hz/s]
    phase = 2.0 * np.pi * (-0.5 * bw * t + 0.5 * k * t * t)
    return (amplitude * np.exp(1j * phase)).astype(np.complex64)


def build_buffer(samp_rate: float, cf: float,  bw: float, chirp_freq: float,
                 amplitude: float, randomization: float = 0):
    """Tile whole periods up to >= MIN_BUFFER_SAMPS. Returns (buffer, n, reps)."""
    period = make_chirp_period(samp_rate, bw, chirp_freq, amplitude)
    n = len(period)
    reps = max(1, math.ceil(MIN_BUFFER_SAMPS / n))
    tiled = np.tile(period, reps)

    # Random-walk phase drift across the entire buffer
    phase_noise = np.cumsum(
        np.random.normal(0, randomization, len(tiled))
    ).astype(np.float32)

    x_shifted = tiled * np.exp(1j * phase_noise)

    return x_shifted, n, reps

class ChirpTx(gr.top_block):
    def __init__(self, center_freq: float, bw: float, chirp_freq: float,
                 gain: float, amplitude: float = 0.7, oversample: float = 1.25,
                 device_args: str = "", randomization: float = 0):

        super().__init__("Linear chirp TX")

        requested_rate = max(oversample * bw, MIN_SAMP_RATE)

        self.usrp = uhd.usrp_sink(
            device_args,
            uhd.stream_args(cpu_format="fc32", channels=[0]),
        )
        self.usrp.set_samp_rate(requested_rate)
        actual_rate = self.usrp.get_samp_rate()  # coerced by UHD

        self.usrp.set_center_freq(center_freq, 0)
        self.usrp.set_gain(gain, 0)


        buf, n, reps = build_buffer(actual_rate, center_freq, bw, chirp_freq, amplitude, randomization)
        self.src = blocks.vector_source_c(buf.tolist(), repeat=True)

        self.connect(self.src, self.usrp)

        line_spacing = actual_rate / n  # true repetition rate after integerizing
        est_lines = max(1, int(round(bw / line_spacing)))

        print(f"Center frequency : {center_freq / 1e6:.3f} MHz")
        print(f"Chirp bandwidth  : {bw / 1e6:.3f} MHz")
        print(f"Chirp rate       : {line_spacing / 1e3:.3f} kHz "
              f"({n} samples/period)")
        print(f"Sample rate      : requested {requested_rate / 1e6:.3f} MHz, "
              f"got {actual_rate / 1e6:.3f} MHz")
        print(f"Stream buffer    : {reps} periods = {len(buf)} samples")
        print(f"Spectral lines   : ~{est_lines} across the band "
              f"(spacing {line_spacing / 1e3:.1f} kHz; use RBW < that to resolve)")
        print(f"TX gain          : {gain:g} dB")


def main():

    p = argparse.ArgumentParser(description="Linear chirp transmitter")
    p.add_argument("-Center-Freq", "--center-freq", type=float, required=True,
                   help="RF center frequency [Hz]")
    p.add_argument("-Sweep-BW", "--bandwidth", type=float, required=True,
                   help="Chirp sweep bandwidth [Hz]")
    p.add_argument("-Sweep-Freq", "--chirp-freq", type=float, required=True,
                   help="Chirp repetition frequency [Hz] "
                        "(lower = more, closer spectral lines)")
    p.add_argument("-Gain", "--gain", type=float, default=30.0,
                   help="USRP TX gain [dB] (default 30)")
    p.add_argument("-Vector-Amplitude", "--amplitude", type=float, default=0.7,
                   help="Baseband amplitude 0-1 (default 0.7)")
    p.add_argument("--Oversample", type=float, default=DEFAULT_OVERSAMPLE,
                   help=f"fs = oversample * bandwidth (default {DEFAULT_OVERSAMPLE}). "
                        "Lower toward 1.0 if a very wide chirp still underflows.")

    p.add_argument("--args", type=str, default="",
                   help='UHD device args, e.g. "serial=1234" or '
                        '"num_send_frames=256" to enlarge the USB send buffer')
    p.add_argument("-Phase-Randomization", "--randomization", type=float, default=0.0,
                   help="Phase random-walk stddev per sample (0 = clean chirp). Setting this to 0.05 will produce a "
                        "a reasonably uniform noise that's not combed.")
    opts = p.parse_args()

    tb = ChirpTx(opts.center_freq, opts.bandwidth, opts.chirp_freq,
                 opts.gain, opts.amplitude, opts.Oversample, opts.args,
                 opts.randomization)

    def stop(sig, frame):
        print("\nStopping...")
        tb.stop()
        tb.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    tb.start()
    print("Transmitting. Ctrl-C to stop.")
    tb.wait()

if __name__ == "__main__":
    main()