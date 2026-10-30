# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Flash workspace identity and local-memory regression tests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
import tiktoken
from openjiuwen.core.foundation.llm import AssistantMessageChunk
from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.memory.lite.manager import INDEX_CACHE
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent
from openjiuwen.core.sys_operation import (
    LocalWorkConfig,
    OperationMode,
    SysOperationCard,
)
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.schema.config import DeepAgentConfig
from openjiuwen.harness.schema.interaction import SendInputRequest
from openjiuwen.harness.workspace.workspace import Workspace

from jiuwenswarm.agents.harness.common.memory import config as memory_config
from jiuwenswarm.server.runtime.agent_adapter import interface_deep, interface_flash
from jiuwenswarm.server.runtime.agent_adapter.interface_flash import (
    JiuwenSwarmFlashAdapter,
)


class _DisabledToolsForContext(DeepAgentRail):
    def __init__(self, names):
        super().__init__()
        self._disabled_tools = names


def _isolate_other_rails(monkeypatch, adapter):
    for name in (
        "_handle_memory_rail_by_config",
        "_handle_external_memory_rail_by_config",
        "_handle_office_ace_user_profile_rail_by_config",
    ):
        monkeypatch.setattr(adapter, name, AsyncMock())
    monkeypatch.setattr(adapter, "_rebind_late_read_rails_to_local", Mock())


@pytest.mark.asyncio
async def test_context_rail_survives_consecutive_flash_requests(monkeypatch):
    adapter = JiuwenSwarmFlashAdapter()
    adapter._config_cache = {"disabled_tools": ["blocked_tool"]}
    adapter._progressive_tool_rail = SimpleNamespace(eager_tools=["read_file", "blocked_tool"])
    adapter._instance = SimpleNamespace(register_rail=AsyncMock(), unregister_rail=AsyncMock())
    _isolate_other_rails(monkeypatch, adapter)

    await adapter._update_rails_for_mode("flash")
    context_rail = adapter._context_assemble_rail
    assert context_rail is not None
    assert context_rail._disabled_tools == {"blocked_tool"}
    assert context_rail._tool_name_allowlist == ["read_file", "blocked_tool"]
    await adapter._update_rails_for_mode("flash")

    assert adapter._context_assemble_rail is context_rail
    adapter._instance.register_rail.assert_awaited_once_with(context_rail)
    adapter._instance.unregister_rail.assert_not_awaited()


@pytest.mark.asyncio
async def test_context_registration_failure_rolls_back_and_can_retry(monkeypatch):
    adapter = JiuwenSwarmFlashAdapter()
    adapter._instance = SimpleNamespace(
        register_rail=AsyncMock(side_effect=[RuntimeError("synthetic failure"), None]),
        unregister_rail=AsyncMock(),
    )
    _isolate_other_rails(monkeypatch, adapter)

    with pytest.raises(RuntimeError, match="synthetic failure"):
        await adapter._update_rails_for_mode("flash")
    assert adapter._context_assemble_rail is None
    adapter._instance.unregister_rail.assert_awaited_once()
    await adapter._update_rails_for_mode("flash")
    assert adapter._context_assemble_rail is not None
    assert adapter._instance.register_rail.await_count == 2


@pytest.mark.asyncio
async def test_concurrent_flash_requests_do_not_duplicate_context_rail(monkeypatch):
    adapter = JiuwenSwarmFlashAdapter()
    registrations = []

    async def register(rail):
        await asyncio.sleep(0)
        registrations.append(rail)

    adapter._instance = SimpleNamespace(register_rail=register, unregister_rail=AsyncMock())
    _isolate_other_rails(monkeypatch, adapter)
    await asyncio.gather(*(adapter._update_rails_for_mode("flash") for _ in range(3)))
    assert registrations == [adapter._context_assemble_rail]


def test_complete_embedding_is_forwarded_unchanged(monkeypatch):
    values = {"model": "synthetic-model", "base_url": "https://embed.invalid", "api_key": "test"}
    adapter = JiuwenSwarmFlashAdapter()
    adapter._startup_config_base = {"embed": {}}
    monkeypatch.setattr(interface_flash, "get_embed_config", lambda: dict(values))
    rail = adapter._build_memory_rail("agent")
    assert rail._embedding_config.model_name == values["model"]
    assert rail._embedding_config.base_url == values["base_url"]
    assert rail._embedding_config.api_key == values["api_key"]


@pytest.mark.parametrize("missing", ["base_url", "api_key", "model"])
def test_incomplete_embedding_still_constructs_flash_memory(monkeypatch, missing):
    values = {"model": "synthetic-model", "base_url": "https://embed.invalid", "api_key": "test"}
    values[missing] = None
    adapter = JiuwenSwarmFlashAdapter()
    adapter._startup_config_base = {"embed": {
        "embed_model": values["model"],
        "embed_base_url": values["base_url"],
        "embed_api_key": values["api_key"],
    }}
    monkeypatch.setattr(interface_flash, "get_embed_config", lambda: dict(values))

    rail = adapter._build_memory_rail("agent")
    assert rail is not None
    assert not rail._embedding_config.api_key


@pytest.mark.asyncio
async def test_flash_memory_rebuilds_when_embedding_configuration_changes(monkeypatch):
    config = {
        "memory": {"mode": "local", "engine": "builtin"},
        "modes": {"agent": {"memory": {"enabled": True}}},
        "embed": {"embed_model": "synthetic-model", "embed_base_url": None, "embed_api_key": None},
    }
    adapter = JiuwenSwarmFlashAdapter()
    adapter._startup_config_base = config
    adapter._instance = SimpleNamespace(register_rail=AsyncMock(), unregister_rail=AsyncMock())
    monkeypatch.setattr(adapter, "_schedule_memory_reindex", Mock())
    monkeypatch.setattr(interface_flash, "get_embed_config", lambda: {
        "model": config["embed"]["embed_model"],
        "base_url": config["embed"]["embed_base_url"],
        "api_key": config["embed"]["embed_api_key"],
    })

    await adapter._handle_memory_rail_by_config("agent")
    keyword_rail = adapter._memory_rail
    assert keyword_rail is not None and not keyword_rail._embedding_config.api_key

    config["embed"].update(embed_base_url="https://embed.invalid", embed_api_key="test")
    await adapter._handle_memory_rail_by_config("agent")
    vector_rail = adapter._memory_rail
    assert vector_rail is not keyword_rail
    assert vector_rail._embedding_config.api_key == "test"

    config["embed"]["embed_base_url"] = None
    await adapter._handle_memory_rail_by_config("agent")
    assert adapter._memory_rail is not vector_rail
    assert not adapter._memory_rail._embedding_config.api_key
    assert adapter._instance.register_rail.await_count == 3
    assert adapter._instance.unregister_rail.await_count == 2


@pytest.mark.asyncio
async def test_real_model_input_keeps_workspace_identity_across_reload(monkeypatch, tmp_path):
    """Two workspaces and a reload use the real SDK model-input boundary."""
    monkeypatch.setattr(tiktoken, "get_encoding", lambda _: SimpleNamespace(
        encode=lambda text, **kwargs: list(range(max(1, len(text) // 4))),
    ))
    agents = []
    sysop_ids = []
    await Runner.start()

    async def build(label):
        root = tmp_path / label
        root.mkdir()
        (root / "USER.md").write_text(f"# User\n- Name: SYNTHETIC_{label}_IDENTITY\n", encoding="utf-8")
        card = SysOperationCard(
            id="identity-test-" + uuid4().hex, mode=OperationMode.LOCAL,
            work_config=LocalWorkConfig(work_dir=str(tmp_path)),
        )
        Runner.resource_mgr.add_sys_operation(card)
        sysop_ids.append(card.id)
        sysop = Runner.resource_mgr.get_sys_operation(card.id)
        workspace = Workspace(root_path=str(root))
        agent = DeepAgent(AgentCard(id="identity-test-" + uuid4().hex, name=label))
        agents.append(agent)
        agent.configure(DeepAgentConfig(
            rails=[], workspace=workspace, sys_operation=sysop, system_prompt="Synthetic identity test.",
            enable_task_loop=False, enable_read_image_multimodal=False, max_iterations=2,
        ))
        await agent.ensure_initialized()
        for name in ("visible_marker", "blocked_marker", "deferred_marker"):
            agent.ability_manager.add(ToolCard(
                id=f"{label}-{name}", name=name, description=f"Synthetic {name}",
                input_params={"type": "object", "properties": {}},
            ))
        adapter = JiuwenSwarmFlashAdapter(workspace_dir=str(root))
        adapter._instance = agent
        adapter._parent_session_id = "identity-" + uuid4().hex
        adapter._disabled_tools_rail = _DisabledToolsForContext(["blocked_marker"])
        adapter._progressive_tool_rail = SimpleNamespace(eager_tools=["visible_marker", "blocked_marker"])
        _isolate_other_rails(monkeypatch, adapter)
        observed = []

        async def model(*, messages, **kwargs):
            observed.append("\n".join(str(message.content) for message in messages if message.role == "system"))
            yield AssistantMessageChunk(content="ok")

        monkeypatch.setattr(agent._react_agent, "_get_llm", lambda: SimpleNamespace(stream=model))
        await adapter._update_rails_for_mode("flash")
        await adapter.start_interaction(adapter._parent_session_id)
        return adapter, agent, workspace, sysop, observed

    async def turn(adapter, agent):
        stream = await agent.attach_output()
        assert stream is not None
        try:
            await agent.send_input(SendInputRequest(
                request_id=uuid4().hex,
                inputs={"query": "Who am I?", "conversation_id": adapter._parent_session_id},
            ))
            async with asyncio.timeout(15):
                async for _ in stream:
                    pass
        finally:
            await stream.close(abort_active_round=False)

    try:
        alice, alice_agent, _, alice_sysop, alice_inputs = await build("ALICE")
        bob, bob_agent, _, _, bob_inputs = await build("BOB")
        await turn(alice, alice_agent)
        await turn(bob, bob_agent)
        assert "SYNTHETIC_ALICE_IDENTITY" in alice_inputs[-1]
        assert "SYNTHETIC_BOB_IDENTITY" not in alice_inputs[-1]
        assert "SYNTHETIC_BOB_IDENTITY" in bob_inputs[-1]
        assert "SYNTHETIC_ALICE_IDENTITY" not in bob_inputs[-1]
        assert "visible_marker" in alice_inputs[-1]
        assert "blocked_marker" not in alice_inputs[-1]
        assert "deferred_marker" not in alice_inputs[-1]

        replacement = tmp_path / "ALICE_RELOADED"
        replacement.mkdir()
        (replacement / "USER.md").write_text(
            "# User\n- Name: SYNTHETIC_ALICE_RELOADED_IDENTITY\n", encoding="utf-8",
        )
        for name in (
            "_build_skill_rail", "_build_skill_credential_injection_rail",
            "_build_skill_active_state_rail", "_build_flash_skill_selection_rail",
        ):
            monkeypatch.setattr(alice, name, lambda *args, **kwargs: None)
        monkeypatch.setattr(alice, "_update_permission_rail", Mock())
        monkeypatch.setattr(
            alice, "_build_disabled_tools_rail", lambda config: _DisabledToolsForContext(config["disabled_tools"]),
        )
        rails, _ = alice._get_current_agent_rails({"disabled_tools": ["visible_marker"]}, {"react": {}})
        alice_agent.configure(DeepAgentConfig(
            rails=rails, workspace=Workspace(root_path=str(replacement)), sys_operation=alice_sysop,
            system_prompt="Synthetic reloaded identity test.", enable_task_loop=False,
            enable_read_image_multimodal=False, max_iterations=2,
        ))
        await alice_agent.ensure_initialized()
        await alice._update_rails_for_mode("flash")
        event = alice_agent._react_agent.agent_callback_manager._get_agent_event(
            AgentCallbackEvent.BEFORE_MODEL_CALL,
        )
        callbacks = Runner.callback_framework._callbacks[event]
        assert sum(getattr(info.callback, "__self__", None) is alice._context_assemble_rail for info in callbacks) == 1
        await turn(alice, alice_agent)
        assert "SYNTHETIC_ALICE_RELOADED_IDENTITY" in alice_inputs[-1]
        assert "SYNTHETIC_BOB_IDENTITY" not in alice_inputs[-1]
        assert "visible_marker" not in alice_inputs[-1]
        assert "blocked_marker" in alice_inputs[-1]
        assert "deferred_marker" not in alice_inputs[-1]
    finally:
        for agent in agents:
            await agent.stop()
            await agent.aclose()
        for sysop_id in sysop_ids:
            Runner.resource_mgr.remove_sys_operation(sysop_id)
        await Runner.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("reopen", [
    pytest.param(False, id="initial"),
    pytest.param(
        True,
        id="reopen",
        marks=pytest.mark.xfail(
            reason="agent-core misidentifies its trigram FTS table on reopen and leaves the index empty",
            raises=AssertionError,
            strict=False,
        ),
    ),
])
async def test_flash_memory_writes_and_finds_keyword_without_embedding(monkeypatch, tmp_path, reopen):
    """Exercise the real Flash tool and SDK SQLite index in a disposable workspace."""
    monkeypatch.setattr(interface_flash, "get_embed_config", lambda: {
        "model": "synthetic-model", "base_url": None, "api_key": None,
    })
    adapter = JiuwenSwarmFlashAdapter(workspace_dir=str(tmp_path))
    adapter._startup_config_base = {"embed": {"embed_model": "synthetic-model"}}
    rail = adapter._build_memory_rail("agent")
    assert rail is not None

    workspace = Workspace(root_path=str(tmp_path))
    workspace.get_node_path("memory").mkdir(parents=True, exist_ok=True)
    card = SysOperationCard(
        id="memory-test-" + uuid4().hex,
        mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(work_dir=str(tmp_path)),
    )
    agent = DeepAgent(AgentCard(id="memory-test-" + uuid4().hex, name="flash"))
    manager = None
    await Runner.start()
    try:
        Runner.resource_mgr.add_sys_operation(card)
        sysop = Runner.resource_mgr.get_sys_operation(card.id)
        agent.configure(DeepAgentConfig(
            rails=[], workspace=workspace, sys_operation=sysop, system_prompt="test",
            enable_task_loop=False, enable_read_image_multimodal=False,
        ))
        await agent.ensure_initialized()
        await agent.register_rail(rail)
        tool = rail._unified_tool
        assert tool is not None

        written = await tool.invoke({
            "mode": "write", "path": "memory/notes.md",
            "content": "The synthetic project codename is heliotropenimbus. 项目别名翠竹星河。",
        })
        assert written.success, written.error
        found = await tool.invoke({"mode": "search", "query": "heliotropenimbus"})
        manager = rail._tool_ctx.manager
        assert found.success, found.error
        assert manager is not None and manager.provider is None and manager.fts_available
        assert any("heliotropenimbus" in result["snippet"] for result in found.data["results"])
        found_chinese = await tool.invoke({"mode": "search", "query": "翠竹星河"})
        assert found_chinese.success
        assert any("翠竹星河" in result["snippet"] for result in found_chinese.data["results"])

        if reopen:
            old_manager = manager
            await old_manager.close()
            found_after_reopen = await tool.invoke({"mode": "search", "query": "heliotropenimbus"})
            manager = rail._tool_ctx.manager
            assert manager is not old_manager
            assert found_after_reopen.success
            assert any("heliotropenimbus" in result["snippet"] for result in found_after_reopen.data["results"])
    finally:
        if manager is not None:
            for key, cached in list(INDEX_CACHE.items()):
                if cached is manager:
                    INDEX_CACHE.pop(key, None)
            await manager.close()
        await agent.stop()
        await agent.aclose()
        Runner.resource_mgr.remove_sys_operation(card.id)
        await Runner.stop()


@pytest.mark.asyncio
async def test_identity_builtin_and_cloud_memory_coexist(monkeypatch, tmp_path):
    """Exercise actual Flash rails together with simulated model and cloud HTTP."""
    monkeypatch.setenv("OFFICE_ACE_DEPLOYMENT", "pc")
    monkeypatch.setenv("OFFICE_CLAW_GLOBAL_CONFIG_ROOT", str(tmp_path / "profile-cache"))
    monkeypatch.setattr(tiktoken, "get_encoding", lambda _: SimpleNamespace(
        encode=lambda text, **kwargs: list(range(max(1, len(text) // 4))),
    ))
    config = {
        "memory": {
            "mode": "local", "engine": "both",
            "external": {
                "provider": "officeace_cloud", "user_id": "synthetic-user",
                "scope_id": "synthetic-scope",
                "officeace_cloud": {"base_url": "https://memory.invalid", "api_key": "test"},
            },
            "office_ace_user_profile": {"endpoint": "https://memory.invalid", "api_key": "test"},
        },
        "modes": {"agent": {"memory": {"enabled": True}}},
        "embed": {"embed_model": "synthetic-model"},
    }
    monkeypatch.setattr(interface_deep, "get_config", lambda: config)
    monkeypatch.setattr(memory_config, "get_memory_section", lambda cfg=None: (cfg or config)["memory"])
    monkeypatch.setattr(interface_flash, "get_embed_config", lambda: {
        "model": "synthetic-model", "base_url": None, "api_key": None,
    })
    requests = []

    async def service(request):
        assert request.url.host == "memory.invalid"
        requests.append(request.url.path)
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"results": [{"content": "SYNTHETIC_CLOUD_RECALL", "score": 0.9}]})
        if request.url.path.endswith("/user-profile"):
            return httpx.Response(200, json={"content": "SYNTHETIC_CLOUD_PROFILE"})
        assert "/pc-threads/" in request.url.path
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient

    def simulated_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(service)
        return client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", simulated_client)
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "USER.md").write_text("# User\nName: SYNTHETIC_WORKSPACE_USER\n", encoding="utf-8")
    workspace = Workspace(root_path=str(root))
    workspace.get_node_path("memory").mkdir(parents=True, exist_ok=True)
    card = SysOperationCard(
        id="identity-cloud-" + uuid4().hex, mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(work_dir=str(root)),
    )
    agent = DeepAgent(AgentCard(id="identity-cloud-" + uuid4().hex, name="flash"))
    adapter = JiuwenSwarmFlashAdapter(workspace_dir=str(root))
    adapter._startup_config_base = config
    adapter._parent_session_id = "identity-cloud-" + uuid4().hex
    adapter._parent_thread_id = "synthetic-thread"
    monkeypatch.setattr(adapter, "_rebind_late_read_rails_to_local", Mock())
    observed = []
    manager = None
    await Runner.start()
    try:
        Runner.resource_mgr.add_sys_operation(card)
        sysop = Runner.resource_mgr.get_sys_operation(card.id)
        agent.configure(DeepAgentConfig(
            rails=[], workspace=workspace, sys_operation=sysop, system_prompt="Synthetic combined test.",
            enable_task_loop=False, enable_read_image_multimodal=False, max_iterations=2,
        ))
        await agent.ensure_initialized()
        adapter._instance = agent
        await adapter._update_rails_for_mode("flash")
        assert adapter._context_assemble_rail is not None
        assert adapter._memory_rail is not None
        assert adapter._external_memory_rail_registered
        assert adapter._office_ace_user_profile_rail_registered
        tool = adapter._memory_rail._unified_tool
        written = await tool.invoke({
            "mode": "write", "path": "memory/notes.md",
            "content": "Synthetic local recall: heliotropenimbus.",
        })
        assert written.success, written.error

        async def model(*, messages, **kwargs):
            observed.append([(message.role, str(message.content)) for message in messages])
            yield AssistantMessageChunk(content="ok")

        monkeypatch.setattr(agent._react_agent, "_get_llm", lambda: SimpleNamespace(stream=model))
        await adapter.start_interaction(adapter._parent_session_id)
        stream = await agent.attach_output()
        try:
            await agent.send_input(SendInputRequest(
                request_id=uuid4().hex,
                inputs={"query": "Who is the user?", "conversation_id": adapter._parent_session_id},
            ))
            async with asyncio.timeout(15):
                async for _ in stream:
                    pass
        finally:
            await stream.close(abort_active_round=False)
        if adapter._external_memory_rail._sync_task is not None:
            await adapter._external_memory_rail._sync_task
        system = "\n".join(content for role, content in observed[-1] if role == "system")
        all_messages = "\n".join(content for _, content in observed[-1])
        assert "SYNTHETIC_WORKSPACE_USER" in system
        assert "SYNTHETIC_CLOUD_RECALL" in all_messages
        assert "SYNTHETIC_CLOUD_PROFILE" in all_messages
        found = await tool.invoke({"mode": "search", "query": "heliotropenimbus"})
        manager = adapter._memory_rail._tool_ctx.manager
        assert found.success and any("heliotropenimbus" in item["snippet"] for item in found.data["results"])
        assert any(path.endswith("/search") for path in requests)
        assert any(path.endswith("/user-profile") for path in requests)
    finally:
        if manager is None and adapter._memory_rail is not None and adapter._memory_rail._tool_ctx is not None:
            manager = adapter._memory_rail._tool_ctx.manager
        if manager is not None:
            await manager.close()
        await agent.stop()
        await agent.aclose()
        Runner.resource_mgr.remove_sys_operation(card.id)
        await Runner.stop()
