"""
ramp — expand a parametric parameter ramp into discrete tune points.

A ramp sweeps one live parameter from `start` to `stop` as a series of HELD LEVELS,
each applied and held for a dwell time, so it fires as a series of `tune` actions.
It's defined by any two of {step-size, hold-time, duration}; the third is derived.
When a ramp is anchored to BOTH the on-air and off-air edges it fills the whole
on-air window, so its duration comes from the placement (a plan's window) and only
ONE of {step, hold} is given.

Held levels + duration
──────────────────────
Every emitted level is held for the dwell time, INCLUDING the last one — so the
ramp's duration is (number of emitted levels) × hold. Two ramps placed back-to-back
therefore tile with no gap and no doubled value at the seam: their durations add.

include_first / include_last (single-anchor ramps only)
───────────────────────────────────────────────────────
The full ladder runs from `start` to `stop`. Dropping the first level (start value)
or the last (stop value) removes that level AND its hold, shrinking the duration by
one dwell — the tool for chaining ramps: e.g. a 0→10 ramp that excludes its last
level, followed by a 10→20 ramp, plays 0,2,…,8,10,12,…,20 with 10 held exactly once.
A window-filling ('both') ramp ignores these flags and always fills its window.

This module is pure (no model imports) so the client can vendor it verbatim. The
`min_on_air_duration` helper duck-types its input: any object with .anchor,
.offset_s, .action and (for ramps) .ramp works, which both the agent's and the
client's SequenceStep satisfy.

Anchoring / offsets (mirrors SequenceStep):
  - anchor "start": the ramp's FIRST point is at on-air T0 + offset_s; it runs
    forward. Its occupied span is offset_s .. offset_s + duration.
  - anchor "stop":  the ramp's LAST level is held right up to off-air + offset_s
    (offset_s ≤ 0); it runs up to that edge. Its span is offset_s - duration .. offset_s.
  - anchor "both":  fills [on-air, off-air]; duration = the on-air window.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

MAX_POINTS = 2000   # a ramp expanding past this is almost certainly a mistake


def _guard(n_intervals: int) -> None:
    """Reject an over-large ramp BEFORE we build its value list (so a tiny step
    over a huge range fails fast instead of trying to allocate millions)."""
    if n_intervals + 1 > MAX_POINTS:
        raise ValueError(
            f"ramp expands to {n_intervals + 1} points (max {MAX_POINTS}); "
            f"use a larger step or hold time")


@dataclass
class ResolvedRamp:
    values: List[float]     # the emitted levels, each held for hold_s
    hold_s: float           # dwell each level is held
    duration_s: float       # len(values) * hold_s — every emitted level held
    n_intervals: int        # increments between emitted levels (points - 1)


def _full_ladder(start: float, stop: float, delta: float, adelta: float, sign: float,
                 *, steps: Optional[int], step: Optional[float],
                 hold_s: Optional[float], duration_s: Optional[float]) -> Tuple[List[float], int]:
    """The full start→stop ladder of held levels (before first/last trimming), plus
    its interval count. Chooses the construction from whichever inputs are given."""
    if step is not None:
        if step <= 0:
            raise ValueError("ramp step must be positive")
        n_int = max(1, math.ceil(adelta / step - 1e-9))
        _guard(n_int)   # reject a huge expansion BEFORE allocating the value list
        # Honour the step size: increment by `step`, last level clamped to stop.
        values = [float(start) + sign * step * i for i in range(n_int)]
        values.append(float(stop))
    elif steps is not None:
        n_int = max(1, int(steps))
        _guard(n_int)
        values = [float(start) + delta * (i / n_int) for i in range(n_int + 1)]
    elif duration_s is not None and hold_s is not None:
        # Duration + hold with no explicit step: the level COUNT is duration/hold
        # (each level held `hold`), so the whole ramp lasts `duration`.
        n_levels = max(2, round(float(duration_s) / float(hold_s)))
        n_int = n_levels - 1
        _guard(n_int)
        values = [float(start) + delta * (i / n_int) for i in range(n_int + 1)]
    else:
        raise ValueError("ramp needs two of {step, hold, duration} (or a window)")
    return values, n_int


def resolve_ramp(start: float, stop: float, *, steps: Optional[int] = None,
                 step: Optional[float] = None, hold_s: Optional[float] = None,
                 duration_s: Optional[float] = None,
                 window_s: Optional[float] = None,
                 include_first: bool = True, include_last: bool = True) -> ResolvedRamp:
    """Compute the concrete levels of a ramp from any valid combination of inputs.
    Prefer `steps` (the number of equal increments) — it always divides the range
    and duration evenly; `step` (a fixed increment size) is still accepted.

    Every emitted level is held for `hold`, so duration = len(values) × hold.
    `include_first` / `include_last` drop the start / stop level (and its hold) — for
    single-anchor ramps only. `window_s`, when given (dual-anchor), is the fixed
    duration and fills the window edge-to-edge, ignoring the include flags.
    Raises ValueError if under-specified, degenerate, or it would expand too far."""
    if start == stop:
        raise ValueError("ramp start and stop must differ")
    delta = float(stop) - float(start)
    adelta = abs(delta)
    sign = 1.0 if delta > 0 else -1.0

    # ── Dual-anchor: fill the on-air window exactly, both ends included (legacy). ──
    if window_s is not None:
        D = window_s
        if steps is not None:
            n_int = max(1, int(steps))
            _guard(n_int)
            values = [float(start) + delta * (i / n_int) for i in range(n_int + 1)]
        elif step is not None:
            if step <= 0:
                raise ValueError("ramp step must be positive")
            n_int = max(1, math.ceil(adelta / step - 1e-9))
            _guard(n_int)
            values = [float(start) + sign * step * i for i in range(n_int)]
            values.append(float(stop))
        elif hold_s is not None:
            n_int = max(1, round(D / float(hold_s)))
            _guard(n_int)
            values = [float(start) + delta * (i / n_int) for i in range(n_int + 1)]
        else:
            raise ValueError("a window-filling ramp needs a step count or hold time")
        hold = D / n_int
        if hold <= 0:
            raise ValueError("ramp hold time resolves to zero — increase the window")
        return ResolvedRamp(values=values, hold_s=hold,
                            duration_s=n_int * hold, n_intervals=n_int)

    # ── Single-anchor: held-levels model with first/last trimming. ──
    full, n_int = _full_ladder(start, stop, delta, adelta, sign,
                               steps=steps, step=step, hold_s=hold_s, duration_s=duration_s)

    # Dwell: taken directly when given, else derived so the FULL ladder lasts
    # `duration` — trimming then removes whole (level, hold) slots.
    if hold_s is not None:
        hold = float(hold_s)
    elif duration_s is not None:
        hold = float(duration_s) / len(full)
    else:
        raise ValueError("ramp needs a hold time or a duration alongside the step")
    if hold <= 0:
        raise ValueError("ramp hold time resolves to zero — increase duration or reduce steps")

    lo = 0 if include_first else 1
    hi = len(full) if include_last else len(full) - 1
    if hi - lo < 1:
        raise ValueError("ramp has no levels left after excluding the first/last")
    values = full[lo:hi]
    return ResolvedRamp(values=values, hold_s=hold,
                        duration_s=len(values) * hold, n_intervals=len(values) - 1)


def place_ramp(anchor: str, offset_s: float,
               resolved: ResolvedRamp) -> List[Tuple[str, float, float]]:
    """Turn a resolved ramp into [(fire_anchor, fire_offset_s, value), …], each of
    which becomes a `tune` fire. fire_anchor is 'start' or 'stop' (never 'both').

    Every level occupies a hold slot. A "stop" ramp's LAST level is held over the
    slot ending at the anchor (off-air + offset_s), so it fires one hold before the
    edge; a "start"/"both" ramp runs forward from offset_s."""
    P, hold = len(resolved.values), resolved.hold_s
    if anchor == "stop":
        # Level i is held over [offset - (P-i)·hold, offset - (P-i-1)·hold]; the last
        # (i = P-1) ends exactly at the anchor.
        return [("stop", offset_s - (P - i) * hold, v)
                for i, v in enumerate(resolved.values)]
    # "start" runs forward from offset_s; "both" starts at its on-air inset offset_s.
    return [("start", offset_s + i * hold, v) for i, v in enumerate(resolved.values)]


# ── Minimum on-air duration ───────────────────────────────────────────────────

def _action_str(action) -> str:
    return action.value if hasattr(action, "value") else str(action)


def min_on_air_duration(steps) -> float:
    """The shortest on-air window (seconds) in which a sequence's steps still fit —
    the last start-anchored point must land at or before the first stop-anchored
    one. Fixed-duration ramps contribute their span; dual-anchor ('both') ramps
    fill the window and add nothing. Duck-typed over SequenceStep."""
    max_start = 0.0     # latest start-anchored fire offset (from T0)
    min_stop = 0.0      # earliest stop-anchored fire offset (from off-air, ≤ 0)
    for s in steps:
        anchor = getattr(s, "anchor", "start")
        offset = float(getattr(s, "offset_s", 0.0))
        if _action_str(getattr(s, "action", "")) == "ramp" and getattr(s, "ramp", None):
            r = s.ramp
            if anchor == "both":
                # Fills the window between its insets: on-air+offset .. off-air+end.
                end = float(getattr(s, "offset_end_s", 0.0) or 0.0)
                max_start = max(max_start, offset)
                min_stop = min(min_stop, end)
                continue
            span = resolve_ramp(r.start, r.stop, steps=getattr(r, "steps", None),
                                 step=r.step, hold_s=r.hold_s,
                                 duration_s=r.duration_s,
                                 include_first=getattr(r, "include_first", True),
                                 include_last=getattr(r, "include_last", True)).duration_s
            if anchor == "start":
                max_start = max(max_start, offset + span)
            elif anchor == "stop":
                min_stop = min(min_stop, offset - span)
        elif anchor == "start":
            max_start = max(max_start, offset)
        elif anchor == "stop":
            min_stop = min(min_stop, offset)
    return max(0.0, max_start) + max(0.0, -min_stop)
