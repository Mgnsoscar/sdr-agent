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

from paramkit.power_law import ABS, Bridge, DENSITY, SAME, parse_bridge

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
    # Per-signal EXTRAPOLATION beyond the measured gain endpoints (docs/calibration.md §7.4).
    # "none" (default) clamps flat past the lowest/highest measured gain — the safe default and
    # byte-identical to every existing document. "down"/"up"/"both" instead continue the curve
    # linearly at its end-segment slope past that end, so the operator can command power at a
    # gain they didn't measure (e.g. below a noise-floor-limited low-gain measurement). The
    # commanded gain is STILL clamped to [min_gain, ceiling] by _snap, so extrapolation extends
    # the curve, never the gain limits. A single-point curve keeps its slope-1 fallback.
    extrapolate: str = "none"
    # Per-unit SOURCE-BIAS Δ dB(f), attached only to the root/source plane by resolve().
    # Normalized so it is 0 at the signal's representative (measured-at) frequency, so it
    # leaves the v1 rep-frequency read-outs unchanged and only shifts the source AWAY from
    # where the curve was measured (the SDR's own gain-flatness). None ⇒ no bias.
    bias: Optional[list] = None              # [(freq_hz, delta_db)], strictly increasing freq
    # Measurement DE-EMBED table Δ dB(f): the loss of the cable/pad between this plane and the
    # analyzer during calibration (a bench artifact, NOT in the transmit path). resolve() folds
    # its NEGATIVE, evaluated at the signal's measured-at frequency, into offset_db — recovering
    # the TRUE power at the plane — then it is gone (never published). None ⇒ nothing to remove.
    deembed: Optional[list] = None           # [(freq_hz, delta_db)], the measurement-path loss
    # Per-signal READING anchor curves at this (source) node (docs/calibration-v2.md §13/§15):
    # a reported and/or limiting reading may be its OWN separately-measured curve (e.g. a
    # main-lobe measurement backing the limit, while the primary measures full bandwidth). Set
    # by resolve() from the signal's reading blocks; None ⇒ that reading reuses the primary
    # curve (Measurement) or a law-derived shift of it. Each is a (gains, powers) pair, already
    # de-embedded (the same measurement cable) and sharing this plane's offset.
    reported_own: Optional[tuple] = None
    limiting_own: Optional[tuple] = None

    @property
    def is_reported(self) -> bool:
        return self.role == "reported"

    def curve_for(self, reading: str = ""):
        """The (gains, powers) backing a reading at this node: an own reported/limiting curve
        when set, else the primary measured curve."""
        if reading == "reported" and self.reported_own is not None:
            return self.reported_own
        if reading == "limiting" and self.limiting_own is not None:
            return self.limiting_own
        return (self.gains, self.powers)

    def power_at(self, gain: float, reading: str = "") -> float:
        g, p = self.curve_for(reading)
        return _interp_extrap(gain, g, p, self.extrapolate) + self.offset_db

    def bias_at(self, freq: Optional[float]) -> float:
        """The source-bias shift (dB) at ``freq`` — 0 when no bias, or when the plane
        isn't the source. A one-point table is constant; an unknown frequency on a
        multi-point table falls back to its lowest-frequency value."""
        if not self.bias:
            return 0.0
        if len(self.bias) == 1 or freq is None:
            return self.bias[0][1]
        return _interp(float(freq), [f for f, _ in self.bias], [d for _, d in self.bias])


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
    # Other params of the SAME task that must be sent on every set but don't vary with power
    # (e.g. an attenuator's serial ``port``): {param_dest: value_string}. Passed verbatim
    # alongside the driving ``param`` so the one-shot has everything the script needs.
    consts: dict = field(default_factory=dict)

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
                "consts": dict(self.consts),
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
    # Constant params: other params of the same task, sent unchanged on every set (e.g. the
    # attenuator's serial ``port``). {dest: value}; the driving param can't also be a const.
    consts_spec = spec.get("consts") or {}
    if not isinstance(consts_spec, dict):
        raise CalibrationError(f"active plane {name!r} 'control.consts' must be an object")
    consts: dict = {}
    for key, val in consts_spec.items():
        if not isinstance(key, str) or not key.strip():
            raise CalibrationError(
                f"active plane {name!r} 'control.consts' keys must be non-empty strings")
        if key.strip() == param.strip():
            raise CalibrationError(
                f"active plane {name!r} 'control.consts' must not include the driving "
                f"param {param.strip()!r}")
        sval = "" if val is None else str(val)
        if sval.strip() != "":
            consts[key.strip()] = sval
    return _ActiveControl(task.strip(), param.strip(), sense, min_db, max_db, step_db, engage,
                          consts=consts)


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
    _freq_limits: list = field(repr=False, default_factory=list)  # [(plane, max_dbm, reason, via)]
    _limit_gauges: list = field(repr=False, default_factory=list)  # per-limit gauge info
    # Normalized per-unit source bias Δ dB(f) (0 at the rep frequency), also attached to the
    # source plane so the power functions fold it. Kept here to publish in the artifact and to
    # decide freq-dependence when the chain is otherwise flat (a bias-only SDR chain).
    _source_bias: Optional[list] = field(repr=False, default=None)  # [(freq_hz, delta_db)]
    # Power-quantity BRIDGES on the operating node (docs/calibration-v2.md §13). The node is
    # measured once; the REPORTED reading (what --power means to the operator) and the
    # LIMITING reading (what a ceiling is gauged against) each derive from that measurement by
    # a bridge. `_reported_delta`/`_limiting_delta` are the constant dB at representative
    # parameter values (baked for the scalar read-outs; a bridge-aware consumer re-folds at the
    # live parameter value from the artifact `readings` block). Default `same`/0 ⇒ every v1
    # document behaves byte-identically.
    _reported: Bridge = field(repr=False, default_factory=Bridge)
    _limiting: Bridge = field(repr=False, default_factory=Bridge)
    _reported_delta: float = 0.0
    _limiting_delta: float = 0.0
    _reported_quantity: str = ""     # the reported reading's quantity (operator-facing)
    _reported_unit: str = ""         # the reported reading's display unit (drives the form)
    # Per-signal MEASUREMENT (docs/calibration-ui-redesign §5): the operator-facing quantity
    # the signal was measured in, and its display unit. Since Reported is retired, these ARE
    # the base --power axis — published as the artifact's operating quantity/unit.
    _measurement_quantity: str = ""
    _measurement_unit: str = ""
    _limiting_cap: Optional[float] = None   # ceiling on the LIMITING reading (its own quantity),
                                            # enforced at runtime by re-folding the limiting bridge

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
        frequency-independent cap, every frequency-dependent limit, and the ceiling on the
        LIMITING reading (gauged through the limiting bridge). The limiting-reading cap is
        applied here for the agent's scalar read-outs but kept OUT of the emitted
        ``gain_ceiling_db`` (a bridge-aware consumer re-folds it from ``readings`` at the live
        parameter value, so baking it would double-count).

        A frequency-dependent stage limit flagged ``via`` is gauged through the operating
        node's LIMITING reading — its dBm threshold is inverted against the operating node's
        limiting curve (an ``own`` reading) or, for a law/same reading, against the measured
        curve after subtracting the limiting delta (``max_dbm − Δlim``). Here that delta is the
        representative value; a bridge-aware consumer re-folds it at the live task parameter."""
        caps = [self._gain_ceiling_const]
        for plane, max_dbm, _rs, via in self._freq_limits:
            if via and self._limiting.is_own:
                caps.append(_gain_for_power_on(max_dbm, plane, self._planes, freq,
                                               for_limit=True, reading="limiting"))
            elif via:
                caps.append(_gain_for_power_on(max_dbm - self._limiting_delta, plane,
                                               self._planes, freq, for_limit=True))
            else:
                caps.append(_gain_for_power_on(max_dbm, plane, self._planes, freq,
                                               for_limit=True))
        if self._limiting_cap is not None:
            if self._limiting.is_own:
                caps.append(_gain_for_power_on(self._limiting_cap, self.operating_plane,
                                               self._planes, freq, for_limit=True,
                                               reading="limiting"))
            else:
                caps.append(_gain_for_power_on(self._limiting_cap - self._limiting_delta,
                                               self.operating_plane, self._planes, freq,
                                               for_limit=True))
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
        # The operator's number is in the REPORTED reading. An OWN reported curve is inverted
        # directly (it is measured in the reported quantity); Measurement/law is an additive
        # offset on the primary (reported = operating + Δr).
        if self._reported.is_own:
            g = _gain_for_power_on(float(delivered_dbm), self.operating_plane,
                                   self._planes, f, reading="reported")
        else:
            g = _gain_for_power_on(float(delivered_dbm) - self._reported_delta,
                                   self.operating_plane, self._planes, f)
        return self._snap(g, f)

    def power_for_gain(self, gain_db: float, freq: Optional[float] = None) -> float:
        """Delivered power (dBm) at the operating plane for an (actual) commanded
        gain — what the radio really settled on, for the report/banner. The gain is snapped
        to the hardware grid first, so the reported power matches what the SDR will set."""
        f = self._eff_freq(freq)
        g = self._snap(float(gain_db), f)
        if self._reported.is_own:
            return _power_on(self.operating_plane, g, self._planes, f, reading="reported")
        return _power_on(self.operating_plane, g, self._planes, f) + self._reported_delta

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
        # An OWN reported curve is measured in the reported quantity, so the grid works in it
        # directly (no reported offset added at the boundary); otherwise the grid is in the
        # operating/measured quantity and the reported offset is applied by the caller.
        rd = "reported" if self._reported.is_own else ""
        grid = AchievableGrid(
            power_for_gain=lambda g: _power_on(op, g, planes, f, rd),
            gain_for_power=lambda p: _gain_for_power_on(p, op, planes, f, reading=rd),
            min_gain=self.min_gain_db, ceiling=self._max_gain_at(f),
            gain_step=self._gain_step, actives=actives)
        return grid, actives

    def realize(self, power: float, freq: Optional[float] = None) -> dict:
        """SDR-first realization of a requested delivered power. Returns the nearest
        ACHIEVABLE power and the device settings that produce it: the SDR gain, and per
        active component its applied gain and the parameter value to command on its task."""
        grid, actives = self._achievable(freq)
        # Requested power is REPORTED; the grid works in the reported quantity already for an
        # OWN curve, else in the operating (measured) quantity — translate by the reported delta.
        dr = 0.0 if self._reported.is_own else self._reported_delta
        res = grid.realize(power - dr)
        settings = []
        for a, applied in zip(actives, res["applied"]):
            name, d = a.meta
            settings.append({"plane": name, "task": d.control.task,
                             "param": d.control.param, "applied_db": applied,
                             "value": round(d.control.param_for_applied(applied), 6),
                             "consts": dict(d.control.consts)})
        return {"power_dbm": res["power_dbm"] + dr, "sdr_gain_db": res["sdr_gain_db"],
                "settings": settings}

    def _rd(self) -> float:
        """Reported offset applied at the grid boundary: 0 for an own curve (the grid is
        already in the reported quantity), else the additive reported delta."""
        return 0.0 if self._reported.is_own else self._reported_delta

    def snap_power(self, power: float, freq: Optional[float] = None) -> float:
        """The nearest achievable delivered power to ``power`` (reported quantity)."""
        dr = self._rd()
        return self._achievable(freq)[0].snap(power - dr) + dr

    def quantize_up(self, power: float, freq: Optional[float] = None) -> float:
        dr = self._rd()
        return self._achievable(freq)[0].quantize_up(power - dr) + dr

    def quantize_down(self, power: float, freq: Optional[float] = None) -> float:
        dr = self._rd()
        return self._achievable(freq)[0].quantize_down(power - dr) + dr

    def active_components(self) -> list:
        """Public descriptors for each active component (for the artifact + UI)."""
        return [d.control.to_public_dict(name, list(d.table))
                for name, d in self._active_hops()]

    @property
    def public_quantity(self) -> str:
        """The operator-facing --power quantity published to the artifact/summary: the reported
        reading's quantity if one is declared, else the signal's measured quantity (Phase 2),
        else the operating plane's quantity."""
        return (self._reported_quantity or self._measurement_quantity
                or self.operating_quantity)

    @property
    def public_unit(self) -> str:
        """The operator-facing --power display unit: the reported reading's unit if declared,
        else the signal's measured unit (Phase 2). Empty ⇒ the consumer defaults to dBm."""
        return self._reported_unit or self._measurement_unit

    def banner_label(self) -> str:
        """e.g. 'EIRP, at antenna_eirp' — so the --power number is never ambiguous."""
        q = self.public_quantity or "power"
        return f"{q}, at {self.operating_plane}"

    def operating_curve(self, freq: Optional[float] = None) -> list:
        """The operating-plane transfer as a flat, gain-sorted ``[[gain, power], …]``
        table at the anchor's measured breakpoints (derived hops folded in at ``freq``).
        A v1 script consumes this directly; a v2 script prefers ``anchor_curve`` +
        ``passive_hops`` so it can re-fold at its live frequency."""
        f = self._eff_freq(freq)
        _, anchor = _anchor(self.operating_plane, self._planes, f)
        # v1-compat curve is in the REPORTED quantity (what --power means to a v1 script);
        # a v2 consumer instead re-folds anchor_curve + passive_hops + the `readings` bridge.
        # Gain breakpoints come from the reported reading's own curve when it has one.
        rd = "reported" if self._reported.is_own else ""
        gains = anchor.curve_for(rd)[0]
        dr = 0.0 if self._reported.is_own else self._reported_delta
        return [[g, _power_on(self.operating_plane, g, self._planes, f, rd) + dr]
                for g in gains]

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

    def _limiting_own_curve(self) -> Optional[list]:
        """The operating node's OWN limiting curve (``[[gain, dBm], …]``, offset folded in) —
        the separately-measured dBm curve a limiting ``own`` reading carries. A frequency-
        dependent stage limit gauged through it inverts against this curve at runtime (the same
        curve ``readings.limiting.anchor_curve`` publishes). None when the limiting reading has
        no own curve."""
        if not self._limiting.is_own:
            return None
        src = _anchor_plane(self.operating_plane, self._planes)
        if src.limiting_own is None:
            return None
        return [[g, src.power_at(g, "limiting")] for g in src.limiting_own[0]]

    def _plane_delta_table(self, plane_name: str) -> list:
        """The TOTAL passive delta from the measured anchor out to ``plane_name`` as one
        ``[[freq, delta], …]`` table (all its hops summed). A consumer inverts a limit on
        this plane by subtracting this from its threshold and inverting the shared
        anchor curve — so it needs no plane model of its own."""
        return [[f, d] for f, d in _sum_tables([dv.table for _, dv in _hops(plane_name, self._planes)])]

    @property
    def has_readings(self) -> bool:
        """True when a non-trivial reported/limiting bridge is set (a v1 doc has neither,
        so its artifact stays byte-identical)."""
        def nontrivial(b: Bridge) -> bool:
            return b.kind != SAME or b.k != 0.0 or bool(b.unit)
        return (nontrivial(self._reported) or nontrivial(self._limiting)
                or self._limiting_cap is not None)

    def readings_public(self) -> dict:
        """The reported/limiting bridges (+ any limiting-reading cap) for a bridge-aware
        consumer to re-fold at the live parameter values (docs/calibration-v2.md §13). The
        laws are embedded, so the consumer needs no script. ``reported_delta_db`` /
        ``limiting_delta_db`` are the representative-value deltas the v1 ``curve``/min/max
        already bake in, so a consumer that re-folds knows the baseline it is replacing."""
        rep = self._reported.to_public_dict()
        if self._reported_quantity:
            rep["quantity"] = self._reported_quantity
        lim = self._limiting.to_public_dict()
        if self._limiting_cap is not None:
            lim["max_dbm"] = self._limiting_cap
        # An OWN reading publishes its SOURCE anchor curve (de-embedded), so a consumer folds
        # the same passive_hops onto it — the reported axis / limiting ceiling track the chain.
        src = _anchor_plane(self.operating_plane, self._planes)
        if self._reported.is_own and src.reported_own is not None:
            rep["anchor_curve"] = [[g, src.power_at(g, "reported")] for g in src.reported_own[0]]
        if self._limiting.is_own and src.limiting_own is not None:
            lim["anchor_curve"] = [[g, src.power_at(g, "limiting")] for g in src.limiting_own[0]]
        return {"reported": rep, "limiting": lim,
                "reported_delta_db": self._reported_delta,
                "limiting_delta_db": self._limiting_delta}

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
            "quantity": self.public_quantity,
            "amplitude": self.amplitude,
            "min_gain_db": self.min_gain_db,
            "max_gain_db": self.max_gain_db,
            "min_power_dbm": self.min_power_dbm,
            "max_power_dbm": self.max_power_dbm,
            "curve": self.operating_curve(),
        }
        # Extrapolation past the measured gain endpoints is a property of the operating
        # curve; publish it (top-level, even on a flat v1 chain) so calkit / the client
        # re-fold --power the same way the resolver did (else they'd clamp and disagree).
        op_anchor = _anchor_plane(self.operating_plane, self._planes)
        if isinstance(op_anchor, _Measured) and op_anchor.extrapolate != "none":
            out["extrapolate"] = op_anchor.extrapolate
        if self._gain_step:
            out["gain_step_db"] = self._gain_step
        if self.public_unit:
            out["operating_unit"] = self.public_unit        # drives the operator form's unit
        if self.has_readings:
            out["readings"] = self.readings_public()
        if self.has_active:
            out["active_components"] = self.active_components()
        hops = self.passive_hops()
        # A non-trivial reading bridge also needs the v2 shape: the anchor is the MEASURED
        # curve and the bridge is applied on top at the live parameter value, so the anchor
        # must be published (the v1 `curve` already bakes the reported delta at rep, for old
        # scripts) even on an otherwise-flat chain.
        if hops or self._freq_limits or self._source_bias or self.has_readings:
            out["anchor_curve"] = self.anchor_curve()
            out["passive_hops"] = hops
            # The per-unit source bias shifts the ANCHOR (so operating power AND limit
            # inversion move with frequency); a v2 consumer adds it to the anchor at its
            # live frequency. Normalized to 0 at the rep frequency, so the v1 curve above
            # is unchanged. A bias alone makes an otherwise-flat SDR chain frequency-aware.
            if self._source_bias:
                out["source_bias_delta_by_freq"] = [[f, d] for f, d in self._source_bias]
            # Each frequency-dependent limit carries its own summed delta from the shared
            # anchor, so a consumer inverts it against the same anchor_curve at the live
            # frequency (no plane model needed script-side).
            fdl = []
            for p, mx, rs, via in self._freq_limits:
                entry = {"plane": p, "max_dbm": mx, "reason": rs,
                         "delta_db_by_freq": self._plane_delta_table(p)}
                if via and self._limiting.is_own:
                    # gauge through the operating node's OWN limiting curve (dBm): publish it so
                    # the consumer inverts THIS limit against it (the same curve readings carry).
                    own = self._limiting_own_curve()
                    if own is not None:
                        entry["anchor_curve"] = own
                elif via:
                    # gauge through a law/same limiting reading: the consumer subtracts the
                    # limiting delta (re-folded at the live task parameter) before inverting.
                    entry["via_limiting"] = True
                else:
                    lac = self._limit_anchor_curve(p)  # its own limiting curve, if it differs
                    if lac is not None:
                        entry["anchor_curve"] = lac    # invert THIS limit against this curve
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


def _interp_extrap(x: float, xs: list[float], ys: list[float], mode: str) -> float:
    """Like :func:`_interp`, but linearly EXTRAPOLATES past an endpoint at that end's
    segment slope when ``mode`` permits the direction: ``"down"`` below ``xs[0]``,
    ``"up"`` above ``xs[-1]``, ``"both"`` either; ``"none"`` (or empty) clamps exactly
    like ``_interp``. A single-point curve keeps the slope-1 fallback (no slope to use).

    Only a MEASURED gain→power curve the operator opted into extrapolating uses this;
    frequency and bias tables always clamp (they call ``_interp`` directly). Because a
    curve's powers are strictly increasing with gain, the SAME call inverts it — pass
    ``(power, powers, gains, mode)`` to get the extrapolated gain for a power."""
    n = len(xs)
    if n < 2 or not mode or mode == "none":
        return _interp(x, xs, ys)
    if x < xs[0] and mode in ("down", "both"):
        slope = (ys[1] - ys[0]) / (xs[1] - xs[0])
        return ys[0] + slope * (x - xs[0])
    if x > xs[-1] and mode in ("up", "both"):
        slope = (ys[-1] - ys[-2]) / (xs[-1] - xs[-2])
        return ys[-1] + slope * (x - xs[-1])
    return _interp(x, xs, ys)


# ── Plane traversal (mirrors docs/calibration.md §7.3) ───────────────────────────

def _power_on(plane_name: str, gain: float, planes: dict, freq: Optional[float],
              reading: str = "") -> float:
    """Power at ``plane_name`` for a commanded gain, walking derived hops down to a
    measured curve. Each derived hop is evaluated at ``freq``. ``reading`` selects the
    source anchor curve (an own reported/limiting curve when set), so a reading measured
    at the source folds through the same passive hops to the operating plane."""
    p = planes[plane_name]
    if isinstance(p, _Measured):
        return p.power_at(gain, reading) + p.bias_at(freq)   # source-bias shift (0 if none)
    return _power_on(p.frm, gain, planes, freq, reading) + p.delta_at(freq)


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
                       freq: Optional[float], for_limit: bool = False,
                       reading: str = "") -> float:
    """Gain that yields ``power`` at ``plane_name``. Subtract downstream derived
    deltas (at ``freq``) to reach the anchor measured plane, then invert its curve
    once. Clamps at the measured range — upward to the top gain (never extrapolated
    past the ceiling), downward to the bottom gain. ``for_limit`` gauges a safety limit:
    the walk punches through ``reported`` planes to the ``limiting`` curve (§4.1).
    ``reading`` selects the source anchor's own reported/limiting curve when set."""
    delta, m = _anchor(plane_name, planes, freq, for_limit=for_limit)
    g_curve, p_curve = m.curve_for(reading)
    target = power - delta - m.offset_db - m.bias_at(freq)   # undo the source-bias shift
    # Invert by interpolating gain(power) — powers are strictly increasing, so it is
    # unambiguous. "none" clamps at the measured endpoints (the historical behaviour); an
    # extrapolate mode continues the end slope past that end. The result is still clamped to
    # [min_gain, ceiling] by _snap, and the ceiling itself never exceeds max_gain_db.
    return _interp_extrap(target, p_curve, g_curve, m.extrapolate)


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
        for plane, _mx, _rs, _via in freq_limits:
            cands.update(_breakpoint_freqs(plane, planes))
        cands.update(_breakpoint_freqs(operating_plane, planes))
        if not cands:
            return None

        def ceiling_at(fr: float) -> float:
            # A uniform limiting-reading offset (a via limit's Δlim) shifts every candidate
            # frequency's ceiling equally, so it can't change the argmin — invert on the passive
            # path here and let _max_gain_at apply Δlim to the chosen frequency's scalar ceiling.
            caps = [gain_ceiling_const]
            for plane, mx, _rs, _via in freq_limits:
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


# ── per-signal measurement quantity/unit (docs/calibration-ui-redesign §5) ────────
# The operator-facing quantity the signal was measured in, and its display unit. The unit's
# FAMILY (absolute dBm vs a spectral density) is what the reading bridges convert from — a
# density measurement feeds the density→dBm laws; an absolute one can be limited "as measured".
_MEASUREMENT_UNIT_FAMILY = {"dBm": ABS, "dBm/Hz": DENSITY, "dBm/kHz": DENSITY, "dBm/MHz": DENSITY}


def _measurement_of(sig: dict, signal_id: str) -> tuple[str, str]:
    """The signal's declared ``measurement`` (quantity, unit), or ("", "") when absent.
    Validates the shape and that the unit is one this agent understands (its family must be
    known to gauge the bridges). Absent ⇒ today's behaviour (the plane quantity, dBm)."""
    m = sig.get("measurement")
    if m is None:
        return "", ""
    if not isinstance(m, dict):
        raise CalibrationError(f"signal {signal_id!r} 'measurement' must be an object")
    quantity = str(m.get("quantity", "") or "").strip()
    unit = str(m.get("unit", "") or "").strip()
    if unit and unit not in _MEASUREMENT_UNIT_FAMILY:
        raise CalibrationError(
            f"signal {signal_id!r} measurement unit {unit!r} is not one of "
            f"{tuple(_MEASUREMENT_UNIT_FAMILY)}")
    return quantity, unit


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

    # Measurement DE-EMBED: remove any measurement-path loss (the cable/pad between a measured
    # plane and the analyzer) from that plane's curve, recovering the TRUE power at the plane.
    # Folded into offset_db as a constant, evaluated at the signal's measured-at frequency
    # (center_freq_hz); a constant-loss cable needs none, and an unknown frequency on a
    # frequency-dependent table falls back to its lowest-frequency value. Done here — BEFORE the
    # ceiling/limit inversion below — so every safety limit gauges true power, and it is a
    # bench artifact that never reaches the artifact or the transmit path.
    for _p in planes.values():
        if isinstance(_p, _Measured) and _p.deembed:
            f = rep_freq if (rep_freq is not None or len(_p.deembed) == 1) else _p.deembed[0][0]
            _p.offset_db -= _eval_table(_p.deembed, f)
            _p.deembed = None

    # Bypassed stages (a component you've physically pulled without deleting it) resolve to
    # transparent 0-dB hops in _build_planes; here their safety limits are dropped too — "as
    # if it weren't there". The source (root) stage can't be bypassed (_build_planes rejects
    # it). A bypassed operating plane re-anchors upstream for free: it's now a 0-dB hop, so
    # the operating point falls through to the nearest live plane.
    bypassed = {n for n, s in planes_spec.items()
                if isinstance(s, dict) and s.get("bypass")}

    # Power-quantity BRIDGES on the operating node (docs/calibration-v2.md §13): the node is
    # measured once; the REPORTED reading (what --power means to the operator) and the
    # LIMITING reading (what a ceiling is gauged against) each derive from that measurement by
    # a self-contained bridge (an embedded law, or `same`+k, or an independent `own` curve).
    # Absent ⇒ `same`/0 dB, i.e. today's behaviour byte-for-byte. Ignored on a bypassed
    # operating plane (it re-anchors upstream). The reported delta shifts the operator power
    # axis; a limiting `max_dbm` cap becomes a synthetic limit on the operating plane in the
    # MEASURED quantity (max_dbm − Δlim), so the existing limit machinery inverts and
    # freq-classifies it. Both deltas are baked at representative parameter values for the
    # scalar read-outs; the artifact carries the bridges so a consumer re-folds at the live value.
    op_spec = planes_spec.get(operating_plane)
    if not isinstance(op_spec, dict) or operating_plane in bypassed:
        op_spec = {}
    # A reading is per-SIGNAL first (a chirp reports full-bandwidth power, a CW at the same
    # node reports plain power), with the operating-plane spec as a shared default — so a
    # single-signal-type unit can set it once on the plane, and a mixed unit overrides per
    # signal. The signal entry wins per key.
    def _reading(key):
        v = sig.get(key)
        if v is None:
            v = op_spec.get(key)
        return v if isinstance(v, dict) else None
    rep_spec = _reading("reported")
    lim_spec = _reading("limiting")
    try:
        reported = parse_bridge(rep_spec)
        limiting = parse_bridge(lim_spec)
    except ValueError as exc:
        raise CalibrationError(f"operating plane {operating_plane!r} reading: {exc}")
    reported_delta = reported.rep_delta_db()
    limiting_delta = limiting.rep_delta_db()
    reported_unit = reported.unit
    reported_quantity = str(rep_spec.get("quantity", "")) if rep_spec else ""

    # Per-signal MEASUREMENT quantity + unit (Phase 2). The operator's base --power axis IS
    # the measured quantity (Reported is retired), so publish this signal's declared quantity
    # and unit; the unit's FAMILY then gauges the reading bridges below. Absent ⇒ unchanged
    # (the plane quantity, dBm).
    meas_quantity, meas_unit = _measurement_of(sig, signal_id)
    if meas_unit:
        meas_fam = _MEASUREMENT_UNIT_FAMILY[meas_unit]
        for role, bridge in (("reported", reported), ("limiting", limiting)):
            if bridge.is_law and bridge.law is not None and bridge.law.in_fam != meas_fam:
                raise CalibrationError(
                    f"signal {signal_id!r} {role} law {bridge.law.id!r} expects a "
                    f"{bridge.law.in_fam!r} measurement, but the signal is measured in "
                    f"{meas_unit!r} ({meas_fam!r})")
        # The safety ceiling is always dBm, so the LIMITING reading must resolve to dBm:
        # "same as measurement" only when the measurement itself is dBm, and a limiting law
        # must return dBm.
        if limiting.is_same and meas_fam != ABS:
            raise CalibrationError(
                f"signal {signal_id!r} limiting is 'same as measurement' but the signal is "
                f"measured in {meas_unit!r} (a density) — a dBm ceiling can't gauge it; use a "
                f"law that returns dBm or a separate dBm measurement")
        if limiting.is_law and limiting.law is not None and limiting.law.out_fam != ABS:
            raise CalibrationError(
                f"signal {signal_id!r} limiting law {limiting.law.id!r} must return dBm "
                f"(out == {ABS!r}), not {limiting.law.out_fam!r}")

    lim_cap = None
    if lim_spec and lim_spec.get("max_dbm") is not None:
        try:
            lim_cap = float(lim_spec["max_dbm"])
        except (TypeError, ValueError):
            raise CalibrationError(
                f"operating plane {operating_plane!r} 'limiting.max_dbm' must be numeric")

    # An `own` reading is a SEPARATELY measured curve at the source node (e.g. a main-lobe
    # measurement backing the limit while the primary measures full bandwidth). Attach it to
    # the observed source anchor so the operator axis / ceiling fold it through the chain like
    # the primary; the de-embed (already folded into offset_db) applies to it too.
    if reported.is_own or limiting.is_own:
        src_anchor = _anchor_plane(operating_plane, planes)
        if reported.is_own:
            src_anchor.reported_own = _own_reading_curve(rep_spec, f"{signal_id!r} reported")
        if limiting.is_own:
            src_anchor.limiting_own = _own_reading_curve(lim_spec, f"{signal_id!r} limiting")

    # Per-unit SOURCE BIAS (the SDR's output-power-vs-frequency flatness): a fixed-gain CW
    # power table, unit-owned (top-level), NOT a component. Skipped when its stage is
    # bypassed. Parsed here; normalized to the rep frequency and attached to the source
    # plane once the rep frequency is settled (below).
    sb = unit_doc.get("source_bias")
    if sb is not None and not isinstance(sb, dict):
        raise CalibrationError("source_bias must be an object")
    bias_pts = (_bias_table(sb.get("power_by_freq"), "source_bias")
                if isinstance(sb, dict) and not sb.get("bypass") else None)

    # The source (root measured) plane the bias attaches to. A source bias makes the source
    # frequency-dependent, so any limit gauged through it must classify as frequency-dependent
    # below — which needs the rep frequency settled first, so derive one from the bias sweep
    # now when the signal declares none.
    src_name = next((n for n, p in planes.items() if isinstance(p, _Measured)), None)
    bias_on_source = bias_pts is not None and src_name is not None
    if bias_pts is not None and rep_freq is None:
        _bf = sorted({f for f, _ in bias_pts})
        rep_freq = 0.5 * (_bf[0] + _bf[-1])

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
    # A stage limit's dBm ceiling is gauged through the operating node's LIMITING reading when it
    # resolves to the same measured plane the operating node's limiting curve does (the shared
    # source). Then the ceiling is compared in the LIMITING quantity, not the measured one — so a
    # single dBm limit caps every signal correctly whatever quantity it is measured in. For a
    # law/same limiting reading the limiting delta is folded in (max_dbm − Δlim); for an OWN
    # reading the limit inverts against that separate dBm curve. A CONSTANT delta bakes into the
    # ceiling; a PARAMETER-KEYED limiting law can't (its delta moves with the task parameter), so
    # that limit is published as a runtime entry the consumer re-folds — even on a flat chain.
    op_lim_anchor = _anchor_plane(operating_plane, planes, for_limit=True)
    lim_reading_nontrivial = limiting.is_own or not limiting.is_same or limiting.k != 0.0
    lim_reading_param_keyed = not limiting.is_constant
    for lim in (chain.get("limits") or []):
        if lim.get("plane") in bypassed:             # limit on a bypassed stage doesn't apply
            continue
        plane = _limit_plane(lim, planes)            # honour side: input → one hop upstream
        anchor = _require_usable(plane, planes, for_limit=True)   # gauge on the LIMITING curve
        limit_gauges.append({
            "reason": lim.get("reason", ""), "max_dbm": float(lim["max_dbm"]),
            "at_plane": plane, "gauge_plane": _plane_name(anchor, planes),
            "gauge_quantity": anchor.quantity})
        max_dbm = float(lim["max_dbm"])
        via = (lim_reading_nontrivial
               and _anchor_plane(plane, planes, for_limit=True) is op_lim_anchor)
        freqdep = _path_freq_dependent(plane, planes) or (
            bias_on_source
            and _anchor_plane(plane, planes, for_limit=True) is planes[src_name])
        if freqdep or (via and lim_reading_param_keyed):
            # runtime: re-folded per frequency (a passive hop / biased source) and/or per the
            # live task parameter (a param-keyed limiting law). The via flag tells the consumer
            # to also fold the limiting delta / invert against the own limiting curve.
            freq_limits.append((plane, max_dbm, lim.get("reason", ""), via))
        elif via and limiting.is_own:
            const_caps.append(_gain_for_power_on(max_dbm, plane, planes, None,
                                                 for_limit=True, reading="limiting"))
        elif via:
            const_caps.append(_gain_for_power_on(max_dbm - limiting_delta, plane, planes, None,
                                                 for_limit=True))
        else:
            const_caps.append(
                _gain_for_power_on(max_dbm, plane, planes, None, for_limit=True))
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
        for plane, _mx, _rs, _via in freq_limits:
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
    # A limit that is only PARAMETER-dependent (a flat path gauged through a param-keyed
    # limiting law) needs no frequency at all, so it doesn't force a representative one.
    has_freq_limit = any(_path_freq_dependent(p, planes) for p, _mx, _rs, _via in freq_limits)
    if (has_freq_limit or _path_freq_dependent(operating_plane, planes)) and rep_freq is None:
        rep_freq = _representative_freq(planes, operating_plane, freq_limits,
                                        gain_ceiling_const)
        if rep_freq is None:
            raise CalibrationError(
                f"signal {signal_id!r} uses a frequency-dependent component but has no "
                f"'center_freq_hz' and no frequency breakpoints to derive a representative "
                f"operating frequency from")

    # Normalize the source bias to the rep frequency (the frequency the curve was measured
    # at) and attach it to the source plane. Zeroing it at the rep frequency keeps the v1
    # rep-frequency read-outs unchanged; it only shifts the source AWAY from there. If the
    # signal declares no rep frequency, derive one from the bias sweep so a bias-only SDR
    # chain (no hops/limits) is still frequency-aware.
    source_bias = None
    if bias_pts is not None:
        zero = _eval_table(bias_pts, rep_freq)            # bias(rep) — the normalization point
        source_bias = [(f, d - zero) for f, d in bias_pts]
        if src_name is not None:
            planes[src_name].bias = source_bias           # fold it wherever source is the anchor

    op_quantity = _quantity_of(planes[operating_plane])
    resolved = ResolvedCalibration(
        signal_id=signal_id, unit_type=unit_type, amplitude=amplitude,
        min_gain_db=gmin, operating_plane=operating_plane, operating_quantity=op_quantity,
        _planes=planes, _freq_hz=rep_freq, _gain_step=gstep,
        _gain_ceiling_const=gain_ceiling_const, _freq_limits=freq_limits,
        _limit_gauges=limit_gauges, _source_bias=source_bias,
        _reported=reported, _limiting=limiting,
        _reported_delta=reported_delta, _limiting_delta=limiting_delta,
        _reported_quantity=reported_quantity, _reported_unit=reported_unit,
        _measurement_quantity=meas_quantity, _measurement_unit=meas_unit,
        _limiting_cap=lim_cap)
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
                "quantity": r.public_quantity,
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


def _deembed_table(spec, components: dict, plane_name: str):
    """Resolve a measured plane's ``measurement_deembed`` into a Δ dB(f) loss table — the
    measurement-path cable/pad between the plane and the analyzer (a bench artifact, removed
    from the reading, never in the transmit path). Accepts a catalog component id or an inline
    table (or ``{delta_db_by_freq: …}``); ``None``/"" ⇒ nothing to de-embed."""
    if spec is None or spec == "":
        return None
    if isinstance(spec, str):
        comp = components.get(spec)
        if not isinstance(comp, dict):
            raise CalibrationError(
                f"measured plane {plane_name!r} de-embeds unknown component {spec!r}")
        return _freq_table(comp.get("delta_db_by_freq"), f"de-embed component {spec!r}")
    if isinstance(spec, dict) and "delta_db_by_freq" in spec:
        return _freq_table(spec["delta_db_by_freq"],
                           f"plane {plane_name!r} measurement_deembed")
    if isinstance(spec, list):
        return _freq_table(spec, f"plane {plane_name!r} measurement_deembed")
    raise CalibrationError(
        f"measured plane {plane_name!r} 'measurement_deembed' must be a component id or table")


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


def _bias_table(raw, ctx: str) -> list:
    """Validate + sort a source-bias ``[[freq_hz, power_dbm], …]`` table: ≥1 point, strictly
    increasing in frequency. The values are the ABSOLUTE power measured for the fixed-gain CW
    at each frequency; resolve() normalizes them to a Δ against the rep frequency. One point ⇒
    a constant (no-op after normalization)."""
    if not isinstance(raw, list) or not raw:
        raise CalibrationError(f"{ctx}: power_by_freq must be a non-empty list")
    try:
        pts = sorted(((float(f), float(p)) for f, p in raw), key=lambda fp: fp[0])
    except (TypeError, ValueError) as exc:
        raise CalibrationError(f"{ctx}: malformed power_by_freq point: {exc}")
    for i in range(1, len(pts)):
        if pts[i][0] <= pts[i - 1][0]:
            raise CalibrationError(
                f"{ctx}: power_by_freq frequencies not strictly increasing near "
                f"{pts[i][0]:g} Hz")
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
            if spec.get("bypass"):
                # Bypassed measured stage → transparent 0-dB hop onto the stage before it
                # (its curve/role are ignored). The source stage has nothing upstream, so it
                # can't be bypassed.
                if prev_name is None:
                    raise CalibrationError(f"the source stage {name!r} can't be bypassed")
                planes[name] = _Derived(frm=prev_name, table=[(0.0, 0.0)], fallback=True,
                                        quantity=quantity, description=description)
                prev_name = name
                continue
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
                    quantity=quantity, description=description, role=role, of=of,
                    extrapolate=_curve_extrapolate(curve, name),
                    deembed=_deembed_table(spec.get("measurement_deembed"), components, name))
            prev_name = name
            continue
        if ptype == "derived":
            frm = spec.get("from")
            if not frm:
                raise CalibrationError(f"derived plane {name!r} has no 'from'")
            if spec.get("bypass"):
                # Bypassed component → transparent 0-dB hop (its delta/component/control are
                # ignored, and it's omitted from the published passive_hops via fallback).
                planes[name] = _Derived(frm=frm, table=[(0.0, 0.0)], fallback=True,
                                        quantity=quantity, description=description)
                prev_name = name
                continue
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


def _own_reading_curve(spec: dict, ctx: str) -> tuple[list[float], list[float]]:
    """Build the (gains, powers) for an ``own`` reading — a separately measured curve at the
    source node embedded in the reading block as ``{curve: {points: […]}}`` (docs
    §13/§15). Invertible like any measured curve."""
    curve = spec.get("curve")
    if not isinstance(curve, dict):
        raise CalibrationError(
            f"{ctx}: an own-measurement reading needs a 'curve' with measured points")
    return _curve_points(curve, ctx)


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


_EXTRAPOLATE_MODES = ("none", "down", "up", "both")


def _curve_extrapolate(curve: dict, name: str) -> str:
    """Validate and normalize a measured curve's optional ``extrapolate`` setting —
    how the curve behaves past its measured gain endpoints (see ``_Measured``). Accepts
    ``"none"``/``"down"``/``"up"``/``"both"`` (case-insensitive), plus a bool for
    convenience (``true`` → ``"both"``); absent/false/null ⇒ ``"none"``."""
    v = curve.get("extrapolate", "none")
    if v is True:
        return "both"
    if v is False or v is None:
        return "none"
    v = str(v).strip().lower()
    if v not in _EXTRAPOLATE_MODES:
        raise CalibrationError(
            f"plane {name!r} has invalid extrapolate {v!r} "
            f"(expected one of {', '.join(_EXTRAPOLATE_MODES)})")
    return v


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
