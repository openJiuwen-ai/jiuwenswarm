# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for skilldev subagent workspace/sys_operation reuse.

验证目标：
- ``build_skilldev_subagents`` 传入 ``workspace_dir`` 后，子 agent 的
  ``SubAgentConfig.workspace`` 非 None，确保 ``create_subagent`` 的条件
  ``spec.workspace is not None`` 成立，从而复用 ``sys_operation``。
- 多次实例化相同 task 的子 agent，``Runner.resource_mgr`` 中不会新增
  孤儿 sysop（即 sysop 被复用而非重复创建）。
"""

from __future__ import annotations

import pytest

from jiuwenclaw.agentserver.skilldev_agent.subagents import build_skilldev_subagents
from jiuwenclaw.agentserver.skilldev_agent.subagents.grader import build_grader_config
from jiuwenclaw.agentserver.skilldev_agent.subagents.skill_executor import (
    build_skill_executor_config,
)


@pytest.fixture
def shared_sys_operation():
    """构造一个共享的 SysOperation（进程级全局单例，需容忍已存在）."""
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.sys_operation import (
        SysOperationCard,
        OperationMode,
        LocalWorkConfig,
    )

    sysop_id = "test_workspace_sysop"
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
    from unittest.mock import MagicMock
    return MagicMock(name="dummy_model")


def _list_sysop_ids() -> set[str]:
    """通过反射获取 Runner.resource_mgr 中已注册的所有 sysop id."""
    from openjiuwen.core.runner import Runner
    rr = getattr(Runner.resource_mgr, "_resource_registry", None)
    sys_mgr = getattr(rr, "_sys_operation_mgr", None)
    sys_ops = getattr(sys_mgr, "_sys_operations", None)
    if sys_ops is None:
        return set()
    try:
        return set(sys_ops.keys())
    except Exception:
        return set()


def test_create_deep_agent_with_subagents_reuses_sysop(shared_sys_operation, dummy_model):
    """通过 create_deep_agent 创建父 agent，再触发 create_subagent 实例化子 agent，验证不新建孤儿 sysop.

    若 SubAgentConfig.workspace=None，create_subagent 会走 else None 分支，导致
    create_deep_agent 新建孤儿 sysop（id 形如 skill_executor_xxx / grader_xxx）。
    显式设置 workspace 后，sys_operation 应被复用。
    """
    from openjiuwen.harness.factory import create_deep_agent
    from openjiuwen.core.single_agent import AgentCard
    from openjiuwen.harness.workspace.workspace import Workspace

    # 构造子 agent 配置（已含 workspace + sys_operation）
    subagents = build_skilldev_subagents(
        dummy_model,
        language="cn",
        sys_operation=shared_sys_operation,
        agent_id="skilldev-agent-task-ws-1",
        workspace_dir="/tmp/test_ws",
    )

    before_sysops = _list_sysop_ids()
    # 创建父 agent
    parent = create_deep_agent(
        model=dummy_model,
        card=AgentCard(
            name="skilldev-agent",
            id="skilldev-agent-task-ws-1",
            description="test parent agent",
        ),
        system_prompt="test system prompt",
        tools=[],  # 父 agent 工具在测试中简化
        subagents=subagents,
        workspace=Workspace(
            root_path="/tmp/test_ws",
            language="cn",
            directories=[],
        ),
        sys_operation=shared_sys_operation,
        language="cn",
    )
    assert parent is not None, "create_deep_agent 应成功返回父 agent 实例"

    # 触发子 agent 实际创建（模拟 ForkAgentExecutor / spawn_subagent 的调用路径）
    for sub_cfg in subagents:
        sub_name = sub_cfg.agent_card.name
        sub_session_id = f"test_{sub_name}_session"
        try:
            # openjiuwen 的 create_subagent 签名通常为 (name, session_id)
            parent.create_subagent(sub_name, sub_session_id)
        except Exception as exc:
            pytest.fail(f"create_subagent({sub_name}, {sub_session_id}) 不应抛异常: {exc}")

    after_sysops = _list_sysop_ids()
    new_sysops = after_sysops - before_sysops

    # 不应出现 skill_executor_* / grader_* 前缀的孤儿 sysop
    orphans = {
        sid for sid in new_sysops
        if sid.startswith(("skill_executor_", "grader_"))
    }
    assert not orphans, (
        f"create_subagent 不应产生 skill_executor_/grader_ 前缀的孤儿 sysop，"
        f"但发现: {orphans}"
    )

    # 验证子 agent 配置中的 workspace 和 sys_operation 被保留
    parent_subagents = getattr(parent, "subagents", None) or getattr(parent, "_subagents", [])
    if parent_subagents:
        for sub in parent_subagents:
            cfg = getattr(sub, "config", sub)
            assert getattr(cfg, "workspace", None) is not None, (
                f"子 agent {cfg.agent_card.name} 的 workspace 不应为 None"
            )
            assert getattr(cfg, "sys_operation", None) is not None, (
                f"子 agent {cfg.agent_card.name} 的 sys_operation 不应为 None"
            )
