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
components spend on their own discrete grids. The achievable SET is therefore every
``P_base(g) − r`` for ``g`` on the SDR gain grid and ``r`` a reachable reduction. When the
SDR step and a component's step are not commensurate (not multiples of one another) this set
is FINER than either step — a vernier — so both snap and quantize search the real set rather
than assuming a single uniform step: ``realize`` scans every SDR grid gain that could reach
the target (bounded by the reduction span), and ``quantize`` enumerates the true neighbouring
levels. Every value offered is genuinely realizable.

This module is imported by BOTH the agent resolver (agent/calibration.py) and the transmit
script (paramkit/calkit.py); the client mirrors it verbatim (sdr-client/state/achievable.py).
Keep the three copies in step — it is pure (no imports beyond the stdlib) so they can be
identical.
"""
from __future__ import annotations

import bisect
import math

_EPS = 1e-9
_SCAN_CAP = 200000        # guard: never iterate more than this many grid points at once


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
        self._red = None                       # cached reachable reduction totals (sorted)

        # Fixed grid geometry (independent of the requested power).
        self._lo_g = self._grid(self._min_gain)
        self._hi_g = self._grid(self._ceiling)
        self._s_lo = self._p_base(self._lo_g)
        self._s_hi = self._p_base(self._hi_g)
        engage = max((a.engage_pct for a in self._act), default=0.0)
        self._thr = (self._s_lo + engage / 100.0 * (self._s_hi - self._s_lo)
                     if self._s_hi > self._s_lo else self._s_lo)
        # The lowest SDR grid gain the SDR is allowed to use (its power ≥ the threshold): at or
        # below the threshold the SDR is pinned here and the components fill the rest.
        gt = self._grid(self._gfp(self._thr - self._sum_hi), "ceil")
        if self._step and self._p_base(gt) < self._thr - _EPS:
            gt = self._grid(gt + self._step, "ceil")
        self._g_thr = min(max(gt, self._lo_g), self._hi_g)

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
        distributed greedily in order (exact for a single component; near-optimal for a
        coarse+fine chain). Returns (total_reduction, [applied_db per component])."""
        remaining = max(0.0, float(x))
        applied = []
        total = 0.0
        for a in self._act:
            if a.step_db > 0:
                take = min(remaining, a.span_db)
                r = min(max(round(take / a.step_db) * a.step_db, 0.0), a.span_db)
            else:
                r = 0.0
            applied.append(round(a.applied_hi - r, 6))
            total += r
            remaining -= r
        return round(total, 6), applied

    def _reduction_values(self):
        """The sorted set of all reachable reduction totals (dB ≥ 0): the Minkowski sum of
        each component's ``{0, step, …, span}`` grid. For one component this is just its
        multiples; for several it is their sum (capped — beyond the cap it degrades to the
        union of the per-component grids, still valid reductions)."""
        if self._red is not None:
            return self._red
        totals = {0.0}
        for a in self._act:
            if a.step_db <= 0:
                continue
            k = int(round(a.span_db / a.step_db))
            steps = [round(i * a.step_db, 6) for i in range(k + 1) if i * a.step_db <= a.span_db + _EPS]
            merged = {round(t + s, 6) for t in totals for s in steps}
            totals = merged if len(merged) <= _SCAN_CAP else (totals | set(steps))
        self._red = sorted(totals)
        return self._red

    # ── candidate SDR gains ────────────────────────────────────────────────────────
    def _gain_points(self, g_a, g_b):
        """Grid gains in ``[g_a, g_b]`` (clamped to the usable range), inclusive."""
        g_a = min(max(g_a, self._g_thr), self._hi_g)
        g_b = min(max(g_b, self._g_thr), self._hi_g)
        if not self._step:
            return {g_a, g_b}
        out = {self._g_thr, self._hi_g}
        g, n = g_a, 0
        while g <= g_b + _EPS and n < _SCAN_CAP:
            out.add(round(g, 6)); g += self._step; n += 1
        return out

    # ── core ─────────────────────────────────────────────────────────────────────
    def realize(self, power):
        """SDR-first realization of a requested delivered power. Returns
        ``{power_dbm, sdr_gain_db, applied}`` — the nearest ACHIEVABLE power and the device
        settings that produce it. Every SDR grid gain whose baseline could trim down to the
        target is tried (so a value between the SDR's coarse grid points is reached by nudging
        the SDR up and trimming with the components), and among equally-near options the one
        with the LEAST reduction wins (SDR-first — components nearest rest)."""
        target = min(max(float(power), self._thr - self._span), self._s_hi)
        # Gains whose baseline power sits in [target, target+span] can trim down to target;
        # the floor/ceil brackets cover the exact-target and undershoot cases.
        g_lo = self._grid(self._gfp(target - self._sum_hi), "floor")
        g_hi = self._grid(self._gfp(target + self._span - self._sum_hi), "ceil")
        best = None
        for g in self._gain_points(g_lo, g_hi):
            pb = self._p_base(g)
            resid = pb - target
            if resid >= -_EPS:
                red, applied = self._snap_reduction(resid)
            else:                                    # undershoots; can't trim upward
                red, applied = 0.0, [a.applied_hi for a in self._act]
            achieved = pb - red
            key = (round(abs(achieved - target), 6), round(red, 6))
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

    # ── quantize (true neighbouring achievable levels) ─────────────────────────────
    def _levels_in(self, lo, hi):
        """Every achievable delivered power in ``[lo, hi]`` (sorted). A level is
        ``P_base(g) − r`` for a grid gain ``g`` and a reachable reduction ``r``; only gains
        whose baseline can land in range (``lo ≤ P_base(g) ≤ hi + span``) and reductions in
        ``[P_base(g)−hi, P_base(g)−lo]`` are considered, so the scan stays bounded."""
        reductions = self._reduction_values()
        g_a = self._grid(self._gfp(lo - self._sum_hi), "ceil")
        g_b = self._grid(self._gfp(hi + self._span - self._sum_hi), "floor")
        out = set()
        for g in self._gain_points(g_a, g_b):
            pb = self._p_base(g)
            i = bisect.bisect_left(reductions, (pb - hi) - _EPS)
            j = bisect.bisect_right(reductions, (pb - lo) + _EPS)
            for r in reductions[i:j]:
                lvl = round(pb - r, 6)
                if lo - _EPS <= lvl <= hi + _EPS:
                    out.add(lvl)
        return sorted(out)

    def _window(self):
        """A power window guaranteed to be at least as wide as the COARSEST gap between
        achievable levels (the SDR's own power step and each component's step), so a single
        pass of ``_levels_in`` finds a neighbour on each side."""
        cands = [a.step_db for a in self._act if a.step_db > 0]
        if self._step:
            p0 = self._p_base(self._g_thr)
            p1 = self._p_base(self._grid(self._g_thr + self._step))
            if abs(p1 - p0) > _EPS:
                cands.append(abs(p1 - p0))
        return (max(cands) if cands else 1.0) * 1.5 + 1e-3

    def quantize_up(self, power):
        cur = self.snap(power)
        win = self._window()
        for _ in range(8):
            above = [l for l in self._levels_in(cur - _EPS, cur + win) if l > cur + 1e-6]
            if above:
                return min(above)
            win *= 4
        return cur

    def quantize_down(self, power):
        cur = self.snap(power)
        win = self._window()
        for _ in range(8):
            below = [l for l in self._levels_in(cur - win, cur + _EPS) if l < cur - 1e-6]
            if below:
                return max(below)
            win *= 4
        return cur
