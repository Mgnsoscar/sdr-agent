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


def set_arg_value(args: list, flags, value, canonical: Optional[str] = None) -> list:
    """Set/replace the value following any flag in `flags` (last occurrence wins), appending
    `canonical value` when the flag is absent. Returns a new list."""
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
        text = f"{float(value):g}" if isinstance(value, (int, float)) else str(value)
        out = set_arg_value(out, list(flags), text, canonical=flags[0])
    return out
