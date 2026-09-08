"""Exercise real Plugin loading across RSI materialization and publication."""

import json
import shutil
from pathlib import Path

import pytest
import yaml

from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness import DeepAgent, DeepAgentConfig
from openjiuwen.harness.rails import SkillUseRail
from openjiuwen.rsi.harness_rsi.member_optimizer.action_executor import MemberActionExecutorAgent
from openjiuwen.rsi.harness_rsi.member_optimizer.schema import MemberOptimizationAction
from openjiuwen.rsi.harness_rsi.member_optimizer.verification import _load_harness_plugin
from openjiuwen.rsi.harness_rsi.member_optimizer.worktree_coordinator import MemberWorktreeCoordinator
from jiuwenswarm.agents.harness.common.rsi import build_rsi_service_context
from jiuwenswarm.agents.harness.common.rsi.materializer import RsiTaskMaterializer
from jiuwenswarm.agents.harness.common.rsi.models import RsiTask, utcnow_iso
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime import extension_package_manager as catalog

_PRESETS = Path(__file__).resolve().parents[3] / "jiuwenswarm/resources/agent/workspace/plugins/plugin_packages"


def _agent():
    return DeepAgent(AgentCard(name="plugin-roundtrip")).configure(
        DeepAgentConfig(enable_task_loop=False, rails=[SkillUseRail(skills_dir=[], include_tools=False)])
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["coding-guard", "content-creation", "office-document-toolkit"])
async def test_preset_plugin_private_copy_loads_real_resources(tmp_path, name):
    material = RsiTaskMaterializer(tmp_path / "tasks").materialize_harness_refs("probe", _PRESETS / name)
    agent = _agent()
    record = await agent.load_plugin(material["package_path"])
    assert record.refs
    assert record.source_uri.startswith(material["package_path"])
    await agent.unload_extension(record)


@pytest.mark.asyncio
async def test_optimized_plugin_can_be_installed_and_restored_in_fresh_agent(tmp_path, monkeypatch):
    tasks_root = tmp_path / "rsi" / "tasks"
    monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path)
    material = RsiTaskMaterializer(tasks_root).materialize_harness_refs("probe", _PRESETS / "coding-guard")
    run = tasks_root / "probe" / "run"
    work = MemberWorktreeCoordinator.prepare_integration_worktree("solver", material["package_path"], str(run / "wt"))
    action = MemberOptimizationAction(
        action_id="add-lesson", role="solver", action_group="prompt", operation="add",
        action_type="prompt_improvement",
        target_path="prompt_sections/files/verification.md", description="Require evidence before completion",
        declared_write_paths=["prompt_sections/files/verification.md", "prompt_sections/sections.yaml"],
    )
    (work / "prompt_sections/files").mkdir(parents=True)
    (work / action.target_path).write_text("Verify the result before reporting completion.", encoding="utf-8")
    MemberActionExecutorAgent._sync_action_registries(
        action_worktree=work, action=action, declared_paths=action.declared_write_paths,
        written_files=[action.target_path],
    )
    spec = _load_harness_plugin(work)
    assert spec.prompt_sections[0].content["en"] == "Verify the result before reporting completion."
    published = run / "published" / "coding-guard"
    shutil.copytree(work, published)
    refs = run / "published" / "harness_refs.yaml"
    refs.write_text(yaml.safe_dump({"harness_refs": {"solver": "coding-guard"}}), encoding="utf-8")
    context = build_rsi_service_context(tasks_root, enable_harness_materialization=False)
    context.store.create(RsiTask(
        task_id="probe", name="Roundtrip", scenario="HARNESS", status="COMPLETED",
        created_at=utcnow_iso(), model_refs={"optimizer": "unused", "tester": "unused"},
        config={}, run_dir=str(run),
    ))
    (run / "single_harness_state.yaml").write_text(yaml.safe_dump({
        "publication_status": "published", "published_harness_refs_path": str(refs),
    }), encoding="utf-8")
    result = await context.harness_installer.install("probe")
    assert result["status"] == "ACTIVE"
    catalog_id = result["installation_id"]
    assert catalog.is_plugin_allowed(catalog_id)
    assert catalog.show_plugin_package(catalog_id)["installed"] is True
    # Select the exported version through the unmodified ordinary chat loader.
    selected = object.__new__(JiuWenSwarmDeepAdapter)
    selected._instance = _agent()
    selected._loaded_plugins = {}
    await selected._load_plugins_for_request({"plugin_names": [catalog_id]})
    selected_record = selected._loaded_plugins[catalog_id][0]
    assert any(ref.identity == "verification" for ref in selected_record.refs)
    assert any(ref.kind.value == "tool" for ref in selected_record.refs)
    assert any(ref.kind.value == "rail" for ref in selected_record.refs)
    # Installing/selecting the same version must not mount its resources twice.
    await selected._apply_rsi_harness_install_local(
        "activate", config_path=str(published), installation_id=catalog_id,
    )
    assert catalog_id not in selected._loaded_plugins
    await selected._load_plugins_for_request({"plugin_names": [catalog_id]})
    assert catalog_id not in selected._loaded_plugins
    await selected._apply_rsi_harness_install_local("deactivate", config_path=str(published), installation_id=catalog_id)
    await selected._instance.unload_extension(selected._loaded_plugins[catalog_id][0])
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = _agent()
    loaded = await adapter._load_rsi_active_harness()
    assert loaded["status"] == "ACTIVE"
    assert any(ref.identity == "verification" for ref in loaded["resources"])
    assert any(ref.kind.value == "tool" for ref in loaded["resources"])
    assert any(ref.kind.value == "rail" for ref in loaded["resources"])
    await adapter._instance.unload_extension(adapter._rsi_harness_load_record)
    assert not (Path(material["package_path"]) / action.target_path).exists()

    # An ordinary chat may already have the original plugin selected. Applying
    # its optimized version must replace that load, not collide with its tools.
    live = object.__new__(JiuWenSwarmDeepAdapter)
    live._instance = _agent()
    original = await live._instance.load_plugin(material["package_path"])
    live._loaded_plugins = {"coding-guard": (original, "1.0.0")}
    activated = await live._apply_rsi_harness_install_local(
        "activate", config_path=str(published), installation_id="candidate",
    )
    assert activated["status"] == "ACTIVE"
    assert "coding-guard" not in live._loaded_plugins
    await live._load_plugins_for_request({"plugin_names": ["coding-guard"]})
    assert "coding-guard" not in live._loaded_plugins
    broken = run / "broken"
    shutil.copytree(published, broken)
    manifest_path = broken / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tools"][0]["class"] = "MissingToolClass"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(Exception, match="MissingToolClass"):
        await live._apply_rsi_harness_install_local(
            "activate", config_path=str(broken), installation_id="broken",
        )
    assert live._rsi_harness_install_id == "candidate"
    assert "coding-guard" not in live._loaded_plugins
    assert any(ref.identity == "verification" for ref in live._rsi_harness_load_record.refs)
    await live._apply_rsi_harness_install_local("deactivate", config_path=str(published), installation_id="candidate")
    assert "coding-guard" in live._loaded_plugins
    await live._instance.unload_extension(live._loaded_plugins["coding-guard"][0])
