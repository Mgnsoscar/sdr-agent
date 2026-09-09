"""RF output gate recognition + muting (paramkit/rf.py).

The gate is the on/off control that mutes the unit. It is recognised by the explicit ``is_rf``
marker, else the ``--rf`` on/off convention (so already-deployed scripts work). ``is_muted`` reads
the gate's effective value, falling back to its schema default.
"""
from paramkit import rf


def _p(dest, **kw):
    kw.setdefault("flags", [])
    return {"dest": dest, **kw}


def test_is_on_tokens():
    assert rf.is_on("on") and rf.is_on("1") and rf.is_on(True) and rf.is_on("YES")
    assert not rf.is_on("off") and not rf.is_on("0")
    assert not rf.is_on(None) and not rf.is_on(False)


def test_gate_prefers_the_explicit_marker():
    params = [_p("mode", choices=["a", "b"]),
              _p("rf", flags=["--rf"], choices=["on", "off"], is_rf=True)]
    assert rf.gate_dest(params) == "rf"


def test_gate_convention_recognises_an_unmarked_rf():
    # a script that predates the is_rf marker: --rf on/off is still recognised
    params = [_p("freq"), _p("rf", flags=["-RF", "--rf"], choices=["on", "off"])]
    g = rf.gate(params)
    assert g is not None and g.get("dest") == "rf" and not g.get("is_rf")


def test_gate_ignores_a_non_onoff_rf_like_control():
    params = [_p("rf_mode", flags=["--rf-mode"], choices=["a", "b"])]
    assert rf.gate(params) is None


def test_no_gate_when_absent():
    assert rf.gate([_p("freq"), _p("power")]) is None
    assert rf.is_muted([_p("freq")], {"freq": 1}) is False


def test_is_muted_reads_the_value_then_the_default():
    params = [_p("rf", flags=["--rf"], choices=["on", "off"], default="on", is_rf=True)]
    assert rf.is_muted(params, {"rf": "off"}) is True
    assert rf.is_muted(params, {"rf": "on"}) is False
    assert rf.is_muted(params, {}) is False            # no value → falls back to default 'on'
    off_default = [_p("rf", flags=["--rf"], choices=["on", "off"], default="off", is_rf=True)]
    assert rf.is_muted(off_default, {}) is True         # default 'off' ⇒ muted
