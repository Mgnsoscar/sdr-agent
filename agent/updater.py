"""
updater — stage, activate, health-check and roll back agent releases (OTA).

An update ships as a *bundle*: a .tar.gz containing a top-level ``VERSION`` file
and the agent payload (``agent/ scripts/ paramkit/ requirements.txt``) — the same
files ``install.sh`` copies. Applying one is deliberately non-destructive and
reversible:

    root/<version>/            an unpacked release (code only; state lives elsewhere)
    root/.markers/pending      the version awaiting a health confirmation
    root/.markers/previous     the version to roll back to
    root/.markers/<ver>.ok     a release that has proven itself healthy
    <link>                     the 'current' symlink → root/<version>, flipped atomically

Flow: ``stage`` unpacks + installs deps into a new release dir; ``activate`` flips
the symlink and records the previous version + a pending marker; the freshly
booted agent calls ``confirm_healthy`` after a grace period. If it never does (it
crashed or hung), an external confirm timer sees ``needs_rollback`` and calls
``rollback``. Keeping the previous release means a bad push can't strand a unit.

This module is pure/testable: all filesystem work is under a caller-supplied
``root``/``link``, and the two side effects (dependency install, service restart)
are injectable so tests run without pip or systemd.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

# Files/dirs a valid bundle must contain at its top level.
_REQUIRED = ("VERSION", "agent")


class UpdateError(Exception):
    """A bundle was malformed or an update step failed."""


@dataclass
class ReleaseInfo:
    version: str
    active: bool
    healthy: bool
    path: str


def _default_deps_install(release_dir: Path) -> None:
    """Install the release's Python deps system-wide, OFFLINE-FIRST.

    Try an install that never touches the network (``--no-index``): when the deps are
    already satisfied — the common case for an update — this finishes instantly. When
    they're not, it fails fast instead of pip retrying an unreachable PyPI for every
    package with time-outs and back-off (the "update with no internet takes forever").
    Only if the offline pass can't satisfy everything do we fall back to a normal
    online install, and even then with fast fail-out rather than a long hang. A
    bundled ``wheels/`` dir (if present) feeds the offline pass so a never-online Pi
    can still install."""
    req = release_dir / "requirements.txt"
    if not req.is_file():
        return
    base = ["pip3", "install", "--break-system-packages", "--root-user-action=ignore",
            "--disable-pip-version-check", "--no-input"]
    offline = base + ["--no-index"]
    wheels = release_dir / "wheels"
    if wheels.is_dir():
        offline += ["--find-links", str(wheels)]
    offline += ["-r", str(req)]
    if subprocess.run(offline).returncode == 0:
        return
    # Something is genuinely missing — go online, but fail fast rather than hang for
    # minutes if there's no route to the index.
    subprocess.run(
        base + ["--retries", "1", "--timeout", "15",
                "--upgrade", "--upgrade-strategy", "only-if-needed", "-r", str(req)],
        check=True,
    )


def _default_restart(service_name: str) -> None:
    """Restart the agent's service a moment from now, so the HTTP response that
    triggered the update flushes before this process is replaced."""
    subprocess.run(
        ["systemd-run", "--on-active=2", "systemctl", "restart", service_name],
        check=True,
    )


def _top_component(name: str) -> str:
    """The top-level path component of a tar member, tolerant of a leading './'
    (which `tar -C dir .` produces)."""
    name = name.lstrip("/")
    if name.startswith("./"):
        name = name[2:]
    return name.split("/", 1)[0]


def _safe_members(tar: tarfile.TarFile, dest: Path) -> list:
    """Reject path-traversal / absolute members before extracting an untrusted tar
    (older Pythons lack tarfile's data filter)."""
    dest = dest.resolve()
    out = []
    for m in tar.getmembers():
        target = (dest / m.name).resolve()
        if not str(target).startswith(str(dest) + os.sep) and target != dest:
            raise UpdateError(f"unsafe path in bundle: {m.name!r}")
        if m.issym() or m.islnk():
            raise UpdateError(f"links not allowed in bundle: {m.name!r}")
        out.append(m)
    return out


class Updater:
    def __init__(self, root: Path, link: Path, service_name: str = "sdr-agent",
                 deps_install: Optional[Callable[[Path], None]] = None,
                 restart: Optional[Callable[[str], None]] = None):
        self.root = Path(root)
        self.link = Path(link)
        self.service_name = service_name
        self._deps_install = deps_install or _default_deps_install
        self._restart = restart or _default_restart
        self.markers = self.root / ".markers"

    # ── Layout helpers ────────────────────────────────────────────────────────

    def release_dir(self, version: str) -> Path:
        return self.root / version

    def current_version(self) -> Optional[str]:
        """The version the `current` symlink points at, or None if unset/dangling."""
        try:
            target = self.link.resolve()
        except OSError:
            return None
        if target.parent == self.root.resolve() and target.exists():
            return target.name
        return None

    def list_releases(self) -> List[ReleaseInfo]:
        if not self.root.is_dir():
            return []
        cur = self.current_version()
        out = []
        for d in sorted(p for p in self.root.iterdir() if p.is_dir() and not p.name.startswith(".")):
            out.append(ReleaseInfo(version=d.name, active=(d.name == cur),
                                   healthy=self._ok_marker(d.name).exists(), path=str(d)))
        return out

    # ── Markers ───────────────────────────────────────────────────────────────

    def _ok_marker(self, version: str) -> Path:
        return self.markers / f"{version}.ok"

    def _pending_file(self) -> Path:
        return self.markers / "pending"

    def _previous_file(self) -> Path:
        return self.markers / "previous"

    def pending_version(self) -> Optional[str]:
        try:
            return self._pending_file().read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def previous_version(self) -> Optional[str]:
        try:
            return self._previous_file().read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def is_confirmed(self, version: str) -> bool:
        """True if `version` has been marked healthy (its .ok marker exists)."""
        return bool(version) and self._ok_marker(version).exists()

    # ── Stage ─────────────────────────────────────────────────────────────────

    def stage(self, bundle_path: Path) -> str:
        """Unpack a bundle into root/<version> and install its deps. Returns the
        version. Idempotent-ish: a half-written release is replaced."""
        with tarfile.open(bundle_path, "r:gz") as tar:
            members = _safe_members(tar, self.root)
            names = {_top_component(m.name) for m in members}
            missing = [r for r in _REQUIRED if r not in names]
            if missing:
                raise UpdateError(f"bundle missing {missing}")
            tmp = Path(tempfile.mkdtemp(prefix=".stage-", dir=self.root))
            try:
                tar.extractall(tmp, members=members)
                version = (tmp / "VERSION").read_text(encoding="utf-8").strip()
                if not version:
                    raise UpdateError("bundle VERSION is empty")
                dest = self.release_dir(version)
                if dest.exists():
                    shutil.rmtree(dest)
                os.replace(tmp, dest)
            except Exception:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
        try:
            self._deps_install(self.release_dir(version))
        except Exception as exc:   # deps failed → don't leave a half-usable release
            shutil.rmtree(self.release_dir(version), ignore_errors=True)
            raise UpdateError(f"dependency install failed: {exc}") from exc
        logger.info("Staged release %s at %s", version, self.release_dir(version))
        return version

    # ── Activate / confirm / rollback ─────────────────────────────────────────

    def activate(self, version: str, mark_pending: bool = True) -> str:
        """Flip the `current` symlink to `version` atomically, recording the prior
        version and (by default) a pending marker for the health check. Returns the
        version replaced (or '')."""
        dest = self.release_dir(version)
        if not dest.is_dir():
            raise UpdateError(f"release {version} is not staged")
        prev = self.current_version() or ""
        self.markers.mkdir(parents=True, exist_ok=True)
        # Atomic symlink swap: create a temp link then rename over the old one.
        tmp_link = self.link.with_name(self.link.name + ".new")
        if tmp_link.is_symlink() or tmp_link.exists():
            tmp_link.unlink()
        tmp_link.symlink_to(dest)
        os.replace(tmp_link, self.link)
        if prev:
            self._previous_file().write_text(prev, encoding="utf-8")
        if mark_pending:
            self._pending_file().write_text(version, encoding="utf-8")
        logger.info("Activated release %s (was %s)", version, prev or "none")
        return prev

    def confirm_healthy(self, version: str) -> None:
        """Mark a release healthy and clear its pending flag — called by the newly
        booted agent once it's been serving cleanly for the grace period."""
        self.markers.mkdir(parents=True, exist_ok=True)
        self._ok_marker(version).write_text(str(time.time()), encoding="utf-8")
        if self.pending_version() == version:
            self._pending_file().unlink(missing_ok=True)
        logger.info("Release %s confirmed healthy", version)

    def needs_rollback(self, grace_s: float) -> Optional[str]:
        """The pending version if it has been unconfirmed for longer than grace_s
        (its boot hung or crashed before confirming) — else None."""
        pending = self.pending_version()
        if not pending or self._ok_marker(pending).exists():
            return None
        try:
            age = time.time() - self._pending_file().stat().st_mtime
        except OSError:
            return None
        return pending if age >= grace_s else None

    def rollback(self, restart: bool = True) -> Optional[str]:
        """Revert to the previous release and restart. Returns the version rolled
        back to, or None if there's nothing to revert to."""
        prev = self.previous_version()
        if not prev or not self.release_dir(prev).is_dir():
            logger.error("Rollback requested but no usable previous release")
            return None
        self.activate(prev, mark_pending=False)
        self._pending_file().unlink(missing_ok=True)
        logger.warning("Rolled back to release %s", prev)
        if restart:
            self._restart(self.service_name)
        return prev

    def prune(self, keep: int = 3) -> List[str]:
        """Delete old releases, keeping the newest `keep` plus the active and
        previous ones. Returns the versions removed."""
        cur, prev = self.current_version(), self.previous_version()
        releases = [r.version for r in self.list_releases()]
        protected = {v for v in (cur, prev) if v}
        removable = [v for v in releases if v not in protected]
        # keep the most recent `keep` (by name sort — versions sort lexically here)
        to_remove = removable[:-keep] if keep > 0 else removable
        removed = []
        for v in to_remove:
            shutil.rmtree(self.release_dir(v), ignore_errors=True)
            self._ok_marker(v).unlink(missing_ok=True)
            removed.append(v)
        if removed:
            logger.info("Pruned releases: %s", ", ".join(removed))
        return removed

    # ── High-level: apply a bundle and restart ────────────────────────────────

    def apply(self, bundle_path: Path) -> str:
        """Stage, activate, and schedule a restart. Returns the new version. The
        caller should reply to the client BEFORE the restart lands."""
        version = self.stage(Path(bundle_path))
        self.activate(version)
        self._restart(self.service_name)
        return version
