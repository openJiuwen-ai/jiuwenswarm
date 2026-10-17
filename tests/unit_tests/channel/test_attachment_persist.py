# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""决策 D6：IM 附件落盘失败（AgentServer 不可达）按可重试错误失败整条消息。

覆盖：
- 三平台 file_service 的 ``_persist_downloaded_file`` 在 persist hook 失败时
  抛出 ``AttachmentPersistError``（不回落本地写盘、不返回 None）。
- 下载入口的 ``except Exception`` 不吞 ``AttachmentPersistError``（穿透到
  connect 层，供整条消息按可重试错误失败处理）。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.gateway.channel_manager.im_platforms.errors import (
    AttachmentPersistError,
)


def _feishu_service(tmp_path) -> object:
    from jiuwenswarm.gateway.channel_manager.im_platforms.feishu.feishu_file_service import (
        FeishuFileService,
    )

    return FeishuFileService(
        api_client=MagicMock(),
        config=SimpleNamespace(download_timeout=60),
        workspace_dir=str(tmp_path),
    )


async def _failing_hook(_content, _category, _filename):
    raise RuntimeError("AgentServer is unavailable")


@pytest.mark.asyncio
async def test_persist_hook_failure_raises_attachment_persist_error(tmp_path):
    """三平台（feishu/dingtalk/wecom）统一：hook 失败抛 AttachmentPersistError，
    不回落本地写盘、不返回 None（决策 D6）。以 feishu 为代表验证共享语义。"""
    service = _feishu_service(tmp_path)
    service.set_persist_hook(_failing_hook)
    with pytest.raises(AttachmentPersistError):
        await service._persist_downloaded_file(b"content", "images", "a.png")


@pytest.mark.asyncio
async def test_feishu_download_image_propagates_persist_error(monkeypatch, tmp_path):
    """下载入口不吞 AttachmentPersistError（穿透到 connect 层整条消息失败）。"""
    service = _feishu_service(tmp_path)
    service.set_persist_hook(_failing_hook)
    service._download_with_retry = AsyncMock(return_value=b"\x89PNG\r\n\x1a\nfake")
    monkeypatch.setattr(
        type(service), "_detect_image_extension", staticmethod(lambda _data: ".png")
    )

    with pytest.raises(AttachmentPersistError):
        await service.download_image("image_key", "message_id")


# ── channel_manager._persist_hook 大小分流（Phase 2 传输取舍） ────────────────


class _FakePersistChannel:
    channel_id = "feishu"

    def __init__(self):
        self.hook = None

    def set_file_persist_hook(self, hook):
        self.hook = hook


class _FakeMessageHandlerWithClient:
    def __init__(self, agent_client):
        self.agent_client = agent_client


@pytest.fixture
def persist_hook():
    """构造一个已注入 IM 附件落盘钩子的通道 hook（三平台 file_service 共用）。"""
    from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager

    channel = _FakePersistChannel()
    manager = ChannelManager(_FakeMessageHandlerWithClient(agent_client=object()))
    manager._try_wire_file_persist_hook(channel)
    assert channel.hook is not None
    return channel.hook


@pytest.mark.asyncio
async def test_persist_hook_large_attachment_uses_http_bridge(monkeypatch, persist_hook):
    """大附件（>4MB）改走受认证 HTTP bridge 上传，不走 base64 E2A
    （避免超过内部 WS 8MB 帧限制击穿 Gateway↔AgentServer 连接）。"""
    from jiuwenswarm.gateway.routing.agent_http_bridge import E2A_PAYLOAD_MAX_BYTES

    uploads = []

    def fake_upload(content, rel_path):
        uploads.append((bytes(content), rel_path))
        return True, {
            "path": f"/tmp/workspace/feishu_files/downloads/files/{len(uploads)}.pdf",
            "size": len(content),
        }

    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.agent_http_bridge.upload_file_bytes",
        fake_upload,
    )

    big = b"B" * (E2A_PAYLOAD_MAX_BYTES + 1)
    result = await persist_hook(big, "files", "report.pdf")

    assert len(uploads) == 1
    assert uploads[0][0] == big
    assert uploads[0][1] == "agent/workspace/feishu_files/downloads/files/report.pdf"
    assert result["path"].endswith(".pdf")
    assert result["name"]
    assert result["size"] == len(big)
    assert result["mime_type"]


@pytest.mark.asyncio
async def test_persist_hook_small_attachment_uses_base64_e2a(monkeypatch, persist_hook):
    """小附件（≤4MB）保持 base64 E2A 落盘（IM_FILE_PERSIST）。"""
    from jiuwenswarm.common.schema.message import ReqMethod

    calls = []

    async def fake_fetch_agent_unary(*, req_method, params, **kwargs):
        calls.append((req_method, params))
        return True, {
            "path": "/tmp/workspace/feishu_files/downloads/images/a.png",
            "name": "a.png",
            "filename": "a.png",
            "size": 3,
            "mime_type": "image/png",
        }

    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.e2a_proxy.fetch_agent_unary",
        fake_fetch_agent_unary,
    )

    result = await persist_hook(b"abc", "images", "a.png")

    assert len(calls) == 1
    method, params = calls[0]
    assert method == ReqMethod.IM_FILE_PERSIST
    assert params["platform"] == "feishu"
    assert params["category"] == "images"
    assert params["filename"] == "a.png"
    assert params["data"]  # base64 非空
    assert result["path"].endswith("a.png")


@pytest.mark.asyncio
async def test_enterprise_feishu_bot_uses_safe_attachment_storage_platform(monkeypatch):
    """企业飞书多 Bot 的 channel_id 可安全映射到附件目录标识。"""
    from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager

    captured = {}

    async def fake_fetch_agent_unary(*, params, **_kwargs):
        captured.update(params)
        return True, {"path": "/tmp/x.png"}

    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.e2a_proxy.fetch_agent_unary",
        fake_fetch_agent_unary,
    )
    channel = _FakePersistChannel()
    channel.channel_id = "feishu_enterprise:bot-a"
    manager = ChannelManager(_FakeMessageHandlerWithClient(agent_client=object()))
    manager._try_wire_file_persist_hook(channel)

    await channel.hook(b"x", "images", "a.png")

    assert captured["platform"] == "feishu_enterprise_bot-a"


@pytest.mark.asyncio
async def test_persist_hook_http_upload_failure_raises(monkeypatch, persist_hook):
    """大附件 HTTP 上传失败按决策 D6 抛错（整条消息按可重试错误失败）。"""
    from jiuwenswarm.gateway.routing.agent_http_bridge import E2A_PAYLOAD_MAX_BYTES

    def fake_upload(content, rel_path):
        return False, {"error": "AgentServer is unavailable", "code": "SERVICE_UNAVAILABLE"}

    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.agent_http_bridge.upload_file_bytes",
        fake_upload,
    )

    big = b"C" * (E2A_PAYLOAD_MAX_BYTES + 1)
    with pytest.raises(RuntimeError):
        await persist_hook(big, "files", "report.pdf")


@pytest.mark.asyncio
async def test_legacy_single_user_keeps_file_service_local_persistence(
    monkeypatch,
):
    """本地单用户保留 FileService 的历史目录与自定义目录语义。"""
    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.e2a_proxy.is_legacy_shared_directory_client",
        lambda _client: True,
    )
    from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager

    channel = _FakePersistChannel()
    manager = ChannelManager(_FakeMessageHandlerWithClient(agent_client=object()))
    manager._try_wire_file_persist_hook(channel)
    assert channel.hook is None


@pytest.mark.asyncio
async def test_agentos_attachment_never_falls_back_to_gateway_workspace(monkeypatch):
    """缺少 IM user_id 路由键时必须失败，不能写 Gateway 本地目录。"""
    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.e2a_proxy.is_agentos_routing_client",
        lambda _client: True,
    )
    from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager

    channel = _FakePersistChannel()
    manager = ChannelManager(_FakeMessageHandlerWithClient(agent_client=object()))
    manager._try_wire_file_persist_hook(channel)

    assert channel.hook is not None
    with pytest.raises(RuntimeError, match="authenticated AgentOS user_id"):
        await channel.hook(b"x", "images", "a.png")
