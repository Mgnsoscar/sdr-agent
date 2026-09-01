"""The shared power-quantity bridge/law math (paramkit/power_law.py): parsing, the
affine-in-log delta, unit families, and fail-safe rejection of malformed declarations.
Mirrored verbatim in the client (state/power_law.py)."""
import math

import pytest

from paramkit.power_law import (
    ABS, DENSITY, SAME, LAW, OWN,
    Bridge, Law, LawTerm, parse_law, parse_laws, parse_bridge,
)


# ── laws ──────────────────────────────────────────────────────────────────────

def test_constant_law_is_pure_k():
    law = parse_law({"id": "peak2total", "name": "peak->total", "in": ABS, "out": ABS,
                     "k": 3.0})
    assert law.params() == []
    assert law.delta_db({}) == 3.0
    assert law.in_fam == ABS and law.out_fam == ABS


def test_single_term_convenience_shape():
    # density (dBm/Hz) -> total power (dBm): + 10*log10(bw / 1 Hz)
    law = parse_law({"name": "Full-bandwidth power", "in": "density", "out": "abs",
                     "param": "bw", "coeff": 10.0, "ref": 1.0})
    assert law.id == "Full-bandwidth power"
    assert law.params() == ["bw"]
    assert law.delta_db({"bw": 1e7}) == pytest.approx(70.0)     # 10*log10(1e7)
    assert law.in_fam == DENSITY and law.out_fam == ABS


def test_comb_per_tooth_negative_coeff():
    # per-tooth = total - 10*log10(N)
    law = parse_law({"id": "tooth", "name": "per-tooth", "param": "ntones",
                     "coeff": -10.0, "ref": 1.0})
    assert law.delta_db({"ntones": 100}) == pytest.approx(-20.0)


def test_multi_term_law():
    law = parse_law({"id": "two", "name": "two", "terms": [
        {"param": "bw", "coeff": 10.0, "ref": 1.0},
        {"param": "n", "coeff": -10.0, "ref": 1.0}]})
    assert sorted(law.params()) == ["bw", "n"]
    assert law.delta_db({"bw": 1e6, "n": 10}) == pytest.approx(60.0 - 10.0)


def test_rep_delta_defaults_to_ref_giving_k():
    law = parse_law({"id": "x", "name": "x", "param": "bw", "coeff": 10.0, "ref": 1.0,
                     "k": 2.0})
    assert law.rep_delta_db() == pytest.approx(2.0)      # rep defaults to ref → log term 0


def test_rep_delta_uses_representative_value():
    law = parse_law({"id": "fbw", "name": "fbw", "param": "bw", "coeff": 10.0, "ref": 1.0,
                     "rep": 1e7})
    assert law.rep_delta_db() == pytest.approx(70.0)     # bounds shown at a typical 10 MHz
    # runtime still evaluates at the live value
    assert law.delta_db({"bw": 1e6}) == pytest.approx(60.0)


def test_rep_survives_public_dict_roundtrip():
    law = parse_law({"id": "fbw", "name": "fbw", "param": "bw", "coeff": 10.0, "rep": 1e7})
    law2 = parse_law(law.to_public_dict())
    assert law2.rep_delta_db() == pytest.approx(law.rep_delta_db())


def test_law_missing_param_raises():
    law = parse_law({"id": "x", "name": "x", "param": "bw", "coeff": 10.0})
    with pytest.raises(ValueError):
        law.delta_db({})               # no 'bw' supplied → fail safe


def test_law_nonpositive_param_raises():
    law = parse_law({"id": "x", "name": "x", "param": "bw", "coeff": 10.0})
    with pytest.raises(ValueError):
        law.delta_db({"bw": 0.0})      # log10 of 0


def test_bad_family_rejected():
    with pytest.raises(ValueError):
        parse_law({"id": "x", "name": "x", "in": "watts", "out": "abs"})


def test_law_needs_id_or_name():
    with pytest.raises(ValueError):
        parse_law({"k": 1.0})


def test_parse_laws_duplicate_id():
    with pytest.raises(ValueError):
        parse_laws([{"id": "a", "name": "a"}, {"id": "a", "name": "a2"}])


def test_parse_laws_none_is_empty():
    assert parse_laws(None) == {}


# ── bridges ─────────────────────────────────────────────────────────────────────

def test_absent_bridge_defaults_to_same_zero():
    b = parse_bridge(None)
    assert b.kind == SAME and b.is_same
    assert b.delta_db() == 0.0
    assert b.keyed_params() == []
    assert b.is_constant


def test_same_with_denominator_offset():
    # dBm/Hz measurement reported in dBm/MHz: +60 dB, a pure constant
    b = parse_bridge({"kind": SAME, "k": 60.0, "unit": "dBm/MHz"})
    assert b.delta_db() == 60.0
    assert b.unit == "dBm/MHz"
    assert b.is_constant


def test_own_bridge_delta_zero():
    b = parse_bridge({"kind": OWN})
    assert b.is_own
    assert b.delta_db() == 0.0
    assert b.is_constant


def test_law_bridge_resolves_from_declared():
    laws = parse_laws([{"id": "fbw", "name": "Full-bandwidth power", "in": "density",
                        "out": "abs", "param": "bw", "coeff": 10.0, "ref": 1.0}])
    b = parse_bridge({"kind": LAW, "law": "fbw"}, laws)
    assert b.is_law and not b.is_constant
    assert b.keyed_params() == ["bw"]
    assert b.delta_db({"bw": 1e6}) == pytest.approx(60.0)


def test_law_bridge_unknown_id_rejected():
    with pytest.raises(ValueError):
        parse_bridge({"kind": LAW, "law": "nope"}, {})


def test_bad_kind_rejected():
    with pytest.raises(ValueError):
        parse_bridge({"kind": "sideways"})


def test_public_dict_roundtrips_law():
    law = parse_law({"id": "fbw", "name": "Full-bandwidth power", "in": "density",
                     "out": "abs", "param": "bw", "coeff": 10.0, "ref": 1.0})
    d = law.to_public_dict()
    law2 = parse_law(d)
    assert law2.params() == law.params()
    assert law2.delta_db({"bw": 1e6}) == pytest.approx(law.delta_db({"bw": 1e6}))
    assert law2.in_fam == DENSITY and law2.out_fam == ABS


def test_public_dict_roundtrips_bridge():
    laws = parse_laws([{"id": "fbw", "name": "fbw", "in": "density", "out": "abs",
                        "param": "bw", "coeff": 10.0, "ref": 1.0}])
    b = parse_bridge({"kind": LAW, "law": "fbw", "unit": "dBm"}, laws)
    d = b.to_public_dict()
    laws2 = {d["law"]["id"]: parse_law(d["law"])}
    b2 = parse_bridge(d, laws2)
    assert b2.delta_db({"bw": 1e6}) == pytest.approx(b.delta_db({"bw": 1e6}))
    assert b2.unit == "dBm"
