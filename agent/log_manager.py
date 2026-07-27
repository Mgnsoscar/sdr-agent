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
        Sends historical lines first, then follows the file in real time.
        """
        # Send backlog
        history = await self.tail(lines)
        for line in history:
            await websocket.send_text(line + "\n")

        # Follow from current position
        try:
            with self.current.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                while True:
                    chunk = fh.read(4096)
                    if chunk:
                        await websocket.send_text(
                            chunk.decode("utf-8", errors="replace")
                        )
                    else:
                        await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass   # Client disconnected — normal exit
        except Exception:
            pass