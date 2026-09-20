"""
Launch-command argument helpers shared by the SequenceRunner (restart reconstruction) and the
ProcessManager (standalone auto-restart relaunch): bake a live-parameter value back onto a
script's command line by its argspec flags. Pure functions, no agent imports — so
process_manager can use them without a circular import of sequence_runner.
"""
from __future__ import annotations

from typing import Optional

# The flags every shipped script uses for the absolute power / SDR gain parameters.
POWER_FLAGS = ("--power", "-Power")
# Canonical flags for the two level parameters, used to bake a reconstructed level onto a relaunch
# command when the argspec is momentarily unreadable (spec=None) so the per-dest flag map is empty.
# Power is the over-power hazard, so it must never fall back to the launch value.
LEVEL_FALLBACK_FLAGS = {"power": tuple(POWER_FLAGS), "gain": ("--gain", "-Gain")}


def dest_flag_map(spec: Optional[dict]) -> dict:
    """{dest: [flags…]} from a script's argspec — the flag(s) that set each parameter, so a
    reconstructed live-param value can be baked back onto the relaunch command."""
    out: dict = {}
    for p in (spec or {}).get("params", []) or []:
        dest = p.get("dest")
        flags = [str(f) for f in (p.get("flags") or [])]
        if dest and flags:
            out[dest] = flags
    return out


def dest_kind_map(spec: Optional[dict]) -> dict:
    """{dest: kind} from a script's argspec ("number" / "integer" / "choice" / …)."""
    return {p.get("dest"): p.get("kind") for p in (spec or {}).get("params", []) or [] if p.get("dest")}


def num_text(value, kind: Optional[str] = None) -> str:
    """The CLI text for a numeric value, EXACT: a whole number without a trailing .0 (an int-typed
    param accepts it), else the shortest round-trip repr — never `%g`, which rounds to 6 significant
    digits (a 1602.5625 MHz carrier became 1602.56, an integer 1234567 became 1.23457e+06 — refused
    by an integer param). An integer-kind param gets a rounded whole number for any value."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if v != v or v in (float("inf"), float("-inf")):
        return repr(v)
    if kind == "integer" or v.is_integer():
        return str(int(round(v)))
    return repr(v)


def _split_eq(tok, flagset):
    """(flag, value) for a `--flag=value` token whose flag is in `flagset`, else None."""
    t = str(tok)
    if "=" in t:
        f, v = t.split("=", 1)
        if f in flagset:
            return f, v
    return None


def set_arg_value(args: list, flags, value, canonical: Optional[str] = None) -> list:
    """Set/replace the value following any flag in `flags` (last occurrence wins; a `--flag=value`
    token is rewritten in place), appending `canonical value` when the flag is absent. Returns a
    new list."""
    flagset = {str(f) for f in flags}
    out = list(args or [])
    found = False
    i = 0
    while i < len(out):
        if str(out[i]) in flagset and i + 1 < len(out):
            out[i + 1] = str(value)
            found = True
            i += 2
            continue
        if str(out[i]) in flagset:                       # a dangling flag as the LAST token: give it
            out.append(str(value))                       # its value instead of a second flag (R6)
            found = True
            break
        eq = _split_eq(out[i], flagset)
        if eq is not None:
            out[i] = f"{eq[0]}={value}"
            found = True
        i += 1
    if not found:
        can = canonical or (sorted(flagset)[0] if flagset else None)
        if can is not None:
            out += [can, str(value)]
    return out


def post_script_args(cmd: list) -> list:
    """A launch command's arguments AFTER the script path — the flags a param parse reads."""
    for i, a in enumerate(cmd):
        if isinstance(a, str) and a.endswith(".py"):
            return list(cmd[i + 1:])
    return list(cmd[1:]) if len(cmd) > 1 else []


def overlay_live_params(args: list, live: dict, spec: Optional[dict], gate: Optional[dict]) -> list:
    """Bake the live-tuned parameter values `live` ({dest: value}) onto a launch's post-script args:
    the RF output gate via its own flags (a string on/off), every other dest with known flags by
    value (booleans are skipped — a store_true flag has no value to set). Returns a new list."""
    dest_flags = dest_flag_map(spec)
    kinds = dest_kind_map(spec)
    gate_dest = (gate.get("dest") or gate.get("name")) if gate else None
    gate_flags = [str(f) for f in (gate.get("flags") or [])] if gate else []
    out = list(args or [])
    for dest, value in (live or {}).items():
        if isinstance(value, bool):
            continue
        if gate_dest is not None and dest == gate_dest:
            if gate_flags:
                out = set_arg_value(out, gate_flags, str(value), canonical=gate_flags[0])
            continue
        flags = dest_flags.get(dest) or LEVEL_FALLBACK_FLAGS.get(dest)
        if not flags:
            continue
        text = num_text(value, kinds.get(dest)) if isinstance(value, (int, float)) else str(value)
        out = set_arg_value(out, list(flags), text, canonical=flags[0])
    return out


# ── Elapsed-time parameter (a time-dependent script's resume point) ──────────────────────────

def elapsed_param(spec: Optional[dict]) -> Optional[dict]:
    """The script's ELAPSED-TIME parameter (its argspec entry with `is_elapsed`, see
    paramkit.Param.is_elapsed), or None when the script declares none / the spec is unreadable.
    The first flagged param wins."""
    for p in (spec or {}).get("params", []) or []:
        if p.get("is_elapsed") and p.get("dest") and (p.get("flags") or []):
            return p
    return None


def resets_elapsed_dests(spec: Optional[dict]) -> set:
    """The dests of the script's elapsed-RESET triggers (params with `resets_elapsed`, see
    paramkit.Param.resets_elapsed): a counted tune that fires one restarts the script's own clock,
    so a restart counts the elapsed from that instant, not from the launch."""
    return {p.get("dest") for p in (spec or {}).get("params", []) or []
            if p.get("resets_elapsed") and p.get("dest")}


def is_reset_fire(params: dict, reset_dests: set) -> bool:
    """Whether a tune's params fire an elapsed-reset trigger (a truthy value on a reset dest)."""
    for d in reset_dests:
        if d in (params or {}):
            v = params[d]
            if isinstance(v, str):
                if v.strip().lower() in ("1", "true", "yes", "on"):
                    return True
            elif v:
                return True
    return False


def arg_value(args: list, flags, default=None):
    """The value following the LAST occurrence of any flag in `flags` (argparse semantics — the
    last one wins), else `default`."""
    flagset = {str(f) for f in flags}
    val = default
    for i, a in enumerate(args or []):
        if str(a) in flagset and i + 1 < len(args):
            val = args[i + 1]
            continue
        eq = _split_eq(a, flagset)
        if eq is not None:
            val = eq[1]
    return val


def elapsed_of_args(args: list, param: dict) -> float:
    """The elapsed seconds a launch's post-script args already carry on the elapsed param (its
    LAST occurrence), else the param's declared default, else 0. Never raises (0 on junk)."""
    raw = arg_value(args, [str(f) for f in (param.get("flags") or [])], None)
    if raw is None:
        raw = param.get("default")
    try:
        v = float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        v = 0.0
    return v if v == v and v >= 0 else 0.0            # NaN / negative → 0


def bake_elapsed(args: list, param: dict, elapsed_s: float) -> list:
    """Set the elapsed param on a launch's post-script args to `elapsed_s` seconds (clamped ≥ 0;
    ms resolution for a number param, a rounded whole second for an integer(...) param — its
    parser refuses a fraction) via its own flags — the relaunch of a time-dependent script resumes
    there."""
    flags = [str(f) for f in (param.get("flags") or [])]
    if not flags:
        return list(args or [])
    val = max(0.0, float(elapsed_s))
    if val != val:
        val = 0.0
    if param.get("kind") == "integer" or str(param.get("type") or "") == "int":
        text = str(int(round(val)))
    else:
        text = f"{val:.3f}".rstrip("0").rstrip(".")
    return set_arg_value(args, flags, text, canonical=flags[0])
