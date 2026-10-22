# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Isolated browser renderer and in-process job registry for share-image export."""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import secrets
import shutil
import struct
import sys
import tempfile
import threading
import time
import uuid
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from jiuwenswarm.common.config import get_config, resolve_env_vars


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"
_PNG_OUTPUT_WIDTH = 2_250
_MAX_PNG_OUTPUT_HEIGHT = 128_000
_JOB_TTL_SECONDS = 60 * 60
# page.goto() 200 不代表 React runner 已挂载; 状态窗口必须在该超时内出现,
# 否则 bundle 加载失败/未挂载/入口分支未命中等静默失败会一直空转。
_RENDER_INIT_TIMEOUT_SECONDS = 15.0
_RENDER_POLL_INTERVAL_SECONDS = 0.25
_PAGE_ERROR_MAX_CHARS = 200


@dataclass(frozen=True)
class ShareImageRenderAuth:
    """Desktop credentials injected into the headless export browser context.

    ``cookie_value`` carries the desktop access token; it must never reach job
    state, snapshot files, error strings, or logs, so its repr is suppressed.
    """

    cookie_name: str
    cookie_value: str = field(repr=False)


def _sanitize_page_error(message: str) -> str:
    """Keep renderer page errors single-line and bounded for job state."""
    cleaned = "".join(char if char.isprintable() else " " for char in message).strip()
    if len(cleaned) > _PAGE_ERROR_MAX_CHARS:
        cleaned = f"{cleaned[:_PAGE_ERROR_MAX_CHARS]}..."
    return cleaned


_ZIP_PART_NAME_PATTERN = re.compile(
    r"^(?P<stem>[A-Za-z0-9._-]+)-part-(?P<part>[0-9]+)-of-(?P<total>[0-9]+)\.png$",
    re.IGNORECASE,
)


def _configured_browser() -> tuple[str | None, str]:
    config = resolve_env_vars(get_config())
    browser = config.get("browser", {}) if isinstance(config, dict) else {}
    if not isinstance(browser, dict):
        browser = {}

    configured_path = browser.get("chrome_path", "")
    if isinstance(configured_path, dict):
        platform_keys = {
            "win32": "windows",
            "cygwin": "windows",
            "darwin": "macos",
            "linux": "linux",
            "linux2": "linux",
        }
        resolved_path = ""
        for key in (platform_keys.get(sys.platform, "default"), "default"):
            value = configured_path.get(key, "")
            if isinstance(value, str) and value.strip():
                resolved_path = value.strip()
                break
        configured_path = resolved_path
    executable_path = configured_path.strip() if isinstance(configured_path, str) else ""
    if executable_path and not Path(executable_path).is_file():
        raise RuntimeError("configured_share_export_browser_not_found")

    raw_browser_type = browser.get("browser_type", "auto")
    browser_type = raw_browser_type.strip().lower() if isinstance(raw_browser_type, str) else "auto"
    channel = "msedge" if browser_type in {"msedge", "edge", "microsoft-edge", "microsoft_edge"} else "chrome"
    return (executable_path or None), channel


def available_share_browser() -> dict[str, Any] | None:
    """Select an installed executable without installing or downloading browsers."""
    try:
        executable, channel = _configured_browser()
    except RuntimeError as exc:
        if str(exc) == "configured_share_export_browser_not_found":
            return None
        raise
    if executable:
        return {"headless": True, "executable_path": executable} if os.access(executable, os.X_OK) else None
    config = resolve_env_vars(get_config())
    browser_config = config.get("browser", {}) if isinstance(config, dict) else {}
    browser_type = browser_config.get("browser_type", "auto") if isinstance(browser_config, dict) else "auto"
    automatic = not isinstance(browser_type, str) or browser_type.strip().lower() == "auto"
    channels = ("chrome", "msedge") if automatic else (channel,)
    candidates: list[str] = []
    for selected in channels:
        if sys.platform == "darwin":
            name = "Google Chrome" if selected == "chrome" else "Microsoft Edge"
            candidates.append(f"/Applications/{name}.app/Contents/MacOS/{name}")
        elif sys.platform in {"win32", "cygwin"}:
            relative = (
                "Google/Chrome/Application/chrome.exe"
                if selected == "chrome" else "Microsoft/Edge/Application/msedge.exe"
            )
            for key in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
                root = os.environ.get(key)
                if root:
                    candidates.append(str(Path(root) / relative))
        else:
            names = (
                ("google-chrome", "google-chrome-stable")
                if selected == "chrome" else ("microsoft-edge", "microsoft-edge-stable", "msedge")
            )
            for name in names:
                found = shutil.which(name)
                if found:
                    candidates.append(found)
            candidates.append("/opt/google/chrome/chrome" if selected == "chrome" else "/opt/microsoft/msedge/msedge")
    if automatic:
        for name in ("chromium", "chromium-browser"):
            found = shutil.which(name)
            if found:
                candidates.append(found)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            candidates.append(playwright.chromium.executable_path)
    for candidate in candidates:
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return {"headless": True, "executable_path": candidate}
    return None


def _validate_png_stream(stream, size: int) -> None:
    if size < len(_PNG_SIGNATURE) + len(_PNG_IEND):
        raise RuntimeError("share_export_png_incomplete")
    if stream.read(len(_PNG_SIGNATURE)) != _PNG_SIGNATURE:
        raise RuntimeError("share_export_png_invalid_signature")
    consumed = len(_PNG_SIGNATURE)
    seen_ihdr = False
    seen_idat = False
    seen_iend = False
    while consumed < size:
        chunk_header = stream.read(8)
        if len(chunk_header) != 8:
            raise RuntimeError("share_export_png_incomplete")
        chunk_length, chunk_type = struct.unpack(">I4s", chunk_header)
        consumed += 8
        if chunk_length > size - consumed - 4:
            raise RuntimeError("share_export_png_incomplete")
        if not seen_ihdr and chunk_type != b"IHDR":
            raise RuntimeError("share_export_png_invalid_header")

        crc = zlib.crc32(chunk_type)
        remaining = chunk_length
        ihdr_data = bytearray()
        while remaining:
            data = stream.read(min(remaining, 1024 * 1024))
            if not data:
                raise RuntimeError("share_export_png_incomplete")
            if chunk_type == b"IHDR":
                ihdr_data.extend(data)
            crc = zlib.crc32(data, crc)
            consumed += len(data)
            remaining -= len(data)
        expected_crc = stream.read(4)
        if len(expected_crc) != 4:
            raise RuntimeError("share_export_png_incomplete")
        consumed += 4
        if struct.unpack(">I", expected_crc)[0] != crc & 0xFFFF_FFFF:
            raise RuntimeError("share_export_png_crc_failed")

        if chunk_type == b"IHDR":
            if seen_ihdr or chunk_length != 13:
                raise RuntimeError("share_export_png_invalid_header")
            width, height = struct.unpack(">II", ihdr_data[:8])
            if width != _PNG_OUTPUT_WIDTH or height <= 0:
                raise RuntimeError("share_export_png_invalid_dimensions")
            if height > _MAX_PNG_OUTPUT_HEIGHT:
                raise RuntimeError("share_export_png_height_unsupported")
            seen_ihdr = True
        elif chunk_type == b"IDAT":
            seen_idat = True
        elif chunk_type == b"IEND":
            if chunk_length != 0:
                raise RuntimeError("share_export_png_invalid_iend")
            seen_iend = True
            break

    if not seen_ihdr or not seen_idat or not seen_iend:
        raise RuntimeError("share_export_png_incomplete")
    if consumed != size or stream.read(1):
        raise RuntimeError("share_export_png_trailing_data")


def _validate_png(path: Path) -> None:
    with path.open("rb") as stream:
        _validate_png_stream(stream, path.stat().st_size)


def _validate_zip(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            files = [info for info in archive.infolist() if not info.is_dir()]
            if len(files) < 2:
                raise RuntimeError("share_export_zip_parts_missing")
            expected_width = max(2, len(str(len(files))))
            expected_stem: str | None = None
            for expected_part, info in enumerate(files, start=1):
                match = _ZIP_PART_NAME_PATTERN.fullmatch(info.filename)
                if match is None:
                    raise RuntimeError("share_export_zip_part_invalid")
                stem = match.group("stem")
                part_text = match.group("part")
                total_text = match.group("total")
                if expected_stem not in {None, stem}:
                    raise RuntimeError("share_export_zip_part_invalid")
                if len(part_text) != expected_width or len(total_text) != expected_width:
                    raise RuntimeError("share_export_zip_part_invalid")
                if int(part_text) != expected_part or int(total_text) != len(files):
                    raise RuntimeError("share_export_zip_part_invalid")
                expected_stem = stem
                with archive.open(info) as stream:
                    _validate_png_stream(stream, info.file_size)
    except zipfile.BadZipFile as exc:
        raise RuntimeError("share_export_zip_invalid") from exc


def render_share_image(
    *,
    base_url: str,
    job_id: str,
    output_path: Path,
    on_phase: Callable[[str], None] | None = None,
    render_auth: ShareImageRenderAuth | None = None,
    init_timeout_seconds: float = _RENDER_INIT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = _RENDER_POLL_INTERVAL_SECONDS,
    browser_options: dict[str, Any] | None = None,
) -> Path:
    """Render one immutable snapshot in a dedicated headless browser process."""

    from playwright.sync_api import sync_playwright

    def report(phase: str) -> None:
        if on_phase is not None:
            on_phase(phase)

    def raise_page_error() -> None:
        if page_errors:
            raise RuntimeError(f"share_export_page_error: {_sanitize_page_error(page_errors[-1])}")

    executable_path, channel = _configured_browser()
    launch_options: dict[str, Any] = {"headless": True}
    if executable_path:
        launch_options["executable_path"] = executable_path
    else:
        launch_options["channel"] = channel
    if browser_options is not None:
        launch_options = browser_options

    temporary_output: Path | None = None
    result_path: Path | None = None
    report("launching")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_options)
        try:
            context = browser.new_context(
                accept_downloads=True,
                viewport={"width": 900, "height": 900},
                device_scale_factor=1,
            )
            try:
                # 桌面模式: 无头 BrowserContext 不共享桌面 WebView 的 Cookie,
                # 必须把当前实例的 desktop cookie 注入同一 loopback origin,
                # 否则 /share-export-runner 直接 403。普通 web 模式不注入。
                if render_auth is not None and render_auth.cookie_name and render_auth.cookie_value:
                    context.add_cookies([{
                        "name": render_auth.cookie_name,
                        "value": render_auth.cookie_value,
                        "url": base_url,
                        "httpOnly": True,
                        "sameSite": "Lax",
                    }])
                page = context.new_page()
                page_errors: list[str] = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                runner_url = f"{base_url.rstrip('/')}/share-export-runner?job_id={quote(job_id, safe='')}"
                response = page.goto(runner_url, wait_until="domcontentloaded", timeout=60_000)
                if response is None:
                    raise RuntimeError("share_export_runner_response_missing")
                if response.status >= 400:
                    raise RuntimeError(f"share_export_runner_http_{response.status}")

                def evaluate_state() -> dict[str, Any] | None:
                    value = page.evaluate("() => window.__SHARE_IMAGE_EXPORT_STATE || null")
                    return value if isinstance(value, dict) else None

                state = evaluate_state()
                init_deadline = time.monotonic() + init_timeout_seconds
                while state is None:
                    raise_page_error()
                    if time.monotonic() >= init_deadline:
                        raise RuntimeError("share_export_runner_not_initialized")
                    page.wait_for_timeout(int(poll_interval_seconds * 1000))
                    state = evaluate_state()

                if state.get("status") == "error":
                    message = state.get("error")
                    raise RuntimeError(
                        message if isinstance(message, str) and message else "share_export_render_failed"
                    )

                # Large snapshots can take arbitrarily long to generate.
                while state.get("status") != "ready":
                    raise_page_error()
                    if state.get("status") == "error":
                        message = state.get("error")
                        raise RuntimeError(
                            message if isinstance(message, str) and message else "share_export_render_failed"
                        )
                    status = state.get("status")
                    if isinstance(status, str) and status:
                        report(status)
                    if on_phase is not None and isinstance(state.get("progress"), int):
                        on_phase(f"rendering:{state['progress']}")
                    page.wait_for_timeout(int(poll_interval_seconds * 1000))
                    state = evaluate_state() or state
                raise_page_error()

                raw_filename = state.get("filename")
                if not isinstance(raw_filename, str) or not raw_filename.strip():
                    raise RuntimeError("share_export_result_filename_missing")
                candidate = raw_filename.strip()
                filename = Path(candidate).name
                if (
                    candidate != filename
                    or not re.fullmatch(r"[A-Za-z0-9._-]+\.(?:png|zip)", filename, re.IGNORECASE)
                ):
                    raise RuntimeError("share_export_result_filename_invalid")
                suffix = Path(filename).suffix.lower()
                if suffix not in {".png", ".zip"}:
                    raise RuntimeError("share_export_result_type_unsupported")
                result_path = output_path.parent / filename
                temporary_output = result_path.with_suffix(f"{result_path.suffix}.partial")

                report("saving")
                with page.expect_download(timeout=60_000) as download_info:
                    page.evaluate("() => window.__DOWNLOAD_SHARE_IMAGE__()")
                download = download_info.value
                download.save_as(temporary_output)
                failure = download.failure()
                if failure:
                    raise RuntimeError(f"share_export_download_failed: {failure}")
            finally:
                context.close()
        finally:
            browser.close()

    if temporary_output is None or result_path is None:
        raise RuntimeError("share_export_result_missing")
    if result_path.suffix.lower() == ".png":
        _validate_png(temporary_output)
    else:
        _validate_zip(temporary_output)
    os.replace(temporary_output, result_path)
    return result_path


@dataclass
class _ShareImageJob:
    job_id: str
    session_id: str
    filename: str
    locale: str
    created_at: float
    updated_at: float
    directory: Path
    snapshot_path: Path
    result_path: Path
    state: str = "queued"
    phase: str = "snapshot_ready"
    error: str | None = None
    renderer: str = "server"
    client_token: str = field(default_factory=lambda: secrets.token_hex(32), repr=False)
    browser_claim_id: str | None = None
    progress: int | None = None
    download_claimed_at: float | None = None
    download_attempt_id: str | None = None
    upload_in_progress: bool = False


class ShareImageExportManager:
    """Own immutable snapshots, browser jobs, and their downloadable results."""

    def __init__(
        self,
        renderer: Callable[..., Path] = render_share_image,
        *,
        job_ttl_seconds: int = _JOB_TTL_SECONDS,
    ) -> None:
        self._renderer = renderer
        self._job_ttl_seconds = job_ttl_seconds
        self._jobs: dict[str, _ShareImageJob] = {}
        self._active_job_ids_by_session: dict[str, str] = {}
        self._lock = threading.Lock()

    def create_job(
        self,
        *,
        session_id: str,
        snapshot: dict[str, Any],
        filename: str,
        locale: str,
        base_url: str,
        render_auth: ShareImageRenderAuth | None = None,
        renderer: str = "server",
        browser_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._cleanup_expired()
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            raise ValueError("missing_session_id")

        with self._lock:
            active_job = self._get_active_job_locked(normalized_session_id)
            if active_job is not None:
                return {**self._status(active_job), "reused": True, "client_token": active_job.client_token}

        job_id = uuid.uuid4().hex
        safe_filename = Path(filename).name
        if safe_filename in {"", ".", ".."}:
            safe_filename = "jiuwenswarm-share.png"
        directory = Path(tempfile.mkdtemp(prefix=f"jiuwenswarm-share-{job_id}-"))
        snapshot_path = directory / "snapshot.json"
        result_path = directory / safe_filename
        snapshot_path.write_text(
            json.dumps(
                {"filename": safe_filename, "locale": locale, "snapshot": snapshot},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        now = time.time()
        job = _ShareImageJob(
            job_id=job_id,
            session_id=normalized_session_id,
            filename=safe_filename,
            locale=locale,
            created_at=now,
            updated_at=now,
            directory=directory,
            snapshot_path=snapshot_path,
            result_path=result_path,
            renderer=renderer,
        )
        with self._lock:
            active_job = self._get_active_job_locked(normalized_session_id)
            if active_job is not None:
                shutil.rmtree(directory, ignore_errors=True)
                return {**self._status(active_job), "reused": True, "client_token": active_job.client_token}
            self._jobs[job_id] = job
            self._active_job_ids_by_session[normalized_session_id] = job_id
        # 认证信息只沿线程参数传入 _run_job, 不写入 _ShareImageJob,
        # 因此不会进入状态响应 / snapshot / TTL 清理等任何持久化路径。
        if renderer == "browser":
            return {**self._status(job), "reused": False, "client_token": job.client_token}
        threading.Thread(
            target=self._run_job,
            args=(job_id, base_url, render_auth, browser_options),
            name=f"share-image-export-{job_id[:8]}",
            daemon=True,
        ).start()
        return {**(self.get_status(job_id) or {}), "reused": False}

    def _run_job(
        self,
        job_id: str,
        base_url: str,
        render_auth: ShareImageRenderAuth | None = None,
        browser_options: dict[str, Any] | None = None,
    ) -> None:
        self._update_job(job_id, state="running", phase="launching", error=None)
        try:
            job = self._get_job(job_id)
            if job is None:
                return
            options = {"browser_options": browser_options} if browser_options is not None else {}

            def report(phase: str) -> None:
                if phase.startswith("rendering:"):
                    self._update_job(job_id, phase="rendering", progress=int(phase.split(":", 1)[1]))
                else:
                    self._update_job(job_id, phase=phase)

            result_path = Path(self._renderer(
                base_url=base_url,
                job_id=job_id,
                output_path=job.result_path,
                on_phase=report,
                render_auth=render_auth,
                **options,
            ))
            if result_path.parent.resolve() != job.directory.resolve() or not result_path.is_file():
                raise RuntimeError("share_export_result_path_invalid")
            self._update_job(
                job_id,
                result_path=result_path,
                filename=result_path.name,
                state="completed",
                phase="completed",
                error=None,
            )
        except Exception as exc:  # noqa: BLE001
            self._update_job(job_id, state="failed", phase="failed", error=str(exc))

    def _get_job(self, job_id: str) -> _ShareImageJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _get_active_job_locked(self, session_id: str) -> _ShareImageJob | None:
        job_id = self._active_job_ids_by_session.get(session_id)
        job = self._jobs.get(job_id) if job_id is not None else None
        if job is None or job.state not in {"queued", "running"}:
            self._active_job_ids_by_session.pop(session_id, None)
            return None
        return job

    @staticmethod
    def _status(job: _ShareImageJob) -> dict[str, Any]:
        return {
            "job_id": job.job_id,
            "session_id": job.session_id,
            "filename": job.filename,
            "state": job.state,
            "phase": job.phase,
            "error": job.error,
            "renderer": job.renderer,
            "progress": job.progress,
            "locale": job.locale,
        }

    def update_browser_job(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Accept only authenticated client-renderer progress and terminal states."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise FileNotFoundError("job_not_found")
            token = payload.get("client_token")
            if (
                job.renderer != "browser" or not isinstance(token, str)
                or not secrets.compare_digest(job.client_token.encode(), token.encode())
            ):
                raise PermissionError("share_export_client_not_authorized")
            if payload.get("claim_download") is True:
                if job.state != "completed":
                    raise ValueError("share_export_download_not_ready")
                now = time.time()
                if job.phase in {"download_started", "download_sent"}:
                    return {**self._status(job), "download_claimed": False}
                if (
                    job.phase != "download_failed"
                    and job.download_claimed_at is not None
                    and now - job.download_claimed_at < 60
                ):
                    return {**self._status(job), "download_claimed": False}
                job.download_claimed_at = now
                job.download_attempt_id = secrets.token_hex(16)
                if job.phase == "download_failed":
                    job.phase = "completed"
                return {**self._status(job), "download_claimed": True, "download_attempt_id": job.download_attempt_id}
            state = payload.get("state")
            phase = payload.get("phase")
            progress = payload.get("progress")
            if not isinstance(state, str) or not isinstance(phase, str):
                raise ValueError("share_export_client_state_invalid")
            if state not in {"running", "failed"} or phase not in {"loading_snapshot", "rendering", "saving", "failed"}:
                raise ValueError("share_export_client_state_invalid")
            if (state == "failed") != (phase == "failed"):
                raise ValueError("share_export_client_state_invalid")
            if progress is not None and (type(progress) is not int or not 0 <= progress <= 100):
                raise ValueError("share_export_client_progress_invalid")
            for key in ("claim_id", "expected_claim_id"):
                if key in payload and (
                    not isinstance(payload[key], str) or not re.fullmatch(r"[a-f0-9]{32}", payload[key])
                ):
                    raise ValueError("share_export_client_state_invalid")
            # A closing page can cancel before or after its claim is applied,
            # but cannot cancel a different page that won the claim.
            if (
                "expected_claim_id" in payload
                and job.state != "queued"
                and job.browser_claim_id != payload["expected_claim_id"]
            ):
                return self._status(job)
            if "expected_state" in payload:
                if payload["expected_state"] != "queued":
                    raise ValueError("share_export_client_state_invalid")
                if job.state != payload["expected_state"]:
                    return self._status(job)
            if payload.get("claim") is True:
                if state != "running" or phase != "loading_snapshot":
                    raise ValueError("share_export_client_state_invalid")
                if job.state != "queued":
                    raise ValueError("share_export_browser_already_started")
                job.browser_claim_id = payload.get("claim_id")
            if job.state in {"completed", "failed"}:
                return self._status(job)
            job.state, job.phase, job.progress = state, phase, progress
            job.error = (
                _sanitize_page_error(str(payload.get("error", "share_export_browser_failed")))
                if state == "failed" else None
            )
            job.updated_at = time.time()
            return self._status(job)

    def store_browser_result(
        self,
        job_id: str,
        token: str,
        filename: str,
        stream: io.BufferedIOBase,
        size: int,
    ) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise FileNotFoundError("job_not_found")
            if job.renderer != "browser" or not secrets.compare_digest(job.client_token.encode(), token.encode()):
                raise PermissionError("share_export_client_not_authorized")
            if not re.fullmatch(r"[A-Za-z0-9._-]+\.(?:png|zip)", filename, re.IGNORECASE) or not 0 < size <= 1024**3:
                raise ValueError("share_export_result_invalid")
            if job.state in {"completed", "failed"}:
                raise ValueError("share_export_job_already_finished")
            if job.upload_in_progress:
                raise ValueError("share_export_upload_in_progress")
            job.upload_in_progress = True
            job.updated_at = time.time()
        result = job.directory / filename
        temporary = job.directory / f"{uuid.uuid4().hex}.partial"
        try:
            with temporary.open("wb") as output:
                remaining = size
                while remaining:
                    # read1 returns available bytes instead of waiting to fill
                    # a 1 MiB buffer. The HTTP handler bounds socket idle time.
                    chunk = stream.read1(min(remaining, 1024 * 1024))
                    if not chunk:
                        raise ValueError("share_export_upload_incomplete")
                    with self._lock:
                        if job.state in {"completed", "failed"}:
                            raise ValueError("share_export_job_already_finished")
                        job.updated_at = time.time()
                    output.write(chunk)
                    remaining -= len(chunk)
            (_validate_png if result.suffix.lower() == ".png" else _validate_zip)(temporary)
            with self._lock:
                if job.state in {"completed", "failed"}:
                    raise ValueError("share_export_job_already_finished")
                os.replace(temporary, result)
                job.result_path, job.filename = result, filename
                job.state, job.phase, job.progress = "completed", "completed", 100
                job.updated_at = time.time()
                return self._status(job)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            finally:
                # Release ownership only after all file handles have closed.
                # A failed request must unlock the session without a client beacon.
                with self._lock:
                    if job.state not in {"completed", "failed"}:
                        job.state, job.phase, job.error = "failed", "failed", "share_export_upload_failed"
                    job.upload_in_progress = False
                    job.updated_at = time.time()

    def mark_browser_download_started(self, job_id: str, attempt_id: str | None) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state != "completed":
                return False
            if job.renderer != "browser":
                return True
            if (
                not isinstance(attempt_id, str) or job.download_attempt_id is None
                or not secrets.compare_digest(job.download_attempt_id.encode(), attempt_id.encode())
            ):
                return False
            if job.phase in {"download_started", "download_sent"}:
                return False
            job.phase = "download_started"
            job.updated_at = time.time()
            return True

    def mark_browser_download_finished(self, job_id: str, succeeded: bool, attempt_id: str | None) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.renderer != "browser" or job.state != "completed":
                return
            if job.phase != "download_started" or not isinstance(attempt_id, str) or job.download_attempt_id is None:
                return
            if not secrets.compare_digest(job.download_attempt_id.encode(), attempt_id.encode()):
                return
            job.phase = "download_sent" if succeeded else "download_failed"
            if not succeeded:
                job.download_attempt_id = None
            job.updated_at = time.time()

    def _update_job(self, job_id: str, **updates: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for key, value in updates.items():
                setattr(job, key, value)
            job.updated_at = time.time()

    def get_status(self, job_id: str, *, include_client_token: bool = False) -> dict[str, Any] | None:
        self._cleanup_expired()
        job = self._get_job(job_id)
        if job is None:
            return None
        status = self._status(job)
        if include_client_token and job.renderer == "browser":
            status["client_token"] = job.client_token
        return status

    def get_active_status(self, session_id: str, *, include_client_token: bool = False) -> dict[str, Any] | None:
        self._cleanup_expired()
        with self._lock:
            job = self._get_active_job_locked(session_id.strip())
            if job is None:
                return None
            status = self._status(job)
            if include_client_token and job.renderer == "browser":
                status["client_token"] = job.client_token
            return status

    def get_snapshot_path(self, job_id: str) -> Path | None:
        self._cleanup_expired()
        job = self._get_job(job_id)
        return job.snapshot_path if job is not None and job.snapshot_path.is_file() else None

    def get_result(self, job_id: str) -> tuple[Path, str] | None:
        self._cleanup_expired()
        job = self._get_job(job_id)
        if job is None or job.state != "completed" or not job.result_path.is_file():
            return None
        return job.result_path, job.filename

    def _cleanup_expired(self) -> None:
        cutoff = time.time() - self._job_ttl_seconds
        expired: list[_ShareImageJob] = []
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                # Browser renderers report heartbeats; server renderers own
                # their lifecycle. Uploads own their files through validation
                # and release them after completion or a socket idle timeout.
                if job.upload_in_progress or job.phase == "download_started":
                    continue
                if job.state == "running" and job.renderer != "browser":
                    continue
                if job.updated_at < cutoff:
                    expired_job = self._jobs.pop(job_id)
                    if self._active_job_ids_by_session.get(expired_job.session_id) == job_id:
                        self._active_job_ids_by_session.pop(expired_job.session_id, None)
                    expired.append(expired_job)
        for job in expired:
            shutil.rmtree(job.directory, ignore_errors=True)


class _CliEventHandler(logging.StreamHandler):
    """Propagate protocol output failures instead of dropping events."""

    def handleError(self, record: logging.LogRecord) -> None:
        raise sys.exception()


def _write_cli_event(event: dict[str, str]) -> None:
    """Write one flushed JSONL protocol event for the Vite export manager."""
    handler = _CliEventHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord(
        name=f"{__name__}.cli",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg=json.dumps(event),
        args=(),
        exc_info=None,
    )
    try:
        handler.handle(record)
    finally:
        handler.close()


def _run_cli() -> int:
    parser = argparse.ArgumentParser(description="Render a WorkSwarm share image in an isolated browser")
    parser.add_argument("--base-url")
    parser.add_argument("--job-id")
    parser.add_argument("--output")
    parser.add_argument("--validate-result")
    parser.add_argument("--allow-browser-render", action="store_true")
    args = parser.parse_args()
    if args.validate_result:
        result = Path(args.validate_result)
        (_validate_png if result.suffix.lower() == ".png" else _validate_zip)(result)
        _write_cli_event({"phase": "completed", "filename": result.name})
        return 0
    if not args.base_url or not args.job_id or not args.output:
        parser.error("--base-url, --job-id and --output are required for rendering")

    def report(phase: str) -> None:
        _write_cli_event({"phase": phase})

    try:
        browser_options = available_share_browser() if args.allow_browser_render else None
        if args.allow_browser_render and browser_options is None:
            _write_cli_event({"renderer": "browser", "phase": "snapshot_ready"})
            return 0
        if args.allow_browser_render:
            _write_cli_event({"renderer": "server", "phase": "launching"})
        options = {"browser_options": browser_options} if browser_options is not None else {}
        result_path = render_share_image(
            base_url=args.base_url,
            job_id=args.job_id,
            output_path=Path(args.output),
            on_phase=report,
            **options,
        )
        _write_cli_event({"phase": "completed", "filename": result_path.name})
    except Exception as exc:  # noqa: BLE001
        _write_cli_event({"error": str(exc)})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_cli())
