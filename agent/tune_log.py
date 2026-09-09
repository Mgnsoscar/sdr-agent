"""
Human-readable log blocks for sequence TUNE steps.

A tune step changes one or more live parameters on a running task. The wire value of a
calibrated ``--power`` is the base measured quantity (e.g. a spectral density in dBm/Hz),
but the operator authored the change thinking in another quantity (main-lobe power, full
signal power, …). Read back from the run log, a bare ``power=-99.65`` is close to useless.

``format_tune_step`` renders every changed parameter as its own grouped block so the log
reads in the quantities the operator thinks in:

  * a calibrated ``--power`` → the base measured quantity AND every declared power-law
    view (main-lobe, full-signal, …), each folded at the step's live operating point;
  * any other parameter → its value, plus any VISIBLE derived readout that tracks it
    (e.g. a passband bandwidth that follows the sidelobe count).

The whole step's text — every changed parameter's block — is returned as ONE string, and
the runner writes it with a single atomic log append, so two tune steps firing at the same
instant can never interleave line-by-line.

Pure and dependency-light (only :mod:`paramkit.power_law`): the runner gathers the argspec,
the resolved calibration artifact, and the task's effective parameter state, and hands them
here; the formatting has no I/O, so it is trivially unit-testable.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional

from paramkit import power_law


# ── Small derived-formula evaluator (mirrors sdr-client state/power_fold.eval_formula) ──
# The scripts' derived fields (a passband bandwidth from the sidelobe count, an equivalent-
# noise bandwidth a power law keys on) are declared as tiny formula dicts. This evaluates
# them over a value source so the agent can fold a law at the live operating point and print
# a derived readout — the same ops the client folds, so the two can't drift.
def eval_formula(formula: Optional[dict], get_value: Callable[[str], Optional[float]]
                 ) -> Optional[float]:
    if not formula:
        return None
    try:
        op, args = next(iter(formula.items()))
    except StopIteration:
        return None
    if not isinstance(args, (list, tuple)):
        return None

    def arg_value(a):
        if isinstance(a, (int, float)) and not isinstance(a, bool):
            return float(a)
        return get_value(str(a))

    vals = [arg_value(a) for a in args]
    if any(v is None for v in vals):
        return None
    if op == "center":
        return sum(vals) / len(vals)
    if op == "span":
        return abs(vals[-1] - vals[0]) if len(vals) >= 2 else 0.0
    if op == "sum":
        return sum(vals)
    if op == "diff":
        return vals[0] - vals[1] if len(vals) >= 2 else vals[0]
    if op == "count":
        a, b, s = vals[0], vals[1], vals[2]
        if s <= 0 or b < a:
            return None
        return float(math.floor((b - a) / s + 1e-9) + 1)
    if op == "span_to":
        a, b, s = vals[0], vals[1], vals[2]
        if s <= 0 or b < a:
            return None
        return float(math.floor((b - a) / s + 1e-9) * s)
    if op == "term":
        a, n, s = vals[0], vals[1], vals[2]
        return a + (n - 1) * s
    if op == "extent":
        n, s = vals[0], vals[1]
        return (n - 1) * s
    if op == "linear":
        if len(vals) < 3:
            return None
        return vals[0] * vals[1] + vals[2]
    if op == "table":
        tbl = vals[1:]
        if not tbl:
            return None
        idx = int(round(vals[0]))
        return tbl[max(0, min(len(tbl) - 1, idx))]
    return None


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except (TypeError, ValueError):
            return None
    return None


def _make_resolver(params_by_dest: Dict[str, dict],
                   effective: Dict[str, Any]) -> Callable[[str], Optional[float]]:
    """A numeric value source for a formula/law over the task's effective state: the field's
    own value → its own derived ``formula`` (an internal quantity like ``enbw_mhz``, a table
    lookup on ``--sidelobes``) → the schema default. Memoised, cycle-guarded."""
    cache: Dict[str, Optional[float]] = {}

    def get(name: str) -> Optional[float]:
        if name in cache:
            return cache[name]
        cache[name] = None                       # guard against a formula cycle
        v = _num(effective.get(name))
        if v is None:
            p = params_by_dest.get(name)
            if p is not None:
                f = p.get("formula")
                if isinstance(f, dict):
                    v = eval_formula(f, get)
                if v is None:
                    v = _num(p.get("default"))
        cache[name] = v
        return v

    return get


def _label(param: Optional[dict], dest: str) -> str:
    """A display label for a parameter: the script's capitalised metavar-style flag
    (``-Passband-bandwidth`` → ``Passband bandwidth``) when present, else the humanised dest."""
    for f in (param or {}).get("flags", []) or []:
        s = str(f).lstrip("-")
        if s and any(c.isupper() for c in s):
            return s.replace("-", " ").replace("_", " ")
    h = dest.replace("_", " ").strip()
    return (h[:1].upper() + h[1:]) if h else dest


def _fmt_num(v: Optional[float]) -> str:
    if v is None:
        return "?"
    if abs(v - round(v)) < 1e-9:
        return f"{v:.0f}"
    return f"{v:.4f}".rstrip("0").rstrip(".")


def _is_power_dest(dest: str, param: Optional[dict]) -> bool:
    if dest == "power":
        return True
    for f in (param or {}).get("flags", []) or []:
        if str(f).lower() in ("--power", "-power"):
            return True
    return False


def _quantity_lines(base: float, artifact: Optional[dict],
                    laws: List[dict], resolver: Callable[[str], Optional[float]]
                    ) -> List[tuple]:
    """(value_str, unit, name) for the base measured quantity and each declared power-law view."""
    rows: List[tuple] = []
    q = ((artifact or {}).get("quantity") or "power")
    # The measured-quantity display unit is published as ``operating_unit`` (``unit`` is a
    # legacy alias some artifacts still carry); either is the base line's unit.
    unit = (artifact or {}).get("operating_unit") or (artifact or {}).get("unit") or ""
    rows.append((_fmt_num(base), unit, q[:1].upper() + q[1:] if q else "power"))
    for law in laws:
        try:
            parsed = power_law.parse_law(law)
            keyed = {}
            p = law.get("param")
            if p:
                keyed[p] = resolver(p)
            if any(val is None for val in keyed.values()):
                continue                          # can't fold this view → skip it, don't guess
            delta = parsed.delta_db(keyed)
        except Exception:                          # noqa: BLE001 — a bad law never breaks the log
            continue
        rows.append((_fmt_num(base + delta), law.get("unit") or "",
                     str(law.get("name") or law.get("id") or "")))
    return rows


def _derived_readouts(dest: str, params: List[dict],
                      resolver: Callable[[str], Optional[float]]) -> List[tuple]:
    """(value_str, unit, label) for every VISIBLE derived field whose formula reads ``dest``."""
    out: List[tuple] = []
    for p in params:
        if p.get("hidden"):
            continue
        f = p.get("formula")
        if not isinstance(f, dict):
            continue
        args = next(iter(f.values()), None)
        srcs = [str(a) for a in args if isinstance(a, str)] if isinstance(args, (list, tuple)) else []
        if dest not in srcs:
            continue
        val = eval_formula(f, resolver)
        if val is None:
            continue
        out.append((_fmt_num(val), p.get("unit") or "", _label(p, p.get("name") or "")))
    return out


def format_tune_step(task_name: str, changed: Dict[str, Any], effective: Dict[str, Any],
                     spec: Optional[dict], artifact: Optional[dict], clock: str) -> Optional[str]:
    """The full log text for one tune step — a grouped block per changed parameter, every
    header stamped with the same ``clock`` so a step's blocks read as one instant. Returns
    None when there is nothing meaningful to render (the runner then keeps its plain line)."""
    if not changed:
        return None
    spec = spec or {}
    params = spec.get("params") or []
    by_dest = {p.get("dest"): p for p in params if p.get("dest")}
    laws = spec.get("calibration_power_laws") or []
    resolver = _make_resolver(by_dest, effective)

    blocks: List[str] = []
    for dest, value in changed.items():
        param = by_dest.get(dest)
        header = f"[{clock}] ◈ {task_name}    •    {_label(param, dest)}"
        rows: List[tuple] = []
        if _is_power_dest(dest, param) and artifact and laws:
            base = _num(value)
            if base is not None:
                rows = _quantity_lines(base, artifact, laws, resolver)
        if not rows:
            # A plain parameter (or an uncalibrated power): its own value, then any derived
            # readout that tracks it.
            unit = (param or {}).get("unit") or ""
            v = _num(value)
            rows = [(_fmt_num(v) if v is not None else str(value), unit, "")]
            rows += _derived_readouts(dest, params, resolver)
        blocks.append(_render_block(header, rows))
    return "\n".join(blocks) if blocks else None


def _render_block(header: str, rows: List[tuple]) -> str:
    """Header line + aligned ``value unit • name`` body lines (name omitted when blank)."""
    vw = max((len(v) for v, _u, _n in rows), default=0)
    uw = max((len(u) for _v, u, _n in rows), default=0)
    lines = [header]
    for v, u, n in rows:
        line = f"            {v:>{vw}}"
        if u:
            line += f" {u:<{uw}}"
        elif uw:
            line += " " * (uw + 1)
        if n:
            line += f"  • {n}"
        lines.append(line.rstrip())
    return "\n".join(lines)
