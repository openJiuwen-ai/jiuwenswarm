# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""编排根节点 fallback 虚假 success=true 必须被交付物门禁拒绝。

回放 BUG20260917365344（session officeclaw_a6bded7bcac8aece7c4f4065，
2026-09-17 15:17-15:18）根因：ppt_gen_root 的 fallback 子代理仅修复
p4_3_outline_gen 大纲即声明 ``success=true``，契约校验只认 JSON 自证便放行，
P5-P10（含 PPTX 导出与交付）被整体跳过，HITL resume 以成功收尾，用户收到
"PPT 任务已结束，但未能确认文件已生成并发送"。

期望：根节点声明的 ``validate_fallback_success``（PPTX 真实存在且
delivery_status 完成）在 fallback 契约通过后必须被强制执行，虚假成功以
FallbackContractError 终止性降级。夹具（事故输出 / 合成编排根节点）单源维护于
_ppt_gate_fixture；本用例以事故原文输出驱动 root.run_stream 到
executor.fallback_stream 的生产接线全链路，守护 executor 透传节点校验器的
接线缝（门禁套件直调 handler，未覆盖该缝）。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from jiuwenswarm.server.runtime.skill_turbo.executor import SkillTurboExecutor
from jiuwenswarm.server.runtime.skill_turbo.fallback_handler import (
    FallbackContractError,
)
from jiuwenswarm.server.runtime.skill_turbo.plan_node import PlanNode
from tests.unit_tests.server.runtime.skill_turbo._ppt_gate_fixture import (
    INCIDENT_FAILURE,
    INCIDENT_INPUTS,
    INCIDENT_OUTPUT,
    PPTGenRootNode,
    make_handler,
)


def _make_executor(spawn_output: str) -> SkillTurboExecutor:
    env = MagicMock()
    env.config = {}
    env.skill_code_import_prefixes = (
        "jiuwenswarm.server.runtime.skill_turbo.skill_codes",
    )
    env.fallback_handler = make_handler(spawn_output)
    return SkillTurboExecutor(environment=env)


class _FailingPPTGenRootNode(PPTGenRootNode):
    """原生执行必失败的编排根节点：驱动 run_stream 进入 fallback 路径。"""

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(INCIDENT_FAILURE)

    async def _execute_stream(self, inputs: dict[str, Any]):
        raise RuntimeError(INCIDENT_FAILURE)
        yield  # 使本方法成为异步生成器


@pytest.mark.asyncio
async def test_root_fallback_false_success_rejected():
    """验证编排根节点 fallback 仅修复部分阶段即声明 success=true 时按 FallbackContractError 拒绝。"""
    ex = _make_executor(INCIDENT_OUTPUT)
    root = _FailingPPTGenRootNode()
    assert isinstance(root, PlanNode)
    ex._bind_node_callbacks(root)
    inputs = dict(INCIDENT_INPUTS)

    with pytest.raises(FallbackContractError):
        async for _ in root.run_stream(inputs):
            pass
