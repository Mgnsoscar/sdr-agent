"""
Per-unit power calibration resolver.

Turns a unit's calibration document (plus the shared unit-type defaults) into a
resolved, ready-to-use mapping between commanded SDR gain and delivered/radiated
power for one signal. See docs/calibration.md for the full design; this module is
the runtime resolver described there.

The model in one paragraph: a **chain** of measurement *planes* describes the RF
cascade (SDR output → amplifier → cable → antenna). A ``measured`` plane carries a
``gain → power`` curve (interpolated); a ``derived`` plane is a parent plane plus a
constant ``delta_db`` (cable loss / antenna gain). The chain — plane topology, gain
limits, and limit *thresholds* — is unit hardware, stated once. Only the measured
``curves`` are per signal. The safety ceiling lives in gain-space: a limit is
"a plane + a max power", inverted through that plane's transfer to a gain cap; the
tightest wins. ``--power`` reads on the ``operating_plane`` (e.g. EIRP at the
antenna); inversion walks derived hops back to the nearest measured plane.

Fail-safe: this resolver never invents a permissive default. A document that is
present but broken (non-monotonic curve, dangling plane reference, no derivable
ceiling, an operating plane with no usable transfer) raises :class:`CalibrationError`
— the caller must refuse to transmit. A document that simply lacks an entry for the
requested signal raises :class:`SignalNotCalibrated`, which the caller may treat as
"fall back to the script's baked-in conservative defaults (with a warning)". The
"no document at all" case never reaches here — the caller handles it.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Optional

SCHEMA_VERSION = 1


# ── Errors ──────────────────────────────────────────────────────────────────────

class CalibrationError(Exception):
    """A hard, safety-relevant defect in a calibration document. The caller MUST
    refuse to transmit — silently falling back could over- or under-drive."""


class SignalNotCalibrated(CalibrationError):
    """The document is well-formed but has no entry for the requested signal. Softer
    than a broken file: the caller may fall back to the script's baked-in defaults
    (with a warning) rather than refusing. Subclasses CalibrationError so a caller
    that catches neither specifically still fails safe."""


# ── Resolved objects ────────────────────────────────────────────────────────────

@dataclass
class _Measured:
    """A measured plane: a monotonic gain→power curve plus a fixed offset.

    ``role`` decides what the curve backs (docs/calibration.md §4.1):

    - ``"limiting"`` (default) — the curve safety limits invert through, AND a valid
      operating/reporting anchor. Every v1 plane is this.
    - ``"reported"`` — a *re-measurement of the same physical node in a different
      quantity* (e.g. main-lobe power vs the node's full-band ``limiting`` curve). It is
      the number shown to the operator, but it is INVISIBLE to limit inversion: the limit
      walk punches straight through it (0 dB, same node) to the ``limiting`` curve named
      by ``of``. So a limit is always gauged in its own quantity while ``--power`` reports
      the region of interest. A ``reported`` plane MUST set ``of`` to a ``limiting`` plane;
      that requirement makes the root (source) plane impossible to mark ``reported``."""
    gains: list[float]
    powers: list[float]
    offset_db: float = 0.0
    quantity: str = ""
    description: str = ""
    role: str = "limiting"
    of: str = ""                             # reported → the limiting plane it re-measures

    @property
    def is_reported(self) -> bool:
        return self.role == "reported"

    def power_at(self, gain: float) -> float:
        return _interp(gain, self.gains, self.powers) + self.offset_db


@dataclass
class _ActiveControl:
    """The dynamic-control descriptor of an ACTIVE component (e.g. a programmable
    attenuator). On top of a derived plane's passive baseline table (its behaviour at
    0 dB applied), an active component can apply a variable gain/attenuation set through
    ``param`` of ``task``. ``min_db``/``max_db`` are that parameter's own value range and
    ``step_db`` its resolution; ``sense`` says whether the parameter adds gain or removes
    it. ``engage_pct`` is where, as a percentage of the SDR-only dynamic range, the
    component starts contributing (0 ⇒ only once the SDR is exhausted at its bottom;
    higher ⇒ engage sooner, keeping the SDR in its upper/optimal region).

    Everything is expressed in *applied gain* (signed dB the component adds to the chain):
    ``applied_hi`` is its max-power / rest state, ``applied_lo`` its max-reduction state."""
    task: str
    param: str
    sense: str                 # "attenuation" (param removes power) | "gain" (param adds it)
    min_db: float              # the parameter's own min value
    max_db: float              # the parameter's own max value
    step_db: float             # the parameter's resolution
    engage_pct: float = 0.0    # % of the SDR dynamic range below which it engages

    @property
    def applied_hi(self) -> float:
        """Max applied gain (rest / max-power state)."""
        return -self.min_db if self.sense == "attenuation" else self.max_db

    @property
    def applied_lo(self) -> float:
        """Min applied gain (max-reduction state)."""
        return -self.max_db if self.sense == "attenuation" else self.min_db

    @property
    def span_db(self) -> float:
        """How much power this component can remove below its rest state (≥ 0)."""
        return self.applied_hi - self.applied_lo

    def param_for_applied(self, applied: float) -> float:
        """The parameter value to command for a given applied gain (clamped to range)."""
        v = -applied if self.sense == "attenuation" else applied
        return min(max(v, self.min_db), self.max_db)

    def to_public_dict(self, plane: str, baseline: list) -> dict:
        return {"plane": plane, "task": self.task, "param": self.param,
                "sense": self.sense, "min_db": self.min_db, "max_db": self.max_db,
                "step_db": self.step_db, "engage_pct": self.engage_pct,
                "baseline_delta_by_freq": [[f, db] for f, db in baseline]}


def _parse_control(spec: object, name: str) -> "_ActiveControl":
    """Validate a plane's ``control`` block and build an _ActiveControl (fail-safe: any
    defect raises, so a broken active component can never silently transmit)."""
    if not isinstance(spec, dict):
        raise CalibrationError(f"plane {name!r} 'control' must be an object")
    task, param = spec.get("task"), spec.get("param")
    if not isinstance(task, str) or not task.strip():
        raise CalibrationError(
            f"active plane {name!r} 'control.task' must be a non-empty string")
    if not isinstance(param, str) or not param.strip():
        raise CalibrationError(
            f"active plane {name!r} 'control.param' must be a non-empty string")
    sense = spec.get("sense", "attenuation")
    if sense not in ("attenuation", "gain"):
        raise CalibrationError(
            f"active plane {name!r} 'control.sense' must be 'attenuation' or 'gain'")
    try:
        min_db = float(spec["min_db"]); max_db = float(spec["max_db"])
        step_db = float(spec["step_db"])
    except (KeyError, TypeError, ValueError):
        raise CalibrationError(
            f"active plane {name!r} 'control' needs numeric min_db, max_db, step_db")
    if not max_db > min_db:
        raise CalibrationError(
            f"active plane {name!r} 'control.max_db' ({max_db}) must exceed min_db ({min_db})")
    if not step_db > 0:
        raise CalibrationError(
            f"active plane {name!r} 'control.step_db' must be > 0")
    try:
        engage = float(spec.get("engage_pct", 0.0))
    except (TypeError, ValueError):
        raise CalibrationError(f"active plane {name!r} 'control.engage_pct' must be numeric")
    if not 0.0 <= engage <= 100.0:
        raise CalibrationError(
            f"active plane {name!r} 'control.engage_pct' must be between 0 and 100")
    return _ActiveControl(task.strip(), param.strip(), sense, min_db, max_db, step_db, engage)


@dataclass
class _Derived:
    """A derived plane: a passive dB hop from a parent plane (cable loss, antenna gain,
    a pad). The hop is a ``delta_db``-vs-frequency table (signed: negative = loss); a
    single-point table is a frequency-independent constant (an inline v1 ``delta_db``,
    or a flat pad). ``component`` names the catalog entry it came from, or ``""`` when
    the hop was stated inline. See docs/calibration-v2.md.

    ``fallback`` marks a *transparent* +0 dB hop synthesised for a MEASURED plane that
    this signal wasn't measured at (a partial measured stage): it lets the plane inherit
    the nearest upstream measured curve so a signal measured only at an earlier stage
    still resolves. Such hops are real for traversal (they contribute 0 dB) but omitted
    from the published passive-hop list — they aren't cables/antennas."""
    frm: str
    table: list                              # [(freq_hz, delta_db)], strictly increasing freq
    component: str = ""
    quantity: str = ""
    description: str = ""
    fallback: bool = False
    control: Optional["_ActiveControl"] = None   # set ⇒ an ACTIVE component (dynamic gain/atten)

    @property
    def is_active(self) -> bool:
        return self.control is not None

    @property
    def is_freq_dependent(self) -> bool:
        return len(self.table) > 1

    def delta_at(self, freq: Optional[float]) -> float:
        """The hop's dB at ``freq`` (linear interp, endpoint-clamped). A one-point table
        is constant, so ``freq`` may be None there; a multi-point table needs a
        frequency (the resolver guarantees one is available before it gets here)."""
        if len(self.table) == 1:
            return self.table[0][1]
        if freq is None:
            raise CalibrationError(
                f"derived hop from {self.frm!r} is frequency-dependent but no frequency "
                f"is available to evaluate it")
        fs = [f for f, _ in self.table]
        ds = [d for _, d in self.table]
        return _interp(float(freq), fs, ds)


@dataclass
class ResolvedCalibration:
    """The runtime-usable result for one (unit, signal). ``gain_for_power`` and
    ``power_for_gain`` are the two functions the transmit script needs; the rest is
    for the banner / reporting.

    Frequency (docs/calibration-v2.md): passive hops (cable, antenna) can vary with
    frequency, so the two functions take an optional ``freq`` — defaulting to the
    signal's representative ``center_freq_hz`` (``_freq_hz``). A constant chain ignores
    frequency entirely, so v1 documents behave exactly as before with ``freq=None``.
    The ceiling is split into a frequency-independent part (``_gain_ceiling_const`` —
    the amp-protection limit lives on a MEASURED plane, so it never moves) and any
    limits whose plane passes through a passive hop (``_freq_limits``)."""
    signal_id: str
    unit_type: str
    amplitude: Optional[float]
    min_gain_db: float
    operating_plane: str
    operating_quantity: str
    _planes: dict = field(repr=False, default_factory=dict)
    _freq_hz: Optional[float] = None                 # representative frequency, or None
    _gain_step: Optional[float] = None               # hardware gain grid (dB), or None
    _gain_ceiling_const: float = float("inf")        # freq-independent gain cap
    _freq_limits: list = field(repr=False, default_factory=list)  # [(plane, max_dbm, reason)]
    _limit_gauges: list = field(repr=False, default_factory=list)  # per-limit gauge info

    def limit_gauges(self) -> list:
        """Per-limit transparency: which plane (and quantity) each safety limit inverts
        against after honouring ``side`` and punching through any ``reported`` planes. Lets
        a UI show ``amp P1dB input → gauged on 'sdr_output' (total in-band power)`` so a
        quantity mismatch is visible at save time rather than as amp compression."""
        return list(self._limit_gauges)

    def _eff_freq(self, freq: Optional[float]) -> Optional[float]:
        return self._freq_hz if freq is None else freq

    def _max_gain_at(self, freq: Optional[float]) -> float:
        """The safety ceiling in gain-space at ``freq``: the tightest of the
        frequency-independent cap and every frequency-dependent limit."""
        caps = [self._gain_ceiling_const]
        for plane, max_dbm, _ in self._freq_limits:
            caps.append(_gain_for_power_on(max_dbm, plane, self._planes, freq, for_limit=True))
        return min(caps)

    def _snap(self, gain: float, freq: Optional[float]) -> float:
        """Clamp to [min_gain, ceiling(freq)] and, when a hardware gain step is set, snap to
        the nearest step on that grid — never above the ceiling (floor there), so
        quantisation can't push a commanded gain past a safety limit."""
        lo, hi = self.min_gain_db, self._max_gain_at(freq)
        step = self._gain_step
        if not step:
            return min(max(float(gain), lo), hi)
        g = round(float(gain) / step) * step
        if g > hi:
            g = math.floor(hi / step) * step
        if g < lo:
            g = math.ceil(lo / step) * step
        return round(g, 6)

    # ── the two functions the script calls ──────────────────────────────────────
    def gain_for_power(self, delivered_dbm: float, freq: Optional[float] = None) -> float:
        """Commanded SDR gain (dB) for a requested power at the operating plane,
        clamped to [min_gain_db, max_gain]. Upward is clamped to the ceiling, never
        extrapolated past it."""
        f = self._eff_freq(freq)
        g = _gain_for_power_on(float(delivered_dbm), self.operating_plane, self._planes, f)
        return self._snap(g, f)

    def power_for_gain(self, gain_db: float, freq: Optional[float] = None) -> float:
        """Delivered power (dBm) at the operating plane for an (actual) commanded
        gain — what the radio really settled on, for the report/banner. The gain is snapped
        to the hardware grid first, so the reported power matches what the SDR will set."""
        f = self._eff_freq(freq)
        g = self._snap(float(gain_db), f)
        return _power_on(self.operating_plane, g, self._planes, f)

    # ── convenience for the script's --power min/max bounds (at the rep. frequency) ─
    @property
    def max_gain_db(self) -> float:
        return self._snap(self._max_gain_at(self._freq_hz), self._freq_hz)

    @property
    def max_power_dbm(self) -> float:
        return self.realize(float("inf"))["power_dbm"]

    @property
    def min_power_dbm(self) -> float:
        return self.realize(float("-inf"))["power_dbm"]

    # ── Active components (programmable gain/attenuation) ─────────────────────────
    #
    # An active component (e.g. a programmable attenuator) sits on a derived plane with a
    # `control` block. On top of its passive baseline delta (already folded by _power_on at
    # 0 dB applied) it adds a variable applied gain on its own grid. We model the whole
    # chain as P = P_base(g) − R, where P_base(g) is the delivered power with every active
    # component at its rest state (max applied gain) and R ≥ 0 is a "reduction budget" the
    # active components spend on their grids. SDR-first: keep the SDR as high as possible
    # (≥ an engagement threshold that keeps it out of its unstable low-gain region) and let
    # the active components trim the rest. Only realizable powers are ever produced.

    def _active_hops(self) -> list:
        """The active derived hops (anchor → operating), in chain order."""
        return [(n, d) for n, d in _hops(self.operating_plane, self._planes) if d.is_active]

    @property
    def has_active(self) -> bool:
        return bool(self._active_hops())

    def _achievable(self, freq: Optional[float]):
        """Build the shared achievable-power resolver at ``freq`` (returns the grid and the
        active-hop list so callers can map each applied gain back to a task/param setting)."""
        from paramkit.achievable import AchievableGrid, Active
        f = self._eff_freq(freq)
        op, planes = self.operating_plane, self._planes
        hops = self._active_hops()
        actives = [Active(d.control.applied_hi, d.control.applied_lo,
                          d.control.step_db, d.control.engage_pct, meta=(n, d))
                   for n, d in hops]
        grid = AchievableGrid(
            power_for_gain=lambda g: _power_on(op, g, planes, f),
            gain_for_power=lambda p: _gain_for_power_on(p, op, planes, f),
            min_gain=self.min_gain_db, ceiling=self._max_gain_at(f),
            gain_step=self._gain_step, actives=actives)
        return grid, actives

    def realize(self, power: float, freq: Optional[float] = None) -> dict:
        """SDR-first realization of a requested delivered power. Returns the nearest
        ACHIEVABLE power and the device settings that produce it: the SDR gain, and per
        active component its applied gain and the parameter value to command on its task."""
        grid, actives = self._achievable(freq)
        res = grid.realize(power)
        settings = []
        for a, applied in zip(actives, res["applied"]):
            name, d = a.meta
            settings.append({"plane": name, "task": d.control.task,
                             "param": d.control.param, "applied_db": applied,
                             "value": round(d.control.param_for_applied(applied), 6)})
        return {"power_dbm": res["power_dbm"], "sdr_gain_db": res["sdr_gain_db"],
                "settings": settings}

    def snap_power(self, power: float, freq: Optional[float] = None) -> float:
        """The nearest achievable delivered power to ``power``."""
        return self._achievable(freq)[0].snap(power)

    def quantize_up(self, power: float, freq: Optional[float] = None) -> float:
        return self._achievable(freq)[0].quantize_up(power)

    def quantize_down(self, power: float, freq: Optional[float] = None) -> float:
        return self._achievable(freq)[0].quantize_down(power)

    def active_components(self) -> list:
        """Public descriptors for each active component (for the artifact + UI)."""
        return [d.control.to_public_dict(name, list(d.table))
                for name, d in self._active_hops()]

    def banner_label(self) -> str:
        """e.g. 'EIRP, at antenna_eirp' — so the --power number is never ambiguous."""
        q = self.operating_quantity or "power"
        return f"{q}, at {self.operating_plane}"

    def operating_curve(self, freq: Optional[float] = None) -> list:
        """The operating-plane transfer as a flat, gain-sorted ``[[gain, power], …]``
        table at the anchor's measured breakpoints (derived hops folded in at ``freq``).
        A v1 script consumes this directly; a v2 script prefers ``anchor_curve`` +
        ``passive_hops`` so it can re-fold at its live frequency."""
        f = self._eff_freq(freq)
        _, anchor = _anchor(self.operating_plane, self._planes, f)
        return [[g, _power_on(self.operating_plane, g, self._planes, f)]
                for g in anchor.gains]

    def anchor_curve(self) -> list:
        """The operating plane's measured anchor curve (gain → power, offset folded in),
        BEFORE any passive hop — the base a v2 consumer re-folds ``passive_hops`` onto."""
        _, m = _anchor(self.operating_plane, self._planes, self._freq_hz)
        return [[g, m.power_at(g)] for g in m.gains]

    def passive_hops(self) -> list:
        """The ordered passive hops (anchor → operating), each with its frequency table.
        Empty when the operating plane is measured (no cable/antenna). Transparent
        fallback hops (a measured stage this signal skipped) are omitted — they aren't
        real parts and contribute 0 dB."""
        return [{"plane": name, "component": d.component or None,
                 "delta_db_by_freq": [[f, db] for f, db in d.table]}
                for name, d in _hops(self.operating_plane, self._planes)
                if not d.fallback]

    def _limit_anchor_curve(self, plane_name: str) -> Optional[list]:
        """The measured LIMITING curve a frequency-dependent limit inverts against, when it
        differs from the operating plane's own (observed) anchor — i.e. a REPORTED operating
        plane, whose observed curve isn't the one the limit gauges on. Returned as a flat
        ``[[gain, power], …]`` so a consumer inverts the limit against it directly. None when
        the limit shares the operating anchor (the shared ``anchor_curve`` already serves).
        The limit's ``delta_db_by_freq`` is valid against this curve too: a reported node and
        its limiting node are 0 dB apart, so the passive delta from either is identical."""
        op_anchor = _anchor_plane(self.operating_plane, self._planes)
        lim_anchor = _anchor_plane(plane_name, self._planes, for_limit=True)
        if lim_anchor is op_anchor:
            return None
        return [[g, lim_anchor.power_at(g)] for g in lim_anchor.gains]

    def _plane_delta_table(self, plane_name: str) -> list:
        """The TOTAL passive delta from the measured anchor out to ``plane_name`` as one
        ``[[freq, delta], …]`` table (all its hops summed). A consumer inverts a limit on
        this plane by subtracting this from its threshold and inverting the shared
        anchor curve — so it needs no plane model of its own."""
        return [[f, d] for f, d in _sum_tables([dv.table for _, dv in _hops(plane_name, self._planes)])]

    def to_public_dict(self) -> dict:
        """The resolved artifact the agent writes for a task to consume.

        Always carries the v1 fields (``curve`` folded at the representative frequency,
        gain clamps, amplitude, quantity) so existing scripts keep working unchanged.
        When the operating point moves with frequency — the operating plane sits behind
        passive hops, OR a frequency-dependent safety limit tightens the ceiling per
        frequency — it ALSO carries the v2 fields (``anchor_curve``, ``passive_hops``, the
        split ceiling) so a frequency-aware consumer can re-fold at its live transmit
        frequency (docs/calibration-v2.md). A frequency-dependent limit alone (with a
        MEASURED operating plane, so no passive hops) still needs these: the max power moves
        with frequency even though the operating curve doesn't."""
        out = {
            "schema_version": SCHEMA_VERSION,
            "signal_id": self.signal_id,
            "unit_type": self.unit_type,
            "operating_plane": self.operating_plane,
            "quantity": self.operating_quantity,
            "amplitude": self.amplitude,
            "min_gain_db": self.min_gain_db,
            "max_gain_db": self.max_gain_db,
            "min_power_dbm": self.min_power_dbm,
            "max_power_dbm": self.max_power_dbm,
            "curve": self.operating_curve(),
        }
        if self._gain_step:
            out["gain_step_db"] = self._gain_step
        if self.has_active:
            out["active_components"] = self.active_components()
        hops = self.passive_hops()
        if hops or self._freq_limits:
            out["anchor_curve"] = self.anchor_curve()
            out["passive_hops"] = hops
            # Each frequency-dependent limit carries its own summed delta from the shared
            # anchor, so a consumer inverts it against the same anchor_curve at the live
            # frequency (no plane model needed script-side).
            fdl = []
            for p, mx, rs in self._freq_limits:
                entry = {"plane": p, "max_dbm": mx, "reason": rs,
                         "delta_db_by_freq": self._plane_delta_table(p)}
                lac = self._limit_anchor_curve(p)     # its own limiting curve, if it differs
                if lac is not None:
                    entry["anchor_curve"] = lac       # invert THIS limit against this curve
                fdl.append(entry)
            out["freq_dependent_limits"] = fdl
            if self._gain_ceiling_const != float("inf"):
                out["gain_ceiling_db"] = self._gain_ceiling_const
            if self._freq_hz is not None:
                out["center_freq_hz"] = self._freq_hz
        return out


# ── Linear interpolation (pure python — the agent has no numpy dependency) ───────

def _interp(x: float, xs: list[float], ys: list[float]) -> float:
    """Piecewise-linear interpolation of y(x) over strictly-increasing xs, with
    endpoint clamping (x outside [xs[0], xs[-1]] returns the nearest endpoint's y).
    A single sample degrades to a slope-1 line through it (dB-for-dB), matching the
    single-point fallback in the spec."""
    n = len(xs)
    if n == 1:
        return ys[0] + (x - xs[0])            # slope-1 fallback (1 dB gain ≈ 1 dB power)
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    # binary-free scan is fine: curves are a handful of points.
    for i in range(1, n):
        if x <= xs[i]:
            x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return ys[-1]                              # unreachable (x < xs[-1] handled above)


# ── Plane traversal (mirrors docs/calibration.md §7.3) ───────────────────────────

def _power_on(plane_name: str, gain: float, planes: dict, freq: Optional[float]) -> float:
    """Power at ``plane_name`` for a commanded gain, walking derived hops down to a
    measured curve. Each derived hop is evaluated at ``freq``."""
    p = planes[plane_name]
    if isinstance(p, _Measured):
        return p.power_at(gain)
    return _power_on(p.frm, gain, planes, freq) + p.delta_at(freq)


def _anchor(plane_name: str, planes: dict, freq: Optional[float],
            for_limit: bool = False) -> tuple[float, _Measured]:
    """Walk derived hops down to the nearest measured ancestor, accumulating the
    total derived delta (evaluated at ``freq``). Returns (delta, measured_plane).

    ``for_limit`` makes the walk transparent through ``reported`` planes: a reported
    plane is a re-measurement of its ``of`` node (0 dB between them), so a limit gauged
    on the node's quantity punches through to that ``limiting`` curve instead of stopping
    on the reported one. The observed walk (``for_limit=False``) stops at the reported
    plane and uses its own curve."""
    delta, p = 0.0, planes[plane_name]
    seen: set[int] = set()
    while True:
        if isinstance(p, _Derived):
            delta += p.delta_at(freq)
            p = planes[p.frm]
        elif for_limit and p.is_reported and p.of:
            if id(p) in seen:                # of-cycle guard (validation forbids this)
                break
            seen.add(id(p))
            p = planes[p.of]
        else:
            break
    return delta, p


def _hops(plane_name: str, planes: dict) -> list:
    """The ordered derived hops from the measured anchor OUT to ``plane_name``, as
    ``[(plane_name, _Derived), …]`` (anchor-first). Empty if the plane is measured."""
    chain = []
    name, p = plane_name, planes[plane_name]
    while isinstance(p, _Derived):
        chain.append((name, p))
        name, p = p.frm, planes[p.frm]
    chain.reverse()
    return chain


def _path_freq_dependent(plane_name: str, planes: dict) -> bool:
    """True if any derived hop between ``plane_name`` and its measured anchor varies
    with frequency (a multi-point component table)."""
    return any(d.is_freq_dependent for _, d in _hops(plane_name, planes))


def _anchor_plane(plane_name: str, planes: dict, for_limit: bool = False) -> _Measured:
    """The measured plane a chain resolves down to, WITHOUT evaluating any delta (so it
    is safe on a frequency-dependent path where the delta needs a frequency). ``for_limit``
    punches through ``reported`` planes to the ``limiting`` curve they re-measure."""
    p = planes[plane_name]
    seen: set[int] = set()
    while True:
        if isinstance(p, _Derived):
            p = planes[p.frm]
        elif for_limit and p.is_reported and p.of and id(p) not in seen:
            seen.add(id(p))
            p = planes[p.of]
        else:
            break
    return p


_LIMIT_SIDES = ("input", "output")


def _upstream_plane(plane_name: str, planes: dict) -> str:
    """The plane feeding INTO ``plane_name``'s stage — one hop upstream in the cascade.
    A stage's output is the plane itself; its input is the plane before it. For a
    derived (passive) plane that upstream plane is its ``from``; for a measured plane it
    is the plane immediately preceding it in cascade order — the same insertion order the
    partial-stage fallback already relies on. The first stage has nothing upstream."""
    p = planes[plane_name]
    if isinstance(p, _Derived):
        return p.frm
    keys = list(planes)
    i = keys.index(plane_name)
    if i == 0:
        raise CalibrationError(
            f"input-side limit on {plane_name!r} has no upstream stage (it is the first "
            f"plane in the chain)")
    return keys[i - 1]


def _limit_plane(lim: dict, planes: dict) -> str:
    """The plane a limit's cap actually applies at, honouring its ``side``. ``output``
    (the default) is the named plane — the stage's output. ``input`` is the plane feeding
    that stage, so an input-protection limit (e.g. an amplifier's max input power) follows
    the stage when a component is inserted upstream, instead of naming a fixed plane that
    silently detaches. Validates the plane reference and the side."""
    plane = lim.get("plane")
    if plane not in planes:
        raise CalibrationError(f"limit references unknown plane {plane!r}")
    side = lim.get("side", "output")
    if side not in _LIMIT_SIDES:
        raise CalibrationError(
            f"limit on {plane!r} has invalid side {side!r} (expected 'input' or 'output')")
    return _upstream_plane(plane, planes) if side == "input" else plane


def _eval_table(table: list, freq: Optional[float]) -> float:
    """One delta table's value at ``freq`` (constant if single-point)."""
    if len(table) == 1:
        return table[0][1]
    fs = [f for f, _ in table]
    ds = [d for _, d in table]
    return _interp(float(freq), fs, ds)


def _sum_tables(tables: list) -> list:
    """Sum several ``[(freq, delta), …]`` tables into one, exactly. The sum of
    piecewise-linear functions is piecewise-linear with breakpoints at the UNION of the
    inputs' breakpoints, so sampling the sum at every multi-point breakpoint (or, if all
    inputs are constant, at a single 0 Hz point) reconstructs it without loss."""
    if not tables:
        return [(0.0, 0.0)]
    multi_freqs = sorted({f for t in tables if len(t) > 1 for f, _ in t})
    if not multi_freqs:
        return [(0.0, sum(t[0][1] for t in tables))]     # every hop constant → one point
    return [(fr, sum(_eval_table(t, fr) for t in tables)) for fr in multi_freqs]


def _gain_for_power_on(power: float, plane_name: str, planes: dict,
                       freq: Optional[float], for_limit: bool = False) -> float:
    """Gain that yields ``power`` at ``plane_name``. Subtract downstream derived
    deltas (at ``freq``) to reach the anchor measured plane, then invert its curve
    once. Clamps at the measured range — upward to the top gain (never extrapolated
    past the ceiling), downward to the bottom gain. ``for_limit`` gauges a safety limit:
    the walk punches through ``reported`` planes to the ``limiting`` curve (§4.1)."""
    delta, m = _anchor(plane_name, planes, freq, for_limit=for_limit)
    target = power - delta - m.offset_db
    if len(m.powers) == 1:
        return m.gains[0] + (target - m.powers[0])         # slope-1 inverse
    if target >= m.powers[-1]:
        return m.gains[-1]
    if target <= m.powers[0]:
        return m.gains[0]
    return _interp(target, m.powers, m.gains)              # powers monotonic → unambiguous


def _breakpoint_freqs(plane_name: str, planes: dict) -> list:
    """The distinct frequency breakpoints of every frequency-dependent hop feeding
    ``plane_name`` (sorted; empty when none of its hops vary with frequency)."""
    fs: set = set()
    for _, d in _hops(plane_name, planes):
        if d.is_freq_dependent:
            fs.update(float(f) for f, _ in d.table)
    return sorted(fs)


def _representative_freq(planes: dict, operating_plane: str, freq_limits: list,
                         gain_ceiling_const: float) -> Optional[float]:
    """A representative frequency to fold the scalar read-outs and the v1-compat artifact
    curve at when the signal declares no ``center_freq_hz`` — the transmit frequency is a
    runtime quantity the task supplies via ``--freq``. A frequency-aware (v2) consumer
    still re-folds at its live frequency from the published ``passive_hops`` /
    ``freq_dependent_limits``; this only fixes the operating point of the flat artifact for
    a v1 script that folds no frequency of its own.

    When frequency-dependent SAFETY limits exist the pick must be conservative: that v1
    script would fold its flat ceiling here, so choose the breakpoint whose gain ceiling is
    TIGHTEST (lowest), guaranteeing the fallback never exceeds a per-frequency limit. The
    combined ceiling is piecewise-linear in frequency, so its minimum over the operating
    band is reached at one of the union of the limits' (and operating plane's) breakpoints.
    With no frequency-dependent limits the choice only shifts the reported operating point,
    so the midpoint of the operating plane's breakpoints is a fair representative value.
    Returns None when no breakpoints exist (nothing to evaluate at)."""
    if freq_limits:
        cands: set = set()
        for plane, _mx, _rs in freq_limits:
            cands.update(_breakpoint_freqs(plane, planes))
        cands.update(_breakpoint_freqs(operating_plane, planes))
        if not cands:
            return None

        def ceiling_at(fr: float) -> float:
            caps = [gain_ceiling_const]
            for plane, mx, _rs in freq_limits:
                caps.append(_gain_for_power_on(mx, plane, planes, fr, for_limit=True))
            return min(caps)

        return min(cands, key=ceiling_at)
    fs = _breakpoint_freqs(operating_plane, planes)
    if not fs:
        return None
    return 0.5 * (fs[0] + fs[-1])


# ── Merge (docs/calibration.md §7.1) ─────────────────────────────────────────────

def _deep_merge(base: dict, override: dict) -> dict:
    """Per-key deep merge; ``override`` wins. Scalars and whole lists are replaced
    (a points array or a limits list is never element-wise merged). Neither input is
    mutated."""
    out = copy.deepcopy(base) if base else {}
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# ── Resolution ───────────────────────────────────────────────────────────────────

def resolve(unit_doc: dict,
            type_defaults: Optional[dict],
            signal_id: str,
            components: Optional[dict] = None,
            freq_hz: Optional[float] = None) -> ResolvedCalibration:
    """Resolve the calibration for one (unit, signal).

    ``unit_doc``      the parsed per-unit calibration.json.
    ``type_defaults`` the ``types[unit_type]`` section from the shared
                      calibration_defaults file (chain/defaults skeleton), or None.
    ``signal_id``     the script's stable CAL_SIGNAL_ID.
    ``components``    the shared component catalog ``{id: {delta_db_by_freq, …}}`` a
                      derived plane may reference (docs/calibration-v2.md), or None.
    ``freq_hz``       representative frequency to evaluate frequency-dependent hops at
                      for the scalar read-outs / the v1-compat artifact curve; falls
                      back to the signal's ``center_freq_hz``.

    Raises :class:`SignalNotCalibrated` if the signal is absent, or
    :class:`CalibrationError` for any hard defect. Otherwise returns a
    :class:`ResolvedCalibration`.
    """
    if not isinstance(unit_doc, dict):
        raise CalibrationError("calibration document is not an object")
    if unit_doc.get("schema_version") != SCHEMA_VERSION:
        raise CalibrationError(
            f"unsupported schema_version {unit_doc.get('schema_version')!r} "
            f"(expected {SCHEMA_VERSION})")

    unit_type = unit_doc.get("unit_type", "")
    td = type_defaults or {}

    # 1. chain = type.chain  ⊕  unit.chain      (unit wins)
    chain = _deep_merge(td.get("chain", {}), unit_doc.get("chain", {}))
    defaults = _deep_merge(td.get("defaults", {}), unit_doc.get("defaults", {}))
    planes_spec = chain.get("planes")
    if not isinstance(planes_spec, dict) or not planes_spec:
        raise CalibrationError("chain.planes is missing or empty")
    operating_plane = chain.get("operating_plane")
    if not operating_plane:
        raise CalibrationError("chain.operating_plane is missing")

    # 2. locate the signal
    signals = unit_doc.get("signals")
    if not isinstance(signals, dict) or signal_id not in signals:
        raise SignalNotCalibrated(f"no calibration for signal {signal_id!r}")
    sig = signals[signal_id] or {}
    curves = sig.get("curves") or {}
    if not isinstance(curves, dict):
        raise CalibrationError(f"signals.{signal_id}.curves is not an object")
    amplitude = sig.get("amplitude", defaults.get("amplitude"))
    # Representative frequency: explicit arg wins, else the signal's center_freq_hz.
    rep = freq_hz if freq_hz is not None else sig.get("center_freq_hz")
    rep_freq = float(rep) if rep is not None else None

    # 3. build planes, attaching this signal's curves into the measured ones and
    #    resolving derived planes' hops (inline delta_db or a catalog component).
    planes = _build_planes(planes_spec, curves, signal_id, components or {})

    # 4. validate topology + operating plane usability
    _validate_topology(planes, operating_plane)

    # 5. gain bounds + ceiling, split into a frequency-independent part (the tightest
    #    that never moves — e.g. the amp-protection limit on the MEASURED sdr_output)
    #    and any limits whose plane sits behind a passive hop (frequency-dependent).
    gl = chain.get("gain_limits") or {}
    gmin = float(gl.get("min_gain_db", 0.0))
    gstep = gl.get("gain_step_db")
    if gstep is not None:
        gstep = float(gstep)
        if gstep <= 0:
            raise CalibrationError(
                f"gain_step_db must be positive, got {gstep:g}")
    const_caps: list = []
    freq_limits: list = []
    limit_gauges: list = []
    if gl.get("max_gain_db") is not None:
        const_caps.append(float(gl["max_gain_db"]))
    for lim in (chain.get("limits") or []):
        plane = _limit_plane(lim, planes)            # honour side: input → one hop upstream
        anchor = _require_usable(plane, planes, for_limit=True)   # gauge on the LIMITING curve
        limit_gauges.append({
            "reason": lim.get("reason", ""), "max_dbm": float(lim["max_dbm"]),
            "at_plane": plane, "gauge_plane": _plane_name(anchor, planes),
            "gauge_quantity": anchor.quantity})
        if _path_freq_dependent(plane, planes):
            freq_limits.append((plane, float(lim["max_dbm"]), lim.get("reason", "")))
        else:
            const_caps.append(
                _gain_for_power_on(float(lim["max_dbm"]), plane, planes, None, for_limit=True))
    if not const_caps and not freq_limits:
        raise CalibrationError("no safety ceiling derivable — refusing to transmit")
    gain_ceiling_const = min(const_caps) if const_caps else float("inf")

    # A frequency-dependent limit is inverted at runtime against a measured anchor curve.
    # The common case shares the operating plane's own anchor. When the operating plane is
    # REPORTED, its observed anchor (the reported curve) differs from the curve the limit
    # gauges on (the limiting curve) — but they are the same physical node, so the limit
    # still inverts cleanly against its own limiting curve, which the artifact publishes
    # per-limit (to_public_dict). Only refuse when the limit resolves through a genuinely
    # DIFFERENT measured plane than the operating one (no shared base for its delta).
    if freq_limits:
        op_anchor = _anchor_plane(operating_plane, planes)                    # observed anchor
        op_lim_anchor = _anchor_plane(operating_plane, planes, for_limit=True)  # its limiting curve
        for plane, _mx, _rs in freq_limits:
            lim_anchor = _anchor_plane(plane, planes, for_limit=True)         # the limiting curve
            if lim_anchor is op_anchor or lim_anchor is op_lim_anchor:
                continue                                                       # shares a base — OK
            raise CalibrationError(
                f"frequency-dependent limit on {plane!r} resolves through a different "
                f"measured plane than the operating plane {operating_plane!r}; this "
                f"topology isn't supported")

    # A frequency-dependent operating plane or ceiling needs a representative frequency,
    # so the scalar read-outs and the v1-compat artifact curve have a defined operating
    # point (a v2 consumer still re-folds per its live transmit frequency). The transmit
    # frequency is a runtime quantity — a task's --freq — so an absent center_freq_hz is
    # not an error: derive a representative one (worst-case tightest ceiling when there are
    # frequency-dependent safety limits, so a v1 script folding no frequency stays safe).
    if (freq_limits or _path_freq_dependent(operating_plane, planes)) and rep_freq is None:
        rep_freq = _representative_freq(planes, operating_plane, freq_limits,
                                        gain_ceiling_const)
        if rep_freq is None:
            raise CalibrationError(
                f"signal {signal_id!r} uses a frequency-dependent component but has no "
                f"'center_freq_hz' and no frequency breakpoints to derive a representative "
                f"operating frequency from")

    op_quantity = _quantity_of(planes[operating_plane])
    resolved = ResolvedCalibration(
        signal_id=signal_id, unit_type=unit_type, amplitude=amplitude,
        min_gain_db=gmin, operating_plane=operating_plane, operating_quantity=op_quantity,
        _planes=planes, _freq_hz=rep_freq, _gain_step=gstep,
        _gain_ceiling_const=gain_ceiling_const, _freq_limits=freq_limits,
        _limit_gauges=limit_gauges)
    if resolved.max_gain_db < gmin:
        raise CalibrationError(
            f"resolved max gain {resolved.max_gain_db:.2f} dB is below min gain "
            f"{gmin:.2f} dB")
    return resolved


def validate_chain_structure(unit_doc: dict, type_defaults: Optional[dict] = None,
                             components: Optional[dict] = None) -> None:
    """Curve-independent structural check of the RF chain, for a document that has no
    signals yet (onboarding: the chain and its safety ceiling are set up *before* any
    signal is measured — docs/calibration.md §9.2). Validates everything that does not
    depend on a measured curve — plane topology, each derived hop (inline ``delta_db``
    or a catalog ``component`` and its frequency table), the operating plane's
    existence, limit plane references, and that a safety ceiling is *declared* — and
    raises :class:`CalibrationError` on any defect. Curve-dependent checks (operating
    plane usability, a derivable ceiling) can't run without points, so they wait until
    a signal is present and :func:`resolve` covers them per-signal."""
    td = type_defaults or {}
    chain = _deep_merge(td.get("chain", {}), unit_doc.get("chain", {}))
    planes_spec = chain.get("planes")
    if not isinstance(planes_spec, dict) or not planes_spec:
        raise CalibrationError("chain.planes is missing or empty")
    operating_plane = chain.get("operating_plane")
    if not operating_plane:
        raise CalibrationError("chain.operating_plane is missing")

    # Build with no curves: this validates every plane's structure (measured/derived
    # type, a derived plane's 'from' + exactly one of component/delta_db, a component's
    # existence and its frequency table) without requiring any measured points.
    planes = _build_planes(planes_spec, {}, "", components or {})

    # Curve-independent subset of _validate_topology: 'from' references resolve, the
    # operating plane exists, and the derived graph is acyclic. (Usability — that the
    # anchor has points — needs a curve, so it waits for a signal.)
    if operating_plane not in planes:
        raise CalibrationError(f"operating_plane {operating_plane!r} does not exist")
    _validate_role_refs(planes)
    for name, p in planes.items():
        if isinstance(p, _Derived) and p.frm not in planes:
            raise CalibrationError(f"plane {name!r} references unknown plane {p.frm!r}")
        seen, cur = set(), name
        while isinstance(planes.get(cur), _Derived):
            if cur in seen:
                raise CalibrationError(f"derived plane cycle through {cur!r}")
            seen.add(cur)
            cur = planes[cur].frm
            if cur not in planes:
                break

    # A ceiling can't be *derived* without curves, but its omission is the key safety
    # footgun — flag it now rather than at the first signal.
    gl = chain.get("gain_limits") or {}
    if gl.get("max_gain_db") is None and not chain.get("limits"):
        raise CalibrationError("no safety ceiling — set a max gain or add at least one limit")
    if gl.get("gain_step_db") is not None and float(gl["gain_step_db"]) <= 0:
        raise CalibrationError(
            f"gain_step_db must be positive, got {float(gl['gain_step_db']):g}")
    for lim in (chain.get("limits") or []):
        if not isinstance(lim, dict):
            raise CalibrationError(f"limit is not an object: {lim!r}")
        _limit_plane(lim, planes)          # validates plane ref, side, and input upstream


def validate_document(unit_doc: dict, type_defaults: Optional[dict] = None,
                      components: Optional[dict] = None) -> dict:
    """Structural validation of a WHOLE calibration document, as it would resolve at
    runtime — for validate-on-upload (docs/calibration.md §9.2).

    Runs :func:`resolve` for every signal in the document (so every measured curve,
    every plane / component reference, the ceiling, and the operating plane are checked
    exactly as at transmit time) and raises :class:`CalibrationError` on any
    document-level defect or any invalid signal. ``components`` is the shared catalog a
    derived plane may reference. On success returns a per-signal summary dict:
    ``{signal_id: {operating_plane, quantity, min/max gain & power}}`` — handy for a
    UI to show what each signal resolved to.
    """
    if not isinstance(unit_doc, dict):
        raise CalibrationError("calibration document is not an object")
    if unit_doc.get("schema_version") != SCHEMA_VERSION:
        raise CalibrationError(
            f"unsupported schema_version {unit_doc.get('schema_version')!r} "
            f"(expected {SCHEMA_VERSION})")
    signals = unit_doc.get("signals")
    if signals is None:
        signals = {}
    if not isinstance(signals, dict):
        raise CalibrationError("signals must be an object")
    if not signals:
        # No signals measured yet (onboarding): validate the chain structure so a
        # broken chain is still rejected, then accept the signal-less document. Nothing
        # can transmit until a signal is added — resolve() raises SignalNotCalibrated
        # for an absent signal — so persisting the chain + ceiling skeleton is safe.
        validate_chain_structure(unit_doc, type_defaults, components)
        return {}

    summary, bad = {}, {}
    for sig_id in signals:
        try:
            r = resolve(unit_doc, type_defaults, sig_id, components)
            summary[sig_id] = {
                "operating_plane": r.operating_plane,
                "quantity": r.operating_quantity,
                "amplitude": r.amplitude,
                "min_gain_db": r.min_gain_db, "max_gain_db": r.max_gain_db,
                "min_power_dbm": r.min_power_dbm, "max_power_dbm": r.max_power_dbm,
                "limit_gauges": r.limit_gauges(),
                # The full resolved artifact (v1 curve + v2 anchor/hops/limits) so a
                # frequency-aware client re-folds the --power range at the frequency the
                # operator picks, the same fold the transmit script does (calkit).
                "artifact": r.to_public_dict(),
            }
        except CalibrationError as exc:
            bad[sig_id] = str(exc)
    if bad:
        # If every signal failed with the SAME message, the defect is document-level
        # (a bad chain/plane the per-signal resolve surfaces each time) — report it
        # once, plainly, instead of blaming each signal for one structural problem.
        msgs = set(bad.values())
        if len(msgs) == 1 and len(bad) == len(signals):
            raise CalibrationError(next(iter(msgs)))
        raise CalibrationError(
            "invalid signal(s): " + "; ".join(f"{k} ({v})" for k, v in bad.items()))
    return summary


def _freq_table(raw, ctx: str) -> list:
    """Validate + sort a ``[[freq_hz, delta_db], …]`` table: ≥1 point, strictly
    increasing in frequency. Signed dB (negative = loss). One point ⇒ a constant hop."""
    if not isinstance(raw, list) or not raw:
        raise CalibrationError(f"{ctx}: delta_db_by_freq must be a non-empty list")
    try:
        pts = sorted(((float(f), float(d)) for f, d in raw), key=lambda fd: fd[0])
    except (TypeError, ValueError) as exc:
        raise CalibrationError(f"{ctx}: malformed delta_db_by_freq point: {exc}")
    for i in range(1, len(pts)):
        if pts[i][0] <= pts[i - 1][0]:
            raise CalibrationError(
                f"{ctx}: delta_db_by_freq frequencies not strictly increasing "
                f"near {pts[i][0]:g} Hz")
    return pts


def _build_planes(planes_spec: dict, curves: dict, signal_id: str,
                  components: dict) -> dict:
    """Turn the chain's plane specs (topology, no points) plus the signal's curves
    into resolved _Measured / _Derived objects. A derived plane's hop comes from an
    inline ``delta_db`` (constant) or a catalog ``component`` (a frequency table)."""
    # every curve must name a measured plane in the chain
    for name in curves:
        p = planes_spec.get(name)
        if not isinstance(p, dict):
            raise CalibrationError(
                f"curve for unknown plane {name!r} in signal {signal_id!r}")
        if p.get("type") != "measured":
            raise CalibrationError(
                f"curve given for derived plane {name!r} in signal {signal_id!r}")

    planes: dict = {}
    prev_name: Optional[str] = None          # the stage immediately before this one
    for name, spec in planes_spec.items():
        if not isinstance(spec, dict):
            raise CalibrationError(f"plane {name!r} is not an object")
        ptype = spec.get("type")
        quantity = spec.get("quantity", "")
        description = spec.get("description", "")
        if ptype == "measured":
            role = spec.get("role", "limiting")
            if role not in ("limiting", "reported"):
                raise CalibrationError(
                    f"plane {name!r} has invalid role {role!r} "
                    f"(expected 'limiting' or 'reported')")
            of = spec.get("of", "")
            if role == "reported":
                if not of:
                    raise CalibrationError(
                        f"reported plane {name!r} must set 'of' to the limiting plane it "
                        f"re-measures (the source/root plane can't be 'reported')")
            elif of:
                raise CalibrationError(
                    f"plane {name!r} sets 'of' but is not 'reported' "
                    f"('of' names the limiting plane a reported plane re-measures)")
            curve = curves.get(name)
            if curve is None:
                # Latent: declared measured but not measured for THIS signal. If a stage
                # precedes it, fall through to that stage with a transparent +0 dB hop, so
                # a signal measured only upstream still resolves — the operating point
                # inherits the nearest upstream measured curve (a "partial measured
                # stage": you can add a downstream measured plane for a signal or two
                # without re-measuring all the rest). The FIRST stage has nothing upstream,
                # so a latent source stays latent and _require_usable flags it clearly.
                # This applies to a reported stage too: a signal not measured there passes
                # straight through to the upstream (limiting) curve — it just reports the
                # upstream quantity for that signal, while safety limits are unaffected
                # (they already gauge on that upstream curve).
                if prev_name is not None:
                    planes[name] = _Derived(frm=prev_name, table=[(0.0, 0.0)],
                                            fallback=True, quantity=quantity,
                                            description=description)
                else:
                    planes[name] = _Measured(gains=[], powers=[], quantity=quantity,
                                             description=description, role=role, of=of)
            else:
                gains, powers = _curve_points(curve, name)
                planes[name] = _Measured(
                    gains=gains, powers=powers,
                    offset_db=float(curve.get("offset_db", 0.0)),
                    quantity=quantity, description=description, role=role, of=of)
            prev_name = name
            continue
        if ptype == "derived":
            frm = spec.get("from")
            if not frm:
                raise CalibrationError(f"derived plane {name!r} has no 'from'")
            has_comp = "component" in spec
            has_delta = "delta_db" in spec
            has_table = "delta_db_by_freq" in spec        # inline Δ dB(f) table (owns its own)
            n_baseline = has_comp + has_delta + has_table
            if n_baseline > 1:
                raise CalibrationError(
                    f"derived plane {name!r} has more than one of 'component', 'delta_db', "
                    f"'delta_db_by_freq' (use exactly one)")
            if n_baseline == 0:
                raise CalibrationError(
                    f"derived plane {name!r} has none of 'component', 'delta_db', "
                    f"'delta_db_by_freq'")
            comp_id = ""
            if has_comp:
                comp_id = spec["component"]
                comp = components.get(comp_id)
                if not isinstance(comp, dict):
                    raise CalibrationError(
                        f"derived plane {name!r} references unknown component "
                        f"{comp_id!r}")
                table = _freq_table(comp.get("delta_db_by_freq"),
                                    f"component {comp_id!r}")
            elif has_table:                               # the plane's OWN frequency table
                table = _freq_table(spec["delta_db_by_freq"], f"plane {name!r}")
            else:
                table = [(0.0, float(spec["delta_db"]))]   # constant, frequency-independent
            # An ACTIVE component adds a `control` block on top of its passive baseline.
            control = _parse_control(spec["control"], name) if "control" in spec else None
            planes[name] = _Derived(frm=frm, table=table, component=comp_id,
                                    quantity=quantity, description=description,
                                    control=control)
            prev_name = name
        else:
            raise CalibrationError(f"plane {name!r} has invalid type {ptype!r}")
    return planes


def _curve_points(curve: dict, name: str) -> tuple[list[float], list[float]]:
    """Extract and validate a measured plane's points: ≥1 point, strictly increasing
    in BOTH gain and power (so the curve is invertible)."""
    pts = curve.get("points")
    if not isinstance(pts, list) or not pts:
        raise CalibrationError(f"plane {name!r} has no points")
    try:
        pairs = sorted(((float(p["gain_db"]), float(p["power_dbm"])) for p in pts),
                       key=lambda gp: gp[0])
    except (KeyError, TypeError, ValueError) as exc:
        raise CalibrationError(f"plane {name!r} has a malformed point: {exc}")
    gains = [g for g, _ in pairs]
    powers = [p for _, p in pairs]
    for i in range(1, len(pairs)):
        if gains[i] <= gains[i - 1]:
            raise CalibrationError(
                f"plane {name!r} gains not strictly increasing at {gains[i]:g} dB")
        if powers[i] <= powers[i - 1]:
            raise CalibrationError(
                f"plane {name!r} power not strictly increasing with gain "
                f"(not invertible) near {gains[i]:g} dB")
    return gains, powers


def _validate_role_refs(planes: dict) -> None:
    """Each ``reported`` plane's ``of`` must name an existing ``limiting`` measured plane —
    the curve its limits punch through to. Pointing at a derived plane, a missing plane, or
    another reported plane is refused (a reported plane never backs a limit)."""
    for name, p in planes.items():
        if isinstance(p, _Measured) and p.is_reported:
            target = planes.get(p.of)
            if target is None:
                raise CalibrationError(
                    f"reported plane {name!r} re-measures unknown plane {p.of!r}")
            if not isinstance(target, _Measured) or target.is_reported:
                raise CalibrationError(
                    f"reported plane {name!r} must re-measure a 'limiting' plane, but "
                    f"{p.of!r} is not one")


def _validate_topology(planes: dict, operating_plane: str) -> None:
    """Every 'from' resolves, the derived graph is acyclic and ends at a measured
    plane, and the operating plane has a usable transfer for this signal."""
    for name, p in planes.items():
        if isinstance(p, _Derived) and p.frm not in planes:
            raise CalibrationError(f"plane {name!r} references unknown plane {p.frm!r}")
    _validate_role_refs(planes)
    if operating_plane not in planes:
        raise CalibrationError(f"operating_plane {operating_plane!r} does not exist")
    # walk from operating plane to its measured anchor, guarding against cycles
    _require_usable(operating_plane, planes)


def _require_usable(plane_name: str, planes: dict, for_limit: bool = False) -> _Measured:
    """Return the measured anchor of ``plane_name`` if it has points; raise otherwise.
    Detects cycles in the derived chain. ``for_limit`` punches through ``reported`` planes
    to the ``limiting`` curve, so a limit is validated against the curve it will actually
    invert on (not the reported one it passes through)."""
    seen: set[str] = set()
    p = planes[plane_name]
    while True:
        if isinstance(p, _Derived):
            if plane_name in seen:
                raise CalibrationError(f"derived plane cycle through {plane_name!r}")
            seen.add(plane_name)
            plane_name = p.frm
            p = planes[plane_name]
        elif for_limit and p.is_reported and p.of:
            if plane_name in seen:
                raise CalibrationError(f"reported-plane cycle through {plane_name!r}")
            seen.add(plane_name)
            plane_name = p.of
            p = planes[plane_name]
        else:
            break
    if not p.gains:                                  # measured but latent (no points)
        raise CalibrationError(
            f"plane {plane_name!r} has no measured curve for this signal "
            f"(measure it, or point operating_plane / limits elsewhere)")
    return p


def _quantity_of(plane) -> str:
    return plane.quantity if plane.quantity else ""


def _plane_name(plane, planes: dict) -> str:
    """The dict key of a resolved plane object (for reporting which curve a limit hit)."""
    return next((n for n, p in planes.items() if p is plane), "")


# ── File loaders (thin; the pure resolver above stays I/O-free & unit-testable) ──

def load_type_defaults(path, unit_type: str) -> Optional[dict]:
    """Read the shared calibration_defaults file (JSON or YAML) and return the
    ``types[unit_type]`` section, or None if the file/section is absent. A malformed
    file raises CalibrationError (a present-but-broken defaults file shouldn't be
    silently ignored)."""
    import json
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    try:
        if p.suffix in (".yaml", ".yml"):
            import yaml
            doc = yaml.safe_load(text) or {}
        else:
            doc = json.loads(text)
    except Exception as exc:                          # noqa: BLE001 - report any parse failure
        raise CalibrationError(f"type-defaults file {p} is not valid: {exc}")
    if not isinstance(doc, dict):
        raise CalibrationError(f"type-defaults file {p} is not an object")
    return (doc.get("types") or {}).get(unit_type)


def load_unit_doc(path) -> Optional[dict]:
    """Read a per-unit calibration.json. Returns None if the file is absent (the
    'no document' case the caller handles by falling back to baked defaults); raises
    CalibrationError if it exists but is not valid JSON."""
    import json
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:                          # noqa: BLE001
        raise CalibrationError(f"calibration file {p} is not valid JSON: {exc}")
    return doc


def load_components(path) -> dict:
    """Read the shared component catalog (JSON or YAML) and return its ``components``
    map ``{id: {kind, delta_db_by_freq, …}}``. Absent file → ``{}`` (no catalog; only
    inline ``delta_db`` hops resolve). A present-but-malformed file raises
    CalibrationError (a broken catalog shouldn't be silently ignored)."""
    import json
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    try:
        if p.suffix in (".yaml", ".yml"):
            import yaml
            doc = yaml.safe_load(text) or {}
        else:
            doc = json.loads(text)
    except Exception as exc:                          # noqa: BLE001
        raise CalibrationError(f"component catalog {p} is not valid: {exc}")
    if not isinstance(doc, dict):
        raise CalibrationError(f"component catalog {p} is not an object")
    comps = doc.get("components")
    return comps if isinstance(comps, dict) else {}


def resolve_from_files(unit_path, defaults_path, signal_id: str,
                       components_path=None, freq_hz=None) -> Optional[ResolvedCalibration]:
    """Convenience: load the per-unit doc + the matching type defaults + the component
    catalog and resolve. Returns None when there is no per-unit document at all (caller
    falls back to the script's baked-in defaults). Propagates SignalNotCalibrated /
    CalibrationError otherwise, so the caller can distinguish 'fall back' from 'refuse'."""
    unit_doc = load_unit_doc(unit_path)
    if unit_doc is None:
        return None
    unit_type = unit_doc.get("unit_type", "")
    type_defaults = load_type_defaults(defaults_path, unit_type) if unit_type else None
    components = load_components(components_path) if components_path else {}
    return resolve(unit_doc, type_defaults, signal_id, components, freq_hz)


def resolve_public(unit_path, defaults_path, signal_id: str,
                   unit_type: str = "", components_path=None,
                   freq_hz=None) -> Optional[dict]:
    """Resolve to the flat public artifact (:meth:`ResolvedCalibration.to_public_dict`)
    the agent injects into a task, or None if there's no per-unit document at all.

    ``unit_type`` (the agent's runtime identity) takes precedence over the doc's own
    ``unit_type`` for selecting the type-defaults layer; if empty the doc's value is
    used. ``components_path`` is the shared component catalog a derived plane may
    reference. Propagates SignalNotCalibrated / CalibrationError so the caller can tell
    'fall back' from 'refuse'."""
    unit_doc = load_unit_doc(unit_path)
    if unit_doc is None:
        return None
    ut = unit_type or unit_doc.get("unit_type", "")
    if ut and not unit_doc.get("unit_type"):
        unit_doc = {**unit_doc, "unit_type": ut}      # so the artifact reports it
    type_defaults = load_type_defaults(defaults_path, ut) if ut else None
    components = load_components(components_path) if components_path else {}
    return resolve(unit_doc, type_defaults, signal_id, components, freq_hz).to_public_dict()
