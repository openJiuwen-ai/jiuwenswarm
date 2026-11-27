# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Work and Code trace the verified display name when available."""

from contextlib import ExitStack, contextmanager
from contextvars import Context
from unittest.mock import AsyncMock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import INVALID_SPAN
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, TaskIterationInputs
from openjiuwen.extensions.observability import setup as otel_setup, span_context
from openjiuwen.extensions.observability.span_context import (
    bind_root_span_context,
    clear_root_span,
    get_current_agent_span,
    set_root_span,
)
from openjiuwen.harness import observability
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.observability.rail import AgentObservabilityRail
from starlette.requests import Request

from agent_gateway.southbound.data.rest_map import assemble_rest_request
from e2a.models import E2AEnvelope

from jiuwenswarm.agents.harness import agent_observability
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.enterprise.config import config_provider
from jiuwenswarm.enterprise.config.effective import db_queries
from jiuwenswarm.enterprise.config.effective.schemas import EffectiveEnterpriseConfig, RoutingContext
from jiuwenswarm.server.agent_http_routes import _rebuild_identity_from_headers
from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


@pytest.fixture(autouse=True)
def enterprise_edition(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")


@pytest.fixture(autouse=True)
def no_catalog_reads(monkeypatch):
    read = AsyncMock(side_effect=AssertionError("Trace naming must not query the catalog"))
    monkeypatch.setattr(db_queries, "list_records", read)
    yield
    read.assert_not_called()


@pytest.fixture
def tracer():
    provider = TracerProvider(shutdown_on_exit=False)
    try:
        yield provider.get_tracer(__name__)
    finally:
        provider.shutdown()


@contextmanager
def _root(tracer):
    with tracer.start_span("turn") as span, bind_root_span_context(span, session_id="session"):
        yield span


@contextmanager
def _sdk_root(tracer, session_id):
    with tracer.start_span("turn") as span:
        set_root_span(span, session_id=session_id)
        try:
            yield span
        finally:
            clear_root_span(session_id=session_id, expected_span=span)


def _adapter(adapter_type, card_name="main_agent", session_id="session"):
    adapter = adapter_type()
    adapter.mark_as_session_scoped(session_id)
    adapter._agent_name = card_name
    agent = DeepAgent(AgentCard(id="runtime-agent", name=card_name))
    for rail in adapter._instantiate_rails([], {}):
        agent.add_rail(rail)
    adapter._instance = agent
    trace_rail, = [rail for rail in agent.configured_rails() if isinstance(rail, AgentObservabilityRail)]
    return adapter, trace_rail


def _request(mode, streaming=False):
    return AgentRequest(
        request_id="request", channel_id="web", session_id="session",
        params={"query": "hello", "mode": mode},
        metadata={"routing": {"bot_id": "selected-bot"}},
        is_stream=streaming,
    )


def _manager_name(name, bot_id="selected-bot"):
    return {"_jiuwen_trace_agent_name": {"bot_id": bot_id, "name": name}}


def _policy(name, bot_id="selected-bot"):
    return EffectiveEnterpriseConfig(
        routing=RoutingContext(group_id="", bot_id=bot_id, user_id=""),
        resource_id=bot_id,
        agent_template={"template_name": name},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_type,mode", [
    (JiuWenSwarmDeepAdapter, "agent"), (JiuwenSwarmCodeAdapter, "code"),
])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("verified_name,policy,expected_name", [
    (None, _policy("配置模板"), "main_agent"),
    ("需求助手", _policy("配置模板"), "需求助手"),
    ("需求助手", None, "需求助手"),
])
async def test_first_turn_names_only_the_recording_trace(
    monkeypatch, tracer, adapter_type, mode, streaming, verified_name, policy, expected_name,
):
    adapter, rail = _adapter(adapter_type)
    request = _request(mode, streaming)
    if verified_name is not None:
        envelope = E2AEnvelope(
            request_id=request.request_id,
            channel=request.channel_id,
            session_id=request.session_id,
            method="chat.send",
            params=request.params,
            channel_context={**request.metadata, "ext": _manager_name(verified_name)},
        )
        rest = assemble_rest_request(envelope, base_url="http://agent.test")
        _, request.metadata = _rebuild_identity_from_headers(Request({
            "type": "http",
            "headers": [(key.lower().encode("ascii"), value.encode("ascii")) for key, value in rest.headers.items()],
        }))
    assert adapter._enterprise_config is None
    resolve = AsyncMock(return_value=config_provider.AgentConfigResult(config={}, policy=policy))
    monkeypatch.setattr(config_provider, "resolve_agent_config", resolve)
    await adapter._load_enterprise_config(request, base={})
    monkeypatch.setattr(adapter, "_has_valid_model_config", lambda name: True)
    monkeypatch.setattr(adapter, "_model_config_error", lambda _: None)
    monkeypatch.setattr(adapter, "_resolve_model_for_request", lambda _: None)
    monkeypatch.setattr(adapter, "_update_runtime_config", AsyncMock())
    monkeypatch.setattr(agent_observability, "sync_agent_observability", lambda **_: None)
    monkeypatch.setattr(otel_setup, "get_tracer", lambda name: tracer)

    class DispatchReached(BaseException):
        """Stop before dispatching the user input to a model."""

    async def dispatch(request, inputs):
        ctx = AgentCallbackContext(agent=adapter._instance, inputs=TaskIterationInputs(
            iteration=1, query="hello", loop_event=None,
        ))
        await rail.before_task_iteration(ctx)
        try:
            agent_span = get_current_agent_span()
            assert agent_span.name == f"agent.{expected_name}.task_iteration.1"
            assert agent_span.attributes["gen_ai.agent.name"] == expected_name
        finally:
            await rail.after_task_iteration(ctx)
        assert adapter._instance.card.name == adapter._agent_name == "main_agent"
        assert adapter._instance.card.id == "runtime-agent"
        scheduler = Context()
        assert scheduler.run(rail.resolve_agent_name, adapter._instance) == expected_name
        with agent_observability.main_agent_trace_turn(request.session_id):
            assert scheduler.run(rail.resolve_agent_name, adapter._instance) == "main_agent"
        raise DispatchReached

    monkeypatch.setattr(adapter, "_prepare_root_input_dispatch", dispatch)
    with _sdk_root(tracer, request.session_id) as span:
        def open_run(**kwargs):
            assert rail.resolve_agent_name(adapter._instance) == "main_agent"
            assert kwargs["session_id"] == "session"
            return span

        monkeypatch.setattr(observability, "open_agent_run_span", open_run)
        monkeypatch.setattr(observability, "close_agent_run_span", lambda *args, **kwargs: None)
        with pytest.raises(DispatchReached):
            if streaming:
                async for _ in adapter.process_message_stream_impl(request, {"query": "hello"}):
                    pass
            else:
                await adapter.process_message_impl(request, {"query": "hello"})

    resolve.assert_awaited_once_with({}, request=request, cached_policy=None)
    assert rail.resolve_agent_name(adapter._instance) == "main_agent"


def test_overlapping_turns_keep_separate_names_and_children_keep_their_cards(tracer):
    adapter, rail = _adapter(JiuWenSwarmDeepAdapter, "configured")
    request = _request("agent")
    request.metadata["ext"] = _manager_name("模板甲")

    with _root(tracer) as first:
        adapter._set_main_agent_trace_name(request, first)
        with _root(tracer) as second:
            request.metadata["ext"] = _manager_name("模板乙")
            adapter._set_main_agent_trace_name(request, second)
            assert rail.resolve_agent_name(adapter._instance) == "模板乙"
            child = DeepAgent(AgentCard(id="child", name="researcher"))
            assert rail.fork_for_agent().resolve_agent_name(child) == "researcher"
        assert rail.resolve_agent_name(adapter._instance) == "模板甲"
        assert adapter._instance.card.name == "configured"
    assert rail.resolve_agent_name(adapter._instance) == "configured"


@pytest.mark.parametrize("first_name,rebuild", [("模板甲", False), (None, True)])
def test_scheduler_callback_without_turn_context_does_not_take_another_turns_name(tracer, first_name, rebuild):
    first_adapter, first_rail = _adapter(JiuWenSwarmDeepAdapter, "configured")
    adapter, rail = _adapter(JiuWenSwarmDeepAdapter, "configured") if rebuild else (first_adapter, first_rail)
    scheduler = Context()

    def start_turn(stack, adapter, name, session_id="session"):
        request = _request("agent")
        request.session_id = session_id
        owner = Context()
        turn = ExitStack()
        stack.callback(owner.run, turn.close)
        owner.run(turn.enter_context, agent_observability.main_agent_trace_turn(session_id))
        span = owner.run(turn.enter_context, _sdk_root(tracer, session_id))
        if name is not None:
            request.metadata["ext"] = _manager_name(name)
        owner.run(adapter._set_main_agent_trace_name, request, span)
        return owner

    with ExitStack() as first, ExitStack() as second, ExitStack() as independent:
        first_owner = start_turn(first, first_adapter, first_name)
        assert scheduler.run(first_rail.resolve_agent_name, first_adapter._instance) == (first_name or "configured")
        owner = start_turn(second, adapter, "模板乙")
        assert first_owner.run(first_rail.resolve_agent_name, first_adapter._instance) == (first_name or "configured")
        assert owner.run(rail.resolve_agent_name, adapter._instance) == "模板乙"
        assert scheduler.run(rail.resolve_agent_name, adapter._instance) == "configured"
        other, other_rail = _adapter(JiuWenSwarmDeepAdapter, session_id="other-session")
        start_turn(independent, other, "独立模板", "other-session")
        assert scheduler.run(other_rail.resolve_agent_name, other._instance) == "独立模板"
        first.close()
        assert scheduler.run(rail.resolve_agent_name, adapter._instance) == "configured"
    with ExitStack() as next_turn:
        start_turn(next_turn, adapter, "模板丙")
        assert scheduler.run(rail.resolve_agent_name, adapter._instance) == "模板丙"


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_type,mode,streaming", [
    (JiuWenSwarmDeepAdapter, "agent", False), (JiuwenSwarmCodeAdapter, "code", True),
])
async def test_request_rejected_before_tracing_keeps_active_turn_identifiable(
    monkeypatch, tracer, adapter_type, mode, streaming,
):
    adapter, rail = _adapter(adapter_type)
    active = _request(mode)
    rejected = _request(mode, streaming)
    rejected.request_id = "rejected"
    scheduler = Context()

    def reject_before_trace(request):
        assert scheduler.run(rail.resolve_agent_name, adapter._instance) == "模板甲"
        return "MODEL_UNAVAILABLE", "model unavailable"

    monkeypatch.setattr(adapter, "_ensure_enterprise_models", AsyncMock())
    monkeypatch.setattr(adapter, "_model_config_error", reject_before_trace)
    active.metadata["ext"] = _manager_name("模板甲")
    with agent_observability.main_agent_trace_turn(active.session_id), _sdk_root(tracer, active.session_id) as span:
        adapter._set_main_agent_trace_name(active, span)
        if streaming:
            reply, = [chunk async for chunk in adapter.process_message_stream_impl(rejected, rejected.params)]
        else:
            reply = await adapter.process_message_impl(rejected, rejected.params)
        assert reply.payload["code"] == "MODEL_UNAVAILABLE"
        assert scheduler.run(rail.resolve_agent_name, adapter._instance) == "模板甲"


@pytest.mark.parametrize("failure", ["constructor", "missing_sdk_lookup"])
def test_template_rail_failure_keeps_native_observability(monkeypatch, failure):
    def fail_to_construct(*args, **kwargs):
        raise RuntimeError("cannot initialize the template name rail")

    if failure == "constructor":
        monkeypatch.setattr(agent_observability.MainAgentObservabilityRail, "__init__", fail_to_construct)
    else:
        monkeypatch.delattr(span_context, "get_bound_root_span")
    _, rail = _adapter(JiuWenSwarmDeepAdapter)
    assert type(rail) is AgentObservabilityRail


def test_rebuilt_agent_gets_observed_name_on_its_own_rail(tracer):
    adapter, old_rail = _adapter(JiuWenSwarmDeepAdapter)
    request = _request("agent")
    request.metadata["ext"] = _manager_name("模板甲")
    with _root(tracer) as first:
        adapter._set_main_agent_trace_name(request, first)
        assert old_rail.resolve_agent_name(adapter._instance) == "模板甲"
    rebuilt, new_rail = _adapter(JiuWenSwarmDeepAdapter)
    adapter._instance = rebuilt._instance
    with _root(tracer) as second:
        adapter._set_main_agent_trace_name(request, second)
        assert new_rail.resolve_agent_name(adapter._instance) == "模板甲"
        assert adapter._instance.card.name == "main_agent"


@pytest.mark.parametrize("run_span", [None, INVALID_SPAN])
def test_disabled_or_unsampled_trace_keeps_card_name(run_span):
    adapter, rail = _adapter(JiuWenSwarmDeepAdapter)
    request = _request("agent")
    request.metadata["ext"] = _manager_name("模板甲")
    adapter._set_main_agent_trace_name(request, run_span)
    assert rail.resolve_agent_name(adapter._instance) == adapter._instance.card.name == "main_agent"


@pytest.mark.parametrize("mode", ["agent", "code"])
def test_explicit_agent_definition_keeps_its_observed_name(tracer, mode):
    adapter_type = JiuwenSwarmCodeAdapter if mode == "code" else JiuWenSwarmDeepAdapter
    adapter, rail = _adapter(adapter_type, "expert")
    adapter._session_instance_agent_definition = {"name": "expert"}
    request = _request(mode)
    request.metadata["ext"] = _manager_name("已校验名称")
    with _root(tracer) as span:
        adapter._set_main_agent_trace_name(request, span)
        assert rail.resolve_agent_name(adapter._instance) == adapter._instance.card.name == "expert"


@pytest.mark.parametrize("params", [{"mode": "team"}, {"mode": "agent", "team": True}, {"mode": "agent.design"}])
def test_other_modes_keep_their_own_name(tracer, params):
    adapter, rail = _adapter(JiuWenSwarmDeepAdapter, "leader")
    request = _request("agent")
    request.params.update(params)
    request.metadata["ext"] = _manager_name("已校验名称")
    with _root(tracer) as span:
        adapter._set_main_agent_trace_name(request, span)
        assert rail.resolve_agent_name(adapter._instance) == adapter._instance.card.name == "leader"


@pytest.mark.parametrize("ext", [
    None,
    _manager_name(None),
    _manager_name(" "),
    _manager_name("a" * 129),
    _manager_name("另一个 Agent", bot_id="other-bot"),
])
def test_missing_or_invalid_manager_name_falls_back_to_runtime_name(tracer, ext):
    adapter, rail = _adapter(JiuwenSwarmCodeAdapter, "configured")
    request = _request("code")
    # A matching configuration template is present but is not a trace name source.
    adapter._enterprise_config = _policy("当前配置模板")
    request.metadata["ext"] = _manager_name("已校验名称")
    with _root(tracer) as previous:
        adapter._set_main_agent_trace_name(request, previous)
        assert rail.resolve_agent_name(adapter._instance) == "已校验名称"
    with _root(tracer) as current:
        request.metadata["ext"] = ext
        request.params.update({"bot_id": "selected-bot", "agent_name": "伪造名称"})
        adapter._set_main_agent_trace_name(request, current)
        assert rail.resolve_agent_name(adapter._instance) == adapter._instance.card.name == "configured"


@pytest.mark.parametrize("metadata", [None, {}, {"routing": {}}])
def test_missing_routing_metadata_ignores_identity_in_params(tracer, caplog, metadata):
    adapter, rail = _adapter(JiuWenSwarmDeepAdapter, "configured")
    request = _request("agent")
    request.params.update({"bot_id": "selected-bot", "agent_name": "伪造名称"})
    request.metadata = {**metadata, "ext": _manager_name("已校验名称")} if metadata is not None else None
    with _root(tracer) as span:
        caplog.clear()
        adapter._set_main_agent_trace_name(request, span)
        assert rail.resolve_agent_name(adapter._instance) == "configured"
        assert not any(record.exc_info for record in caplog.records)


def test_non_mapping_params_skip_trace_naming(tracer):
    adapter, rail = _adapter(JiuWenSwarmDeepAdapter, "configured")
    request = _request("agent")
    request.metadata["ext"] = _manager_name("已校验名称")
    request.params = ["invalid"]
    with _root(tracer) as span:
        adapter._set_main_agent_trace_name(request, span)
        assert rail.resolve_agent_name(adapter._instance) == "configured"
