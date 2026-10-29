# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A1g：gateway 小图落盘 E2A 化后的 normalize 语义。

覆盖：
- E2A 上传失败或 ``agent_client`` 为 None 时保留原 base64 项（与大图路径
  ``_pre_persist_large_media`` 同语义：不静默丢弃用户图片，交下游处理）；
- 上传成功时 base64 项替换为落盘记录并填充 ``files.uploaded_images``；
- 无效项（不支持的 MIME）仍被丢弃。
"""
from __future__ import annotations

import base64
from typing import Any

import pytest

from gateway.channel_manager.web.media.normalizer import (
    normalize_chat_media_attachments,
)

_PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 8).decode("ascii")


def _params() -> dict[str, Any]:
    return {
        "media_items": [
            {
                "type": "image",
                "mimeType": "image/png",
                "filename": "pic.png",
                "base64Data": _PNG_B64,
            }
        ]
    }


@pytest.mark.asyncio
async def test_upload_failure_keeps_original_base64_item(monkeypatch):
    """E2A 上传失败：原 base64 项保留在 media_items，不进 uploaded_images。"""
    from gateway.routing import agent_http_bridge

    async def _fail(_data, _rel_path, **_kwargs):
        return False, {"error": "AgentServer is unavailable"}

    monkeypatch.setattr(agent_http_bridge, "upload_file_bytes_via_e2a", _fail)
    params = _params()

    await normalize_chat_media_attachments(params, "sess-1", agent_client=object())

    stored = params["media_items"]
    assert len(stored) == 1
    assert stored[0]["base64Data"] == _PNG_B64
    assert "path" not in stored[0]
    assert "uploaded_images" not in params.get("files", {})


@pytest.mark.asyncio
async def test_missing_agent_client_keeps_original_base64_item():
    """agent_client 为 None（测试/无上下文）：同样保留原 base64 项。"""
    params = _params()

    await normalize_chat_media_attachments(params, "sess-1", agent_client=None)

    stored = params["media_items"]
    assert len(stored) == 1
    assert stored[0]["base64Data"] == _PNG_B64
    assert "uploaded_images" not in params.get("files", {})


@pytest.mark.asyncio
async def test_upload_success_replaces_base64_with_path(monkeypatch):
    """上传成功：base64 项替换为落盘记录，并填充 files.uploaded_images。"""
    from gateway.routing import agent_http_bridge

    async def _ok(data, rel_path, **_kwargs):
        return True, {"path": f"/data/{rel_path}"}

    monkeypatch.setattr(agent_http_bridge, "upload_file_bytes_via_e2a", _ok)
    params = _params()

    await normalize_chat_media_attachments(params, "sess-1", agent_client=object())

    stored = params["media_items"]
    assert len(stored) == 1
    assert "base64Data" not in stored[0]
    assert stored[0]["path"] == "/data/agent/sessions/sess-1/uploads/pic.png"
    uploaded = params["files"]["uploaded_images"]
    assert len(uploaded) == 1
    assert uploaded[0]["path"] == "/data/agent/sessions/sess-1/uploads/pic.png"


@pytest.mark.asyncio
async def test_mixed_success_and_failure_only_uploaded_records_in_uploaded_images(monkeypatch):
    """混合场景：uploaded_images 只含真实落盘记录，失败保留的 base64 项不进。

    回归守护：推导式曾误遍历 stored（含 _KEEP_ORIGINAL_ITEM 原始项），
    生成 path/mime_type/size_bytes 全 None 的条目。
    """
    from gateway.routing import agent_http_bridge

    async def _ok_first_only(data, rel_path, **_kwargs):
        if "ok.png" in rel_path:
            return True, {"path": f"/data/{rel_path}"}
        return False, {"error": "AgentServer is unavailable"}

    monkeypatch.setattr(agent_http_bridge, "upload_file_bytes_via_e2a", _ok_first_only)
    params = {
        "media_items": [
            {
                "type": "image",
                "mimeType": "image/png",
                "filename": "ok.png",
                "base64Data": _PNG_B64,
            },
            {
                "type": "image",
                "mimeType": "image/png",
                "filename": "fail.png",
                "base64Data": _PNG_B64,
            },
        ]
    }

    await normalize_chat_media_attachments(params, "sess-1", agent_client=object())

    stored = params["media_items"]
    assert len(stored) == 2
    assert "path" in stored[0] and "base64Data" not in stored[0]
    assert stored[1]["base64Data"] == _PNG_B64
    uploaded = params["files"]["uploaded_images"]
    assert len(uploaded) == 1
    assert uploaded[0]["path"] == "/data/agent/sessions/sess-1/uploads/ok.png"
    assert uploaded[0]["mime_type"] == "image/png"


@pytest.mark.asyncio
async def test_unsupported_mime_item_still_dropped():
    """无效项（不支持的 MIME）仍被丢弃，不因回退语义保留。"""
    params = {
        "media_items": [
            {
                "type": "image",
                "mimeType": "application/octet-stream",
                "filename": "blob.bin",
                "base64Data": _PNG_B64,
            }
        ]
    }

    await normalize_chat_media_attachments(params, "sess-1", agent_client=None)

    assert "media_items" not in params
