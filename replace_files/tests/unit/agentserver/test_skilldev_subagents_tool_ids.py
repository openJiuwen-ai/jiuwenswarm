# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for skilldev subagent tool id stability and isolation.

验证目标
- subagent 的 tools 列表为 **混合类型**：文件/shell 工具是 Tool 实例，
  Web 工具与元工具（invoke/exec）为 ToolCard。
- 子 agent 使用独立 agent_id（``{parent}-executor`` / ``{parent}-grader``），
  工具 id 与父 agent 不重叠。
- 相同 agent_id 多次构建，工具 id 集合稳定不变。
- 父 agent 在 ``create_instance`` 中通过 ``_register_tools`` 预注册子 agent 的
  Tool 实例并替换为 ToolCard，避免 ``create_subagent`` 内部 identity 冲突。
"""

from __future__ import annotations

import pytest

from jiuwenclaw.agentserver.skilldev_agent.subagents import build_skilldev_subagents
from jiuwenclaw.agentserver.skilldev_agent.subagents.grader import build_grader_config
from jiuwenclaw.agentserver.skilldev_agent.subagents.skill_executor import (
    _build_executor_tools,
    build_skill_executor_config,
)


@pytest.fixture
def shared_sys_operation():
    """构造一个共享的 SysOperation（模拟主 agent 与 subagent 共用同一 sysop）.

    Runner.resource_mgr 是进程级全局单例，跨测试用例复用，因此 fixture 需容忍
    "resource already exist"（幂等：已存在则直接取）。
    """
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.sys_operation import (
        SysOperationCard,
        OperationMode,
        LocalWorkConfig,
    )

    sysop_id = "test_shared_sysop"
    existing = Runner.resource_mgr.get_sys_operation(sysop_id)
    if existing is None:
        card = SysOperationCard(
            id=sysop_id,
            mode=OperationMode.LOCAL,
            work_config=LocalWorkConfig(),
        )
        Runner.resource_mgr.add_sys_operation(card)
    return Runner.resource_mgr.get_sys_operation(sysop_id)


@pytest.fixture
def dummy_model():
    """构造一个最小占位 Model.

    本测试不实际调用模型，build_*_config 只把 model 存入 SubAgentConfig.model，
    不做字段校验。用 MagicMock 避免 ModelClientConfig 的 pydantic 校验开销。
    """
    from unittest.mock import MagicMock

    return MagicMock(name="dummy_model")


def _tool_id_set(tools):
    """辅助：从工具列表提取 card.id 集合（兼容 Tool 实例和 ToolCard）."""
    from openjiuwen.core.foundation.tool import Tool

    ids = set()
    for t in tools:
        card = getattr(t, "card", None)
        if card is not None:
            ids.add(card.id)
        elif isinstance(t, Tool):
            # 兜底：如果 Tool 实例没有 .card（理论上不应发生）
            ids.add(getattr(t, "id", ""))
        else:
            ids.add(getattr(t, "id", ""))
    return ids


def test_executor_tools_mixed_types(shared_sys_operation):
    """_build_executor_tools 返回混合类型：文件/shell 为 Tool，Web/元工具为 ToolCard."""
    from openjiuwen.core.foundation.tool import Tool, ToolCard

    tools = _build_executor_tools(
        shared_sys_operation, language="cn", agent_id="skilldev-agent-task-1"
    )
    assert len(tools) > 0

    tool_instances = [t for t in tools if isinstance(t, Tool) and not isinstance(t, ToolCard)]
    tool_cards = [t for t in tools if isinstance(t, ToolCard)]

    # 文件/shell 工具应为 Tool 实例（8 个：Read/Write/Edit/Glob/Grep/ListDir/Bash/Code）
    assert len(tool_instances) >= 8, (
        f"应包含至少 8 个 Tool 实例（文件/shell），实际 {len(tool_instances)}: "
        f"{[type(t).__name__ for t in tool_instances]}"
    )

    # Web + invoke + exec 应为 ToolCard（4 个）
    assert len(tool_cards) == 4, (
        f"应包含 4 个 ToolCard（WebSearch/WebFetch/invoke/exec），实际 {len(tool_cards)}: "
        f"{[getattr(t, 'id', '') for t in tool_cards]}"
    )


def test_grader_tool_ids_stable_across_calls(shared_sys_operation, dummy_model):
    """grader 同理：固定 agent_id → 工具 id 稳定."""
    config_a = build_grader_config(
        dummy_model,
        language="cn",
        sys_operation=shared_sys_operation,
        agent_id="skilldev-agent-task-1",
    )
    config_b = build_grader_config(
        dummy_model,
        language="cn",
        sys_operation=shared_sys_operation,
        agent_id="skilldev-agent-task-1",
    )

    ids_a = _tool_id_set(config_a.tools)
    ids_b = _tool_id_set(config_b.tools)
    assert ids_a == ids_b, "grader 相同 agent_id 应生成相同工具 id"


def test_main_and_subagent_distinct_tool_ids(shared_sys_operation, dummy_model):
    """主 agent 与子 agent 工具 id 应互不重叠.

    子 agent 使用独立 agent_id（-executor / -grader），确保父 agent 重建 instance
    时 _register_tools 的 remove-add 不会误伤子 agent。
    """
    from jiuwenclaw.agentserver.skilldev_agent.tools import build_skilldev_tools

    parent_tools = build_skilldev_tools(
        sys_operation=shared_sys_operation,
        language="cn",
        agent_id="skilldev-agent-task-1",
    )
    sub_tools = _build_executor_tools(
        shared_sys_operation,
        language="cn",
        agent_id="skilldev-agent-task-1-executor",
    )

    parent_ids = _tool_id_set(parent_tools)
    sub_ids = _tool_id_set(sub_tools)

    # 排除全局固定 id 的元工具（invoke/exec/ask/upload 等）后应无重叠
    meta_ids = {
        "jiuwenclaw_invoke_tool",
        "skilldev_exec_tool",
        "jiuwenclaw_ask_user_question",
    }
    overlap = (parent_ids - meta_ids) & (sub_ids - meta_ids)
    assert not overlap, (
        f"主 agent 与 subagent 的工具 id（除元工具外）不应重叠，"
        f"overlap={overlap}, parent={parent_ids}, sub={sub_ids}"
    )


def test_build_skilldev_subagents_distinct_agent_ids(shared_sys_operation, dummy_model):
    """build_skilldev_subagents 应为不同子 agent 分配独立 agent_id，且 tools 非空."""
    subagents = build_skilldev_subagents(
        dummy_model,
        language="cn",
        sys_operation=shared_sys_operation,
        agent_id="skilldev-agent-task-1",
    )
    names = {sub.agent_card.name for sub in subagents}
    assert names == {"skill_executor", "grader"}

    executor = next(sub for sub in subagents if sub.agent_card.name == "skill_executor")
    grader = next(sub for sub in subagents if sub.agent_card.name == "grader")

    executor_ids = _tool_id_set(executor.tools)
    grader_ids = _tool_id_set(grader.tools)

    # 排除元工具后，executor 与 grader 也不应重叠
    meta_ids = {
        "jiuwenclaw_invoke_tool",
        "skilldev_exec_tool",
    }
    overlap = (executor_ids - meta_ids) & (grader_ids - meta_ids)
    assert not overlap, (
        f"skill_executor 与 grader 的工具 id（除元工具外）不应重叠，overlap={overlap}"
    )


def test_grader_tools_are_tool_instances(shared_sys_operation, dummy_model):
    """grader 的 tools 应全部为 Tool 实例（无 ToolCard）."""
    from openjiuwen.core.foundation.tool import Tool, ToolCard

    config = build_grader_config(
        dummy_model,
        language="cn",
        sys_operation=shared_sys_operation,
        agent_id="skilldev-agent-task-1-grader",
    )
    for t in config.tools:
        assert isinstance(t, Tool), (
            f"grader 工具应为 Tool 实例，实际类型: {type(t).__name__}"
        )
        assert not isinstance(t, ToolCard), (
            f"grader 工具不应是 ToolCard: {getattr(t, 'id', '')}"
        )
