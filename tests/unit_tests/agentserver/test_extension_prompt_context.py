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
