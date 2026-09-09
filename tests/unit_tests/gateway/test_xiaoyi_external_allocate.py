"""J2: xiaoyi 渠道新会话沿用上层 conversationId 作 AgentServer session_id（裸值）。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from collections.abc import AsyncIterator

import pytest

from jiuwenswarm.common.schema import Message
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler


class _CapturingAgentClient:
    def __init__(self) -> None:
        self.requests: list[object] = []

    async def send_request(self, env: object) -> SimpleNamespace:
        self.requests.append(env)
        # 模拟 AgentServer 按 session.create 透传的 session_id 落盘并回包。
        params = getattr(env, "params", {}) or {}
        sid = str(params.get("session_id") or "xiaoyi_allocated_fallback")
        return SimpleNamespace(
            ok=True,
            payload={
                "session_id": sid,
                "sessionId": sid,
                "projectId": "default",
                "projectDir": "",
                "workMode": "work",
                "prewarm_hit": False,
                "prewarm_status": "bypassed",
            },
        )

    async def send_request_stream(self, env: object) -> AsyncIterator[object]:
        if False:  # pragma: no cover
            yield env
        return


class _TestMessageHandler(MessageHandler):
    @classmethod
    def create(cls) -> "_TestMessageHandler":
        setattr(MessageHandler, "_instance", None)
        setattr(cls, "_instance", None)
        return cls(_CapturingAgentClient())


@pytest.mark.asyncio
async def test_xiaoyi_numeric_new_conv_passes_conversation_id_as_session_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """新手机会话：session.create 的 params.session_id = 裸 conversationId。"""
    # 反查目录为空 → 走分配
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: tmp_path
    )

    handler = _TestMessageHandler.create()
    numeric_conv = "1788936453184"
    msg = Message(
        id="req-1",
        type="req",
        channel_id="xiaoyi",
        session_id=numeric_conv,
        params={"query": "哈喽"},
        timestamp=1.0,
        ok=True,
        metadata={"xiaoyi_session_id": numeric_conv},
    )
    await handler._resolve_external_channel_session(msg)

    assert len(handler.agent_client.requests) == 1
    env = handler.agent_client.requests[0]
    create_params = getattr(env, "params", {})
    assert create_params["session_id"] == numeric_conv
    # resolved 回写到 msg.session_id
    assert msg.session_id == numeric_conv
    assert msg.metadata["external_session_id"] == numeric_conv


@pytest.mark.asyncio
async def test_xiaoyi_invalid_conversation_id_falls_back_to_allocated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """conversationId 含正则外字符（如 '/'）→ 不透传，回退 AgentServer 自分配。"""
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: tmp_path
    )

    handler = _TestMessageHandler.create()
    bad_conv = "1788/invalid"
    msg = Message(
        id="req-2",
        type="req",
        channel_id="xiaoyi",
        session_id=bad_conv,
        params={"query": "你好呀"},
        timestamp=1.0,
        ok=True,
        metadata={"xiaoyi_session_id": bad_conv},
    )
    await handler._resolve_external_channel_session(msg)

    assert len(handler.agent_client.requests) == 1
    create_params = getattr(handler.agent_client.requests[0], "params", {})
    assert "session_id" not in create_params
    # 回包的 session_id 由 AgentServer 自分配（fake 用 fallback 占位）
    assert msg.session_id == "xiaoyi_allocated_fallback"


@pytest.mark.asyncio
async def test_a2a_numeric_does_not_pass_session_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """a2a 不在本次改动：不透传 session_id（J2 仅 gate xiaoyi）。"""
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: tmp_path
    )

    handler = _TestMessageHandler.create()
    msg = Message(
        id="req-3",
        type="req",
        channel_id="a2a",
        session_id="a2a-conv-001",
        params={"query": "hi"},
        timestamp=1.0,
        ok=True,
    )
    await handler._resolve_external_channel_session(msg)

    assert len(handler.agent_client.requests) == 1
    create_params = getattr(handler.agent_client.requests[0], "params", {})
    assert "session_id" not in create_params
