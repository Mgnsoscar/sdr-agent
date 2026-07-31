"""
Per-run sequence logging.

A sequence run spreads output across several tasks at different times, with no
single place to see what the whole run did. RunLog writes ONE file per run that
interleaves:

  * the run's annotated choreography — armed, ON AIR, each step fired with its
    exact command, OFF AIR, and the outcome (timestamped); and
  * each step's program output, collected from the task's own current.log (which
    already captures it) and prefixed with the task name so concurrent tasks stay
    legible.

Collecting from the tasks' existing logs means no change to how tasks launch —
the run log is assembled entirely by reading files the agent already writes.

The file is managed by a LogManager rooted under logs/_sequences/<seq_id>/, so it
tails over the same WebSocket machinery the Logs tab already uses (backlog +
follow, survives rotation, waits for the file to appear).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Dict, Optional

from .log_manager import LogManager


def _clock() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


class RunLog:
    """Writes one sequence run's timeline + interleaved task output to a log."""

    def __init__(self, lm: LogManager,
                 get_task_lm: Callable[[str], Optional[LogManager]]):
        self._lm = lm
        self._get_task_lm = get_task_lm
        self._fh = None
        # name -> {"inode": int|None, "offset": int, "buf": bytes}
        self._tasks: Dict[str, dict] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def open(self, title: str) -> None:
        """Start a fresh log for this run (previous run kept as an archive)."""
        self._lm.rotate()
        self._lm.cleanup()
        self._fh = self._lm.open_for_write()
        self._write(f"===== {title} =====\n")

    def annotate(self, text: str) -> None:
        self._write(f"[{_clock()}] {text}\n")

    def close(self, footer: str = "") -> None:
        if self._fh is None:
            return
        self.collect()                          # flush any pending task output
        for name, st in self._tasks.items():    # and any trailing partial line
            if st["buf"]:
                self._emit_line(name, st["buf"])
                st["buf"] = b""
        if footer:
            self.annotate(footer)
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None

    # ── Task output collection ────────────────────────────────────────────────

    def watch_task(self, name: str) -> None:
        """Begin collecting a task's output from its current end — called when a
        step for that task fires, so only that run's output is included."""
        if name in self._tasks:
            return
        inode, offset = None, 0
        lm = self._get_task_lm(name)
        if lm is not None:
            try:
                st = lm.current.stat()
                inode, offset = st.st_ino, st.st_size   # only NEW output from here on
            except OSError:
                pass
        self._tasks[name] = {"inode": inode, "offset": offset, "buf": b""}

    def collect(self) -> None:
        """Read any new output from watched tasks and interleave it. Call each tick."""
        if self._fh is None:
            return
        for name, st in self._tasks.items():
            lm = self._get_task_lm(name)
            if lm is None:
                continue
            cur = lm.current
            try:
                stat = cur.stat()
            except OSError:
                continue
            # A rotation (new inode) or truncation (size shrank) → follow the new
            # file from its start so nothing is skipped.
            if stat.st_ino != st["inode"] or stat.st_size < st["offset"]:
                st["inode"], st["offset"], st["buf"] = stat.st_ino, 0, b""
            if stat.st_size <= st["offset"]:
                continue
            try:
                with cur.open("rb") as fh:
                    fh.seek(st["offset"])
                    data = fh.read()
            except OSError:
                continue
            st["offset"] += len(data)
            st["buf"] += data
            *complete, st["buf"] = st["buf"].split(b"\n")
            for line in complete:
                self._emit_line(name, line)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _emit_line(self, name: str, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace")
        # The task log carries a one-shot marker header ("--- <ts> one-shot: … ---");
        # the run log already annotates the run (⚡ run …), so drop it as noise here.
        s = text.strip()
        if s.startswith("--- ") and "one-shot:" in s and s.endswith("---"):
            return
        self._write(f"  {name}: " + text + "\n")

    def _write(self, s: str) -> None:
        if self._fh is None:
            return
        try:
            self._fh.write(s.encode("utf-8"))
            self._fh.flush()
        except OSError:
            pass
