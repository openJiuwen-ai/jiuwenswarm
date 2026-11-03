"""Verify the final model boundary of isolated application role conversations."""

import asyncio
from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.app_role import APP_ROLE_SYSTEM_PROMPT, normalize_execution_profile
from jiuwenswarm.server.runtime.agent_adapter import interface_app_role as role_module
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _request(method=ReqMethod.CHAT_SEND, **params):
    return AgentRequest(
        request_id="current-turn", channel_id="web", session_id="role-session", req_method=method, params=params,
    )


def _install(monkeypatch, records=None):
    metadata = {"execution_profile": "app_role", "role_instructions": "You are the fisherman. Return reply/emotion JSON.",
                "project_id": "creating-project", "work_mode": "code"}
    monkeypatch.setattr(role_module, "get_session_metadata", lambda *a, **k: metadata)
    monkeypatch.setattr(role_module, "load_history_records", lambda *a: records or [])
    monkeypatch.setattr(role_module, "get_config", lambda: {})
    return metadata


async def _adapter(monkeypatch, model):
    adapter = role_module.AppRoleAdapter()
    adapter.mark_as_session_scoped("role-session")
    await adapter.create_instance({"channel_id": "web"}, mode="agent.code.normal")
    monkeypatch.setattr(adapter, "_create_model", lambda config: model)
    monkeypatch.setattr(adapter, "_resolve_model_for_request", lambda request: model)
    return adapter


@pytest.mark.asyncio
async def test_final_model_input_keeps_base_and_persona_without_host_prompts(monkeypatch):
    records = [
        {"role": "system", "content": "PROJECT_INSTRUCTIONS_MUST_NOT_ENTER"},
        {"role": "user", "content": "Where do you fish?", "request_id": "previous"},
        {"role": "assistant", "event_type": "chat.final", "content": "Near the pier.", "request_id": "previous"},
        {"role": "tool", "content": "PERSONAL_MEMORY_MUST_NOT_ENTER"},
        {"role": "user", "content": "Current already persisted", "request_id": "current-turn"},
    ]
    _install(monkeypatch, records)
    captured = {}

    class Model:
        async def stream(self, **kwargs):
            captured.update(kwargs)
            yield AssistantMessageChunk(content='{"reply":"Hello",')
            yield AssistantMessageChunk(content='"emotion":"calm"}')

    adapter = await _adapter(monkeypatch, Model())
    chunks = [c async for c in adapter.process_message_stream_impl(_request(), {"query": "Player input"})]
    messages = captured["messages"]
    assert captured["tools"] is None
    assert APP_ROLE_SYSTEM_PROMPT in messages[0].content
    assert "You are the fisherman" in messages[0].content
    assert [m.content for m in messages[1:]] == ["Where do you fish?", "Near the pier.", "Player input"]
    assert "MUST_NOT_ENTER" not in str(messages)
    assert adapter._instance is None
    assert chunks[-1].payload["event_type"] == "chat.final"
    assert chunks[-1].payload["content"] == '{"reply":"Hello","emotion":"calm"}'
    assert not adapter.is_session_active("role-session")


@pytest.mark.asyncio
async def test_role_rejects_equipment_and_prompt_mutation_before_model_call(monkeypatch):
    _install(monkeypatch)
    adapter = await _adapter(monkeypatch, None)
    for params in ({"mcp": ["server"]}, {"execution_profile": "default"}, {"role_instructions": "changed"}):
        chunks = [c async for c in adapter.process_message_stream_impl(_request(**params), {"query": "Hi"})]
        assert chunks[-1].payload["event_type"] == "chat.error"


@pytest.mark.asyncio
async def test_role_rejects_unexpected_model_tool_calls(monkeypatch):
    _install(monkeypatch)

    class Model:
        async def stream(self, **kwargs):
            yield SimpleNamespace(tool_calls=[{"name": "bash"}], content="", usage_metadata=None)

    adapter = await _adapter(monkeypatch, Model())
    chunks = [c async for c in adapter.process_message_stream_impl(_request(), {"query": "Hi"})]
    assert chunks[-1].payload["event_type"] == "chat.error"
    assert "tool calls" in chunks[-1].payload["error"]


@pytest.mark.asyncio
async def test_role_cancel_closes_model_stream_without_final(monkeypatch):
    _install(monkeypatch)
    entered, closed = asyncio.Event(), asyncio.Event()

    class Model:
        async def stream(self, **kwargs):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield AssistantMessageChunk(content="late")
            finally:
                closed.set()

    adapter = await _adapter(monkeypatch, Model())
    async def consume():
        return [c async for c in adapter.process_message_stream_impl(_request(), {"query": "Hi"})]
    task = asyncio.create_task(consume())
    await entered.wait()
    result = await adapter.process_interrupt(_request(wait_for_stop=True))
    assert result.ok
    assert task.cancelled()
    assert closed.is_set()
    assert not adapter.is_session_active("role-session")


@pytest.mark.asyncio
async def test_root_restores_role_adapter_without_building_general_agent(monkeypatch):
    from jiuwenswarm.server.runtime.session import session_metadata

    metadata = _install(monkeypatch)
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda *a, **k: metadata)
    root = JiuWenSwarmDeepAdapter()
    first = await root._get_or_create_session_adapter("role-session")
    assert isinstance(first, role_module.AppRoleAdapter)
    assert first._instance is None
    assert await root._get_or_create_session_adapter("role-session") is first
    assert await root.resolve_runtime_binding("role-session", {}) == {"kind": "app_role"}
    with pytest.raises(ValueError, match="not supported"):
        await root.resolve_runtime_binding("role-session", {"agent_template_name": "expert"})


def test_facade_keeps_role_input_raw_and_disables_cloud_memory(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
    from jiuwenswarm.server.runtime.session import session_metadata

    metadata = _install(monkeypatch)
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda *a, **k: metadata)
    facade = object.__new__(JiuWenSwarm)
    inputs, memory_mode, turn = facade._build_inputs(_request(query='{"player_says":"Hello"}'))
    assert inputs["query"] == turn.text == '{"player_says":"Hello"}'
    assert inputs["enable_memory"] is False
    assert memory_mode == "off"


@pytest.mark.asyncio
async def test_profile_survives_fork_and_stale_metadata_writes(monkeypatch, tmp_path):
    from jiuwenswarm.common import utils
    from jiuwenswarm.server.runtime.session import session_metadata, session_history
    from jiuwenswarm.agents.harness.common import session_ops_service

    monkeypatch.setattr(utils, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(session_ops_service, "get_agent_sessions_dir", lambda: tmp_path)
    session_metadata.init_session_metadata(
        session_id="role-source", work_mode="code", execution_profile="app_role", role_instructions="Fisherman",
    )
    session_metadata.update_session_metadata(session_id="role-source", title="New title", sync_write=True)
    stale = dict(session_metadata.get_session_metadata("role-source"))
    stale.pop("execution_profile")
    stale.pop("role_instructions")
    session_metadata._enqueue_write("role-source", stale, sync_write=True)
    session_ops_service.fork_session(source_session_id="role-source", target_session_id="role-fork")
    restored = session_metadata.get_session_metadata("role-fork", cache_bust=True)
    assert restored["execution_profile"] == "app_role"
    assert restored["role_instructions"] == "Fisherman"
    assert restored["work_mode"] == "code"
    monkeypatch.setattr(role_module, "get_session_metadata", session_metadata.get_session_metadata)
    root = JiuWenSwarmDeepAdapter()
    restored_adapter = await root._get_or_create_session_adapter("role-fork")
    assert isinstance(restored_adapter, role_module.AppRoleAdapter)
    assert restored_adapter._instance is None


def test_profile_validation_and_context_budget():
    for profile, instructions in (("unknown", "rules"), ("app_role", " "), ("default", "rules")):
        with pytest.raises(ValueError):
            normalize_execution_profile(profile, instructions)
    records = [
        {"role": "user", "request_id": "old", "content": "x" * 48000},
        {"role": "assistant", "event_type": "chat.final", "request_id": "old", "content": "answer"},
    ]
    messages = role_module.build_role_messages("persona", records, "Hi", "new")
    assert len(messages) == 2
    assert APP_ROLE_SYSTEM_PROMPT in messages[0].content
    with pytest.raises(ValueError, match="size limit"):
        role_module.build_role_messages("persona", [], "x" * 48000, "new")
