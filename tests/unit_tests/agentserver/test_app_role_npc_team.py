"""Verify NPC roster validation, equipment rejection, and member consultations."""

import asyncio
from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.llm.schema.message import ToolMessage
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.app_role import (
    APP_ROLE_MEMBER_PROMPT,
    APP_ROLE_SYSTEM_PROMPT,
    normalize_npc_team,
    validate_app_role_request,
)
from jiuwenswarm.server.runtime.agent_adapter import interface_app_role as role_module


def _request(**params):
    return AgentRequest(
        request_id="current-turn", channel_id="web", session_id="role-session",
        req_method=ReqMethod.CHAT_SEND, params=params,
    )


def _install(monkeypatch, metadata, records=None):
    monkeypatch.setattr(role_module, "get_session_metadata", lambda *a, **k: metadata)
    monkeypatch.setattr(role_module, "load_history_records", lambda *a: records or [])
    monkeypatch.setattr(role_module, "get_config", lambda: {})
    return metadata


def _leader_metadata(**overrides):
    metadata = {
        "execution_profile": "app_role",
        "role_instructions": "You are the village chief. Return reply/emotion JSON.",
        "project_id": "creating-project",
        "work_mode": "code",
        "npc_team": {
            "members": [
                {
                    "name": "fisherman",
                    "role_instructions": "You are the old fisherman.",
                    "title": "",
                    "desc": "Knows the sea and the catch.",
                }
            ]
        },
        "npc_member_sessions": {"fisherman": "member-session-1"},
    }
    metadata.update(overrides)
    return metadata


def _member_metadata():
    return {
        "execution_profile": "app_role",
        "role_instructions": "You are the old fisherman.",
        "project_id": "creating-project",
        "work_mode": "code",
        "npc_member": {"leader_session_id": "role-session", "name": "fisherman"},
    }


async def _adapter(monkeypatch, model):
    adapter = role_module.AppRoleAdapter()
    adapter.mark_as_session_scoped("role-session")
    await adapter.create_instance({"channel_id": "web"}, mode="agent.code.normal")
    monkeypatch.setattr(adapter, "_create_model", lambda config: model)
    monkeypatch.setattr(adapter, "_resolve_model_for_request", lambda request: model)
    return adapter


def _tool_call(name, arguments):
    return ToolCall(id="call-1", type="function", name=name, arguments=arguments)


# ── roster validation and equipment rejection ────────────────────────────────────


def test_normalize_npc_team_accepts_and_defaults():
    team = normalize_npc_team(
        {"members": [{"name": "fisherman", "role_instructions": "Old fisherman rules."}]}
    )
    assert team["members"][0]["name"] == "fisherman"
    assert team["members"][0]["desc"] == "Old fisherman rules."
    assert normalize_npc_team(None) is None


@pytest.mark.parametrize(
    "team",
    [
        {"members": []},
        {"members": [{"name": "x", "role_instructions": "rules"}]},
        {"members": [{"name": "Bad_Name", "role_instructions": "rules"}]},
        {"members": [{"name": "dup", "role_instructions": "a"}, {"name": "dup", "role_instructions": "b"}]},
        {"members": [{"name": "ok", "role_instructions": " "}]},
        {"members": [{"name": "ok", "role_instructions": "rules"}], "extra": 1},
        {"members": [{"name": "ok", "role_instructions": "r", "unknown": 1}]},
        {"members": [{"name": f"m{i}", "role_instructions": "r"} for i in range(6)]},
        "not-a-dict",
    ],
)
def test_normalize_npc_team_rejects_invalid(team):
    with pytest.raises(ValueError):
        normalize_npc_team(team)


def test_validate_app_role_request_rejects_equipment_and_pins_roster():
    metadata = _leader_metadata()
    validate_app_role_request(metadata, {"tools": []})
    validate_app_role_request(
        metadata,
        {
            "npc_team": {
                "members": [
                    {
                        "name": "fisherman",
                        "role_instructions": "You are the old fisherman.",
                        "title": "",
                        "desc": "Knows the sea and the catch.",
                    }
                ]
            }
        },
    )
    with pytest.raises(ValueError, match="not supported"):
        validate_app_role_request(metadata, {"tools": ["web_search"]})
    with pytest.raises(ValueError, match="not supported"):
        validate_app_role_request(metadata, {"tools": ["bash"]})
    with pytest.raises(ValueError, match="fixed at session creation"):
        validate_app_role_request(
            metadata,
            {"npc_team": {"members": [{"name": "merchant", "role_instructions": "Different roster."}]}},
        )


# ── adapter tool loop ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_role_keeps_tools_none(monkeypatch):
    _install(
        monkeypatch,
        {
            "execution_profile": "app_role",
            "role_instructions": "Plain role.",
            "project_id": "p", "work_mode": "code",
        },
    )
    captured = {}

    class Model:
        async def stream(self, **kwargs):
            captured.update(kwargs)
            yield AssistantMessageChunk(content="ok")

    adapter = await _adapter(monkeypatch, Model())
    chunks = [c async for c in adapter.process_message_stream_impl(_request(), {"query": "Hi"})]
    assert captured["tools"] is None
    assert chunks[-1].payload["event_type"] == "chat.final"


@pytest.mark.asyncio
async def test_leader_asks_member_and_receives_tool_result(monkeypatch):
    metadata = _leader_metadata()
    _install(monkeypatch, metadata)
    captured = []

    class Model:
        def __init__(self):
            self.round = 0

        async def stream(self, **kwargs):
            captured.append(kwargs)
            self.round += 1
            if self.round == 1:
                yield AssistantMessageChunk(
                    tool_calls=[_tool_call("ask_fisherman", '{"question": "How is the sea?"}')],
                )
            else:
                yield AssistantMessageChunk(content='{"reply":"The sea is calm"}')

    asked = {}

    class Executor:
        async def ask(self, member_sid, name, question, channel_id):
            asked["sid"] = member_sid
            asked["name"] = name
            asked["question"] = question
            return "The sea is rough near the pier.", {"total_tokens": 5}

    adapter = await _adapter(monkeypatch, Model())
    adapter.bind_member_executor(Executor())
    chunks = [c async for c in adapter.process_message_stream_impl(_request(), {"query": "Ask the fisherman"})]

    tool_names = {definition["function"]["name"] for definition in captured[0]["tools"]}
    assert tool_names == {"ask_fisherman"}
    assert asked == {
        "sid": "member-session-1", "name": "fisherman", "question": "How is the sea?",
    }
    member_events = [c.payload for c in chunks if c.payload.get("event_type") == "npc.member"]
    assert [event["phase"] for event in member_events] == ["started", "finished"]
    assert member_events[-1]["summary"] == "The sea is rough near the pier."
    # The consultation reply must reach the model as a tool message.
    second_round_messages = captured[1]["messages"]
    tool_messages = [m for m in second_round_messages if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].content == "The sea is rough near the pier."
    assert tool_messages[0].tool_call_id == "call-1"
    assert chunks[-1].payload["event_type"] == "chat.final"
    assert chunks[-1].payload["content"] == '{"reply":"The sea is calm"}'


@pytest.mark.asyncio
async def test_member_session_exposes_no_ask_tools(monkeypatch):
    _install(monkeypatch, _member_metadata())
    captured = {}

    class Model:
        async def stream(self, **kwargs):
            captured.update(kwargs)
            yield AssistantMessageChunk(content="members answer")

    adapter = await _adapter(monkeypatch, Model())
    adapter.bind_member_executor(SimpleNamespace(ask=None))
    chunks = [c async for c in adapter.process_message_stream_impl(_request(), {"query": "Sea?"})]
    assert captured["tools"] is None
    assert APP_ROLE_MEMBER_PROMPT in captured["messages"][0].content
    assert "You are the old fisherman." in captured["messages"][0].content
    assert chunks[-1].payload["event_type"] == "chat.final"


@pytest.mark.asyncio
async def test_member_base_prompt_used_for_member_sessions(monkeypatch):
    _install(monkeypatch, _member_metadata())

    class Model:
        async def stream(self, **kwargs):
            yield AssistantMessageChunk(content="ok")

    adapter = await _adapter(monkeypatch, Model())
    captured = {}

    async def capture_stream(**kwargs):
        captured.update(kwargs)
        yield AssistantMessageChunk(content="ok")

    adapter._resolve_model_for_request = lambda request: SimpleNamespace(stream=capture_stream)
    [c async for c in adapter.process_message_stream_impl(_request(), {"query": "Hi"})]
    assert APP_ROLE_MEMBER_PROMPT in captured["messages"][0].content
    assert APP_ROLE_SYSTEM_PROMPT not in captured["messages"][0].content


@pytest.mark.asyncio
async def test_tool_budget_exceeded_raises(monkeypatch):
    _install(monkeypatch, _leader_metadata())

    class Model:
        async def stream(self, **kwargs):
            yield AssistantMessageChunk(tool_calls=[_tool_call("ask_fisherman", '{"question": "q"}')])

    async def ask(member_sid, name, question, channel_id):
        return "member answer", None

    adapter = await _adapter(monkeypatch, Model())
    adapter.bind_member_executor(SimpleNamespace(ask=ask))
    chunks = [c async for c in adapter.process_message_stream_impl(_request(), {"query": "Hi"})]
    assert chunks[-1].payload["event_type"] == "chat.error"
    assert "tool call budget" in chunks[-1].payload["error"]


@pytest.mark.asyncio
async def test_member_failure_degrades_to_error_text(monkeypatch):
    metadata = _leader_metadata()
    _install(monkeypatch, metadata)

    class Model:
        def __init__(self):
            self.round = 0

        async def stream(self, **kwargs):
            self.round += 1
            if self.round == 1:
                yield AssistantMessageChunk(tool_calls=[_tool_call("ask_fisherman", '{"question": "q"}')])
            else:
                yield AssistantMessageChunk(content="recovered answer")

    async def failing_ask(member_sid, name, question, channel_id):
        raise RuntimeError("member session deleted")

    adapter = await _adapter(monkeypatch, Model())
    adapter.bind_member_executor(SimpleNamespace(ask=failing_ask))
    chunks = [c async for c in adapter.process_message_stream_impl(_request(), {"query": "Hi"})]
    member_events = [c.payload for c in chunks if c.payload.get("event_type") == "npc.member"]
    assert member_events[-1]["phase"] == "finished"
    assert "error" in member_events[-1]
    assert chunks[-1].payload["event_type"] == "chat.final"


@pytest.mark.asyncio
async def test_leader_rejects_unadvertised_search_tool(monkeypatch):
    _install(monkeypatch, _leader_metadata())

    class Model:
        async def stream(self, **kwargs):
            assert [tool["name"] for tool in kwargs["tools"]] == ["ask_fisherman"]
            yield AssistantMessageChunk(tool_calls=[_tool_call("web_search", '{"query":"q"}')])

    adapter = await _adapter(monkeypatch, Model())
    adapter.bind_member_executor(SimpleNamespace(ask=None))
    chunks = [c async for c in adapter.process_message_stream_impl(_request(), {"query": "Hi"})]
    assert chunks[-1].payload["event_type"] == "chat.error"
    assert "does not support tool: web_search" in chunks[-1].payload["error"]


# ── member executor persistence contract ─────────────────────────────────────


@pytest.mark.asyncio
async def test_member_executor_persists_consultation_history(monkeypatch):
    persisted = []

    def fake_append(**kwargs):
        persisted.append(kwargs)

    class MemberAdapter:
        async def process_message_stream_impl(self, request, inputs):
            from jiuwenswarm.common.schema.agent import AgentResponseChunk

            yield AgentResponseChunk(
                request_id=request.request_id, channel_id=request.channel_id,
                payload={"event_type": "chat.delta", "content": "partial"},
            )
            yield AgentResponseChunk(
                request_id=request.request_id, channel_id=request.channel_id,
                payload={"event_type": "chat.usage_summary", "session_id": request.session_id,
                         "usage": {"total_tokens": 7}},
            )
            yield AgentResponseChunk(
                request_id=request.request_id, channel_id=request.channel_id,
                payload={"event_type": "chat.final", "content": "The tide is low."},
                is_complete=True,
            )

    class Facade:
        async def _get_or_create_session_adapter(self, session_id, **kwargs):
            return MemberAdapter()

    monkeypatch.setattr(role_module, "append_history_record", fake_append)
    monkeypatch.setattr(
        role_module, "get_session_metadata",
        lambda *a, **k: _member_metadata(),
    )
    executor = role_module.AppRoleMemberExecutor(Facade(), "role-session")
    answer, usage = await executor.ask(
        "member-session-1", "fisherman", "How is the sea?", "web",
    )
    assert answer == "The tide is low."
    assert usage == {"total_tokens": 7}
    user_record, final_record = persisted
    assert user_record["role"] == "user"
    assert user_record["content"] == "How is the sea?"
    assert user_record["request_id"] == final_record["request_id"]
    assert final_record["role"] == "assistant"
    assert final_record["event_type"] == "chat.final"
    assert final_record["content"] == "The tide is low."
    assert final_record["extra"]["completion_state"] == "completed"


@pytest.mark.asyncio
async def test_member_executor_rejects_empty_answer(monkeypatch):
    class MemberAdapter:
        async def process_message_stream_impl(self, request, inputs):
            from jiuwenswarm.common.schema.agent import AgentResponseChunk

            yield AgentResponseChunk(
                request_id=request.request_id, channel_id=request.channel_id,
                payload={"event_type": "chat.final", "content": ""},
                is_complete=True,
            )

    class Facade:
        async def _get_or_create_session_adapter(self, session_id, **kwargs):
            return MemberAdapter()

    monkeypatch.setattr(role_module, "append_history_record", lambda **kwargs: None)
    monkeypatch.setattr(role_module, "get_session_metadata", lambda *a, **k: _member_metadata())
    executor = role_module.AppRoleMemberExecutor(Facade(), "role-session")
    with pytest.raises(ValueError, match="empty answer"):
        await executor.ask("member-session-1", "fisherman", "q", "web")


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [
    {},
    {"execution_profile": "default"},
    {"execution_profile": "app_role", "role_instructions": "member",
     "npc_member": {"leader_session_id": "another-leader", "name": "fisherman"}},
])
async def test_member_executor_rejects_missing_or_rebound_member_before_assembly(monkeypatch, metadata):
    from unittest.mock import AsyncMock, Mock

    facade = SimpleNamespace(_get_or_create_session_adapter=AsyncMock())
    persist = Mock()
    monkeypatch.setattr(role_module, "get_session_metadata", lambda *a, **k: metadata)
    monkeypatch.setattr(role_module, "append_history_record", persist)
    executor = role_module.AppRoleMemberExecutor(facade, "role-session")
    with pytest.raises(ValueError, match="missing or no longer bound"):
        await executor.ask("member-session-1", "fisherman", "q", "web")
    facade._get_or_create_session_adapter.assert_not_awaited()
    persist.assert_not_called()


@pytest.mark.asyncio
async def test_member_deleted_during_selection_never_builds_general_agent(monkeypatch):
    from unittest.mock import Mock
    from jiuwenswarm.server.runtime.session import session_metadata

    root = role_module.JiuWenSwarmDeepAdapter()
    general_factory = Mock(side_effect=AssertionError("must not assemble a general agent"))
    persist = Mock()
    monkeypatch.setattr(role_module, "get_session_metadata", lambda *a, **k: _member_metadata())
    # The first read succeeded, but the member disappeared before the root lock.
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda *a, **k: {})
    monkeypatch.setattr(root, "_new_session_scoped_adapter", general_factory)
    monkeypatch.setattr(role_module, "append_history_record", persist)
    executor = role_module.AppRoleMemberExecutor(root, "role-session")
    with pytest.raises(ValueError, match="missing or no longer bound"):
        await executor.ask("member-session-1", "fisherman", "q", "web")
    general_factory.assert_not_called()
    persist.assert_not_called()
