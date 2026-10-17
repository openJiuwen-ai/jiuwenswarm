"""Regression tests for controlled-channel /rewind notifications."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler


class _RemoteClient:
    async def send_request(self, _env: object) -> SimpleNamespace:
        return SimpleNamespace(
            ok=False,
            payload={"error": "目标轮次不存在", "code": "BAD_REQUEST"},
        )


class _UnreachableClient:
    """模拟 AgentServer 不可达：请求阶段直接抛连接异常。"""

    async def send_request(self, _env: object) -> SimpleNamespace:  # pragma: no cover
        raise ConnectionError("AgentServer WebSocket connection closed")


class _RewindHandler(MessageHandler):
    @classmethod
    def create(cls, client: object | None = None) -> "_RewindHandler":
        setattr(MessageHandler, "_instance", None)
        setattr(cls, "_instance", None)
        for owner in (MessageHandler, cls):
            if "_singleton_initialized" in owner.__dict__:
                del owner._singleton_initialized
        handler = cls(client if client is not None else _RemoteClient())
        handler.notices: list[object] = []
        handler._get_config_raw = lambda: {}  # type: ignore[assignment]
        return handler

    async def _cancel_agent_work_for_session(self, _msg: object, _session_id: str) -> None:
        return None

    async def send_channel_notice(
        self,
        _user_infos: dict,
        _channel_id: str,
        _session_id: str | None,
        payload: object,
    ) -> None:
        self.notices.append(payload)


@pytest.mark.asyncio
async def test_rewind_remote_business_error_is_not_reported_as_unavailable() -> None:
    handler = _RewindHandler.create()
    msg = SimpleNamespace(channel_id="feishu", session_id="conversation-1")
    handler._channel_states["feishu:conversation-1"] = SimpleNamespace(session_id="target-session")

    await handler._rewind_slash_notice({}, "feishu", "conversation-1", msg, turn_index=99)

    assert handler.notices == [{"error": "目标轮次不存在", "code": "BAD_REQUEST"}]


@pytest.mark.asyncio
async def test_rewind_agent_server_unreachable_reports_error_without_local_fallback() -> None:
    """PR-1R：AgentServer 不可达时报错，不再回退本地截断。"""
    handler = _RewindHandler.create(client=_UnreachableClient())
    msg = SimpleNamespace(channel_id="feishu", session_id="conversation-1")
    handler._channel_states["feishu:conversation-1"] = SimpleNamespace(session_id="target-session")

    await handler._rewind_slash_notice({}, "feishu", "conversation-1", msg, turn_index=2)

    assert handler.notices == [{"error": "AgentServer 不可达，无法回退"}]
