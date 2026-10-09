from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import (
    AgentRequest,
    AgentResponse,
    AgentResponseChunk,
)
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


class _ImmediateSessionManager:
    @staticmethod
    def get_session_id(session_id: str | None) -> str:
        return session_id or "default"

    @staticmethod
    async def submit_and_wait(_session_id: str, task_func):
        return await task_func()


class _ChatAdapter:
    async def handle_heartbeat(self, _request: AgentRequest):
        return None

    async def process_message_impl(
        self,
        request: AgentRequest,
        _inputs: dict,
    ) -> AgentResponse:
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={},
        )

    async def process_message_stream_impl(
        self,
        request: AgentRequest,
        _inputs: dict,
    ):
        yield AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload=None,
            is_complete=True,
        )


def _chat_facade(monkeypatch: pytest.MonkeyPatch):
    facade = JiuWenSwarm()
    facade._adapter = _ChatAdapter()
    facade._sdk_name = "harness"
    facade._session_manager = _ImmediateSessionManager()
    reconcile = AsyncMock()
    facade.reconcile_session_mcp = reconcile
    monkeypatch.setattr(interface_module, "append_history_record", lambda **_kwargs: None)
    monkeypatch.setattr(interface_module, "get_config", lambda: {})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "off")
    monkeypatch.setattr(interface_module, "build_user_prompt", lambda query, **_kwargs: query)
    return facade, reconcile


def _request(*, stream: bool = False) -> AgentRequest:
    return AgentRequest(
        request_id="request-current",
        channel_id="web",
        session_id="session-1",
        params={"query": "你好", "mode": "agent"},
        is_stream=stream,
    )


@pytest.mark.asyncio
async def test_unary_chat_passes_history_boundary_to_mcp_reconcile(monkeypatch):
    facade, reconcile = _chat_facade(monkeypatch)

    await facade.process_message(_request())

    reconcile.assert_awaited_once_with(
        "session-1",
        [],
        model_name=None,
        history_before_request_id="request-current",
    )


@pytest.mark.asyncio
async def test_stream_chat_passes_history_boundary_to_mcp_reconcile(monkeypatch):
    facade, reconcile = _chat_facade(monkeypatch)

    async for _chunk in facade.process_message_stream(_request(stream=True)):
        pass

    reconcile.assert_awaited_once_with(
        "session-1",
        [],
        model_name=None,
        history_before_request_id="request-current",
    )


@pytest.mark.asyncio
async def test_external_runtime_is_resolved_before_stream_mcp_reconcile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    expected_binding = {
        "kind": "external_harness",
        "agent_template_name": "codex-expert",
        "provider_name": "codex",
    }

    class _ExternalChatAdapter(_ChatAdapter):
        def resolve_runtime_binding(
            self,
            session_id: str,
            params: dict,
        ) -> dict:
            calls.append("resolve")
            assert session_id == "external-session"
            assert params["agent_template_name"] == "codex-expert"
            return expected_binding

        async def reconcile_session_mcp(
            self,
            _session_id: str | None,
            _needed: list[str] | None,
            *,
            runtime_binding: dict | None = None,
            **_kwargs,
        ) -> None:
            calls.append("reconcile")
            if runtime_binding is not expected_binding:
                raise AssertionError("MCP reconcile would create a DeepAgent child")

        async def process_message_stream_impl(
            self,
            request: AgentRequest,
            _inputs: dict,
        ):
            calls.append("stream")
            assert request.trusted_runtime_binding is expected_binding
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload=None,
                is_complete=True,
            )

    facade = JiuWenSwarm()
    facade._adapter = _ExternalChatAdapter()
    facade._sdk_name = "harness"
    facade._session_manager = _ImmediateSessionManager()
    monkeypatch.setattr(interface_module, "append_history_record", lambda **_kwargs: None)
    monkeypatch.setattr(interface_module, "get_config", lambda: {})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "off")
    monkeypatch.setattr(interface_module, "build_user_prompt", lambda query, **_kwargs: query)
    request = AgentRequest(
        request_id="external-request",
        channel_id="web",
        session_id="external-session",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "query": "hello",
            "mode": "agent",
            "agent_template_name": "codex-expert",
        },
        is_stream=True,
    )

    async for _chunk in facade.process_message_stream(request):
        pass

    assert calls == ["resolve", "reconcile", "stream"]


@pytest.mark.asyncio
async def test_deep_mcp_reconcile_passes_boundary_to_session_creation():
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    child = SimpleNamespace(
        _instance=None,
        _pending_skill_scan_mcp_names=set(),
    )
    adapter._get_or_create_session_adapter = AsyncMock(return_value=child)

    await adapter.reconcile_session_mcp(
        "session-1",
        [],
        model_name="model-1",
        history_before_request_id="request-current",
    )

    adapter._get_or_create_session_adapter.assert_awaited_once_with(
        "session-1",
        model_name="model-1",
        pending_mcp_scan_names=set(),
        history_before_request_id="request-current",
    )


@pytest.mark.asyncio
async def test_deep_external_mcp_reconcile_skips_session_creation() -> None:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._get_or_create_session_adapter = AsyncMock(
        side_effect=AssertionError("External MCP reconcile must not create a child")
    )

    await adapter.reconcile_session_mcp(
        "external-session",
        ["connector-a"],
        runtime_binding={
            "kind": "external_harness",
            "agent_template_name": "codex-expert",
            "provider_name": "codex",
        },
    )

    adapter._get_or_create_session_adapter.assert_not_awaited()


@pytest.mark.asyncio
async def test_facade_mcp_reconcile_keeps_legacy_adapter_signature_compatible():
    calls = []

    class _LegacyAdapter:
        async def reconcile_session_mcp(self, session_id, needed):
            calls.append((session_id, needed))

    facade = JiuWenSwarm()
    facade._adapter = _LegacyAdapter()

    await facade.reconcile_session_mcp(
        "session-1",
        [],
        model_name="model-1",
        history_before_request_id="request-current",
    )

    assert calls == [("session-1", [])]
