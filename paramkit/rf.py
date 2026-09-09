"""
RF output gate — the shared notion of an on/off control that MUTES the unit.

A transmit script declares one parameter as its RF gate: an on/off control that, when off, means
the unit is not emitting. While off the script zeros its SDR gain + baseband amplitude, and the
agent additionally drives every programmable attenuator to maximum attenuation. Because an off
state emits nothing, the run log and the spreadsheet export blank the power quantities on an off
row instead of showing the phantom held level (a plot of power-vs-time then simply has no points
during a muted warm-up).

A gate is recognised either by the explicit ``is_rf`` marker (``paramkit .choice(is_rf=True)``)
or, for scripts that predate the marker, by the ``--rf`` on/off convention. This module is pure
stdlib and works on the argspec parameter dicts (``extract_params(...)["params"]``), so the agent
(run_table, tune_log, process_manager) shares one source of truth for what the gate is and what
"on" means.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# The tokens that mean "transmitting" — mirrors every transmit script's own
# ``str(value).strip().lower() in (...)`` parse of ``--rf``.
ON_TOKENS = frozenset({"on", "1", "true", "yes"})


def is_on(value: Any) -> bool:
    """Whether an RF-gate value means the output is ON. Matches the scripts' own parsing."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ON_TOKENS


def _looks_like_rf(param: Dict[str, Any]) -> bool:
    """The ``--rf`` on/off convention: dest ``rf`` or a ``--rf``/``-RF`` flag, whose choices (if
    declared) are the on/off set — so a script that predates the ``is_rf`` marker is still
    recognised. (Declared choices that aren't on/off rule it out — some other ``rf*`` control.)"""
    dest = param.get("dest") or param.get("name")
    flags = [str(f).lower() for f in (param.get("flags") or [])]
    named_rf = dest == "rf" or "--rf" in flags or "-rf" in flags
    if not named_rf:
        return False
    choices = {str(c).strip().lower() for c in (param.get("choices") or [])}
    return not choices or choices <= {"on", "off"}


def gate(params: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """The RF-gate parameter among ``params`` (argspec dicts), or None. An explicit ``is_rf``
    marker wins; otherwise the ``--rf`` on/off convention."""
    if not params:
        return None
    for p in params:
        if p.get("is_rf"):
            return p
    for p in params:
        if _looks_like_rf(p):
            return p
    return None


def gate_dest(params: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """The dest of the RF gate, or None if there is no gate."""
    g = gate(params)
    return (g.get("dest") or g.get("name")) if g else None


def is_muted(params: Optional[List[Dict[str, Any]]],
             values: Optional[Dict[str, Any]]) -> bool:
    """True iff ``params`` declares an RF gate AND its effective value is OFF. The gate value is
    read from ``values``; if absent there it falls back to the gate's schema default (so a launch
    that never set ``--rf`` uses the default). No gate ⇒ never muted (backward compatible)."""
    g = gate(params)
    if g is None:
        return False
    values = values or {}
    dest = g.get("dest") or g.get("name")
    if dest in values and values.get(dest) is not None:
        return not is_on(values.get(dest))
    default = g.get("default")
    if default is not None:
        return not is_on(default)
    return False
