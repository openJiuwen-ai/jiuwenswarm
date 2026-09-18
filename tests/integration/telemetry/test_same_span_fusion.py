"""Integration coverage for AgentCore spans enriched in place by JiuwenSwarm."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from types import SimpleNamespace
import uuid

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from openjiuwen.agent_teams.observability import (
    ObservabilityConfig,
    init_observability,
    shutdown_observability,
)
from openjiuwen.agent_teams.observability.rail import TeamObservabilityRail
from openjiuwen.harness.observability.rail import AgentObservabilityRail
from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec
from openjiuwen.agent_teams.schema.team import TeamRole
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall, UsageMetadata
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.runner import Runner
from openjiuwen.core.runner.callback import LLMCallEvents, ToolCallEvents
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    TaskIterationInputs,
)

from jiuwenswarm.extensions.identity_provider import IdentityInfo, IdentityStore
from jiuwenswarm.telemetry.context_propagation import inject_trace_context
from jiuwenswarm.telemetry.config import TelemetryConfig
from jiuwenswarm.telemetry.enrichment.callbacks import RichTelemetryCallbacks
from jiuwenswarm.telemetry.metrics import TelemetryMetrics
from jiuwenswarm.telemetry.request_context import (
    TraceBindingRegistry,
    bind_incoming_request,
    reset_incoming_request,
)
from jiuwenswarm.telemetry.span_registry import SpanRegistryProcessor


_FORBIDDEN_DUPLICATE_NAMES = {
    "jiuwenswarm.agent.invoke",
    "jiuwenswarm.agent.invoke.stream",
    "gen_ai.chat",
}


@pytest.fixture
async def fusion_env() -> AsyncIterator[SimpleNamespace]:
    shutdown_observability()
    identity_token = IdentityStore.set_identity(None)
    exporter = InMemorySpanExporter()
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": "same-span-integration",
                "service.version": "integration",
                "jiuwenclaw.claw.id": "claw-integration",
            }
        )
    )
    span_registry = SpanRegistryProcessor(max_spans=256, ttl_seconds=60)
    provider.add_span_processor(span_registry)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    metric_reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[metric_reader])
    callbacks = RichTelemetryCallbacks(
        span_registry=span_registry,
        metrics=TelemetryMetrics(meter_provider),
        config=TelemetryConfig(enabled=True, claw_id="claw-integration"),
    )
    init_observability(
        ObservabilityConfig(enabled=True, backend="otlp"),
        tracer_provider_override=provider,
        owns_provider=False,
    )
    await callbacks.register(Runner.callback_framework)
    runtime = SimpleNamespace(
        is_unified_active=lambda: True,
        tracer_provider=provider,
        span_registry=span_registry,
        trace_bindings=TraceBindingRegistry(max_bindings=64, ttl_seconds=60),
    )
    try:
        yield SimpleNamespace(
            callbacks=callbacks,
            exporter=exporter,
            framework=Runner.callback_framework,
            meter_provider=meter_provider,
            metric_reader=metric_reader,
            provider=provider,
            runtime=runtime,
            span_registry=span_registry,
        )
    finally:
        await callbacks.unregister(Runner.callback_framework)
        shutdown_observability()
        provider.force_flush()
        meter_provider.shutdown()
        provider.shutdown()
        IdentityStore.clear(identity_token)


def _assert_parent_chain(spans: list[object]) -> None:
    span_ids = {span.context.span_id for span in spans}
    assert not [
        span.name
        for span in spans
        if span.parent is not None and span.parent.span_id not in span_ids
    ]


def _assert_no_duplicate_enterprise_spans(spans: list[object]) -> None:
    assert not [
        span.name
        for span in spans
        if span.name in _FORBIDDEN_DUPLICATE_NAMES
        or span.name.startswith("gen_ai.tool.execute:")
    ]


@pytest.mark.asyncio
async def test_code_agent_tree_keeps_core_spans_and_adds_rich_attributes(
    fusion_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness import agent_observability

    monkeypatch.setattr(
        agent_observability,
        "_get_unified_runtime",
        lambda: fusion_env.runtime,
    )
    identity_token = IdentityStore.set_identity(
        IdentityInfo(user_id="user-code", domain_id="domain-a", app_id="app-code")
    )
    handle = agent_observability.open_agent_run_span(
        session_id="session-code",
        request_id="request-code",
        channel_id="channel-code",
        mode="code.normal",
    )
    assert handle is not None

    # ObservabilityRail is now a facade whose hooks live on the inner rails.
    team_rail = TeamObservabilityRail()
    agent_rail = AgentObservabilityRail()
    inputs = TaskIterationInputs(
        iteration=1,
        loop_event=None,
        query="Use the weather tool",
    )
    agent = SimpleNamespace(
        member_name="coder",
        team_name="single-agent",
        card=SimpleNamespace(name="coder"),
        role=TeamRole.LEADER,
        deep_config=SimpleNamespace(enable_task_loop=True),
    )
    context = AgentCallbackContext(
        agent=agent,
        inputs=inputs,
        session=SimpleNamespace(get_session_id=lambda: "session-code"),
    )
    business_result = SimpleNamespace(
        content="sunny",
        finish_reason="stop",
        tool_calls=[],
        usage_metadata=SimpleNamespace(
            input_tokens=7,
            output_tokens=3,
            cache_read_input_tokens=2,
            cache_creation_input_tokens=1,
            reasoning_tokens=1,
        ),
    )
    try:
        await team_rail.before_task_iteration(context)
        await agent_rail.before_task_iteration(context)
        await fusion_env.framework.trigger(
            LLMCallEvents.LLM_INVOKE_INPUT,
            messages=[{"role": "user", "content": "weather in Paris"}],
            model="core-model",
            temperature=0.2,
        )
        await fusion_env.framework.trigger(
            ToolCallEvents.TOOL_CALL_STARTED,
            tool_name="weather",
            tool_id="tool-code",
            inputs=(("Paris",), {}),
        )
        tool_result = {"forecast": "sunny"}
        tool_callbacks = await fusion_env.framework.trigger(
            ToolCallEvents.TOOL_CALL_FINISHED,
            tool_name="weather",
            tool_id="tool-code",
            inputs=(("Paris",), {}),
            result=tool_result,
        )
        llm_callbacks = await fusion_env.framework.trigger(
            LLMCallEvents.LLM_INVOKE_OUTPUT,
            result=business_result,
        )
        inputs.result = {"output": "sunny"}
        await team_rail.after_task_iteration(context)
        await agent_rail.after_task_iteration(context)
    finally:
        agent_observability.close_agent_run_span(
            handle,
            session_id="session-code",
        )
        IdentityStore.clear(identity_token)

    fusion_env.provider.force_flush()
    spans = list(fusion_env.exporter.get_finished_spans())
    llm_spans = [span for span in spans if span.name == "llm.call"]
    tool_spans = [span for span in spans if span.name.startswith("tool.")]
    agent_spans = [
        span for span in spans if span.name.startswith("agent.coder.task_iteration")
    ]

    assert len(llm_spans) == 1
    assert len(tool_spans) == 1
    assert len(agent_spans) == 1
    assert llm_callbacks[0] is business_result
    assert tool_callbacks[0] is tool_result
    assert llm_spans[0].attributes["gen_ai.input.messages.count"] == 1
    assert llm_spans[0].attributes["gen_ai.usage.input_tokens"] == 7
    assert llm_spans[0].attributes["user.id"] == "user-code"
    assert tool_spans[0].attributes["gen_ai.tool.call.id"] == "tool-code"
    assert tool_spans[0].attributes["jiuwenclaw.request.id"] == "request-code"
    assert handle.root_span.attributes["jiuwenswarm.mode"] == "code.normal"
    assert {span.context.trace_id for span in spans} == {
        handle.root_span.get_span_context().trace_id
    }
    _assert_parent_chain(spans)
    _assert_no_duplicate_enterprise_spans(spans)
    assert fusion_env.span_registry.active_count() == 0
    assert fusion_env.callbacks._span_state.active_count() == 0
    assert fusion_env.callbacks._metric_state.active_count() == 0
    assert (
        fusion_env.runtime.trace_bindings.resolve("session-code", "request-code")
        is None
    )


@pytest.mark.asyncio
async def test_real_team_runner_uses_same_provider_and_has_no_orphans(
    fusion_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from openjiuwen.core.foundation.llm import model as model_module

    monkeypatch.setattr(
        "jiuwenswarm.telemetry.get_telemetry_runtime",
        lambda: fusion_env.runtime,
    )

    class _ModelClient:
        def __init__(self) -> None:
            self.stream_calls = 0
            self.offered_tool_names: list[set[str]] = []

        async def invoke(self, messages, **_kwargs):
            return AssistantMessage(
                content="team answer",
                usage_metadata=UsageMetadata(
                    model_name="mock-team-model",
                    input_tokens=4,
                    output_tokens=2,
                    total_tokens=6,
                    finish_reason="stop",
                ),
            )

        async def stream(self, messages, **kwargs):
            del messages
            tools = kwargs.get("tools") or []
            offered_names = {
                str(getattr(tool, "name", "") or tool.get("name", ""))
                if isinstance(tool, dict)
                else str(getattr(tool, "name", ""))
                for tool in tools
            }
            self.offered_tool_names.append(offered_names)
            self.stream_calls += 1
            if self.stream_calls == 1:
                tool_calls = [
                    ToolCall(
                        id="team-build-tool",
                        type="function",
                        name="build_team",
                        arguments=(
                            '{"display_name":"Fusion Team",'
                            '"team_desc":"Exercise real telemetry events",'
                            '"leader_display_name":"Leader",'
                            '"leader_desc":"Runs the integration",'
                            '"enable_hitt":true}'
                        ),
                        index=0,
                    )
                ]
                content = ""
                finish_reason = "tool_calls"
            elif self.stream_calls == 2:
                tool_calls = [
                    ToolCall(
                        id="team-task-tool",
                        type="function",
                        name="create_task",
                        arguments=(
                            '{"tasks":[{"task_id":"team-task-1",'
                            '"title":"Observe","content":"Verify telemetry"}]}'
                        ),
                        index=0,
                    )
                ]
                content = ""
                finish_reason = "tool_calls"
            elif self.stream_calls == 3:
                tool_calls = [
                    ToolCall(
                        id="team-message-tool",
                        type="function",
                        name="send_message",
                        arguments=(
                            '{"to":"observer","content":"Telemetry ready",'
                            '"summary":"ready"}'
                        ),
                        index=0,
                    )
                ]
                content = ""
                finish_reason = "tool_calls"
            else:
                tool_calls = None
                content = "team answer"
                finish_reason = "stop"
            usage = UsageMetadata(
                model_name="mock-team-model",
                input_tokens=4,
                output_tokens=2,
                total_tokens=6,
                finish_reason=finish_reason,
            )
            try:
                yield AssistantMessageChunk(
                    content=content,
                    tool_calls=tool_calls,
                    finish_reason=finish_reason,
                    usage_metadata=UsageMetadata(
                        model_name="mock-team-model",
                        input_tokens=4,
                        output_tokens=2,
                        total_tokens=6,
                        finish_reason=finish_reason,
                    ),
                )
            finally:
                await Runner.callback_framework.trigger(
                    LLMCallEvents.LLM_OUTPUT,
                    response=content,
                    tool_calls=tool_calls,
                    usage=usage,
                    model_name="mock-team-model",
                    model_provider="OpenAI",
                    is_stream=True,
                )

    fake_client = _ModelClient()
    monkeypatch.setattr(
        model_module,
        "create_model_client",
        lambda **_kwargs: fake_client,
    )
    from openjiuwen.agent_teams.agent.team_agent import TeamAgent

    async def _skip_observer_runtime(self, member_name: str) -> None:
        del self
        assert member_name == "observer"

    monkeypatch.setattr(TeamAgent, "_on_teammate_created", _skip_observer_runtime)
    team_name = f"fusion_{uuid.uuid4().hex[:6]}"
    session_id = f"fusion_session_{uuid.uuid4().hex[:6]}"
    spec = TeamAgentSpec.model_validate(
        {
            "team_name": team_name,
            "lifecycle": "temporary",
            "spawn_mode": "inprocess",
            "enable_hitt": True,
            "leader": {
                "member_name": "leader",
                "display_name": "Leader",
                "desc": "Answer briefly.",
            },
            "predefined_members": [
                {
                    "member_name": "observer",
                    "display_name": "Observer",
                    "desc": "Receives the telemetry handoff.",
                    "role_type": "human_agent",
                }
            ],
            "agents": {
                "leader": {
                    "model": {
                        "model_client_config": {
                            "client_provider": "OpenAI",
                            "api_base": "http://mock",
                            "api_key": "mock-key",
                            "verify_ssl": False,
                        },
                        "model_request_config": {
                            "model": "mock-team-model",
                            "temperature": 0.0,
                        },
                    },
                    "tools": [],
                    "max_iterations": 4,
                    "language": "en",
                },
                "teammate": {
                    "model": {
                        "model_client_config": {
                            "client_provider": "OpenAI",
                            "api_base": "http://mock",
                            "api_key": "mock-key",
                            "verify_ssl": False,
                        },
                        "model_request_config": {
                            "model": "mock-team-model",
                            "temperature": 0.0,
                        },
                    },
                    "tools": [],
                    "max_iterations": 1,
                    "language": "en",
                },
            },
            "transport": {"type": "inprocess"},
            "storage": {"type": "memory"},
        }
    )

    async def _consume_one_answer() -> None:
        stream = Runner.run_agent_team_streaming(
            agent_team=spec,
            inputs={"query": "Say hello"},
            session=session_id,
        )
        async with aclosing(stream):
            async for chunk in stream:
                if getattr(chunk, "type", "") in {"answer", "team_completed"}:
                    return

    monkeypatch.chdir(tmp_path)
    await Runner.start()
    try:
        gateway_tracer = fusion_env.provider.get_tracer("test.gateway")
        with gateway_tracer.start_as_current_span("channel.request"):
            metadata = {
                "user_id": "user-team",
                "domain_id": "domain-team",
                "app_id": "app-team",
            }
            inject_trace_context(metadata)
            request_binding = bind_incoming_request(
                SimpleNamespace(
                    metadata=metadata,
                    request_id="request-team",
                    session_id=session_id,
                    channel_id="web",
                    params={"mode": "team"},
                    req_method=SimpleNamespace(value="chat.send"),
                    is_stream=True,
                )
            )
            try:
                await asyncio.wait_for(_consume_one_answer(), timeout=8.0)
            finally:
                reset_incoming_request(request_binding)
    finally:
        await Runner.stop()

    fusion_env.provider.force_flush()
    spans = list(fusion_env.exporter.get_finished_spans())
    gateway_spans = [span for span in spans if span.name == "channel.request"]
    team_spans = [span for span in spans if span.name == f"team.{team_name}"]
    agent_spans = [span for span in spans if span.name.startswith("agent.")]
    member_spans = [span for span in spans if span.name.startswith("member.")]
    task_spans = [span for span in spans if span.name.startswith("task.")]
    message_spans = [span for span in spans if span.name.startswith("msg.")]
    llm_spans = [span for span in spans if span.name == "llm.call"]
    tool_spans = [
        span
        for span in spans
        if span.name in {"tool.build_team", "tool.create_task", "tool.send_message"}
    ]

    assert [span.name for span in gateway_spans] == ["channel.request"]
    assert [span.name for span in team_spans] == [f"team.{team_name}"]
    # Agent-core now opens one agent.* span per iteration/invoke, not a single
    # fused agent span for the whole team run.
    assert agent_spans
    assert all(span.name.startswith("agent.leader.") for span in agent_spans)
    assert [span.name for span in member_spans].count("member.observer.spawned") == 1
    assert [span.name for span in task_spans].count("task.team-task-1") == 1
    assert [span.name for span in task_spans].count("task.team-task-1.created") == 1
    assert [span.name for span in message_spans].count("msg.leader->observer") == 1
    assert len(llm_spans) == 4
    assert [span.name for span in tool_spans] == [
        "tool.build_team",
        "tool.create_task",
        "tool.send_message",
    ]
    assert fake_client.stream_calls == 4
    assert all(
        {"build_team", "create_task", "send_message"} <= names
        for names in fake_client.offered_tool_names
    )
    team_span = team_spans[0]
    assert team_span.parent.span_id == gateway_spans[0].context.span_id
    assert len({span.context.trace_id for span in spans}) == 1
    request_spans = [
        *team_spans,
        *agent_spans,
        *member_spans,
        *task_spans,
        *message_spans,
        *llm_spans,
        *tool_spans,
    ]
    assert request_spans
    assert all(
        span.attributes["jiuwenclaw.request.id"] == "request-team"
        and span.attributes["jiuwenclaw.channel.id"] == "web"
        and span.attributes["jiuwenclaw.session.id"] == session_id
        and span.attributes["jiuwenclaw.mode"] == "team"
        and span.attributes["user.id"] == "user-team"
        for span in request_spans
    )
    assert all(
        span.attributes["gen_ai.request.model"] == "mock-team-model"
        for span in llm_spans
    )
    assert {span.attributes["gen_ai.tool.call.id"] for span in tool_spans} == {
        f"build_team_{team_name}_leader",
        f"create_task_{team_name}_leader",
        f"send_message_{team_name}_leader",
    }
    team_span_id = team_span.context.span_id
    assert all(span.parent.span_id == team_span_id for span in member_spans)
    task_root = next(span for span in task_spans if span.name == "task.team-task-1")
    task_created = next(
        span for span in task_spans if span.name == "task.team-task-1.created"
    )
    assert task_root.parent.span_id == team_span_id
    assert task_created.parent.span_id == task_root.context.span_id
    assert all(span.parent.span_id == team_span_id for span in message_spans)
    agent_ids = {span.context.span_id for span in agent_spans}
    assert all(
        span.parent is not None
        and (
            span.parent.span_id == team_span_id
            or span.parent.span_id in agent_ids
        )
        for span in agent_spans
    )
    assert all(span.parent.span_id in agent_ids for span in llm_spans + tool_spans)
    _assert_parent_chain(spans)
    _assert_no_duplicate_enterprise_spans(spans)
    assert fusion_env.span_registry.active_count() == 0
