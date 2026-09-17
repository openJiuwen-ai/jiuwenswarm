# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Isolated browser renderer and in-process job registry for share-image export."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import struct
import sys
import tempfile
import threading
import time
import uuid
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from jiuwenswarm.common.config import get_config, resolve_env_vars


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"
_PNG_OUTPUT_WIDTH = 2_250
_MAX_PNG_OUTPUT_HEIGHT = 128_000
_JOB_TTL_SECONDS = 60 * 60
_RENDER_IDLE_TIMEOUT_SECONDS = 15 * 60
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
) -> Path:
    """Render one immutable snapshot in a dedicated headless browser process."""

    from playwright.sync_api import sync_playwright

    def report(phase: str) -> None:
        if on_phase is not None:
            on_phase(phase)

    executable_path, channel = _configured_browser()
    launch_options: dict[str, Any] = {"headless": True}
    if executable_path:
        launch_options["executable_path"] = executable_path
    else:
        launch_options["channel"] = channel

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
                page = context.new_page()
                page_errors: list[str] = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                runner_url = f"{base_url.rstrip('/')}/share-export-runner?job_id={quote(job_id, safe='')}"
                page.goto(runner_url, wait_until="domcontentloaded", timeout=60_000)

                idle_deadline = time.monotonic() + _RENDER_IDLE_TIMEOUT_SECONDS
                last_heartbeat: int | float | None = None
                state: dict[str, Any] = {}
                while time.monotonic() < idle_deadline:
                    value = page.evaluate("() => window.__SHARE_IMAGE_EXPORT_STATE || null")
                    state = value if isinstance(value, dict) else {}
                    status = state.get("status")
                    if status in {"ready", "error"}:
                        break
                    heartbeat = state.get("heartbeat")
                    if isinstance(heartbeat, (int, float)) and heartbeat != last_heartbeat:
                        last_heartbeat = heartbeat
                        idle_deadline = time.monotonic() + _RENDER_IDLE_TIMEOUT_SECONDS
                    if isinstance(status, str) and status:
                        report(status)
                    page.wait_for_timeout(250)
                else:
                    raise RuntimeError("share_export_render_stalled")

                if state.get("status") == "error":
                    message = state.get("error")
                    raise RuntimeError(
                        message if isinstance(message, str) and message else "share_export_render_failed"
                    )
                if page_errors:
                    raise RuntimeError(f"share_export_page_error: {page_errors[-1]}")

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
    ) -> dict[str, Any]:
        self._cleanup_expired()
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            raise ValueError("missing_session_id")

        with self._lock:
            active_job = self._get_active_job_locked(normalized_session_id)
            if active_job is not None:
                return {**self._status(active_job), "reused": True}

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
        )
        with self._lock:
            active_job = self._get_active_job_locked(normalized_session_id)
            if active_job is not None:
                shutil.rmtree(directory, ignore_errors=True)
                return {**self._status(active_job), "reused": True}
            self._jobs[job_id] = job
            self._active_job_ids_by_session[normalized_session_id] = job_id
        threading.Thread(
            target=self._run_job,
            args=(job_id, base_url),
            name=f"share-image-export-{job_id[:8]}",
            daemon=True,
        ).start()
        return {**(self.get_status(job_id) or {}), "reused": False}

    def _run_job(self, job_id: str, base_url: str) -> None:
        self._update_job(job_id, state="running", phase="launching", error=None)
        try:
            job = self._get_job(job_id)
            if job is None:
                return
            result_path = Path(self._renderer(
                base_url=base_url,
                job_id=job_id,
                output_path=job.result_path,
                on_phase=lambda phase: self._update_job(job_id, phase=phase),
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
        }

    def _update_job(self, job_id: str, **updates: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for key, value in updates.items():
                setattr(job, key, value)
            job.updated_at = time.time()

    def get_status(self, job_id: str) -> dict[str, Any] | None:
        self._cleanup_expired()
        job = self._get_job(job_id)
        if job is None:
            return None
        return self._status(job)

    def get_active_status(self, session_id: str) -> dict[str, Any] | None:
        self._cleanup_expired()
        with self._lock:
            job = self._get_active_job_locked(session_id.strip())
            return self._status(job) if job is not None else None

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
                if job.updated_at < cutoff and job.state in {"completed", "failed"}:
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
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    def report(phase: str) -> None:
        _write_cli_event({"phase": phase})

    try:
        result_path = render_share_image(
            base_url=args.base_url,
            job_id=args.job_id,
            output_path=Path(args.output),
            on_phase=report,
        )
        _write_cli_event({"phase": "completed", "filename": result_path.name})
    except Exception as exc:  # noqa: BLE001
        _write_cli_event({"error": str(exc)})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_cli())
