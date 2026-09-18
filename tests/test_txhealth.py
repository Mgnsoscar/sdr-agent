"""
paramkit.txhealth.watch_flowgraph — the script-side done-watcher (RF-fault Phase 1, §5.1).

GNU Radio does not re-raise a halted flowgraph to Python; tb.wait() just RETURNS. So a daemon thread
blocks in tb.wait() and, when it returns with the script's stop flag still unset, prints a marker,
sets stop, and flags .faulted so main() returns non-zero (the agent's crash pipeline then sees it).
"""
import io
import threading

from paramkit.txhealth import watch_flowgraph, FAULT_MARKER
from agent import config as cfg


class _FakeTB:
    """A stand-in top block whose wait() blocks until the test releases it — the way a real flowgraph
    blocks until it halts (fault) or is stopped."""
    def __init__(self):
        self._released = threading.Event()

    def wait(self):
        self._released.wait(timeout=5)

    def release(self):
        self._released.set()


def test_silent_halt_becomes_a_fault():
    tb = _FakeTB()
    stop = threading.Event()
    out = io.StringIO()
    w = watch_flowgraph(tb, stop, reason="flowgraph halted", stream=out)
    tb.release()                      # the flowgraph halts on its own, nobody set stop
    w.join(timeout=5)
    assert w.faulted is True
    assert stop.is_set()              # the watcher breaks the main loop
    assert FAULT_MARKER in out.getvalue()
    assert 'reason="flowgraph halted"' in out.getvalue()


def test_intentional_stop_is_not_a_fault():
    tb = _FakeTB()
    stop = threading.Event()
    out = io.StringIO()
    w = watch_flowgraph(tb, stop, stream=out)
    stop.set()                        # the operator/SIGTERM asked to stop FIRST
    tb.release()                      # then the graph tears down
    w.join(timeout=5)
    assert w.faulted is False
    assert out.getvalue() == ""       # no marker on a clean stop


def test_marker_matches_the_agent_pattern_list():
    # The done-watcher's marker IS the agent's authoritative log-scan pattern (a contract).
    assert FAULT_MARKER in cfg.HEALTH_FAULT_PATTERNS
