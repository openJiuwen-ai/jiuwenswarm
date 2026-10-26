# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""PPT 根级 fallback 门禁测试共享夹具（单源维护）。

BUG20260917365344 事故夹具与合成编排根节点在此单源维护，供
test_fallback_deliverable_gate / test_repro_root_fallback_false_success 引用。
合成 PPTGenRootNode 的 validate_fallback_success 语义与 relay-claw 技能包实现
（office-claw-skills/pptx-craft/turbo/turbo_codes/ppt/ppt_gen_root.py）对齐，
技能包侧语义变更时须同步更新本副本。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from jiuwenswarm.server.runtime.skill_turbo.fallback_handler import (
    DeepAgentFallbackHandler,
)
from jiuwenswarm.server.runtime.skill_turbo.plan_node import PlanNode

# 根级兜底子代理仅完成 P4 阶段的输出形态（事故原文结构）：
# 校验正文 + 末尾单行 JSON 契约，result 仅含 P4 级字段，无 pptx_path / delivery_status。
INCIDENT_OUTPUT = (
    "校验全部通过：✅ 无 chapter/section/agenda 结构页；✅ 研究需求=13 页（P2–P14），"
    "与 page_count=13 完全一致；✅ P1=cover、P15=ending；✅ 每页字段齐全。\n"
    "\n"
    "**本轮 fallback 完成情况总结**\n"
    "\n"
    "本次替代失败的 p4_content_plan / p4_3_outline_gen 链路，已修复根因并完成 P4 产物。\n"
    "以下字段可直接回写 inputs 供下游（P5 页面内容生成）消费：\n"
    "\n"
    '`{"success": true, "result": {"outline_path": '
    '"C:/ws/20260917151247/output/20260917_151258_000/outline.md", '
    '"p4_outline_gen_status": "completed", "p4_validate_status": "passed", '
    '"content_plan_status": "completed", "total_pages": 15, "content_pages": 13}}`'
)

INCIDENT_INPUTS = {
    "output_dir": "C:/ws/20260917151247/output/20260917_151258_000",
    "outline_path": "C:/ws/20260917151247/output/20260917_151258_000/outline.md",
    "topic": "糖尿病防治指南",
    "p4_validate_status": "failed",
}

INCIDENT_FAILURE = "fallback 未达成节点 p4_content_plan 契约: fallback 输出未包含 JSON 契约声明"


def make_handler(contract_output: str) -> DeepAgentFallbackHandler:
    adapter = MagicMock()
    adapter.spawn_fallback = AsyncMock(return_value=contract_output)
    return DeepAgentFallbackHandler(
        adapter, request_id="req-ut", channel_id="officeclaw", session_id="sess-ut"
    )


class PPTGenRootNode(PlanNode):
    """PPT 编排根节点（本地合成：ppt 技能源码已外部化，校验语义对齐技能包实现）。"""

    def __init__(self) -> None:
        super().__init__(
            plan_name="ppt_gen_root",
            instruction="PPT生成任务流根节点，串联P0-P10全流程",
            sub_plans=[],
            depth=0,
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"node": self.plan_name, "status": "ok"}

    def validate_fallback_success(
        self,
        inputs: dict[str, Any],
        contract_result: dict[str, Any],
    ) -> str | None:
        merged = {**inputs, **(contract_result or {})}
        pptx_path = str(merged.get("pptx_path") or "").strip()
        if not pptx_path or not Path(pptx_path).is_file():
            return (
                "root fallback 未产出可交付的 PPTX（pptx_path 缺失或文件不存在），"
                "仅完成部分阶段不能视为全流程成功"
            )
        if str(merged.get("delivery_status") or "").strip() not in ("ok", "partial"):
            return "root fallback 未完成 P10 交付（delivery_status 缺失或为 failed）"
        return None
