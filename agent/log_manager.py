"""
Per-process log management.

Each task gets its own log directory:
  /opt/sdr-agent/logs/<task_name>/
      current.log          <- active log (stdout+stderr merged)
      run_<timestamp>.log  <- archived on each new start

Tailing is done by reading the file from a byte offset, making it
safe to stream over WebSockets without blocking the event loop.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path


class LogManager:
    def __init__(self, log_root: Path, task_name: str):
        self.task_dir   = log_root / task_name
        self.task_dir.mkdir(parents=True, exist_ok=True)
        self.current    = self.task_dir / "current.log"

    def rotate(self) -> Path:
        """Archive current.log to a timestamped file, return the new current path."""
        if self.current.exists() and self.current.stat().st_size > 0:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            archive = self.task_dir / f"run_{ts}.log"
            self.current.rename(archive)
        # Touch a fresh file
        self.current.touch()
        return self.current

    def cleanup(self, keep_runs: int = 10, max_age_days: float = 7.0) -> int:
        """
        Delete old archived run logs for this task.

        Keeps at most `keep_runs` most-recent archives, and additionally
        removes any archive older than `max_age_days` regardless of count.
        current.log is never touched. Returns the number of files deleted.
        """
        import time
        archives = sorted(
            self.task_dir.glob("run_*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,   # newest first
        )

        deleted = 0
        cutoff = time.time() - (max_age_days * 86400)

        for idx, path in enumerate(archives):
            too_many = idx >= keep_runs
            too_old  = path.stat().st_mtime < cutoff
            if too_many or too_old:
                try:
                    path.unlink()
                    deleted += 1
                except OSError:
                    pass

        return deleted

    def open_for_write(self):
        """Return a writable file object for the current log (append mode)."""
        return self.current.open("ab")   # binary to handle raw subprocess output

    async def tail(self, lines: int = 100) -> list[str]:
        """Return the last N lines from current.log (non-blocking)."""
        if not self.current.exists():
            return []
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._read_tail, lines)

    def _read_tail(self, lines: int) -> list[str]:
        """Synchronous tail implementation (runs in a thread pool)."""
        try:
            with self.current.open("rb") as fh:
                # Efficient tail: seek from end
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                block = min(size, 1024 * lines)   # rough heuristic
                fh.seek(max(0, size - block))
                content = fh.read().decode("utf-8", errors="replace")
                all_lines = content.splitlines()
                return all_lines[-lines:]
        except OSError:
            return []

    async def stream(self, websocket, lines: int = 50):
        """
        Stream live log output to a WebSocket connection.

        Sends the recent backlog first, then follows current.log in real time.

        Three robustness fixes vs. a naive follow loop:
          1. Waits for the log to appear. A task tailed before its first run (or a
             sequence step that fires later) hasn't created current.log yet. A
             naive open() would raise and end the stream, so nothing would ever
             show even once the task fires. We poll until the file exists and then
             follow it, so output appears the moment the task starts.
          2. Survives log rotation. When a task restarts, rotate() renames
             current.log to an archive and creates a fresh current.log. A handle
             opened once would keep following the *renamed* (now archived) inode
             and silently go quiet. We detect the inode change and reopen so the
             tail keeps following the new run's output.
          3. Detects a client that goes away even while the log is idle. The
             client only ever reads, so we run a concurrent receive that resolves
             when it disconnects — otherwise a gone client is noticed only on the
             next write, which never happens for an idle task, leaking the
             coroutine and socket.
        """
        # Whether the file exists now decides where we start following (see below).
        existed_at_start = self.current.exists()

        # Backlog first
        history = await self.tail(lines)
        for line in history:
            await websocket.send_text(line + "\n")

        # The client never sends data frames; this future resolves when it closes,
        # giving prompt disconnect detection even with no new log output.
        async def _await_close() -> None:
            try:
                while True:
                    await websocket.receive_text()
            except Exception:
                return

        closed = asyncio.ensure_future(_await_close())

        def _open_current():
            fh = self.current.open("rb")
            return fh, os.fstat(fh.fileno()).st_ino

        try:
            # The log may not exist yet — wait for it instead of ending the stream.
            fh = None
            while not closed.done():
                try:
                    fh, cur_inode = _open_current()
                    break
                except OSError:
                    await asyncio.sleep(0.2)
            if fh is None:
                return   # client disconnected before the log appeared

            if existed_at_start:
                fh.seek(0, os.SEEK_END)   # existing file: only new output (backlog already sent)
            # else: the file was created after we started tailing (the run we were
            # waiting for) — follow from the start so its first output isn't missed.
            try:
                while not closed.done():
                    chunk = fh.read(4096)
                    if chunk:
                        await websocket.send_text(
                            chunk.decode("utf-8", errors="replace")
                        )
                        continue

                    # No new data — has current.log been rotated out from under us?
                    try:
                        rotated = (
                            not self.current.exists()
                            or self.current.stat().st_ino != cur_inode
                        )
                    except OSError:
                        rotated = False

                    if rotated:
                        # Open the fresh file BEFORE closing the old one, so a
                        # failed reopen (mid-rotate) leaves us on a valid handle.
                        try:
                            new_fh, new_inode = _open_current()
                        except OSError:
                            await asyncio.sleep(0.1)
                            continue
                        try:
                            fh.close()
                        except OSError:
                            pass
                        # Follow the new run from its start (do NOT seek to end).
                        fh, cur_inode = new_fh, new_inode
                        continue

                    await asyncio.sleep(0.1)
            finally:
                try:
                    fh.close()
                except OSError:
                    pass
        except asyncio.CancelledError:
            pass   # Client disconnected / server shutting down — normal exit
        except Exception:
            pass
        finally:
            closed.cancel()