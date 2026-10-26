# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Steering stays bound to the existing owner and never opens another stream."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter.steering import SteeringSession


def run_request(**kwargs):
    defaults = dict(
        request_id="run-1",
        channel_id="officeclaw",
        session_id="session-1",
        agent_id="agent-1",
        service_id="service-1",
        workspace_key="workspace-1",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent", "invocation_id": "invocation-1"},
    )
    defaults.update(kwargs)
    return AgentRequest(**defaults)


def control_request(*, query=False, **params):
    defaults = dict(
        session_id="session-1",
        invocation_id="invocation-1",
        active_request_id="run-1",
        input_id="input-1",
        client_message_id="client-1",
        content="Only analyse East China",
    )
    if query:
        defaults.pop("client_message_id")
        defaults.pop("content")
    defaults.update(params)
    return replace(
        run_request(),
        request_id="control-1",
        req_method=ReqMethod.CHAT_STEER_STATUS if query else ReqMethod.CHAT_STEER,
        params=defaults,
    )


def runtime():
    receipts = {}

    async def steer(**kwargs):
        receipt = {"input_id": kwargs["input_id"], "status": "accepted"}
        receipts[kwargs["input_id"]] = receipt
        return receipt

    obj = SimpleNamespace(
        steer_active=AsyncMock(side_effect=steer),
        get_steering_capability=AsyncMock(return_value={"supported": True}),
        get_active_steering_request_id=MagicMock(return_value="leader-round-1"),
        get_steering_status=AsyncMock(
            side_effect=lambda **kw: dict(receipts[kw["input_id"]])
        ),
        attach_output=AsyncMock(),
        send_input=AsyncMock(),
        abort=AsyncMock(),
        receipts=receipts,
    )
    return obj


def session(mode="agent"):
    agent = runtime()
    resolver = AsyncMock(return_value=agent)
    control = SteeringSession(resolver)
    binding = control.bind_request(
        run_request(params={"mode": mode, "invocation_id": "invocation-1"})
    )
    return control, binding, agent, resolver


@pytest.mark.asyncio
async def test_control_reuses_owner_without_new_reader_or_input_dispatch():
    control, _, agent, _ = session()
    result = await control.handle(control_request(), query=False)
    assert result["status"] == "accepted"
    agent.steer_active.assert_awaited_once_with(
        active_request_id="run-1", input_id="input-1", content="Only analyse East China"
    )
    agent.attach_output.assert_not_awaited()
    agent.send_input.assert_not_awaited()
    agent.abort.assert_not_awaited()


@pytest.mark.asyncio
async def test_retries_return_current_receipt_and_conflicting_content_is_rejected():
    control, _, agent, _ = session()
    request = control_request()
    await control.handle(request, query=False)
    agent.receipts["input-1"]["status"] = "consumed"
    assert (await control.handle(request, query=False))["status"] == "consumed"
    result = await control.handle(control_request(content="Different"), query=False)
    assert result["reason"] == "IDEMPOTENCY_CONFLICT"
    assert agent.steer_active.await_count == 1
    result = await control.handle(control_request(input_id="input-2"), query=False)
    assert result["reason"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_concurrent_retries_are_enqueued_once():
    control, _, agent, _ = session()
    result = await asyncio.gather(
        *(control.handle(control_request(), query=False) for _ in range(5))
    )
    assert all(item["status"] == "accepted" for item in result)
    assert agent.steer_active.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("agent_id", "other"),
        ("service_id", "other"),
        ("workspace_key", "other"),
        ("channel_id", "web"),
        ("session_id", "other"),
    ],
)
async def test_other_identity_never_reaches_existing_runtime(field, value):
    control, _, agent, resolver = session()
    request = replace(control_request(), **{field: value})
    assert not control.owns(request)
    assert (await control.handle(request, query=False))["reason"] == "RUN_NOT_ACTIVE"
    agent.steer_active.assert_not_awaited()
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_rerun_and_wrong_invocation_do_not_receive_late_inputs():
    control, _, agent, _ = session()
    result = await control.handle(control_request(invocation_id="other"), query=False)
    assert result["reason"] == "RUN_NOT_ACTIVE"
    control.bind_request(run_request(request_id="run-2"))
    result = await control.handle(control_request(), query=False)
    assert result["reason"] == "RUN_NOT_ACTIVE"
    agent.steer_active.assert_not_awaited()


@pytest.mark.asyncio
async def test_final_receipts_survive_original_stream_closing():
    control, binding, agent, _ = session()
    await control.handle(control_request(), query=False)
    agent.receipts["input-1"]["status"] = "consumed"
    await control.finish_request(binding)
    assert binding.inputs["input-1"].runtime is None
    result = await control.handle(control_request(query=True), query=True)
    assert result["status"] == "consumed"
    assert (
        await control.handle(
            control_request(input_id="late", client_message_id="late"), query=False
        )
    )["reason"] == "RUN_NOT_ACTIVE"


@pytest.mark.asyncio
async def test_missing_consumption_evidence_after_finish_becomes_unknown():
    control, binding, _, _ = session()
    await control.handle(control_request(), query=False)
    await control.finish_request(binding)
    assert (await control.handle(control_request(query=True), query=True))[
        "status"
    ] == "unknown"


@pytest.mark.asyncio
async def test_team_literal_text_targets_leader_and_retains_original_round_for_query():
    control, _, agent, resolver = session(mode="team")
    await control.handle(control_request(content="@member #normal $text"), query=False)
    resolver.assert_awaited_once_with("team", "officeclaw")
    agent.steer_active.assert_awaited_once_with(
        active_request_id="leader-round-1",
        input_id="input-1",
        content="@member #normal $text",
    )
    agent.get_active_steering_request_id.return_value = "leader-round-2"
    await control.handle(control_request(query=True), query=True)
    agent.get_steering_status.assert_awaited_with(
        active_request_id="leader-round-1", input_id="input-1"
    )


@pytest.mark.asyncio
async def test_waiting_permission_is_not_resumed_and_capability_is_nonmutating():
    control, _, agent, _ = session()
    agent.get_steering_capability.return_value = {
        "supported": False,
        "reason": "waiting_input",
    }
    result = await control.handle(control_request(input_id="", query=True), query=True)
    assert result == {
        "supported": False,
        "reason": "RUN_WAITING_INPUT",
        "target": "single",
    }
    assert (await control.handle(control_request(), query=False))[
        "reason"
    ] == "RUN_WAITING_INPUT"
    agent.send_input.assert_not_awaited()
    agent.steer_active.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["flash", "deepresearch", "swarmflow", "skill_turbo"])
async def test_unverified_modes_remain_unsupported(mode):
    control, _, agent, resolver = session(mode=mode)
    assert (await control.handle(control_request(), query=False))[
        "reason"
    ] == "STEER_UNSUPPORTED"
    agent.steer_active.assert_not_awaited()
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_admission_checks_runtime_again_after_capability_race():
    control, _, agent, _ = session()
    agent.steer_active.return_value = None
    agent.steer_active.side_effect = lambda **kw: {
        "input_id": kw["input_id"],
        "status": "not_applied",
        "reason": "not_active",
    }
    assert (await control.handle(control_request(), query=False))[
        "reason"
    ] == "RUN_NOT_ACTIVE"


@pytest.mark.asyncio
async def test_ambiguous_delivery_keeps_same_id_queryable():
    control, _, agent, _ = session()
    agent.steer_active.side_effect = RuntimeError("lost ACK")
    agent.get_steering_status.side_effect = lambda **kw: {
        "input_id": kw["input_id"],
        "status": "consumed",
    }
    assert (await control.handle(control_request(), query=False))["status"] == "unknown"
    assert (await control.handle(control_request(), query=False))[
        "status"
    ] == "consumed"
    agent.steer_active.assert_awaited_once()


@pytest.mark.asyncio
async def test_handler_pure_tenant_lookup_and_ack_uses_control_id(monkeypatch):
    from jiuwenswarm.server.handlers import steering

    owner = SimpleNamespace(
        owns_steering_request=lambda request: True,
        process_steering=AsyncMock(
            return_value={"input_id": "input-1", "status": "accepted"}
        ),
    )
    manager = SimpleNamespace(iter_jiuwenswarm_instances=lambda: [owner])
    pool = SimpleNamespace(
        get_cached_agent_manager=AsyncMock(return_value=manager),
        get_agent_manager=AsyncMock(),
    )
    ctx = SimpleNamespace(
        request=control_request(),
        sink=SimpleNamespace(send_wire=AsyncMock()),
        services=SimpleNamespace(agent_manager=object(), tenant_pool=lambda: pool),
    )
    monkeypatch.setattr(
        steering, "encode_agent_response_for_wire", lambda response, **kw: response
    )
    await steering.handle_chat_steering(ctx)
    response = ctx.sink.send_wire.await_args.args[0]
    assert response.request_id == "control-1"
    assert response.payload["active_request_id"] == "run-1"
    assert response.ok is True
    pool.get_cached_agent_manager.assert_awaited_once_with(
        "agent-1", "service-1", "workspace-1"
    )
    pool.get_agent_manager.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,reason",
    [
        ({"attachments": [{"path": "x"}]}, "CONTENT_UNSUPPORTED"),
        ({"attachments": []}, "CONTENT_UNSUPPORTED"),
        ({"images": None}, "CONTENT_UNSUPPORTED"),
        ({"model": "other"}, "INVALID_REQUEST"),
        ({"workspace": "other"}, "INVALID_REQUEST"),
        ({"content": " "}, "CONTENT_UNSUPPORTED"),
        ({"content": "x" * 32001}, "CONTENT_UNSUPPORTED"),
        ({"active_request_id": ""}, "INVALID_REQUEST"),
        ({"session_id": "other"}, "INVALID_REQUEST"),
    ],
)
async def test_invalid_control_payload_does_not_lookup_or_create_runtime(
    monkeypatch, change, reason
):
    from jiuwenswarm.server.handlers import steering

    ctx = SimpleNamespace(
        request=control_request(**change),
        services=object(),
        sink=SimpleNamespace(send_wire=AsyncMock()),
    )
    monkeypatch.setattr(
        steering, "encode_agent_response_for_wire", lambda response, **kw: response
    )
    await steering.handle_chat_steering(ctx)
    response = ctx.sink.send_wire.await_args.args[0]
    assert response.ok is False
    assert response.payload["error_code"] == "E2A.AGENT_ERROR"
    assert response.payload["code"] == response.payload["reason"] == reason


@pytest.mark.asyncio
async def test_unknown_session_query_does_not_allocate_agent_manager(monkeypatch):
    from jiuwenswarm.server.handlers import steering

    from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool

    pool = TenantAgentPool()
    monkeypatch.setattr(pool, "_ensure_agent_manager", AsyncMock(
        side_effect=AssertionError("A missing owner must not create a manager")
    ))
    ctx = SimpleNamespace(
        request=control_request(query=True),
        sink=SimpleNamespace(send_wire=AsyncMock()),
        services=SimpleNamespace(agent_manager=object(), tenant_pool=lambda: pool),
    )
    monkeypatch.setattr(
        steering, "encode_agent_response_for_wire", lambda response, **kw: response
    )
    await steering.handle_chat_steering(ctx)
    assert ctx.sink.send_wire.await_args.args[0].payload["status"] == "unknown"
    pool._ensure_agent_manager.assert_not_awaited()
    assert len(pool._agent_wrappers) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["complete", "error", "cancel"])
async def test_actual_adapter_stream_finalizes_binding_when_original_stream_ends(outcome):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True
    adapter._parent_session_id = "session-1"
    adapter._instance = runtime()
    started = asyncio.Event()
    finish = asyncio.Event()

    async def chunks(request, inputs):
        started.set()
        await finish.wait()
        if outcome == "error":
            raise RuntimeError("stream failed")
        yield SimpleNamespace(payload={"content": "original output"})

    adapter._process_message_stream_impl = chunks
    outputs = []

    async def consume():
        async for chunk in adapter.process_message_stream_impl(
            run_request(), {"query": "original"}
        ):
            outputs.append(chunk.payload)

    task = asyncio.create_task(consume())
    await started.wait()
    assert adapter.owns_steering_request(control_request())
    result = await adapter.process_steering(control_request(), query=False)
    assert result["status"] == "accepted"
    assert not task.done()
    adapter._instance.receipts["input-1"]["status"] = "consumed"
    if outcome == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        finish.set()
        if outcome == "error":
            with pytest.raises(RuntimeError, match="stream failed"):
                await task
        else:
            await task
    expected = [{"content": "original output"}] if outcome == "complete" else []
    assert outputs == expected
    assert (await adapter.process_steering(control_request(query=True), query=True))[
        "status"
    ] == "consumed"
    assert await adapter.process_steering(
        control_request(query=True, input_id=""), query=True
    ) == {"supported": False, "reason": "RUN_NOT_ACTIVE"}
    late = control_request(input_id="late", client_message_id="late")
    assert (await adapter.process_steering(late, query=False))["reason"] == "RUN_NOT_ACTIVE"


@pytest.mark.asyncio
async def test_hosted_subagent_wait_rejects_text_without_resolving_approval(
    monkeypatch,
):
    from openjiuwen.harness.security.skill_authorization.subagent_approval_registry import (
        SubagentApprovalRegistry,
    )

    approvals = SimpleNamespace(
        pending_requests=lambda: [SimpleNamespace(session_id="session-1")]
    )
    monkeypatch.setattr(SubagentApprovalRegistry, "peek_instance", lambda: approvals)
    control, _, agent, resolver = session()
    result = await control.handle(control_request(), query=False)
    assert result["reason"] == "RUN_WAITING_INPUT"
    agent.steer_active.assert_not_awaited()
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_team_runtime_manager_is_not_created(monkeypatch):
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager
    from openjiuwen.core.runner import runner

    fake_runner = SimpleNamespace()
    monkeypatch.setattr(runner, "GLOBAL_RUNNER", fake_runner)
    manager = TeamManager.__new__(TeamManager)
    manager._active_team_names = {"session-1": "team-1"}
    assert await manager.get_active_steering_leader("session-1") is None
    assert not hasattr(fake_runner, "_team_runtime_manager")


@pytest.mark.asyncio
async def test_gateway_steering_never_runs_chat_hooks_or_cancels_original_stream():
    from jiuwenswarm.common.schema.message import Message
    from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler

    message = Message(
        id="control-1",
        type="req",
        channel_id="web",
        session_id="session-1",
        params=control_request().params,
        timestamp=0,
        ok=True,
        req_method=ReqMethod.CHAT_STEER,
        is_stream=True,
    )
    handler = SimpleNamespace(_running=True)

    async def consume(**kwargs):
        handler._running = False
        return message

    handler.consume_user_messages = consume
    handler._prepare_agent_dispatch_message = AsyncMock(side_effect=lambda msg: msg)
    handler.message_to_e2a = lambda msg: SimpleNamespace(is_stream=msg.is_stream)
    handler._process_non_stream_request = AsyncMock()
    handler._is_external_channel_cancel = lambda msg: False
    handler._handle_channel_control = AsyncMock()
    handler._cancel_stream_tasks_for_channel = AsyncMock()
    await MessageHandler._forward_loop(handler)
    handler._process_non_stream_request.assert_awaited_once()
    control_msg, envelope = handler._process_non_stream_request.await_args.args
    assert control_msg.id == "control-1"
    assert envelope.is_stream is False
    handler._handle_channel_control.assert_not_awaited()
    handler._cancel_stream_tasks_for_channel.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source,approval_id",
    [
        ("subagent_skill_load", "approval-1"),
        ("subagent_tool_permission", "approval-1"),
        ("", "subagent_skill_load_legacy"),
        ("", "subagent_tool_permission_legacy"),
    ],
)
async def test_approval_chat_send_does_not_replace_original_steering_binding(
    source, approval_id
):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True
    adapter._parent_session_id = "session-1"
    adapter._instance = runtime()
    adapter._steering_control = SteeringSession(adapter._resolve_steering_runtime)
    original = adapter._steering_control.bind_request(run_request())

    async def approval_ack(*args):
        yield SimpleNamespace(payload={"event_type": "runtime.accepted"})

    adapter._process_message_stream_impl = approval_ack
    request = run_request(
        request_id="approval-wire",
        params={
            "mode": "agent",
            "source": source,
            "request_id": approval_id,
            "answers": [{"value": "approve"}],
        },
    )
    async for _ in adapter.process_message_stream_impl(request, {"query": "approval"}):
        pass
    assert adapter._steering_control._active is original
    assert (await adapter.process_steering(control_request(), query=False))[
        "status"
    ] == "accepted"


@pytest.mark.asyncio
async def test_unsupported_parameters_use_existing_e2a_error_classification():
    from jiuwenswarm.server.handlers.steering import handle_chat_steering

    ctx = SimpleNamespace(
        request=control_request(model="other"),
        services=object(),
        sink=SimpleNamespace(send_wire=AsyncMock()),
    )
    await handle_chat_steering(ctx)
    wire = ctx.sink.send_wire.await_args.args[0]
    assert wire["body"]["code"] == "E2A.AGENT_ERROR"
    assert wire["body"]["details"]["reason"] == "INVALID_REQUEST"
    assert wire["body"]["details"]["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize("method", [ReqMethod.CHAT_STEER, ReqMethod.CHAT_STEER_STATUS])
def test_gateway_keeps_control_parameters_unchanged(method):
    from jiuwenswarm.common.schema.message import Message
    from jiuwenswarm.gateway.app_gateway import _normalize_gateway_message

    params = control_request(query=method == ReqMethod.CHAT_STEER_STATUS).params
    msg = Message(
        id="control",
        type="req",
        channel_id="web",
        session_id="session-1",
        params=params,
        timestamp=0,
        ok=True,
        req_method=method,
    )
    assert _normalize_gateway_message(msg).params == params


@pytest.mark.asyncio
@pytest.mark.parametrize("known_alias", [False, True])
async def test_acp_control_only_resolves_existing_alias(known_alias):
    from jiuwenswarm.common.schema.message import Message
    from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler

    params = control_request().params
    msg = Message(
        id="control",
        type="req",
        channel_id="acp",
        session_id="session-1",
        params=params,
        timestamp=0,
        ok=True,
        req_method=ReqMethod.CHAT_STEER,
    )
    handler = SimpleNamespace(
        _acp_session_aliases={"session-1": "internal"} if known_alias else {},
        _attach_original_request_to_ask_user_answer=MagicMock(),
        _resolve_acp_internal_session_id=AsyncMock(),
    )
    result = await MessageHandler._prepare_agent_dispatch_message(handler, msg)
    assert result.session_id == ("internal" if known_alias else "session-1")
    handler._attach_original_request_to_ask_user_answer.assert_not_called()
    handler._resolve_acp_internal_session_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_cannot_claim_an_unbound_invocation():
    control, _, agent, _ = session()
    original = control.bind_request(run_request(params={"mode": "agent"}))
    result = await control.handle(control_request(query=True, input_id=""), query=True)
    assert result == {"supported": False, "reason": "RUN_NOT_ACTIVE"}
    assert original.invocation_id == ""
    result = await control.handle(control_request(), query=False)
    assert result["reason"] == "RUN_NOT_ACTIVE"
    agent.steer_active.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_rejects_chat_only_parameters_before_lookup():
    from jiuwenswarm.server.handlers.steering import handle_chat_steering

    ctx = SimpleNamespace(
        request=control_request(query=True, content="unwanted"),
        services=object(),
        sink=SimpleNamespace(send_wire=AsyncMock()),
    )
    await handle_chat_steering(ctx)
    wire = ctx.sink.send_wire.await_args.args[0]
    assert wire["body"]["details"]["reason"] == "INVALID_REQUEST"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_scoped,method,params,interactive",
    [
        pytest.param(False, ReqMethod.CHAT_SEND, {}, False, id="root-adapter"),
        pytest.param(True, ReqMethod.CHAT_STEER, {}, False, id="control-request"),
        pytest.param(True, ReqMethod.CHAT_SEND, {"input_mode": "steer"}, False, id="steer"),
        pytest.param(True, ReqMethod.CHAT_SEND, {"input_mode": "follow_up"}, False, id="follow-up"),
        pytest.param(
            True, ReqMethod.CHAT_SEND,
            {"mode": "team", "source": "permission_interrupt"}, True,
            id="team-permission",
        ),
        pytest.param(
            True, ReqMethod.CHAT_SEND,
            {"mode": "team", "source": "ask_user_interrupt"}, True,
            id="team-answer",
        ),
    ],
)
async def test_non_owner_stream_preserves_original_steering_binding(
    session_scoped, method, params, interactive
):
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = session_scoped
    control, original, _, _ = session()
    adapter._steering_control = control

    async def chunks(*args):
        yield SimpleNamespace(payload={"event_type": "runtime.accepted"})

    adapter._process_message_stream_impl = chunks
    request = run_request(request_id="non-owner", req_method=method, params=params)
    query = InteractiveInput() if interactive else "follow-up"
    outputs = [
        chunk.payload
        async for chunk in adapter.process_message_stream_impl(request, {"query": query})
    ]
    assert outputs == [{"event_type": "runtime.accepted"}]
    assert original.active
    assert (await control.handle(control_request(), query=False))["status"] == "accepted"
