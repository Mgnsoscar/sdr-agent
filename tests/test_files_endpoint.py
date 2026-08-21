"""The per-unit data store endpoints (/files, /calibration), called directly with an
injected UploadFile and cfg paths pointed at a tmp dir — no full app / lifespan.
Skipped where starlette/fastapi aren't installed (same as the other endpoint tests)."""
import asyncio
import io
import json

import pytest

UploadFile = pytest.importorskip("starlette.datastructures").UploadFile
HTTPException = pytest.importorskip("fastapi").HTTPException

from agent import main
from agent import config as cfg


SDR_POINTS = [(40, -36.0), (50, -26.0), (60, -16.0), (70, -6.0), (74, -2.5)]


def _pts(pairs):
    return [{"gain_db": g, "power_dbm": p} for g, p in pairs]


def _valid_doc():
    return {
        "schema_version": 1, "unit_id": "u1", "unit_type": "broadcaster",
        "chain": {
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 89.75},
            "operating_plane": "sdr_output",
            "limits": [{"plane": "sdr_output", "max_dbm": -2.5}],
            "planes": {"sdr_output": {"type": "measured", "quantity": "total in-band power"}},
        },
        "signals": {"gps_l1_mcode": {"amplitude": 0.8, "curves": {
            "sdr_output": {"points": _pts(SDR_POINTS)}}}},
    }


def _wire(tmp_path, monkeypatch, unit_type="broadcaster"):
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(cfg, "CALIBRATION_DOC", tmp_path / "data" / "calibration.json")
    monkeypatch.setattr(cfg, "CALIBRATION_DEFAULTS", tmp_path / "configs" / "calibration_defaults.yaml")
    monkeypatch.setattr(cfg, "UNIT_TYPE", unit_type)


def _upload(name: str, content: bytes):
    uf = UploadFile(filename=name, file=io.BytesIO(content))
    return asyncio.run(main.upload_file(file=uf))


def test_upload_valid_calibration(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, unit_type="")            # no type-defaults needed
    resp = _upload("calibration.json", json.dumps(_valid_doc()).encode())
    assert resp["saved"] == "calibration.json"
    assert "gps_l1_mcode" in resp["calibration"]
    assert (tmp_path / "data" / "calibration.json").is_file()


def test_upload_invalid_calibration_is_rejected_and_not_written(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, unit_type="")
    doc = _valid_doc()
    doc["signals"]["gps_l1_mcode"]["curves"]["sdr_output"]["points"] = _pts(
        [(40, -36.0), (50, -36.0)])                       # not invertible
    with pytest.raises(HTTPException) as exc:
        _upload("calibration.json", json.dumps(doc).encode())
    assert exc.value.status_code == 400
    assert not (tmp_path / "data" / "calibration.json").exists()


def test_upload_malformed_json_rejected(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, unit_type="")
    with pytest.raises(HTTPException) as exc:
        _upload("calibration.json", b"{ not json ")
    assert exc.value.status_code == 400


def test_executable_upload_refused(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    with pytest.raises(HTTPException) as exc:
        _upload("evil.py", b"print('hi')")
    assert exc.value.status_code == 400


def test_generic_file_roundtrip(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    _upload("notes.txt", b"hello")
    listing = asyncio.run(main.list_files())
    assert any(f["name"] == "notes.txt" for f in listing)
    got = asyncio.run(main.get_file("notes.txt"))
    assert got["content"] == "hello"
    asyncio.run(main.delete_file("notes.txt"))
    assert not (tmp_path / "data" / "notes.txt").exists()


def test_traversal_rejected(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    with pytest.raises(HTTPException):
        _upload("../escape.txt", b"x")


def test_get_calibration_summary(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, unit_type="")
    _upload("calibration.json", json.dumps(_valid_doc()).encode())
    view = asyncio.run(main.get_calibration())
    assert view["valid"] is True
    assert "gps_l1_mcode" in view["signals"]


def test_get_calibration_absent_is_404(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.get_calibration())
    assert exc.value.status_code == 404
