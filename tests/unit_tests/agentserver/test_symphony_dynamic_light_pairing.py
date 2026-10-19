"""轻量专家规划与 beta3 原技能 Symphony 在同一真实 Core 运行时兼容。"""

from pathlib import Path

import pytest
import yaml

from jiuwenswarm.agents.harness.common.tools.symphony_toolkits import SymphonyToolkit
from jiuwenswarm.common.config import is_dynamic_experts_enabled
from jiuwenswarm.extensions.symphony.extension import SymphonyExtension
from jiuwenswarm.symphony.config import load_symphony_config
from jiuwenswarm.symphony.expert_team import ExpertPlanTool, PLAN_TOOL_NAME
from jiuwenswarm.symphony.orchestration.service import plan_from_score


def test_packaged_config_preserves_beta3_score_paths_and_independent_defaults():
    config_path = Path(__file__).resolve().parents[3] / "jiuwenswarm/resources/config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert config["symphony"]["paths"] == {"skills_root": "", "score_dir": ""}
    assert config["symphony"]["enabled"] is False
    assert config["symphony"]["dynamic_experts"]["enabled"] is None
    assert load_symphony_config(config).enabled is False
    assert is_dynamic_experts_enabled(config) is True
    assert SymphonyToolkit().get_tools(config) == []


@pytest.mark.parametrize("dynamic_enabled", [False, True])
def test_beta3_score_tools_and_rpc_load_alongside_real_expert_planner(dynamic_enabled):
    config = {"symphony": {
        "enabled": True,
        "dynamic_experts": {"enabled": dynamic_enabled},
    }}
    tools = SymphonyToolkit().get_tools(config)
    assert {tool.card.name for tool in tools} == {
        "symphony_read_score", "symphony_refresh_score", "symphony_compose_score",
    }
    assert ExpertPlanTool(lambda _session_id: None).card.name == PLAN_TOOL_NAME
    # These are the real beta3 extension/service modules. No complete Core
    # Symphony module is substituted to make their imports succeed.
    extension = SymphonyExtension()
    assert extension.plan.__module__ == "jiuwenswarm.extensions.symphony.extension"
    assert plan_from_score.__module__ == "jiuwenswarm.symphony.orchestration.service"


@pytest.mark.asyncio
async def test_expert_plan_tool_is_unavailable_without_an_ordinary_active_round():
    tool = ExpertPlanTool(lambda _session_id: None)
    assert await tool.invoke({"query": "请专家处理"}) == {
        "success": False, "status": "error", "reason": "expert_plan_unavailable",
    }
