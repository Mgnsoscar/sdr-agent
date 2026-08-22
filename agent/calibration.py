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
    """A measured plane: a monotonic gain→power curve plus a fixed offset."""
    gains: list[float]
    powers: list[float]
    offset_db: float = 0.0
    quantity: str = ""
    description: str = ""

    def power_at(self, gain: float) -> float:
        return _interp(gain, self.gains, self.powers) + self.offset_db


@dataclass
class _Derived:
    """A derived plane: a constant dB hop from a parent plane."""
    frm: str
    delta_db: float
    quantity: str = ""
    description: str = ""


@dataclass
class ResolvedCalibration:
    """The runtime-usable result for one (unit, signal). ``gain_for_power`` and
    ``power_for_gain`` are the two functions the transmit script needs; the rest is
    for the banner / reporting."""
    signal_id: str
    unit_type: str
    amplitude: Optional[float]
    min_gain_db: float
    max_gain_db: float
    operating_plane: str
    operating_quantity: str
    _planes: dict = field(repr=False, default_factory=dict)

    # ── the two functions the script calls ──────────────────────────────────────
    def gain_for_power(self, delivered_dbm: float) -> float:
        """Commanded SDR gain (dB) for a requested power at the operating plane,
        clamped to [min_gain_db, max_gain_db]. Upward is clamped to the ceiling,
        never extrapolated past it."""
        g = _gain_for_power_on(float(delivered_dbm), self.operating_plane, self._planes)
        return min(max(g, self.min_gain_db), self.max_gain_db)

    def power_for_gain(self, gain_db: float) -> float:
        """Delivered power (dBm) at the operating plane for an (actual) commanded
        gain — what the radio really settled on, for the report/banner."""
        g = min(max(float(gain_db), self.min_gain_db), self.max_gain_db)
        return _power_on(self.operating_plane, g, self._planes)

    # ── convenience for the script's --power min/max bounds ─────────────────────
    @property
    def max_power_dbm(self) -> float:
        return self.power_for_gain(self.max_gain_db)

    @property
    def min_power_dbm(self) -> float:
        return self.power_for_gain(self.min_gain_db)

    def banner_label(self) -> str:
        """e.g. 'EIRP, at antenna_eirp' — so the --power number is never ambiguous."""
        q = self.operating_quantity or "power"
        return f"{q}, at {self.operating_plane}"

    def operating_curve(self) -> list[list[float]]:
        """The operating-plane transfer as a flat, gain-sorted ``[[gain, power], …]``
        table at the anchor's measured breakpoints (derived hops folded in). This is
        the whole mapping a script needs to convert --power ↔ gain at runtime — it
        can linearly interpolate/invert this directly, no plane model required."""
        _, anchor = _anchor(self.operating_plane, self._planes)
        return [[g, _power_on(self.operating_plane, g, self._planes)]
                for g in anchor.gains]

    def to_public_dict(self) -> dict:
        """The resolved artifact the agent writes for a task to consume. Everything
        a script needs and nothing about planes: the flattened operating curve, the
        gain clamps, the amplitude the curves were measured at, and the quantity
        label for the banner. A single-point curve here means the reader applies the
        same slope-1 fallback the resolver does."""
        return {
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

def _power_on(plane_name: str, gain: float, planes: dict) -> float:
    """Power at ``plane_name`` for a commanded gain, walking derived hops down to a
    measured curve."""
    p = planes[plane_name]
    if isinstance(p, _Measured):
        return p.power_at(gain)
    return _power_on(p.frm, gain, planes) + p.delta_db     # derived: constant hop


def _anchor(plane_name: str, planes: dict) -> tuple[float, _Measured]:
    """Walk derived hops down to the nearest measured ancestor, accumulating the
    total derived delta. Returns (delta, measured_plane)."""
    delta, p = 0.0, planes[plane_name]
    while isinstance(p, _Derived):
        delta += p.delta_db
        p = planes[p.frm]
    return delta, p


def _gain_for_power_on(power: float, plane_name: str, planes: dict) -> float:
    """Gain that yields ``power`` at ``plane_name``. Subtract downstream derived
    deltas to reach the anchor measured plane, then invert its curve once. Clamps at
    the measured range — upward to the top gain (never extrapolated past the
    ceiling), downward to the bottom gain."""
    delta, m = _anchor(plane_name, planes)
    target = power - delta - m.offset_db
    if len(m.powers) == 1:
        return m.gains[0] + (target - m.powers[0])         # slope-1 inverse
    if target >= m.powers[-1]:
        return m.gains[-1]
    if target <= m.powers[0]:
        return m.gains[0]
    return _interp(target, m.powers, m.gains)              # powers monotonic → unambiguous


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
            signal_id: str) -> ResolvedCalibration:
    """Resolve the calibration for one (unit, signal).

    ``unit_doc``      the parsed per-unit calibration.json.
    ``type_defaults`` the ``types[unit_type]`` section from the shared
                      calibration_defaults file (chain/defaults skeleton), or None.
    ``signal_id``     the script's stable CAL_SIGNAL_ID.

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

    # 3. build planes, attaching this signal's curves into the measured ones
    planes = _build_planes(planes_spec, curves, signal_id)

    # 4. validate topology + operating plane usability
    _validate_topology(planes, operating_plane)

    # 5. gain bounds + ceiling
    gl = chain.get("gain_limits") or {}
    gmin = float(gl.get("min_gain_db", 0.0))
    gmax = _resolve_max_gain(chain, planes)
    if gmax < gmin:
        raise CalibrationError(
            f"resolved max gain {gmax:.2f} dB is below min gain {gmin:.2f} dB")

    op_quantity = _quantity_of(planes[operating_plane])
    return ResolvedCalibration(
        signal_id=signal_id, unit_type=unit_type, amplitude=amplitude,
        min_gain_db=gmin, max_gain_db=gmax,
        operating_plane=operating_plane, operating_quantity=op_quantity,
        _planes=planes)


def validate_document(unit_doc: dict, type_defaults: Optional[dict] = None) -> dict:
    """Structural validation of a WHOLE calibration document, as it would resolve at
    runtime — for validate-on-upload (docs/calibration.md §9.2).

    Runs :func:`resolve` for every signal in the document (so every measured curve,
    every plane reference, the ceiling, and the operating plane are checked exactly
    as at transmit time) and raises :class:`CalibrationError` on any document-level
    defect or any invalid signal. On success returns a per-signal summary dict:
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
    if not isinstance(signals, dict) or not signals:
        raise CalibrationError("document has no signals")

    summary, bad = {}, {}
    for sig_id in signals:
        try:
            r = resolve(unit_doc, type_defaults, sig_id)
            summary[sig_id] = {
                "operating_plane": r.operating_plane,
                "quantity": r.operating_quantity,
                "amplitude": r.amplitude,
                "min_gain_db": r.min_gain_db, "max_gain_db": r.max_gain_db,
                "min_power_dbm": r.min_power_dbm, "max_power_dbm": r.max_power_dbm,
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


def _build_planes(planes_spec: dict, curves: dict, signal_id: str) -> dict:
    """Turn the chain's plane specs (topology, no points) plus the signal's curves
    into resolved _Measured / _Derived objects."""
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
    for name, spec in planes_spec.items():
        if not isinstance(spec, dict):
            raise CalibrationError(f"plane {name!r} is not an object")
        ptype = spec.get("type")
        quantity = spec.get("quantity", "")
        description = spec.get("description", "")
        if ptype == "measured":
            curve = curves.get(name)
            if curve is None:
                # latent: declared but not measured for this signal. Legal unless it
                # turns out to be needed (validated later).
                planes[name] = _Measured(gains=[], powers=[], quantity=quantity,
                                         description=description)
                continue
            gains, powers = _curve_points(curve, name)
            planes[name] = _Measured(
                gains=gains, powers=powers,
                offset_db=float(curve.get("offset_db", 0.0)),
                quantity=quantity, description=description)
        elif ptype == "derived":
            frm = spec.get("from")
            if not frm:
                raise CalibrationError(f"derived plane {name!r} has no 'from'")
            if "delta_db" not in spec:
                raise CalibrationError(f"derived plane {name!r} has no 'delta_db'")
            planes[name] = _Derived(frm=frm, delta_db=float(spec["delta_db"]),
                                    quantity=quantity, description=description)
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


def _validate_topology(planes: dict, operating_plane: str) -> None:
    """Every 'from' resolves, the derived graph is acyclic and ends at a measured
    plane, and the operating plane has a usable transfer for this signal."""
    for name, p in planes.items():
        if isinstance(p, _Derived) and p.frm not in planes:
            raise CalibrationError(f"plane {name!r} references unknown plane {p.frm!r}")
    if operating_plane not in planes:
        raise CalibrationError(f"operating_plane {operating_plane!r} does not exist")
    # walk from operating plane to its measured anchor, guarding against cycles
    _require_usable(operating_plane, planes)


def _require_usable(plane_name: str, planes: dict) -> _Measured:
    """Return the measured anchor of ``plane_name`` if it has points; raise otherwise.
    Detects cycles in the derived chain."""
    seen: set[str] = set()
    p = planes[plane_name]
    while isinstance(p, _Derived):
        if plane_name in seen:
            raise CalibrationError(f"derived plane cycle through {plane_name!r}")
        seen.add(plane_name)
        plane_name = p.frm
        p = planes[plane_name]
    if not p.gains:                                  # measured but latent (no points)
        raise CalibrationError(
            f"plane {plane_name!r} has no measured curve for this signal "
            f"(measure it, or point operating_plane / limits elsewhere)")
    return p


def _resolve_max_gain(chain: dict, planes: dict) -> float:
    """The safety ceiling in gain-space: the tightest of the explicit hardware max
    and every limit (each inverted through its plane against THIS signal's curve).
    A ceiling is mandatory."""
    candidates: list[float] = []
    gl = chain.get("gain_limits") or {}
    if gl.get("max_gain_db") is not None:
        candidates.append(float(gl["max_gain_db"]))
    for lim in (chain.get("limits") or []):
        plane = lim.get("plane")
        if plane not in planes:
            raise CalibrationError(f"limit references unknown plane {plane!r}")
        _require_usable(plane, planes)               # need a curve to invert against
        candidates.append(_gain_for_power_on(float(lim["max_dbm"]), plane, planes))
    if not candidates:
        raise CalibrationError("no safety ceiling derivable — refusing to transmit")
    return min(candidates)


def _quantity_of(plane) -> str:
    return plane.quantity if plane.quantity else ""


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


def resolve_from_files(unit_path, defaults_path, signal_id: str) -> Optional[ResolvedCalibration]:
    """Convenience: load the per-unit doc + the matching type defaults and resolve.
    Returns None when there is no per-unit document at all (caller falls back to the
    script's baked-in defaults). Propagates SignalNotCalibrated / CalibrationError
    otherwise, so the caller can distinguish 'fall back' from 'refuse'."""
    unit_doc = load_unit_doc(unit_path)
    if unit_doc is None:
        return None
    unit_type = unit_doc.get("unit_type", "")
    type_defaults = load_type_defaults(defaults_path, unit_type) if unit_type else None
    return resolve(unit_doc, type_defaults, signal_id)


def resolve_public(unit_path, defaults_path, signal_id: str,
                   unit_type: str = "") -> Optional[dict]:
    """Resolve to the flat public artifact (:meth:`ResolvedCalibration.to_public_dict`)
    the agent injects into a task, or None if there's no per-unit document at all.

    ``unit_type`` (the agent's runtime identity) takes precedence over the doc's own
    ``unit_type`` for selecting the type-defaults layer; if empty the doc's value is
    used. Propagates SignalNotCalibrated / CalibrationError so the caller can tell
    'fall back' from 'refuse'."""
    unit_doc = load_unit_doc(unit_path)
    if unit_doc is None:
        return None
    ut = unit_type or unit_doc.get("unit_type", "")
    if ut and not unit_doc.get("unit_type"):
        unit_doc = {**unit_doc, "unit_type": ut}      # so the artifact reports it
    type_defaults = load_type_defaults(defaults_path, ut) if ut else None
    return resolve(unit_doc, type_defaults, signal_id).to_public_dict()
