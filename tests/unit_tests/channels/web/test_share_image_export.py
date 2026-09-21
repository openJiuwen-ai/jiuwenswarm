# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

from __future__ import annotations

import io
import json
import logging
import shutil
import struct
import sys
import threading
import time
import types
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from urllib.parse import urlparse

import pytest

from jiuwenswarm.channels.web import app_web, share_image_export
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

    def renderer(*, base_url, job_id, output_path, on_phase, render_auth) -> Path:
        rendered.update(base_url=base_url, job_id=job_id, output_path=output_path, render_auth=render_auth)
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


# --- render_share_image: desktop cookie injection and fast-fail state machine ---


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class _FakeDownload:
    def __init__(self, writer: Callable[[Path], None]) -> None:
        self._writer = writer

    def save_as(self, path: Path) -> None:
        self._writer(Path(path))

    def failure(self):
        return None


class _FakeDownloadWaiter:
    def __init__(self, download: _FakeDownload) -> None:
        self.value = download

    def __enter__(self) -> "_FakeDownloadWaiter":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


class _FakePage:
    def __init__(
        self,
        *,
        goto_status: int | None = 200,
        states: list[dict | None] | None = None,
        state_fn: Callable[[int], dict | None] | None = None,
        download_writer: Callable[[Path], None] | None = None,
        page_error: str | None = None,
    ) -> None:
        self.goto_calls: list[tuple[str, str | None, int | None]] = []
        self.evaluate_calls: list[str] = []
        self.wait_calls: list[int] = []
        self.pageerror_handler: Callable[[str], None] | None = None
        self._goto_status = goto_status
        self._states = list(states or [])
        self._state_fn = state_fn
        self._download_writer = download_writer
        self._page_error = page_error
        self._evaluate_index = 0

    def on(self, event: str, handler: Callable[[str], None]) -> None:
        if event == "pageerror":
            self.pageerror_handler = handler

    def _emit_page_error(self) -> None:
        if self._page_error is not None and self.pageerror_handler is not None:
            self.pageerror_handler(self._page_error)

    def goto(self, url: str, wait_until=None, timeout=None):
        self.goto_calls.append((url, wait_until, timeout))
        self._emit_page_error()
        if self._goto_status is None:
            return None
        return _FakeResponse(self._goto_status)

    def evaluate(self, script: str):
        self.evaluate_calls.append(script)
        index = self._evaluate_index
        self._evaluate_index += 1
        if self._state_fn is not None:
            return self._state_fn(index)
        if not self._states:
            return None
        if index < len(self._states):
            return self._states[index]
        return self._states[-1]

    def wait_for_timeout(self, ms: int) -> None:
        self.wait_calls.append(ms)
        time.sleep(ms / 1000)

    def expect_download(self, timeout=None) -> _FakeDownloadWaiter:
        assert self._download_writer is not None
        return _FakeDownloadWaiter(_FakeDownload(self._download_writer))


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self.page = page
        self.cookies: list[dict] = []
        self.events: list[str] = []
        self.closed = False

    def add_cookies(self, cookies: list[dict]) -> None:
        self.events.append("add_cookies")
        self.cookies.extend(cookies)

    def new_page(self) -> _FakePage:
        self.events.append("new_page")
        return self.page

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, context: _FakeContext) -> None:
        self.context = context
        self.new_context_kwargs: dict = {}
        self.closed = False

    def new_context(self, **kwargs) -> _FakeContext:
        self.new_context_kwargs = kwargs
        return self.context

    def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser) -> None:
        self._browser = browser
        self.launch_kwargs: dict = {}

    def launch(self, **kwargs) -> _FakeBrowser:
        self.launch_kwargs = kwargs
        return self._browser


class _FakePlaywright:
    def __init__(self, browser: _FakeBrowser) -> None:
        self.chromium = _FakeChromium(browser)


class _FakeSyncPlaywright:
    def __init__(self, browser: _FakeBrowser) -> None:
        self._browser = browser

    def __enter__(self) -> _FakePlaywright:
        return _FakePlaywright(self._browser)

    def __exit__(self, *exc_info) -> bool:
        return False


@dataclass
class _RenderHarness:
    page: _FakePage
    context: _FakeContext
    browser: _FakeBrowser
    output_path: Path


def _write_test_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("share-part-01-of-02.png", _test_png())
        archive.writestr("share-part-02-of-02.png", _test_png())


def _make_render_harness(monkeypatch: pytest.MonkeyPatch, page: _FakePage, tmp_path: Path) -> _RenderHarness:
    context = _FakeContext(page)
    browser = _FakeBrowser(context)
    playwright_api = types.ModuleType("playwright.sync_api")
    playwright_api.sync_playwright = lambda: _FakeSyncPlaywright(browser)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", playwright_api)
    monkeypatch.setattr(share_image_export, "_configured_browser", lambda: (None, "chrome"))
    return _RenderHarness(page=page, context=context, browser=browser, output_path=tmp_path / "share.png")


def _call_render(harness: _RenderHarness, **kwargs) -> Path:
    return share_image_export.render_share_image(
        base_url="http://127.0.0.1:5173",
        job_id="job-render-1",
        output_path=harness.output_path,
        **kwargs,
    )


_READY_ZIP_STATE: list[dict] = [{"status": "ready", "filename": "share.zip"}]


def test_render_injects_desktop_cookie_before_opening_page(monkeypatch, tmp_path: Path) -> None:
    page = _FakePage(states=_READY_ZIP_STATE, download_writer=_write_test_zip)
    harness = _make_render_harness(monkeypatch, page, tmp_path)
    auth = share_image_export.ShareImageRenderAuth(cookie_name="__wsdt6173", cookie_value="desktop-secret")

    result = _call_render(harness, render_auth=auth)

    assert result.is_file()
    assert harness.context.events == ["add_cookies", "new_page"]
    assert harness.context.cookies == [{
        "name": "__wsdt6173",
        "value": "desktop-secret",
        "url": "http://127.0.0.1:5173",
        "httpOnly": True,
        "sameSite": "Lax",
    }]
    runner_url = page.goto_calls[0][0]
    assert runner_url.startswith("http://127.0.0.1:5173/share-export-runner?job_id=")
    assert "desktop-secret" not in runner_url


def test_render_cookie_tracks_instance_port_and_loopback_origin(monkeypatch, tmp_path: Path) -> None:
    page = _FakePage(states=_READY_ZIP_STATE, download_writer=_write_test_zip)
    harness = _make_render_harness(monkeypatch, page, tmp_path)
    auth = share_image_export.ShareImageRenderAuth(cookie_name="__wsdt5173", cookie_value="token-5173")

    _call_render(harness, render_auth=auth)

    cookie = harness.context.cookies[0]
    assert cookie["name"] == "__wsdt5173"
    assert cookie["url"] == "http://127.0.0.1:5173"
    assert cookie["httpOnly"] is True
    assert cookie["sameSite"] == "Lax"
    assert cookie.get("secure") in (None, False)


def test_render_without_desktop_auth_skips_cookie_injection(monkeypatch, tmp_path: Path) -> None:
    page = _FakePage(states=_READY_ZIP_STATE, download_writer=_write_test_zip)
    harness = _make_render_harness(monkeypatch, page, tmp_path)

    result = _call_render(harness)

    assert result.is_file()
    assert harness.context.events == ["new_page"]
    assert harness.context.cookies == []


@pytest.mark.parametrize("status", [403, 404, 500])
def test_render_http_error_fails_fast_without_leaking_token(
    monkeypatch,
    tmp_path: Path,
    status: int,
) -> None:
    page = _FakePage(goto_status=status)
    harness = _make_render_harness(monkeypatch, page, tmp_path)
    auth = share_image_export.ShareImageRenderAuth(cookie_name="__wsdt5173", cookie_value="desktop-secret")

    started = time.monotonic()
    with pytest.raises(RuntimeError, match=f"share_export_runner_http_{status}") as exc_info:
        _call_render(harness, render_auth=auth)
    elapsed = time.monotonic() - started

    assert elapsed < 5
    assert "desktop-secret" not in str(exc_info.value)
    assert page.evaluate_calls == []
    assert harness.context.closed
    assert harness.browser.closed


def test_render_missing_goto_response_fails_fast(monkeypatch, tmp_path: Path) -> None:
    page = _FakePage(goto_status=None)
    harness = _make_render_harness(monkeypatch, page, tmp_path)

    with pytest.raises(RuntimeError, match="share_export_runner_response_missing"):
        _call_render(harness)


def test_render_runner_not_initialized_fails_within_init_timeout(monkeypatch, tmp_path: Path) -> None:
    page = _FakePage(states=[None])
    harness = _make_render_harness(monkeypatch, page, tmp_path)

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="share_export_runner_not_initialized") as exc_info:
        _call_render(harness, init_timeout_seconds=0.2, poll_interval_seconds=0.05)
    elapsed = time.monotonic() - started

    assert elapsed < 5
    assert "desktop-secret" not in str(exc_info.value)
    assert harness.context.closed


def test_render_page_error_during_init_fails_fast_and_stays_single_line(
    monkeypatch,
    tmp_path: Path,
) -> None:
    page = _FakePage(states=[None], page_error="Uncaught TypeError:\nboom\nat bundle.js:1")
    harness = _make_render_harness(monkeypatch, page, tmp_path)
    auth = share_image_export.ShareImageRenderAuth(cookie_name="__wsdt5173", cookie_value="desktop-secret")

    with pytest.raises(RuntimeError, match="share_export_page_error") as exc_info:
        _call_render(harness, render_auth=auth, init_timeout_seconds=5, poll_interval_seconds=0.05)

    message = str(exc_info.value)
    assert "boom" in message
    assert "\n" not in message
    assert "desktop-secret" not in message


def test_render_runner_error_status_fails_immediately(monkeypatch, tmp_path: Path) -> None:
    page = _FakePage(states=[
        {"status": "rendering", "heartbeat": 0},
        {"status": "error", "error": "share_export_snapshot_http_404"},
    ])
    harness = _make_render_harness(monkeypatch, page, tmp_path)

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="share_export_snapshot_http_404") as exc_info:
        _call_render(
            harness,
            idle_timeout_seconds=30,
            absolute_timeout_seconds=30,
            poll_interval_seconds=0.05,
        )
    elapsed = time.monotonic() - started

    assert elapsed < 5
    assert page.wait_calls == [50]
    assert "desktop-secret" not in str(exc_info.value)


def test_render_idle_timeout_when_heartbeat_stops(monkeypatch, tmp_path: Path) -> None:
    page = _FakePage(states=[{"status": "rendering", "heartbeat": 0}])
    harness = _make_render_harness(monkeypatch, page, tmp_path)

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="share_export_render_stalled"):
        _call_render(
            harness,
            idle_timeout_seconds=0.3,
            absolute_timeout_seconds=30,
            poll_interval_seconds=0.05,
        )
    assert time.monotonic() - started < 10


def test_render_absolute_timeout_bounds_live_heartbeat(monkeypatch, tmp_path: Path) -> None:
    counter = {"value": 0}

    def state_fn(_index: int) -> dict:
        counter["value"] += 1
        return {"status": "rendering", "heartbeat": counter["value"]}

    page = _FakePage(state_fn=state_fn)
    harness = _make_render_harness(monkeypatch, page, tmp_path)

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="share_export_render_timeout") as exc_info:
        _call_render(
            harness,
            idle_timeout_seconds=30,
            absolute_timeout_seconds=0.5,
            poll_interval_seconds=0.05,
        )
    elapsed = time.monotonic() - started

    assert elapsed < 10
    assert "share_export_render_stalled" not in str(exc_info.value)


def test_share_image_render_auth_hides_cookie_value_in_repr() -> None:
    auth = share_image_export.ShareImageRenderAuth(cookie_name="__wsdt5173", cookie_value="desktop-secret")

    assert auth.cookie_name == "__wsdt5173"
    assert "desktop-secret" not in repr(auth)


# --- ShareImageExportManager: render auth passthrough without persistence ---


def test_share_image_export_manager_passes_render_auth_to_renderer() -> None:
    captured: dict[str, object] = {}

    def renderer(**kwargs) -> Path:
        captured.update(kwargs)
        kwargs["output_path"].write_bytes(b"png-result")
        return kwargs["output_path"]

    manager = ShareImageExportManager(renderer=renderer)
    auth = share_image_export.ShareImageRenderAuth(cookie_name="__wsdt5173", cookie_value="desktop-secret")
    created = manager.create_job(
        session_id="session-auth-1",
        snapshot={"session_id": "session-auth-1", "records": []},
        filename="share.png",
        locale="zh",
        base_url="http://127.0.0.1:5173",
        render_auth=auth,
    )

    status = _wait_for_terminal_state(manager, created["job_id"])
    assert status["state"] == "completed"
    assert captured["render_auth"] is auth
    assert "desktop-secret" not in json.dumps(status)
    snapshot_path = manager.get_snapshot_path(created["job_id"])
    assert snapshot_path is not None
    assert "desktop-secret" not in snapshot_path.read_text(encoding="utf-8")
    shutil.rmtree(snapshot_path.parent)


def test_share_image_export_manager_without_render_auth_passes_none() -> None:
    captured: dict[str, object] = {}

    def renderer(**kwargs) -> Path:
        captured.update(kwargs)
        kwargs["output_path"].write_bytes(b"png-result")
        return kwargs["output_path"]

    manager = ShareImageExportManager(renderer=renderer)
    created = manager.create_job(
        session_id="session-auth-2",
        snapshot={"session_id": "session-auth-2", "records": []},
        filename="share.png",
        locale="zh",
        base_url="http://127.0.0.1:5173",
    )

    status = _wait_for_terminal_state(manager, created["job_id"])
    assert status["state"] == "completed"
    assert captured["render_auth"] is None
    snapshot_path = manager.get_snapshot_path(created["job_id"])
    shutil.rmtree(snapshot_path.parent)


# --- app_web: desktop render auth handed to create_job ---


class _FakeShareManager:
    def __init__(self) -> None:
        self.create_job_kwargs: dict | None = None

    def get_active_status(self, session_id: str) -> None:
        return None

    def create_job(self, **kwargs) -> dict:
        self.create_job_kwargs = kwargs
        return {
            "job_id": "a" * 32,
            "session_id": kwargs["session_id"],
            "filename": "share.png",
            "state": "queued",
            "phase": "snapshot_ready",
            "error": None,
            "reused": False,
        }


class _SharePostHandlerStub:
    """Bind the real /share-api POST handler onto a minimal request double."""

    _handle_share_api_post = app_web._SpaStaticHandler._handle_share_api_post
    _read_request_body = app_web._SpaStaticHandler._read_request_body

    def __init__(self, *, body: dict, desktop_token: str, cookie_name: str, manager: _FakeShareManager) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.command = "POST"
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.desktop_token = desktop_token
        self.desktop_cookie_name = cookie_name
        self.server = SimpleNamespace(server_address=("127.0.0.1", 6173))
        self.manager = manager
        self.responses: list[tuple[int, dict]] = []

    def _write_json(self, status: int, payload: dict) -> None:
        self.responses.append((status, payload))

    def _build_share_snapshot(self, *, session_id: str) -> tuple[dict, str]:
        return {"session_id": session_id, "records": []}, "share.png"


def _patch_share_dependencies(monkeypatch: pytest.MonkeyPatch, manager: _FakeShareManager) -> None:
    monkeypatch.setattr(app_web, "_get_share_image_export_manager", lambda: manager)
    monkeypatch.setattr(app_web, "_uses_agentos_routing", lambda: False)


def test_share_api_post_hands_desktop_render_auth_to_create_job(monkeypatch) -> None:
    manager = _FakeShareManager()
    _patch_share_dependencies(monkeypatch, manager)
    handler = _SharePostHandlerStub(
        body={"session_id": "session-1"},
        desktop_token="desktop-secret",
        cookie_name="__wsdt6173",
        manager=manager,
    )

    handler._handle_share_api_post(urlparse("/share-api/jobs"))

    kwargs = manager.create_job_kwargs
    assert kwargs is not None
    assert kwargs["base_url"] == "http://127.0.0.1:6173"
    auth = kwargs["render_auth"]
    assert auth.cookie_name == "__wsdt6173"
    assert auth.cookie_value == "desktop-secret"
    status, payload = handler.responses[0]
    assert status == 202
    assert "desktop-secret" not in json.dumps(payload)


def test_share_api_post_web_mode_hands_no_render_auth(monkeypatch) -> None:
    manager = _FakeShareManager()
    _patch_share_dependencies(monkeypatch, manager)
    handler = _SharePostHandlerStub(
        body={"session_id": "session-2"},
        desktop_token="",
        cookie_name="__wsdt6173",
        manager=manager,
    )

    handler._handle_share_api_post(urlparse("/share-api/jobs"))

    kwargs = manager.create_job_kwargs
    assert kwargs is not None
    assert kwargs["render_auth"] is None
    status, payload = handler.responses[0]
    assert status == 202
    assert payload["state"] == "queued"
