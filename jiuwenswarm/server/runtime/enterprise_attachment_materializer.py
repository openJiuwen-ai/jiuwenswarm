# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""企业版附件预落盘：将 MinIO/HTTP URL 下载到 session 工作区."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.gateway.channel_manager.web.file_http import safe_filename

logger = logging.getLogger(__name__)

_UPLOADS_SUBDIR = "uploads"
_DEFAULT_HTTP_TIMEOUT = 120
_DEFAULT_MAX_FILE_SIZE = 100 * 1024 * 1024


def enterprise_files_need_download(files: dict | list | None) -> bool:
    """是否仍有需 Agent 自行下载的 URL 附件（无有效本地 path）。"""
    if not files:
        return False
    if isinstance(files, list):
        items = files
    elif isinstance(files, dict):
        items = list(files.values())
    else:
        return False

    for item in items:
        if isinstance(item, list):
            if enterprise_files_need_download(item):
                return True
            continue
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("uri") or "").strip()
        if not url or not url.startswith(("http://", "https://")):
            continue
        path = str(item.get("path") or "").strip()
        if path and Path(path).is_file():
            continue
        return True
    return False


def _coerce_file_list(files: list[dict[str, Any]] | dict[str, Any] | None) -> list[Any]:
    if isinstance(files, list):
        return list(files)
    if not isinstance(files, dict):
        return []
    items: list[Any] = []
    for value in files.values():
        if isinstance(value, list):
            items.extend(value)
        elif isinstance(value, dict):
            items.append(value)
    return items


def _resolve_download_filename(file_info: dict[str, Any]) -> str:
    name = (
        str(file_info.get("name") or file_info.get("filename") or "").strip()
        or Path(urlparse(str(file_info.get("url") or file_info.get("uri") or "")).path).name
        or "attachment"
    )
    return safe_filename(name, default="attachment")


def _unique_dest_path(uploads_dir: Path, filename: str, url: str) -> Path:
    base = uploads_dir / filename
    if not base.exists():
        return base
    stem = Path(filename).stem or "attachment"
    suffix = Path(filename).suffix
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    return uploads_dir / f"{stem}_{digest}{suffix}"


def _requests_verify() -> bool:
    raw = os.environ.get("JIUWENCLAW_SSL_VERIFY", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


def _resolve_max_file_size() -> int:
    raw = os.environ.get("JIUWENSWARM_ENTERPRISE_ATTACHMENT_MAX_BYTES", "").strip()
    if raw.isdigit():
        return max(int(raw), 0)
    try:
        from jiuwenswarm.common.config import get_config

        ft = get_config().get("file_transfer") or {}
        if isinstance(ft, dict) and ft.get("max_file_size") is not None:
            return max(int(ft.get("max_file_size") or 0), 0)
    except Exception:  # noqa: BLE001
        logger.debug("[EnterpriseAttachment] load max_file_size from config failed", exc_info=True)
    return _DEFAULT_MAX_FILE_SIZE


def _resolve_transfer_timeout() -> int:
    try:
        from jiuwenswarm.common.config import get_config

        ft = get_config().get("file_transfer") or {}
        if isinstance(ft, dict) and ft.get("transfer_timeout") is not None:
            return max(int(ft.get("transfer_timeout") or 0), _DEFAULT_HTTP_TIMEOUT)
    except Exception:  # noqa: BLE001
        logger.debug("[EnterpriseAttachment] load transfer_timeout from config failed", exc_info=True)
    return _DEFAULT_HTTP_TIMEOUT


def _download_http_to_path(
    url: str,
    dest: Path,
    *,
    max_file_size: int,
    timeout: int,
) -> None:
    from jiuwenswarm.gateway.message_handler.outbound_file_materialize import (
        assert_minio_url_allowed,
    )

    assert_minio_url_allowed(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(
        url,
        stream=True,
        timeout=timeout,
        verify=_requests_verify(),
        allow_redirects=False,
    )
    if resp.status_code in (301, 302, 303, 307, 308):
        resp.close()
        raise ValueError(f"attachment download redirects are not allowed: {url}")
    resp.raise_for_status()
    total = 0
    with dest.open("wb") as handle:
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if max_file_size > 0 and total > max_file_size:
                handle.close()
                dest.unlink(missing_ok=True)
                raise ValueError(
                    f"attachment exceeds max size ({max_file_size} bytes): {url}"
                )
            handle.write(chunk)


async def materialize_url_attachments(
    files: list[dict[str, Any]] | dict[str, Any],
    workspace_dir: str,
    *,
    request_id: str = "",
) -> list[dict[str, Any]] | dict[str, Any]:
    """将带 URL 且无本地 path 的附件下载到 session 工作区 uploads 目录."""
    if not is_enterprise():
        return files

    original_is_list = isinstance(files, list)
    items = _coerce_file_list(files if isinstance(files, (list, dict)) else None)
    if not items:
        return files

    uploads_dir = Path(workspace_dir).resolve() / _UPLOADS_SUBDIR
    timeout = _resolve_transfer_timeout()
    max_file_size = _resolve_max_file_size()

    materialized: list[Any] = []
    changed = False

    for file_info in items:
        if not isinstance(file_info, dict):
            materialized.append(file_info)
            continue

        updated = dict(file_info)
        url = str(updated.get("url") or updated.get("uri") or "").strip()
        local_path = str(updated.get("path") or "").strip()

        if not url or not url.startswith(("http://", "https://")):
            materialized.append(updated)
            continue

        if local_path and Path(local_path).is_file():
            materialized.append(updated)
            continue

        filename = _resolve_download_filename(updated)
        dest = _unique_dest_path(uploads_dir, filename, url)

        try:
            await asyncio.to_thread(
                _download_http_to_path,
                url,
                dest,
                max_file_size=max_file_size,
                timeout=timeout,
            )
        except Exception as exc:
            logger.warning(
                "[EnterpriseAttachment] 附件预下载失败: request_id=%s url=%s dest=%s error=%s",
                request_id,
                url,
                dest,
                exc,
            )
            materialized.append(updated)
            continue

        updated["path"] = str(dest)
        updated["_materialized"] = True
        materialized.append(updated)
        changed = True
        logger.info(
            "[EnterpriseAttachment] 附件已预下载到工作区: request_id=%s name=%s path=%s bytes=%d",
            request_id,
            filename,
            dest,
            dest.stat().st_size,
        )

    if not changed:
        return files
    if original_is_list:
        return materialized
    # Preserve dict shape used by personal chat.send when values were lists.
    if isinstance(files, dict):
        rebuilt: dict[str, Any] = {}
        cursor = 0
        for key, value in files.items():
            if isinstance(value, list):
                count = len(value)
                rebuilt[key] = materialized[cursor:cursor + count]
                cursor += count
            else:
                rebuilt[key] = value
        return rebuilt
    return materialized
