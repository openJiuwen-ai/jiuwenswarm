"""Exercise external memory assembly through swarm specs and code adapters."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage, AssistantMessageChunk
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.schema.config import DeepAgentConfig

from jiuwenswarm.agents.harness.common.memory.celia.config import CeliaConfig
from jiuwenswarm.agents.harness.common.memory.celia.prompt import load_celia_agent_prompt
from jiuwenswarm.agents.harness.common.memory.celia.rail import CeliaMemoryRail
from jiuwenswarm.agents.swarm import SwarmBuildContext, register_swarm_providers
from jiuwenswarm.agents.swarm import registry
from jiuwenswarm.agents.swarm.config_specs import build_member_capability_specs
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter


@pytest.fixture
def memory_config():
    return {
        "memory": {
            "engine": "external",
            "external": {"provider": "celia", "user_id": "alice", "scope_id": "user"},
        }
    }


@pytest.fixture
def unavailable_backend(monkeypatch):
    # Even an enabled preflight must never stop the builder mounting the rail.
    preflight = Mock(return_value=["Celia requires Linux"])
    manager = SimpleNamespace(acquire=AsyncMock(side_effect=RuntimeError("backend unavailable")))
    monkeypatch.setattr(CeliaConfig, "preflight_issues", preflight)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.memory.celia.provider.get_celia_client_manager",
        lambda: manager,
    )
    return manager


def _agent(name):
    card = AgentCard(id=name, name=name)
    agent = DeepAgent(card)
    agent.configure(DeepAgentConfig(card=card, system_prompt="Test agent.", language="en"))
    return agent


async def _capture_model_request(agent, *, streaming=False):
    """Run the real agent, callbacks and context engine; replace only the LLM."""
    captured = []

    async def invoke(**kwargs):
        captured.append(kwargs)
        return AssistantMessage(content="OK")

    async def stream(**kwargs):
        captured.append(kwargs)
        yield AssistantMessageChunk(content="OK")

    agent._react_agent.set_llm(SimpleNamespace(invoke=invoke, stream=stream))
    inputs = {"query": "Check memory injection.", "conversation_id": agent.card.id}
    if streaming:
        async for _ in agent.stream(inputs):
            pass
    else:
        result = await agent.invoke(inputs)
        assert result["output"] == "OK"
    assert len(captured) == 1
    return captured[0]


def _assert_model_received_celia_prompt(request):
    systems = [message.content for message in request["messages"] if message.role == "system"]
    assert len(systems) == 1
    assert systems[0].count(load_celia_agent_prompt()) == 1
    tools = {tool.name: tool for tool in request["tools"]}
    assert "memory_global_load" in tools
    assert "memory_scene_search" in tools
    assert tools["memory_record_search"].parameters["required"] == ["searchType", "query"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["team", "team.plan", "code.team", "design", "design.normal", "design.plan"])
@pytest.mark.parametrize("role", ["leader", "teammate"])
@pytest.mark.parametrize("preflight_enabled", [False, True])
async def test_swarm_member_mounts_celia_prompt_when_backend_is_unavailable(
    tmp_path, memory_config, unavailable_backend, mode, role, preflight_enabled,
):
    register_swarm_providers()
    memory_config["memory"]["external"]["celia"] = {"preflight_enabled": preflight_enabled}
    specs, _ = build_member_capability_specs(memory_config, mode, role)
    external_specs = [spec for spec in specs if spec.type == registry.EXTERNAL_MEMORY]
    assert len(external_specs) == 1
    context = SwarmBuildContext(
        config=memory_config,
        mode=mode,
        role=role,
        session_id="conversation-a",
        workspace=SimpleNamespace(root_path=str(tmp_path)),
    )
    rail = external_specs[0].build(language="en", context=context)
    assert isinstance(rail, CeliaMemoryRail)
    assert rail._session_id == "conversation-a"
    assert rail._provider.config.workspace_dir == str(tmp_path)
    agent = _agent(f"celia-{mode}-{role}")
    try:
        await agent.register_rail(rail)
        await rail._prewarm_task
        assert rail._initialized is False
        assert load_celia_agent_prompt() in agent.system_prompt_builder.build()
        assert rail._owned_tool_names == {
            schema["name"] for schema in rail._provider.get_tool_schemas()
        }
        assert "memory_store" in rail._owned_tool_names
        _assert_model_received_celia_prompt(await _capture_model_request(agent))
    finally:
        await agent.unregister_rail(rail)
        await asyncio.sleep(0)
    assert agent.system_prompt_builder.get_section(SectionName.EXTERNAL_MEMORY) is None


@pytest.mark.parametrize("engine, provider", [("builtin", "celia"), ("none", "celia"), ("external", "")])
def test_swarm_external_memory_respects_disable_config(tmp_path, memory_config, engine, provider):
    register_swarm_providers()
    memory_config["memory"]["engine"] = engine
    memory_config["memory"]["external"]["provider"] = provider
    specs, _ = build_member_capability_specs(memory_config, "team", "leader")
    spec = next(spec for spec in specs if spec.type == registry.EXTERNAL_MEMORY)
    context = SwarmBuildContext(config=memory_config, workspace=SimpleNamespace(root_path=str(tmp_path)))
    assert spec.build(language="en", context=context) is None


@pytest.mark.asyncio
async def test_code_and_design_mode_switches_preserve_one_memory_rail_and_can_disable_it(
    tmp_path, monkeypatch, memory_config, unavailable_backend,
):
    monkeypatch.setattr(interface_deep, "get_config", lambda: memory_config)
    adapter = object.__new__(JiuwenSwarmCodeAdapter)
    adapter._instance = _agent("celia-code-adapter")
    adapter._workspace_dir = str(tmp_path)
    adapter._parent_session_id = "conversation-code"
    adapter._external_memory_rail = None
    adapter._external_memory_rail_registered = False
    # Unrelated code rails are already mounted; use the real memory lifecycle.
    adapter._subagent_rail = object()
    adapter._project_memory_rail = object()
    adapter._coding_memory_rail = object()
    mounted = None
    try:
        for mode in ("code.normal", "code.plan", "design", "design.normal", "design.plan", "code.normal"):
            adapter._instance.configure(DeepAgentConfig(
                card=adapter._instance.card,
                system_prompt=f"Test agent in {mode}.",
                language="en",
                rails=[],
            ))
            await adapter._update_rails_for_mode(mode)
            rail = adapter._external_memory_rail
            assert isinstance(rail, CeliaMemoryRail)
            await rail._prewarm_task
            if mounted is None:
                mounted = rail
            assert rail is mounted
            assert adapter._external_memory_rail_registered is True
            _assert_model_received_celia_prompt(await _capture_model_request(adapter._instance))
    finally:
        memory_config["memory"]["engine"] = "none"
        await adapter._update_rails_for_mode("code.normal")
        await asyncio.sleep(0)
    assert adapter._external_memory_rail is None
    assert adapter._external_memory_rail_registered is False
    assert adapter._instance.system_prompt_builder.get_section(SectionName.EXTERNAL_MEMORY) is None
    assert mounted._owned_tool_names == set()
    request = await _capture_model_request(adapter._instance)
    assert all(load_celia_agent_prompt() not in message.content for message in request["messages"])
    assert not any(tool.name.startswith("memory_") for tool in request["tools"] or [])


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_deep_adapter_injects_prompt_into_final_model_request(
    tmp_path, monkeypatch, memory_config, unavailable_backend, streaming,
):
    monkeypatch.setattr(interface_deep, "get_config", lambda: memory_config)
    adapter = object.__new__(interface_deep.JiuWenSwarmDeepAdapter)
    adapter._instance = _agent(f"celia-deep-adapter-{streaming}")
    adapter._workspace_dir = str(tmp_path)
    adapter._parent_session_id = "conversation-deep"
    adapter._external_memory_rail = None
    adapter._external_memory_rail_registered = False
    try:
        await adapter._handle_external_memory_rail_by_config()
        _assert_model_received_celia_prompt(
            await _capture_model_request(adapter._instance, streaming=streaming)
        )
    finally:
        memory_config["memory"]["engine"] = "none"
        await adapter._handle_external_memory_rail_by_config()
        await asyncio.sleep(0)
