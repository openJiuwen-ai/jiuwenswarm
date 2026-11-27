# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Swarm Coding assembly uses dependency stubs at the Core/Memory boundary."""
import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.memory import jiuwen_rail_builder as builder
from jiuwenswarm.agents.harness.common.memory import team_memory_members
from jiuwenswarm.agents.harness.common.rails.memory_forbidden_rail import _targets_memory
from jiuwenswarm.common.coding_memory_paths import resolve_coding_memory_project_name
from jiuwenswarm.common.memory_config import get_builtin_memory_provider
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
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

    class JiuwenTeamMemoryRail(_MemoryRailStub):
        pass

    class Provider:
        def __init__(self, **options):
            self.options = options
            self.shutdown = AsyncMock()

        def is_available(self):
            return True

    module = ModuleType("jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider")
    module.AgentMemoryMemoryProvider = Provider
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(rails, "JiuwenMemoryRail", JiuwenMemoryRail, raising=False)
    monkeypatch.setattr(rails, "JiuwenMemoryCodingRail", JiuwenMemoryCodingRail, raising=False)
    monkeypatch.setattr(rails, "JiuwenTeamMemoryRail", JiuwenTeamMemoryRail, raising=False)
    monkeypatch.setattr(builder, "_build_security_provider", lambda org, user: object())
    prepare = AsyncMock(return_value=True)
    monkeypatch.setattr(team_memory_members, "prepare_project_memory", prepare)
    return prepare


def config():
    return {"modes": {mode: {"memory": {"enabled": True}} for mode in ("code", "agent")},
            "memory": {"mode": "local", "engine": "builtin", "builtin": {
                "provider": "jiuwen", "jiuwen": {"mode": "http", "agent_id": "shared"}}}}


@pytest.mark.parametrize("section,expected", [
    ({}, ""),
    ({"memory": None}, ""),
    ({"memory": {"builtin": []}}, ""),
    ({"memory": {"builtin": {"provider": " jiuwen "}}}, "jiuwen"),
    ({"memory": {"builtin": {"provider": "native"}}}, "native"),
])
def test_provider_reader_preserves_explicit_config_and_builder_fallback(
    monkeypatch, section, expected,
):
    load_config = Mock(return_value=config())
    monkeypatch.setattr(builder, "_load_config", load_config)
    assert get_builtin_memory_provider(section) == expected
    assert builder.get_builtin_memory_provider(section) == expected
    load_config.assert_not_called()
    assert get_builtin_memory_provider(None) == ""
    assert builder.get_builtin_memory_provider() == "jiuwen"
    load_config.assert_called_once_with()


def adapter(monkeypatch, tmp_path, cfg):
    obj = interface_code.JiuwenSwarmCodeAdapter()
    obj._project_dir = str(tmp_path / "repo")
    obj._workspace_dir = str(tmp_path)
    obj._agent_workspace_dir = str(tmp_path / "agent")
    monkeypatch.setattr(obj, "_active_code_config", lambda: cfg)
    return obj


@pytest.mark.parametrize("name,session", [("main_agent", ""), ("review_agent", "configured-session")])
@pytest.mark.parametrize("mode", ["http", "sdk"])
def test_work_code_share_configured_identity_and_team_keeps_stable_binding(monkeypatch, tmp_path, name, session, mode):
    cfg = config()
    cfg["memory"]["builtin"]["jiuwen"].update(session_id=session, mode=mode)
    monkeypatch.setattr(builder, "get_embed_config", lambda: {})
    obj = adapter(monkeypatch, tmp_path, cfg)
    obj._agent_name = name
    monkeypatch.setattr(interface_deep, "get_config", lambda: cfg)
    work_adapter = interface_deep.JiuWenSwarmDeepAdapter()
    work_adapter._agent_name = "work-" + name
    code = obj._build_coding_memory_rail()
    work_adapter._instance = SimpleNamespace(_jiuwenswarm_memory_project_dir=obj._project_dir)
    work = work_adapter._build_memory_rail("agent.work.normal", cfg)
    assert type(code).__name__ == "JiuwenMemoryCodingRail"
    assert code._provider.options["scene"] == "code"
    assert work._provider.options.get("scene", "agent") == "agent"
    assert code._provider.options["project"] == resolve_coding_memory_project_name(obj._project_dir)
    assert code._provider.options["collective_coordinates"] == work._provider.options["collective_coordinates"] == {
        "project": code._provider.options["project"],
    }
    assert type(work).__name__ == "SwarmJiuwenMemoryRail"
    work_context = SimpleNamespace(agent=work_adapter._instance)
    assert work._resolve_project(work_context) == code._provider.options["project"]
    assert code._identity["agent_id"] == work._agent_id == "shared"
    assert code._identity["session_id"] == work._session_id == session
    assert code is obj._build_coding_memory_rail()
    obj._project_dir = str(tmp_path / "other" / "repo")
    changed = obj._build_coding_memory_rail()
    assert changed is not code
    assert changed._provider.options["project"] != code._provider.options["project"]
    team = builder.build_jiuwen_memory_rail(cfg, agent_name="team-member", scene="team",
        project="repo", team_binding={"team": "repo--" + "a" * 24, "member": "member"})
    assert team._provider.options["collective_coordinates"] == {"project": "repo", "team": "repo--" + "a" * 24}
    assert (team._agent_id, team._session_id) == ("main_agent", "")
    assert {rail._provider.options["agent_id"] for rail in (code, work)} == {"shared"}
    assert team._provider.options["agent_id"] == "main_agent"
    assert obj._agent_name == name and work_adapter._agent_name == "work-" + name


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
    assert work._build_memory_rail("agent.work.normal", cfg)._agent_id == "shared"
    assert code._build_coding_memory_rail()._identity["agent_id"] == "shared"


@pytest.mark.parametrize("scene", ["agent", "code"])
def test_builder_identity_fallback_preserves_configured_session(scene):
    cfg = config()
    cfg["memory"]["builtin"]["jiuwen"]["session_id"] = "configured-session"
    rail = builder.build_jiuwen_memory_rail(cfg, scene=scene, shared_document_root=True)
    assert rail._provider.options["agent_id"] == "shared"
    del cfg["memory"]["builtin"]["jiuwen"]["agent_id"]
    rail = builder.build_jiuwen_memory_rail(cfg, scene=scene, shared_document_root=True)
    assert rail._provider.options["agent_id"] == "main_agent"
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
async def test_reload_removes_preexisting_legacy_code_memory(monkeypatch, tmp_path):
    cfg = config()
    obj = adapter(monkeypatch, tmp_path, cfg)
    obj._instance = SimpleNamespace(
        unregister_rail=AsyncMock(), register_rail=AsyncMock(),
        deep_config=SimpleNamespace(subagents=[]),
    )
    first = obj._build_coding_memory_rail()
    first.aclose = AsyncMock()
    await obj._handle_memory_rail_by_config("agent.code.normal")
    obj._instance.unregister_rail.assert_awaited_once_with(first)
    first.aclose.assert_awaited_once()
    obj._instance.register_rail.assert_not_awaited()
    assert obj._coding_memory_rail is None
    assert obj._memory_rail is None


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
async def test_jiuwen_team_work_does_not_mount_legacy_work_rail(monkeypatch):
    cfg = config()
    monkeypatch.setattr(interface_deep, "get_config", lambda: cfg)
    monkeypatch.setattr(interface_deep, "is_memory_enabled", lambda mode, cfg: True)
    obj = interface_deep.JiuWenSwarmDeepAdapter()
    obj._instance = SimpleNamespace(unregister_rail=AsyncMock(), register_rail=AsyncMock())
    await obj._handle_memory_rail_by_config("team.work.normal")
    first = obj._memory_rail
    assert first is None
    obj._instance.register_rail.assert_not_awaited()
    obj._instance.unregister_rail.assert_not_awaited()

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
async def test_code_mode_keeps_project_instructions_without_legacy_memory(monkeypatch, tmp_path):
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
        ("", True, {"ProjectMemoryRail"}),
        ("jiuwen", True, {"ProjectMemoryRail"}),
        ("", True, {"ProjectMemoryRail"}),
        ("jiuwen", True, {"ProjectMemoryRail"}),
        ("jiuwen", False, set()),
    ]:
        cfg["memory"]["builtin"]["provider"] = provider
        cfg["modes"]["code"]["memory"]["enabled"] = enabled
        await obj._update_rails_for_mode("agent.code.normal")
        assert {type(rail).__name__ for rail in registered} == expected
        assert obj._memory_rail is None and obj._external_memory_rail is None


@pytest.mark.parametrize("markdown", [[], {"default": []}, {"default": {"params": []}}])
def test_sdk_rejects_invalid_mapping_instead_of_discarding_config(markdown):
    with pytest.raises(ValueError, match="must be a mapping"):
        builder._sdk_inline_config({"sdk": {"markdown_store": markdown}}, shared_document_root=True)


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


@pytest.mark.parametrize("mode", ["sdk", "http"])
def test_registered_memory_scene_uses_shared_builder(monkeypatch, mode):
    class ReviewRail(_MemoryRailStub):
        pass

    def review_spec(settings, project):
        return builder.MemoryRailSpec(
            ReviewRail,
            provider_options={"scene": "review", "project": project},
            rail_options={"review_policy": settings["review_policy"]},
        )

    monkeypatch.setitem(builder.SCENE_FACTORIES, "review", review_spec)
    cfg = config()
    cfg["memory"]["builtin"]["jiuwen"].update(mode=mode, review_policy="strict")
    first = builder.build_jiuwen_memory_rail(cfg, agent_name="reviewer", scene="review", project="a")
    second = builder.build_jiuwen_memory_rail(cfg, agent_name="reviewer", scene="review", project="b")
    assert isinstance(first, ReviewRail) and isinstance(second, ReviewRail)
    assert first is not second and first._provider is not second._provider
    assert first._provider.options["scene"] == "review"
    assert first._provider.options["project"] == "a"
    assert second._provider.options["project"] == "b"
    assert first._identity["agent_id"] == "shared"
    assert first._identity["review_policy"] == "strict"
    assert "session_end_extract" not in first._identity


def test_work_assembly_does_not_import_coding_rail(monkeypatch):
    import openjiuwen.harness.rails as rails

    monkeypatch.delattr(rails, "JiuwenMemoryCodingRail")
    assert builder.build_jiuwen_memory_rail(config()) is not None
    assert builder.build_jiuwen_memory_rail(config(), scene="code") is None


def test_unknown_memory_scene_is_disabled():
    assert builder.build_jiuwen_memory_rail(config(), scene="unknown") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("attach_only", [False, True])
async def test_team_stream_only_reconciles_memory_for_user_input(monkeypatch, attach_only):
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers

    cfg = config()
    monkeypatch.setattr(interface_deep, "get_config", lambda: cfg)
    obj = interface_deep.JiuWenSwarmDeepAdapter()
    obj._is_session_scoped_adapter = True
    obj._instance = SimpleNamespace(unregister_rail=AsyncMock())
    previous = object()
    obj._memory_rail = previous
    monkeypatch.setattr(obj, "_drain_jiuwen_memory_rail", AsyncMock())
    monkeypatch.setattr(obj, "_ensure_enterprise_models", AsyncMock())
    monkeypatch.setattr(obj, "_model_config_error", lambda _request: None)
    monkeypatch.setattr(obj, "_resolve_model_for_request", lambda _request: None)
    monkeypatch.setattr(obj, "_apply_model_to_react_agent", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(obj, "_write_runtime_state", lambda **_kwargs: None)

    async def team_stream(request, _inputs, _instance):
        yield AgentResponseChunk(
            request_id=request.request_id, channel_id=request.channel_id,
            payload={"event_type": "chat.delta", "content": "team event"},
        )

    monkeypatch.setattr(team_helpers, "process_team_message_stream", team_stream)
    request = AgentRequest(
        request_id="observer" if attach_only else "user-turn", channel_id="web",
        session_id="memory-race", is_stream=True,
        params={"mode": "team.work.normal", "query": "" if attach_only else "count",
                "attach_only": attach_only},
    )
    chunks = [chunk async for chunk in obj.process_message_stream_impl(
        request, {"query": request.params["query"]},
    )]

    assert [chunk.payload for chunk in chunks] == [
        {"event_type": "chat.delta", "content": "team event"},
    ], "Both input and passive subscriptions must reach the Team stream"
    if attach_only:
        obj._drain_jiuwen_memory_rail.assert_not_awaited()
        obj._instance.unregister_rail.assert_not_awaited()
        assert obj._memory_rail is previous
    else:
        obj._drain_jiuwen_memory_rail.assert_awaited_once_with(previous)
        obj._instance.unregister_rail.assert_awaited_once_with(previous)
        assert obj._memory_rail is None


@pytest.mark.asyncio
async def test_concurrent_team_memory_reconciliation_unregisters_once(monkeypatch):
    monkeypatch.setattr(interface_deep, "get_config", config)
    obj = interface_deep.JiuWenSwarmDeepAdapter()
    previous = SimpleNamespace(get_callbacks=lambda: {})
    obj._memory_rail = previous
    obj._instance = SimpleNamespace(
        unregister_rail=AsyncMock(side_effect=lambda candidate: candidate.get_callbacks()),
    )
    draining = asyncio.Event()
    release = asyncio.Event()
    contender_started = asyncio.Event()

    async def drain(_rail):
        draining.set()
        await release.wait()

    async def contend():
        contender_started.set()
        await obj._handle_memory_rail_by_config("team")

    monkeypatch.setattr(obj, "_drain_jiuwen_memory_rail", AsyncMock(side_effect=drain))
    tasks = [asyncio.create_task(obj._handle_memory_rail_by_config("team.work.normal"))]
    try:
        await asyncio.wait_for(draining.wait(), timeout=2)
        tasks.append(asyncio.create_task(contend()))
        await asyncio.wait_for(contender_started.wait(), timeout=2)
        drain_count_while_waiting = obj._drain_jiuwen_memory_rail.await_count
    finally:
        release.set()
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=2,
        )

    assert results == [None, None], "Concurrent reconciliation must not unregister None"
    assert drain_count_while_waiting == 1, "The second request must wait for the first cleanup"
    obj._drain_jiuwen_memory_rail.assert_awaited_once_with(previous)
    obj._instance.unregister_rail.assert_awaited_once_with(previous)
    assert obj._memory_rail is None
