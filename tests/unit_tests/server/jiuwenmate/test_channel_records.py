"""JM-10: a colleague page reads its own group records and execution trace."""

from datetime import datetime, timedelta, timezone

import pytest

from jiuwenswarm.server.jiuwenmate.channel_records import ChannelRecords, trace_rows
from jiuwenswarm.server.jiuwenmate.conversation_archive import ConversationArchive
from jiuwenswarm.server.jiuwenmate.execution_models import (
    AccessSnapshot,
    ConversationRef,
    build_execution_context,
)
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.spec_provider import publish_spec
from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.harness import persistent_agent as core
from openjiuwen.harness.schema.deep_agent_spec import TeamModelConfig

NS = "local"
AGENT = "mate_reader"
OTHER = "mate_other"
NOW = datetime(2026, 10, 17, 8, tzinfo=timezone.utc)
ACCESS = AccessSnapshot(
    principal_ref="user-1",
    startup_scope=frozenset({"chat"}),
    source_scope=frozenset({"chat"}),
    policy_revision="policy-1",
)


def _snapshot():
    return publish_spec(
        AGENT,
        "v1",
        {
            "card": {"id": AGENT, "name": "小九", "description": "助理"},
            "system_prompt": "你是小九。",
            "model": TeamModelConfig(
                model_client_config=ModelClientConfig(
                    client_id=f"jiuwenmate:{AGENT}",
                    client_provider="OpenAI",
                    api_key="sk-test",
                    api_base="https://example.invalid/v1",
                    model_name="deepseek",
                ),
                model_request_config=ModelRequestConfig(model="deepseek"),
            ),
        },
    )


def _records(tmp_path):
    groups = GroupChatRepository(tmp_path / "agents.sqlite3")
    archive = ConversationArchive(tmp_path / "group_messages", groups)
    sessions = ExecutionRepository(tmp_path / "agents.sqlite3")
    return ChannelRecords(groups, archive, sessions, clock=lambda: NOW), groups, archive, sessions


def _message(group_id, number, **changes):
    values = dict(
        message_id=f"m{number}", sender_id="user", sender_name="Alice", sender_kind="human",
        received_at=NOW + timedelta(seconds=number), text=f"决定 {number}",
    )
    return core.GroupMessage(**(values | changes))


def test_only_the_colleagues_active_group_is_listed_and_its_body_is_returned(tmp_path):
    records, groups, archive, _sessions = _records(tmp_path)
    group = groups.resolve(NS, "feishu", "oc_product", now=NOW)
    groups.set_name(NS, group.group_chat_id, "产品群")
    groups.observe_access(NS, AGENT, group.group_chat_id, "app-a", observed_at=NOW, source_ref="binding:a")
    hidden = groups.resolve(NS, "feishu", "oc_other", now=NOW)
    groups.observe_access(NS, OTHER, hidden.group_chat_id, "app-b", observed_at=NOW, source_ref="binding:b")
    archive.append(NS, group.group_chat_id, _message(group.group_chat_id, 1), source_ref="feishu:message:m1")

    listed = records.list_channels(NS, AGENT)
    assert [item["name"] for item in listed] == ["产品群"]
    page = records.messages(NS, AGENT, group.group_chat_id)
    assert page["messages"][0]["text"] == "决定 1"
    with pytest.raises(PersistentAgentError, match="FORBIDDEN|group access"):
        records.messages(NS, AGENT, hidden.group_chat_id)


def test_a_recalled_message_keeps_no_body(tmp_path):
    records, groups, archive, _sessions = _records(tmp_path)
    group = groups.resolve(NS, "feishu", "oc_product", now=NOW)
    groups.observe_access(NS, AGENT, group.group_chat_id, "app-a", observed_at=NOW, source_ref="binding:a")
    archive.append(
        NS, group.group_chat_id, _message(group.group_chat_id, 1, status="recalled", text=None),
        source_ref="feishu:message:m1",
    )
    page = records.messages(NS, AGENT, group.group_chat_id)
    assert page["messages"][0]["text"] == "" and page["messages"][0]["status"] == "recalled"


def test_an_ambiguous_binding_is_not_listed(tmp_path):
    records, groups, _archive, _sessions = _records(tmp_path)
    group = groups.resolve(NS, "feishu", "oc_product", now=NOW)
    for app_id in ("app-a", "app-b"):
        groups.update_membership(
            NS, AGENT, group.group_chat_id, app_id, status="active", observed_at=NOW, source_ref=f"member:{app_id}",
        )
    assert records.list_channels(NS, AGENT) == []


def test_trace_uses_this_colleagues_group_session(tmp_path):
    records, groups, _archive, sessions = _records(tmp_path)
    group = groups.resolve(NS, "feishu", "oc_product", now=NOW)
    groups.observe_access(NS, AGENT, group.group_chat_id, "app-a", observed_at=NOW, source_ref="binding:a")
    session = sessions.resolve_session(NS, AGENT, ConversationRef.group(group.group_chat_id), now=NOW)
    state, _created = sessions.accept_request(
        session,
        snapshot=_snapshot(),
        source_ref="feishu:message:m1",
        access=ACCESS,
        request_input=core.RequestInput(kind="scheduled", text="整理进展", reply_target="reply-1"),
        received_at=NOW,
    )
    execution = sessions.start_execution(NS, state.record.request_id, now=NOW)
    context = build_execution_context(state.record, execution, capability_snapshot=frozenset({"construction"}))
    sessions.record_events([
        core.AgentEvent.create(
            context, event_type=core.EventType.TOOL_INTENT, local_seq=1, occurred_at=NOW,
            payload={
                "operation_id": "op-1", "tool_call_id": "call-1", "tool_name": "write_file",
                "params_digest": "a" * 64, "args_summary": "写入 report.md",
            },
        ),
    ], now=NOW)

    report = records.trace(NS, AGENT, group.group_chat_id)
    contents = [row["content"] for row in report["rows"]]
    assert "整理进展" in contents
    assert any("write_file" in str(item) for item in contents)
    with pytest.raises(PersistentAgentError):
        records.trace(NS, OTHER, group.group_chat_id)


def test_trace_rows_skip_events_without_text():
    rows = trace_rows([("整理进展", ())])
    assert rows[0]["role"] == "user" and rows[0]["content"] == "整理进展"
