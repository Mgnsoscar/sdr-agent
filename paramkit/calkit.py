"""calkit — the transmit script's power-calibration consumer.

The sdr-agent resolves each unit's measured calibration and injects a flat artifact
at ``$SDR_CALIBRATION_FILE`` (see the agent's docs/calibration.md and
docs/calibration-v2.md). This module turns that artifact into a :class:`PowerMap`:
the same ``--power`` (dBm) ↔ commanded-gain mapping the scripts already use, backed by
the unit's MEASURED curve instead of a single baked anchor.

Two artifact shapes, one consumer:

  - **v1 (constant chain):** a pre-flattened ``curve`` (gain→power at the operating
    plane, derived hops already folded). Frequency is irrelevant. Behaviour is exactly
    as before.
  - **v2 (frequency-aware passive chain):** an ``anchor_curve`` (the operating plane's
    measured anchor) plus ``passive_hops`` — the cable/antenna as ``delta_db``-vs-
    frequency tables — and a frequency-split ceiling. The script hands
    :meth:`gain_for_power` / :meth:`power_for_gain` its **current transmit frequency**;
    the passive hops are folded at that frequency, so ``--power`` stays accurate as the
    radio retunes (a chirp, a live-tuned centre). A script declares which of its params
    is the frequency via ``CAL_FREQ_PARAM`` and passes that value in.

When no artifact is present — the unit/signal isn't calibrated, or you're running
off-unit — :meth:`PowerMap.load` returns a map built from the script's baked
constants, byte-identical to the previous behaviour.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

CALIBRATION_FILE_ENV = "SDR_CALIBRATION_FILE"

# Amplitudes are authored numbers in [0, 1]; treat anything within this of the script's
# fixed amplitude as "the same amplitude" (guards float noise, not a real difference).
AMPLITUDE_MATCH_TOL = 1e-6

log = logging.getLogger("calkit")


class NoAbsoluteScale(Exception):
    """Raised when a caller asks for an absolute-power (dBm) conversion but the signal is
    not calibrated on this unit — there is no baked dBm fallback. A script maps --power only
    on a real measured curve; uncalibrated it runs on a relative gain, never on invented
    power levels."""


def _interp(x: float, xs: list, ys: list) -> float:
    """Piecewise-linear y(x) over strictly-increasing xs, endpoint-clamped. A single
    sample degrades to a slope-1 line through it (1 dB gain ≈ 1 dB power) — the same
    single-point fallback the agent resolver uses."""
    n = len(xs)
    if n == 1:
        return ys[0] + (x - xs[0])
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(1, n):
        if x <= xs[i]:
            x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return ys[-1]


def _table_at(freqs: list, deltas: list, freq: Optional[float]) -> float:
    """A delta table's value at ``freq``: constant if single-point; if the frequency is
    unknown for a multi-point table, fall back to its lowest-frequency value."""
    if len(freqs) == 1:
        return deltas[0]
    if freq is None:
        return deltas[0]
    return _interp(freq, freqs, deltas)


class PowerMap:
    """Maps a requested delivered/radiated power (dBm) to a commanded SDR gain (dB) and
    back, over a monotonic measured anchor curve, with any frequency-dependent passive
    hops (cable, antenna) folded at the transmit frequency, clamped to the unit's gain
    limits (the ceiling itself may tighten with frequency)."""

    def __init__(self, gains, powers, min_gain_db, ceiling_const, amplitude,
                 source, label, hops=(), freq_limits=(), center_freq=None):
        if not gains or len(gains) != len(powers):
            raise ValueError("calibration curve is empty or malformed")
        # keep gain-sorted; agent guarantees strict monotonicity, verify defensively
        pairs = sorted(zip((float(g) for g in gains), (float(p) for p in powers)))
        self._gains = [g for g, _ in pairs]
        self._powers = [p for _, p in pairs]
        for i in range(1, len(pairs)):
            if self._gains[i] <= self._gains[i - 1] or self._powers[i] <= self._powers[i - 1]:
                raise ValueError("calibration curve is not strictly monotonic "
                                 "(not invertible)")
        self.min_gain_db = float(min_gain_db)
        self._ceiling_const = float(ceiling_const)
        self.amplitude = float(amplitude)
        self.source = source                 # human tag: where the map came from
        self.label = label                   # what --power means (quantity, at plane)
        self.warning = None                  # set when a calibration was rejected (see load)
        self.has_absolute = True             # a real dBm↔gain scale (False = uncalibrated)
        # v2 frequency machinery ((freqs, deltas) per passive hop; per-limit tables).
        self._hops = [([float(f) for f, _ in t], [float(d) for _, d in t]) for t in hops]
        self._freq_limits = [
            (float(mx), [float(f) for f, _ in t], [float(d) for _, d in t])
            for mx, t in freq_limits]
        self._center_freq = None if center_freq is None else float(center_freq)

    # ── frequency-dependent internals ────────────────────────────────────────────
    def _eff(self, freq: Optional[float]) -> Optional[float]:
        return freq if freq is not None else self._center_freq

    def _op_delta(self, freq: Optional[float]) -> float:
        """Total passive delta (cable + antenna …) at ``freq`` — 0 for a v1 curve."""
        return sum(_table_at(fs, ds, freq) for fs, ds in self._hops)

    def _invert(self, target_power: float) -> float:
        """Anchor gain that yields ``target_power`` at the measured anchor, clamped to
        the measured range (up to the top gain, never extrapolated)."""
        return _interp(target_power, self._powers, self._gains)

    def _ceiling(self, freq: Optional[float]) -> float:
        """Gain ceiling at ``freq``: the frequency-independent cap tightened by any
        frequency-dependent limit (inverted through the shared anchor). Tightest wins."""
        cap = self._ceiling_const
        for max_dbm, fs, ds in self._freq_limits:
            cap = min(cap, self._invert(max_dbm - _table_at(fs, ds, freq)))
        return cap

    # ── the two functions the script calls ──────────────────────────────────────
    def gain_for_power(self, delivered_dbm: float, freq: Optional[float] = None) -> float:
        """Commanded gain (dB) for a requested power at ``freq`` (defaults to the
        artifact's representative frequency), clamped to [min, ceiling(freq)]. Upward is
        clamped to the ceiling, never extrapolated past it."""
        if not self.has_absolute:
            raise NoAbsoluteScale(
                "this signal is not calibrated on this unit — absolute --power (dBm) has "
                "no meaning here; provide --gain (raw dB) instead")
        f = self._eff(freq)
        g = self._invert(float(delivered_dbm) - self._op_delta(f))
        return min(max(g, self.min_gain_db), self._ceiling(f))

    def power_for_gain(self, gain_db: float, freq: Optional[float] = None) -> float:
        """Delivered power (dBm) at the operating plane for an (actual) gain at ``freq``."""
        if not self.has_absolute:
            raise NoAbsoluteScale("uncalibrated: no absolute power scale for this signal")
        f = self._eff(freq)
        g = min(max(float(gain_db), self.min_gain_db), self._ceiling(f))
        return _interp(g, self._gains, self._powers) + self._op_delta(f)

    @property
    def max_gain_db(self) -> float:
        return self._ceiling(self._center_freq)

    @property
    def max_power_dbm(self):
        """Top of the delivered-power range, or None when uncalibrated (no dBm scale)."""
        return self.power_for_gain(self.max_gain_db) if self.has_absolute else None

    @property
    def min_power_dbm(self):
        return self.power_for_gain(self.min_gain_db) if self.has_absolute else None

    def power_field_kwargs(self) -> dict:
        """paramkit ``.number()`` bounds for a script's --power field: min/max/default from the
        resolved calibration range, or ``{}`` (unbounded, no default) when uncalibrated — so an
        uncalibrated script offers no baked dBm scale, only a relative --gain."""
        if not self.has_absolute:
            return {}
        return {"min": round(self.min_power_dbm, 2), "max": round(self.max_power_dbm, 2),
                "default": round(self.max_power_dbm, 2)}

    @property
    def freq_dependent(self) -> bool:
        """True when --power ↔ gain (or the ceiling) actually moves with frequency, so a
        caller knows to pass its live frequency."""
        return (any(len(fs) > 1 for fs, _ in self._hops)
                or any(len(fs) > 1 for _, fs, _ in self._freq_limits))

    # ── constructors ────────────────────────────────────────────────────────────
    @classmethod
    def from_linear(cls, min_gain_db, max_gain_db, min_power_dbm, max_power_dbm,
                    amplitude, label="SDR port (uncalibrated)") -> "PowerMap":
        """Baked fallback: a straight line between (min_gain, min_power) and
        (max_gain, max_power). With the scripts' constants this is exactly the old
        slope-1 anchor model, so behaviour is unchanged when uncalibrated."""
        return cls([min_gain_db, max_gain_db], [min_power_dbm, max_power_dbm],
                   min_gain_db, max_gain_db, amplitude,
                   source="baked defaults", label=label)

    @classmethod
    def uncalibrated(cls, min_gain_db, max_gain_db, amplitude) -> "PowerMap":
        """A gain-only map for when NO calibration is injected: it carries the gain limits
        (so a relative gain still clamps to a safe range) but has NO absolute dBm scale.
        ``gain_for_power`` / ``power_for_gain`` refuse (:class:`NoAbsoluteScale`), the power
        range is None, and ``power_field_kwargs`` is empty. Replaces the old baked slope-1
        fallback — an uncalibrated script never invents dBm levels."""
        self = cls.__new__(cls)
        self._gains = [float(min_gain_db), float(max_gain_db)]
        self._powers = []
        self.min_gain_db = float(min_gain_db)
        self._ceiling_const = float(max_gain_db)
        self.amplitude = float(amplitude)
        self.source = "uncalibrated"
        self.label = "raw gain only (no calibration)"
        self.warning = None
        self._hops = []
        self._freq_limits = []
        self._center_freq = None
        self.has_absolute = False
        return self

    @classmethod
    def from_artifact(cls, art: dict, fallback_amplitude: float) -> "PowerMap":
        """Build from the agent's resolved artifact dict — v2 (anchor_curve +
        passive_hops) when present, else the v1 flat curve."""
        amp = art.get("amplitude")
        amp = fallback_amplitude if amp is None else amp
        plane = art.get("operating_plane", "")
        quantity = art.get("quantity") or "power"
        label = f"{quantity}, at {plane}" if plane else quantity

        anchor = art.get("anchor_curve")
        if anchor:                                    # v2: fold passive hops at frequency
            gains = [pt[0] for pt in anchor]
            powers = [pt[1] for pt in anchor]
            hops = [h.get("delta_db_by_freq") or [] for h in art.get("passive_hops", [])]
            freq_limits = [(lim["max_dbm"], lim.get("delta_db_by_freq") or [])
                           for lim in art.get("freq_dependent_limits", [])]
            ceiling_const = art.get("gain_ceiling_db")
            if ceiling_const is None:
                ceiling_const = float("inf")          # ceiling comes purely from limits
            return cls(gains, powers, art.get("min_gain_db"), ceiling_const, amp,
                       source="calibration file", label=label,
                       hops=hops, freq_limits=freq_limits,
                       center_freq=art.get("center_freq_hz"))

        curve = art.get("curve") or []                # v1: pre-flattened operating curve
        gains = [pt[0] for pt in curve]
        powers = [pt[1] for pt in curve]
        return cls(gains, powers, art.get("min_gain_db"), art.get("max_gain_db"), amp,
                   source="calibration file", label=label)

    @classmethod
    def load(cls, baked: "PowerMap", env_var: str = CALIBRATION_FILE_ENV) -> "PowerMap":
        """Return the injected calibration map if ``$SDR_CALIBRATION_FILE`` is set,
        else ``baked``. A path that is set but unreadable/malformed raises — the agent
        only ever writes a valid artifact, so a broken one is a real error, not a
        reason to silently fall back to a different power scale.

        Amplitude gate: the script transmits at a FIXED baseband amplitude
        (``baked.amplitude``) and the calibration is only valid at the amplitude it was
        measured at (the artifact's ``amplitude``). If they differ, the calibrated dBm↔gain
        mapping no longer describes this script — so the calibration is REJECTED: the baked
        (uncalibrated) map is returned with a loud ``warning`` and a logged WARNING, never a
        silent switch to a mismatched power scale. Re-calibrating at the script's amplitude
        restores it."""
        path = os.environ.get(env_var)
        if not path:
            return baked
        try:
            with open(path, encoding="utf-8") as fh:
                art = json.load(fh)
        except (OSError, ValueError) as exc:
            raise ValueError(f"{env_var}={path} could not be read: {exc}") from exc
        art_amp = art.get("amplitude")
        if art_amp is not None and abs(float(art_amp) - baked.amplitude) > AMPLITUDE_MATCH_TOL:
            baked.warning = (
                f"calibration IGNORED — it was measured at amplitude {float(art_amp):g}, but "
                f"this script transmits at {baked.amplitude:g}; its calibrated power is no "
                f"longer valid. Running UNCALIBRATED. Re-run calibration at amplitude "
                f"{baked.amplitude:g} to restore it.")
            log.warning("%s", baked.warning)
            return baked
        return cls.from_artifact(art, fallback_amplitude=baked.amplitude)

    def describe(self) -> str:
        """One-line banner summary, e.g. 'calibration file — EIRP, at antenna_eirp'.
        A frequency-dependent map notes that its numbers are evaluated at a frequency."""
        base = f"{self.source} — {self.label}"
        return base + " (frequency-dependent)" if self.freq_dependent else base
