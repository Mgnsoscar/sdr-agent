"""
ramp — expand a parametric parameter ramp into discrete tune points.

A ramp sweeps one live parameter from `start` to `stop` in steps, each held for a
dwell time, so it fires as a series of `tune` actions. It's defined by any two of
{step-size, hold-time, duration}; the third is derived. When a ramp is anchored to
BOTH the on-air and off-air edges it fills the whole on-air window, so its duration
comes from the placement (a plan's window) and only ONE of {step, hold} is given.

This module is pure (no model imports) so the client can vendor it verbatim. The
`min_on_air_duration` helper duck-types its input: any object with .anchor,
.offset_s, .action and (for ramps) .ramp works, which both the agent's and the
client's SequenceStep satisfy.

Anchoring / offsets (mirrors SequenceStep):
  - anchor "start": the ramp's FIRST point is at on-air T0 + offset_s; it runs
    forward. Its latest point is offset_s + duration.
  - anchor "stop":  the ramp's LAST point is at off-air + offset_s (offset_s ≤ 0);
    it runs up to that edge. Its earliest point is offset_s - duration.
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
    values: List[float]     # the value at each point (length n_intervals + 1)
    hold_s: float           # dwell between points
    duration_s: float       # first point → last point
    n_intervals: int        # number of steps taken (points - 1)


def resolve_ramp(start: float, stop: float, *, step: Optional[float] = None,
                 hold_s: Optional[float] = None, duration_s: Optional[float] = None,
                 window_s: Optional[float] = None) -> ResolvedRamp:
    """Compute the concrete points of a ramp from any valid combination of inputs.
    `window_s`, when given (dual-anchor), is the duration and overrides duration_s.
    Raises ValueError if under-specified, degenerate, or it would expand too far."""
    if start == stop:
        raise ValueError("ramp start and stop must differ")
    delta = float(stop) - float(start)
    adelta = abs(delta)
    sign = 1.0 if delta > 0 else -1.0

    # A window (dual-anchor) is the duration; otherwise use an explicit duration.
    D = window_s if window_s is not None else duration_s

    if step is not None:
        # Honour the step size: values increment by `step`, last clamped to stop.
        if step <= 0:
            raise ValueError("ramp step must be positive")
        n_intervals = max(1, math.ceil(adelta / step - 1e-9))
        if D is not None:
            hold = D / n_intervals
        elif hold_s is not None:
            hold = float(hold_s)
        else:
            raise ValueError("ramp needs a hold time or a duration alongside step")
        _guard(n_intervals)
        values = [float(start) + sign * step * i for i in range(n_intervals)]
        values.append(float(stop))
    else:
        # No step: need duration AND hold; derive an even step to fit exactly.
        if D is None or hold_s is None:
            raise ValueError("ramp needs two of {step, hold, duration} (or a window)")
        n_intervals = max(1, round(D / float(hold_s)))
        hold = D / n_intervals
        _guard(n_intervals)
        values = [float(start) + delta * (i / n_intervals) for i in range(n_intervals + 1)]

    if hold <= 0:
        raise ValueError("ramp hold time resolves to zero — increase duration or reduce steps")
    return ResolvedRamp(values=values, hold_s=hold,
                        duration_s=n_intervals * hold, n_intervals=n_intervals)


def place_ramp(anchor: str, offset_s: float,
               resolved: ResolvedRamp) -> List[Tuple[str, float, float]]:
    """Turn a resolved ramp into [(fire_anchor, fire_offset_s, value), …], each of
    which becomes a `tune` fire. fire_anchor is 'start' or 'stop' (never 'both')."""
    n, hold = resolved.n_intervals, resolved.hold_s
    if anchor == "stop":
        # Last point sits at off-air + offset_s; earlier points precede it.
        return [("stop", offset_s - (n - i) * hold, v)
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
            span = resolve_ramp(r.start, r.stop, step=r.step, hold_s=r.hold_s,
                                 duration_s=r.duration_s).duration_s
            if anchor == "start":
                max_start = max(max_start, offset + span)
            elif anchor == "stop":
                min_stop = min(min_stop, offset - span)
        elif anchor == "start":
            max_start = max(max_start, offset)
        elif anchor == "stop":
            min_stop = min(min_stop, offset)
    return max(0.0, max_start) + max(0.0, -min_stop)
