"""
Log-tail regression tests.

Two bugs made a task's output not show up in the Logs tab when you started
tailing before the task fired:

  * stream() opened current.log immediately; if it didn't exist yet (a task
    tailed before its first run, or a sequence step that fires later), the open
    raised and the stream ended silently — so nothing ever showed, even once the
    task fired.  It now waits for the file and follows it from the start.
  * one-shot ("run") steps wrote to a separate <task>/oneshot.log the tail never
    read.  They now append to the task's current.log, so a one-shot is visible in
    the Logs tab like any other output.
"""
import asyncio
from pathlib import Path

import pytest

from agent.log_manager import LogManager
from agent.models import TaskConfig
from agent.process_manager import ProcessManager


class _FakeWS:
    """Minimal stand-in for the log-stream WebSocket."""

    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(text)

    async def receive_text(self):
        await asyncio.sleep(3600)   # client stays connected, never sends


async def _stream_briefly(lm, ws, do, timeout=1.5):
    """Run lm.stream while `do()` pokes the log, then stop by timeout."""
    task = asyncio.create_task(do())
    try:
        await asyncio.wait_for(lm.stream(ws, lines=50), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    await task
    return "".join(ws.sent)


def test_stream_waits_for_a_log_created_after_tailing_starts(tmp_path):
    lm = LogManager(tmp_path, "later")
    assert not lm.current.exists()   # tailing before the task's first run

    async def fire_later():
        await asyncio.sleep(0.4)
        with lm.current.open("ab") as fh:
            fh.write(b"line one\nline two\n")

    got = asyncio.run(_stream_briefly(lm, _FakeWS(), fire_later))
    assert "line one" in got and "line two" in got   # nothing missed


def test_stream_existing_log_sends_backlog_then_only_new(tmp_path):
    lm = LogManager(tmp_path, "existing")
    lm.current.write_bytes(b"old backlog line\n")

    async def append_later():
        await asyncio.sleep(0.3)
        with lm.current.open("ab") as fh:
            fh.write(b"fresh line\n")

    got = asyncio.run(_stream_briefly(lm, _FakeWS(), append_later))
    assert "old backlog line" in got            # backlog sent once
    assert got.count("old backlog line") == 1   # not duplicated by the follow
    assert "fresh line" in got                  # new output followed


def test_oneshot_output_goes_to_the_tasks_current_log(tmp_path):
    cfg = TaskConfig(
        name="t",
        command=["python3", "-c",
                 "import sys; print('ONESHOT STDOUT'); print('ONESHOT STDERR', file=sys.stderr)"],
        working_dir=str(tmp_path),
    )
    mgr = ProcessManager({"t": cfg}, tmp_path, unit_id="u")

    async def go():
        await mgr.run_oneshot("t", [])
        for proc, _fh, _rid in list(mgr._oneshots.values()):
            await proc.wait()
        await asyncio.sleep(0.1)   # let the watcher close the file handle

    asyncio.run(go())

    current = mgr._get("t").log.current
    assert current.exists(), "one-shot must write the task's current.log"
    text = current.read_text()
    assert "ONESHOT STDOUT" in text        # print() visible in the Logs tab
    assert "ONESHOT STDERR" in text        # stderr/logging too
    assert "one-shot:" in text             # the run marker header
    assert not (tmp_path / "t" / "oneshot.log").exists()   # no separate hidden file
