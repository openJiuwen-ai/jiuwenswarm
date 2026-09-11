"""Verify provider selection, disabled legacy paths, and reversible configuration."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.core.sys_operation import LocalWorkConfig, SysOperation, SysOperationCard
from openjiuwen.harness.prompts import SystemPromptBuilder
from openjiuwen.harness.rails.skills.skill_use_rail import SkillUseRail
from openjiuwen.harness.schema.deep_agent_spec import WorkspaceSpec

from jiuwenswarm.common import config as config_module
from jiuwenswarm.agents.harness.common.memory import external_memory_config as policy
from jiuwenswarm.agents.harness.common.memory.config import is_auto_memory_enabled, is_memory_enabled
from jiuwenswarm.agents.harness.common.memory.celia.prompt import (
    CeliaMcpPromptRail, load_celia_agent_prompt, load_old_celia_agent_prompt,
)
from jiuwenswarm.agents.harness.common.memory.celia.rail import CeliaMemoryRail
from jiuwenswarm.agents.harness.common.memory.dreaming.sweeper import DreamingConfig
from jiuwenswarm.agents.harness.common.memory.external_memory_builder import build_external_memory_rail
from jiuwenswarm.agents.harness.common.rails.tool_usage_prompt_rail import OrderedContextAssembleRail
from jiuwenswarm.server.runtime.agent_adapter import interface_deep


@pytest.mark.parametrize("template", ["config.yaml", "config.team.distributed.leader.yaml", "config.team.distributed.teammate.yaml"])
def test_default_templates_mount_new_celia_without_legacy_extraction(template, monkeypatch, tmp_path):
    for key in ("MEMORY_MODE", "MEMORY_ENGINE", "MEMORY_EXTERNAL_PROVIDER", "DREAMING_AGENT_ENABLED", "DREAMING_CODE_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    resources = Path(__file__).resolve().parents[4] / "jiuwenswarm/resources"
    raw = yaml.safe_load((resources / template).read_text())
    config = config_module.resolve_env_vars({key: raw[key] for key in ("memory", "modes", "auto_memory_enabled")})
    monkeypatch.setattr(config_module, "get_config", lambda: config)
    assert policy.is_external_memory_enabled(config)
    assert not policy.is_builtin_memory_allowed(config)
    assert config["memory"]["mode"] == "cloud"
    assert isinstance(build_external_memory_rail(config, workspace_dir=str(tmp_path)), CeliaMcpPromptRail)
    for mode in ("agent", "code"):
        assert not is_memory_enabled(mode, config)
        assert not is_auto_memory_enabled(mode, config)
        assert not DreamingConfig.load(mode).enabled
    # Selecting the preserved provider constructs the real old rail.
    config["memory"]["mode"] = "local"
    config["memory"]["external"]["provider"] = "old-celia"
    assert isinstance(build_external_memory_rail(config, workspace_dir=str(tmp_path)), CeliaMemoryRail)


def test_providers_load_separate_matching_prompts():
    new, old = load_celia_agent_prompt(), load_old_celia_agent_prompt()
    assert "`mcp_celiamcp_memory_store`" in new and "USER.md" not in new
    assert "`memory_store`" in old and "mcp_celiamcp_" not in old
    assert "USER.md" in old


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,engine,expected", [("celia", "external", False), ("old-celia", "external", True), ("", "builtin", True), ("", "none", False)])
async def test_context_reads_follow_legacy_configuration(tmp_path, monkeypatch, provider, engine, expected):
    config = {"memory": {"mode": "local", "engine": engine, "external": {"provider": provider}},
              "modes": {"agent": {"memory": {"enabled": True}}}}
    monkeypatch.setattr(config_module, "get_config", lambda: config)
    workspace = WorkspaceSpec(root_path=str(tmp_path), language="en").build()
    (tmp_path / "USER.md").write_text("USER_MEMORY_SENTINEL")
    operation = SysOperation(SysOperationCard(id="memory-context-fs", work_config=LocalWorkConfig(sandbox_root=[str(tmp_path)])))
    operation.fs().read_file = AsyncMock(wraps=operation.fs().read_file)
    builder = SystemPromptBuilder(language="en")
    agent = SimpleNamespace(system_prompt_builder=builder, prompt_attachment_manager=None, ability_manager=SimpleNamespace(list=lambda: []))
    rail = OrderedContextAssembleRail()
    rail.set_workspace(workspace)
    rail.set_sys_operation(operation)
    rail.init(agent)
    ctx = AgentCallbackContext(agent=agent, inputs=SimpleNamespace(), session=None, extra={})
    await rail.before_model_call(ctx)
    assert ("USER_MEMORY_SENTINEL" in builder.build()) is expected
    user_reads = [call for call in operation.fs().read_file.await_args_list if "USER.md" in str(call)]
    assert bool(user_reads) is expected
    assert (tmp_path / "USER.md").read_text() == "USER_MEMORY_SENTINEL"


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_enabled", [False, True])
async def test_request_tool_restoration_respects_builtin_switch(monkeypatch, tmp_path, legacy_enabled):
    config = {"memory": {"mode": "local", "engine": "builtin" if legacy_enabled else "external"},
              "modes": {"agent": {"memory": {"enabled": legacy_enabled}}}}
    monkeypatch.setattr(interface_deep, "get_config", lambda: config)
    monkeypatch.setattr(interface_deep, "TOOL_PERMISSION_CONTEXT", SimpleNamespace(get=lambda: SimpleNamespace(group_digital_avatar=False, avatar_mode=False, enable_memory=True)))
    import openjiuwen.core.memory.lite.memory_tools as sdk_tools
    tools = [SimpleNamespace(card=SimpleNamespace(name=name)) for name in ("write_memory", "edit_memory")]
    # Emulate SDK versions that still expose the optional restoration helper.
    monkeypatch.setattr(sdk_tools, "get_decorated_tools", lambda: tools, raising=False)
    adapter = Mock(_project_dir=str(tmp_path), _runtime_prompt_rail=None, _response_prompt_rail=None,
                   _subagent_rail=None, _permission_rail=None, _circuit_breaker_rail=None)
    for name in ("_update_rails_for_mode", "_set_user_interaction_enabled", "_update_tools_for_mode", "_update_session_tools"):
        setattr(adapter, name, AsyncMock())
    runtime = SimpleNamespace(workspace=str(tmp_path), project_dir=str(tmp_path), cwd=str(tmp_path),
                              channel_id="web", session_id="session", request_id="request", mode="agent",
                              supports_user_interaction=True, request_metadata={})
    await interface_deep.JiuWenSwarmDeepAdapter._apply_runtime_config_stages(adapter, runtime, Mock(), bind_request=True)
    assert adapter._instance.ability_manager.add.call_count == (2 if legacy_enabled else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("skill_mode", [SkillUseRail.SKILL_MODE_AUTO_LIST, SkillUseRail.SKILL_MODE_ALL])
async def test_celia_memory_follows_actual_skill_section(tmp_path, skill_mode):
    builder = SystemPromptBuilder(language="en")
    agent = SimpleNamespace(system_prompt_builder=builder, prompt_attachment_manager=None,
                            card=SimpleNamespace(id="skill-memory"), ability_manager=Mock(),
                            deep_config=SimpleNamespace(enable_read_image_multimodal=False))
    skill = SkillUseRail(str(tmp_path), skill_mode=skill_mode, include_tools=False)
    skill.init(agent)
    memory = CeliaMcpPromptRail()
    memory.init(agent)
    ctx = AgentCallbackContext(agent=agent, inputs=SimpleNamespace(tools=[]), session=None, extra={})
    await skill.before_model_call(ctx)
    await memory.before_model_call(ctx)
    assert builder.build().index("# Skills") < builder.build().index("## Memory")
    assert builder.build().count(load_celia_agent_prompt()) == 1
