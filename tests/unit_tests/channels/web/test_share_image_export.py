# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

from __future__ import annotations

import json
import logging
import shutil
import struct
import threading
import time
import zipfile
import zlib
from pathlib import Path

import pytest

from jiuwenswarm.channels.web import share_image_export
from jiuwenswarm.channels.web.share_image_export import (
    ShareImageExportManager,
    _validate_png,
    _validate_zip,
)


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", zlib.crc32(chunk_type + data))


def _test_png(*, height: int = 1) -> bytes:
    ihdr = struct.pack(">IIBBBBB", 2250, height, 8, 6, 0, 0, 0)
    pixels = b"\x00" + b"\x00\x00\x00\xff" * 2250
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(pixels))
        + _png_chunk(b"IEND", b"")
    )


def _wait_for_terminal_state(manager: ShareImageExportManager, job_id: str) -> dict:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status = manager.get_status(job_id)
        assert status is not None
        if status["state"] in {"completed", "failed"}:
            return status
        time.sleep(0.01)
    raise AssertionError("share image export job did not finish")


@pytest.mark.parametrize("error", [None, "render_failed\n下一行"])
def test_share_image_cli_flushes_json_protocol_events(monkeypatch, tmp_path: Path, error: str | None) -> None:
    events: list[dict[str, str]] = []

    class ProtocolOutput:
        pending = ""

        def write(self, text: str) -> int:
            self.pending += text
            return len(text)

        def flush(self) -> None:
            assert self.pending.endswith("\n")
            assert len(self.pending.splitlines()) == 1
            events.append(json.loads(self.pending))
            self.pending = ""

    def renderer(*, base_url, job_id, output_path, on_phase) -> Path:
        assert base_url == "http://127.0.0.1:5173"
        assert job_id == "job-1"
        assert output_path == tmp_path / "share.png"
        on_phase("rendering")
        assert events == [{"phase": "rendering"}]
        if error is not None:
            raise RuntimeError(error)
        return output_path.with_suffix(".zip")

    monkeypatch.setattr(share_image_export.sys, "stdout", ProtocolOutput())
    monkeypatch.setattr(share_image_export.sys, "argv", [
        "share_image_export",
        "--base-url", "http://127.0.0.1:5173",
        "--job-id", "job-1",
        "--output", str(tmp_path / "share.png"),
    ])
    monkeypatch.setattr(share_image_export, "render_share_image", renderer)

    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        assert share_image_export._run_cli() == (1 if error is not None else 0)
    finally:
        logging.disable(previous_disable)
    expected_result = {"error": error} if error is not None else {"phase": "completed", "filename": "share.zip"}
    assert events == [{"phase": "rendering"}, expected_result]


@pytest.mark.parametrize("failure_stage", ["write", "flush"])
def test_share_image_cli_propagates_output_failures_and_closes_handler(monkeypatch, failure_stage: str) -> None:
    failure = OSError("protocol_output_failed")
    handlers = []
    closed_handlers = []
    handler_class = share_image_export._CliEventHandler

    class FailingOutput:
        def write(self, text: str) -> int:
            if failure_stage == "write":
                raise failure
            return len(text)

        def flush(self) -> None:
            if failure_stage == "flush":
                raise failure

    class TrackingHandler(handler_class):
        def __init__(self, stream):
            super().__init__(stream)
            handlers.append(self)

        def close(self) -> None:
            closed_handlers.append(self)
            super().close()

    monkeypatch.setattr(share_image_export.sys, "stdout", FailingOutput())
    monkeypatch.setattr(share_image_export, "_CliEventHandler", TrackingHandler)

    with pytest.raises(OSError) as raised:
        share_image_export._write_cli_event({"phase": "rendering"})

    assert raised.value is failure
    assert len(handlers) == 1
    assert closed_handlers == handlers


def test_share_image_export_job_freezes_snapshot_and_publishes_result() -> None:
    rendered: dict[str, object] = {}

    def renderer(*, base_url, job_id, output_path, on_phase) -> Path:
        rendered.update(base_url=base_url, job_id=job_id, output_path=output_path)
        on_phase("rendering")
        output_path.write_bytes(b"png-result")
        return output_path

    manager = ShareImageExportManager(renderer=renderer)
    created = manager.create_job(
        session_id="session-1",
        snapshot={"session_id": "session-1", "records": [{"role": "user", "content": "hello"}]},
        filename="share.png",
        locale="en",
        base_url="http://127.0.0.1:5173",
    )

    status = _wait_for_terminal_state(manager, created["job_id"])
    assert status == {
        "job_id": created["job_id"],
        "session_id": "session-1",
        "filename": "share.png",
        "state": "completed",
        "phase": "completed",
        "error": None,
    }
    snapshot_path = manager.get_snapshot_path(created["job_id"])
    assert snapshot_path is not None
    assert json.loads(snapshot_path.read_text(encoding="utf-8")) == {
        "filename": "share.png",
        "locale": "en",
        "snapshot": {
            "session_id": "session-1",
            "records": [{"role": "user", "content": "hello"}],
        },
    }
    result = manager.get_result(created["job_id"])
    assert result is not None
    assert result[0].read_bytes() == b"png-result"
    assert rendered["base_url"] == "http://127.0.0.1:5173"
    assert rendered["job_id"] == created["job_id"]
    shutil.rmtree(snapshot_path.parent)


def test_share_image_export_job_reports_renderer_failure() -> None:
    def renderer(**_kwargs) -> Path:
        raise RuntimeError("renderer_failed")

    manager = ShareImageExportManager(renderer=renderer)
    created = manager.create_job(
        session_id="session-2",
        snapshot={"session_id": "session-2", "records": []},
        filename="share.png",
        locale="zh",
        base_url="http://127.0.0.1:5173",
    )

    status = _wait_for_terminal_state(manager, created["job_id"])
    assert status["state"] == "failed"
    assert status["phase"] == "failed"
    assert status["error"] == "renderer_failed"
    assert manager.get_result(created["job_id"]) is None
    snapshot_path = manager.get_snapshot_path(created["job_id"])
    assert snapshot_path is not None
    shutil.rmtree(snapshot_path.parent)


def test_share_image_export_filename_cannot_escape_job_directory() -> None:
    def renderer(*, output_path: Path, **_kwargs) -> Path:
        output_path.write_bytes(b"png-result")
        return output_path

    manager = ShareImageExportManager(renderer=renderer)
    created = manager.create_job(
        session_id="session-3",
        snapshot={"session_id": "session-3", "records": []},
        filename="../../outside.png",
        locale="zh",
        base_url="http://127.0.0.1:5173",
    )

    status = _wait_for_terminal_state(manager, created["job_id"])
    result = manager.get_result(created["job_id"])
    assert status["filename"] == "outside.png"
    assert result is not None
    assert result[0].name == "outside.png"
    assert result[0].parent == manager.get_snapshot_path(created["job_id"]).parent
    shutil.rmtree(result[0].parent)

    created = manager.create_job(
        session_id="session-4",
        snapshot={"session_id": "session-4", "records": []},
        filename="..",
        locale="zh",
        base_url="http://127.0.0.1:5173",
    )
    status = _wait_for_terminal_state(manager, created["job_id"])
    result = manager.get_result(created["job_id"])
    assert status["filename"] == "jiuwenswarm-share.png"
    assert result is not None
    assert result[0].parent == manager.get_snapshot_path(created["job_id"]).parent
    shutil.rmtree(result[0].parent)


def test_share_image_export_reuses_active_job_for_same_session() -> None:
    started = threading.Event()
    release = threading.Event()

    def renderer(*, output_path: Path, **_kwargs) -> Path:
        started.set()
        assert release.wait(timeout=2)
        output_path.write_bytes(b"png-result")
        return output_path

    manager = ShareImageExportManager(renderer=renderer)
    first = manager.create_job(
        session_id="session-5",
        snapshot={"session_id": "session-5", "records": [{"content": "first"}]},
        filename="first.png",
        locale="zh",
        base_url="http://127.0.0.1:5173",
    )
    assert started.wait(timeout=2)

    duplicate = manager.create_job(
        session_id="session-5",
        snapshot={"session_id": "session-5", "records": [{"content": "duplicate"}]},
        filename="duplicate.png",
        locale="zh",
        base_url="http://127.0.0.1:5173",
    )

    assert first["reused"] is False
    assert duplicate["reused"] is True
    assert duplicate["job_id"] == first["job_id"]
    assert manager.get_active_status("session-5")["job_id"] == first["job_id"]

    release.set()
    status = _wait_for_terminal_state(manager, first["job_id"])
    assert status["state"] == "completed"
    assert manager.get_active_status("session-5") is None
    snapshot_path = manager.get_snapshot_path(first["job_id"])
    assert snapshot_path is not None
    shutil.rmtree(snapshot_path.parent)


def test_share_image_export_job_publishes_renderer_selected_archive() -> None:
    def renderer(*, output_path: Path, **_kwargs) -> Path:
        archive_path = output_path.with_suffix(".zip")
        archive_path.write_bytes(b"zip-result")
        return archive_path

    manager = ShareImageExportManager(renderer=renderer)
    created = manager.create_job(
        session_id="session-6",
        snapshot={"session_id": "session-6", "records": []},
        filename="share.png",
        locale="zh",
        base_url="http://127.0.0.1:5173",
    )

    status = _wait_for_terminal_state(manager, created["job_id"])
    assert status["state"] == "completed"
    assert status["filename"] == "share.zip"
    result = manager.get_result(created["job_id"])
    assert result is not None
    assert result[0].read_bytes() == b"zip-result"
    assert result[1] == "share.zip"
    shutil.rmtree(result[0].parent)


def test_share_image_export_validates_png_height_and_zip_parts(tmp_path: Path) -> None:
    png_path = tmp_path / "share.png"
    png_path.write_bytes(_test_png())
    _validate_png(png_path)

    max_height_path = tmp_path / "max-height.png"
    max_height_path.write_bytes(_test_png(height=128_000))
    _validate_png(max_height_path)

    too_tall_path = tmp_path / "too-tall.png"
    too_tall_path.write_bytes(_test_png(height=128_001))
    with pytest.raises(RuntimeError, match="share_export_png_height_unsupported"):
        _validate_png(too_tall_path)

    archive_path = tmp_path / "share.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("share-part-01-of-02.png", _test_png())
        archive.writestr("share-part-02-of-02.png", _test_png())
    _validate_zip(archive_path)

    invalid_archive_path = tmp_path / "invalid.zip"
    with zipfile.ZipFile(invalid_archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("share.png", _test_png())
    with pytest.raises(RuntimeError, match="share_export_zip_parts_missing"):
        _validate_zip(invalid_archive_path)

    out_of_order_archive_path = tmp_path / "out-of-order.zip"
    with zipfile.ZipFile(out_of_order_archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("share-part-02-of-02.png", _test_png())
        archive.writestr("share-part-01-of-02.png", _test_png())
    with pytest.raises(RuntimeError, match="share_export_zip_part_invalid"):
        _validate_zip(out_of_order_archive_path)

    inconsistent_archive_path = tmp_path / "inconsistent.zip"
    with zipfile.ZipFile(inconsistent_archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("share-part-01-of-03.png", _test_png())
        archive.writestr("share-part-02-of-03.png", _test_png())
    with pytest.raises(RuntimeError, match="share_export_zip_part_invalid"):
        _validate_zip(inconsistent_archive_path)
