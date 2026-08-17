"""Updater: staging, atomic activate, health-check rollback, prune — all under a
tmp root with the two side effects (deps install, restart) injected."""
import io
import os
import tarfile
import time
from pathlib import Path

import pytest

import agent.updater as updater_mod
from agent.updater import Updater, UpdateError, _default_deps_install


def _bundle(tmp: Path, version: str, *, extra=None, unsafe=False) -> Path:
    """Write a minimal valid bundle .tar.gz and return its path."""
    path = tmp / f"bundle-{version}.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        def add(name, data=b""):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        add("VERSION", version.encode())
        add("agent/__init__.py", b"# agent\n")
        add("requirements.txt", b"")
        if unsafe:
            add("../evil.py", b"pwned")
        for name, data in (extra or {}).items():
            add(name, data)
    return path


def _updater(tmp: Path, calls: dict) -> Updater:
    root = tmp / "releases"
    root.mkdir()
    link = tmp / "current"
    return Updater(
        root, link, service_name="sdr-agent",
        deps_install=lambda rel: calls.setdefault("deps", []).append(rel.name),
        restart=lambda svc: calls.setdefault("restart", []).append(svc),
    )


def test_stage_creates_release_and_installs_deps(tmp_path):
    calls = {}
    up = _updater(tmp_path, calls)
    v = up.stage(_bundle(tmp_path, "1.1.0"))
    assert v == "1.1.0"
    assert up.release_dir("1.1.0").is_dir()
    assert (up.release_dir("1.1.0") / "agent" / "__init__.py").is_file()
    assert calls["deps"] == ["1.1.0"]


def test_stage_rejects_malformed_and_unsafe_bundles(tmp_path):
    up = _updater(tmp_path, {})
    # missing agent/
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        info = tarfile.TarInfo("VERSION"); info.size = 3
        tar.addfile(info, io.BytesIO(b"9.9"))
    with pytest.raises(UpdateError):
        up.stage(bad)
    # path traversal
    with pytest.raises(UpdateError):
        up.stage(_bundle(tmp_path, "2.0.0", unsafe=True))


def test_activate_flips_symlink_and_records_previous(tmp_path):
    calls = {}
    up = _updater(tmp_path, calls)
    up.stage(_bundle(tmp_path, "1.0.0"))
    up.stage(_bundle(tmp_path, "1.1.0"))

    assert up.activate("1.0.0") == ""            # nothing was active before
    assert up.current_version() == "1.0.0"
    assert up.activate("1.1.0") == "1.0.0"       # replaced 1.0.0
    assert up.current_version() == "1.1.0"
    assert up.previous_version() == "1.0.0"
    assert up.pending_version() == "1.1.0"       # awaiting health confirmation


def test_confirm_healthy_clears_pending(tmp_path):
    up = _updater(tmp_path, {})
    up.stage(_bundle(tmp_path, "1.0.0")); up.activate("1.0.0")
    assert up.needs_rollback(grace_s=0) == "1.0.0"   # unconfirmed → would roll back
    up.confirm_healthy("1.0.0")
    assert up.pending_version() is None
    assert up.needs_rollback(grace_s=0) is None       # confirmed → safe
    assert up.list_releases()[0].healthy is True


def test_needs_rollback_after_grace(tmp_path):
    up = _updater(tmp_path, {})
    up.stage(_bundle(tmp_path, "1.0.0")); up.activate("1.0.0")
    up.stage(_bundle(tmp_path, "1.1.0")); up.activate("1.1.0")   # pending 1.1.0
    # fresh pending → not yet
    assert up.needs_rollback(grace_s=1000) is None
    # age the pending marker into the past
    old = time.time() - 500
    os.utime(up._pending_file(), (old, old))
    assert up.needs_rollback(grace_s=90) == "1.1.0"
    # confirming clears it
    up.confirm_healthy("1.1.0")
    assert up.needs_rollback(grace_s=90) is None


def test_rollback_reverts_and_restarts(tmp_path):
    calls = {}
    up = _updater(tmp_path, calls)
    up.stage(_bundle(tmp_path, "1.0.0")); up.activate("1.0.0"); up.confirm_healthy("1.0.0")
    up.stage(_bundle(tmp_path, "1.1.0")); up.activate("1.1.0")   # bad release
    assert up.rollback() == "1.0.0"
    assert up.current_version() == "1.0.0"
    assert up.pending_version() is None
    assert calls["restart"] == ["sdr-agent"]


def test_rollback_without_previous_is_noop(tmp_path):
    up = _updater(tmp_path, {})
    up.stage(_bundle(tmp_path, "1.0.0")); up.activate("1.0.0")
    assert up.rollback() is None                 # nothing to revert to


def test_prune_keeps_active_previous_and_newest(tmp_path):
    up = _updater(tmp_path, {})
    for v in ("1.0.0", "1.1.0", "1.2.0", "1.3.0"):
        up.stage(_bundle(tmp_path, v))
    up.activate("1.0.0")     # previous=none
    up.activate("1.3.0")     # active=1.3.0, previous=1.0.0
    removed = up.prune(keep=1)
    kept = {r.version for r in up.list_releases()}
    assert "1.3.0" in kept and "1.0.0" in kept   # active + previous protected
    assert set(removed).issubset({"1.1.0", "1.2.0"})


class _Result:
    def __init__(self, rc):
        self.returncode = rc


def test_deps_install_offline_first_stops_when_satisfied(tmp_path, monkeypatch):
    """When the offline (--no-index) pass succeeds — deps already satisfied — no
    online install is attempted (no PyPI retries, so no long hang)."""
    (tmp_path / "requirements.txt").write_text("fastapi==1\n")
    calls = []
    monkeypatch.setattr(updater_mod.subprocess, "run",
                        lambda cmd, **kw: (calls.append((cmd, kw)), _Result(0))[1])
    _default_deps_install(tmp_path)
    assert len(calls) == 1
    assert "--no-index" in calls[0][0]
    assert calls[0][1].get("check") is not True   # offline pass isn't check=True


def test_deps_install_falls_back_online_when_missing(tmp_path, monkeypatch):
    """When the offline pass fails (something genuinely missing) it falls back to a
    fast-failing online install."""
    (tmp_path / "requirements.txt").write_text("fastapi==1\n")
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return _Result(1 if "--no-index" in cmd else 0)
    monkeypatch.setattr(updater_mod.subprocess, "run", fake_run)
    _default_deps_install(tmp_path)
    assert len(calls) == 2
    assert "--no-index" in calls[0][0]
    online = calls[1][0]
    assert "--no-index" not in online
    assert "--retries" in online and calls[1][1].get("check") is True


def test_deps_install_uses_bundled_wheels_offline(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("fastapi==1\n")
    (tmp_path / "wheels").mkdir()
    seen = {}
    monkeypatch.setattr(updater_mod.subprocess, "run",
                        lambda cmd, **kw: (seen.setdefault("cmd", cmd), _Result(0))[1])
    _default_deps_install(tmp_path)
    assert "--find-links" in seen["cmd"]
    assert str(tmp_path / "wheels") in seen["cmd"]


def test_apply_stages_activates_and_restarts(tmp_path):
    calls = {}
    up = _updater(tmp_path, calls)
    v = up.apply(_bundle(tmp_path, "2.0.0"))
    assert v == "2.0.0"
    assert up.current_version() == "2.0.0"
    assert up.pending_version() == "2.0.0"
    assert calls["restart"] == ["sdr-agent"]
    assert calls["deps"] == ["2.0.0"]
