# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Enterprise file push helpers — land on Gateway disk and sign download tokens.

Used by chunked ``file_transfer`` / ``POST /file-api/push`` (escape hatch).
Enterprise ``send_file`` OBS path does **not** land files here; Gateway
proxies MinIO on ``GET /file-api/download?url=``.
"""

from __future__ import annotations
from jiuwenswarm.edition import is_enterprise

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


async def push_file_to_web_and_get_token(
    file_path: str,
    filename: str,
    session_id: str,
) -> dict[str, Any] | None:
    """Copy a local file into Gateway received dir and return download metadata."""
    if not is_enterprise():
        return None

    try:
        from jiuwenswarm.gateway.channel_manager.web.file_http import save_pushed_file

        with open(file_path, "rb") as handle:
            raw = handle.read()
        result = save_pushed_file(
            file_bytes=raw,
            filename=filename,
            session_id=session_id,
        )
        file_size = os.path.getsize(file_path)
        logger.info(
            "[WebFilePush] Gateway local receive: %s download_url=%s",
            filename,
            result.get("download_url"),
        )
        return {
            "name": filename,
            "size": file_size,
            "mime_type": "application/octet-stream",
            "download_url": result.get("download_url"),
            "download_token": result.get("download_token"),
            "expires_at": result.get("expires_at"),
        }
    except Exception as exc:
        logger.error(
            "[WebFilePush] Gateway local receive failed: %s, error: %s",
            filename,
            exc,
            exc_info=True,
        )
        return None


def resolve_web_server_push_url() -> str:
    """Deprecated: push no longer targets Web Pod."""
    return ""
