"""Exercise the Swarm boundary through real Core runtime and Discovery sessions."""

import asyncio
import json
import re
from copy import deepcopy
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.tools.skill_retrieval_toolkits import (
    SkillRetrievalToolkit, build_model_discovery_settings,
    is_valid_skill_retrieval_session_profile, skill_sources_from_manager,
)
from jiuwenswarm.common.symphony_config import load_symphony_config
from jiuwenswarm.runtime.symphony.skill_retrieval import SkillTaxonomyRuntime


def config_for(tmp_path, *, context=200):
    return {"symphony": {
        "capability": {"sources": [{"type": "skill", "root": str(tmp_path / "skills")}]},
        "storage": {"artifact_root": str(tmp_path / "runtime")},
        "retrieval": {"enabled": True, "prompt": {"context_window_tokens": context},
                      "index_build": {"output": {"show_tree": False, "cache_metrics_enabled": False},
                                "request": {"max_retries": 0}}},
    }}


def write_skill(root, identifier, name=None):
    path = root / identifier / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name or identifier}\ndescription: Process and organize documents\n---\n# Instructions\nWrite reports.\n")
    return path


class Model:
    def __init__(self):
        self.calls = []
        self.loops = []
        self.closed = False
        self.model_config = SimpleNamespace(model_name="session-model")
        self._agentos_ctx_window = 4000

    async def invoke(self, **kwargs):
        self.calls.append(kwargs)
        self.loops.append(asyncio.get_running_loop())
        names = re.findall(r"^### (.+)$", kwargs["messages"][-1]["content"], re.MULTILINE)
        return SimpleNamespace(content=json.dumps({"skills/documents": names}), finish_reason="stop")

    async def aclose(self):
        self.closed = True


async def finished(runtime):
    for _ in range(200):
        state = await asyncio.to_thread(runtime.status)
        if state["status"] not in {"idle", "running", "queued"}:
            return state
        await asyncio.sleep(0.01)
    raise AssertionError("taxonomy task did not finish")


def test_actual_model_supplies_only_missing_context_defaults(tmp_path):
    config = config_for(tmp_path)
    config["symphony"]["retrieval"]["prompt"] = {"candidate_budget_ratio": 0.02}
    settings = build_model_discovery_settings(config, model=Model())
    assert settings.prompt.context_window_tokens == 4000
    assert settings.prompt.candidate_budget_tokens == 80
    config["symphony"]["retrieval"]["prompt"]["context_window_tokens"] = 10000
    assert build_model_discovery_settings(config, model=Model()).prompt.candidate_budget_tokens == 200


def test_status_and_constructor_are_lazy_and_start_requires_host_loop(tmp_path):
    (tmp_path / "skills").mkdir()
    runtime = SkillTaxonomyRuntime(config=load_symphony_config(config_for(tmp_path)))
    assert runtime.status()["status"] == "idle"
    assert not (tmp_path / "runtime").exists()
    with pytest.raises(RuntimeError, match="running event loop"):
        runtime.start_build()
    assert not (tmp_path / "runtime").exists()


@pytest.mark.asyncio
async def test_status_during_initial_binding_returns_queued_job(tmp_path, monkeypatch):
    from openjiuwen.symphony.shared.fingerprint import _io as artifact_io

    config = load_symphony_config(config_for(tmp_path))
    write_skill(tmp_path / "skills", "alpha")
    runtime = SkillTaxonomyRuntime(config=config)
    marker = tmp_path / "runtime" / "runtime.json"
    binding_started = Event()
    finish_binding = Event()
    replace = artifact_io.os.replace

    def publish_marker(source, target):
        if Path(target) == marker:
            binding_started.set()
            assert finish_binding.wait(5), "Status polling did not release marker publication"
        return replace(source, target)

    def read_during_binding():
        assert binding_started.wait(5), "Runtime did not start collection binding"
        try:
            return runtime.status()
        finally:
            finish_binding.set()

    monkeypatch.setattr(artifact_io.os, "replace", publish_marker)
    poll = asyncio.create_task(asyncio.to_thread(read_during_binding))
    started = runtime.start_build()
    try:
        state = await asyncio.wait_for(poll, 10)
        assert state["status"] == "running"
        assert state["stage"] == "queued"
        assert state["build_id"] == started["data"]["build_id"]
        assert (await finished(runtime))["status"] == "success"
        assert marker.is_file()
        assert runtime.status()["fresh"]
    finally:
        finish_binding.set()
        await runtime.cancel()


def test_status_rejects_unbound_populated_artifact_root(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "legacy.json").write_text("{}")
    runtime = SkillTaxonomyRuntime(config=load_symphony_config(config_for(tmp_path)))

    with pytest.raises(ValueError, match="Unsupported Symphony artifact layout"):
        runtime.status()


@pytest.mark.asyncio
async def test_global_build_excludes_session_overlay_and_session_revision_stays_frozen(tmp_path, monkeypatch):
    from jiuwenswarm.runtime.symphony import service

    config = config_for(tmp_path)
    config["symphony"]["retrieval"]["prompt"] = {}
    root = tmp_path / "skills"
    overlay = tmp_path / "session-skills"
    write_skill(root, "alpha", "Alpha display name")
    (root / "skills_state.json").write_text(json.dumps({
        "local_skills": [{"name": "alpha", "source": "installed-local"}],
    }))
    write_skill(overlay, "session-only", "Session only")
    model = Model()
    model.model_config.model_name = "session-selected"
    model._agentos_ctx_window = 200
    host_model = Model()
    host_model.model_config.model_name = "host-default"
    host_model._agentos_ctx_window = 8000
    monkeypatch.setattr(service, "get_swarm_symphony_service", lambda: SimpleNamespace(
        runtime=lambda: SimpleNamespace(model=host_model),
    ))
    disabled = set()
    toolkit = SkillRetrievalToolkit(
        skill_directories=lambda: [str(root), str(overlay)], disabled_skills=lambda: disabled,
        visible_skill_names=lambda: {"alpha", "session-only"}, source_by_name={"alpha": "installed-local"},
        config_base=config, model=model, auto_build_index=True, session_scope="old-session",
    )
    runtime = SkillTaxonomyRuntime(config=load_symphony_config(config), source_by_id={"alpha": "installed-local"})
    new_toolkit = None
    try:
        assert toolkit.auto_build_result["success"]
        assert toolkit.settings.prompt.context_window_tokens == 200
        assert toolkit.candidate_budget_tokens == 2
        assert toolkit.session.environment is toolkit.environment
        original_snapshot = toolkit.frozen_prompt_snapshot
        assert toolkit.session_profile()["pinned_index_revision"] == ""
        state = await finished(runtime)
        assert state["status"] == "success", state
        assert state["fresh"]
        assert len(host_model.calls) == 1 and host_model.loops == [asyncio.get_running_loop()]
        assert model.calls == []
        tree = json.loads((tmp_path / "runtime" / "tree" / "index" / "manifest.json").read_text())
        assert tree["worker_ids"] == ["alpha"]
        assert "Session only" not in host_model.calls[0]["messages"][-1]["content"]
        assert toolkit.frozen_prompt_snapshot == original_snapshot
        assert toolkit.session_profile()["pinned_index_revision"] == ""
        new_toolkit = SkillRetrievalToolkit(skill_directories=[str(root), str(overlay)],
                                             config_base=config, model=model, session_scope="new-session")
        assert new_toolkit.session_profile()["pinned_index_revision"]
        assert {record.worker_id for record in toolkit.current_records()} == {"alpha", "session-only"}
        assert toolkit.current_records()[0].source == "installed-local"
        disabled.add("session-only")
        assert set(toolkit.environment.selection_cards()) == {"alpha"}
        result = await toolkit.skill_index(operation="search", query="Session only")
        assert "session-only" not in str(result)
        profile = toolkit.session_profile()
        assert is_valid_skill_retrieval_session_profile(profile)
    finally:
        await toolkit.aclose()
        if new_toolkit is not None:
            await new_toolkit.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await toolkit.skill_index(operation="list")
    assert not model.closed and not host_model.closed


@pytest.mark.asyncio
async def test_restored_profile_keeps_budget_and_generation_after_config_changes(tmp_path):
    config = config_for(tmp_path, context=10000)
    root = tmp_path / "skills"
    write_skill(root, "alpha")
    first = SkillRetrievalToolkit(skill_directories=[str(root)], config_base=config)
    profile = first.session_profile()
    changed = deepcopy(config)
    changed["symphony"]["retrieval"]["prompt"]["context_window_tokens"] = 500
    restored = SkillRetrievalToolkit(skill_directories=[str(root)], config_base=changed, frozen_profile=profile)
    try:
        assert restored.frozen_prompt_snapshot == first.frozen_prompt_snapshot
        assert restored.candidate_budget_tokens == 100
        assert restored.settings.prompt.context_window_tokens == 10000
    finally:
        await first.aclose()
        await restored.aclose()


@pytest.mark.asyncio
async def test_cancel_after_disable_uses_running_owner_and_preserves_previous_tree(tmp_path):
    config = config_for(tmp_path)
    write_skill(tmp_path / "skills", "alpha")
    baseline = SkillTaxonomyRuntime(config=load_symphony_config(config))
    baseline.start_build()
    assert (await finished(baseline))["status"] == "success"
    path = tmp_path / "runtime" / "tree" / "index" / "tree_index.yaml"
    previous = path.read_bytes()
    entered, cleaned = asyncio.Event(), asyncio.Event()

    class SlowModel(Model):
        async def invoke(self, **kwargs):
            entered.set()
            try:
                await asyncio.sleep(60)
            finally:
                await asyncio.sleep(0.02)
                cleaned.set()

    model = SlowModel()
    runtime = SkillTaxonomyRuntime(config=load_symphony_config(config), model=model)
    runtime.start_build(force=True)
    await asyncio.wait_for(entered.wait(), 2)
    disabled = deepcopy(config)
    disabled["symphony"]["retrieval"]["enabled"] = False
    other = SkillTaxonomyRuntime(config=load_symphony_config(disabled))
    result = await other.cancel()
    assert result["success"] and result["data"]["state"] == "cancelled"
    assert cleaned.is_set()
    assert path.read_bytes() == previous
    assert not model.closed


def test_installation_sources_are_keyed_by_capability_id():
    manager = SimpleNamespace(get_local_skills=lambda: [
        {"name": "Alpha display name", "dir_name": "alpha", "source": "local"},
    ], get_installed_plugins=lambda: [])
    assert skill_sources_from_manager(manager) == {"alpha": "local"}


@pytest.mark.asyncio
async def test_status_with_model_checks_identity_but_read_only_status_can_omit_it(tmp_path):
    config = load_symphony_config(config_for(tmp_path))
    write_skill(tmp_path / "skills", "alpha")
    first = SkillTaxonomyRuntime(config=config, model=Model())
    first.start_build()
    assert (await finished(first))["fresh"]
    changed_model = Model()
    changed_model.model_config.model_name = "different-model"
    changed = SkillTaxonomyRuntime(config=config, model=changed_model)
    assert not changed.status()["fresh"]
    assert changed.status()["model_identity_checked"]
    reader = SkillTaxonomyRuntime(config=config)
    assert reader.status()["fresh"]
    assert not reader.status()["model_identity_checked"]
    await first.cancel()


@pytest.mark.asyncio
async def test_immediate_queued_cancellation_never_starts_the_model(tmp_path):
    config = load_symphony_config(config_for(tmp_path))
    write_skill(tmp_path / "skills", "alpha")
    model = Model()
    runtime = SkillTaxonomyRuntime(config=config, model=model)
    started = runtime.start_build()
    assert started["data"]["build_id"]
    assert runtime.status()["build_id"] == started["data"]["build_id"]
    result = await runtime.cancel(build_id=started["data"]["build_id"])
    assert result["data"]["build_id"] == started["data"]["build_id"]
    assert result["success"]
    assert runtime.status()["status"] == "cancelled"
    assert model.calls == []
    assert not (tmp_path / "runtime" / "tree" / "index").exists()
    assert not model.closed


@pytest.mark.asyncio
async def test_manager_build_status_and_search_use_configured_global_collection(tmp_path, monkeypatch):
    from jiuwenswarm.common import config as common_config
    from jiuwenswarm.agents.harness.common.tools import skill_retrieval_toolkits as toolkits
    from jiuwenswarm.runtime.symphony import service
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

    config = config_for(tmp_path)
    config["symphony"]["orchestration"] = {"enabled": False}
    write_skill(tmp_path / "skills", "alpha", "Configured alpha")
    manager = SkillManager(workspace_dir=str(tmp_path))
    model = Model()
    monkeypatch.setattr(common_config, "get_config", lambda: config)
    monkeypatch.setattr(toolkits, "get_config", lambda: config)
    monkeypatch.setattr(service, "get_swarm_symphony_service", lambda: SimpleNamespace(
        runtime=lambda: SimpleNamespace(model=model),
    ))
    status = await manager.handle_skills_retrieval_status({})
    assert status["enabled"] and status["build_supported"]
    assert status["installed_count"] == 1 and status["installed_enabled_count"] == 1
    started = await manager.handle_skills_retrieval_index_build({})
    assert started["success"] and started["background"] and started["build_id"]
    queued = await manager.handle_skills_retrieval_status({"build_id": started["build_id"]})
    assert queued["build_id"] == started["build_id"]
    assert (await finished(manager._skill_taxonomy_runtime()))["status"] == "success"
    status = await manager.handle_skills_retrieval_status({})
    assert status["fresh"] and status["indexed_count"] == 1
    assert status["build_id"] == started["build_id"]
    assert status["core_build_id"] == status["version"]
    assert status["core_build_id"] and status["core_build_id"] != status["build_id"]
    assert model.loops == [asyncio.get_running_loop()]
    result = await manager.handle_skills_retrieval_search({"query": "Configured alpha"})
    assert result["success"] and "alpha" in result["result"]
    manager.set_skill_enabled("alpha", False)
    result = await manager.handle_skills_retrieval_search({"query": "Configured alpha"})
    assert result["success"] and "Configured alpha" not in result["result"]
    config["symphony"]["retrieval"]["enabled"] = False
    assert (await manager.handle_skills_retrieval_index_cancel({}))["success"]
    disabled_search = await manager.handle_skills_retrieval_search({"query": "alpha"})
    assert not disabled_search["success"]
    assert disabled_search["error_code"] == "skill_retrieval_disabled"
    assert not model.closed


@pytest.mark.asyncio
async def test_adapter_reload_cancels_old_artifact_root_and_waits_for_model_cleanup(tmp_path):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    config = config_for(tmp_path)
    write_skill(tmp_path / "skills", "alpha")
    entered, cleaned = asyncio.Event(), asyncio.Event()

    class WaitingModel(Model):
        async def invoke(self, **kwargs):
            entered.set()
            try:
                await asyncio.sleep(60)
            finally:
                await asyncio.sleep(0.01)
                cleaned.set()

    model = WaitingModel()
    runtime = SkillTaxonomyRuntime(config=load_symphony_config(config), model=model)
    runtime.start_build()
    await asyncio.wait_for(entered.wait(), 2)
    previous = JiuWenSwarmDeepAdapter._skill_retrieval_build_profile(config)
    changed = deepcopy(config)
    changed["symphony"]["storage"]["artifact_root"] = str(tmp_path / "changed-runtime")
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    await adapter._cancel_skill_retrieval_build_after_reload(previous, changed)
    assert cleaned.is_set()
    assert runtime.status()["status"] == "cancelled"
    assert not (tmp_path / "runtime" / "tree" / "index").exists()
    assert not model.closed


@pytest.mark.asyncio
async def test_adapter_disabled_tools_are_closed_and_joined_during_cleanup(tmp_path):
    from unittest.mock import AsyncMock
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    config = config_for(tmp_path)
    root = tmp_path / "skills"
    write_skill(root, "alpha")
    toolkit = SkillRetrievalToolkit(skill_directories=[str(root)], config_base=config)
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._skill_retrieval_toolkit = toolkit
    adapter._skill_retrieval_environment = toolkit.environment
    adapter._skill_retrieval_tools = toolkit.get_tools()
    adapter._skill_retrieval_tools_registered = True
    adapter._skill_retrieval_tools_enabled_for_runtime = lambda *args: False
    adapter._sync_tool_group = lambda **kwargs: ([], False)
    adapter._drop_tool_names_from_runtime = lambda *args: None
    adapter._cleanup_adapter_resources = AsyncMock()
    adapter.close_moa_runtime = AsyncMock()
    adapter._sync_skill_retrieval_tools_for_runtime(config)
    assert adapter._skill_retrieval_toolkit is None
    with pytest.raises(RuntimeError, match="closed"):
        await toolkit.skill_index(operation="list")
    await adapter.cleanup()
    assert toolkit._close_task.done()
    assert adapter._retired_skill_retrieval_toolkits == []


@pytest.mark.asyncio
async def test_adapter_persisted_profile_roundtrip_keeps_v2_discovery_schema(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    config = config_for(tmp_path, context=10000)
    root = tmp_path / "skills"
    write_skill(root, "alpha")
    toolkit = SkillRetrievalToolkit(skill_directories=[str(root)], config_base=config, session_scope="persisted")
    profile_path = tmp_path / "profile.json"
    monkeypatch.setattr(JiuWenSwarmDeepAdapter, "_skill_retrieval_profile_path",
                        staticmethod(lambda session_id: profile_path))
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._skill_retrieval_toolkit = toolkit
    adapter._is_session_scoped_adapter = True
    adapter._skill_retrieval_session_enabled = True
    adapter._parent_session_id = "persisted"
    adapter._initial_skill_retrieval_model_name = "actual-model"
    adapter._skill_retrieval_context_model_name = "actual-model"
    adapter._skill_retrieval_context_window_tokens = 10000
    try:
        adapter._persist_skill_retrieval_session_profile()
        restored = JiuWenSwarmDeepAdapter._load_skill_retrieval_session_profile("persisted")
        assert restored["enabled"] and restored["schema_version"] == 2
        assert set(restored["settings"]) == {"prompt", "output", "use_existing_index"}
        assert restored["settings"]["prompt"]["context_window_tokens"] == 10000
        settings = restored["settings"]
        for invalid_settings in (
            {**settings, "enabled": True},
            {**settings, "index_build": {}},
            {key: value for key, value in settings.items() if key != "output"},
            {**settings, "prompt": {"context_window_tokens": 10000}},
            {**settings, "use_existing_index": "true"},
        ):
            assert not is_valid_skill_retrieval_session_profile({**restored, "settings": invalid_settings})
        changed = json.loads(profile_path.read_text())
        changed["schema_version"] = 1
        profile_path.write_text(json.dumps(changed))
        rejected = JiuWenSwarmDeepAdapter._load_skill_retrieval_session_profile("persisted")
        assert not rejected["enabled"]
        assert rejected["profile_recovery_error"] == "invalid_profile_schema"
    finally:
        await toolkit.aclose()


@pytest.mark.asyncio
async def test_same_host_wrapper_can_build_again_after_completed_runtime_is_closed(tmp_path):
    config = load_symphony_config(config_for(tmp_path))
    write_skill(tmp_path / "skills", "alpha")
    model = Model()
    runtime = SkillTaxonomyRuntime(config=config, model=model)
    runtime.start_build()
    assert (await finished(runtime))["status"] == "success"
    await runtime.cancel()
    runtime.start_build(force=True)
    assert (await finished(runtime))["status"] == "success"
    assert len(model.calls) == 2
    assert model.loops == [asyncio.get_running_loop()] * 2
    await runtime.cancel()
    assert not model.closed


@pytest.mark.asyncio
async def test_manual_fallback_build_works_when_graph_flow_and_index_consumption_are_disabled(tmp_path, monkeypatch):
    from jiuwenswarm.common import config as common_config
    from jiuwenswarm.runtime.symphony import service
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

    config = config_for(tmp_path)
    config["symphony"]["orchestration"] = {"enabled": False}
    config["symphony"]["retrieval"]["use_existing_index"] = False
    write_skill(tmp_path / "skills", "alpha")
    manager = SkillManager(workspace_dir=str(tmp_path))
    monkeypatch.setattr(common_config, "get_config", lambda: config)

    def no_model():
        raise ValueError("No model is configured")

    monkeypatch.setattr(service, "get_swarm_symphony_service", lambda: SimpleNamespace(runtime=no_model))
    status = await manager.handle_skills_retrieval_status({})
    assert status["enabled"] and not status["index_enabled"]
    assert status["build_supported"]
    assert (await manager.handle_skills_retrieval_index_build({}))["success"]
    runtime = manager._skill_taxonomy_runtime()
    assert (await finished(runtime))["status"] == "success"
    assert (tmp_path / "runtime" / "tree" / "index" / "tree_index.yaml").is_file()
    await runtime.cancel()


@pytest.mark.asyncio
async def test_deep_adapter_discovery_and_execution_share_workspace_skills_and_selected_mcp(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    config = config_for(tmp_path, context=10000)
    catalog, workspace, overlay, hidden = (
        tmp_path / name for name in ("skills", "workspace-skills", "selected-mcp", "hidden-mcp")
    )
    for directory, identifier in (
        (catalog, "catalog-only"), (workspace, "installed"), (overlay, "selected"), (hidden, "hidden"),
    ):
        write_skill(directory, identifier)
    monkeypatch.setattr(interface_deep, "get_agent_skills_dir", lambda: workspace)
    adapter = object.__new__(interface_deep.JiuWenSwarmDeepAdapter)
    adapter._instance_overrides = {}
    adapter._config_base_cache = config
    adapter._is_session_scoped_adapter = True
    adapter._session_selected_mcp = {"chosen"}
    adapter._pending_skill_scan_mcp_names = set()
    adapter._skill_retrieval_toolkit = None
    adapter._skill_manager = SimpleNamespace(
        _mcp_skills_dirs=lambda: [{"name": "chosen", "dir": str(overlay)}, {"name": "hidden", "dir": str(hidden)}],
        list_execution_disabled_skills=lambda: [],
    )
    adapter._skill_retrieval_session_scope = lambda: "workspace-root"
    adapter._persist_skill_retrieval_session_profile = lambda: None
    adapter._skill_retrieval_tools_enabled_for_runtime = lambda *args: True
    toolkit = adapter._get_or_create_skill_retrieval_toolkit()
    try:
        assert adapter._skill_scan_dirs() == [str(workspace), str(overlay)]
        assert {item.worker_id for item in toolkit.current_records()} == {"installed", "selected"}
        rail = adapter._build_skill_rail({})
        assert rail.skills_dir == [str(workspace), str(overlay)]
    finally:
        await toolkit.aclose()


@pytest.mark.asyncio
async def test_team_discovery_execution_and_seed_recovery_share_workspace_skills(tmp_path, monkeypatch):
    from openjiuwen.agent_teams.rails.elements import TEAM_SKILL_USE
    from openjiuwen.agent_teams.skill.rail_spec import complete_declared_team_skill_rails
    from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec
    from jiuwenswarm.agents.swarm.context import SwarmBuildContext
    from jiuwenswarm.agents.swarm.config_specs import build_member_deep_agent_spec
    from jiuwenswarm.agents.swarm.providers.tools import build_skill_retrieval, skill_retrieval_toolkit_for_context
    from jiuwenswarm.agents.swarm.registry import register_swarm_providers

    config = config_for(tmp_path, context=10000)
    catalog, workspace = tmp_path / "skills", tmp_path / "workspace-skills"
    write_skill(catalog, "catalog-only")
    write_skill(workspace, "installed")
    state_file = workspace / "skills_state.json"
    monkeypatch.setattr("openjiuwen.agent_teams.skill.rail_spec.global_skills_dir", lambda: workspace)
    monkeypatch.setattr("jiuwenswarm.server.runtime.skill.skilldev.state_utils.get_state_file", lambda: state_file)
    ctx = SwarmBuildContext(config=config, global_skills_dir=str(workspace), team_id="team", member_name="member",
                            workspace=SimpleNamespace(root_path=str(tmp_path / "member")))
    assert ctx.global_skills_dir == str(workspace)
    seed = ctx.to_seed()
    restored = SwarmBuildContext.from_seed(seed, config=config, trajectory_span_processor=None)
    assert restored.global_skills_dir == str(workspace)
    toolkit = skill_retrieval_toolkit_for_context(ctx)
    try:
        assert {item.worker_id for item in toolkit.current_records()} == {"installed"}
        search, = build_skill_retrieval({}, ctx)
        assert search.card.name == "skill_index"
        result = await search.invoke({"operation": "list"})
        assert result.detailed_output["observed_skill_ids"] == ["installed"]
        register_swarm_providers()
        spec = build_member_deep_agent_spec(config, "team", "teammate", DeepAgentSpec())
        rails = complete_declared_team_skill_rails(spec.rails, team_name="team", member_name="member", config_skills=[],
                                                   member_workspace_path=str(tmp_path / "member"))
        declared = next(rail for rail in rails if rail.type == TEAM_SKILL_USE)
        execution = declared.build(language="cn", context=ctx)
        assert execution.skills_dir == [str(workspace)]
        async def read_file(path, **kwargs):
            return SimpleNamespace(code=0, data=SimpleNamespace(content=Path(path).read_text()))
        execution.set_sys_operation(SimpleNamespace(fs=lambda: SimpleNamespace(read_file=read_file)))
        await execution.reload_skills()
        assert [item.name for item in execution.get_skills_for_session()] == ["installed"]
        state_file.write_text(json.dumps({
            "local_skills": [{"name": "installed", "source": "workspace-library"}],
            "skill_configs": {"installed": {"enabled": False}},
        }))
        assert toolkit.current_records() == ()
        result = await search.invoke({"operation": "list"})
        assert result.detailed_output["observed_skill_ids"] == []
        await execution.reload_skills()
        assert execution.get_skills_for_session() == []
        state_file.write_text(json.dumps({
            "local_skills": [{"name": "installed", "source": "workspace-library"}],
        }))
        assert toolkit.current_records()[0].source == "workspace-library"
        await execution.reload_skills()
        assert [item.name for item in execution.get_skills_for_session()] == ["installed"]
    finally:
        await toolkit.aclose()


@pytest.mark.asyncio
async def test_manager_job_id_stays_stable_through_running_status_and_cancel(tmp_path, monkeypatch):
    from jiuwenswarm.common import config as common_config
    from jiuwenswarm.runtime.symphony import service
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

    config = config_for(tmp_path)
    write_skill(tmp_path / "skills", "alpha")
    entered, cleaned = asyncio.Event(), asyncio.Event()

    class WaitingModel(Model):
        async def invoke(self, **kwargs):
            entered.set()
            try:
                await asyncio.sleep(60)
            finally:
                await asyncio.sleep(0.01)
                cleaned.set()

    model = WaitingModel()
    monkeypatch.setattr(common_config, "get_config", lambda: config)
    monkeypatch.setattr(service, "get_swarm_symphony_service", lambda: SimpleNamespace(runtime=lambda: SimpleNamespace(model=model)))
    manager = SkillManager(workspace_dir=str(tmp_path))
    started = await manager.handle_skills_retrieval_index_build({})
    assert started["success"] and started["build_id"]
    await asyncio.wait_for(entered.wait(), 2)
    status = await manager.handle_skills_retrieval_status({"build_id": started["build_id"]})
    assert status["build_status"] == "running" and status["build_id"] == started["build_id"]
    assert status["core_build_id"] and status["core_build_id"] != status["build_id"]
    mismatch = await manager.handle_skills_retrieval_index_cancel({"build_id": "wrong-id"})
    assert not mismatch["success"] and not cleaned.is_set()
    cancelled = await manager.handle_skills_retrieval_index_cancel({"build_id": started["build_id"]})
    assert cancelled["success"] and cancelled["build_id"] == started["build_id"]
    assert cleaned.is_set()
    status = await manager.handle_skills_retrieval_status({"build_id": started["build_id"]})
    assert status["build_status"] == "cancelled" and status["build_id"] == started["build_id"]
    assert not model.closed


def test_explicit_library_state_is_read_only_and_default_install_semantics_are_preserved(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.skill.skilldev import state_utils
    from jiuwenswarm.agents.harness.common.tools.skill_retrieval_toolkits import skill_sources_from_directories

    default, configured, missing = (tmp_path / name for name in ("default", "configured", "missing"))
    default.mkdir()
    configured.mkdir()
    (default / "skills_state.json").write_text(json.dumps({
        "local_skills": [{"name": "default-skill"}],
        "skill_configs": {"default-skill": {"enabled": False}, "uninstalled": {"enabled": False}},
    }))
    (configured / "skills_state.json").write_text(json.dumps({
        "local_skills": [{"name": "Display", "dir_name": "configured-skill", "source": "custom"}],
        "skill_configs": {"configured-skill": {"enabled": False}, "unregistered-skill": {"enabled": False}},
    }))
    monkeypatch.setattr(state_utils, "get_state_file", lambda: default / "skills_state.json")
    monkeypatch.setattr(state_utils, "get_agent_skills_dir", lambda: default)
    assert state_utils.load_execution_disabled_skills() == ["default-skill"]
    assert state_utils.load_execution_disabled_skills(skills_dir=configured) == ["configured-skill", "unregistered-skill"]
    assert skill_sources_from_directories([configured]) == {"configured-skill": "custom"}
    assert state_utils.load_execution_disabled_skills(skills_dir=missing) == []
    assert skill_sources_from_directories([missing]) == {}
    assert not missing.exists()


@pytest.mark.asyncio
async def test_manager_reads_disabled_state_and_sources_from_configured_root_not_install_workspace(tmp_path, monkeypatch):
    from jiuwenswarm.common import config as common_config
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

    config = config_for(tmp_path)
    root = tmp_path / "skills"
    write_skill(root, "configured")
    write_skill(root, "blocked")
    (root / "skills_state.json").write_text(json.dumps({
        "local_skills": [{"name": "Configured display", "dir_name": "configured", "source": "custom-library"}],
        "skill_configs": {"blocked": {"enabled": False}},
    }))
    legacy_workspace = tmp_path / "legacy-workspace"
    write_skill(legacy_workspace / "skills", "legacy")
    write_skill(legacy_workspace / "skills", "legacy-second")
    manager = SkillManager(workspace_dir=str(legacy_workspace))
    monkeypatch.setattr(common_config, "get_config", lambda: config)
    status = await manager.handle_skills_retrieval_status({})
    assert status["installed_count"] == 2 and status["installed_enabled_count"] == 1
    search = await manager.handle_skills_retrieval_search({"query": "blocked"})
    assert search["success"] and "blocked/SKILL.md" not in search["result"]
    runtime = manager._skill_taxonomy_runtime()
    assert runtime._sources() == {"configured": "custom-library"}
    assert runtime._disabled() == ["blocked"]
    config["symphony"]["retrieval"]["enabled"] = False
    disabled = await manager.handle_skills_retrieval_status({})
    assert not disabled["enabled"]
    assert disabled["installed_count"] == 1 and disabled["installed_enabled_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("overlay_disabled", [True, False])
async def test_auto_build_never_publishes_session_overlay_denials_or_provenance(tmp_path, monkeypatch, overlay_disabled):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep
    from jiuwenswarm.runtime.symphony import service

    config = config_for(tmp_path)
    root, overlay = tmp_path / "skills", tmp_path / "overlay"
    write_skill(root, "alpha")
    write_skill(root, "beta")
    monkeypatch.setattr(interface_deep, "get_agent_skills_dir", lambda: root)
    write_skill(overlay, "alpha" if overlay_disabled else "overlay-only", "Overlay alpha")
    (overlay / "skills_state.json").write_text(json.dumps({
        "local_skills": [{"name": "alpha", "source": "session-overlay"}],
        "skill_configs": {"alpha": {"enabled": not overlay_disabled}},
    }))
    model = Model()
    monkeypatch.setattr(service, "get_swarm_symphony_service", lambda: SimpleNamespace(runtime=lambda: SimpleNamespace(model=model)))
    adapter = object.__new__(interface_deep.JiuWenSwarmDeepAdapter)
    adapter._instance_overrides = {}
    adapter._config_base_cache = config
    adapter._is_session_scoped_adapter = True
    adapter._session_selected_mcp = {"overlay"}
    adapter._pending_skill_scan_mcp_names = set()
    adapter._skill_retrieval_toolkit = None
    adapter._skill_manager = SimpleNamespace(_mcp_skills_dirs=lambda: [{"name": "overlay", "dir": str(overlay)}])
    adapter._skill_retrieval_session_scope = lambda: "overlap"
    adapter._persist_skill_retrieval_session_profile = lambda: None
    toolkit = adapter._get_or_create_skill_retrieval_toolkit()
    runtime = SkillTaxonomyRuntime(config=load_symphony_config(config))
    try:
        expected = {"beta"} if overlay_disabled else {"alpha", "beta", "overlay-only"}
        assert {item.worker_id for item in toolkit.current_records()} == expected
        assert toolkit.auto_build_result["success"]
        assert (await finished(runtime))["status"] == "success"
        manifest = json.loads((tmp_path / "runtime" / "tree" / "index" / "manifest.json").read_text())
        assert manifest["worker_ids"] == ["alpha", "beta"]
        assert runtime.status()["fresh"]
        assert len(model.calls) == 1
    finally:
        await toolkit.aclose()
        await runtime.cancel()
