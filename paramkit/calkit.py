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
import math
import os
from typing import Optional

from paramkit.power_law import parse_bridge

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


def _active_applied(a: dict) -> tuple:
    """(applied_hi, applied_lo) — the applied gain (signed dB) range of an active component.
    An attenuator's parameter subtracts power, a gain block's adds it."""
    lo, hi = float(a["min_db"]), float(a["max_db"])
    if a.get("sense", "attenuation") == "attenuation":
        return (-lo, -hi)
    return (hi, lo)


def _active_param_value(a: dict, applied: float) -> float:
    """The parameter value to command on the component's task for a given applied gain."""
    lo, hi = float(a["min_db"]), float(a["max_db"])
    v = -applied if a.get("sense", "attenuation") == "attenuation" else applied
    return min(max(v, lo), hi)


class PowerMap:
    """Maps a requested delivered/radiated power (dBm) to a commanded SDR gain (dB) and
    back, over a monotonic measured anchor curve, with any frequency-dependent passive
    hops (cable, antenna) folded at the transmit frequency, clamped to the unit's gain
    limits (the ceiling itself may tighten with frequency)."""

    def __init__(self, gains, powers, min_gain_db, ceiling_const, amplitude,
                 source, label, hops=(), freq_limits=(), center_freq=None,
                 gain_step_db=None, actives=(), source_bias=(),
                 reported=None, limiting=None, limiting_cap=None,
                 reported_applies=False):
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
        # Hardware gain step (dB): the SDR only settles on a discrete gain grid, so the
        # commanded gain is snapped to the nearest grid point (never above the ceiling), and
        # the reported power reflects that actual gain. None/0 = continuous (no snapping).
        self._gain_step = float(gain_step_db) if gain_step_db and float(gain_step_db) > 0 else None
        # v2 frequency machinery ((freqs, deltas) per passive hop; per-limit tables).
        self._hops = [([float(f) for f, _ in t], [float(d) for _, d in t]) for t in hops]
        # Each frequency-dependent limit: (max_dbm, freqs, deltas, anchor_gains, anchor_powers,
        # via_limiting). anchor_* is None when the limit inverts against the shared operating
        # anchor; it is a separate LIMITING curve when the limit gauges on a different curve at
        # the same node (a reported operating plane, or an OWN limiting reading). via_limiting is
        # True when the limit is gauged through a law/same LIMITING reading — the consumer folds
        # the limiting delta (max_dbm − Δlim) at the live parameter value before inverting on the
        # shared anchor (see the agent's to_public_dict).
        self._freq_limits = []
        for item in freq_limits:
            mx, t = item[0], item[1]
            anchor = item[2] if len(item) > 2 else None
            via = item[3] if len(item) > 3 else False
            fs = [float(f) for f, _ in t]
            ds = [float(d) for _, d in t]
            if anchor:
                pairs = sorted((float(g), float(p)) for g, p in anchor)
                ag = [g for g, _ in pairs]
                ap = [p for _, p in pairs]
            else:
                ag = ap = None
            self._freq_limits.append((float(mx), fs, ds, ag, ap, bool(via)))
        self._center_freq = None if center_freq is None else float(center_freq)
        # Active components (programmable gain/attenuation): each carries its own applied-gain
        # grid on top of its passive baseline (already folded into the hops). Empty = a plain
        # passive chain (v1/v2 behaviour unchanged).
        self._actives = [dict(a) for a in (actives or [])]
        # Per-unit SOURCE BIAS Δ dB(f): the SDR's own output-vs-frequency flatness, shifting
        # the measured ANCHOR with frequency (normalized to 0 at the rep frequency). Applied
        # to the anchor everywhere — delivered power AND the limit/ceiling inversion — so it
        # mirrors the agent resolver exactly. Empty ⇒ no bias.
        self._bias = [(float(f), float(d)) for f, d in (source_bias or [])]
        # Power-quantity BRIDGES (docs/calibration-v2.md §13): how the REPORTED reading (what
        # --power means to the operator) and the LIMITING reading (what the cap gauges) derive
        # from the node's MEASURED curve. Evaluated at the live parameter values the script
        # passes in (like the live frequency), so --power stays accurate as a keyed parameter
        # (e.g. sweep bandwidth) is tuned. `reported_applies` is True only when the map is
        # built from the MEASURED anchor (v2), where the reported delta must be added on top;
        # a v1 flat curve already bakes it, so it is not re-applied. None ⇒ a plain map.
        self._reported = reported
        self._limiting = limiting
        self._limiting_cap = None if limiting_cap is None else float(limiting_cap)
        self._reported_applies = bool(reported_applies and reported is not None)

    # ── frequency-dependent internals ────────────────────────────────────────────
    def _eff(self, freq: Optional[float]) -> Optional[float]:
        return freq if freq is not None else self._center_freq

    def _reading_delta(self, bridge, params: Optional[dict]) -> float:
        """The dB a bridge adds to the measured value: at the live parameter values when the
        bridge is param-keyed and ALL of them are supplied (and usable), else at the
        representative values (the bounds fall back to representative, exactly as frequency
        falls back to center_freq). A law keyed on a parameter with no form field — e.g. a
        script-internal one the transmit script fills — folds at its representative value here
        rather than raising."""
        if bridge is None:
            return 0.0
        keyed = bridge.keyed_params()
        if keyed and params and all(params.get(k) is not None for k in keyed):
            try:
                return bridge.delta_db(params)
            except (ValueError, TypeError):
                pass                                  # non-positive/invalid value → representative
        return bridge.rep_delta_db()

    def _reported_shift(self, params: Optional[dict]) -> float:
        """dB between the operating (measured) power and the operator's reported number."""
        return self._reading_delta(self._reported, params) if self._reported_applies else 0.0

    def _op_delta(self, freq: Optional[float]) -> float:
        """Total passive delta (cable + antenna …) at ``freq`` — 0 for a v1 curve."""
        return sum(_table_at(fs, ds, freq) for fs, ds in self._hops)

    def _source_bias_at(self, freq: Optional[float]) -> float:
        """The source-bias shift (dB) applied to the measured anchor at ``freq`` — 0 when
        there's no bias, or (single-point) a constant."""
        if not self._bias:
            return 0.0
        return _table_at([f for f, _ in self._bias], [d for _, d in self._bias], freq)

    def _invert(self, target_power: float) -> float:
        """Anchor gain that yields ``target_power`` at the measured anchor, clamped to
        the measured range (up to the top gain, never extrapolated)."""
        return _interp(target_power, self._powers, self._gains)

    def _ceiling(self, freq: Optional[float], params: Optional[dict] = None) -> float:
        """Gain ceiling at ``freq``: the frequency-independent cap tightened by any
        frequency-dependent limit and by a ceiling on the LIMITING reading. Each frequency
        limit inverts against its own published limiting curve when it carries one (a reported
        operating plane), else the shared anchor. Tightest wins."""
        cap = self._ceiling_const
        b = self._source_bias_at(freq)
        for max_dbm, fs, ds, ag, ap, via in self._freq_limits:
            target = max_dbm - _table_at(fs, ds, freq)
            if via:                               # gauged through the law/same LIMITING reading:
                target -= self._reading_delta(self._limiting, params)  # fold Δlim at live param
            if ag is not None:                    # own (downstream) limiting curve → no bias
                cap = min(cap, _interp(target, ap, ag))
            else:                                 # shared operating anchor = the biased source
                cap = min(cap, _interp(target - b, self._powers, self._gains))
        # Ceiling on the operating node's LIMITING reading (limiting = measured + Δlim), gauged
        # against the measured anchor at the live parameter value — so a param-keyed limit
        # (e.g. a total-power cap when the measurement is a density) tightens as the parameter
        # is tuned, never baked at the wrong value.
        if self._limiting_cap is not None:
            target = self._limiting_cap - self._reading_delta(self._limiting, params)
            cap = min(cap, _interp(target - b, self._powers, self._gains))
        return cap

    def _snap(self, gain: float, freq: Optional[float], params: Optional[dict] = None) -> float:
        """Clamp ``gain`` to [min, ceiling(freq)] and, when a hardware gain step is set,
        snap it to the nearest step on that grid — but NEVER above the ceiling (floor to the
        grid there) so quantisation can't push past a safety limit."""
        lo, hi = self.min_gain_db, self._ceiling(freq, params)
        step = self._gain_step
        if not step:
            return min(max(float(gain), lo), hi)
        g = round(float(gain) / step) * step
        if g > hi:                       # rounding up must not breach the ceiling
            g = math.floor(hi / step) * step
        if g < lo:
            g = math.ceil(lo / step) * step
        return round(g, 6)

    # ── active-component achievable-level resolver ───────────────────────────────
    def _achievable(self, freq: Optional[float], params: Optional[dict] = None):
        """Build the shared achievable-power resolver at ``freq`` (returns the grid and the
        active descriptors), in the OPERATING (measured) quantity. The SDR power map
        (components at baseline) feeds it; the grid adds the components' applied-gain ranges.
        The reported bridge is applied by the caller on top. Empty actives ⇒ a plain SDR grid."""
        from paramkit.achievable import AchievableGrid, Active
        f = self._eff(freq)
        od = self._op_delta(f)
        b = self._source_bias_at(f)
        actives = []
        for a in self._actives:
            hi, lo = _active_applied(a)
            actives.append(Active(hi, lo, a["step_db"], a.get("engage_pct", 0.0), meta=a))
        grid = AchievableGrid(
            power_for_gain=lambda g: _interp(g, self._gains, self._powers) + od + b,
            gain_for_power=lambda p: _interp(p - od - b, self._powers, self._gains),
            min_gain=self.min_gain_db, ceiling=self._ceiling(f, params),
            gain_step=self._gain_step, actives=actives)
        return grid, actives

    def realize(self, delivered_dbm: float, freq: Optional[float] = None,
                params: Optional[dict] = None) -> dict:
        """SDR-first realization of a requested delivered power with active components:
        ``{power_dbm, sdr_gain_db, settings}`` where settings names each component's task,
        parameter and the value to command on it. (The SDR side sets ``sdr_gain_db``; the
        host commands the component tasks to their values.) The requested/returned power is
        in the REPORTED quantity; the grid works in the operating quantity, so it is shifted
        by the reported bridge (evaluated at ``params``) in and back out."""
        if not self.has_absolute:
            raise NoAbsoluteScale("uncalibrated: no absolute power scale for this signal")
        dr = self._reported_shift(params)
        grid, actives = self._achievable(freq, params)
        res = grid.realize(float(delivered_dbm) - dr)
        settings = []
        for act, applied in zip(actives, res["applied"]):
            a = act.meta
            settings.append({"plane": a.get("plane"), "task": a["task"], "param": a["param"],
                             "applied_db": applied,
                             "value": round(_active_param_value(a, applied), 6)})
        return {"power_dbm": res["power_dbm"] + dr, "sdr_gain_db": res["sdr_gain_db"],
                "settings": settings}

    def snap_power(self, delivered_dbm: float, freq: Optional[float] = None,
                   params: Optional[dict] = None) -> float:
        dr = self._reported_shift(params)
        return self._achievable(freq, params)[0].snap(float(delivered_dbm) - dr) + dr

    # ── the two functions the script calls ──────────────────────────────────────
    def gain_for_power(self, delivered_dbm: float, freq: Optional[float] = None,
                       params: Optional[dict] = None) -> float:
        """Commanded SDR gain (dB) for a requested delivered power at ``freq`` (defaults to
        the artifact's representative frequency), clamped to [min, ceiling(freq)]. The power
        is in the REPORTED quantity; a reported bridge (evaluated at ``params``) converts it
        to the operating quantity before inverting the curve. With an active component the
        gain comes from the SDR-first realization (the SDR carries the signal; the component
        fills below the engagement threshold) — the host commands the component to the
        matching value so together they deliver the requested power."""
        if not self.has_absolute:
            raise NoAbsoluteScale(
                "this signal is not calibrated on this unit — absolute --power (dBm) has "
                "no meaning here; provide --gain (raw dB) instead")
        f = self._eff(freq)
        if self._actives:
            return self.realize(float(delivered_dbm), freq, params)["sdr_gain_db"]
        op_power = float(delivered_dbm) - self._reported_shift(params)
        g = self._invert(op_power - self._op_delta(f) - self._source_bias_at(f))
        return self._snap(g, f, params)

    def power_for_gain(self, gain_db: float, freq: Optional[float] = None,
                       params: Optional[dict] = None) -> float:
        """Delivered power (dBm) at the operating plane for an (actual) gain at ``freq``, in
        the REPORTED quantity (the reported bridge is applied on top). The gain is snapped to
        the hardware grid first, so the reported power reflects what the SDR really settles on."""
        if not self.has_absolute:
            raise NoAbsoluteScale("uncalibrated: no absolute power scale for this signal")
        f = self._eff(freq)
        g = self._snap(float(gain_db), f, params)
        op = _interp(g, self._gains, self._powers) + self._op_delta(f) + self._source_bias_at(f)
        return op + self._reported_shift(params)

    @property
    def max_gain_db(self) -> float:
        return self._snap(self._ceiling(self._center_freq), self._center_freq)

    @property
    def max_power_dbm(self):
        """Top of the delivered-power range (reported quantity), or None when uncalibrated.
        With active components this is the extended range."""
        if not self.has_absolute:
            return None
        if self._actives:
            return self._achievable(None)[0].bounds()[1] + self._reported_shift(None)
        return self.power_for_gain(self.max_gain_db)

    @property
    def min_power_dbm(self):
        if not self.has_absolute:
            return None
        if self._actives:
            return self._achievable(None)[0].bounds()[0] + self._reported_shift(None)
        return self.power_for_gain(self.min_gain_db)

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
                or any(len(fs) > 1 for _, fs, _ds, _ag, _ap, _via in self._freq_limits)
                or len(self._bias) > 1)

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
        self._gain_step = None
        self._actives = []
        self._bias = []
        self._reported = None
        self._limiting = None
        self._limiting_cap = None
        self._reported_applies = False
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

        step = art.get("gain_step_db")
        actives = art.get("active_components") or ()
        # Power-quantity bridges (docs/calibration-v2.md §13): reported/limiting readings and a
        # limiting-reading cap, laws embedded, so no script metadata is needed at runtime.
        reported = limiting = None
        limiting_cap = None
        readings = art.get("readings")
        if isinstance(readings, dict):
            reported = parse_bridge(readings.get("reported"))
            lim_spec = readings.get("limiting") or {}
            limiting = parse_bridge(lim_spec)
            if lim_spec.get("max_dbm") is not None:
                limiting_cap = lim_spec["max_dbm"]
        anchor = art.get("anchor_curve")
        if anchor:                                    # v2: fold passive hops at frequency
            gains = [pt[0] for pt in anchor]
            powers = [pt[1] for pt in anchor]
            hops = [h.get("delta_db_by_freq") or [] for h in art.get("passive_hops", [])]
            freq_limits = [(lim["max_dbm"], lim.get("delta_db_by_freq") or [],
                            lim.get("anchor_curve"), lim.get("via_limiting", False))
                           for lim in art.get("freq_dependent_limits", [])]
            ceiling_const = art.get("gain_ceiling_db")
            if ceiling_const is None:
                ceiling_const = float("inf")          # ceiling comes purely from limits
            # The v2 anchor is the MEASURED curve, so the reported bridge is applied on top.
            return cls(gains, powers, art.get("min_gain_db"), ceiling_const, amp,
                       source="calibration file", label=label,
                       hops=hops, freq_limits=freq_limits,
                       center_freq=art.get("center_freq_hz"), gain_step_db=step,
                       actives=actives,
                       source_bias=art.get("source_bias_delta_by_freq") or (),
                       reported=reported, limiting=limiting, limiting_cap=limiting_cap,
                       reported_applies=True)

        curve = art.get("curve") or []                # v1: pre-flattened operating curve
        gains = [pt[0] for pt in curve]
        powers = [pt[1] for pt in curve]
        return cls(gains, powers, art.get("min_gain_db"), art.get("max_gain_db"), amp,
                   source="calibration file", label=label, gain_step_db=step,
                   actives=actives)

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
