"""
Shared, pure achievable-power resolver for ACTIVE components (programmable
gain/attenuation stages — e.g. a step attenuator).

Given the SDR-plane power map (the delivered power for a commanded SDR gain, with every
active component at its 0-dB-applied baseline) and the active components' grids, this
computes:

  * the EXTENDED achievable range (the SDR alone, plus what the components can add/remove);
  * ``snap`` — the nearest power the whole chain can actually produce;
  * ``realize`` — the device settings that produce it (SDR gain + each component's applied
    gain), SDR-first: keep the components at rest so the SDR carries the signal down to an
    engagement threshold, below which the SDR is pinned and the components fill the rest;
  * ``quantize_up``/``quantize_down`` — the next/previous achievable level (non-uniform).

The model: ``P = P_base(g) − R`` where ``P_base(g)`` is the delivered power with the SDR at
gain ``g`` and every component at rest (max applied gain), and ``R ≥ 0`` is a reduction the
components spend on their own grids. Because a fine component trims the fraction between the
SDR's coarse grid points, the achievable resolution is the finest device step across the
whole range, and every offered value is realizable.

This module is imported by BOTH the agent resolver (agent/calibration.py) and the transmit
script (paramkit/calkit.py); the client mirrors it verbatim (sdr-client/state/achievable.py).
Keep the three copies in step — it is pure (no imports beyond math) so they can be identical.
"""
from __future__ import annotations

import math


class Active:
    """One active component's grid, in *applied gain* (signed dB it adds to the chain).

    ``applied_hi`` is its rest / max-power state, ``applied_lo`` its max-reduction state;
    ``step_db`` its resolution and ``engage_pct`` where (as a % of the SDR dynamic range) it
    starts contributing. ``meta`` is opaque passthrough for the caller (plane/task/param…)."""
    __slots__ = ("applied_hi", "applied_lo", "span_db", "step_db", "engage_pct", "meta")

    def __init__(self, applied_hi, applied_lo, step_db, engage_pct=0.0, meta=None):
        self.applied_hi = float(applied_hi)
        self.applied_lo = float(applied_lo)
        self.span_db = self.applied_hi - self.applied_lo
        self.step_db = float(step_db)
        self.engage_pct = float(engage_pct)
        self.meta = meta if meta is not None else {}


class AchievableGrid:
    """Resolver over an SDR power map + active-component grids. ``power_for_gain`` maps a
    commanded SDR gain to delivered power (components at baseline); ``gain_for_power`` is its
    inverse. Both are the RAW (un-snapped) maps — this class does its own grid snapping."""

    def __init__(self, power_for_gain, gain_for_power, min_gain, ceiling,
                 gain_step, actives):
        self._pfg = power_for_gain
        self._gfp = gain_for_power
        self._min_gain = float(min_gain)
        self._ceiling = float(ceiling)
        self._step = float(gain_step) if gain_step else 0.0
        self._act = list(actives)
        self._sum_hi = sum(a.applied_hi for a in self._act)
        self._span = sum(a.span_db for a in self._act)

    # ── gain grid ────────────────────────────────────────────────────────────────
    def _grid(self, g, mode="nearest"):
        lo, hi = self._min_gain, self._ceiling
        g = min(max(float(g), lo), hi)
        s = self._step
        if not s:
            return g
        q = (math.floor(g / s) if mode == "floor"
             else math.ceil(g / s) if mode == "ceil" else round(g / s))
        gg = q * s
        if gg > hi:
            gg = math.floor(hi / s) * s
        if gg < lo:
            gg = math.ceil(lo / s) * s
        return round(gg, 6)

    def _p_base(self, g):
        """Delivered power with the SDR at gain ``g`` and every component at rest."""
        return self._pfg(g) + self._sum_hi

    def _snap_reduction(self, x):
        """Snap a desired reduction ``x`` dB (≥0) to what the components can produce,
        distributed greedily in order. Returns (total_reduction, [applied_db per component])."""
        remaining = max(0.0, float(x))
        applied = []
        total = 0.0
        for a in self._act:
            take = min(remaining, a.span_db)
            r = min(max(round(take / a.step_db) * a.step_db, 0.0), a.span_db)
            applied.append(round(a.applied_hi - r, 6))
            total += r
            remaining -= r
        return round(total, 6), applied

    # ── core ─────────────────────────────────────────────────────────────────────
    def realize(self, power):
        """SDR-first realization of a requested delivered power. Returns
        ``{power_dbm, sdr_gain_db, applied}`` where ``applied`` is the per-component applied
        gain (component order). The nearest achievable power is chosen; values between SDR
        grid points are reached by nudging the SDR up a step and trimming down with the
        components (so every offered value is realizable)."""
        lo_g = self._grid(self._min_gain)
        hi_g = self._grid(self._ceiling)
        s_lo, s_hi = self._p_base(lo_g), self._p_base(hi_g)
        engage = max((a.engage_pct for a in self._act), default=0.0)
        thr = s_lo + engage / 100.0 * (s_hi - s_lo) if s_hi > s_lo else s_lo
        target = min(max(float(power), thr - self._span), s_hi)

        g_thr = self._grid(self._gfp(thr - self._sum_hi), "ceil")
        if self._step and self._p_base(g_thr) < thr - 1e-9:
            g_thr = self._grid(g_thr + self._step, "ceil")
        g_thr = min(max(g_thr, lo_g), hi_g)

        g_floor = self._grid(self._gfp(target - self._sum_hi), "floor")
        cands = {g_floor, g_thr, hi_g}
        if self._step:
            cands.add(self._grid(g_floor + self._step, "ceil"))
        best = None
        for g in cands:
            g = min(max(g, g_thr), hi_g)
            pb = self._p_base(g)
            resid = pb - target
            if resid >= -1e-9:
                red, applied = self._snap_reduction(resid)
            else:                                    # undershoots; can't trim upward
                red, applied = 0.0, [a.applied_hi for a in self._act]
            achieved = pb - red
            # Nearest to target; then SDR-first — least reduction (components nearest rest).
            key = (abs(achieved - target), red)
            if best is None or key < best[0]:
                best = (key, g, achieved, applied)
        _, g, achieved, applied = best
        return {"power_dbm": round(achieved, 6), "sdr_gain_db": round(g, 6),
                "applied": applied}

    def snap(self, power):
        return self.realize(power)["power_dbm"]

    def bounds(self):
        return (self.realize(float("-inf"))["power_dbm"],
                self.realize(float("inf"))["power_dbm"])

    def _nominal_step(self, power):
        cands = [a.step_db for a in self._act]
        if self._step:
            g = self.realize(power)["sdr_gain_db"]
            p0 = self._p_base(g)
            p1 = self._p_base(self._grid(g + self._step))
            if abs(p1 - p0) > 1e-9:
                cands.append(abs(p1 - p0))
        return min(cands) if cands else 0.1

    def quantize_up(self, power):
        cur = self.snap(power)
        n = self._nominal_step(cur)
        step = n
        for _ in range(64):
            cand = self.snap(cur + step)
            if cand > cur + 1e-6:
                return cand
            step += n
        return cur

    def quantize_down(self, power):
        cur = self.snap(power)
        n = self._nominal_step(cur)
        step = n
        for _ in range(64):
            cand = self.snap(cur - step)
            if cand < cur - 1e-6:
                return cand
            step += n
        return cur
