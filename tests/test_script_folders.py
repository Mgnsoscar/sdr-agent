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
from agent.process_manager import _resolve_script_path


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
