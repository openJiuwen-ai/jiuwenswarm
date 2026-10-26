# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Media attachment normalizer: base64 → E2A chunk upload → path enrichment."""

from __future__ import annotations

import base64
import binascii
import logging
import os
from contextlib import suppress
from pathlib import Path
from typing import Any

from jiuwenswarm.common.attachment_safety import (
    ensure_image_upload_filename,
    image_suffix_for_mime,
    safe_session_dirname,
    safe_upload_filename,
)

logger = logging.getLogger(__name__)

_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_IMAGE_COUNT = 8


class _KeepOriginalItem:
    """E2A 不可用或上传失败的标记：调用方保留原 base64 项交下游处理。"""


_KEEP_ORIGINAL_ITEM = _KeepOriginalItem()


async def normalize_chat_media_attachments(
    params: dict[str, Any],
    session_id: str | None,
    *,
    agent_client: Any = None,
    user_id: str | None = None,
) -> None:
    """Validate browser media_items, persist images, and enrich the chat params.

    The frontend sends images as base64 for cross-platform browser compatibility.
    Images are persisted under the current session directory and returned as
    structured image file records. Downstream multimodal rails can load images
    from these paths without sending long base64 payloads through normal text
    context.

    A1g：落盘统一经 E2A 分块上传到 AgentServer（``agent/sessions/<sid>/uploads``，
    与 server 侧 ``_store_image_item`` 同路径）——gateway 不再本地写盘。
    ``agent_client`` 为 None（测试/无上下文）或 E2A 上传失败时保留原 base64
    项交下游处理（与大图路径 ``_pre_persist_large_media`` 同语义：消息在帧限
    内则由 server 侧 normalize 落盘，超限则下游返回可重试错误）。
    """

    raw_items = params.get("media_items")
    if not isinstance(raw_items, list) or not raw_items:
        return

    stored: list[dict[str, Any]] = []
    uploaded: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items[:_MAX_IMAGE_COUNT]):
        if not isinstance(item, dict):
            continue
        if item.get("type") != "image":
            continue
        stored_item = await _store_image_item(
            item,
            session_id=session_id,
            index=index,
            agent_client=agent_client,
            user_id=user_id,
        )
        if stored_item is _KEEP_ORIGINAL_ITEM:
            stored.append(item)
        elif stored_item:
            stored.append(stored_item)
            uploaded.append(stored_item)

    if not stored:
        params.pop("media_items", None)
        return

    params["media_items"] = stored
    if not uploaded:
        return
    files = params.get("files")
    if not isinstance(files, dict):
        files = {}
    files["uploaded_images"] = [
        {
            "filename": item.get("filename"),
            "path": item.get("path"),
            "mime_type": item.get("mime_type"),
            "size_bytes": item.get("size_bytes"),
        }
        for item in uploaded
    ]
    params["files"] = files


async def _store_image_item(
    item: dict[str, Any],
    *,
    session_id: str | None,
    index: int,
    agent_client: Any = None,
    user_id: str | None = None,
) -> dict[str, Any] | _KeepOriginalItem | None:
    mime_type = str(item.get("mimeType") or item.get("mime_type") or "").lower().strip()
    suffix = image_suffix_for_mime(mime_type)
    if suffix is None:
        return None

    # Gateway 侧已经通过受认证 HTTP bridge 落盘到注入目录的大图（Phase 2 传输
    # 取舍：超内部 WS 帧限制的 base64 不压 E2A 链路）：直接透传落盘记录，不重复
    # 解码/写盘。路径必须存在，否则视为无效项丢弃。
    if item.get("_persisted"):
        path = item.get("path")
        if isinstance(path, str) and path.strip():
            try:
                exists = os.path.isfile(path)
                size = os.path.getsize(path) if exists else 0
            except OSError:
                exists = False
                size = 0
            if exists:
                return {
                    "type": "image",
                    "filename": Path(path).name,
                    "mime_type": mime_type,
                    "path": path,
                    "size_bytes": size,
                }
        return None

    raw_base64 = item.get("base64Data") or item.get("base64_data")
    if not isinstance(raw_base64, str) or not raw_base64.strip():
        return None
    data: bytes | None = None
    with suppress(binascii.Error):
        # 剥离 data:...;base64, 前缀（react-dropzone/readAsDataURL 等会带）。
        payload = raw_base64.split(",", 1)[-1] if raw_base64.startswith("data:") else raw_base64
        data = base64.b64decode(payload, validate=True)
    if not data or len(data) > _MAX_IMAGE_BYTES:
        return None

    if agent_client is None:
        # 测试/无 AgentServer 上下文：保留原 base64 项（由调用方透传）
        return _KEEP_ORIGINAL_ITEM

    safe_session_id = safe_session_dirname(session_id)
    filename = ensure_image_upload_filename(
        safe_upload_filename(
            str(item.get("filename") or f"image-{index + 1}{suffix}"),
            fallback=f"image-{index + 1}{suffix}",
        ),
        suffix,
    )
    rel_path = f"agent/sessions/{safe_session_id}/uploads/{filename}"

    from gateway.routing.agent_http_bridge import upload_file_bytes_via_e2a

    ok, payload = await upload_file_bytes_via_e2a(
        data,
        rel_path,
        agent_client=agent_client,
        user_id=user_id,
        channel_id="web",
        session_id=session_id,
    )
    if not ok:
        # 与大图路径（``_pre_persist_large_media``）同语义：上传失败保留原
        # base64 项交下游处理，不静默丢弃用户图片。
        logger.warning("[media.normalize] image upload failed: %s", payload.get("error"))
        return _KEEP_ORIGINAL_ITEM

    return {
        "type": "image",
        "filename": filename,
        "mime_type": mime_type,
        "path": str(payload.get("path") or rel_path),
        "size_bytes": len(data),
    }
