"""直连云 WS 形态的 clawd_bot_init 注册与连接级心跳回归测试。

背景（2026-10-14 排查）：桌面补丁面回流（93be9c8af）曾把 channel 层的
clawd_bot_init / 连接级心跳整体移除、交由桌面 cloud-ws-relay 统一代发，
导致独立部署的后台 server（ws_url 直连小艺云）连上后从不注册、收不到
任何消息。修复后按传输形态分工：

- 直连云 WS（wss://...）：channel 层负责 clawd_bot_init 注册帧 + 20s 心跳；
- np:// 命名管道（桌面集成形态）：维持 relay 代发，channel 层不注册。
"""
import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_connect import (
    XiaoyiChannel,
    XiaoyiChannelConfig,
)

_WS_URL = "wss://hag.cloud.huawei.com/openclaw/v1/ws/link"
_NP_URL = "np://claw-relay/ws/link"


def _build_channel() -> XiaoyiChannel:
    return XiaoyiChannel(
        XiaoyiChannelConfig(
            enabled=True,
            agent_id="agent-1",
            ak="ak-1",
            sk="sk-1",
            ws_url1=_WS_URL,
        ),
        RobotMessageRouter(),
    )


class _FakeWS:
    """websockets.connect 返回的假连接：迭代挂起模拟长连接静默。"""

    def __init__(self) -> None:
        self.close_code = None
        self.close_reason = None

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        await asyncio.sleep(3600)
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_direct_ws_connect_sends_init_and_starts_heartbeat() -> None:
    channel = _build_channel()
    channel._running = True
    sent: list[dict[str, Any]] = []

    async def fake_safe_ws_send(url_key: str, payload: dict[str, Any]) -> None:
        sent.append(payload)

    channel._safe_ws_send = fake_safe_ws_send

    connect_cm = MagicMock()
    connect_cm.__aenter__ = AsyncMock(return_value=_FakeWS())
    connect_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("websockets.connect", return_value=connect_cm):
        task = asyncio.create_task(channel._connect("ws_url1", _WS_URL))
        try:
            # 等待注册帧发出（_connect 建连后立即发送）
            for _ in range(100):
                if sent:
                    break
                await asyncio.sleep(0.02)
            assert sent, "直连云形态应发送 clawd_bot_init 注册帧"
            assert sent[0] == {
                "msgType": "clawd_bot_init",
                "agentId": "agent-1",
            }
            # 连接级心跳任务已启动
            assert channel._heartbeat_tasks.get("ws_url1") is not None
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    # 连接关闭路径收信心跳任务
    assert channel._heartbeat_tasks.get("ws_url1") is None
    channel._running = False


@pytest.mark.asyncio
async def test_pipe_connect_skips_channel_level_init() -> None:
    channel = _build_channel()
    channel._running = True
    init_calls: list[str] = []
    pipe_calls: list[str] = []

    async def fake_init(url_key: str) -> None:
        init_calls.append(url_key)

    async def fake_connect_pipe(url_key: str, url: str) -> None:
        pipe_calls.append(url_key)

    channel._send_init_message = fake_init
    channel._connect_pipe = fake_connect_pipe

    await channel._connect("ws_url1", _NP_URL)

    assert pipe_calls == ["ws_url1"], "np:// 管道形态应路由到 _connect_pipe"
    assert init_calls == [], "管道形态由桌面 relay 代发，channel 层不应发注册帧"
    assert channel._heartbeat_tasks.get("ws_url1") is None
