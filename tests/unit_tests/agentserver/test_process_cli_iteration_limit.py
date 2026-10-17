# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Process CLI iteration stops use structured results on the locked SDK."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    ModelClientConfig,
    ModelRequestConfig,
    ToolCall,
    UsageMetadata,
)
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.session import InteractiveInput
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.session.stream import OutputSchema
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ModelCallInputs
from openjiuwen.harness import create_deep_agent

from jiuwenswarm.agents.harness.common.rails.ask_user_rail import StructuredAskUserRail
from jiuwenswarm.runtime.iteration_limit import ProcessCliIterationLimitRail
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def _adapter(instance, channel="process_cli"):
    adapter = JiuWenSwarmDeepAdapter()
    adapter._channel_id = channel
    adapter._is_session_scoped_adapter = True
    adapter._instance = instance
    return adapter


def _facade(adapter):
    # Use the real facade forwarding method with an already-created adapter.
    facade = object.__new__(JiuWenSwarm)
    facade._adapter = adapter
    return facade


@pytest.mark.asyncio
async def test_facade_awaits_run_configuration_before_returning():
    configure = AsyncMock()
    tool = object()
    await _facade(
        SimpleNamespace(configure_process_cli_run=configure)
    ).configure_process_cli_run(max_turns=1, host_tools=(tool,))
    configure.assert_awaited_once_with(max_turns=1, host_tools=(tool,))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("existing", "requested", "effective"), [(3, 5, 3), (10, 2, 2), (None, 4, 4)]
)
async def test_configure_preserves_smaller_cap_and_adds_only_a_guard_iteration(
    existing, requested, effective
):
    config = SimpleNamespace(max_iterations=existing)
    config.model_copy = lambda: SimpleNamespace(max_iterations=existing)
    react = SimpleNamespace(config=config, configure=MagicMock())
    instance = SimpleNamespace(
        react_agent=react,
        register_rail=AsyncMock(),
        ability_manager=SimpleNamespace(list=lambda: []),
    )
    await _adapter(instance).configure_process_cli_run(
        max_turns=requested, host_tools=()
    )
    configured = react.configure.call_args.args[0]
    assert configured.max_iterations == effective + 1
    rail = instance.register_rail.call_args.args[0]
    assert isinstance(rail, ProcessCliIterationLimitRail)
    assert rail.max_iterations == effective
    assert config.max_iterations == existing


@pytest.mark.asyncio
async def test_no_run_cap_does_not_reconfigure_or_mount_a_limit_rail():
    instance = SimpleNamespace(
        react_agent=MagicMock(),
        register_rail=AsyncMock(),
        ability_manager=SimpleNamespace(list=lambda: []),
    )
    await _adapter(instance).configure_process_cli_run(max_turns=None, host_tools=())
    instance.register_rail.assert_not_awaited()
    instance.react_agent.configure.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["web", "tui"])
async def test_other_channels_reject_options_without_mounting_a_rail(channel):
    instance = SimpleNamespace(react_agent=MagicMock(), register_rail=AsyncMock())
    with pytest.raises(ValueError, match="Process CLI Agent"):
        await _adapter(instance, channel).configure_process_cli_run(
            max_turns=1, host_tools=()
        )
    instance.register_rail.assert_not_awaited()
    instance.react_agent.configure.assert_not_called()


@pytest.mark.asyncio
async def test_guard_keeps_existing_finish_and_original_exception():
    rail = ProcessCliIterationLimitRail(1)
    ctx = AgentCallbackContext(
        agent=object(), inputs=ModelCallInputs(react_iteration=2)
    )
    original = {"output": "original outcome", "result_type": "answer"}
    ctx.request_force_finish(original)
    await rail.before_model_call(ctx)
    await rail.after_react_iteration(ctx)
    assert ctx.consume_force_finish().result == original
    ctx.exception = ValueError("real failure")
    await rail.after_react_iteration(ctx)
    assert not ctx.has_force_finish_request


@pytest.mark.parametrize("code", [None, "", "   ", 123, "MAX_ITERATIONS_REACHED"])
def test_answer_failure_only_adds_a_valid_structured_code(code):
    payload = {"output": "a diagnostic message", "result_type": "error"}
    if code is not None:
        payload["code"] = code
    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(
        OutputSchema(type="answer", index=0, payload=payload)
    )
    expected = {"event_type": "chat.error", "error": "a diagnostic message"}
    if code == "MAX_ITERATIONS_REACHED":
        expected["code"] = code
    assert parsed == expected


class _ScriptedModel:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0
        self.model_client_config = ModelClientConfig(
            client_provider="OpenAI",
            api_key="local-test",
            api_base="http://127.0.0.1:1",
        )
        self.model_config = ModelRequestConfig(model_name="iteration-test")

    async def invoke(self, messages, **kwargs):
        self.calls += 1
        if self.outcome == "model_error":
            raise ValueError("actual model failure")
        tool_calls = []
        if self.calls == 1 and self.outcome in {"tool", "ask", "terminal_tool_error"}:
            name = "ask_user" if self.outcome == "ask" else "iteration_probe"
            arguments = (
                '{"questions":[{"header":"Choice","question":"Continue?",'
                '"options":[{"label":"Yes","description":"continue"},'
                '{"label":"No","description":"stop"}]}]}'
                if self.outcome == "ask"
                else "{}"
            )
            tool_calls = [
                ToolCall(
                    id="limit-call", type="function", name=name, arguments=arguments
                )
            ]
        return AssistantMessage(
            content="" if tool_calls else "completed",
            tool_calls=tool_calls,
            usage_metadata=UsageMetadata(
                model_name="iteration-test", finish_reason="stop"
            ),
        )

    async def stream(self, messages, **kwargs):
        response = await self.invoke(messages, **kwargs)
        yield AssistantMessageChunk(
            content=response.content,
            tool_calls=response.tool_calls,
            usage_metadata=response.usage_metadata,
        )


class _ProbeTool(Tool):
    def __init__(self):
        super().__init__(
            ToolCard(name="iteration_probe", description="Complete one tool call")
        )
        self.calls = 0

    async def invoke(self, inputs, **kwargs):
        self.calls += 1
        return "tool completed"

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "existing", "requested", "task_loop"),
    [
        ("answer", 5, 1, False),
        ("tool", 5, 1, False),
        ("tool", 1, 5, False),
        ("model_error", 5, 1, False),
        ("terminal_tool_error", 5, 1, False),
        ("ask", 5, 1, False),
        ("answer", 5, 1, True),
        ("tool", 5, 1, True),
        ("ask", 5, 1, True),
    ],
)
async def test_locked_sdk_limit_keeps_final_failures_and_resumes_last_interaction(
    tmp_path, outcome, existing, requested, task_loop
):
    """Exercise actual ReAct/task lifecycle; only the external model is scripted."""
    await Runner.start()
    model, tool = _ScriptedModel(outcome), _ProbeTool()
    agent = create_deep_agent(
        model=model,
        tools=[tool],
        workspace=str(tmp_path),
        rails=[StructuredAskUserRail()] if outcome == "ask" else [],
        enable_task_loop=task_loop,
        max_iterations=existing,
        enable_model_anomaly_detection_rail=False,
        enable_read_image_multimodal=False,
    )
    session = create_agent_session(
        session_id=f"iteration-limit-{uuid.uuid4().hex}", card=agent.card
    )
    await session.pre_run(inputs={})
    try:
        if task_loop:
            # Production applies ready callbacks after the interaction Agent
            # has started; merely queuing a rail then misses the active loop.
            await agent.start(session=session)
        await _facade(_adapter(agent)).configure_process_cli_run(
            max_turns=requested, host_tools=()
        )
        rail = agent.find_rails_by_type((ProcessCliIterationLimitRail,))[0]
        assert agent.is_registered_rail(rail)
        assert not agent.is_pending_rail(rail)
        if outcome == "terminal_tool_error":
            agent.ability_manager.execute = AsyncMock(
                side_effect=ValueError("actual tool failure")
            )
        if outcome in {"model_error", "terminal_tool_error"}:
            with pytest.raises(ValueError, match="actual .* failure"):
                await agent.invoke({"query": "perform this test"}, session=session)
            assert model.calls == 1
            return
        result = await agent.invoke({"query": "perform this test"}, session=session)
        if outcome == "ask":
            assert result["result_type"] == "interrupt", result
            assert model.calls == 1
            answer = InteractiveInput()
            answer.update("limit-call", {"answers": {"Continue?": "continue"}})
            result = await agent.invoke({"query": answer}, session=session)
        if outcome == "answer":
            assert result["result_type"] == "answer", result
            assert result["output"] == "completed"
        else:
            assert result["result_type"] == "error", result
            assert result["code"] == "MAX_ITERATIONS_REACHED", result
        assert model.calls == 1
        assert tool.calls == (1 if outcome == "tool" else 0)
    finally:
        await agent.stop()
        await session.post_run()
        await Runner.stop()
