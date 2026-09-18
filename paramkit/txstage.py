"""
Shared /dev/shm staging hygiene — the contract between the transmit scripts and the agent.

Some transmit scripts stage a large IQ loop-file (or a FIFO) into ``/dev/shm`` for the run.
On a clean exit ``atexit`` removes it; but on ``SIGKILL`` / a C++ abort / a wedged
``tb.wait()`` that the agent then ``SIGKILL``s (exactly the RF-fault path), ``atexit`` never
runs and the file is orphaned — cumulative ``/dev/shm`` pressure across a unit's uptime that
raises the odds of a GNU Radio ``vmcircbuf`` buffer allocation failing on the next launch
(see ``sdr-agent/docs/rf-fault-recovery.md`` §3.5).

The fix has two halves that meet here:

  * the SCRIPT stages under a TAGGED, PID-bearing directory name via :func:`staging_dir`
    (``/dev/shm/sdrtx-<pid>-<signal>-<rand>``), and
  * the AGENT sweeps only those tagged names whose owning PID is DEAD via
    :func:`sweep_orphans` — at boot, before each launch, and after each task ends.

The tag is what makes the sweep SAFE: it removes only *our* staged dirs left by a *dead*
process, never a live sibling task's buffers, never GNU Radio's own ``vmcircbuf_*`` objects,
never a foreign owner's shared memory. The agent sweep is strictly more robust than a
per-script signal handler because it also catches ``SIGKILL`` / crash (no handler runs then).

Pure stdlib so both the agent AND the scripts (which import ``paramkit`` off the agent) share
one prefix constant — the single source of truth for the sweep contract.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile

# The fixed sentinel every staged dir/file name starts with. The sweep keys on it, so it is a
# CONTRACT: change it here and both the staging (scripts) and the sweep (agent) move together.
SHM_PREFIX = "sdrtx-"

# The tmpfs staging root. /dev/shm is RAM-backed (fast, no SD-card wear); absent (a dev box, an
# odd image) we fall back to the system temp dir so a script still runs — the file just isn't in
# tmpfs and the agent's /dev/shm sweep won't see it (atexit still cleans a graceful exit).
_SHM_ROOT = "/dev/shm"


def _shm_base() -> str | None:
    return _SHM_ROOT if os.path.isdir(_SHM_ROOT) else None


def _sanitize(tag) -> str:
    """A short, name-safe signal tag. Only informational (the sweep parses the PID, not the tag);
    keep it readable and free of the '-' we split the PID on being confused for structure."""
    s = "".join(c if c.isalnum() else "_" for c in str(tag))
    return s[:24] or "tx"


def staging_dir(signal, *, base: str | None = None) -> str:
    """Create and return a per-run staging directory under ``/dev/shm`` (or ``base``), named
    ``sdrtx-<pid>-<signal>-<rand>`` so the agent can identify and sweep OUR orphans safely.

    Registers an ``atexit`` cleanup (the graceful-exit backstop, matching what the scripts did
    before). A hard kill leaves the tagged dir for the agent's :func:`sweep_orphans` to reclaim.
    Drop-in for ``tempfile.mkdtemp(prefix=..., dir=shm) + atexit.register(rmtree)``.
    """
    root = base if base is not None else _shm_base()
    prefix = f"{SHM_PREFIX}{os.getpid()}-{_sanitize(signal)}-"
    path = tempfile.mkdtemp(prefix=prefix, dir=root)
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID exists. A foreign-owner PID (EPERM) counts as ALIVE, so
    the sweep never deletes a dir whose PID number is currently in use by anyone — the safe side."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # exists, owned by another user
    except OSError:
        return True          # unknown → assume alive, never delete
    return True


def sweep_orphans(base: str | None = None) -> int:
    """Remove ``sdrtx-<pid>-…`` staging entries under ``/dev/shm`` (or ``base``) whose owning PID
    is DEAD. Returns the count removed. Never touches a live-PID entry, a non-tagged object, or a
    name whose PID token is unparseable — so it can only ever reclaim our own leaked staging.
    Best-effort: any per-entry error is skipped, never raised."""
    root = base if base is not None else _shm_base()
    if not root or not os.path.isdir(root):
        return 0
    try:
        names = os.listdir(root)
    except OSError:
        return 0

    removed = 0
    for name in names:
        if not name.startswith(SHM_PREFIX):
            continue
        pid_token = name[len(SHM_PREFIX):].split("-", 1)[0]
        try:
            pid = int(pid_token)
        except ValueError:
            continue                       # not our <pid>-tagged shape → leave it
        if _pid_alive(pid):
            continue                       # a live task (or a reused PID) → never touch
        path = os.path.join(root, name)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.unlink(path)
            removed += 1
        except OSError:
            pass                           # vanished / racing another sweep → fine
    return removed
