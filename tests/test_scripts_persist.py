"""The deployed transmit LIBRARY survives an agent (OTA) update — the agent half of
"scripts survive an update". The library moved to the persistent SCRIPTS_DIR
(STATE_DIR/scripts), alongside configs/data, instead of living inside the release code
dir that an update swaps out. Two boot-time behaviours make that transition seamless:

  * ``_seed_scripts_dir`` (main) populates an empty persistent dir on first boot — MIGRATING
    the previous release's stranded library (a field unit upgrading from a pre-persistent
    agent keeps its scripts) or SEEDING the release's bundled defaults (a fresh install).
  * ``_ensure_paramkit_on_path`` / ``_resolve_script_path`` (process_manager) keep a relocated
    script launchable: ``import paramkit`` still resolves (BASE_DIR on PYTHONPATH) and a task
    baked with the OLD release-local command path still finds its file (SCRIPTS_DIR fallback).

Seed/migration is driven directly against a tmp OTA layout (like the other endpoint tests);
skipped where starlette/fastapi aren't installed."""
import os

import pytest

pytest.importorskip("starlette.datastructures")
pytest.importorskip("fastapi")

from agent import config as cfg
from agent import main
from agent.process_manager import _ensure_paramkit_on_path, _resolve_script_path


# ── import paramkit + old-path resolution keep a relocated script launchable ─────

def test_ensure_paramkit_prepends_base_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "BASE_DIR", tmp_path / "opt" / "sdr-agent")
    env = _ensure_paramkit_on_path({})
    assert env["PYTHONPATH"] == str(tmp_path / "opt" / "sdr-agent")
    # An existing PYTHONPATH is preserved, BASE_DIR first (so the shipped paramkit wins).
    env2 = _ensure_paramkit_on_path({"PYTHONPATH": "/some/where"})
    assert env2["PYTHONPATH"] == str(tmp_path / "opt" / "sdr-agent") + os.pathsep + "/some/where"


def test_resolve_script_path_falls_back_to_scripts_dir(monkeypatch, tmp_path):
    # The task was baked with the OLD release-local path; after the update that file is gone,
    # the real script now lives (migrated) in the persistent SCRIPTS_DIR.
    persistent = tmp_path / "state" / "scripts"
    (persistent / "PRN GPS").mkdir(parents=True)
    (persistent / "PRN GPS" / "gps.py").write_text("print(1)\n")
    monkeypatch.setattr(cfg, "SCRIPTS_DIR", persistent)
    stale = str(tmp_path / "opt" / "sdr-agent-releases" / "1.22.0" / "scripts" / "gps.py")
    out = _resolve_script_path(["python3", stale, "--freq", "1"])
    assert out[1] == str(persistent / "PRN GPS" / "gps.py")           # found by basename in SCRIPTS_DIR
    assert out[0] == "python3" and out[2:] == ["--freq", "1"]


def test_resolve_script_path_prefers_the_command_path_dir(monkeypatch, tmp_path):
    # If the basename still resolves under the command path's own dir (a subfolder move), that
    # wins over the SCRIPTS_DIR fallback — SCRIPTS_DIR is only consulted when the local dir misses.
    local = tmp_path / "local" / "scripts"
    (local / "Sub").mkdir(parents=True)
    (local / "Sub" / "cw.py").write_text("print('local')\n")
    other = tmp_path / "state" / "scripts"
    other.mkdir(parents=True)
    (other / "cw.py").write_text("print('persistent')\n")
    monkeypatch.setattr(cfg, "SCRIPTS_DIR", other)
    out = _resolve_script_path(["python3", str(local / "cw.py")])
    assert out[1] == str(local / "Sub" / "cw.py")                     # local subfolder, not SCRIPTS_DIR


# ── _seed_scripts_dir: OTA layout fixtures ──────────────────────────────────────

def _ota_layout(tmp_path, *, current="1.23.0", previous=None,
                releases=None, previous_marker=None):
    """Build a versioned OTA tree under tmp_path and point cfg at it. `releases` maps a
    version → the .py basenames its scripts/ dir holds; `current` is symlinked as CURRENT_LINK.
    Returns (releases_root, current_link, markers_dir)."""
    releases = releases or {}
    root = tmp_path / "sdr-agent-releases"
    root.mkdir(parents=True, exist_ok=True)
    for ver, names in releases.items():
        sd = root / ver / "scripts"
        sd.mkdir(parents=True, exist_ok=True)
        for n in names:
            (sd / n).write_text(f"# {ver}:{n}\nprint('{ver}')\n")
    link = tmp_path / "current"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(root / current)
    markers = root / ".markers"
    markers.mkdir(parents=True, exist_ok=True)
    marker_ver = previous_marker if previous_marker is not None else previous
    if marker_ver:
        (markers / "previous").write_text(marker_ver, encoding="utf-8")
    return root, link, markers


def _point_cfg(monkeypatch, tmp_path, root, link, scripts_dir, bundled):
    monkeypatch.setattr(cfg, "RELEASES_DIR", root)
    monkeypatch.setattr(cfg, "CURRENT_LINK", link)
    monkeypatch.setattr(cfg, "BASE_DIR", link)          # BASE_DIR is the `current` symlink in an OTA install
    monkeypatch.setattr(cfg, "SCRIPTS_DIR", scripts_dir)
    monkeypatch.setattr(cfg, "BUNDLED_SCRIPTS_DIR", bundled)
    monkeypatch.setattr(main, "SCRIPTS_DIR", scripts_dir)


def _names(d):
    return sorted(p.name for p in d.rglob("*.py"))


# ── _seed_scripts_dir behaviours ────────────────────────────────────────────────

def test_seed_is_a_noop_when_scripts_dir_already_has_scripts(monkeypatch, tmp_path):
    root, link, _ = _ota_layout(tmp_path, current="1.23.0", previous="1.22.0",
                                releases={"1.23.0": ["bundled.py"], "1.22.0": ["old.py"]})
    persistent = tmp_path / "state" / "scripts"
    persistent.mkdir(parents=True)
    (persistent / "deployed.py").write_text("print('mine')\n")     # already populated
    _point_cfg(monkeypatch, tmp_path, root, link, persistent, link / "scripts")
    main._seed_scripts_dir()
    assert _names(persistent) == ["deployed.py"]                   # untouched — not overwritten/merged


def test_seed_migrates_the_previous_releases_library(monkeypatch, tmp_path):
    # The load-bearing field case: a unit upgrading from a pre-persistent agent keeps its library.
    root, link, _ = _ota_layout(
        tmp_path, current="1.23.0", previous="1.22.0",
        releases={"1.23.0": ["bundled.py"],
                  "1.22.0": ["gps_ca.py", "cw.py"]})              # the deployed lib, stranded in the old release
    persistent = tmp_path / "state" / "scripts"                   # empty (first 1.23.0 boot)
    _point_cfg(monkeypatch, tmp_path, root, link, persistent, link / "scripts")
    main._seed_scripts_dir()
    assert _names(persistent) == ["cw.py", "gps_ca.py"]           # migrated from 1.22.0, NOT the bundled defaults
    assert (persistent / "gps_ca.py").read_text().startswith("# 1.22.0")


def test_seed_preserves_subfolders_when_migrating(monkeypatch, tmp_path):
    root, link, _ = _ota_layout(tmp_path, current="1.23.0", previous="1.22.0",
                                releases={"1.23.0": ["bundled.py"]})
    # give the previous release a nested library
    old = root / "1.22.0" / "scripts" / "PRN GPS"
    old.mkdir(parents=True)
    (old / "gps.py").write_text("# nested\n")
    persistent = tmp_path / "state" / "scripts"
    _point_cfg(monkeypatch, tmp_path, root, link, persistent, link / "scripts")
    main._seed_scripts_dir()
    assert (persistent / "PRN GPS" / "gps.py").is_file()          # folder structure preserved


def test_seed_falls_back_to_another_release_without_a_previous_marker(monkeypatch, tmp_path):
    # A very old agent may have no `previous` marker; still rescue a library from another release.
    root, link, _ = _ota_layout(tmp_path, current="1.23.0", previous_marker="",
                                releases={"1.23.0": ["bundled.py"], "1.21.0": ["legacy.py"]})
    persistent = tmp_path / "state" / "scripts"
    _point_cfg(monkeypatch, tmp_path, root, link, persistent, link / "scripts")
    main._seed_scripts_dir()
    assert _names(persistent) == ["legacy.py"]                    # found the only other release with scripts


def test_seed_from_bundled_on_a_fresh_install(monkeypatch, tmp_path):
    # No previous release to migrate → seed the release's bundled defaults.
    root, link, _ = _ota_layout(tmp_path, current="1.23.0",
                                releases={"1.23.0": ["bundled_a.py", "bundled_b.py"]})
    persistent = tmp_path / "state" / "scripts"
    _point_cfg(monkeypatch, tmp_path, root, link, persistent, link / "scripts")
    main._seed_scripts_dir()
    assert _names(persistent) == ["bundled_a.py", "bundled_b.py"]  # the current release's bundled scripts


def test_seed_is_a_noop_for_a_classic_single_dir_install(monkeypatch, tmp_path):
    # STATE_DIR == BASE_DIR, so SCRIPTS_DIR == BUNDLED_SCRIPTS_DIR — nothing should be copied
    # onto itself, and no release lookup should strand the install.
    base = tmp_path / "opt" / "sdr-agent"
    scripts = base / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "installed.py").write_text("print('installed')\n")
    monkeypatch.setattr(cfg, "RELEASES_DIR", tmp_path / "nonexistent-releases")
    monkeypatch.setattr(cfg, "CURRENT_LINK", base)
    monkeypatch.setattr(cfg, "BASE_DIR", base)
    monkeypatch.setattr(cfg, "SCRIPTS_DIR", scripts)
    monkeypatch.setattr(cfg, "BUNDLED_SCRIPTS_DIR", scripts)
    monkeypatch.setattr(main, "SCRIPTS_DIR", scripts)
    main._seed_scripts_dir()
    assert _names(scripts) == ["installed.py"]                    # unchanged


def test_seed_never_raises_on_a_broken_layout(monkeypatch, tmp_path):
    # A missing releases root AND a missing bundled dir must not break startup.
    persistent = tmp_path / "state" / "scripts"
    monkeypatch.setattr(cfg, "RELEASES_DIR", tmp_path / "no-releases")
    monkeypatch.setattr(cfg, "CURRENT_LINK", tmp_path / "no-current")
    monkeypatch.setattr(cfg, "BASE_DIR", tmp_path / "no-base")
    monkeypatch.setattr(cfg, "SCRIPTS_DIR", persistent)
    monkeypatch.setattr(cfg, "BUNDLED_SCRIPTS_DIR", tmp_path / "no-base" / "scripts")
    monkeypatch.setattr(main, "SCRIPTS_DIR", persistent)
    main._seed_scripts_dir()                                       # no exception
    assert not persistent.exists() or _names(persistent) == []     # nothing to seed → left empty
