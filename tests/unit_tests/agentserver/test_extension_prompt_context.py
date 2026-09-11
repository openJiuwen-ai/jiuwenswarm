from __future__ import annotations

from types import SimpleNamespace

import pytest

from openjiuwen.core.context_engine.base import ContextWindow
from openjiuwen.core.foundation.llm import UserMessage
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import SystemPromptBuilder

from jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail import RuntimePromptRail
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions.prompt_context import (
    clear_extension_prompt_context,
    get_extension_prompt_context,
    set_extension_prompt_context,
)
from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


class _FakeSession:
    def get_session_id(self) -> str:
        return "sess1"


class _FakeAgent:
    def __init__(self, builder: SystemPromptBuilder) -> None:
        self.system_prompt_builder = builder
        self.prompt_attachment_manager = None


def test_extension_prompt_context_is_replaced_and_cleared_per_session():
    set_extension_prompt_context(
        "session-1",
        system_prompt_blocks=[" policy "],
        reference_context_blocks=[" memory "],
    )
    assert get_extension_prompt_context("session-1").system_prompt_blocks == (
        "policy",
    )
    assert get_extension_prompt_context("session-1").reference_context_blocks == (
        "memory",
    )

    set_extension_prompt_context("session-1")

    assert get_extension_prompt_context("session-1").system_prompt_blocks == ()
    assert get_extension_prompt_context("session-1").reference_context_blocks == ()


@pytest.mark.asyncio
async def test_runtime_rail_separates_extension_policy_memory_and_user_query():
    builder = SystemPromptBuilder(language="cn")
    agent = _FakeAgent(builder)
    runtime_rail = RuntimePromptRail(language="cn", channel="web")
    runtime_rail.init(agent)
    context = SimpleNamespace(_window_mutators=[])
    callback_ctx = AgentCallbackContext(
        agent=agent,
        session=_FakeSession(),
        context=context,
        extra={},
    )
    set_extension_prompt_context(
        "sess1",
        system_prompt_blocks=["GaussPD 固定记忆策略"],
        reference_context_blocks=["<gausspd_memory>固定加载内容</gausspd_memory>"],
    )

    try:
        await runtime_rail.before_model_call(callback_ctx)

        assert "GaussPD 固定记忆策略" in builder.build()
        assert len(context._window_mutators) == 1
        window = ContextWindow(
            context_messages=[
                UserMessage(content="历史问题"),
                UserMessage(content="当前用户问题"),
            ]
        )
        updated = await context._window_mutators[0](context, window)
        assert [message.content for message in updated.context_messages] == [
            "历史问题",
            "<gausspd_memory>固定加载内容</gausspd_memory>",
            "当前用户问题",
        ]
        assert updated.context_messages[1].metadata[
            "jiuwenswarm_extension_reference_context"
        ] is True
    finally:
        clear_extension_prompt_context("sess1")


@pytest.mark.asyncio
async def test_runtime_rail_clears_stale_extension_prompt_context():
    builder = SystemPromptBuilder(language="cn")
    agent = _FakeAgent(builder)
    runtime_rail = RuntimePromptRail(language="cn", channel="web")
    runtime_rail.init(agent)
    context = SimpleNamespace(_window_mutators=[])
    callback_ctx = AgentCallbackContext(
        agent=agent,
        session=_FakeSession(),
        context=context,
        extra={},
    )
    set_extension_prompt_context(
        "sess1",
        system_prompt_blocks=["上一轮策略"],
        reference_context_blocks=["上一轮记忆"],
    )
    await runtime_rail.before_model_call(callback_ctx)
    clear_extension_prompt_context("sess1")

    await runtime_rail.before_model_call(callback_ctx)

    assert "上一轮策略" not in builder.build()
    assert context._window_mutators == []


@pytest.mark.asyncio
async def test_before_chat_hook_publishes_only_extension_owned_prompt_blocks(
    monkeypatch,
):
    class _FakeRegistry:
        async def trigger(self, _event, context):
            context.system_prompt_blocks.append("trusted policy")
            context.reference_context_blocks.append("reference memory")

    monkeypatch.setattr(
        ExtensionRegistry,
        "get_instance",
        classmethod(lambda cls: _FakeRegistry()),
    )
    request = AgentRequest(
        request_id="request-1",
        channel_id="desktop",
        session_id="session-1",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "query": "用户问题",
            # 同名客户端字段不会被读取为可信 prompt 数据。
            "system_prompt_blocks": ["untrusted client policy"],
        },
    )
    server = SimpleNamespace(
        _should_trigger_before_chat_request_hook=(
            AgentWebSocketServer._should_trigger_before_chat_request_hook
        )
    )

    try:
        await AgentWebSocketServer._trigger_before_chat_request_hook(server, request)

        prompt_context = get_extension_prompt_context("session-1")
        assert prompt_context.system_prompt_blocks == ("trusted policy",)
        assert prompt_context.reference_context_blocks == ("reference memory",)
        assert request.params["query"] == "用户问题"
    finally:
        clear_extension_prompt_context("session-1")


def _before_chat_server_capturing_trace(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeRegistry:
        async def trigger(self, _event, context):
            captured["trace_id"] = context.trace_id
            captured["request_id"] = context.request_id

    monkeypatch.setattr(
        ExtensionRegistry,
        "get_instance",
        classmethod(lambda cls: _FakeRegistry()),
    )
    server = SimpleNamespace(
        _should_trigger_before_chat_request_hook=(
            AgentWebSocketServer._should_trigger_before_chat_request_hook
        )
    )
    return server, captured


@pytest.mark.asyncio
async def test_before_chat_hook_fills_desktop_billing_trace_id(monkeypatch):
    """PC：hook.trace_id = session&短码，与可变的 request_id UUID 分离。"""
    server, captured = _before_chat_server_capturing_trace(monkeypatch)
    session_id = "desktop_1a08f97a7d6_5e266462c9be"
    request_uuid = "e2c7953d-96b4-4aaa-bbbb-cccccccccccc"
    request = AgentRequest(
        request_id=request_uuid,
        channel_id="desktop",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "用户问题"},
        metadata={"interaction_id": request_uuid},
    )
    try:
        await AgentWebSocketServer._trigger_before_chat_request_hook(server, request)
        assert captured["request_id"] == request_uuid
        assert captured["trace_id"] == f"{session_id}&e2c7953d"
    finally:
        clear_extension_prompt_context(session_id)


@pytest.mark.asyncio
async def test_before_chat_hook_trace_id_stable_across_hitl_request_id(monkeypatch):
    """HITL 续跑换 request_id，hook.trace_id 仍钉在同一 billing core。"""
    server, captured = _before_chat_server_capturing_trace(monkeypatch)
    session_id = "desktop_sess_1"
    interaction_id = "inter-1"

    first = AgentRequest(
        request_id="req-1",
        channel_id="desktop",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "q1"},
        metadata={"interaction_id": interaction_id},
    )
    resumed = AgentRequest(
        request_id="req-2",
        channel_id="desktop",
        session_id=session_id,
        req_method=ReqMethod.CHAT_RESUME,
        params={"query": "q2"},
        metadata={"interaction_id": interaction_id},
    )
    try:
        await AgentWebSocketServer._trigger_before_chat_request_hook(server, first)
        first_trace = captured["trace_id"]
        await AgentWebSocketServer._trigger_before_chat_request_hook(server, resumed)
        assert first_trace == "desktop_sess_1&inter-1"
        assert captured["trace_id"] == first_trace
        assert captured["request_id"] == "req-2"
    finally:
        clear_extension_prompt_context(session_id)


@pytest.mark.asyncio
async def test_before_chat_hook_xiaoyi_keeps_composite_task_id(monkeypatch):
    """手机：无 interaction_id 时 hook.trace_id 保持 task_id 复合串，不是 session&前8。"""
    server, captured = _before_chat_server_capturing_trace(monkeypatch)
    composite = "sess-x&19&ea5d&0"
    request = AgentRequest(
        request_id="req-1",
        channel_id="xiaoyi",
        session_id="jiuwen-1",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "用户问题"},
        metadata={"xiaoyi_task_id": composite},
    )
    try:
        await AgentWebSocketServer._trigger_before_chat_request_hook(server, request)
        assert captured["trace_id"] == composite
        assert captured["trace_id"] != "jiuwen-1&sess-x"
    finally:
        clear_extension_prompt_context("jiuwen-1")
