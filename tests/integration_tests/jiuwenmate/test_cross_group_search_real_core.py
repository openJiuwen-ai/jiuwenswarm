"""JM-21: a real PA-23 tool call searches another authorized group from a private Session."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from jiuwenswarm.server.jiuwenmate.budget_repository import BudgetRepository
from jiuwenswarm.server.jiuwenmate.conversation_archive import ConversationArchive
from jiuwenswarm.server.jiuwenmate.execution_driver import ExecutionDriver
from jiuwenswarm.server.jiuwenmate.execution_models import AccessSnapshot, ConversationRef
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository
from jiuwenswarm.server.jiuwenmate.ports.budget import HostBudgetPort
from jiuwenswarm.server.jiuwenmate.ports.commit import CommitQueue, HostCommitPort
from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
from jiuwenswarm.server.jiuwenmate.ports.workspace_access import WorkspaceStore
from jiuwenswarm.server.jiuwenmate.spec_provider import publish_spec
from openjiuwen.core.context_engine.context.context import SessionModelContext
from openjiuwen.core.context_engine.token import TokenCounter
from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.llm.schema.message import ToolMessage, UsageMetadata
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
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


class SearchToolModel:
    """Make one real PA-23 tool call, then surface exactly what the tool returned."""

    model_client_config = None
    model_config = None

    def __init__(self) -> None:
        self.calls: list[list] = []
        self.tool_outputs: list[str] = []

    async def invoke(self, messages, **kwargs):
        raise AssertionError("the local model streams the native agent")

    async def stream(self, messages, **kwargs):
        self.calls.append(list(messages))
        results = [message.content for message in messages if isinstance(message, ToolMessage)]
        usage = UsageMetadata(input_tokens=3, output_tokens=4)
        if not results:
            call = ToolCall(
                id="search-call-1",
                type="function",
                name="search_group_messages",
                arguments=json.dumps({"query": "release decision", "group_names": ["产品群"], "limit": 10}),
            )
            yield AssistantMessageChunk(content="", tool_calls=[call], finish_reason="tool_calls",
                                         usage_metadata=usage)
            return
        self.tool_outputs.extend(str(result) for result in results)
        yield AssistantMessageChunk(content=str(results[-1]), finish_reason="stop", usage_metadata=usage)


@pytest.mark.asyncio
async def test_private_execution_searches_another_authorized_group_with_real_core(tmp_path, monkeypatch):
    model = SearchToolModel()
    monkeypatch.setattr(ModelSpec, "build", lambda self: model)
    monkeypatch.setattr(SessionModelContext, "token_counter", lambda self: LocalCounter())

    db = tmp_path / "agents.sqlite3"
    groups = GroupChatRepository(db)
    sessions = ExecutionRepository(db)
    archive = ConversationArchive(tmp_path / "group_messages", groups)
    target = groups.resolve("local", "feishu", "oc_product", now=NOW)
    unrelated = groups.resolve("local", "feishu", "oc_engineering", now=NOW)
    private = groups.resolve("local", "feishu", "oc_private_only", now=NOW)
    groups.set_name("local", target.group_chat_id, "产品群")
    groups.set_name("local", unrelated.group_chat_id, "研发群")
    groups.set_name("local", private.group_chat_id, "产品群")
    for group in (target, unrelated):
        groups.observe_access("local", "agent-a", group.group_chat_id, "app-a", observed_at=NOW,
                              source_ref=f"joined:{group.group_chat_id}")
    groups.observe_access("local", "agent-b", private.group_chat_id, "app-b", observed_at=NOW,
                          source_ref="joined:private")
    archive.append(
        "local",
        target.group_chat_id,
        core.GroupMessage(
            message_id="m-product-1", sender_id="alice", sender_name="Alice", sender_kind="human",
            received_at=NOW, text="release decision: move launch to Friday",
        ),
        source_ref="feishu:message:m-product-1",
    )
    archive.append(
        "local",
        private.group_chat_id,
        core.GroupMessage(
            message_id="m-private-1", sender_id="bob", sender_name="Bob", sender_kind="human",
            received_at=NOW, text="release decision: confidential private group",
        ),
        source_ref="feishu:message:m-private-1",
    )

    snapshot = publish_spec("agent-a", "v1", {
        "card": {"id": "agent-a", "name": "Agent A"},
        "enable_sys_operation": False,
        "enable_security_rail": False,
        "enable_tool_resilience_rail": False,
        "enable_read_image_multimodal": False,
        "model_input_limit_tokens": 900_000,
        "capacity_safety_margin_tokens": 1000,
        "model": TeamModelConfig(
            model_client_config=ModelClientConfig(
                client_provider="OpenAI", api_key="fake", api_base="https://example.invalid/v1",
            ),
            model_request_config=ModelRequestConfig(context_window=1_000_000, max_tokens=4000),
        ),
    })
    session = sessions.resolve_session("local", "agent-a", ConversationRef.im_direct("feishu", "user-a"), now=NOW)
    request_input = core.RequestInput(
        kind="direct_message",
        text="产品群的发布决定是什么？",
        source=core.DirectMessageSource(
            message_id="dm-1", sender_id="user-a", sender_name="User", received_at=NOW,
        ),
        reply_target="user-a",
    )
    state, _ = sessions.accept_request(
        session,
        snapshot=snapshot,
        source_ref="feishu:dm-1",
        access=AccessSnapshot(
            principal_ref="user-a", startup_scope=frozenset({"chat"}), source_scope=frozenset({"chat"}),
            policy_revision="p1",
        ),
        request_input=request_input,
        received_at=NOW,
    )
    execution = sessions.start_execution("local", state.record.request_id, now=NOW)

    async def verify(access, group):
        return "active"

    queue = CommitQueue(sessions)
    await queue.start()
    budgets = BudgetRepository(db)
    driver = ExecutionDriver(
        snapshots=lambda *args: snapshot,
        commit_port=HostCommitPort(sessions, queue),
        queue=queue,
        budget_port=HostBudgetPort(budgets, sessions),
        budgets=budgets,
        windows=WindowBackendStore(db),
        workspaces=WorkspaceStore(tmp_path / "workspaces"),
        groups=groups,
        group_archive=archive,
        sessions=sessions,
        verify_group_membership=verify,
        clock=lambda: NOW,
    )
    try:
        outcome = await driver.run(state, execution, driver.new_handle())
        events = sessions.read_execution_log("local", session.session_id, state.record.request_id,
                                             execution.execution_id)
    finally:
        await queue.aclose()

    assert outcome == "completed"
    assert any(hit.group_chat_id in model.tool_outputs[0] for hit in (target,))
    assert "产品群" in model.tool_outputs[0]
    assert "move launch to Friday" in model.tool_outputs[0]
    assert "confidential private group" not in model.tool_outputs[0]
    assert any(event.event_type == core.EventType.TOOL_COMPLETED for event in events)
