"""The /admin/update|rollback|releases handlers, called directly with an injected
Updater (a tmp root + fake deps/restart) so no pip, systemd, or full app is needed."""
import asyncio
import io
import tarfile
from pathlib import Path

from starlette.datastructures import UploadFile

from agent import main
from agent.updater import Updater


def _bundle_bytes(version: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("VERSION", version.encode()),
                           ("agent/__init__.py", b"# agent\n"),
                           ("requirements.txt", b"")):
            info = tarfile.TarInfo(name); info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _patch_updater(monkeypatch, tmp: Path, calls: dict) -> Updater:
    root = tmp / "releases"; root.mkdir()
    up = Updater(root, tmp / "current", service_name="sdr-agent",
                 deps_install=lambda rel: calls.setdefault("deps", []).append(rel.name),
                 restart=lambda svc: calls.setdefault("restart", []).append(svc))
    monkeypatch.setattr(main, "_make_updater", lambda: up)
    return up


def test_admin_update_and_releases(tmp_path, monkeypatch):
    calls = {}
    up = _patch_updater(monkeypatch, tmp_path, calls)

    async def scenario():
        bundle = UploadFile(filename="b.tar.gz", file=io.BytesIO(_bundle_bytes("1.2.0")))
        res = await main.admin_update(bundle=bundle)
        assert res.ok and res.to_version == "1.2.0"
        assert up.current_version() == "1.2.0"
        assert calls["restart"] == ["sdr-agent"] and calls["deps"] == ["1.2.0"]

        releases = await main.admin_releases()
        assert [r.version for r in releases] == ["1.2.0"]
        assert releases[0].active is True

    asyncio.run(scenario())


def test_admin_update_rejects_bad_bundle(tmp_path, monkeypatch):
    _patch_updater(monkeypatch, tmp_path, {})

    async def scenario():
        bad = io.BytesIO()
        with tarfile.open(fileobj=bad, mode="w:gz") as tar:
            info = tarfile.TarInfo("VERSION"); info.size = 3
            tar.addfile(info, io.BytesIO(b"9.9"))          # no agent/ → invalid
        res = await main.admin_update(bundle=UploadFile(filename="b.tar.gz",
                                                        file=io.BytesIO(bad.getvalue())))
        assert res.ok is False and "missing" in res.message.lower()

    asyncio.run(scenario())


def test_admin_rollback(tmp_path, monkeypatch):
    calls = {}
    up = _patch_updater(monkeypatch, tmp_path, calls)

    async def scenario():
        for v in ("1.0.0", "1.1.0"):
            await main.admin_update(bundle=UploadFile(filename="b.tar.gz",
                                                      file=io.BytesIO(_bundle_bytes(v))))
        res = await main.admin_rollback()
        assert res.ok and res.to_version == "1.0.0"
        assert up.current_version() == "1.0.0"

    asyncio.run(scenario())


def test_admin_rollback_without_previous(tmp_path, monkeypatch):
    _patch_updater(monkeypatch, tmp_path, {})

    async def scenario():
        await main.admin_update(bundle=UploadFile(filename="b.tar.gz",
                                                  file=io.BytesIO(_bundle_bytes("1.0.0"))))
        res = await main.admin_rollback()
        assert res.ok is False and "no previous" in res.message.lower()

    asyncio.run(scenario())
