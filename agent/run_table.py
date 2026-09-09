"""
Tabular run log — one row per state CHANGE, every parameter in its own column.

A sequence/plan run is a timeline of steps on a duration task. This reconstructs, for one
task, the time-series a spreadsheet wants: a header of columns (every power quantity, the
realized SDR gain + attenuation, each live parameter, and each visible derived readout) and a
row at every instant the state actually changed — a launch is the first row; a later tune that
moves anything adds a row; a tune that changes nothing adds none.

Pure and dependency-light (reuses :mod:`agent.tune_log` for the quantity/derived fold and a
caller-supplied ``realize`` for the SDR gain / attenuator split). The client turns one of these
per unit into a sheet of an .xlsx workbook.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from . import tune_log
from paramkit import power_law
from paramkit import rf as _rf


def _fnum(v: Optional[float], places: int = 6):
    """Round a float for the sheet (kept numeric, not stringified), pass through non-floats."""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        r = round(float(v), places)
        return int(r) if r == int(r) else r
    return v


def _hhmmss(iso: Optional[str]) -> str:
    """Fire time as HH:MM:SS.mmm. Millisecond precision matters: a ramp can fire points
    well under a second apart, so whole-second truncation would print two DISTINCT,
    sequential state changes under one timestamp (they look simultaneous in the sheet)."""
    if not iso or not isinstance(iso, str):
        return ""
    # "2026-09-09T09:00:27.071796+00:00" → "09:00:27.071"
    t = iso.split("T", 1)[1] if "T" in iso else iso
    t = t.split("+", 1)[0].split("Z", 1)[0]          # drop the timezone suffix
    hms, _, frac = t.partition(".")
    return f"{hms[:8]}.{(frac + '000')[:3]}"


def _args_to_params(args: list, flag_to_dest: dict) -> dict:
    out: dict = {}
    i = 0
    while i < len(args):
        dest = flag_to_dest.get(str(args[i]))
        if dest is None:
            i += 1
            continue
        nxt = args[i + 1] if i + 1 < len(args) else None
        if nxt is not None and str(nxt) not in flag_to_dest:
            try:
                out[dest] = float(nxt)
            except (TypeError, ValueError):
                out[dest] = nxt
            i += 2
        else:
            out[dest] = True
            i += 1
    return out


def _reads(formula: dict, dest: str) -> bool:
    args = next(iter(formula.values()), None) if isinstance(formula, dict) else None
    srcs = [str(a) for a in args if isinstance(a, str)] if isinstance(args, (list, tuple)) else []
    return dest in srcs


def _rf_like(param: Optional[dict], value: Any) -> bool:
    """An on/off parameter (rendered 1/0 under a '… on' header, like the example's 'RF on')."""
    if isinstance(value, str) and value.strip().lower() in ("on", "off"):
        return True
    choices = [str(c).lower() for c in (param or {}).get("choices", []) or []]
    return set(choices) == {"on", "off"}


class _Col:
    __slots__ = ("header", "kind", "ref")

    def __init__(self, header: str, kind: str, ref: Any = None):
        self.header, self.kind, self.ref = header, kind, ref


def _columns(spec: dict, artifact: Optional[dict], laws: list, by_dest: dict,
             has_realize: bool) -> List[_Col]:
    cols: List[_Col] = [_Col("Time", "time")]
    power = by_dest.get("power")
    calibrated = bool(power and artifact)
    if calibrated:
        base_q = (artifact.get("quantity") or "power")
        base_u = artifact.get("operating_unit") or artifact.get("unit") or ""
        cols.append(_Col(_hdr(base_q[:1].upper() + base_q[1:], base_u), "power_base"))
        for law in laws:
            cols.append(_Col(_hdr(law.get("name") or law.get("id") or "", law.get("unit") or ""),
                             "law", law))
        if has_realize:
            cols.append(_Col("SDR gain [dB]", "sdr_gain"))
            if artifact.get("active_components"):
                cols.append(_Col("Attenuation [dB]", "atten"))
    # Operator-facing parameters (excluding power/gain, which the quantities + SDR gain already
    # cover, and hidden/derived fields, which the derived readouts below cover). LIVE params come
    # first — each followed by any visible derived readout that tracks it — then the FIXED params
    # at the end as a constant reference (a self-contained record). A derived field is never a
    # plain column; it appears only as a readout under the parameter it tracks.
    all_params = spec.get("params", []) or []
    emitted_derived: set = set()

    def _plain(p) -> bool:
        d = p.get("dest")
        return bool(d) and not p.get("hidden") and not isinstance(p.get("formula"), dict) \
            and d not in ("power", "gain")

    def _emit(p) -> None:
        d = p["dest"]
        if _rf_like(p, p.get("default")) or d == "rf":
            cols.append(_Col(f"{tune_log._label(p, d)} on", "rf", d))
        else:
            cols.append(_Col(_hdr(tune_log._label(p, d), p.get("unit") or ""), "param", d))
        for dp in all_params:                        # visible derived readouts that track d
            dd = dp.get("dest")
            if not dd or dd in emitted_derived or dp.get("hidden") \
                    or not isinstance(dp.get("formula"), dict):
                continue
            if _reads(dp["formula"], d):
                emitted_derived.add(dd)
                cols.append(_Col(_hdr(tune_log._label(dp, dd), dp.get("unit") or ""), "derived", dp))

    for p in all_params:                             # live params (+ their derived) first
        if _plain(p) and p.get("live"):
            _emit(p)
    for p in all_params:                             # then fixed params as constant columns
        if _plain(p) and not p.get("live"):
            _emit(p)
    return cols


def _hdr(name: str, unit: str) -> str:
    return f"{name} [{unit}]" if unit else name


def _row_values(cols: List[_Col], effective: dict, by_dest: dict, artifact: Optional[dict],
                realize: Optional[Callable], freq_hz: Optional[float],
                rf_gate: Optional[dict] = None) -> list:
    resolver = tune_log._make_resolver(by_dest, effective)
    base = tune_log._num(effective.get("power"))
    # RF output gate off ⇒ MUTED: nothing is emitting, so blank every power quantity and report the
    # muted device state (SDR gain 0, attenuators at max) instead of the phantom held level.
    rf_off = False
    if rf_gate is not None:
        gd = rf_gate.get("dest") or rf_gate.get("name")
        rf_off = not _rf.is_on(effective.get(gd, rf_gate.get("default")))
    real = None
    if realize is not None:
        try:
            real = (realize(None, freq_hz, rf_on=False) if rf_off
                    else (realize(base, freq_hz) if base is not None else None))
        except Exception:                            # noqa: BLE001
            real = None
    out: list = []
    for c in cols:
        if c.kind == "time":
            out.append("")                           # filled by the caller (the fire time)
        elif c.kind == "power_base":
            out.append(None if rf_off else _fnum(base))
        elif c.kind == "law":
            v = None
            if not rf_off and base is not None:
                try:
                    parsed = power_law.parse_law(c.ref)
                    keyed = {}
                    if c.ref.get("param"):
                        keyed[c.ref["param"]] = resolver(c.ref["param"])
                    if not any(x is None for x in keyed.values()):
                        v = base + parsed.delta_db(keyed)
                except Exception:                    # noqa: BLE001
                    v = None
            out.append(_fnum(v))
        elif c.kind == "sdr_gain":
            out.append(_fnum((real or {}).get("sdr_gain_db")))
        elif c.kind == "atten":
            out.append(_fnum((real or {}).get("atten_db")))
        elif c.kind == "rf":
            val = effective.get(c.ref)
            out.append(1 if (isinstance(val, str) and val.strip().lower() == "on") or val is True else 0)
        elif c.kind == "param":
            out.append(_fnum(tune_log._num(effective.get(c.ref)))
                       if tune_log._num(effective.get(c.ref)) is not None else effective.get(c.ref))
        elif c.kind == "derived":
            out.append(_fnum(tune_log.eval_formula(c.ref["formula"], resolver)))
        else:
            out.append(None)
    return out


def build_task_table(task_name: str, steps: list, spec: Optional[dict], artifact: Optional[dict],
                     realize: Optional[Callable] = None, *, freq_hz: Optional[float] = None) -> dict:
    """Reconstruct one task's per-change table. ``steps`` is the run's fired StepFire list (any
    order); ``realize(power_dbm, freq_hz)`` returns ``{'sdr_gain_db', 'atten_db'}`` (or None).
    Returns ``{'task', 'columns': [str], 'rows': [[...]]}`` — a row only where a value changed."""
    spec = spec or {}
    params = spec.get("params", []) or []
    by_dest = {p.get("dest"): p for p in params if p.get("dest")}
    laws = spec.get("calibration_power_laws") or []
    flag_to_dest = {str(f): p["dest"] for p in params if p.get("dest") for f in (p.get("flags") or [])}
    rf_gate = _rf.gate(params)                        # the RF output gate (muted power ⇒ blank cells)

    fired = [s for s in steps if getattr(s, "task_name", None) == task_name
             and getattr(s, "fired_actual", None) and str(getattr(s, "fired_actual")) != "skipped"
             and getattr(s, "action", None) in ("start", "run", "tune")]
    fired.sort(key=lambda s: str(s.fired_actual))
    cols = _columns(spec, artifact, laws, by_dest, has_realize=realize is not None)

    rows: List[list] = []
    effective: dict = {}
    last: Optional[list] = None
    for s in fired:
        if s.action in ("start", "run"):
            effective.update(_args_to_params(list(getattr(s, "args", []) or []), flag_to_dest))
        if getattr(s, "params", None):
            effective.update(dict(s.params))
        values = _row_values(cols, effective, by_dest, artifact, realize, freq_hz, rf_gate)
        body = values[1:]                            # everything but Time
        if last is not None and body == last:
            continue                                 # nothing changed → no new row
        last = body
        values[0] = _hhmmss(str(s.fired_actual))
        rows.append(values)
    return {"task": task_name, "columns": [c.header for c in cols], "rows": rows}
