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
        "renderer": "server",
        "progress": None,
        "locale": "en",
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
        {"status": "rendering"},
        {"status": "error", "error": "share_export_snapshot_http_404"},
    ])
    harness = _make_render_harness(monkeypatch, page, tmp_path)

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="share_export_snapshot_http_404") as exc_info:
        _call_render(harness, poll_interval_seconds=0.05)
    elapsed = time.monotonic() - started

    assert elapsed < 5
    assert page.wait_calls == [50]
    assert "desktop-secret" not in str(exc_info.value)


def test_render_defaults_allow_long_generation_without_heartbeat(monkeypatch, tmp_path: Path) -> None:
    clock = {"value": 0.0}
    page = _FakePage(states=[
        {"status": "rendering"},
        {"status": "rendering"},
        {"status": "ready", "filename": "share.zip"},
    ], download_writer=_write_test_zip)
    harness = _make_render_harness(monkeypatch, page, tmp_path)
    monkeypatch.setattr(share_image_export.time, "monotonic", lambda: clock["value"])
    monkeypatch.setattr(page, "wait_for_timeout", lambda _ms: clock.update(value=clock["value"] + 86_400))

    result = _call_render(harness)

    assert clock["value"] == 2 * 86_400
    assert result.is_file()
    assert harness.context.closed
    assert harness.browser.closed


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

    def get_active_status(self, session_id: str, *, include_client_token: bool = False) -> None:
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
    _parse_content_length = app_web._SpaStaticHandler._parse_content_length

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


@pytest.mark.parametrize("available", [None, {"headless": True, "executable_path": "/installed/chromium"}])
def test_web_export_selects_renderer_before_starting_job(monkeypatch, available) -> None:
    manager = _FakeShareManager()
    _patch_share_dependencies(monkeypatch, manager)
    monkeypatch.setattr(share_image_export, "available_share_browser", lambda: available)
    handler = _SharePostHandlerStub(
        body={"session_id": "remote-session", "allow_browser_render": True},
        desktop_token="", cookie_name="", manager=manager,
    )
    handler._handle_share_api_post(urlparse("/share-api/jobs"))
    assert manager.create_job_kwargs["renderer"] == ("server" if available else "browser")
    assert manager.create_job_kwargs["browser_options"] == available


def _browser_job():
    def unexpected_renderer(**kwargs):
        raise AssertionError("A browser-mode job must not launch a server browser")
    manager = ShareImageExportManager(renderer=unexpected_renderer)
    snapshot = {"session_id": "remote", "records": [{"content": "captured"}]}
    created = manager.create_job(
        session_id="remote", snapshot=snapshot, filename="share.png", locale="zh",
        base_url="http://localhost", renderer="browser",
    )
    snapshot["records"].append({"content": "later"})
    return manager, created


def _claim_browser_download(manager, created) -> str:
    claim = manager.update_browser_job(created['job_id'], {
        'client_token': created['client_token'], 'claim_download': True,
    })
    assert claim['download_claimed'] is True
    attempt_id = claim['download_attempt_id']
    assert len(attempt_id) == 32 and all(char in '0123456789abcdef' for char in attempt_id)
    return attempt_id


def test_browser_export_owns_fixed_snapshot_and_authenticated_progress() -> None:
    manager, created = _browser_job()
    job_id, token = created["job_id"], created["client_token"]
    try:
        assert created["state"] == "queued"
        assert created["renderer"] == "browser"
        assert "client_token" not in manager.get_status(job_id)
        saved = json.loads(manager.get_snapshot_path(job_id).read_text())
        assert saved["snapshot"]["records"] == [{"content": "captured"}]
        status = manager.update_browser_job(job_id, {
            "client_token": token, "state": "running", "phase": "rendering", "progress": 40,
        })
        assert status["progress"] == 40
        png = _test_png()
        status = manager.store_browser_result(job_id, token, "share.png", io.BytesIO(png), len(png))
        assert status["state"] == "completed"
        assert manager.get_active_status("remote") is None
        assert manager.get_result(job_id)[0].read_bytes() == png
        late = manager.update_browser_job(job_id, {
            "client_token": token, "state": "failed", "phase": "failed",
        })
        assert late["state"] == "completed"
        assert manager.mark_browser_download_started(job_id, _claim_browser_download(manager, created)) is True
        assert manager.get_status(job_id)["phase"] == "download_started"
    finally:
        shutil.rmtree(manager.get_snapshot_path(job_id).parent)


@pytest.mark.parametrize("token", ["wrong", "中文", None])
def test_browser_export_rejects_unauthenticated_updates(token) -> None:
    manager, created = _browser_job()
    try:
        with pytest.raises(PermissionError):
            manager.update_browser_job(created["job_id"], {
                "client_token": token, "state": "completed", "phase": "completed",
            })
        assert manager.get_status(created["job_id"])["state"] == "queued"
    finally:
        shutil.rmtree(manager.get_snapshot_path(created["job_id"]).parent)


@pytest.mark.parametrize("update", [
    {"state": {}, "phase": "rendering"},
    {"state": "running", "phase": []},
    {"state": "completed", "phase": "rendering"},
    {"state": "running", "phase": "rendering", "progress": True},
    {"state": "running", "phase": "rendering", "progress": 101},
])
def test_browser_export_validates_transition_payload(update) -> None:
    manager, created = _browser_job()
    try:
        with pytest.raises(ValueError):
            manager.update_browser_job(created["job_id"], {**update, "client_token": created["client_token"]})
        assert manager.get_status(created["job_id"])["state"] == "queued"
    finally:
        shutil.rmtree(manager.get_snapshot_path(created["job_id"]).parent)


def test_browser_export_abandoned_snapshot_expires(monkeypatch) -> None:
    manager, created = _browser_job()
    snapshot = manager.get_snapshot_path(created["job_id"])
    monkeypatch.setattr(share_image_export.time, "time", lambda: 10**12)
    assert manager.get_status(created["job_id"]) is None
    assert not snapshot.parent.exists()


def test_missing_explicit_browser_does_not_install_or_probe_other_browsers(monkeypatch) -> None:
    monkeypatch.setattr(share_image_export, "get_config", lambda: {"browser": {"chrome_path": "/missing-export-browser"}})
    assert share_image_export.available_share_browser() is None


def test_available_configured_browser_is_reused(monkeypatch, tmp_path) -> None:
    executable = tmp_path / "chromium"
    executable.touch()
    executable.chmod(0o755)
    monkeypatch.setattr(share_image_export, "get_config", lambda: {"browser": {"chrome_path": str(executable)}})
    assert share_image_export.available_share_browser() == {"headless": True, "executable_path": str(executable)}


@pytest.mark.parametrize("data, size", [(b"not a png", 9), (_test_png(), 10**10), (_test_png(), len(_test_png()) + 1)])
def test_browser_result_upload_rejects_invalid_or_incomplete_files(data, size) -> None:
    manager, created = _browser_job()
    try:
        with pytest.raises((ValueError, RuntimeError)):
            manager.store_browser_result(created["job_id"], created["client_token"], "share.png", io.BytesIO(data), size)
        assert manager.get_result(created["job_id"]) is None
        assert not list(manager.get_snapshot_path(created["job_id"]).parent.glob('*.partial'))
    finally:
        shutil.rmtree(manager.get_snapshot_path(created["job_id"]).parent)


def test_duplicate_browser_upload_cannot_overwrite_completed_result() -> None:
    manager, created = _browser_job()
    data = _test_png()
    try:
        manager.store_browser_result(created["job_id"], created["client_token"], "share.png", io.BytesIO(data), len(data))
        with pytest.raises(ValueError, match="already_finished"):
            manager.store_browser_result(created["job_id"], created["client_token"], "share.png", io.BytesIO(b"corrupt"), 7)
        assert manager.get_status(created["job_id"])["state"] == "completed"
        assert manager.get_result(created["job_id"])[0].read_bytes() == data
    finally:
        shutil.rmtree(manager.get_snapshot_path(created["job_id"]).parent)


def test_browser_renderer_claim_is_atomic_and_duplicate_cannot_fail_the_owner() -> None:
    manager, created = _browser_job()
    job_id, token = created['job_id'], created['client_token']
    claim = {'client_token': token, 'state': 'running', 'phase': 'loading_snapshot', 'claim': True}
    try:
        assert manager.update_browser_job(job_id, claim)['state'] == 'running'
        with pytest.raises(ValueError, match='already_started'):
            manager.update_browser_job(job_id, claim)
        assert manager.get_status(job_id)['state'] == 'running'
        assert 'client_token' not in manager.get_active_status('remote')
        resumed = manager.get_active_status('remote', include_client_token=True)
        assert resumed['client_token'] == token
        assert resumed['job_id'] == job_id
    finally:
        shutil.rmtree(manager.get_snapshot_path(job_id).parent)


@pytest.mark.parametrize('claim_first', [False, True])
def test_closing_browser_page_cancels_its_own_claim_in_either_request_order(claim_first: bool) -> None:
    manager, created = _browser_job()
    job_id, token = created['job_id'], created['client_token']
    claim_id = 'b' * 32
    claim = {
        'client_token': token, 'state': 'running', 'phase': 'loading_snapshot',
        'claim': True, 'claim_id': claim_id,
    }
    try:
        if claim_first:
            manager.update_browser_job(job_id, claim)
        ended = manager.update_browser_job(job_id, {
            'client_token': token, 'state': 'failed', 'phase': 'failed',
            'error': 'share_export_tab_closed', 'expected_claim_id': claim_id,
        })
        assert ended['state'] == 'failed'
        assert ended['error'] == 'share_export_tab_closed'
        assert manager.get_active_status('remote') is None
        with pytest.raises(ValueError, match='already_started'):
            manager.update_browser_job(job_id, claim)
    finally:
        shutil.rmtree(manager.get_snapshot_path(job_id).parent)


def test_rejected_browser_page_cannot_cancel_or_fail_the_claim_owner() -> None:
    manager, created = _browser_job()
    job_id, token = created['job_id'], created['client_token']
    claim = {'client_token': token, 'state': 'running', 'phase': 'loading_snapshot', 'claim': True}
    try:
        owner = manager.update_browser_job(job_id, {**claim, 'claim_id': 'b' * 32})
        with pytest.raises(ValueError, match='already_started'):
            manager.update_browser_job(job_id, {**claim, 'claim_id': 'c' * 32})
        for error in ['share_export_tab_closed', 'share_export_status_timeout']:
            ignored = manager.update_browser_job(job_id, {
                'client_token': token, 'state': 'failed', 'phase': 'failed',
                'error': error, 'expected_claim_id': 'c' * 32,
            })
            assert ignored == owner
        assert manager.get_active_status('remote')['job_id'] == job_id
    finally:
        shutil.rmtree(manager.get_snapshot_path(job_id).parent)


def test_closing_browser_page_cannot_fail_its_published_artifact() -> None:
    manager, created = _browser_job()
    job_id, token = created['job_id'], created['client_token']
    claim_id = 'b' * 32
    try:
        manager.update_browser_job(job_id, {
            'client_token': token, 'state': 'running', 'phase': 'loading_snapshot',
            'claim': True, 'claim_id': claim_id,
        })
        png = _test_png()
        published = manager.store_browser_result(job_id, token, 'share.png', io.BytesIO(png), len(png))
        ended = manager.update_browser_job(job_id, {
            'client_token': token, 'state': 'failed', 'phase': 'failed',
            'error': 'share_export_tab_closed', 'expected_claim_id': claim_id,
        })
        assert ended == published
        assert manager.get_result(job_id)[0].read_bytes() == png
    finally:
        shutil.rmtree(manager.get_snapshot_path(job_id).parent)


@pytest.mark.parametrize('key', ['claim_id', 'expected_claim_id'])
@pytest.mark.parametrize('value', [None, '', 'x' * 32, 'a' * 31, ['a' * 32]])
def test_browser_claim_identity_rejects_invalid_values(key: str, value: object) -> None:
    manager, created = _browser_job()
    job_id = created['job_id']
    try:
        with pytest.raises(ValueError, match='client_state_invalid'):
            manager.update_browser_job(job_id, {
                'client_token': created['client_token'], 'state': 'failed', 'phase': 'failed', key: value,
            })
        assert manager.get_status(job_id)['state'] == 'queued'
    finally:
        shutil.rmtree(manager.get_snapshot_path(job_id).parent)


def test_closing_unstarted_browser_job_allows_fresh_snapshot_and_rejects_late_results() -> None:
    manager, created = _browser_job()
    job_id, token = created['job_id'], created['client_token']
    directories = [manager.get_snapshot_path(job_id).parent]
    try:
        ended = manager.update_browser_job(job_id, {
            'client_token': token, 'state': 'failed', 'phase': 'failed',
            'error': 'share_export_tab_closed', 'expected_state': 'queued',
        })
        assert ended['state'] == 'failed'
        assert manager.get_active_status('remote') is None
        snapshot = {'session_id': 'remote', 'records': [{'content': 'latest conversation'}]}
        fresh = manager.create_job(
            session_id='remote', snapshot=snapshot, filename='fresh.png', locale='zh',
            base_url='http://localhost', renderer='browser',
        )
        directories.append(manager.get_snapshot_path(fresh['job_id']).parent)
        assert fresh['job_id'] != job_id
        assert json.loads(manager.get_snapshot_path(fresh['job_id']).read_text())['snapshot'] == snapshot
        late = manager.update_browser_job(job_id, {
            'client_token': token, 'state': 'running', 'phase': 'rendering', 'progress': 50,
        })
        assert late['state'] == 'failed'
        with pytest.raises(ValueError, match='already_finished'):
            png = _test_png()
            manager.store_browser_result(job_id, token, 'share.png', io.BytesIO(png), len(png))
        assert manager.get_active_status('remote')['job_id'] == fresh['job_id']
    finally:
        for directory in directories:
            shutil.rmtree(directory)


@pytest.mark.parametrize('state', ['running', 'completed'])
def test_queued_tab_cancellation_cannot_cancel_a_claimed_or_published_job(state) -> None:
    manager, created = _browser_job()
    job_id, token = created['job_id'], created['client_token']
    try:
        manager.update_browser_job(job_id, {
            'client_token': token, 'state': 'running', 'phase': 'loading_snapshot', 'claim': True,
        })
        if state == 'completed':
            png = _test_png()
            manager.store_browser_result(job_id, token, 'share.png', io.BytesIO(png), len(png))
        result = manager.update_browser_job(job_id, {
            'client_token': token, 'state': 'failed', 'phase': 'failed',
            'error': 'share_export_tab_closed', 'expected_state': 'queued',
        })
        assert result['state'] == state
        assert result['error'] is None
    finally:
        shutil.rmtree(manager.get_snapshot_path(job_id).parent)


def test_running_browser_job_has_no_generation_deadline_or_ttl(monkeypatch) -> None:
    manager, created = _browser_job()
    job_id = created['job_id']
    now = share_image_export.time.time()
    manager.update_browser_job(job_id, {
        'client_token': created['client_token'], 'state': 'running', 'phase': 'loading_snapshot', 'claim': True,
    })
    try:
        monkeypatch.setattr(share_image_export.time, 'time', lambda: now + 86_400)
        assert manager.get_active_status('remote')['job_id'] == job_id
        assert manager.get_status(job_id)['state'] == 'running'
        assert manager.get_status(job_id)['error'] is None
    finally:
        shutil.rmtree(manager.get_snapshot_path(job_id).parent)


@pytest.mark.parametrize('state', ['completed', 'failed'])
def test_long_running_browser_job_expires_only_after_generation_finishes(monkeypatch, state) -> None:
    manager, created = _browser_job()
    job_id, token = created['job_id'], created['client_token']
    directory = manager.get_snapshot_path(job_id).parent
    now = share_image_export.time.time()
    manager.update_browser_job(job_id, {'client_token': token, 'state': 'running', 'phase': 'loading_snapshot', 'claim': True})
    monkeypatch.setattr(share_image_export.time, 'time', lambda: now + 86_400)
    try:
        if state == 'completed':
            data = _test_png()
            manager.store_browser_result(job_id, token, 'share.png', io.BytesIO(data), len(data))
        else:
            manager.update_browser_job(job_id, {'client_token': token, 'state': 'failed', 'phase': 'failed'})
        assert manager.get_status(job_id)['state'] == state
        monkeypatch.setattr(share_image_export.time, 'time', lambda: now + 86_400 + 3_601)
        assert manager.get_status(job_id) is None
        assert not directory.exists()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_browser_download_claim_is_atomic_expires_and_does_not_extend_artifact_ttl(monkeypatch) -> None:
    manager, created = _browser_job()
    job_id, token = created['job_id'], created['client_token']
    directory = manager.get_snapshot_path(job_id).parent
    data = _test_png()
    now = share_image_export.time.time()
    monkeypatch.setattr(share_image_export.time, 'time', lambda: now)
    manager.store_browser_result(job_id, token, 'share.png', io.BytesIO(data), len(data))
    barrier = threading.Barrier(3)
    claimed: list[bool] = []
    def claim() -> None:
        barrier.wait()
        claimed.append(manager.update_browser_job(job_id, {'client_token': token, 'claim_download': True})['download_claimed'])
    threads = [threading.Thread(target=claim) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()
        assert sorted(claimed) == [False, True]
        assert 'client_token' not in manager.get_status(job_id)
        assert 'download_attempt_id' not in manager.get_status(job_id)
        assert manager.get_status(job_id, include_client_token=True)['client_token'] == token
        monkeypatch.setattr(share_image_export.time, 'time', lambda: now + 59)
        assert manager.update_browser_job(job_id, {'client_token': token, 'claim_download': True})['download_claimed'] is False
        monkeypatch.setattr(share_image_export.time, 'time', lambda: now + 60)
        assert manager.update_browser_job(job_id, {'client_token': token, 'claim_download': True})['download_claimed'] is True
        assert manager.get_result(job_id)[0].read_bytes() == data
        monkeypatch.setattr(share_image_export.time, 'time', lambda: now + 3_601)
        assert manager.get_status(job_id) is None
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_download_started_cannot_be_claimed_again_after_claim_window(monkeypatch) -> None:
    manager, created = _browser_job()
    job_id, token = created['job_id'], created['client_token']
    directory = manager.get_snapshot_path(job_id).parent
    data = _test_png()
    now = share_image_export.time.time()
    try:
        manager.store_browser_result(job_id, token, 'share.png', io.BytesIO(data), len(data))
        assert manager.mark_browser_download_started(job_id, _claim_browser_download(manager, created)) is True
        monkeypatch.setattr(share_image_export.time, 'time', lambda: now + 61)
        assert manager.update_browser_job(job_id, {'client_token': token, 'claim_download': True})['download_claimed'] is False
        assert manager.get_status(job_id)['phase'] == 'download_started'
    finally:
        shutil.rmtree(directory)


def test_browser_failed_download_can_retry_once_immediately_without_regenerating() -> None:
    manager, created = _browser_job()
    job_id, token = created['job_id'], created['client_token']
    directory = manager.get_snapshot_path(job_id).parent
    data = _test_png()
    claim = {'client_token': token, 'claim_download': True}
    try:
        manager.store_browser_result(job_id, token, 'share.png', io.BytesIO(data), len(data))
        attempt_id = _claim_browser_download(manager, created)
        assert manager.mark_browser_download_started(job_id, attempt_id) is True
        manager.mark_browser_download_finished(job_id, succeeded=False, attempt_id=attempt_id)
        assert manager.get_status(job_id)['state'] == 'completed'
        assert manager.get_status(job_id)['phase'] == 'download_failed'
        assert manager.mark_browser_download_started(job_id, attempt_id) is False
        retried = manager.update_browser_job(job_id, claim)
        assert retried['download_claimed'] is True
        assert retried['download_attempt_id'] != attempt_id
        assert retried['phase'] == 'completed'
        assert manager.update_browser_job(job_id, claim)['download_claimed'] is False
        assert manager.get_result(job_id)[0].read_bytes() == data
    finally:
        shutil.rmtree(directory)


def test_browser_sent_download_cannot_be_claimed_again(monkeypatch) -> None:
    manager, created = _browser_job()
    job_id, token = created['job_id'], created['client_token']
    directory = manager.get_snapshot_path(job_id).parent
    data = _test_png()
    now = share_image_export.time.time()
    try:
        manager.store_browser_result(job_id, token, 'share.png', io.BytesIO(data), len(data))
        attempt_id = _claim_browser_download(manager, created)
        assert manager.mark_browser_download_started(job_id, attempt_id) is True
        manager.mark_browser_download_finished(job_id, succeeded=True, attempt_id=attempt_id)
        monkeypatch.setattr(share_image_export.time, 'time', lambda: now + 61)
        assert manager.update_browser_job(job_id, {'client_token': token, 'claim_download': True})['download_claimed'] is False
        assert manager.get_status(job_id)['phase'] == 'download_sent'
        assert manager.get_status(job_id)['state'] == 'completed'
    finally:
        shutil.rmtree(directory)


def test_browser_in_flight_download_has_no_transfer_deadline_or_ttl(monkeypatch) -> None:
    manager, created = _browser_job()
    job_id = created['job_id']
    directory = manager.get_snapshot_path(job_id).parent
    data = _test_png()
    now = share_image_export.time.time()
    try:
        manager.store_browser_result(job_id, created['client_token'], 'share.png', io.BytesIO(data), len(data))
        attempt_id = _claim_browser_download(manager, created)
        assert manager.mark_browser_download_started(job_id, attempt_id) is True
        monkeypatch.setattr(share_image_export.time, 'time', lambda: now + 86_400)
        assert manager.get_status(job_id)['phase'] == 'download_started'
        assert manager.get_result(job_id)[0].read_bytes() == data
        manager.mark_browser_download_finished(job_id, succeeded=True, attempt_id=attempt_id)
        assert manager.get_status(job_id)['phase'] == 'download_sent'
        monkeypatch.setattr(share_image_export.time, 'time', lambda: now + 86_400 + 3_601)
        assert manager.get_status(job_id) is None
        assert not directory.exists()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


class _ShareGetHandlerStub:
    """Run the real file-streaming handler with observable response writes."""

    _handle_share_api_get = app_web._SpaStaticHandler._handle_share_api_get
    _parse_query = staticmethod(app_web._SpaStaticHandler._parse_query)

    def __init__(self, *, command: str, output) -> None:
        self.command = command
        self.wfile = output
        self.responses: list[tuple[int, dict | None]] = []
        self.response_status: int | None = None
        self.response_headers: dict[str, str] = {}

    def _write_json(self, status: int, payload: dict | None) -> None:
        self.responses.append((status, payload))

    def send_response(self, status: int) -> None:
        self.response_status = status

    def send_header(self, name: str, value: str) -> None:
        self.response_headers[name] = value

    def end_headers(self) -> None:
        pass


@pytest.mark.parametrize('command', ['GET', 'HEAD'])
@pytest.mark.parametrize('target, expected', [
    ('/share-api/jobs?session_id=idle-session', (200, None)),
    ('/share-api/jobs', (400, {'error': 'missing_session_id'})),
    ('/share-api/jobs/' + 'a' * 32, (404, {'error': 'job_not_found'})),
])
def test_share_job_lookup_empty_result_preserves_request_errors(monkeypatch, command, target, expected) -> None:
    manager = SimpleNamespace(get_active_status=lambda session_id: None, get_status=lambda job_id: None)
    _patch_share_dependencies(monkeypatch, manager)
    handler = _ShareGetHandlerStub(command=command, output=io.BytesIO())
    handler._handle_share_api_get(urlparse(target))
    assert handler.responses == [expected]


@pytest.mark.parametrize('failure_stage', ['manager', 'query'])
def test_share_file_stream_closes_when_preparation_fails(monkeypatch, tmp_path: Path, failure_stage) -> None:
    snapshot_path = tmp_path / 'snapshot.json'
    snapshot_path.write_text('{}', encoding='utf-8')
    stream = snapshot_path.open('rb')
    file_path = SimpleNamespace(is_file=lambda: True, open=lambda mode: stream)
    manager = SimpleNamespace(get_snapshot_path=lambda job_id: file_path)
    _patch_share_dependencies(monkeypatch, manager)
    handler = _ShareGetHandlerStub(command='GET', output=io.BytesIO())
    error = RuntimeError('request preparation failed')
    manager_calls = 0

    def get_manager():
        nonlocal manager_calls
        manager_calls += 1
        if failure_stage == 'manager' and manager_calls == 2:
            raise error
        return manager

    def parse_query(query: str):
        raise error

    monkeypatch.setattr(app_web, '_get_share_image_export_manager', get_manager)
    if failure_stage == 'query':
        monkeypatch.setattr(handler, '_parse_query', parse_query)
    try:
        with pytest.raises(RuntimeError) as caught:
            handler._handle_share_api_get(urlparse('/share-api/jobs/' + 'a' * 32 + '/snapshot'))
        assert caught.value is error
        assert stream.closed
        assert handler.response_status is None
    finally:
        stream.close()


@pytest.mark.parametrize('failure_stage', ['write', 'flush'])
def test_share_snapshot_stream_error_closes_file_and_propagates(monkeypatch, tmp_path: Path, failure_stage) -> None:
    snapshot_path = tmp_path / 'snapshot.json'
    snapshot_path.write_text('{}', encoding='utf-8')
    stream = snapshot_path.open('rb')
    file_path = SimpleNamespace(is_file=lambda: True, open=lambda mode: stream)
    manager = SimpleNamespace(get_snapshot_path=lambda job_id: file_path)
    _patch_share_dependencies(monkeypatch, manager)
    error = OSError('snapshot stream interrupted')

    class Output(io.BytesIO):
        def write(self, chunk: bytes) -> int:
            if failure_stage == 'write':
                raise error
            return super().write(chunk)

        def flush(self) -> None:
            if failure_stage == 'flush':
                raise error
            super().flush()

    handler = _ShareGetHandlerStub(command='GET', output=Output())
    try:
        with pytest.raises(OSError) as caught:
            handler._handle_share_api_get(urlparse('/share-api/jobs/' + 'a' * 32 + '/snapshot'))
        assert caught.value is error
        assert stream.closed
        assert handler.responses == []
    finally:
        stream.close()


def test_share_download_marks_sent_only_after_complete_write_and_flush(monkeypatch) -> None:
    manager, created = _browser_job()
    job_id = created['job_id']
    directory = manager.get_snapshot_path(job_id).parent
    png = _test_png()
    data = png[:-12] + _png_chunk(b'tEXt', b'Description\x00' + b'x' * (2 * 1024 * 1024)) + png[-12:]
    phases: list[tuple[str, str]] = []
    class Output(io.BytesIO):
        def write(self, chunk: bytes) -> int:
            phases.append(('write', manager.get_status(job_id)['phase']))
            return super().write(chunk)

        def flush(self) -> None:
            phases.append(('flush', manager.get_status(job_id)['phase']))
            super().flush()

    output = Output()
    _patch_share_dependencies(monkeypatch, manager)
    handler = _ShareGetHandlerStub(command='GET', output=output)
    try:
        manager.store_browser_result(job_id, created['client_token'], 'share.png', io.BytesIO(data), len(data))
        attempt_id = _claim_browser_download(manager, created)
        handler._handle_share_api_get(urlparse(f'/share-api/jobs/{job_id}/download?download_attempt_id={attempt_id}'))
        assert handler.response_status == 200
        assert handler.response_headers['Content-Length'] == str(len(data))
        assert output.getvalue() == data
        assert phases == [('write', 'download_started')] * 3 + [('flush', 'download_started')]
        assert manager.get_status(job_id)['phase'] == 'download_sent'
    finally:
        shutil.rmtree(directory)


@pytest.mark.parametrize('error_type', [BrokenPipeError, ConnectionResetError, OSError])
@pytest.mark.parametrize('failure_stage', ['write', 'flush'])
def test_share_download_stream_error_preserves_generated_artifact(monkeypatch, error_type, failure_stage) -> None:
    manager, created = _browser_job()
    job_id = created['job_id']
    directory = manager.get_snapshot_path(job_id).parent
    data = _test_png()
    class Output(io.BytesIO):
        def write(self, chunk: bytes) -> int:
            if failure_stage == 'write':
                super().write(chunk[:len(chunk) // 2])
                raise error_type('stream interrupted')
            return super().write(chunk)

        def flush(self) -> None:
            if failure_stage == 'flush':
                raise error_type('stream interrupted')
            super().flush()

    _patch_share_dependencies(monkeypatch, manager)
    handler = _ShareGetHandlerStub(command='GET', output=Output())
    try:
        manager.store_browser_result(job_id, created['client_token'], 'share.png', io.BytesIO(data), len(data))
        attempt_id = _claim_browser_download(manager, created)
        handler._handle_share_api_get(urlparse(f'/share-api/jobs/{job_id}/download?download_attempt_id={attempt_id}'))
        assert manager.get_status(job_id)['state'] == 'completed'
        assert manager.get_status(job_id)['phase'] == 'download_failed'
        assert manager.get_result(job_id)[0].read_bytes() == data
        assert manager.update_browser_job(job_id, {'client_token': created['client_token'], 'claim_download': True})['download_claimed'] is True
    finally:
        shutil.rmtree(directory)


def test_share_download_head_never_confirms_a_download(monkeypatch) -> None:
    manager, created = _browser_job()
    job_id = created['job_id']
    directory = manager.get_snapshot_path(job_id).parent
    data = _test_png()
    output = io.BytesIO()
    _patch_share_dependencies(monkeypatch, manager)
    handler = _ShareGetHandlerStub(command='HEAD', output=output)
    try:
        manager.store_browser_result(job_id, created['client_token'], 'share.png', io.BytesIO(data), len(data))
        handler._handle_share_api_get(urlparse(f'/share-api/jobs/{job_id}/download'))
        assert handler.response_status == 200
        assert handler.response_headers['Content-Length'] == str(len(data))
        assert output.getvalue() == b''
        assert manager.get_status(job_id)['phase'] == 'completed'
    finally:
        shutil.rmtree(directory)


def test_share_download_cannot_be_confirmed_when_result_open_fails(monkeypatch) -> None:
    manager, created = _browser_job()
    job_id = created['job_id']
    directory = manager.get_snapshot_path(job_id).parent
    data = _test_png()
    _patch_share_dependencies(monkeypatch, manager)
    handler = _ShareGetHandlerStub(command='GET', output=io.BytesIO())
    try:
        manager.store_browser_result(job_id, created['client_token'], 'share.png', io.BytesIO(data), len(data))
        result_path = manager.get_result(job_id)[0]
        original_open = Path.open
        def fail_result_open(path, *args, **kwargs):
            if path == result_path:
                raise PermissionError('result unavailable')
            return original_open(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'open', fail_result_open)
        handler._handle_share_api_get(urlparse(f'/share-api/jobs/{job_id}/download'))
        assert handler.responses == [(404, {'error': 'job_file_not_found'})]
        assert handler.response_status is None
        assert manager.get_status(job_id)['phase'] == 'completed'
    finally:
        shutil.rmtree(directory)


def test_expired_download_claim_cannot_start_or_finish_a_new_attempt(monkeypatch) -> None:
    manager, created = _browser_job()
    job_id = created['job_id']
    directory = manager.get_snapshot_path(job_id).parent
    data = _test_png()
    now = share_image_export.time.time()
    try:
        manager.store_browser_result(job_id, created['client_token'], 'share.png', io.BytesIO(data), len(data))
        old_attempt = _claim_browser_download(manager, created)
        monkeypatch.setattr(share_image_export.time, 'time', lambda: now + 61)
        new_attempt = _claim_browser_download(manager, created)
        assert new_attempt != old_attempt
        assert manager.mark_browser_download_started(job_id, old_attempt) is False
        assert manager.get_status(job_id)['phase'] == 'completed'
        assert manager.mark_browser_download_started(job_id, new_attempt) is True
        assert manager.mark_browser_download_started(job_id, new_attempt) is False
        manager.mark_browser_download_finished(job_id, succeeded=False, attempt_id=old_attempt)
        manager.mark_browser_download_finished(job_id, succeeded=True, attempt_id=old_attempt)
        assert manager.get_status(job_id)['phase'] == 'download_started'
        manager.mark_browser_download_finished(job_id, succeeded=True, attempt_id=new_attempt)
        manager.mark_browser_download_finished(job_id, succeeded=False, attempt_id=old_attempt)
        assert manager.get_status(job_id)['phase'] == 'download_sent'
        assert manager.mark_browser_download_started(job_id, new_attempt) is False
    finally:
        shutil.rmtree(directory)


def test_share_download_rejects_late_old_get_without_streaming_or_changing_current_attempt(monkeypatch) -> None:
    manager, created = _browser_job()
    job_id = created['job_id']
    directory = manager.get_snapshot_path(job_id).parent
    data = _test_png()
    now = share_image_export.time.time()
    _patch_share_dependencies(monkeypatch, manager)
    try:
        manager.store_browser_result(job_id, created['client_token'], 'share.png', io.BytesIO(data), len(data))
        old_attempt = _claim_browser_download(manager, created)
        monkeypatch.setattr(share_image_export.time, 'time', lambda: now + 61)
        new_attempt = _claim_browser_download(manager, created)
        stale_output = io.BytesIO()
        stale_handler = _ShareGetHandlerStub(command='GET', output=stale_output)
        stale_handler._handle_share_api_get(urlparse(f'/share-api/jobs/{job_id}/download?download_attempt_id={old_attempt}'))
        assert stale_handler.responses == [(409, {'error': 'share_export_download_attempt_invalid'})]
        assert stale_handler.response_status is None
        assert stale_output.getvalue() == b''
        assert manager.get_status(job_id)['phase'] == 'completed'
        output = io.BytesIO()
        handler = _ShareGetHandlerStub(command='GET', output=output)
        handler._handle_share_api_get(urlparse(f'/share-api/jobs/{job_id}/download?download_attempt_id={new_attempt}'))
        assert handler.response_status == 200
        assert output.getvalue() == data
        assert manager.get_status(job_id)['phase'] == 'download_sent'
        duplicate_output = io.BytesIO()
        duplicate_handler = _ShareGetHandlerStub(command='GET', output=duplicate_output)
        duplicate_handler._handle_share_api_get(urlparse(f'/share-api/jobs/{job_id}/download?download_attempt_id={new_attempt}'))
        assert duplicate_handler.responses == [(409, {'error': 'share_export_download_attempt_invalid'})]
        assert duplicate_output.getvalue() == b''
        assert manager.get_status(job_id)['phase'] == 'download_sent'
    finally:
        shutil.rmtree(directory)


@pytest.mark.parametrize('attempt_id', [None, '', 'wrong', '中文'])
def test_browser_download_requires_current_claimed_attempt(attempt_id) -> None:
    manager, created = _browser_job()
    job_id = created['job_id']
    directory = manager.get_snapshot_path(job_id).parent
    data = _test_png()
    try:
        manager.store_browser_result(job_id, created['client_token'], 'share.png', io.BytesIO(data), len(data))
        _claim_browser_download(manager, created)
        assert manager.mark_browser_download_started(job_id, attempt_id) is False
        assert manager.get_status(job_id)['phase'] == 'completed'
    finally:
        shutil.rmtree(directory)


def test_server_rendered_download_does_not_require_browser_attempt(monkeypatch) -> None:
    data = _test_png()
    def renderer(**kwargs) -> Path:
        kwargs['output_path'].write_bytes(data)
        return kwargs['output_path']
    manager = ShareImageExportManager(renderer=renderer)
    created = manager.create_job(
        session_id='server-download', snapshot={'records': []}, filename='share.png', locale='zh', base_url='http://localhost',
    )
    job_id = created['job_id']
    directory = manager.get_snapshot_path(job_id).parent
    _wait_for_terminal_state(manager, job_id)
    _patch_share_dependencies(monkeypatch, manager)
    output = io.BytesIO()
    handler = _ShareGetHandlerStub(command='GET', output=output)
    try:
        handler._handle_share_api_get(urlparse(f'/share-api/jobs/{job_id}/download'))
        assert handler.response_status == 200
        assert output.getvalue() == data
        assert manager.get_status(job_id)['phase'] == 'completed'
        assert manager.get_status(job_id)['renderer'] == 'server'
    finally:
        shutil.rmtree(directory)


def test_same_download_attempt_can_start_only_one_concurrent_stream() -> None:
    manager, created = _browser_job()
    job_id = created['job_id']
    directory = manager.get_snapshot_path(job_id).parent
    data = _test_png()
    barrier = threading.Barrier(3)
    started: list[bool] = []
    try:
        manager.store_browser_result(job_id, created['client_token'], 'share.png', io.BytesIO(data), len(data))
        attempt_id = _claim_browser_download(manager, created)
        def start() -> None:
            barrier.wait()
            started.append(manager.mark_browser_download_started(job_id, attempt_id))
        threads = [threading.Thread(target=start) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()
        assert sorted(started) == [False, True]
        assert manager.get_status(job_id)['phase'] == 'download_started'
        manager.mark_browser_download_finished(job_id, succeeded=True, attempt_id=attempt_id)
    finally:
        shutil.rmtree(directory)


@pytest.mark.parametrize('error_type', [BrokenPipeError, ConnectionResetError, OSError])
def test_rejected_download_response_failure_cannot_finish_the_accepted_stream(monkeypatch, error_type) -> None:
    manager, created = _browser_job()
    job_id = created['job_id']
    directory = manager.get_snapshot_path(job_id).parent
    data = _test_png()
    class DisconnectedHandler(_ShareGetHandlerStub):
        def _write_json(self, status: int, payload: dict) -> None:
            assert status == 409
            assert payload == {'error': 'share_export_download_attempt_invalid'}
            raise error_type('rejected client disconnected')
    _patch_share_dependencies(monkeypatch, manager)
    output = io.BytesIO()
    handler = DisconnectedHandler(command='GET', output=output)
    try:
        manager.store_browser_result(job_id, created['client_token'], 'share.png', io.BytesIO(data), len(data))
        attempt_id = _claim_browser_download(manager, created)
        assert manager.mark_browser_download_started(job_id, attempt_id) is True
        handler._handle_share_api_get(urlparse(f'/share-api/jobs/{job_id}/download?download_attempt_id={attempt_id}'))
        assert output.getvalue() == b''
        assert manager.get_status(job_id)['phase'] == 'download_started'
        manager.mark_browser_download_finished(job_id, succeeded=True, attempt_id=attempt_id)
        assert manager.get_status(job_id)['phase'] == 'download_sent'
    finally:
        shutil.rmtree(directory)
