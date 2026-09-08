"""Regression tests for the nine-tool Celia MCP contract supplied on 2026-09-08."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jsonschema import validate

from jiuwenswarm.agents.harness.common.memory.celia.config import CeliaConfig
from jiuwenswarm.agents.harness.common.memory.celia.provider import CeliaMemoryProvider
from jiuwenswarm.agents.harness.common.memory.celia import tools as schemas


NEW_TOOLS = {
    "memory_add",
    "memory_store",
    "memory_record_search",
    "memory_global_load",
    "memory_scene_load",
    "memory_scene_search",
    "memory_backup",
    "memory_restore",
    "memory_update_config",
}


@pytest.fixture
def provider(tmp_path):
    runtime = tmp_path / ".xiaoyiruntime"
    runtime.write_text("MEMORYSTATE=true\n")
    config = CeliaConfig(
        server_binary_path="/unused/celia",
        db_path=str(tmp_path / "memory.db"),
        log_path=str(tmp_path / "memory.log"),
        workspace_dir=str(tmp_path),
        tenant_id="tenant-a",
        user_id="alice",
        scope_id="user",
        runtime_state_path=str(runtime),
    )
    current = CeliaMemoryProvider(config, user_id="alice", session_id="conversation-a")
    current._initialized = True
    current._supported_mcp_tools = NEW_TOOLS
    current._lease = SimpleNamespace(
        client=SimpleNamespace(
            call_tool=AsyncMock(return_value={"items": [{"id": "fact-a", "content": "remembered"}]}),
        )
    )
    return current


def test_wire_contract_has_exact_nine_tools_and_model_projection():
    wire = {item["name"]: item for item in schemas.mcp_tool_schemas()}
    assert set(wire) == NEW_TOOLS
    model = {item["name"]: item for item in schemas.tool_schemas()}
    assert set(model) == NEW_TOOLS - {
        "memory_add",
        "memory_backup",
        "memory_restore",
        "memory_update_config",
    }
    for item in model.values():
        assert not {"userId", "sessionId", "traceId", "requestScope", "scope", "scopeFilter"} & set(
            item["parameters"]["properties"]
        )
    assert model["memory_record_search"]["parameters"]["required"] == ["searchType", "query"]


@pytest.mark.asyncio
@pytest.mark.parametrize("search_type", ["atomic_fact", "raw_conv"])
async def test_search_uses_new_endpoint_and_preserves_response(provider, search_type):
    payload = {"searchType": search_type, "query": "debugging", "topK": 100}
    result = json.loads(await provider.handle_tool_call("memory_record_search", payload))
    assert result["ok"] is True
    call = provider.client.call_tool.await_args
    assert call.args[0] == "memory_record_search"
    assert call.args[1]["searchType"] == search_type
    assert call.args[1]["topK"] == 100
    assert call.args[1]["userId"] == "alice"
    assert call.args[1]["scopeFilter"] == 1
    assert "sessionId" not in call.args[1]  # user-wide retrieval includes past sessions
    assert result["result"] == {"items": [{"id": "fact-a", "content": "remembered"}]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool, args",
    [
        ("memory_record_search", {"query": "x"}),
        ("memory_record_search", {"query": "x", "searchType": "atomic_fact", "topK": 101}),
        ("memory_record_search", {"query": "x", "searchType": "raw_conv", "dedupPolicy": 2}),
        ("memory_record_search", {"query": "x", "searchType": "raw_conv", "recallMode": 3}),
        ("memory_scene_load", {"sceneIds": []}),
        ("memory_scene_load", {"sceneIds": ["a"] * 6}),
        ("memory_scene_search", {"subSceneTag": "x", "topK": 11}),
        ("memory_store", {"text": "old argument"}),
        ("memory_store", {"content": "海" * 27307}),
        ("memory_store", {"content": "x", "userId": "mallory"}),
        ("memory_store", {"content": "x", "scope": 0}),
        ("memory_scene_search", {"subSceneTag": "x", "requestScope": {"tenantId": "other"}}),
    ],
)
async def test_invalid_or_model_controlled_identity_never_reaches_mcp(provider, tool, args):
    result = json.loads(await provider.handle_tool_call(tool, args))
    assert result["ok"] is False
    provider.client.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_store_persists_and_does_not_claim_success_offline(provider):
    result = json.loads(
        await provider.handle_tool_call("memory_store", {"content": "I prefer concise replies"})
    )
    assert result["ok"] is True
    call = provider.client.call_tool.await_args
    assert call.args[0] == "memory_store"
    assert call.args[1]["content"] == "I prefer concise replies"
    assert call.args[1]["scope"] == 1
    assert call.args[1]["sessionId"] == "conversation-a"
    provider.client.call_tool.side_effect = RuntimeError("offline")
    result = json.loads(await provider.handle_tool_call("memory_store", {"content": "Another preference"}))
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_memory_state_off_keeps_raw_recall_and_ingestion(provider):
    from pathlib import Path

    Path(provider.config.runtime_state_path).write_text("MEMORYSTATE=false\n")
    atomic = json.loads(
        await provider.handle_tool_call(
            "memory_record_search",
            {"query": "past work", "searchType": "atomic_fact"},
        )
    )
    assert atomic["reason"] == "memory_disabled"
    assert atomic["alternative_arguments"] == {"searchType": "raw_conv"}
    raw = json.loads(
        await provider.handle_tool_call(
            "memory_record_search",
            {"query": "past work", "searchType": "raw_conv"},
        )
    )
    assert raw["ok"] is True
    provider.client.call_tool.reset_mock()
    await provider.sync_turn("user question", "assistant answer")
    calls = provider.client.call_tool.await_args_list
    assert len(calls) == 2
    for index, call in enumerate(calls):
        assert call.args[0] == "memory_add"
        assert call.args[1]["role"] == index
        assert call.args[1]["skipExtraction"] == 1
        assert call.args[1]["sessionId"] == "conversation-a"
        assert (
            not {"tenant_id", "scope", "conversationId", "ingestMode", "memoryState", "_trace_id"}
            & call.args[1].keys()
        )


@pytest.mark.asyncio
async def test_advanced_tools_require_config_and_backend_capability(provider):
    assert "memory_update_config" not in {item["name"] for item in provider.get_tool_schemas()}
    rejected = json.loads(await provider.handle_tool_call("memory_update_config", {"updates": {}}))
    assert rejected["ok"] is False
    provider.client.call_tool.assert_not_awaited()
    provider.config = replace(provider.config, advanced_tools=("memory_update_config",))
    result = json.loads(
        await provider.handle_tool_call(
            "memory_update_config", {"updates": {"migration": {"inProgress": True}}}
        )
    )
    assert result["ok"] is True
    assert provider.client.call_tool.await_args.args == (
        "memory_update_config",
        {"updates": {"migration": {"inProgress": True}}},
    )
    provider._supported_mcp_tools = set()
    assert not provider.get_tool_schemas()


@pytest.mark.asyncio
async def test_initialize_does_not_call_removed_memory_open(provider, monkeypatch):
    provider._initialized = False
    provider.client.list_tools = AsyncMock(return_value=NEW_TOOLS)
    manager = SimpleNamespace(acquire=AsyncMock(return_value=provider._lease))
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.memory.celia.provider.get_celia_client_manager",
        lambda: manager,
    )
    await provider.initialize()
    assert provider.is_initialized
    provider.client.call_tool.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name, arguments",
    [
        ("memory_store", {"content": "Remember my preference"}),
        ("memory_record_search", {"query": "travel", "searchType": "atomic_fact"}),
        ("memory_global_load", {}),
        ("memory_scene_load", {"sceneIds": ["scene-a"]}),
        ("memory_scene_search", {"subSceneTag": "travel"}),
        ("memory_backup", {}),
        ("memory_restore", {"bundleJson": "{}", "dryRun": 1}),
        ("memory_update_config", {"updates": {"migration": {"inProgress": True}}}),
    ],
)
async def test_registered_local_function_satisfies_wire_schema(provider, name, arguments):
    import asyncio
    from openjiuwen.harness.prompts.builder import SystemPromptBuilder
    from jiuwenswarm.agents.harness.common.memory.celia.rail import CeliaMemoryRail

    class Abilities:
        def __init__(self):
            self.tools = {}

        def add_ability(self, card, tool):
            self.tools[card.name] = tool
            return SimpleNamespace(added=True)

        def remove_ability(self, name):
            self.tools.pop(name, None)

    provider.config = replace(provider.config, advanced_tools=tuple(schemas.ADVANCED_TOOLS))
    provider.shutdown = AsyncMock()
    abilities = Abilities()
    agent = SimpleNamespace(ability_manager=abilities, system_prompt_builder=SystemPromptBuilder())
    rail = CeliaMemoryRail(provider)
    rail.init(agent)
    try:
        await rail._prewarm_task
        result = await abilities.tools[name].invoke(arguments)
        assert result["ok"] is True
        call = provider.client.call_tool.await_args
        assert call.args[0] == name
        wire = next(item["parameters"] for item in schemas.mcp_tool_schemas() if item["name"] == name)
        validate(call.args[1], wire)
        if name == "memory_update_config":
            assert call.args[1] == arguments
        provider._supported_mcp_tools = {"memory_store"}
        rail._register_provider_tools(agent)
        assert set(abilities.tools) == {"memory_store"}
    finally:
        rail.uninit(agent)
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_request_metadata_overrides_defaults_and_isolates_cache(provider, monkeypatch):
    from jiuwenswarm.server import request_context

    request = SimpleNamespace(
        metadata={
            "celia_user_id": "bob",
            "celia_tenant_id": "tenant-b",
            "celia_request_scope": {"project": "project-a"},
            "trace_id": "trace-b",
        },
        session_id="session-b",
        chat_id="conversation-b",
        request_id="request-b",
    )
    monkeypatch.setattr(request_context, "get_current_agent_request", lambda: request)
    context_a = provider._context()
    await provider.handle_tool_call("memory_record_search", {"searchType": "atomic_fact", "query": "x"})
    args = provider.client.call_tool.await_args.args[1]
    assert args["userId"] == "bob"
    assert args["traceId"] == "trace-b"
    assert args["requestScope"] == {"project": "project-a", "tenantId": "tenant-b"}
    request.metadata["celia_request_scope"] = {"project": "project-b"}
    assert provider._cache_key(context_a) != provider._cache_key(provider._context())


@pytest.mark.asyncio
async def test_session_authorization_filters_search(provider):
    provider._default_scope_id = "session"
    await provider.handle_tool_call("memory_record_search", {"searchType": "raw_conv", "query": "x"})
    args = provider.client.call_tool.await_args.args[1]
    assert args["scopeFilter"] == 3
    assert args["sessionId"] == "conversation-a"


@pytest.mark.asyncio
async def test_long_turn_is_split_on_utf8_boundaries_without_losing_text(provider):
    original = "海" * 30000
    await provider.sync_turn(original, "received")
    calls = provider.client.call_tool.await_args_list
    user_chunks = [call.args[1]["content"] for call in calls if call.args[1]["role"] == 0]
    assert len(user_chunks) == 2
    assert "".join(user_chunks) == original
    assert all(len(chunk.encode("utf-8")) <= 81920 for chunk in user_chunks)
    assert calls[-1].args[1]["content"] == "received"
    assert all(call.args[1]["skipExtraction"] == 0 for call in calls)


@pytest.mark.asyncio
async def test_client_serializes_new_trace_field(provider, monkeypatch):
    from jiuwenswarm.agents.harness.common.memory.celia.client import CeliaMcpClient

    client = CeliaMcpClient(provider.config)
    request = AsyncMock(return_value={"content": [{"text": '{"status":0}'}]})
    monkeypatch.setattr(client, "start", AsyncMock())
    monkeypatch.setattr(client, "_request", request)
    result = await client.call_tool("memory_global_load", {"userId": "alice"}, trace_id="trace-a")
    assert result == {"status": 0}
    assert request.await_args.args == (
        "tools/call",
        {
            "name": "memory_global_load",
            "arguments": {"userId": "alice", "traceId": "trace-a"},
        },
    )


@pytest.mark.asyncio
async def test_old_backend_contract_is_reported_and_lease_released(provider, monkeypatch):
    provider._initialized = False
    provider.client.list_tools = AsyncMock(return_value={"memory_search_l2", "memory_open"})
    lease = provider._lease
    manager = SimpleNamespace(acquire=AsyncMock(return_value=lease), release=AsyncMock())
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.memory.celia.provider.get_celia_client_manager",
        lambda: manager,
    )
    with pytest.raises(Exception, match="Celia MCP contract is missing tools"):
        await provider.initialize()
    manager.release.assert_awaited_once_with(lease)
    provider.client.call_tool.assert_not_awaited()


def test_swarm_builder_preserves_remote_request_identity(tmp_path):
    from jiuwenswarm.agents.swarm import SwarmBuildContext
    from jiuwenswarm.agents.swarm.providers.member_rails import _build_external_memory_rail

    rail = _build_external_memory_rail(
        {},
        SwarmBuildContext(
            config={"memory": {"engine": "external", "external": {"provider": "celia"}}},
            workspace=SimpleNamespace(root_path=str(tmp_path)),
            session_id="team-session",
            request_metadata={
                "celia_user_id": "remote-user",
                "celia_request_scope": {"project": "team-project"},
            },
        ),
    )
    context = rail._provider._context()
    assert context.user_id == "remote-user"
    assert context.conversation_id == "team-session"
    assert context.request_scope["project"] == "team-project"
