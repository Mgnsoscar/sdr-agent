"""
Flowgraph health self-report — the script side of RF-fault DETECTION (Phase 1).

The field incident: a transmit script hit a GNU Radio buffer (`vmcircbuf`) error at startup but did
NOT exit. GNU Radio does **not** re-raise a halted flowgraph to Python — the C++ scheduler logs the
error and HALTS, and ``tb.wait()`` RETURNS NORMALLY. So the surrounding Python keeps looping, the
process stays alive, and the agent (whose liveness is exit-code-only) shows the task *running* while
the SDR emits nothing. The silent return of ``tb.wait()`` with nobody having asked to stop **is the
only signal available**, and this helper is what turns it into something visible.

:func:`watch_flowgraph` starts one daemon thread that blocks in ``tb.wait()``. When it returns:
  * if the script already set its ``stop`` flag → an intentional stop (SIGTERM/SIGINT or the loop's
    own exit); do nothing.
  * otherwise → the flowgraph halted on its own = the fault: print a stable marker line (for the
    agent's log-scan watchdog), set ``stop`` so the main loop breaks, and flag ``.faulted`` so
    ``main()`` returns NON-ZERO — which the agent's existing exit-driven crash pipeline
    (RUNNING → CRASHED → CrashEvent) already handles.

Boundary: this catches a HALT where ``tb.wait()`` returns. A *true wedge* — a scheduler thread stuck
in a blocking UHD/USB call so ``tb.wait()`` never returns — is the agent watchdog's job (a log-scan
for the same marker / a GR error), not this helper's.

Pure stdlib + threading, shared like ``paramkit/txstage.py``: it lives in ``sdr-agent/paramkit`` and
the transmit scripts import it via ``PYTHONPATH`` (``from paramkit.txhealth import watch_flowgraph``).
"""
from __future__ import annotations

import sys
import threading

# The exact literal a faulted flowgraph prints, and the agent's log-scan keys on. It is a CONTRACT
# between this helper (which prints it) and the agent's HEALTH_FAULT_PATTERNS (which matches it) —
# keep them in step (a test pins the agreement). flush=True because tasks run with GR/UHD console
# logging suppressed and a buffered marker could be swallowed before the crash tail reads it.
FAULT_MARKER = "HEALTH state=faulted"


class Watcher:
    """Handle returned by :func:`watch_flowgraph`. ``.faulted`` is set True iff the flowgraph halted
    on its own (a fault), so ``main()`` can ``return 1 if health.faulted else 0``."""

    __slots__ = ("faulted", "_thread")

    def __init__(self) -> None:
        self.faulted = False
        self._thread: threading.Thread | None = None

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)


def watch_flowgraph(tb, stop, *, reason: str = "flowgraph halted", stream=None) -> Watcher:
    """Detect a silent GNU Radio flowgraph halt and convert it into a non-zero ``main()`` exit.

    Call it on the line right AFTER ``tb.start()`` (``tb.wait()`` on a not-yet-started top block
    returns immediately, so an earlier call would report a false halt). ``stop`` is any object with
    ``.is_set()``/``.set()`` — a ``threading.Event`` (what every RPi transmit script already uses for
    its SIGTERM/SIGINT stop flag). Never raises; the watcher thread is a daemon so a true wedge can
    never block interpreter exit.
    """
    out = stream if stream is not None else sys.stdout
    w = Watcher()

    def _run() -> None:
        try:
            tb.wait()
        except Exception:      # noqa: BLE001 — a wait() that raises is itself an abnormal end
            pass
        try:
            if stop.is_set():
                return         # intentional stop → not a fault
        except Exception:      # noqa: BLE001 — a broken stop flag: treat the halt as a fault
            pass
        w.faulted = True
        try:
            print(f'{FAULT_MARKER} reason="{reason}"', file=out, flush=True)
        except Exception:      # noqa: BLE001 — never let reporting break the exit
            pass
        try:
            stop.set()         # break the main loop so main() returns promptly and exits non-zero
        except Exception:      # noqa: BLE001
            pass

    t = threading.Thread(target=_run, name="txhealth", daemon=True)
    w._thread = t
    t.start()
    return w
