"""
Shared, pure power-quantity BRIDGES: how a node's REPORTED and LIMITING readings
derive from the single curve you measured at that node.

A calibration node is measured ONCE (a gain -> power curve in one quantity, e.g. spectral
density). Two readings are computed from that measurement:

  * REPORTED  -- the number the operator reads and sets as ``--power``;
  * LIMITING  -- the number a safety ceiling is gauged against.

Each reading relates to the measurement by a BRIDGE, one of:

  * ``same``  -- the reading IS the measured quantity, up to a constant ``k`` dB (a pure
                 denominator restatement, e.g. dBm/Hz -> dBm/MHz is +60 dB);
  * ``law``   -- a signal-declared conversion that may change the quantity itself and may
                 depend on a runtime task parameter (e.g. total power = density +
                 10*log10(bandwidth)); the ONLY bridge that crosses unit families;
  * ``own``   -- the reading is measured independently (its own curve); this module treats
                 its bridge delta as 0 (the curve is resolved elsewhere).

A LAW is affine in log10 of task parameters:

    out = in + k + Sum_i  coeff_i * log10( params[param_i] / ref_i )

``k`` alone is a constant offset (a PRN peak->total ratio, a denominator restatement); one
log term is a density<->total or per-tooth<->total conversion; several terms handle a law
keyed on more than one parameter. Every dB power-quantity conversion in RF is of this shape,
so the template is closed (no expression evaluator) yet covers the real cases -- and it is
identical to evaluate in Python and JS, which matters because the SAME law is applied by the
agent resolver (for the UI bounds), the transmit script (at the live parameter value), and
the client fold (for the form read-out). A parity bug here would mean wrong emitted power, so
the shape is deliberately small and declarative.

Unit FAMILIES: ``abs`` (absolute power, dBm) and ``density`` (spectral density). A law fixes
the family of each side (``in_fam``/``out_fam``); it is only applicable when the measurement's
family matches ``in_fam``, and the reading it produces is in ``out_fam``. Within the density
family the denominator (/Hz, /kHz, /MHz) is a pure constant offset, carried in ``k``.

This module is imported by BOTH the agent resolver (agent/calibration.py) and the transmit
script (paramkit/calkit.py); the client mirrors it verbatim (sdr-client/state/power_law.py).
Keep the copies in step -- it is pure (stdlib only) so they can be byte-identical.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# Unit families. A bridge/law fixes the family of each side; within DENSITY the
# denominator (/Hz, /kHz, /MHz) is a pure constant offset carried in the reading's `k`.
ABS = "abs"
DENSITY = "density"
_FAMILIES = (ABS, DENSITY)

# Bridge kinds.
SAME = "same"    # reading == measurement (+ constant k, e.g. a denominator restatement)
LAW = "law"      # a signal-declared conversion (may change family; may key on a param)
OWN = "own"      # reading has its own measured curve (resolved elsewhere; delta is 0 here)
_KINDS = (SAME, LAW, OWN)


@dataclass
class LawTerm:
    """One ``coeff * log10(param / ref)`` term of a law."""
    param: str            # task parameter dest whose value drives this term
    coeff: float          # dB per decade of (param / ref)
    ref: float = 1.0      # reference the param is normalized against (same unit as param)
    rep: Optional[float] = None   # a representative param value for the agent's scalar
                                  # read-outs (defaults to ref); runtime uses the live value

    @property
    def rep_value(self) -> float:
        return self.ref if self.rep is None else self.rep


@dataclass
class Law:
    """A signal-declared conversion, affine in log10 of task parameters:

        out = in + k + Sum  term.coeff * log10( params[term.param] / term.ref )

    ``in_fam``/``out_fam`` fix the unit family each side lives in. A law with no terms is a
    pure constant ``k`` (``out = in + k``)."""
    id: str
    name: str
    in_fam: str = ABS
    out_fam: str = ABS
    k: float = 0.0
    terms: list = field(default_factory=list)   # list[LawTerm]

    def params(self) -> list:
        """The task-parameter dests this law reads (empty for a pure-constant law)."""
        return [t.param for t in self.terms]

    def rep_delta_db(self) -> float:
        """The dB the law adds at its representative parameter values — for the agent's
        scalar read-outs when no live value is available (the client/script re-evaluate at
        the live value). Each term uses its ``rep`` (or ``ref``), so a term at its reference
        contributes 0 and the law reduces to ``k``."""
        d = float(self.k)
        for t in self.terms:
            d += float(t.coeff) * math.log10(t.rep_value / float(t.ref))
        return d

    def delta_db(self, values: dict) -> float:
        """The dB the law adds to the measured value, given a ``{param: value}`` mapping.
        A missing parameter or a non-positive value/ref raises ValueError so the caller
        can fail safe rather than emit a wrong power."""
        d = float(self.k)
        for t in self.terms:
            if t.param not in values or values[t.param] is None:
                raise ValueError(f"law {self.id!r} needs parameter {t.param!r}")
            v = float(values[t.param])
            if v <= 0.0 or t.ref <= 0.0:
                raise ValueError(
                    f"law {self.id!r}: log10 needs positive {t.param!r} and ref")
            d += float(t.coeff) * math.log10(v / float(t.ref))
        return d

    def to_public_dict(self) -> dict:
        out = {"id": self.id, "name": self.name, "in": self.in_fam, "out": self.out_fam}
        if self.k:
            out["k"] = self.k
        if self.terms:
            terms = []
            for t in self.terms:
                td = {"param": t.param, "coeff": t.coeff, "ref": t.ref}
                if t.rep is not None:
                    td["rep"] = t.rep
                terms.append(td)
            out["terms"] = terms
        return out


@dataclass
class Bridge:
    """How one reading (reported or limiting) relates to the node's measurement."""
    kind: str = SAME
    k: float = 0.0             # constant dB (SAME: denominator/offset; ignored for LAW/OWN)
    law: Optional[Law] = None  # when kind == LAW
    unit: str = ""             # display-unit string, metadata (e.g. "dBm", "dBm/MHz")

    @property
    def is_same(self) -> bool:
        return self.kind == SAME

    @property
    def is_law(self) -> bool:
        return self.kind == LAW

    @property
    def is_own(self) -> bool:
        return self.kind == OWN

    def keyed_params(self) -> list:
        """Task parameters whose live values this bridge needs (empty unless a param law)."""
        return self.law.params() if (self.kind == LAW and self.law) else []

    @property
    def is_constant(self) -> bool:
        """True when the bridge delta does not depend on any runtime parameter -- so the
        agent can bake it at resolve time instead of carrying it for runtime evaluation."""
        return not self.keyed_params()

    def delta_db(self, values: Optional[dict] = None) -> float:
        """dB added to the measured value to get this reading. SAME -> k; LAW -> its law;
        OWN -> 0 (its curve is independent and handled by the caller)."""
        if self.kind == LAW and self.law:
            return self.law.delta_db(values or {})
        return float(self.k)   # SAME (constant k) or OWN (0 by construction)

    def rep_delta_db(self) -> float:
        """dB at representative parameter values, for the agent's scalar read-outs (the
        client/script re-fold at the live value). SAME -> k; LAW -> its rep delta; OWN -> 0."""
        if self.kind == LAW and self.law:
            return self.law.rep_delta_db()
        return float(self.k)

    def to_public_dict(self) -> dict:
        out = {"kind": self.kind}
        if self.unit:
            out["unit"] = self.unit
        if self.kind == LAW and self.law:
            out["law"] = self.law.to_public_dict()
        elif self.k:
            out["k"] = self.k
        return out


def parse_law(spec: object) -> Law:
    """Build a Law from a declared dict (fail hard on a malformed declaration).

    Accepts a single-term convenience shape -- ``{param, coeff, ref}`` inline -- as well as
    an explicit ``terms`` list, and both ``in``/``out`` and ``in_fam``/``out_fam`` keys."""
    if not isinstance(spec, dict):
        raise ValueError("law must be an object")
    lid = str(spec.get("id") or spec.get("name") or "").strip()
    if not lid:
        raise ValueError("law needs an 'id' or 'name'")
    name = str(spec.get("name") or lid).strip()
    in_fam = spec.get("in", spec.get("in_fam", ABS))
    out_fam = spec.get("out", spec.get("out_fam", ABS))
    if in_fam not in _FAMILIES or out_fam not in _FAMILIES:
        raise ValueError(
            f"law {lid!r}: 'in'/'out' family must each be one of {_FAMILIES}")
    try:
        k = float(spec.get("k", 0.0))
    except (TypeError, ValueError):
        raise ValueError(f"law {lid!r}: 'k' must be numeric")
    raw_terms = spec.get("terms")
    if raw_terms is None and spec.get("param"):        # single-term convenience shape
        raw_terms = [{"param": spec["param"], "coeff": spec.get("coeff", 10.0),
                      "ref": spec.get("ref", spec.get("ref_hz", 1.0)),
                      "rep": spec.get("rep", spec.get("rep_hz"))}]
    terms: list = []
    for t in (raw_terms or []):
        if not isinstance(t, dict):
            raise ValueError(f"law {lid!r}: each term must be an object")
        p = str(t.get("param", "")).strip()
        if not p:
            raise ValueError(f"law {lid!r}: a term needs a 'param'")
        try:
            coeff = float(t.get("coeff", 10.0))
            ref = float(t.get("ref", 1.0))
            rep = float(t["rep"]) if t.get("rep") is not None else None
        except (TypeError, ValueError):
            raise ValueError(f"law {lid!r}: term coeff/ref/rep must be numeric")
        if ref <= 0.0:
            raise ValueError(f"law {lid!r}: term 'ref' must be > 0")
        if rep is not None and rep <= 0.0:
            raise ValueError(f"law {lid!r}: term 'rep' must be > 0")
        terms.append(LawTerm(param=p, coeff=coeff, ref=ref, rep=rep))
    return Law(id=lid, name=name, in_fam=in_fam, out_fam=out_fam, k=k, terms=terms)


def parse_laws(specs: object) -> dict:
    """Parse a list of declared laws into ``{id: Law}`` (duplicate id -> error)."""
    laws: dict = {}
    if specs is None:
        return laws
    if not isinstance(specs, (list, tuple)):
        raise ValueError("power laws must be a list")
    for s in specs:
        law = parse_law(s)
        if law.id in laws:
            raise ValueError(f"duplicate power-law id {law.id!r}")
        laws[law.id] = law
    return laws


def parse_bridge(spec: object, laws: Optional[dict] = None) -> Bridge:
    """Build a Bridge from a reading's declared dict. ``laws`` maps law id -> Law (the
    signal's declared laws). ``None``/absent -> the default ``same`` bridge (0 dB), so a
    document that declares no reading resolves exactly as before. Fail hard on an unknown
    law id or a bad kind."""
    if spec is None:
        return Bridge(kind=SAME)
    if not isinstance(spec, dict):
        raise ValueError("a reading bridge must be an object")
    kind = spec.get("kind", SAME)
    if kind not in _KINDS:
        raise ValueError(f"bridge 'kind' {kind!r} must be one of {_KINDS}")
    unit = str(spec.get("unit", ""))
    if kind == LAW:
        ref = spec.get("law")
        if isinstance(ref, dict):
            # Artifact / self-contained form: the full law is embedded.
            law = parse_law(ref)
        else:
            # Authored / doc form: a law id resolved against the signal's declared laws.
            lid = str(ref or "").strip()
            law = (laws or {}).get(lid)
            if law is None:
                raise ValueError(f"bridge references undeclared law {lid!r}")
        return Bridge(kind=LAW, law=law, unit=unit)
    try:
        k = float(spec.get("k", 0.0))
    except (TypeError, ValueError):
        raise ValueError("bridge 'k' must be numeric")
    return Bridge(kind=kind, k=k, unit=unit)
