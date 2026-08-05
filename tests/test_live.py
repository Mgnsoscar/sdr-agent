"""Tests for paramkit.live — the live-parameter control socket."""
import json
import socket
import threading
import time
import types

import pytest

from paramkit import Script


def _script() -> Script:
    return (
        Script("live demo")
        .number("--freq", unit="Hz", min=70e6, max=6e9, default=100e6,
                presets={"ISM 915 MHz": 915e6}, live=True)
        .integer("--gain", unit="dB", min=0, max=49, default=30, live=True)
        .number("--dur", unit="s", default=10.0)          # NOT live
    )


def _rpc(sock_path: str, req: dict, timeout: float = 3.0) -> dict:
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(timeout)
    c.connect(sock_path)
    f = c.makefile("rwb")
    f.write((json.dumps(req) + "\n").encode("utf-8"))
    f.flush()
    line = f.readline()
    c.close()
    return json.loads(line)


def _run_loop(ctrl, stop, quantise_gain_even=True):
    """A stand-in for a script's main loop: drain changes and report the value
    the 'device' took (gain quantised to even numbers to mimic hardware steps)."""
    def loop():
        while not stop.is_set():
            for ch in ctrl.drain():
                if ch.name == "gain" and quantise_gain_even:
                    ctrl.report("gain", (int(ch.value) // 2) * 2)
                else:
                    ctrl.report(ch.name, ch.value)
            time.sleep(0.003)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def test_set_get_roundtrip_with_quantisation(tmp_path):
    s = _script()
    args = types.SimpleNamespace(freq=100e6, gain=30, dur=10.0)
    sock = str(tmp_path / "ctl.sock")
    ctrl = s.live_control(args, socket_path=sock)
    stop = threading.Event()
    _run_loop(ctrl, stop)
    try:
        resp = _rpc(sock, {"op": "set", "values": {"freq": 101e6, "gain": 41}, "wait": 2.0})
        assert resp["ok"] is True
        assert resp["rejected"] == {}
        assert resp["applied"]["freq"] == 101e6
        assert resp["applied"]["gain"] == 40      # device quantised 41 → 40
        assert resp["pending"] == []

        got = _rpc(sock, {"op": "get"})
        assert got["ok"] is True
        assert got["current"]["gain"] == 41       # requested
        assert got["applied"]["gain"] == 40       # actually taken
        assert set(got["live"]) == {"freq", "gain"}
    finally:
        stop.set()
        ctrl.close()


def test_validation_rejects_out_of_range_and_unknown(tmp_path):
    s = _script()
    args = types.SimpleNamespace(freq=100e6, gain=30, dur=10.0)
    sock = str(tmp_path / "ctl.sock")
    ctrl = s.live_control(args, socket_path=sock)
    stop = threading.Event()
    _run_loop(ctrl, stop)
    try:
        resp = _rpc(sock, {"op": "set",
                           "values": {"gain": 999, "dur": 5, "nope": 1}, "wait": 0.3})
        assert resp["ok"] is False
        assert "gain" in resp["rejected"]         # above max
        assert "dur" in resp["rejected"]          # not a live parameter
        assert "nope" in resp["rejected"]         # unknown
        assert resp["accepted"] == {}
    finally:
        stop.set()
        ctrl.close()


def test_preset_key_accepted(tmp_path):
    s = _script()
    args = types.SimpleNamespace(freq=100e6, gain=30, dur=10.0)
    sock = str(tmp_path / "ctl.sock")
    ctrl = s.live_control(args, socket_path=sock)
    stop = threading.Event()
    _run_loop(ctrl, stop)
    try:
        resp = _rpc(sock, {"op": "set", "values": {"freq": "ISM 915 MHz"}, "wait": 2.0})
        assert resp["ok"] is True
        assert resp["applied"]["freq"] == 915e6
    finally:
        stop.set()
        ctrl.close()


def test_pending_when_loop_not_reporting(tmp_path):
    """If the script never drains/reports, set() returns the change as pending
    (still recorded) rather than blocking forever."""
    s = _script()
    args = types.SimpleNamespace(freq=100e6, gain=30, dur=10.0)
    sock = str(tmp_path / "ctl.sock")
    ctrl = s.live_control(args, socket_path=sock)
    try:
        t0 = time.monotonic()
        resp = _rpc(sock, {"op": "set", "values": {"gain": 20}, "wait": 0.4})
        assert time.monotonic() - t0 < 1.5          # bounded by wait, didn't hang
        assert resp["accepted"]["gain"] == 20
        assert "gain" in resp["pending"]
        # the change is still queued for the script to pick up
        drained = ctrl.drain()
        assert [c.name for c in drained] == ["gain"]
        assert drained[0].value == 20
    finally:
        ctrl.close()


def test_no_socket_is_inert(monkeypatch):
    monkeypatch.delenv("SDR_CTRL_SOCK", raising=False)
    s = _script()
    args = types.SimpleNamespace(freq=100e6, gain=30, dur=10.0)
    ctrl = s.live_control(args)          # no path, no env → no server
    assert ctrl.drain() == []
    ctrl.report("gain", 30)              # no error
    assert ctrl.value("gain") == 30
    assert ctrl.live_names == ["freq", "gain"] or set(ctrl.live_names) == {"freq", "gain"}
    ctrl.close()
