"""Real owner RPC, host search gate and native confirmation, with a local model."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.jiuwenmate.web_adapter import WEB_NAMESPACE
from jiuwenswarm.server.jiuwenmate.agent_manager import AgentManager
from jiuwenswarm.server.jiuwenmate.execution_models import AccessSnapshot, ConversationRef, RequestRouteSnapshot
from openjiuwen.core.context_engine.context.context import SessionModelContext
from openjiuwen.harness import persistent_agent as core
from openjiuwen.harness.schema.deep_agent_spec import ModelSpec
from tests.integration_tests.jiuwenmate.test_cross_group_search_real_core import LocalCounter, SearchToolModel
from tests.unit_tests.server.jiuwenmate.test_web_private_chat import host, NOW


@pytest.mark.asyncio
@pytest.mark.parametrize("source_platform,target_platform", [
    ("feishu", "feishu"), ("feishu", "dingtalk"), ("dingtalk", "feishu"),
])
@pytest.mark.parametrize("decision", ["once", "reject"])
async def test_web_owner_decision_resumes_real_group_request_only_in_im_runtime(
    host, monkeypatch, source_platform, target_platform, decision,
):
    from openjiuwen.core.foundation.llm.schema.message import AssistantMessage, UsageMetadata

    class GroupSearchModel(SearchToolModel):
        async def invoke(self, messages, **kwargs):
            return AssistantMessage(content='{"decision":"respond","reason":"explicit mention"}',
                                    usage_metadata=UsageMetadata(input_tokens=3, output_tokens=3))

    model = GroupSearchModel()
    monkeypatch.setattr(ModelSpec, "build", lambda self: model)
    monkeypatch.setattr(SessionModelContext, "token_counter", lambda self: LocalCounter())
    agent_id = await host.colleague()
    web = await host.adapter._runtime()
    web._approval_channels = frozenset({"web"})
    web.scheduler._driver.run = AsyncMock(side_effect=AssertionError("Web cannot execute a Feishu request"))
    from jiuwenswarm.server.jiuwenmate.budget_repository import BudgetRepository

    im = AgentManager(web._service, web._sessions, web._windows, BudgetRepository(host.db),
                      execute=core.execute_persistent_request, approval_channels=frozenset({"feishu", "dingtalk"}))
    checks = []

    async def verify(access, group):
        checks.append(group.group_chat_id)
        return "active"

    im.verify_group_membership = verify
    await im.start()
    try:
        source = im.group_chats.resolve(WEB_NAMESPACE, source_platform, "oc_source", now=NOW)
        target = im.group_chats.resolve(WEB_NAMESPACE, target_platform, "oc_target", now=NOW)
        im.group_chats.set_name(WEB_NAMESPACE, source.group_chat_id, "产品群")
        for group in (source, target):
            im.group_chats.observe_access(WEB_NAMESPACE, agent_id, group.group_chat_id, "app-1",
                                          observed_at=NOW, source_ref=f"join:{group.group_chat_id}")
        secret = "release decision: TWO-RUNTIME-PRIVATE-RESULT"
        im.group_archive.append(WEB_NAMESPACE, source.group_chat_id,
            core.GroupMessage(message_id="source-secret", sender_id="alice", sender_name="Alice",
                              sender_kind="human", received_at=NOW, text=secret), source_ref="source-secret")
        trigger = core.GroupMessage(message_id="group-question", sender_id="requester", sender_name="Requester",
                                    sender_kind="human", received_at=NOW, text="产品群的发布决定是什么？",
                                    mention_ids=(agent_id,))
        im.group_archive.append(WEB_NAMESPACE, target.group_chat_id, trigger, source_ref="question")
        session = im._sessions.resolve_session(WEB_NAMESPACE, agent_id, ConversationRef.group(target.group_chat_id), now=NOW)
        state, _ = await im.scheduler.accept(session, snapshot=im._service.current_snapshot(WEB_NAMESPACE, agent_id),
            source_ref=f"{target_platform}:group-question", access=AccessSnapshot(principal_ref=f"{target_platform}:requester",
                startup_scope=frozenset({"chat"}), source_scope=frozenset({"chat"}), policy_revision="test"),
            request_input=core.RequestInput(kind="group_message", text=trigger.text, source=trigger, reply_target="oc_target"),
            route_snapshot=RequestRouteSnapshot(channel=target_platform, app_id="app-1", target_id="oc_target",
                                                source_message_id=trigger.message_id), received_at=NOW)
        request_id = state.record.request_id
        async with asyncio.timeout(12):
            while im._sessions.get_request(WEB_NAMESPACE, request_id).outcome != "paused" or im.scheduler.running:
                await asyncio.sleep(0.01)
        assert not model.tool_outputs
        listed = await host.call(ReqMethod.JIUWENMATE_SHARING_LIST, agent_id=agent_id)
        assert listed.ok, listed.payload
        approval, = listed.payload["approvals"]
        assert approval["source_groups"][0]["platform"] == source_platform
        assert approval["target"]["channel"] == target_platform
        checks.clear()
        decided = await host.call(ReqMethod.JIUWENMATE_SHARING_DECIDE, agent_id=agent_id,
                                   approval_id=approval["approval_id"], decision=decision)
        assert decided.ok, decided.payload
        await web._recover_permission_approvals()
        await im._recover_permission_approvals()
        async with asyncio.timeout(12):
            while im._sessions.get_request(WEB_NAMESPACE, request_id).outcome != "completed" or im.scheduler.running:
                await asyncio.sleep(0.01)
        web.scheduler._driver.run.assert_not_awaited()
        assert checks
        assert (secret in str(model.tool_outputs)) is (decision == "once")
        assert im._sessions.get_request(WEB_NAMESPACE, request_id).record.route_snapshot.channel == target_platform
        assert len(im._sessions.executions(WEB_NAMESPACE, request_id)) == 2
        assert not im._permission_approvals.has_pending_session(WEB_NAMESPACE, session.session_id)
    finally:
        await im.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["once", "reject"])
async def test_owner_approval_resumes_real_native_search_without_early_disclosure(host, monkeypatch, decision):
    model = SearchToolModel()
    monkeypatch.setattr(ModelSpec, "build", lambda self: model)
    monkeypatch.setattr(SessionModelContext, "token_counter", lambda self: LocalCounter())
    host.core.execute = core.execute_persistent_request
    agent_id = await host.colleague()
    manager = await host.adapter._runtime()

    async def verify(access, group):
        return "active"

    manager.verify_group_membership = verify
    group = manager.group_chats.resolve(WEB_NAMESPACE, "feishu", "source-private", now=NOW)
    manager.group_chats.set_name(WEB_NAMESPACE, group.group_chat_id, "产品群")
    manager.group_chats.observe_access(WEB_NAMESPACE, agent_id, group.group_chat_id, "source-app",
                                      observed_at=NOW, source_ref="test:join")
    secret = "release decision: OWNER-APPROVED-PRIVATE-CONTENT"
    manager.group_archive.append(WEB_NAMESPACE, group.group_chat_id,
        core.GroupMessage(message_id="private-message", sender_id="alice", sender_name="Alice",
                          sender_kind="human", received_at=NOW, text=secret), source_ref="test:source")

    response = await host.send(agent_id, "产品群的发布决定是什么？", message_id="real-sharing-1")
    assert response.ok
    request_id = response.payload["request"]["request_id"]
    paused = await host.settled(request_id)
    assert paused["outcome"] == "paused", paused
    assert not model.tool_outputs and secret not in str(model.calls) + str(host.pushed)
    listed = await host.call(ReqMethod.JIUWENMATE_SHARING_LIST, agent_id=agent_id)
    assert listed.ok
    approval, = listed.payload["approvals"]
    assert approval["messages"][0]["text"] == secret and approval["can_decide"]
    decided = await host.call(ReqMethod.JIUWENMATE_SHARING_DECIDE, agent_id=agent_id,
                             approval_id=approval["approval_id"], decision=decision)
    assert decided.ok, decided.payload

    for _ in range(600):
        view = await manager.request_view(WEB_NAMESPACE, request_id)
        if not view.running and view.state.status == "ended" and view.state.outcome != "paused":
            break
        await asyncio.sleep(0.01)
    assert view.state.outcome == "completed", view.to_dict()
    assert (secret in str(model.tool_outputs)) is (decision == "once")
    assert not manager._permission_approvals.has_pending_session(WEB_NAMESPACE, view.state.record.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["once", "reject"])
async def test_owner_rpc_resumes_original_bash_cli_with_real_core_and_full_access(host, monkeypatch, decision):
    from openjiuwen.core.foundation.llm.schema.message import ToolMessage, UsageMetadata
    from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall

    secret = "ORIGINAL-BASH-PRIVATE-MESSAGE"

    class BashModel(SearchToolModel):
        async def stream(self, messages, **kwargs):
            self.calls.append(list(messages))
            results = [message.content for message in messages if isinstance(message, ToolMessage)]
            usage = UsageMetadata(input_tokens=3, output_tokens=4)
            if not results:
                call = ToolCall(id="cli-call-1", type="function", name="bash", arguments=json.dumps({
                    "command": "lark-cli im +chat-messages-list --chat-id oc_source",
                }))
                yield AssistantMessageChunk(content="", tool_calls=[call], finish_reason="tool_calls",
                                             usage_metadata=usage)
                return
            self.tool_outputs.extend(str(result) for result in results)
            yield AssistantMessageChunk(content=str(results[-1]), finish_reason="stop", usage_metadata=usage)

    model = BashModel()
    monkeypatch.setattr(ModelSpec, "build", lambda self: model)
    monkeypatch.setattr(SessionModelContext, "token_counter", lambda self: LocalCounter())
    host.core.execute = core.execute_persistent_request
    agent_id = await host.colleague()
    manager = await host.adapter._runtime()
    settings = manager._service.read_settings(WEB_NAMESPACE, agent_id)
    manager._service.update_settings(WEB_NAMESPACE, agent_id, replace(settings, permission_profile="full_access"))
    manager._service.publish(WEB_NAMESPACE, agent_id)
    manager.verify_group_membership = AsyncMock(return_value="active")
    monkeypatch.setattr(manager.cli_runtime, "status", lambda *args: {"status": "ready", "app_id": "app-1"})
    raw = {"message_id": "om_source", "chat_id": "oc_source", "create_time": str(int(NOW.timestamp() * 1000)),
           "sender": {"id": "alice", "name": "Alice"}, "content": secret}
    async def cli_read(namespace, agent, argv):
        if argv[:2] == ["api", "GET"]:
            return {"code": 0, "data": {"items": [raw]}}
        display = {**raw, "create_time": NOW.strftime("%Y-%m-%d %H:%M")}
        return {"ok": True, "data": {"messages": [display]}}
    read = AsyncMock(side_effect=cli_read)
    monkeypatch.setattr(manager.cli_runtime, "run_group_read", read)
    group = manager.group_chats.resolve(WEB_NAMESPACE, "feishu", "oc_source", now=NOW)
    manager.group_chats.set_name(WEB_NAMESPACE, group.group_chat_id, "产品群")
    manager.group_chats.observe_access(WEB_NAMESPACE, agent_id, group.group_chat_id, "app-1",
                                      observed_at=NOW, source_ref="test:join")
    response = await host.send(agent_id, "读取产品群内容", message_id="original-bash-sharing")
    request_id = response.payload["request"]["request_id"]
    paused = await host.settled(request_id)
    assert paused["outcome"] == "paused", paused
    assert not model.tool_outputs and secret not in str(model.calls) + str(host.pushed)
    listed = await host.call(ReqMethod.JIUWENMATE_SHARING_LIST, agent_id=agent_id)
    approval, = listed.payload["approvals"]
    assert approval["messages"][0]["text"] == secret
    decided = await host.call(ReqMethod.JIUWENMATE_SHARING_DECIDE, agent_id=agent_id,
                             approval_id=approval["approval_id"], decision=decision)
    assert decided.ok, decided.payload
    async with asyncio.timeout(12):
        while True:
            view = await manager.request_view(WEB_NAMESPACE, request_id)
            if not view.running and view.state.status == "ended" and view.state.outcome != "paused":
                break
            await asyncio.sleep(0.01)
    assert view.state.outcome == "completed", view.to_dict()
    assert (secret in str(model.tool_outputs)) is (decision == "once")
    assert len(manager._sessions.executions(WEB_NAMESPACE, request_id)) == 2
    assert sum("+chat-messages-list" in call.args[2] for call in read.await_args_list) == 1
    listed = await host.call(ReqMethod.JIUWENMATE_SHARING_LIST, agent_id=agent_id)
    assert len(listed.payload["approvals"]) == 1
