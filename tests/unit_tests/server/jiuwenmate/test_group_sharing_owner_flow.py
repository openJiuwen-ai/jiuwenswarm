"""Owner Web approvals resume an original request without weakening tool approvals."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3

import pytest

from openjiuwen.harness import persistent_agent as core
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.jiuwenmate import execution_driver
from jiuwenswarm.server.jiuwenmate.group_share_repository import GroupShareRepository
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.permission_approval_repository import PermissionApprovalRepository
from jiuwenswarm.server.jiuwenmate.web_adapter import WEB_ACCESS
from tests.unit_tests.server.jiuwenmate.test_web_private_chat import host, NOW, WEB_NAMESPACE, CONVERSATION


async def seed_native_waiting_scene(manager, request):
    from openjiuwen.core.foundation.llm import AssistantMessage, UserMessage
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
    from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
    from openjiuwen.core.single_agent.interrupt.state import ToolInterruptEntry, ToolInterruptionState
    from openjiuwen.harness.persistent_agent.schema.session_record import SessionStateRecord, encode_interruption, encode_messages

    record = manager._sessions.get_request(WEB_NAMESPACE, request["request_id"]).record
    call = ToolCall(id="sharing-call", name="search_group_messages", arguments='{"query":"launch"}', type="function")
    assistant = AssistantMessage(content="", tool_calls=[call])
    interrupted = ToolInterruptionState(ai_message=assistant, iteration=1, original_query="query",
        interrupted_tools={call.id: ToolInterruptEntry(tool_call=call,
            interrupt_requests={call.id: InterruptRequest(message="waiting")})})
    state = SessionStateRecord(agent_id=record.agent_id, session_id=record.session_id,
        messages=encode_messages([UserMessage(content="query"), assistant]),
        interruption=encode_interruption(interrupted), checkpoint=None, last_group_message_id=None,
        current_window_id=None, window_seq=None, last_rollover=None,
        ending={"outcome": "awaiting_confirmation", "request_id": record.request_id,
                "execution_id": "paused-execution", "spec_fingerprint": record.config_fingerprint})
    backend = manager._windows.bind(WEB_NAMESPACE, record.agent_id, record.session_id)
    key = core.session_key_prefix(record.agent_id, record.session_id) + "state"
    await backend.set(key, state.model_dump_json())
    return backend, key


async def start_sharing(host, monkeypatch, *, duplicate_query=False, pause_time=None, unregistered=False):
    agent_id = await host.colleague()
    resumed = []

    async def execute(spec, context, inputs):
        execution = context.execution
        occurred_at = pause_time or datetime.now(timezone.utc)
        message = core.GroupMessage(message_id="secret-1", sender_id="alice", sender_name="Alice",
                                    sender_kind="human", received_at=NOW, text="confidential launch plan")
        hit = core.GroupMessageSearchHit(group_id="private-group", group_name="Private group",
                                       message_ref="private-ref", message=message)
        result = core.GroupMessageSearchResult(**core.InvocationRef.from_execution(execution).model_dump(),
                                              status="found", hits=(hit,), query_complete=True)
        scope = {"namespace_id": WEB_NAMESPACE, "config_id": agent_id, "agent_id": agent_id,
                 "request_id": execution.request_id, "principal_ref": "local", "requester_name": "User",
                 "target": {"channel": "web", "app_id": "", "kind": "direct", "id": CONVERSATION,
                            "name": "Owner conversation"}, "query": {"query": "launch"}}
        row = GroupShareRepository(host.db).create_or_get(
            scope, hashlib.sha256(b"launch").hexdigest(), result.model_dump(mode="json"),
            [{"group_id": "private-group", "name": "Private group"}], now=occurred_at,
        )
        calls = [{"tool_call_id": "sharing-call", "tool_name": "search_group_messages",
                  "arguments": '{"query":"launch"}', "approval_ref": row["approval_id"]}]
        if duplicate_query:
            calls.append({**calls[0], "tool_call_id": "sharing-call-2"})
        event = core.AgentEvent.create(
            execution, event_type=core.EventType.EXECUTION_PAUSED, local_seq=1, occurred_at=occurred_at,
            payload={"reason": "awaiting_confirmation", "intercepted": calls},
        )
        await context.ports.commit.record(execution, event)
        yield event

    async def resume(spec, context, reply):
        resumed.append((context.execution, reply))
        event = core.AgentEvent.create(context.execution, event_type=core.EventType.EXECUTION_FINAL, local_seq=1,
                                       occurred_at=NOW, payload={"status": "completed", "output_refs": ()})
        await context.ports.commit.record(context.execution, event)
        yield event

    host.core.execute = execute
    monkeypatch.setattr(execution_driver.core, "resume_persistent_request", resume)
    response = await host.send(agent_id, "cross-group request", message_id="sharing-request-1")
    assert response.ok, response.payload
    initial = response.payload["request"]
    request = await host.settled(initial["request_id"])
    manager = host.managers[0]
    listed = await host.call(ReqMethod.JIUWENMATE_SHARING_LIST, agent_id=agent_id)
    assert listed.ok, listed.payload
    row, = listed.payload["approvals"]
    assert row["can_decide"] is not unregistered and row["status"] == "pending"
    return agent_id, request, row, manager, resumed


def fail_native_creation(monkeypatch):
    original = PermissionApprovalRepository.create
    fault = {"enabled": True}

    def create(repository, *args, **kwargs):
        if fault["enabled"]:
            raise sqlite3.OperationalError("injected native approval persistence failure")
        return original(repository, *args, **kwargs)

    monkeypatch.setattr(PermissionApprovalRepository, "create", create)
    return fault


async def wait_for_sharing_link(manager, approval_id):
    async with asyncio.timeout(6):
        while True:
            row = manager.sharing_repository.get(approval_id)
            if row["native_approval_id"] is not None:
                return row
            await asyncio.sleep(0.01)


async def test_unregistered_sharing_pause_blocks_the_next_session_request(host, monkeypatch):
    fail_native_creation(monkeypatch)
    agent_id, request, row, manager, resumed = await start_sharing(host, monkeypatch, unregistered=True)
    state = manager._sessions.get_request(WEB_NAMESPACE, request["request_id"])
    assert manager._permission_approvals.has_pending_session(WEB_NAMESPACE, state.record.session_id)
    sent = await host.send(agent_id, "next request", message_id="after-unregistered-pause")
    assert sent.ok and sent.payload["request"]["status"] == "queued"
    assert not manager._sessions.executions(WEB_NAMESPACE, sent.payload["request"]["request_id"])
    assert not resumed


@pytest.mark.parametrize("restart", [False, True])
async def test_native_create_fault_recovers_without_replaying_the_search(host, monkeypatch, restart):
    fault = fail_native_creation(monkeypatch)
    pause_time = datetime.now(timezone.utc) - timedelta(minutes=2)
    agent_id, request, row, manager, resumed = await start_sharing(
        host, monkeypatch, pause_time=pause_time, unregistered=True, duplicate_query=True,
    )
    if restart:
        await manager.aclose()
        manager = host.restarted_manager()
        await manager.start()
    assert manager.sharing_repository.get(row["approval_id"])["native_approval_id"] is None
    fault["enabled"] = False
    linked = await wait_for_sharing_link(manager, row["approval_id"])
    native = manager._permission_approvals.get(linked["native_approval_id"])
    assert native.expires_at == pause_time + timedelta(minutes=15)
    assert linked["tool_call_ids"] == ["sharing-call", "sharing-call-2"]
    assert len(manager._sessions.executions(WEB_NAMESPACE, request["request_id"])) == 1
    assert not resumed
    await manager._recover_permission_approvals()
    assert manager._permission_approvals.pending(WEB_NAMESPACE) == [native]
    await manager.decide_sharing(WEB_NAMESPACE, agent_id, row["approval_id"], decision="once", access=WEB_ACCESS)
    async with asyncio.timeout(6):
        while manager._sessions.get_request(WEB_NAMESPACE, request["request_id"]).outcome != "completed":
            await asyncio.sleep(0.01)
    assert len(resumed) == 1


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("expiry", ["owner_list", "periodic"])
async def test_unregistered_expired_sharing_is_linked_and_rejected_without_a_new_deadline(
    host, monkeypatch, restart, expiry,
):
    fault = fail_native_creation(monkeypatch)
    pause_time = datetime.now(timezone.utc) - timedelta(minutes=2)
    agent_id, request, row, manager, resumed = await start_sharing(
        host, monkeypatch, pause_time=pause_time, unregistered=True,
    )
    advanced = pause_time + timedelta(minutes=15, seconds=1)
    monkeypatch.setattr(manager, "_clock", lambda: advanced)
    if expiry == "owner_list":
        listed = await manager.sharing_list(WEB_NAMESPACE, agent_id, access=WEB_ACCESS)
        assert listed["approvals"][0]["status"] == "expired"
    if restart:
        await manager.aclose()
        manager = host.restarted_manager()
        monkeypatch.setattr(manager, "_clock", lambda: advanced)
    fault["enabled"] = False
    if restart:
        await manager.start()
    async with asyncio.timeout(6):
        while manager._sessions.get_request(WEB_NAMESPACE, request["request_id"]).outcome != "completed":
            await asyncio.sleep(0.01)
    linked = manager.sharing_repository.get(row["approval_id"])
    native = manager._permission_approvals.get(linked["native_approval_id"])
    assert linked["status"] == "expired" and native.status == "consumed"
    assert native.expires_at == pause_time + timedelta(minutes=15)
    assert len(resumed) == 1 and resumed[0][1]["responses"][0]["choice"] == "reject"
    assert not manager._permission_approvals.has_pending_session(WEB_NAMESPACE, native.session_id)


@pytest.mark.parametrize("decision,choice", [("once", "approve"), ("permanent", "approve"), ("reject", "reject")])
async def test_owner_buttons_resume_the_original_request(host, monkeypatch, decision, choice):
    agent_id, request, row, manager, resumed = await start_sharing(host, monkeypatch)
    conversations = await host.conversations(agent_id)
    assert any(item["conversation_id"] == "sharing-approvals" for item in conversations)
    response = await host.call(ReqMethod.JIUWENMATE_SHARING_DECIDE, agent_id=agent_id,
                               approval_id=row["approval_id"], decision=decision)
    assert response.ok, response.payload
    for _ in range(400):
        state = await manager.request_view(WEB_NAMESPACE, request["request_id"])
        if state.state.outcome == "completed":
            break
        await asyncio.sleep(0.005)
    assert state.state.outcome == "completed"
    assert len(resumed) == 1 and resumed[0][0].request_id == request["request_id"]
    assert resumed[0][1]["responses"][0]["choice"] == choice
    assert not any("confidential launch plan" in str(item) for item in host.pushed)


async def test_original_tool_approval_endpoint_cannot_approve_business_share(host, monkeypatch):
    agent_id, request, row, manager, resumed = await start_sharing(host, monkeypatch)
    native = request["approval"]["approval_id"]
    response = await host.call(ReqMethod.JIUWENMATE_PERMISSION_DECIDE, approval_id=native,
                               conversation_id=CONVERSATION, decision="approve", tool_call_id="sharing-call")
    assert not response.ok and response.payload["code"] == "FORBIDDEN"
    assert not resumed


async def test_business_marker_blocks_generic_approval_before_host_link_is_visible(host, monkeypatch):
    agent_id, request, row, manager, resumed = await start_sharing(host, monkeypatch)
    monkeypatch.setattr(manager.sharing_repository, "for_native", lambda _: None)
    with pytest.raises(PersistentAgentError) as rejected:
        await manager.decide_permission_approval(
            request["approval"]["approval_id"], decision="approve", access=WEB_ACCESS,
            channel="web", target_id=CONVERSATION, tool_call_id="sharing-call",
        )
    assert rejected.value.code == "FORBIDDEN"
    assert not resumed


async def test_wrong_config_or_web_principal_cannot_approve(host, monkeypatch):
    agent_id, request, row, manager, resumed = await start_sharing(host, monkeypatch)
    other_id = await host.colleague()
    response = await host.call(ReqMethod.JIUWENMATE_SHARING_DECIDE, agent_id=other_id,
                               approval_id=row["approval_id"], decision="permanent")
    assert not response.ok
    with pytest.raises(PersistentAgentError, match="local owner"):
        await manager.decide_sharing(WEB_NAMESPACE, agent_id, row["approval_id"], decision="once",
                                     access=replace(WEB_ACCESS, principal_ref="untrusted-user"))
    assert not resumed


async def test_owner_can_revoke_a_permanent_grant(host, monkeypatch):
    agent_id, request, row, manager, resumed = await start_sharing(host, monkeypatch)
    approved = await host.call(ReqMethod.JIUWENMATE_SHARING_DECIDE, agent_id=agent_id,
                               approval_id=row["approval_id"], decision="permanent")
    assert approved.ok
    grant, = approved.payload["grants"]
    revoked = await host.call(ReqMethod.JIUWENMATE_SHARING_REVOKE, agent_id=agent_id, grant_id=grant["grant_id"])
    assert revoked.ok and revoked.payload["grants"][0]["status"] == "revoked"


async def test_owner_decision_before_restart_recovers_original_request(host, monkeypatch):
    agent_id, request, row, manager, resumed = await start_sharing(host, monkeypatch)
    manager.sharing_repository.decide(row["approval_id"], namespace_id=WEB_NAMESPACE, config_id=agent_id,
                                     owner_principal="local", decision="once", now=datetime.now(timezone.utc))
    await manager.aclose()
    restored = host.restarted_manager()
    await restored.start()
    for _ in range(400):
        state = await restored.request_view(WEB_NAMESPACE, request["request_id"])
        if state.state.outcome == "completed":
            break
        await asyncio.sleep(0.005)
    assert state.state.outcome == "completed" and len(resumed) == 1


async def test_expiry_rejects_and_resumes_without_an_owner_click(host, monkeypatch):
    agent_id, request, row, manager, resumed = await start_sharing(host, monkeypatch)
    advanced = datetime.fromisoformat(row["expires_at"]) + timedelta(seconds=1)
    monkeypatch.setattr(manager, "_clock", lambda: advanced)

    async def completed():
        while True:
            state = await manager.request_view(WEB_NAMESPACE, request["request_id"])
            if state.state.outcome == "completed":
                return state
            await asyncio.sleep(0.01)

    await asyncio.wait_for(completed(), timeout=5)
    assert resumed[0][1]["responses"][0]["choice"] == "reject"
    listed = await host.call(ReqMethod.JIUWENMATE_SHARING_LIST, agent_id=agent_id)
    assert listed.payload["approvals"][0]["status"] == "expired"


async def test_duplicate_parallel_query_survives_restart_and_resumes_both_calls(host, monkeypatch):
    agent_id, request, row, manager, resumed = await start_sharing(host, monkeypatch, duplicate_query=True)
    await manager.aclose()
    restored = host.restarted_manager()
    await restored.start()
    response = await restored.decide_sharing(WEB_NAMESPACE, agent_id, row["approval_id"],
                                             decision="once", access=WEB_ACCESS)
    assert response["approvals"][0]["status"] == "approved_once"
    for _ in range(400):
        state = await restored.request_view(WEB_NAMESPACE, request["request_id"])
        if state.state.outcome == "completed":
            break
        await asyncio.sleep(0.005)
    assert state.state.outcome == "completed" and len(resumed) == 1
    assert {item["tool_call_id"] for item in resumed[0][1]["responses"]} == {"sharing-call", "sharing-call-2"}
    assert all(item["choice"] == "approve" for item in resumed[0][1]["responses"])


@pytest.mark.parametrize("failure", ["preflight", "stream_error", "missing_terminal"])
async def test_failed_continuation_settles_approval_and_unblocks_session(host, monkeypatch, failure):
    agent_id, request, row, manager, resumed = await start_sharing(host, monkeypatch)
    backend, key = await seed_native_waiting_scene(manager, request)

    if failure == "preflight":
        async def fail_preflight(*args):
            raise PersistentAgentError("group trigger is no longer readable", code="TRIGGER_UNAVAILABLE")
        monkeypatch.setattr(execution_driver, "prepare_agent_mail_invocation", fail_preflight)
    else:
        async def fail_resume(*args):
            if failure == "stream_error":
                raise RuntimeError("resume stream failed")
            if False:
                yield
        monkeypatch.setattr(execution_driver.core, "resume_persistent_request", fail_resume)

    decided = await host.call(ReqMethod.JIUWENMATE_SHARING_DECIDE, agent_id=agent_id,
                              approval_id=row["approval_id"], decision="once")
    assert decided.ok
    for _ in range(400):
        state = await manager.request_view(WEB_NAMESPACE, request["request_id"])
        if state.state.outcome == "failed":
            break
        await asyncio.sleep(0.005)
    assert state.state.outcome == "failed"
    for _ in range(400):
        native = manager._permission_approvals.get(request["approval"]["approval_id"])
        if native.status == "consumed":
            break
        await asyncio.sleep(0.005)
    assert native.status == "consumed"
    assert not manager._permission_approvals.has_pending_session(WEB_NAMESPACE, state.state.record.session_id)
    assert json.loads(await backend.get(key))["interruption"] is None


async def test_restart_settles_decided_approval_of_an_already_failed_request(host, monkeypatch):
    agent_id, request, row, manager, resumed = await start_sharing(host, monkeypatch)
    backend, key = await seed_native_waiting_scene(manager, request)
    manager.sharing_repository.decide(row["approval_id"], namespace_id=WEB_NAMESPACE, config_id=agent_id,
                                     owner_principal="local", decision="once", now=datetime.now(timezone.utc))
    native = manager._permission_approvals.decide_sharing(request["approval"]["approval_id"],
        decisions={"sharing-call": True}, now=datetime.now(timezone.utc))
    assert manager._sessions.reopen_for_continuation(WEB_NAMESPACE, request["request_id"], now=NOW)
    execution = manager._sessions.start_execution(WEB_NAMESPACE, request["request_id"], now=NOW)
    manager._sessions.record_execution_failure(WEB_NAMESPACE, request["request_id"], execution.execution_id,
        reason="error", detail="Trigger no longer readable", now=NOW)
    await manager.aclose()
    restored = host.restarted_manager()
    await restored.start()
    for _ in range(400):
        if restored._permission_approvals.get(native.approval_id).status == "consumed":
            break
        await asyncio.sleep(0.005)
    assert restored._permission_approvals.get(native.approval_id).status == "consumed"
    assert json.loads(await backend.get(key))["interruption"] is None
    assert not resumed

