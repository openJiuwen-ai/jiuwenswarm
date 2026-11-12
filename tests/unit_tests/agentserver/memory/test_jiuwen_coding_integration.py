# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Swarm Coding assembly uses dependency stubs at the Core/Memory boundary."""
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.memory import jiuwen_rail_builder as builder
from jiuwenswarm.agents.harness.common.rails.memory_forbidden_rail import _targets_memory
from jiuwenswarm.common.coding_memory_paths import resolve_coding_memory_project_name
from jiuwenswarm.server.runtime.agent_adapter import interface_code, interface_deep


class _MemoryRailStub(DeepAgentRail):
    def __init__(self, provider, **identity):
        super().__init__()
        self._provider = provider
        self._identity = identity
        self._agent_id = identity["agent_id"]
        self._session_id = identity["session_id"]
        self.init = Mock()
        self.uninit = Mock()
        self.aclose = AsyncMock()


@pytest.fixture(autouse=True)
def memory_dependencies(monkeypatch):
    """Record boundary arguments without requiring unreleased Core/Memory APIs."""
    import openjiuwen.harness.rails as rails

    class JiuwenMemoryRail(_MemoryRailStub):
        pass

    class JiuwenMemoryCodingRail(_MemoryRailStub):
        pass

    def provider(**options):
        return SimpleNamespace(options=options, is_available=lambda: True, shutdown=AsyncMock())

    module = ModuleType("jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider")
    module.AgentMemoryMemoryProvider = provider
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(rails, "JiuwenMemoryRail", JiuwenMemoryRail, raising=False)
    monkeypatch.setattr(rails, "JiuwenMemoryCodingRail", JiuwenMemoryCodingRail, raising=False)
    monkeypatch.setattr(builder, "_build_security_provider", lambda org, user: object())


def config():
    return {"modes": {mode: {"memory": {"enabled": True}} for mode in ("code", "agent")},
            "memory": {"mode": "local", "engine": "builtin", "builtin": {
                "provider": "jiuwen", "jiuwen": {"mode": "http", "agent_id": "shared"}}}}


def adapter(monkeypatch, tmp_path, cfg):
    obj = interface_code.JiuwenSwarmCodeAdapter()
    obj._project_dir = str(tmp_path / "repo")
    obj._workspace_dir = str(tmp_path)
    obj._agent_workspace_dir = str(tmp_path / "agent")
    monkeypatch.setattr(obj, "_active_code_config", lambda: cfg)
    return obj


@pytest.mark.parametrize("name,session", [("main_agent", ""), ("review_agent", "configured-session")])
def test_code_builder_binds_project_and_work_runtime_identity(monkeypatch, tmp_path, name, session):
    cfg = config()
    cfg["memory"]["builtin"]["jiuwen"]["session_id"] = session
    obj = adapter(monkeypatch, tmp_path, cfg)
    obj._agent_name = name
    monkeypatch.setattr(interface_deep, "get_config", lambda: cfg)
    work_adapter = interface_deep.JiuWenSwarmDeepAdapter()
    work_adapter._agent_name = name
    code = obj._build_coding_memory_rail()
    work = work_adapter._build_memory_rail("agent.work.normal")
    assert type(code).__name__ == "JiuwenMemoryCodingRail"
    assert code._provider.options["scene"] == "code"
    assert work._provider.options.get("scene", "agent") == "agent"
    assert code._provider.options["project"] == resolve_coding_memory_project_name(obj._project_dir)
    assert code._identity["agent_id"] == work._agent_id == name
    assert code._identity["session_id"] == work._session_id == session
    assert code is obj._build_coding_memory_rail()
    obj._project_dir = str(tmp_path / "other" / "repo")
    changed = obj._build_coding_memory_rail()
    assert changed is not code
    assert changed._provider.options["project"] != code._provider.options["project"]
    team = builder.build_jiuwen_memory_rail(cfg, agent_name="team-member")
    assert team._agent_id == "team-member" and team._session_id == session


@pytest.mark.asyncio
@pytest.mark.parametrize("configured,override,definition,expected", [
    (None, {}, None, "main_agent"),
    ("configured", {}, None, "configured"),
    ("configured", {"agent_name": "instance"}, None, "instance"),
    ("configured", {"agent_name": "instance"},
     {"name": "custom-agent", "instructions": "Be helpful."}, "custom-agent"),
])
async def test_work_code_creation_resolves_the_same_name(
    monkeypatch, tmp_path, configured, override, definition, expected,
):
    cfg = config()
    cfg["react"] = {"project_dir": str(tmp_path / "repo")}
    if configured is not None:
        cfg["react"]["agent_name"] = configured
    monkeypatch.setattr(interface_code, "get_config", lambda: cfg)
    monkeypatch.setattr(interface_deep, "get_config", lambda: cfg)
    monkeypatch.setattr(interface_deep, "load_dotenv_runtime", lambda **kw: None)
    work = interface_deep.JiuWenSwarmDeepAdapter()
    code = interface_code.JiuwenSwarmCodeAdapter()
    for obj in (work, code):
        monkeypatch.setattr(obj, "set_checkpoint", AsyncMock())
        monkeypatch.setattr(obj, "_refresh_multimodal_configs", lambda _cfg: None)
        monkeypatch.setattr(obj, "_load_enterprise_config",
                            AsyncMock(return_value=SimpleNamespace(config=cfg)))
        monkeypatch.setattr(obj, "_sync_enterprise_skill_prebuilt", AsyncMock())
        await obj.create_instance(override, agent_definition=definition)
        assert obj._agent_name == expected
    assert work._build_memory_rail("agent.work.normal")._agent_id == expected
    assert code._build_coding_memory_rail()._identity["agent_id"] == expected


@pytest.mark.parametrize("scene", ["agent", "code"])
def test_builder_identity_fallback_preserves_configured_session(scene):
    cfg = config()
    cfg["memory"]["builtin"]["jiuwen"]["session_id"] = "configured-session"
    rail = builder.build_jiuwen_memory_rail(cfg, scene=scene, shared_document_root=True)
    assert rail._provider.options["agent_id"] == "shared"
    del cfg["memory"]["builtin"]["jiuwen"]["agent_id"]
    rail = builder.build_jiuwen_memory_rail(cfg, scene=scene, shared_document_root=True)
    assert rail._provider.options["agent_id"] == "jiuwenswarm"
    session = rail._identity["session_id"] if scene == "code" else rail._session_id
    assert session == "configured-session"


@pytest.mark.parametrize("blocked", ["enabled", "engine", "mode", "eternal", "initialize"])
def test_jiuwen_selection_never_falls_back_to_legacy(monkeypatch, tmp_path, blocked):
    cfg = config()
    obj = adapter(monkeypatch, tmp_path, cfg)
    if blocked == "enabled":
        cfg["modes"]["code"]["memory"]["enabled"] = False
    elif blocked == "engine":
        cfg["memory"]["engine"] = "external"
    elif blocked == "mode":
        cfg["memory"]["mode"] = "cloud"
    elif blocked == "eternal":
        obj._eternal_conversation_enabled = True
    else:
        monkeypatch.setattr(interface_code, "build_jiuwen_memory_rail", lambda *a, **kw: None)
    def legacy(**kw):
        pytest.fail("selected jiuwen must not initialize legacy memory")
    monkeypatch.setattr(interface_code, "create_coding_memory_rail", legacy)
    assert obj._build_coding_memory_rail() is None
    assert obj.uses_jiuwen_coding_memory()


@pytest.mark.asyncio
async def test_reload_replaces_and_disables_code_memory_without_work_rail(monkeypatch, tmp_path):
    cfg = config()
    obj = adapter(monkeypatch, tmp_path, cfg)
    obj._instance = SimpleNamespace(
        unregister_rail=AsyncMock(), register_rail=AsyncMock(),
        deep_config=SimpleNamespace(subagents=[]))
    first = obj._build_coding_memory_rail()
    first.aclose = AsyncMock()
    obj._agent_name = "new-identity"
    await obj._handle_memory_rail_by_config("agent.code.normal")
    obj._instance.unregister_rail.assert_awaited_with(first)
    first.aclose.assert_awaited_once()
    assert obj._memory_rail is None
    current = obj._coding_memory_rail
    assert current._identity["agent_id"] == "new-identity"
    current.aclose = AsyncMock()
    cfg["modes"]["code"]["memory"]["enabled"] = False
    await obj._handle_memory_rail_by_config("agent.code.normal")
    assert obj._coding_memory_rail is None
    current.aclose.assert_awaited_once()
    await obj._handle_external_memory_rail_by_config()
    assert obj._external_memory_rail is None


def test_new_write_edit_are_guarded_without_changing_work_tools():
    assert _targets_memory("agent_memory_coding_memory_write", {})
    assert _targets_memory("agent_memory_coding_memory_edit", {})
    assert not _targets_memory("agent_memory_coding_memory_read", {})
    assert not _targets_memory("agent_memory_conclude", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("jiuwen", [False, True])
async def test_auto_memory_keeps_legacy_tools_and_excludes_jiuwen_rail(monkeypatch, tmp_path, jiuwen):
    from openjiuwen.harness import factory
    from jiuwenswarm.agents.harness.common.auto_memory import extraction_runner as runner

    runtime_rail, project_rail, coding_rail = object(), object(), object()
    normal_tool = SimpleNamespace(name="read_file")
    jiuwen_tools = [SimpleNamespace(name=f"agent_memory_coding_memory_{action}")
                   for action in ("read", "write", "edit", "search")]
    parent_tools = [normal_tool, *jiuwen_tools] if jiuwen else [normal_tool]
    legacy_tool = SimpleNamespace(name="coding_memory_write")
    ctx = SimpleNamespace(memory_tools=[legacy_tool], workspace=object(),
                          sys_operation=object(), cleanup=Mock())
    create_context = AsyncMock(return_value=ctx)
    monkeypatch.setattr(runner, "_create_memory_extraction_context", create_context)
    parent = SimpleNamespace(
        uses_jiuwen_coding_memory=lambda: jiuwen,
        _runtime_prompt_rail=runtime_rail, _project_memory_rail=project_rail,
        _coding_memory_rail=coding_rail,
        _instance=SimpleNamespace(
            deep_config=SimpleNamespace(model=object(), system_prompt="parent prompt"),
            ability_manager=SimpleNamespace(list=lambda: parent_tools),
        ),
    )
    captured = {}

    class ExtractionAgent:
        create_new_context_engine = AsyncMock(return_value="auto-memory-test")

        async def stream(self, inputs, *, session):
            captured["query"] = inputs["query"]
            yield {"content": "done"}

    def create_agent(**kwargs):
        captured.update(kwargs)
        agent = ExtractionAgent()
        agent.card = kwargs["card"]
        return agent

    monkeypatch.setattr(factory, "create_deep_agent", create_agent)
    await runner._run_memory_extraction_with_cache_sharing(
        memory_dir=tmp_path, prompt="Extract memories", existing_memories=[],
        history=[{"role": "user", "content": "Use pytest"}], parent_agent=parent,
    )
    assert runtime_rail in captured["rails"] and project_rail in captured["rails"]
    assert (coding_rail in captured["rails"]) is not jiuwen
    assert captured["tools"] == [normal_tool, legacy_tool]
    assert "coding_memory_write" in captured["query"]
    create_context.assert_awaited_once_with(
        tmp_path, agent_id="auto_memory_cache_subagent", sysop_id_prefix="extract_memories_cache_",
    )
    ctx.cleanup.assert_called_once()


@pytest.mark.parametrize("markdown", [
    None, {"default": None}, {"default": {"params": None}}, {"default": "local"},
    {"default": {"target": "local", "params": {"root": "/explicit/root"}}},
])
def test_sdk_uses_shared_root_without_mutating_config(monkeypatch, markdown):
    from copy import deepcopy

    cfg = config()
    cfg["memory"]["builtin"]["jiuwen"].update({
        "mode": "sdk", "sdk": {"markdown_store": markdown}})
    original = deepcopy(cfg)
    monkeypatch.setattr(builder, "get_embed_config", lambda: {})
    code = builder.build_jiuwen_memory_rail(cfg, scene="code", shared_document_root=True)
    work = builder.build_jiuwen_memory_rail(cfg, shared_document_root=True)
    assert code is not None and work is not None
    assert code._provider.options["security_provider"] is not None
    assert work._provider.options["security_provider"] is not None
    code_store = code._provider.options["config_dict"]["markdown_store"]["default"]
    assert code_store == work._provider.options["config_dict"]["markdown_store"]["default"]
    assert code_store["params"]["root"]
    if markdown and isinstance(markdown["default"], dict):
        params = markdown["default"].get("params") or {}
        if "root" in params:
            assert code_store["params"]["root"] == params["root"]
    assert cfg == original


@pytest.mark.asyncio
async def test_work_rebinds_memory_when_runtime_name_changes(monkeypatch):
    cfg = config()
    monkeypatch.setattr(interface_deep, "get_config", lambda: cfg)
    obj = interface_deep.JiuWenSwarmDeepAdapter()
    obj._instance = SimpleNamespace(unregister_rail=AsyncMock(), register_rail=AsyncMock())
    await obj._handle_memory_rail_by_config("agent.work.normal")
    first = obj._memory_rail
    assert first._agent_id == "main_agent"
    await obj._handle_memory_rail_by_config("agent.work.normal")
    assert obj._memory_rail is first
    obj._agent_name = "review_agent"
    await obj._handle_memory_rail_by_config("agent.work.normal")
    obj._instance.unregister_rail.assert_awaited_once_with(first)
    assert obj._memory_rail._agent_id == "review_agent"


@pytest.mark.parametrize("mode", ["http", "sdk"])
@pytest.mark.parametrize("changed", ["root", "models", "embed", "workspace"])
def test_memory_rebuild_ignores_local_settings_only_in_http(monkeypatch, tmp_path, mode, changed):
    cfg = config()
    jiuwen = cfg["memory"]["builtin"]["jiuwen"]
    jiuwen.update({"mode": mode, "sdk": {"markdown_store": {
        "default": {"target": "local", "params": {"root": str(tmp_path / "unused")}}}}})
    monkeypatch.setattr(builder, "get_embed_config", lambda: {})
    obj = adapter(monkeypatch, tmp_path, cfg)
    first = obj._build_coding_memory_rail()
    assert first is not None
    if changed == "root":
        jiuwen["sdk"]["markdown_store"]["default"]["params"]["root"] = str(tmp_path / "other")
    elif changed == "workspace":
        obj._agent_workspace_dir = str(tmp_path / "another-workspace")
    else:
        cfg[changed] = {"model": "changed-local-model"}
    assert (obj._build_coding_memory_rail() is first) == (mode == "http")
    if mode == "http":
        assert first._provider.options.get("config_dict") is None
        assert first._provider.options.get("security_provider") is None
        assert not (tmp_path / "unused").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["root", "base_url", "agent_id", "provider", "enabled"])
async def test_http_v08_reload_uses_effective_memory_config(monkeypatch, tmp_path, change):
    cfg = config()
    obj = adapter(monkeypatch, tmp_path, cfg)
    obj._instance = SimpleNamespace(unregister_rail=AsyncMock(), register_rail=AsyncMock(),
                                    deep_config=SimpleNamespace(subagents=[]))
    first = obj._build_coding_memory_rail()
    first.aclose = AsyncMock()
    jiuwen = cfg["memory"]["builtin"]["jiuwen"]
    if change == "root":
        jiuwen["sdk"] = {"markdown_store": {"default": {"params": {"root": "unused"}}}}
    elif change == "base_url":
        jiuwen["base_url"] = "http://127.0.0.1:9813"
    elif change == "agent_id":
        obj._agent_name = "another-agent"
    elif change == "provider":
        cfg["memory"]["builtin"]["provider"] = ""
    else:
        cfg["modes"]["code"]["memory"]["enabled"] = False
    await obj._handle_memory_rail_by_config("agent.code.normal")
    if change == "root":
        assert obj._coding_memory_rail is first
        first.aclose.assert_not_awaited()
        obj._instance.register_rail.assert_not_awaited()
    else:
        first.aclose.assert_awaited_once()
        assert obj._coding_memory_rail is not first
    if change == "provider":
        assert type(obj._coding_memory_rail).__name__ == "CodingMemoryRail"
    assert obj._memory_rail is None and obj._external_memory_rail is None


@pytest.mark.asyncio
async def test_v01_v05_mode_update_preserves_exact_memory_rail_set(monkeypatch, tmp_path):
    cfg = config()
    obj = adapter(monkeypatch, tmp_path, cfg)
    registered = []
    async def register(rail):
        registered.append(rail)
    async def unregister(rail):
        registered.remove(rail)
    obj._instance = SimpleNamespace(register_rail=register, unregister_rail=unregister,
                                    deep_config=SimpleNamespace(subagents=[]))
    obj._subagent_rail = object()
    monkeypatch.setattr(obj, "_sync_personal_context_rail", AsyncMock())
    for provider, enabled, expected in [
        ("", True, {"CodingMemoryRail", "ProjectMemoryRail"}),
        ("jiuwen", True, {"JiuwenMemoryCodingRail", "ProjectMemoryRail"}),
        ("", True, {"CodingMemoryRail", "ProjectMemoryRail"}),
        ("jiuwen", True, {"JiuwenMemoryCodingRail", "ProjectMemoryRail"}),
        ("jiuwen", False, set()),
    ]:
        cfg["memory"]["builtin"]["provider"] = provider
        cfg["modes"]["code"]["memory"]["enabled"] = enabled
        await obj._update_rails_for_mode("agent.code.normal")
        assert {type(rail).__name__ for rail in registered} == expected
        assert obj._memory_rail is None and obj._external_memory_rail is None


def test_v04_only_code_agent_reuses_the_http_memory_rail(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    cfg = config()
    obj = adapter(monkeypatch, tmp_path, cfg)
    obj._sys_operation = MagicMock()
    monkeypatch.setattr(obj, "_browser_runtime_enabled", lambda: False)
    rail = obj._build_coding_memory_rail()
    specs, _ = obj._build_configured_subagents(
        MagicMock(), {"subagents": {"code_agent": {"enabled": True}}}, cfg)
    by_name = {spec.agent_card.name: spec for spec in specs}
    assert rail in by_name["code_agent"].rails
    for name in ("explore_agent", "plan_agent"):
        assert rail not in (by_name[name].rails or [])


@pytest.mark.asyncio
async def test_http_root_change_keeps_client_through_full_spec_reload(monkeypatch, tmp_path):
    from copy import copy
    from unittest.mock import MagicMock

    from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
    from openjiuwen.core.runner import Runner
    from openjiuwen.harness.factory import create_deep_agent

    cfg = config()
    obj = adapter(monkeypatch, tmp_path, cfg)
    obj._last_mode = "agent.code.normal"
    rail = obj._build_coding_memory_rail()
    model = Model(model_client_config=ModelClientConfig(
        client_provider="OpenAI", api_key="mock", api_base="http://mock-base"),
        model_config=ModelRequestConfig(model="mock-model"))
    agent = create_deep_agent(model=model, rails=[rail], workspace=str(tmp_path),
                              enable_task_loop=False, add_general_purpose_agent=False,
                              enable_read_image_multimodal=False)
    obj._instance = agent
    obj._code_spec_rails = [rail]
    await Runner.start()
    try:
        await agent.ensure_initialized()
        lifecycle = Mock()
        lifecycle.attach_mock(rail.init, "mount")
        lifecycle.attach_mock(rail.uninit, "unmount")
        ensure_initialized = agent.ensure_initialized

        async def ensure_after_remount():
            lifecycle.ready()
            await ensure_initialized()

        monkeypatch.setattr(agent, "ensure_initialized", ensure_after_remount)
        cfg["memory"]["builtin"]["jiuwen"]["sdk"] = {"markdown_store": {
            "default": {"target": "local", "params": {"root": str(tmp_path / "unused")}}}}
        candidate = obj._build_coding_memory_rail()
        assert candidate is rail
        parts = SimpleNamespace(config=copy(agent.deep_config), rails=[candidate],
                                tool_cards=[], tool_instances=[])
        spec = MagicMock()
        spec.resolve_parts.return_value = parts
        context = SimpleNamespace(tool_owner_id=None, artifacts=SimpleNamespace(tools=[]))
        monkeypatch.setattr(obj, "_apply_reload_config_snapshot", AsyncMock(return_value=cfg))
        monkeypatch.setattr(obj, "_create_model", lambda _: model)
        monkeypatch.setattr(obj, "_build_code_spec_snapshot", lambda *a, **kw: (spec, context))
        for name in ("_load_active_packages", "load_user_rails", "_sync_mcp_servers_for_runtime",
                     "_fan_out_reload_to_session_adapters", "_sync_skill_retrieval_prompt_rail_for_runtime"):
            monkeypatch.setattr(obj, name, AsyncMock())
        for name in ("_sync_active_evolution_review_agent_after_reload", "_sync_paid_search_tool_for_runtime",
                     "_sync_symphony_tools_for_runtime", "_sync_skill_retrieval_tools_for_runtime"):
            monkeypatch.setattr(obj, name, MagicMock())
        await obj.reload_agent_config(cfg, {})
        assert obj._coding_memory_rail is rail
        rail.aclose.assert_not_awaited()
        assert lifecycle.mock_calls[:3] == [
            call.unmount(agent), call.mount(agent), call.ready(),
        ]
        assert sum(item is rail for item in agent.configured_rails()) == 1
    finally:
        await agent.unregister_rail(rail)
        await Runner.stop()


@pytest.mark.parametrize("markdown", [[], {"default": []}, {"default": {"params": []}}])
def test_sdk_rejects_invalid_mapping_instead_of_discarding_config(markdown):
    with pytest.raises(ValueError, match="must be a mapping"):
        builder._sdk_inline_config({"sdk": {"markdown_store": markdown}}, shared_document_root=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["models", "project", "identity"])
@pytest.mark.parametrize("raises", [False, True])
async def test_failed_jiuwen_rebuild_clears_memory_and_can_recover(
    monkeypatch, tmp_path, changed, raises,
):
    cfg = config()
    cfg["memory"]["builtin"]["jiuwen"].update({"mode": "sdk"})
    monkeypatch.setattr(builder, "get_embed_config", lambda: {})
    obj = adapter(monkeypatch, tmp_path, cfg)
    obj._instance = SimpleNamespace(register_rail=AsyncMock(), unregister_rail=AsyncMock(),
                                    deep_config=SimpleNamespace(subagents=[]))
    first = obj._build_coding_memory_rail()
    assert first is not None
    first.aclose = AsyncMock()
    project_rail = object()
    obj._project_memory_rail = project_rail
    obj._code_spec_rails = [project_rail, first]
    code_spec = SimpleNamespace(agent_card=SimpleNamespace(name="code_agent"), rails=[first])
    obj._instance.deep_config.subagents = [code_spec]
    monkeypatch.setattr(interface_code, "create_coding_memory_rail",
                        lambda **kwargs: pytest.fail("Jiuwen must not fall back to the native rail"))
    if changed == "models":
        cfg["models"] = {"model": "changed-model"}
    elif changed == "project":
        obj._memory_project_dir = str(tmp_path / "another-project")
    else:
        obj._agent_name = "another-agent"
    original = interface_code.build_jiuwen_memory_rail

    def fail_build(*args, **kwargs):
        if raises:
            raise ValueError("invalid memory configuration")
        return None

    monkeypatch.setattr(interface_code, "build_jiuwen_memory_rail", fail_build)
    await obj._reconcile_coding_memory()
    assert obj._coding_memory_rail is None
    assert obj._coding_memory_binding is None
    assert obj._coding_memory_config_fingerprint is None
    first.aclose.assert_awaited_once()
    obj._instance.unregister_rail.assert_awaited_once_with(first)
    obj._instance.register_rail.assert_not_awaited()
    assert obj._project_memory_rail is project_rail
    assert obj._code_spec_rails == [project_rail]
    assert first not in code_spec.rails
    monkeypatch.setattr(interface_code, "build_jiuwen_memory_rail", original)
    await obj._reconcile_coding_memory()
    assert obj._coding_memory_rail is not None and obj._coding_memory_rail is not first
    obj._instance.register_rail.assert_awaited_once_with(obj._coding_memory_rail)
    assert obj._coding_memory_rail in code_spec.rails


def test_native_memory_fingerprint_matches_constructor_directory(monkeypatch, tmp_path):
    cfg = config()
    cfg["memory"]["builtin"]["provider"] = ""
    obj = adapter(monkeypatch, tmp_path, cfg)
    calls = []

    def build(**kwargs):
        calls.append(kwargs["project_dir"])
        return object()

    monkeypatch.setattr(interface_code, "create_coding_memory_rail", build)
    first = obj._build_coding_memory_rail()
    obj._memory_project_dir = str(tmp_path / "runtime-project")
    assert obj._build_coding_memory_rail() is first
    assert calls == [obj._project_dir]
    obj._project_dir = None
    assert obj._build_coding_memory_rail() is not first
    assert calls[-1] == obj._workspace_dir
