"""
Static script-parameter introspection.

Parses a script's *source* with `ast` (no code execution) and extracts its
declared parameters, so the client can render a form for them.

Two declaration styles are recognised:

  1. paramkit  — `Script(...).number(...).choice(...)...` (see paramkit/). Yields
     the rich schema: kind, unit, min/max, named presets, choices. Preferred.
  2. argparse  — plain `parser.add_argument(...)`. Yields the classic fields.

`extract_params(source)` picks the paramkit path when the source uses paramkit and
falls back to argparse otherwise. Every param dict is a SUPERSET of the classic
argparse shape (dest, flags, positional, type, required, default, choices,
is_flag, nargs, help), so older consumers keep working; paramkit adds
name, kind, unit, min, max, step, presets, multiple on top.

No code is ever executed — paramkit scripts are read statically, the same as
argparse ones (module-level `NAME = <literal>` constants, including dict/list
literals used for presets, are resolved).
"""
from __future__ import annotations

import ast
import re
from typing import Any, Dict, List, Optional


# ── Literal / help resolution ────────────────────────────────────────────────

def _literal(node, consts) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name) and node.id in consts:
        return consts[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        v = _literal(node.operand, consts)
        return -v if isinstance(v, (int, float)) else None
    if isinstance(node, ast.BinOp):
        # Resolve simple numeric arithmetic on constants, e.g. max=A + B or a
        # module-level MAX_VALUE = A + B, so computed bounds appear in the static
        # schema too (the runtime already evaluates these before .number()).
        left, right = _literal(node.left, consts), _literal(node.right, consts)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            op = node.op
            try:
                if isinstance(op, ast.Add): return left + right
                if isinstance(op, ast.Sub): return left - right
                if isinstance(op, ast.Mult): return left * right
                if isinstance(op, ast.Div): return left / right
                if isinstance(op, ast.FloorDiv): return left // right
                if isinstance(op, ast.Mod): return left % right
                if isinstance(op, ast.Pow): return left ** right
            except (ZeroDivisionError, ValueError):
                return None
        return None
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_literal(e, consts) for e in node.elts]
    if isinstance(node, ast.Dict):
        out: Dict[Any, Any] = {}
        for k, v in zip(node.keys, node.values):
            if k is None:      # dict unpacking (**x) — can't resolve statically
                continue
            key = _literal(k, consts)
            if key is not None:
                out[key] = _literal(v, consts)
        return out
    return None


def _joined_help(node, consts) -> str:
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.JoinedStr):
        out = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                out.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                inner = v.value
                if isinstance(inner, ast.Name) and inner.id in consts:
                    out.append(str(consts[inner.id]))
                elif isinstance(inner, ast.Constant):
                    out.append(str(inner.value))
                else:
                    out.append("…")
        return "".join(out)
    return ""


def _type_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _collect_consts(tree) -> Dict[str, Any]:
    """Resolve module-level `NAME = <literal>` assignments (incl. dict/list)."""
    consts: Dict[str, Any] = {}
    for n in tree.body:
        if not isinstance(n, ast.Assign) or len(n.targets) != 1:
            continue
        target = n.targets[0]
        if isinstance(target, ast.Name):
            if isinstance(n.value, ast.Constant):
                consts[target.id] = n.value.value
            else:
                v = _literal(n.value, consts)
                if v is not None:
                    consts[target.id] = v
        elif (isinstance(target, (ast.Tuple, ast.List))
              and isinstance(n.value, (ast.Tuple, ast.List))
              and len(target.elts) == len(n.value.elts)):
            # tuple unpacking: A, B = 30.0, 61.44
            for tgt, val in zip(target.elts, n.value.elts):
                if isinstance(tgt, ast.Name):
                    v = _literal(val, consts)
                    if v is not None:
                        consts[tgt.id] = v
    return consts


# ── argparse extractor (classic) ─────────────────────────────────────────────

def extract_argparse_spec(source: str) -> Dict[str, Any]:
    tree = ast.parse(source)
    consts = _collect_consts(tree)

    params = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            continue
        flags = [a.value for a in node.args
                 if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        kw = {k.arg: k.value for k in node.keywords}
        options = [f for f in flags if f.startswith("-")]
        positional = [f for f in flags if not f.startswith("-")]

        if "dest" in kw and isinstance(kw["dest"], ast.Constant):
            dest = kw["dest"].value
        elif options:
            dest = max(options, key=len).lstrip("-").replace("-", "_")
        elif positional:
            dest = positional[0].replace("-", "_")
        else:
            continue

        action = (kw["action"].value if "action" in kw
                  and isinstance(kw["action"], ast.Constant) else None)
        is_flag = action in ("store_true", "store_false")

        params.append({
            "dest": dest,
            "flags": options,
            "positional": not options,
            "type": _type_name(kw["type"]) if "type" in kw else ("str" if not is_flag else None),
            "required": (bool(kw["required"].value) if "required" in kw
                         and isinstance(kw["required"], ast.Constant) else False),
            "default": (_literal(kw["default"], consts) if "default" in kw
                        else (False if action == "store_true"
                              else True if action == "store_false" else None)),
            "choices": _literal(kw["choices"], consts) if "choices" in kw else None,
            "is_flag": is_flag,
            "nargs": (kw["nargs"].value if "nargs" in kw
                      and isinstance(kw["nargs"], ast.Constant) else None),
            "help": _joined_help(kw["help"], consts) if "help" in kw else "",
            # Plain argparse can't declare live tuning; keep the key present so
            # consumers can rely on it existing regardless of the schema source.
            "live": False,
        })
    return {"params": params}


# ── paramkit extractor (rich) ────────────────────────────────────────────────

# builder method name → paramkit "kind"
_BUILDERS = {
    "number": "number",
    "integer": "integer",
    "text": "text",
    "choice": "choice",
    "flag": "flag",
}
# kind → classic argparse "type" (for backward-compatible consumers)
_KIND_TYPE = {"number": "float", "integer": "int", "text": "str",
              "choice": "str", "flag": None}


def _slug(text: str) -> str:
    """Mirror paramkit.slug: 'WiFi ch1 (2.4G)' → 'wifi_ch1_2_4g'."""
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(text).strip().lower())
    return s.strip("_") or "preset"


def _presets_to_list(value) -> List[Dict[str, Any]]:
    """Normalise a resolved presets literal ({label: value} or [(label, value)])
    into [{key, label, value}], mirroring paramkit._normalise_presets."""
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def add(label, val) -> None:
        key = _slug(label)
        base, i = key, 2
        while key in seen:
            key = f"{base}_{i}"; i += 1
        seen.add(key)
        out.append({"key": key, "label": str(label), "value": val})

    if isinstance(value, dict):
        for label, val in value.items():
            add(label, val)
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                add(item[0], item[1])
    return out


def _num(v) -> Optional[float]:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _uses_paramkit(source: str) -> bool:
    return "paramkit" in source


def extract_paramkit_spec(source: str) -> Dict[str, Any]:
    tree = ast.parse(source)
    consts = _collect_consts(tree)

    # Script("<description>") — first positional or description= keyword.
    description = ""
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "Script"):
            if node.args and isinstance(node.args[0], ast.Constant):
                description = str(node.args[0].value)
            else:
                for k in node.keywords:
                    if k.arg == "description" and isinstance(k.value, ast.Constant):
                        description = str(k.value.value)
            break

    # Every builder call, in source order. For a fluent chain
    # (Script(...).number(...).flag(...)) every Call node shares the outer
    # expression's start position, so we sort by where each ".method" token ENDS
    # (node.func is the Attribute for ".number"/".flag"/…), which does increase
    # in source order.
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr in _BUILDERS]
    calls.sort(key=lambda n: (getattr(n.func, "end_lineno", 0) or 0,
                              getattr(n.func, "end_col_offset", 0) or 0))

    params = []
    for node in calls:
        kind = _BUILDERS[node.func.attr]
        kw = {k.arg: k.value for k in node.keywords}
        flags = [a.value for a in node.args
                 if isinstance(a, ast.Constant) and isinstance(a.value, str)]

        if "name" in kw and isinstance(kw["name"], ast.Constant):
            name = kw["name"].value
        else:
            longs = [f for f in flags if f.startswith("--")]
            src = longs[0] if longs else (flags[0] if flags else "")
            name = src.lstrip("-").replace("-", "_")
        if not name:
            continue

        options = [f for f in flags if f.startswith("-")]
        unit = kw["unit"].value if "unit" in kw and isinstance(kw["unit"], ast.Constant) else ""
        presets = _presets_to_list(_literal(kw["presets"], consts)) if "presets" in kw else []
        raw_choices = _literal(kw["options"], consts) if "options" in kw else None
        # options may be a [value, ...] sequence or a {value: label} mapping.
        if isinstance(raw_choices, dict):
            choices = [str(c) for c in raw_choices]
            choice_labels = {str(k): str(v) for k, v in raw_choices.items()}
        elif raw_choices:
            choices = [str(c) for c in raw_choices]
            choice_labels = None
        else:
            choices = None
            choice_labels = None
        multiple = bool(_literal(kw["multiple"], consts)) if "multiple" in kw else False
        required = bool(_literal(kw["required"], consts)) if "required" in kw else False
        live = bool(_literal(kw["live"], consts)) if "live" in kw else False
        default = (_literal(kw["default"], consts) if "default" in kw
                   else (False if kind == "flag" else None))

        params.append({
            # rich (paramkit) fields
            "name": name,
            "kind": kind,
            "unit": unit,
            "min": _num(_literal(kw["min"], consts)) if "min" in kw else None,
            "max": _num(_literal(kw["max"], consts)) if "max" in kw else None,
            "step": _num(_literal(kw["step"], consts)) if "step" in kw else None,
            "presets": presets,
            "multiple": multiple,
            "live": live,
            # classic (argparse-compatible) fields
            "dest": name,
            "flags": options,
            "positional": not options,
            "type": _KIND_TYPE[kind],
            "required": required,
            "default": default,
            "choices": choices,
            "choice_labels": choice_labels,
            "is_flag": kind == "flag",
            "nargs": "+" if multiple else None,
            "help": _joined_help(kw["help"], consts) if "help" in kw else "",
        })
    # A calibration-aware script declares a stable CAL_SIGNAL_ID module constant; a
    # task opts into power calibration by setting SDR_CAL_SIGNAL_ID to it. Surface it
    # so the client can wire the task's env automatically.
    cal_signal = consts.get("CAL_SIGNAL_ID")
    out = {"format": "paramkit", "description": description, "params": params}
    if isinstance(cal_signal, str) and cal_signal:
        out["calibration_signal"] = cal_signal
    return out


# ── Dispatcher ───────────────────────────────────────────────────────────────

def extract_params(source: str) -> Dict[str, Any]:
    """Return the richest schema we can extract: paramkit if the script uses it
    (and it yields params), otherwise the classic argparse schema."""
    if _uses_paramkit(source):
        spec = extract_paramkit_spec(source)
        if spec.get("params"):
            return spec
    return extract_argparse_spec(source)
