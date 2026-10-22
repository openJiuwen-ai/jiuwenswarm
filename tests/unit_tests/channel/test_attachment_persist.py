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
async def test_persist_hook_large_attachment_uses_e2a_chunks(monkeypatch, persist_hook):
    """大附件（>4MB）走 E2A 分块上传（2MB/块，FILE_UPLOAD_CHUNK）。

    不再依赖 HTTP bridge（resolve_agent_upload_base 的 ws_port+1 推导仅对
    已启动独立上传监听器的部署有效，单机模式不监听该端口——legacy 单机
    下大附件必然连接拒绝）。仅 mock E2A 传输层（fetch_agent_unary），
    upload_file_bytes_via_e2a 的分块循环真实运行。
    """
    import base64 as _b64

    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.gateway.routing.agent_http_bridge import (
        _E2A_UPLOAD_CHUNK_BYTES,
        E2A_PAYLOAD_MAX_BYTES,
    )

    chunks: list[tuple[ReqMethod, dict]] = []
    total = 0

    async def fake_fetch_agent_unary(*, req_method, params, **_kwargs):
        nonlocal total
        chunks.append((req_method, params))
        total += len(_b64.b64decode(params["data"]))
        resolved = str(params.get("resolved_path") or "").strip()
        return True, {
            "path": resolved or f"/ws/{params['target_rel_path']}",
            "size": total,
            "upload_id": params["upload_id"],
            "final": params["final"],
        }

    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.e2a_proxy.fetch_agent_unary",
        fake_fetch_agent_unary,
    )

    big = b"B" * (E2A_PAYLOAD_MAX_BYTES + 1)
    result = await persist_hook(big, "files", "report.pdf")

    rel_path = "agent/workspace/feishu_files/downloads/files/report.pdf"
    expected_chunks = -(-len(big) // _E2A_UPLOAD_CHUNK_BYTES)  # ceil
    assert len(chunks) == expected_chunks
    assert [method for method, _ in chunks] == [ReqMethod.FILE_UPLOAD_CHUNK] * expected_chunks
    assert all(params["target_rel_path"] == rel_path for _, params in chunks)
    # 首块无 resolved_path，后续块沿用前块响应路径
    assert chunks[0][1]["resolved_path"] == ""
    assert chunks[1][1]["resolved_path"] == f"/ws/{rel_path}"
    assert [params["final"] for _, params in chunks] == [False] * (expected_chunks - 1) + [True]
    # 分块重组等于原文
    assert b"".join(_b64.b64decode(params["data"]) for _, params in chunks) == big
    assert result["path"] == f"/ws/{rel_path}"
    assert result["name"] == "report.pdf"
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
async def test_persist_hook_e2a_upload_failure_raises(monkeypatch, persist_hook):
    """E2A 分块上传失败按决策 D6 抛错（整条消息按可重试错误失败）。"""
    from jiuwenswarm.gateway.routing.agent_http_bridge import E2A_PAYLOAD_MAX_BYTES

    async def fake_fetch_agent_unary(*, req_method, params, **_kwargs):
        del req_method, params
        return False, {"error": "AgentServer is unavailable", "code": "SERVICE_UNAVAILABLE"}

    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.e2a_proxy.fetch_agent_unary",
        fake_fetch_agent_unary,
    )

    big = b"C" * (E2A_PAYLOAD_MAX_BYTES + 1)
    with pytest.raises(RuntimeError):
        await persist_hook(big, "files", "report.pdf")


def test_legacy_single_user_keeps_file_service_local_persistence():
    """A1g 后 legacy 单用户也装真 hook（E2A IM_FILE_PERSIST），不再本地写盘。"""
    from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager

    channel = _FakePersistChannel()
    manager = ChannelManager(_FakeMessageHandlerWithClient(agent_client=object()))
    manager._try_wire_file_persist_hook(channel)
    # legacy 单用户现在也注入 hook（AgentOS 态改为明确失败的 stub）
    assert channel.hook is not None


@pytest.mark.asyncio
async def test_agentos_attachment_never_falls_back_to_gateway_workspace(monkeypatch):
    """缺少 IM user_id 路由键时必须失败，不能写 Gateway 本地目录。

    AgentOS 态安装的是明确失败的 stub hook（等 IM 多用户路由）——
    该安全路径在 A1g 中保留，此处防止回归。
    """
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

