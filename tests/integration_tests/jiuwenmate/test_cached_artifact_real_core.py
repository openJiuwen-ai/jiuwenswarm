"""Current-group published files are visible; direct cross-group reads still need consent."""

import asyncio
from dataclasses import replace
import hashlib
import json
import sqlite3

import pytest

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.jiuwenmate.execution_models import AccessSnapshot, ConversationRef, RequestRouteSnapshot
from jiuwenswarm.server.jiuwenmate.web_adapter import WEB_NAMESPACE
from openjiuwen.core.context_engine.context.context import SessionModelContext
from openjiuwen.core.foundation.llm.schema.message import AssistantMessage, ToolMessage, UsageMetadata
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.harness import persistent_agent as core
from openjiuwen.harness.schema.deep_agent_spec import ModelSpec
from tests.integration_tests.jiuwenmate.test_cross_group_search_real_core import LocalCounter, SearchToolModel
from tests.unit_tests.server.jiuwenmate.test_web_private_chat import host, NOW


@pytest.mark.asyncio
@pytest.mark.parametrize("target_platform", ["feishu", "dingtalk"])
@pytest.mark.parametrize("profile", ["default", "full_access"])
@pytest.mark.parametrize("decision", ["once", "reject"])
@pytest.mark.parametrize("tool_name", ["read_file", "share_file", "bash", "search_files", "search_messages", "search_cached"])
async def test_published_copy_and_cross_group_reads_under_both_profiles(
    host, monkeypatch, profile, decision, tool_name, target_platform,
):
    secret = "CACHED-FOREIGN-FILE-CONTENT"
    path = {"value": ""}
    target = {"value": "oc_target"}

    class Model(SearchToolModel):
        async def invoke(self, messages, **kwargs):
            return AssistantMessage(content='{"decision":"respond","effort":"quick","reason":"explicit mention"}')

        async def stream(self, messages, **kwargs):
            self.calls.append(list(messages))
            results = [message.content for message in messages if isinstance(message, ToolMessage)]
            usage = UsageMetadata(input_tokens=3, output_tokens=4)
            if not results:
                args = ({"command": 'python -c "print(open(\'sessions/source-session/output/report.txt\').read())"'}
                        if tool_name == "bash" else {"file_path": path["value"]})
                if tool_name.startswith("search_"):
                    args = {"group_ids": [target["value"] if tool_name == "search_cached" else "oc_source"],
                            "content_types": ["messages" if tool_name == "search_messages" else "files"]}
                if tool_name == "share_file":
                    args["share_mode"] = "attachment"
                call = ToolCall(id="cached-file-call", type="function",
                                name="search_group_messages" if tool_name.startswith("search_") else tool_name,
                                arguments=json.dumps(args))
                yield AssistantMessageChunk(content="", tool_calls=[call], finish_reason="tool_calls", usage_metadata=usage)
            else:
                if tool_name == "search_cached" and decision == "reject" and len(results) == 1:
                    call = ToolCall(id="retry-cached-file-call", type="function", name="search_group_messages",
                        arguments=json.dumps({"group_ids": [target["value"]], "content_types": ["files"], "query": "report"}))
                    yield AssistantMessageChunk(content="", tool_calls=[call], finish_reason="tool_calls", usage_metadata=usage)
                    return
                self.tool_outputs.extend(str(item) for item in results)
                yield AssistantMessageChunk(content=str(results[-1]), finish_reason="stop", usage_metadata=usage)

    model = Model()
    monkeypatch.setattr(ModelSpec, "build", lambda self: model)
    monkeypatch.setattr(SessionModelContext, "token_counter", lambda self: LocalCounter())
    host.core.execute = core.execute_persistent_request
    agent = await host.colleague()
    manager = await host.adapter._runtime()
    settings = manager._service.read_settings(WEB_NAMESPACE, agent)
    manager._service.update_settings(WEB_NAMESPACE, agent, replace(settings, permission_profile=profile))
    manager._service.publish(WEB_NAMESPACE, agent)
    async def verify(*args):
        return "active"
    manager.verify_group_membership = verify
    groups = [manager.group_chats.resolve(WEB_NAMESPACE, platform, chat, now=NOW)
              for platform, chat in (("feishu", "oc_source"), (target_platform, "oc_target"))]
    for group in groups:
        manager.group_chats.observe_access(WEB_NAMESPACE, agent, group.group_chat_id, "app-1",
                                          observed_at=NOW, source_ref="join:" + group.group_chat_id)
    session = manager._sessions.resolve_session(WEB_NAMESPACE, agent, ConversationRef.group(groups[1].group_chat_id), now=NOW)
    workspace = manager._service.workspaces
    digest = "sha256:" + hashlib.sha256(secret.encode()).hexdigest()
    for index, (sid, group) in enumerate((("source-session", groups[0]), (session.session_id, groups[1]))):
        root = workspace.ensure_session(WEB_NAMESPACE, agent, sid)
        (root / "output" / "report.txt").write_text(secret, encoding="utf-8")
        relative = f"sessions/{sid}/output/report.txt"
        if index:
            path["value"] = relative
        with sqlite3.connect(host.db) as conn:
            conn.execute("INSERT INTO session_artifacts(namespace_id,agent_id,session_id,source_group_id,artifact_id,"
                         "artifact_kind,output_id,title,file_name,file_path,file_ref,file_size,created_at) "
                         "VALUES (?,?,?,?,?,'file',?,?,?,?,?,?,?)",
                         (WEB_NAMESPACE, agent, sid, group.group_chat_id, f"artifact-{index}", f"output-{index}",
                          "report.txt", "report.txt", relative, digest, len(secret.encode()),
                          NOW.isoformat()))
    trigger = core.GroupMessage(message_id="cached-request", sender_id="requester", sender_name="Requester",
        sender_kind="human", received_at=NOW, text="把来源群的 report.txt 发给我", mention_ids=(agent,))
    manager.group_archive.append(WEB_NAMESPACE, groups[1].group_chat_id, trigger, source_ref="cached-question")
    manager.group_archive.append(WEB_NAMESPACE, groups[0].group_chat_id,
        core.GroupMessage(message_id="source-message", sender_id="author", sender_name="Author",
                          sender_kind="human", received_at=NOW, text=secret), source_ref="source-message")
    state, _ = await manager.scheduler.accept(session, snapshot=manager._service.current_snapshot(WEB_NAMESPACE, agent),
        source_ref=f"{target_platform}:cached-question", access=AccessSnapshot(principal_ref=f"{target_platform}:requester",
            startup_scope=frozenset({"chat", "workspace_read", "workspace_write"}),
            source_scope=frozenset({"chat", "workspace_read", "workspace_write"}), policy_revision="test"),
        request_input=core.RequestInput(kind="group_message", text=trigger.text, source=trigger, reply_target="oc_target"),
        route_snapshot=RequestRouteSnapshot(channel=target_platform, app_id="app-1", target_id="oc_target",
                                            source_message_id=trigger.message_id), received_at=NOW)
    rid = state.record.request_id
    async with asyncio.timeout(15):
        while not manager._sessions.get_request(WEB_NAMESPACE, rid).outcome or manager.scheduler.running:
            await asyncio.sleep(0.01)
    current = manager._sessions.get_request(WEB_NAMESPACE, rid)
    if tool_name in {"read_file", "share_file", "search_cached"}:
        assert current.outcome == "completed", model.tool_outputs
        listed = await host.call(ReqMethod.JIUWENMATE_SHARING_LIST, agent_id=agent)
        assert not listed.payload["approvals"]
        if tool_name == "read_file":
            assert secret in str(model.tool_outputs)
        elif tool_name == "share_file":
            assert "registered" in str(model.tool_outputs)
        else:
            assert "output/report.txt" in str(model.tool_outputs)
        return
    assert current.outcome == "paused", model.tool_outputs
    assert not model.tool_outputs and secret not in str(model.calls)
    listed = await host.call(ReqMethod.JIUWENMATE_SHARING_LIST, agent_id=agent)
    approval, = listed.payload["approvals"]
    assert approval["source_groups"][0]["group_id"] == groups[0].group_chat_id
    assert approval["source_groups"][0]["platform"] == "feishu"
    assert approval["target"]["channel"] == target_platform
    assert approval["content_types"] == (["messages"] if tool_name == "search_messages" else ["files"])
    decided = await host.call(ReqMethod.JIUWENMATE_SHARING_DECIDE, agent_id=agent,
                             approval_id=approval["approval_id"], decision=decision)
    assert decided.ok, decided.payload
    async with asyncio.timeout(15):
        while manager._sessions.get_request(WEB_NAMESPACE, rid).outcome in (None, "paused") or manager.scheduler.running:
            await asyncio.sleep(0.01)
    assert manager._sessions.get_request(WEB_NAMESPACE, rid).outcome == "completed"
    assert (secret in str(model.tool_outputs)) is (decision == "once" and tool_name in {"read_file", "search_messages"})
    if decision == "once" and tool_name == "share_file":
        assert "registered" in str(model.tool_outputs)
    if decision == "once" and tool_name in {"bash", "search_files", "search_cached"}:
        assert "imports/" in str(model.tool_outputs)
