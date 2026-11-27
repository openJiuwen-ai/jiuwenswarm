from types import SimpleNamespace
from pathlib import Path
from unittest.mock import ANY, patch, MagicMock
import sys

import pytest

# Mock trafilatura before importing jiuwenclaw modules
sys.modules['trafilatura'] = MagicMock()

from openjiuwen.core.foundation.llm import Model
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection, SystemPromptBuilder

from jiuwenclaw.agentserver.deep_agent.interface_deep import JiuWenClawDeepAdapter
from jiuwenclaw.agentserver.deep_agent.prompt_builder import build_identity_prompt
from jiuwenclaw.agentserver.deep_agent.rails.runtime_prompt_rail import RuntimePromptRail
from jiuwenclaw.agentserver.tools.harness_named_web_tools import (
    JiuwenHarnessFetchWebpageTool,
    JiuwenHarnessFreeSearchTool,
)


class _TestableJiuWenClawDeepAdapter(JiuWenClawDeepAdapter):
    def set_workspace_dir(self, workspace_dir: str) -> None:
        self._workspace_dir = workspace_dir

    def build_configured_subagents(
        self,
        model: Model,
        config: dict,
        config_base: dict | None = None,
    ):
        return self._build_configured_subagents(model, config, config_base)


def test_build_identity_prompt_contains_identity_section_only():
    prompt = build_identity_prompt(mode="agent.fast", language="zh", channel="web")

    assert "# 消息说明" not in prompt


@pytest.mark.asyncio
async def test_runtime_time_section_participates_in_priority_order():
    builder = SystemPromptBuilder(language="cn")
    builder.add_section(PromptSection(name="identity", content={"cn": "identity"}, priority=10))
    builder.add_section(PromptSection(name="tools", content={"cn": "# 可用工具"}, priority=30))
    builder.add_section(PromptSection(name="mid_static", content={"cn": "# 中间静态区"}, priority=70))

    runtime_rail = RuntimePromptRail(
        language="cn",
        channel="web",
        agent_name="main_agent",
        model_name="test-model",
    )
    runtime_rail.init(SimpleNamespace(system_prompt_builder=builder))

    ctx = AgentCallbackContext(agent=None, inputs=None, session=None)
    await runtime_rail.before_model_call(ctx)

    prompt = builder.build()
    ordered_markers = [
        "identity",
        "# 你的家",
        "# 可用工具",
        "# 中间静态区",
        "# 当前日期与时间",
        "# 运行时",
    ]
    positions = [prompt.index(marker) for marker in ordered_markers]
    assert positions == sorted(positions)
    assert "频道：web" in prompt


def test_resolve_skill_mode_accepts_all_and_auto_list():
    assert JiuWenClawDeepAdapter._resolve_skill_mode({"skill_mode": "all"}) == "all"
    assert JiuWenClawDeepAdapter._resolve_skill_mode({"skill_mode": "auto_list"}) == "auto_list"
    assert JiuWenClawDeepAdapter._resolve_skill_mode({"skill_mode": "invalid"}) == "all"


def test_build_configured_subagents_includes_optional_browser_and_configured_code_research():
    adapter = _TestableJiuWenClawDeepAdapter()
    adapter.set_workspace_dir("/tmp/jiuwenclaw-workspace")
    model = object()
    config = {
        "max_iterations": 9,
        "subagents": {
            "code_agent": {"enabled": True, "max_iterations": 5},
            "research_agent": {"enabled": True},
            "browser_agent": {"max_iterations": 7},
        },
    }

    with (
        patch.object(adapter, "_resolve_runtime_language", return_value="cn"),
        patch(
            "jiuwenclaw.agentserver.deep_agent.interface_deep.JiuWenClawDeepAdapter._browser_runtime_enabled",
            return_value=True,
        ),
        patch(
            "jiuwenclaw.agentserver.deep_agent.interface_deep.get_memory_mode",
            return_value="remote",
        ),
        patch(
            "jiuwenclaw.agentserver.deep_agent.interface_deep.build_code_agent_config",
            return_value="code_spec",
        ) as mock_code,
        patch(
            "jiuwenclaw.agentserver.deep_agent.interface_deep.build_research_agent_config",
            return_value="research_spec",
        ) as mock_research,
        patch(
            "jiuwenclaw.agentserver.deep_agent.interface_deep.build_browser_agent_config",
            return_value="browser_spec",
        ) as mock_browser,
    ):
        subagents = adapter.build_configured_subagents(model, config)

    assert subagents == ["code_spec", "research_spec", "browser_spec"]
    mock_code.assert_called_once_with(
        model,
        workspace="/tmp/jiuwenclaw-workspace",
        language="cn",
        rails=None,
        max_iterations=5,
    )
    mock_research.assert_called_once_with(
        model,
        workspace="/tmp/jiuwenclaw-workspace",
        language="cn",
        max_iterations=9,
        tools=ANY,
    )
    research_tools = mock_research.call_args.kwargs["tools"]
    assert len(research_tools) == 2
    assert isinstance(research_tools[0], JiuwenHarnessFreeSearchTool)
    assert isinstance(research_tools[1], JiuwenHarnessFetchWebpageTool)
    mock_browser.assert_called_once_with(
        model,
        workspace="/tmp/jiuwenclaw-workspace",
        language="cn",
        max_iterations=7,
    )


def test_build_configured_subagents_omits_code_research_without_explicit_enable():
    adapter = _TestableJiuWenClawDeepAdapter()
    adapter.set_workspace_dir("/tmp/jiuwenclaw-workspace")
    model = object()
    config = {"max_iterations": 9}

    with (
        patch.object(adapter, "_resolve_runtime_language", return_value="cn"),
        patch(
            "jiuwenclaw.agentserver.deep_agent.interface_deep.JiuWenClawDeepAdapter._browser_runtime_enabled",
            return_value=True,
        ),
        patch(
            "jiuwenclaw.agentserver.deep_agent.interface_deep.build_code_agent_config",
            return_value="code_spec",
        ) as mock_code,
        patch(
            "jiuwenclaw.agentserver.deep_agent.interface_deep.build_research_agent_config",
            return_value="research_spec",
        ) as mock_research,
        patch(
            "jiuwenclaw.agentserver.deep_agent.interface_deep.build_browser_agent_config",
            return_value="browser_spec",
        ) as mock_browser,
    ):
        subagents = adapter.build_configured_subagents(model, config)

    assert subagents == ["browser_spec"]
    mock_code.assert_not_called()
    mock_research.assert_not_called()
    mock_browser.assert_called_once()


@pytest.mark.asyncio
async def test_runtime_rail_multi_tenant_workspace_dirs():
    """测试多租户模式下 _get_workspace_dirs 返回正确路径。"""
    
    builder = SystemPromptBuilder(language="cn")
    runtime_rail = RuntimePromptRail(
        language="cn",
        channel="web",
        agent_name="main_agent",
        model_name="test-model",
        agent_id="test_agent_001",
        service_id="test_service_001",
    )
    runtime_rail.init(SimpleNamespace(system_prompt_builder=builder))

    # Mock get_multi_tenant_user_workspace_dir 返回测试路径
    expected_base = Path("/tmp/test_jiuwenclaw/service_test_service_001/agent_test_agent_001")
    with patch(
        "jiuwenclaw.agentserver.deep_agent.rails.runtime_prompt_rail.get_multi_tenant_user_workspace_dir",
        return_value=expected_base,
    ):
        ctx = AgentCallbackContext(agent=None, inputs=None, session=None)
        await runtime_rail.before_model_call(ctx)

    prompt = builder.build()
    
    # 验证多租户路径出现在 prompt 中（兼容 Windows 路径分隔符）
    assert "config" in prompt
    assert "jiuwenclaw_workspace" in prompt
    assert "memory" in prompt
    assert "skills" in prompt
    assert "todo" in prompt
    # 验证多租户路径特征
    assert "service_test_service_001" in prompt
    assert "agent_test_agent_001" in prompt
    
    # 验证完整的绝对路径格式（兼容 Windows/Linux 路径分隔符）
    expected_config = str(expected_base / "config")
    expected_workspace = str(expected_base / "jiuwenclaw_workspace")
    expected_memory = str(expected_base / "jiuwenclaw_workspace" / "memory")
    expected_skills = str(expected_base / "jiuwenclaw_workspace" / "skills")
    expected_todo = str(expected_base / "jiuwenclaw_workspace" / "todo")
    
    # Windows 下 Path 会转换为 \ 分隔符，需要兼容
    expected_config_win = expected_config.replace("/", "\\")
    expected_workspace_win = expected_workspace.replace("/", "\\")
    expected_memory_win = expected_memory.replace("/", "\\")
    expected_skills_win = expected_skills.replace("/", "\\")
    expected_todo_win = expected_todo.replace("/", "\\")
    
    # 验证所有路径都出现在 prompt 中（兼容两种分隔符）
    assert (expected_config in prompt or expected_config_win in prompt), f"Config path not found: {expected_config}"
    assert (expected_workspace in prompt or expected_workspace_win in prompt), f"Workspace path not found: {expected_workspace}"
    assert (expected_memory in prompt or expected_memory_win in prompt), f"Memory path not found: {expected_memory}"
    assert (expected_skills in prompt or expected_skills_win in prompt), f"Skills path not found: {expected_skills}"
    assert (expected_todo in prompt or expected_todo_win in prompt), f"Todo path not found: {expected_todo}"


@pytest.mark.asyncio
async def test_runtime_rail_single_tenant_workspace_dirs():
    """测试单租户模式下 _get_workspace_dirs 回退到默认路径。"""
    builder = SystemPromptBuilder(language="cn")
    runtime_rail = RuntimePromptRail(
        language="cn",
        channel="web",
        agent_name="main_agent",
        model_name="test-model",
        # 不传 agent_id 和 service_id，触发单租户模式
    )
    runtime_rail.init(SimpleNamespace(system_prompt_builder=builder))

    with (
        patch("jiuwenclaw.agentserver.deep_agent.rails.runtime_prompt_rail.get_user_workspace_dir") as mock_user_ws,
        patch("jiuwenclaw.agentserver.deep_agent.rails.runtime_prompt_rail.get_agent_workspace_dir") as mock_agent_ws,
        patch("jiuwenclaw.agentserver.deep_agent.rails.runtime_prompt_rail.get_agent_memory_dir") as mock_memory,
        patch("jiuwenclaw.agentserver.deep_agent.rails.runtime_prompt_rail.get_agent_registered_skill_dirs") as mock_skills,
        patch("jiuwenclaw.agentserver.deep_agent.rails.runtime_prompt_rail.get_deepagent_todo_dir") as mock_todo,
    ):
        mock_user_ws.return_value = Path("/home/user/.jiuwenclaw")
        mock_agent_ws.return_value = Path("/home/user/.jiuwenclaw/workspace")
        mock_memory.return_value = Path("/home/user/.jiuwenclaw/memory")
        mock_skills.return_value = [Path("/home/user/.jiuwenclaw/skills")]
        mock_todo.return_value = Path("/home/user/.jiuwenclaw/todo")
        
        ctx = AgentCallbackContext(agent=None, inputs=None, session=None)
        await runtime_rail.before_model_call(ctx)

    prompt = builder.build()
    
    # 验证单租户路径出现在 prompt 中（兼容 Windows 路径分隔符）
    assert "config" in prompt
    assert "workspace" in prompt
    assert "memory" in prompt
    assert "skills" in prompt
    assert "todo" in prompt
    # 验证路径格式（Windows 使用 \，Linux 使用 /）
    assert (".jiuwenclaw" in prompt)
    
    # 验证完整的绝对路径格式（兼容 Windows/Linux 路径分隔符）
    expected_config = "/home/user/.jiuwenclaw/config"
    expected_workspace = "/home/user/.jiuwenclaw/workspace"
    expected_memory = "/home/user/.jiuwenclaw/memory"
    expected_skills = "/home/user/.jiuwenclaw/skills"
    expected_todo = "/home/user/.jiuwenclaw/todo"
    
    # Windows 下 Path 会转换为 \ 分隔符，需要兼容
    expected_config_win = expected_config.replace("/", "\\")
    expected_workspace_win = expected_workspace.replace("/", "\\")
    expected_memory_win = expected_memory.replace("/", "\\")
    expected_skills_win = expected_skills.replace("/", "\\")
    expected_todo_win = expected_todo.replace("/", "\\")
    
    # 验证所有路径都出现在 prompt 中（兼容两种分隔符）
    assert (expected_config in prompt or expected_config_win in prompt), f"Config path not found: {expected_config}"
    assert (expected_workspace in prompt or expected_workspace_win in prompt), f"Workspace path not found: {expected_workspace}"
    assert (expected_memory in prompt or expected_memory_win in prompt), f"Memory path not found: {expected_memory}"
    assert (expected_skills in prompt or expected_skills_win in prompt), f"Skills path not found: {expected_skills}"
    assert (expected_todo in prompt or expected_todo_win in prompt), f"Todo path not found: {expected_todo}"


def test_interface_deep_skill_rail_uses_multi_tenant_paths():
    """测试 interface_deep 中的 SkillUseRail 使用多租户 skills 路径。"""
    from jiuwenclaw.utils import get_multi_tenant_skill_dirs
    
    # 测试多租户模式
    service_id = "test_service"
    agent_id = "test_agent"
    
    with patch(
        "jiuwenclaw.utils.get_multi_tenant_user_workspace_dir",
    ) as mock_workspace:
        mock_workspace.return_value = Path("/tmp/test/service_test/agent_test")
        skill_dirs = get_multi_tenant_skill_dirs(service_id, agent_id)
    
    # 验证返回的是多租户路径
    assert len(skill_dirs) == 1
    assert "service_test" in str(skill_dirs[0])
    assert "agent_test" in str(skill_dirs[0])
    assert "skills" in str(skill_dirs[0])
    
    # 测试单租户模式（不传参数）
    with patch(
        "jiuwenclaw.utils.get_agent_skills_dir",
    ) as mock_single:
        mock_single.return_value = Path("/home/user/.jiuwenclaw/skills")
        skill_dirs_single = get_multi_tenant_skill_dirs(None, None)
    
    assert len(skill_dirs_single) == 1
    assert str(skill_dirs_single[0]) == "/home/user/.jiuwenclaw/skills"
