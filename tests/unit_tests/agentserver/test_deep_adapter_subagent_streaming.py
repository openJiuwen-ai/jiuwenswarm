# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Subagents use streaming transport without changing TaskTool's final result."""

import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from openai import AsyncOpenAI
from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.llm.model_clients.openai_model_client import OpenAIModelClient
from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.foundation.tool.function.function import LocalFunction
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.rail.base import AgentRail
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.schema.config import SubAgentConfig
from openjiuwen.harness.tools.subagent.task_tool import TaskTool

from jiuwenswarm.common.invocation_context.model_trace import TraceAwareModel
from jiuwenswarm.llm_sse_patch import apply_openai_sse_stream_patch
# Import the desktop/Swarm adapter's runtime patches.
from jiuwenswarm.server.runtime.agent_adapter import interface_deep  # noqa: F401


@pytest.fixture(autouse=True)
def _capture_execution_logs(caplog):
    # Capture the ordinary agent-server sink, irrespective of root propagation.
    from jiuwenswarm.common.subagent_logging import logger

    # First LazyLogger resolution configures the sink and clears old handlers.
    getattr(logger, "info")
    log = logging.getLogger("server")
    previous_propagate = log.propagate
    log.propagate = False
    log.addHandler(caplog.handler)
    caplog.set_level("INFO", logger="server")
    try:
        yield
    finally:
        log.removeHandler(caplog.handler)
        log.propagate = previous_propagate


def _sse(*deltas, finish_reason="stop", gateway_format=False):
    frames = [
        {
            "id": "completion-test", "created": 1, "model": "test",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }
        for delta in deltas
    ]
    frames.append({
        "id": "completion-test", "created": 1, "model": "test",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    })
    if gateway_format:
        for frame in frames:
            choice = frame["choices"][0]
            delta = choice.pop("delta")
            choice["message"] = {
                "token_text": delta.get("content", ""),
                "reasoning_token_text": delta.get("reasoning_content", ""),
            }
    return "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n"


@pytest.fixture
def parent(tmp_path, monkeypatch):
    # The independent vision-capability probe is not part of task execution.
    monkeypatch.setattr("openjiuwen.harness.deep_agent.schedule_image_support_probe",
                        lambda model: None)
    model = TraceAwareModel(
        model_client_config=ModelClientConfig(
            client_provider="OpenAI", api_key="test-key", api_base="http://test.invalid/v1",
        ),
        model_config=ModelRequestConfig(model="test"),
    )
    return create_deep_agent(
        model=model, workspace=str(tmp_path), auto_create_workspace=False,
        add_general_purpose_agent=True, max_iterations=3,
        subagents=[SubAgentConfig(
            agent_card=AgentCard(name="specialist", description="test specialist"),
            system_prompt="Test specialist.",
            factory_kwargs={"auto_create_workspace": False},
        )],
    )


async def _delegate(parent, subagent_type):
    session = create_agent_session(session_id=parent.card.id, card=parent.card)
    await session.pre_run()
    tool = TaskTool(ToolCard(id="test-task", name="task_tool", description="test"), parent)
    try:
        return await tool.invoke(
            {"subagent_type": subagent_type, "task_description": "Return the test result."},
            session=session, tool_call_id="test-call",
        )
    finally:
        await session.post_run()


@pytest_asyncio.fixture
async def gateway(parent, monkeypatch):
    # Production startup installs this compatibility patch for the desktop gateway.
    monkeypatch.setattr(OpenAIModelClient, "_parse_stream_chunk", OpenAIModelClient._parse_stream_chunk)
    monkeypatch.setattr(OpenAIModelClient, "_sse_stream_patch_applied",
                        getattr(OpenAIModelClient, "_sse_stream_patch_applied", False), raising=False)
    apply_openai_sse_stream_patch()
    requests = []
    responses = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if not body.get("stream"):
            # Reproduce the gateway's HTTP 200 / empty non-streaming reply.
            return httpx.Response(200, json={
                "id": "empty", "created": 1, "model": "test", "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": ""},
                             "finish_reason": "stop"}],
            })
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=responses.pop(0),
        )

    async with AsyncOpenAI(
        api_key="test-key", base_url="http://test.invalid/v1", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as sdk:
        monkeypatch.setattr(parent._deep_config.model._client, "_create_async_openai_client",
                            lambda timeout=None: sdk)
        yield requests, responses


@pytest.mark.asyncio
@pytest.mark.parametrize("subagent_type", ["general-purpose", "specialist"])
@pytest.mark.parametrize("gateway_format", [False, True], ids=["openai", "desktop-gateway"])
async def test_task_tool_streams_and_returns_aggregated_text(parent, gateway, subagent_type, gateway_format):
    requests, responses = gateway
    messages = []

    class CaptureResponseRail(AgentRail):
        async def after_model_call(self, ctx):
            messages.append(ctx.inputs.response)

    spec = parent._find_subagent_spec(subagent_type)
    spec.rails = [*(spec.rails or []), CaptureResponseRail()]
    responses.append(_sse({"reasoning_content": "Think first."},
                          {"content": "Hello "}, {"content": "world"}, gateway_format=gateway_format))
    result = await _delegate(parent, subagent_type)
    assert requests and all(request["stream"] is True for request in requests)
    assert result.success is True
    assert result.data["output"] == "Hello world"
    assert result.data["agent_id"]
    assert messages[0].reasoning_content == "Think first."
    assert messages[0].usage_metadata.total_tokens == 12


@pytest.mark.asyncio
async def test_task_tool_executes_fragmented_tool_call_then_streams_answer(parent, gateway):
    requests, responses = gateway
    calls = []

    def echo(text):
        calls.append(text)
        return "tool result: " + text

    parent._find_subagent_spec("specialist").tools = [LocalFunction(
        ToolCard(id="test-echo", name="echo", description="Echo text.", input_params={
            "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
        }), echo,
    )]
    responses.extend([
        _sse(
            {"tool_calls": [{"index": 0, "id": "call-echo", "type": "function",
                             "function": {"name": "echo", "arguments": '{"text":"'}}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": 'hello"}'}}]},
            finish_reason="tool_calls",
        ),
        _sse({"content": "Done: "}, {"content": "hello"}),
    ])
    result = await _delegate(parent, "specialist")
    assert len(requests) == 2
    assert all(request["stream"] is True for request in requests)
    assert calls == ["hello"]
    assert any(message["role"] == "tool" and "tool result: hello" in message["content"]
               for message in requests[1]["messages"])
    assert result.success is True
    assert result.data["output"] == "Done: hello"


@pytest.mark.asyncio
async def test_stream_error_is_not_reported_as_successful_empty_output(parent, gateway):
    requests, responses = gateway
    # Native LLMRetryRail retries twice; all attempts must preserve the upstream error.
    responses.extend(['data: {"error":{"message":"gateway unavailable","type":"server_error"}}\n\n'] * 3)
    with pytest.raises(Exception, match="gateway unavailable"):
        await _delegate(parent, "specialist")
    assert requests and all(request["stream"] is True for request in requests)


@pytest.mark.asyncio
async def test_task_cancellation_propagates_to_stream_request(parent, monkeypatch, caplog):
    caplog.set_level("INFO")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def respond(request):
        assert json.loads(request.content)["stream"] is True
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async with AsyncOpenAI(
        api_key="test-key", base_url="http://test.invalid/v1", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as sdk:
        monkeypatch.setattr(parent._deep_config.model._client, "_create_async_openai_client",
                            lambda timeout=None: sdk)
        task = asyncio.create_task(_delegate(parent, "specialist"))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert cancelled.is_set()
    events = _execution_events(caplog)
    assert events[-1]["event"] == "dispatch.end" and events[-1]["status"] == "cancelled"
    assert any(e["event"] == "model.end" and e["status"] == "cancelled" for e in events)
    from jiuwenswarm.common.subagent_logging import current_subagent_trace

    assert current_subagent_trace() is None


@pytest.mark.asyncio
async def test_subagent_streaming_does_not_change_parent_invoke(parent, gateway):
    requests, responses = gateway
    responses.append(_sse({"content": "child result"}))
    model = parent._deep_config.model
    await _delegate(parent, "specialist")
    await parent.invoke({"query": "Main agent.", "conversation_id": parent.card.id})
    assert [request["stream"] for request in requests] == [True, False]
    assert parent._deep_config.model is model
    assert parent._react_agent._get_llm() is model


def test_reused_subagent_and_runtime_patch_are_idempotent(parent):
    child = parent.create_subagent("specialist", "first")
    parent._deep_config.subagents = [child]
    interface_deep._jws_install_subagent_runtime_patch()
    assert parent.create_subagent("specialist", "second") is child
    assert parent.create_subagent("specialist", "third") is child
    assert len(child.find_rails_by_type((interface_deep._SubagentStreamingRail,))) == 1


def _execution_events(caplog):
    prefix = "[SubagentExecution] "
    return [json.loads(record.getMessage()[len(prefix):])
            for record in caplog.records if record.getMessage().startswith(prefix)]


@pytest.mark.asyncio
async def test_normal_delegation_logs_actual_model_tools_and_raw_result(parent, gateway, caplog):
    caplog.set_level("INFO")
    requests, responses = gateway
    parent._find_subagent_spec("specialist").tools = [LocalFunction(
        ToolCard(id="logged-echo", name="echo", description="Echo.", input_params={
            "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
        }), lambda text: "private tool output: " + text,
    )]
    responses.extend([
        _sse({"tool_calls": [{"index": 0, "id": "call-logged-echo", "type": "function",
                              "function": {"name": "echo", "arguments": '{"text":"private argument"}'}}]},
             finish_reason="tool_calls"),
        _sse({"content": "private final answer"}),
    ])
    result = await _delegate(parent, "specialist")
    events = _execution_events(caplog)
    assert events, "normal TaskTool execution must emit correlated execution events"
    assert result.data["output"] == "private final answer"
    assert len(requests) == 2
    assert len({event["dispatch_id"] for event in events}) == 1
    assert all(event["parent_session_id"] == parent.card.id for event in events)
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    model_ends = [event for event in events if event["event"] == "model.end"]
    assert len(model_ends) == 2
    assert all(event["model"] == "test" and event["streaming"] for event in model_ends)
    assert all(event["duration_ms"] >= 0 and event["first_chunk_ms"] >= 0 for event in model_ends)
    tool_ends = [event for event in events if event["event"] == "tool.end"]
    assert len(tool_ends) == 1
    assert tool_ends[0]["tool_call_id"] == "call-logged-echo"
    assert tool_ends[0]["tool_name"] == "echo"
    assert tool_ends[0]["execution_started"] is True
    assert events[-1]["event"] == "dispatch.end"
    assert events[-1]["raw_result_type"] == "answer"
    assert events[-1]["model_calls"] == 2
    assert events[-1]["tool_calls"] == 1
    assert events[-1]["post_invoke_ms"] >= 0
    assert not any("private" in json.dumps(event) for event in events)


@pytest.mark.asyncio
async def test_iteration_limit_is_logged_without_changing_task_tool_result(parent, gateway, caplog):
    caplog.set_level("INFO")
    _, responses = gateway
    parent._find_subagent_spec("specialist").max_iterations = 1
    parent._find_subagent_spec("specialist").tools = [LocalFunction(
        ToolCard(id="limit-echo", name="echo", description="Echo.", input_params={"type": "object"}),
        lambda: "done",
    )]
    responses.append(_sse({"tool_calls": [{"index": 0, "id": "limit-call", "type": "function",
                                           "function": {"name": "echo", "arguments": "{}"}}]},
                          finish_reason="tool_calls"))
    result = await _delegate(parent, "specialist")
    # F01 is deliberately unchanged: observe the raw error before TaskTool wraps it.
    assert result.success is True
    assert "Max iterations" in result.data["output"]
    ends = [e for e in _execution_events(caplog) if e["event"] == "dispatch.end"]
    assert ends and ends[-1]["raw_result_type"] == "error"


@pytest.mark.asyncio
async def test_failed_gateway_logs_retry_attempts_without_logging_error_body(parent, gateway, caplog):
    caplog.set_level("INFO")
    _, responses = gateway
    responses.extend(['data: {"error":{"message":"private upstream error","type":"server_error"}}\n\n'] * 3)
    with pytest.raises(Exception, match="private upstream error"):
        await _delegate(parent, "specialist")
    events = _execution_events(caplog)
    ends = [e for e in events if e["event"] == "model.end"]
    assert len(ends) == 3
    assert all(e["status"] == "error" and e["exception_type"] for e in ends)
    assert events[-1]["event"] == "dispatch.end"
    assert events[-1]["status"] == "error"
    assert not any("private upstream error" in json.dumps(e) for e in events)


@pytest.mark.asyncio
async def test_approval_resume_keeps_child_identity_and_logs_interruption(parent, gateway, caplog):
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
    from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
    from openjiuwen.harness.rails.interrupt.interrupt_base import BaseInterruptRail

    caplog.set_level("INFO")
    _, responses = gateway
    calls = []

    class ApprovalRail(AgentRail):
        allowed = False

        async def before_tool_call(self, ctx):
            if not self.allowed:
                BaseInterruptRail()._raise_interrupt(
                    ctx.inputs.tool_name, ctx.inputs.tool_call, InterruptRequest(message="private approval"),
                )

    approval_rail = ApprovalRail()
    spec = parent._find_subagent_spec("specialist")
    spec.rails = [approval_rail]
    spec.tools = [LocalFunction(
        ToolCard(id="approve-echo", name="echo", description="Echo.", input_params={"type": "object"}),
        lambda: calls.append("executed") or "done",
    )]
    responses.extend([
        _sse({"tool_calls": [{"index": 0, "id": "approval-call", "type": "function",
                              "function": {"name": "echo", "arguments": "{}"}}]},
             finish_reason="tool_calls"),
        _sse({"content": "approved result"}),
    ])
    session = create_agent_session(session_id=parent.card.id, card=parent.card)
    await session.pre_run()
    tool = TaskTool(ToolCard(id="approval-task", name="task_tool", description="test"), parent)
    inputs = {"subagent_type": "specialist", "task_description": "test"}
    try:
        first = await tool.invoke(inputs, session=session, tool_call_id="same-outer-call")
        assert first["result_type"] == "interrupt" and not calls
        ends = [e for e in _execution_events(caplog) if e["event"] == "tool.end"]
        assert len(ends) == 1 and ends[0]["status"] == "interrupted"
        assert ends[0]["execution_started"] is False
        approval_rail.allowed = True
        approval = InteractiveInput()
        approval.update(first["interrupt_ids"][0], {"approved": True})
        result = await tool.invoke({**inputs, "query": approval}, session=session, tool_call_id="same-outer-call")
        assert result.data["output"] == "approved result" and calls == ["executed"]
        dispatches = [e for e in _execution_events(caplog) if e["event"] == "dispatch.end"]
        assert [e["status"] for e in dispatches] == ["interrupted", "completed"]
        assert len({e["child_session_id"] for e in dispatches}) == 1
        assert all(e["parent_tool_call_id"] == "same-outer-call" for e in dispatches)
        assert [e["model_calls"] for e in dispatches] == [1, 1]
        model_ends = [e for e in _execution_events(caplog) if e["event"] == "model.end"]
        assert [e["dispatch_id"] for e in model_ends] == [e["dispatch_id"] for e in dispatches]
    finally:
        await session.post_run()


@pytest.mark.asyncio
async def test_parallel_dispatches_keep_model_events_in_their_own_task(parent, monkeypatch, caplog):
    caplog.set_level("INFO")
    both_started = asyncio.Event()
    seen = []

    async def respond(request):
        query = json.loads(request.content)["messages"][-1]["content"]
        seen.append(query)
        if len(seen) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=5)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text=_sse({"content": "answer " + query}))

    async with AsyncOpenAI(
        api_key="test-key", base_url="http://test.invalid/v1", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as sdk:
        monkeypatch.setattr(parent._deep_config.model._client, "_create_async_openai_client",
                            lambda timeout=None: sdk)
        session = create_agent_session(session_id=parent.card.id, card=parent.card)
        await session.pre_run()
        tool = TaskTool(ToolCard(id="parallel-task", name="task_tool", description="test"), parent)
        try:
            results = await asyncio.gather(*(
                tool.invoke({"subagent_type": "specialist", "task_description": name},
                            session=session, tool_call_id=name)
                for name in ("outer-A", "outer-B")
            ))
        finally:
            await session.post_run()
    assert [result.data["output"] for result in results] == ["answer outer-A", "answer outer-B"]
    events = _execution_events(caplog)
    ends = [e for e in events if e["event"] == "dispatch.end"]
    assert len(ends) == 2 and len({e["dispatch_id"] for e in ends}) == 2
    assert len({e["child_session_id"] for e in ends}) == 2
    for end in ends:
        group = [e for e in events if e["dispatch_id"] == end["dispatch_id"]]
        assert all(e["parent_tool_call_id"] == end["parent_tool_call_id"] for e in group)
        assert len([e for e in group if e["event"] == "model.end"]) == 1
    from jiuwenswarm.common.subagent_logging import current_subagent_trace

    assert current_subagent_trace() is None


@pytest.mark.asyncio
async def test_logging_sink_failure_does_not_change_delegation(parent, gateway, monkeypatch):
    from jiuwenswarm.common import subagent_logging

    _, responses = gateway
    responses.append(_sse({"content": "original result"}))

    def broken_sink(*args, **kwargs):
        raise RuntimeError("log sink unavailable")

    monkeypatch.setattr(subagent_logging, "logger", SimpleNamespace(info=broken_sink))
    result = await _delegate(parent, "specialist")
    assert result.data["output"] == "original result"


@pytest.mark.asyncio
async def test_logging_preserves_sdk_invalid_session_error(parent):
    tool = TaskTool(ToolCard(id="invalid-task", name="task_tool", description="test"), parent)
    with pytest.raises(Exception, match="TaskTool requires a valid session"):
        await tool.invoke({"subagent_type": "specialist", "task_description": "test"}, session=object())


@pytest.mark.asyncio
async def test_cancellation_closes_inflight_tool_log(parent, gateway, caplog):
    caplog.set_level("INFO")
    _, responses = gateway
    started = asyncio.Event()

    async def wait_tool():
        started.set()
        await asyncio.Event().wait()

    parent._find_subagent_spec("specialist").tools = [LocalFunction(
        ToolCard(id="waiting-tool", name="wait_tool", description="Wait.",
                 input_params={"type": "object"}), wait_tool,
    )]
    responses.append(_sse({"tool_calls": [{"index": 0, "id": "waiting-call", "type": "function",
                                           "function": {"name": "wait_tool", "arguments": "{}"}}]},
                          finish_reason="tool_calls"))
    task = asyncio.create_task(_delegate(parent, "specialist"))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    ends = [e for e in _execution_events(caplog) if e["event"] == "tool.end"]
    assert len(ends) == 1 and ends[0]["status"] == "cancelled"
    assert ends[0]["tool_call_id"] == "waiting-call" and ends[0]["execution_started"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("positional", [False, True])
async def test_model_log_matches_actual_request_override_and_message_count(parent, gateway, caplog, positional):
    from jiuwenswarm.common.subagent_logging import SubagentTrace, _TRACE

    caplog.set_level("INFO")
    requests, responses = gateway
    responses.append(_sse({"content": "result"}))
    messages = [{"role": "user", "content": "private message"}]
    token = _TRACE.set(SubagentTrace("parent", "outer-call", "specialist"))
    try:
        model = parent._deep_config.model
        stream = (model.stream(messages, model="request-override") if positional else
                  model.stream(messages=messages, model="request-override"))
        chunks = [chunk async for chunk in stream]
    finally:
        _TRACE.reset(token)
    assert chunks and requests[0]["model"] == "request-override"
    ends = [e for e in _execution_events(caplog) if e["event"] == "model.end"]
    assert len(ends) == 1 and ends[0]["model"] == "request-override"
    assert ends[0]["message_count"] == 1


@pytest.mark.asyncio
async def test_normal_delegation_reaches_ordinary_file_sink(parent, gateway, tmp_path):
    _, responses = gateway
    responses.append(_sse({"content": "file sink result"}))
    path = tmp_path / "ordinary.log"
    sink = logging.FileHandler(path, encoding="utf-8")
    sink.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("server")
    log.addHandler(sink)
    try:
        result = await _delegate(parent, "specialist")
    finally:
        log.removeHandler(sink)
        sink.close()
    assert result.data["output"] == "file sink result"
    prefix = "[SubagentExecution] "
    events = [json.loads(line[len(prefix):]) for line in path.read_text(encoding="utf-8").splitlines()
              if line.startswith(prefix)]
    assert events[0]["event"] == "dispatch.start"
    assert events[-1]["event"] == "dispatch.end" and events[-1]["status"] == "completed"
    assert len({e["dispatch_id"] for e in events}) == 1
    assert any(e["event"] == "model.end" and e["model"] == "test" for e in events)
