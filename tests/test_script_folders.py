"""Scripts in organizational subfolders — the agent half of the "real
subdirectories" feature. A script keeps its basename identity: it deploys into a
real subdir, but /scripts still addresses it by basename (resolved recursively),
the launcher finds it by basename when a task references it, and GET /library
reports each script's folder so drift round-trips. Endpoint fns are called
directly with SCRIPTS_DIR pointed at a tmp dir (no full app), like the other
endpoint tests — skipped where starlette/fastapi aren't installed."""
import asyncio
import io

import pytest

UploadFile = pytest.importorskip("starlette.datastructures").UploadFile
HTTPException = pytest.importorskip("fastapi").HTTPException

from agent import main
from agent.models import TaskConfig
from agent.process_manager import ProcessManager, _resolve_script_path


def _scripts_dir(tmp_path, monkeypatch):
    d = tmp_path / "scripts"
    d.mkdir()
    monkeypatch.setattr(main, "SCRIPTS_DIR", d)
    return d


# ── launcher resolves a basename to its nested path ─────────────────────────────

def test_resolve_script_path_finds_nested(tmp_path):
    root = tmp_path / "scripts"
    (root / "GPS PRN").mkdir(parents=True)
    (root / "GPS PRN" / "foo.py").write_text("print(1)\n")
    # the task command still names the flat path (the basename identity)
    cmd = ["python3", str(root / "foo.py"), "--freq", "1"]
    out = _resolve_script_path(cmd)
    assert out[1] == str(root / "GPS PRN" / "foo.py")
    assert out[0] == "python3" and out[2:] == ["--freq", "1"]


def test_resolve_script_path_leaves_a_flat_script(tmp_path):
    root = tmp_path / "scripts"
    root.mkdir()
    (root / "foo.py").write_text("print(1)\n")
    cmd = ["python3", str(root / "foo.py")]
    assert _resolve_script_path(cmd) == cmd


# ── the run-log / export spec path resolves a subfolder script the SAME way ──────
# The launch relocates a basename-referenced script into its real subfolder
# (_resolve_script_path, above); the run-log / spreadsheet-export spec (_script_spec →
# _read_script_source) must do the same, or a task whose script lives in a subfolder
# launches fine yet logs/exports with NO parameter columns and power in the base
# quantity only (the uncalibrated fallback), even though the client authored it against
# a valid spec (its /scripts params search finds the subfolder file too).

_SCRIPT_SRC = (
    "from paramkit import Script\n"
    "SCRIPT = (Script('gps')\n"
    "    .number('--freq', name='Center frequency', unit='MHz', default=1575.42, is_freq=True)\n"
    "    .number('--power', name='Power', unit='dBm', default=-50.0, live=True)\n"
    "    .choice('--rf', options=['on', 'off'], name='RF', default='on', live=True))\n"
)


def test_read_script_source_finds_nested_script(tmp_path):
    root = tmp_path / "scripts"
    (root / "GPS PRN").mkdir(parents=True)
    (root / "GPS PRN" / "gps.py").write_text(_SCRIPT_SRC)
    # The task command names the flat path (basename identity) — the file is nested.
    naive = str(root / "gps.py")
    src = ProcessManager._read_script_source(naive, None)
    assert src == _SCRIPT_SRC                                       # found in the subfolder
    from agent.argspec import extract_params
    params = extract_params(src)["params"]
    assert len(params) == 3                                         # a real spec, not empty


def test_read_script_source_flat_and_missing(tmp_path):
    root = tmp_path / "scripts"
    root.mkdir()
    (root / "flat.py").write_text(_SCRIPT_SRC)
    assert ProcessManager._read_script_source(str(root / "flat.py"), None) == _SCRIPT_SRC
    # A genuinely absent script (nowhere under its dir) still yields None, not a raise.
    assert ProcessManager._read_script_source(str(root / "nope.py"), None) is None


def test_read_script_source_relative_to_working_dir(tmp_path):
    root = tmp_path / "scripts"
    root.mkdir()
    (root / "rel.py").write_text(_SCRIPT_SRC)
    # A relative command path resolves against the task's working_dir.
    assert ProcessManager._read_script_source("rel.py", str(root)) == _SCRIPT_SRC


# ── _script_spec never negatively-caches; reload drops the cache ─────────────────
# An agent update wipes the release-local scripts dir, so a task can be registered while its
# script is momentarily absent. Reading its spec then must NOT poison the argspec cache with a
# None — otherwise a later library deploy (which restores the script) can't fix the run log /
# export until the agent restarts (the field report: "run before deploy → deploy no longer fixes").

def _mgr_with_task(tmp_path, script_path):
    tasks = {"tx": TaskConfig(name="tx", command=["python3", str(script_path)],
                              working_dir=str(script_path.parent))}
    return ProcessManager(tasks, tmp_path, "unit-a")


def test_script_spec_missing_then_deployed_recovers_without_restart(tmp_path):
    root = tmp_path / "scripts"
    root.mkdir()
    script = root / "tx.py"                               # not yet deployed to this unit
    mgr = _mgr_with_task(tmp_path, script)
    assert mgr._script_spec("tx") is None                # absent → None
    assert mgr._script_specs == {}                       # …and the miss is NOT cached
    script.write_text(_SCRIPT_SRC)                       # library deployed (no restart)
    spec = mgr._script_spec("tx")                        # picked up on the next read
    assert spec is not None and len(spec["params"]) == 3


def test_reload_clears_the_script_spec_cache(tmp_path):
    root = tmp_path / "scripts"
    root.mkdir()
    script = root / "tx.py"
    script.write_text(_SCRIPT_SRC)
    mgr = _mgr_with_task(tmp_path, script)
    assert len(mgr._script_spec("tx")["params"]) == 3    # a real spec is cached
    assert mgr._script_specs != {}
    # A deploy re-registers the task; the argspec cache is dropped so a changed/re-uploaded
    # script is re-read rather than served stale from the cache.
    asyncio.run(mgr.reload({"tx": TaskConfig(name="tx", command=["python3", str(script)],
                                             working_dir=str(root))}))
    assert mgr._script_specs == {}


def test_active_flag_missing_then_deployed_recovers_without_restart(tmp_path):
    # _active_flag has the SAME negative-cache hazard as _script_spec (it used to cache {} on a
    # read miss and never recover). It now memoises only a real read and uses the same
    # subfolder/SCRIPTS_DIR-aware resolution, so a re-deploy fixes it without a restart.
    root = tmp_path / "scripts"
    root.mkdir()
    script = root / "tx.py"                               # not yet deployed
    mgr = _mgr_with_task(tmp_path, script)
    assert mgr._active_flag("tx", "rf") == "--rf"         # absent → fallback flag
    assert mgr._active_flags == {}                        # …and the miss is NOT cached
    script.write_text(_SCRIPT_SRC)                        # library deployed (no restart)
    assert mgr._active_flag("tx", "rf") == "--rf"         # re-read from the deployed script
    assert mgr._active_flags != {}                        # a real read IS cached now


# ── /scripts addresses by basename regardless of subfolder ──────────────────────

def test_upload_into_folder_then_recursive_list_get_delete(tmp_path, monkeypatch):
    d = _scripts_dir(tmp_path, monkeypatch)
    uf = UploadFile(filename="chirp.py", file=io.BytesIO(b"print('hi')\n"))
    resp = asyncio.run(main.upload_script(file=uf, folder="Chirps & sweeps"))
    assert resp["saved"] == "chirp.py"
    assert (d / "Chirps & sweeps" / "chirp.py").is_file()          # real subdir
    assert asyncio.run(main.list_scripts()) == ["chirp.py"]        # basename, recursive
    got = asyncio.run(main.get_script("chirp.py"))                 # resolved by basename
    assert got["content"] == "print('hi')\n"
    asyncio.run(main.delete_script("chirp.py"))
    assert asyncio.run(main.list_scripts()) == []


def test_get_missing_script_is_404(tmp_path, monkeypatch):
    _scripts_dir(tmp_path, monkeypatch)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(main.get_script("nope.py"))
    assert ei.value.status_code == 404


# ── GET /library reports each script's folder + declared folders ────────────────

def test_library_scripts_report_folder_and_empty_folders(tmp_path, monkeypatch):
    d = _scripts_dir(tmp_path, monkeypatch)
    (d / "GPS PRN").mkdir()
    (d / "GPS PRN" / "gps.py").write_text('"""g"""\n')
    (d / "cw.py").write_text('"""c"""\n')
    (d / "Empty").mkdir()                                          # an empty folder
    by_name = {s.name: s for s in main._library_scripts()}
    assert by_name["gps.py"].folder == "GPS PRN"
    assert by_name["cw.py"].folder == ""
    assert set(main._library_folders()) == {"GPS PRN", "Empty"}


# ── folder sanitation rejects traversal ─────────────────────────────────────────

@pytest.mark.parametrize("bad", ["../etc", "a/../../b", "a/..", ".."])
def test_safe_folder_rejects_traversal(bad):
    with pytest.raises(HTTPException):
        main._safe_folder(bad)


def test_safe_folder_normalizes(tmp_path):
    assert main._safe_folder("  GPS PRN/ ") == "GPS PRN"
    assert main._safe_folder("") == ""
    assert main._safe_folder("a/b") == "a/b"
    assert main._safe_folder("/abs") == "abs"     # a leading slash is stripped, not an escape
