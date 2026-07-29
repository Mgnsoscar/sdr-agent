"""
Static argparse introspection.

Parses a script's *source* with `ast` (no code execution) and extracts the
parameters declared via argparse `add_argument(...)` calls, so the client can
render a form for them. Module-level `NAME = <literal>` constants are resolved
so defaults like `default=DEFAULT_OVERSAMPLE` come through as their value, and
f-string help is reconstructed on a best-effort basis.

Returns: {"params": [ {dest, flags, positional, type, required, default,
                        choices, is_flag, nargs, help}, ... ]}
Scripts that don't use argparse simply yield an empty param list.
"""
from __future__ import annotations

import ast
from typing import Any, Dict


def _literal(node, consts) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name) and node.id in consts:
        return consts[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        v = _literal(node.operand, consts)
        return -v if isinstance(v, (int, float)) else None
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_literal(e, consts) for e in node.elts]
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


def extract_argparse_spec(source: str) -> Dict[str, Any]:
    tree = ast.parse(source)

    consts: Dict[str, Any] = {}
    for n in tree.body:
        if (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)):
            if isinstance(n.value, ast.Constant):
                consts[n.targets[0].id] = n.value.value
            else:
                v = _literal(n.value, consts)
                if v is not None:
                    consts[n.targets[0].id] = v

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
        })
    return {"params": params}