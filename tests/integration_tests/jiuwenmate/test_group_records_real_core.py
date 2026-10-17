"""JM-11 real SQLite and PA-07/PA-23; model and platform membership are controlled locally."""

from datetime import datetime, timezone

import pytest

from jiuwenswarm.server.jiuwenmate.budget_repository import BudgetRepository
from jiuwenswarm.server.jiuwenmate.conversation_archive import ConversationArchive
from jiuwenswarm.server.jiuwenmate.execution_models import AccessSnapshot, ConversationRef
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.execution_driver import ExecutionDriver
from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository
from jiuwenswarm.server.jiuwenmate.ports.budget import HostBudgetPort
from jiuwenswarm.server.jiuwenmate.ports.commit import CommitQueue, HostCommitPort
from jiuwenswarm.server.jiuwenmate.ports.group_chat import HostGroupChatPort
from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
from jiuwenswarm.server.jiuwenmate.ports.workspace_access import WorkspaceStore
from jiuwenswarm.server.jiuwenmate.spec_provider import build_invocation, publish_spec
from openjiuwen.core.context_engine.context.context import SessionModelContext
from openjiuwen.core.context_engine.token import TokenCounter
from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.llm.schema.message import AssistantMessage, UsageMetadata
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.harness import persistent_agent as core
from openjiuwen.harness.schema.deep_agent_spec import ModelSpec, TeamModelConfig

NOW = datetime(2026, 10, 17, tzinfo=timezone.utc)


class LocalCounter(TokenCounter):
    measurement_source = "native_tokenizer"
    measurement_estimated = False

    def count(self, text, *, model="", **kwargs):
        return len(text or "")

    def count_messages(self, messages, *, model="", **kwargs):
        return sum(len(message.model_dump_json()) for message in messages)

    def count_tools(self, tools, *, model="", **kwargs):
        return sum(len(tool.model_dump_json()) for tool in tools)


@pytest.mark.asyncio
@pytest.mark.parametrize("through_driver", [False, True, "recalled"])
async def test_group_record_reaches_participation_and_real_execution_window(tmp_path, monkeypatch, through_driver):
    received = []

    class LocalModel:
        model_client_config = None
        model_config = None

        async def invoke(self, messages, **kwargs):
            return AssistantMessage(content='{"decision":"respond","reason":"within duties"}',
                                     usage_metadata=UsageMetadata(input_tokens=3, output_tokens=3))

        async def stream(self, messages, **kwargs):
            received.extend(messages)
            yield AssistantMessageChunk(content="done", finish_reason="stop",
                                         usage_metadata=UsageMetadata(input_tokens=3, output_tokens=3))

    monkeypatch.setattr(ModelSpec, "build", lambda self: LocalModel())
    counter = LocalCounter()
    monkeypatch.setattr(SessionModelContext, "token_counter", lambda self: counter)
    db = tmp_path / "agents.sqlite3"
    groups, sessions = GroupChatRepository(db), ExecutionRepository(db)
    group = groups.resolve("local", "feishu", "oc_group", now=NOW)
    archive = ConversationArchive(tmp_path / "group_messages", groups)
    groups.observe_access("local", "agent", group.group_chat_id, "app", observed_at=NOW, source_ref="joined:1")
    background = core.GroupMessage(message_id="m0", sender_id="alice", sender_name="Alice", sender_kind="human",
                                   received_at=NOW, text="background note")
    trigger = background.model_copy(update={"message_id": "m1", "text": "do this"})
    for message in (background, trigger):
        archive.append("local", group.group_chat_id, message, source_ref=f"feishu:message:{message.message_id}")
    snapshot = publish_spec("agent", "v1", {
        "card": {"id": "agent", "name": "Agent"}, "enable_sys_operation": False,
        "enable_security_rail": False, "enable_tool_resilience_rail": False,
        "enable_read_image_multimodal": False,
        "model_input_limit_tokens": 900_000, "capacity_safety_margin_tokens": 1000,
        "model": TeamModelConfig(model_client_config=ModelClientConfig(client_provider="OpenAI", api_key="fake",
            api_base="https://example.invalid/v1"),
            model_request_config=ModelRequestConfig(context_window=1_000_000, max_tokens=4000)),
    })
    session = sessions.resolve_session("local", "agent", ConversationRef.group(group.group_chat_id), now=NOW)
    request = core.RequestInput(kind="group_message", text="do this", source=trigger, reply_target="oc_group")
    state, _ = sessions.accept_request(session, snapshot=snapshot, source_ref="feishu:message:m1",
        access=AccessSnapshot(principal_ref="alice", startup_scope=frozenset({"chat"}),
                              source_scope=frozenset({"chat"}), policy_revision="p1"),
        request_input=request, received_at=NOW)
    execution = sessions.start_execution("local", state.record.request_id, now=NOW)

    async def verify(access, group):
        return "active"

    group_port = HostGroupChatPort(groups, archive, sessions, namespace_id="local", agent_id="agent",
                                   verify_membership=verify)
    queue = CommitQueue(sessions)
    await queue.start()
    ports = core.PersistentPorts(commit=HostCommitPort(sessions, queue),
        budget=HostBudgetPort(BudgetRepository(db), sessions),
        window_backend=WindowBackendStore(db).bind("local", "agent", session.session_id), group_chat=group_port)
    spec, context = build_invocation(snapshot, state.record, execution, ports=ports)
    try:
        if through_driver:
            driver = ExecutionDriver(snapshots=lambda *args: snapshot, commit_port=ports.commit, queue=queue,
                budget_port=ports.budget, budgets=BudgetRepository(db), windows=WindowBackendStore(db),
                workspaces=WorkspaceStore(tmp_path / "workspaces"), groups=groups, group_archive=archive,
                sessions=sessions, verify_group_membership=verify)
            if through_driver == "recalled":
                archive.recall("local", group.group_chat_id, "m1", source_ref="recall:queued", changed_at=NOW)
                assert await driver.run(state, execution, driver.new_handle()) == "failed"
                assert not received
                return
            assert await driver.run(state, execution, driver.new_handle()) == "completed"
            events = sessions.read_execution_log("local", session.session_id, state.record.request_id,
                                                 execution.execution_id)
        else:
            events = [event async for event in core.execute_persistent_request(spec, context, request)]
    finally:
        await queue.aclose()
    assert events[-1].payload.status == "completed"
    assert any(event.event_type == core.EventType.PARTICIPATION_DECIDED for event in events)
    assert any("background note" in str(message.content) for message in received)
    assert sessions.read_execution_log("local", session.session_id, state.record.request_id, execution.execution_id)
