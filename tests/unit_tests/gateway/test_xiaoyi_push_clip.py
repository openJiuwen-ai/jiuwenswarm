"""小艺 push 正文超长截断。

真机现象：子 agent 产出的回复 3065 字，push webhook 直接
``HTTP 400 invalidParam: ...parts[0].text: size must be between 0 and 1024``，
且没有重试，用户只看得到循环前的"正在执行中"占位。
"""
from __future__ import annotations

import pytest

from gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils import (
    push as push_module,
)
from gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.push import (
    PUSH_TEXT_MAX_CHARS,
    PUSH_TEXT_TRUNCATED_SUFFIX,
    PushConfig,
    XiaoYiPushService,
    clip_push_text,
)


def test_clip_push_text_keeps_short_text() -> None:
    assert clip_push_text("已完成") == "已完成"
    exact = "x" * PUSH_TEXT_MAX_CHARS
    assert clip_push_text(exact) == exact


def test_clip_push_text_truncates_long_text_with_marker() -> None:
    long_text = "代码行\n" * 800  # 远超 1024
    clipped = clip_push_text(long_text)

    assert len(clipped) <= PUSH_TEXT_MAX_CHARS
    assert clipped.endswith(PUSH_TEXT_TRUNCATED_SUFFIX)
    assert clipped.startswith("代码行\n")


@pytest.mark.asyncio
async def test_send_push_payload_text_is_clipped(monkeypatch) -> None:
    """发送出去的 payload 里 parts[0].text 必须 ≤1024（上游硬限制）。"""
    captured: dict = {}

    class _Response:
        status = 200

        async def text(self) -> str:
            return ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Session:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, url, *, headers=None, json=None, timeout=None):
            captured["payload"] = json
            return _Response()

    monkeypatch.setattr(push_module.aiohttp, "ClientSession", _Session)

    service = XiaoYiPushService(
        PushConfig(
            mode="xiaoyi_channel",
            api_id="api",
            push_id="push",
            ak="ak",
            sk="sk",
            push_url="https://example.invalid/webhook",
        )
    )
    assert await service.send_push("标题", "长" * 3000) is True

    part = captured["payload"]["result"]["artifacts"][0]["parts"][0]["text"]
    assert len(part) <= PUSH_TEXT_MAX_CHARS
    assert part.endswith(PUSH_TEXT_TRUNCATED_SUFFIX)
