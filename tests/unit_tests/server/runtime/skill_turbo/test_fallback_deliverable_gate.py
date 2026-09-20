# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""根级 fallback 交付物门禁：虚假 success=true 必须被拒绝。

根级 fallback subagent 若仅完成部分阶段（如只修复大纲）便声明
success=true，后续导出与交付阶段会被整体跳过。本套测试守护
validate_fallback_success 校验缝：PPTX 真实存在且交付完成才放行。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.server.runtime.skill_turbo.fallback_handler import (
    DeepAgentFallbackHandler,
    FallbackCall,
    FallbackContractError,
)
from jiuwenswarm.server.runtime.skill_turbo.plan_node import PlanNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_gen_root import (
    PPTGenRootNode,
)

# 根级兜底子代理仅完成 P4 阶段的输出形态：
# 校验正文 + 末尾单行 JSON 契约，result 仅含 P4 级字段。
_INCIDENT_OUTPUT = (
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

_INCIDENT_INPUTS = {
    "output_dir": "C:/ws/20260917151247/output/20260917_151258_000",
    "outline_path": "C:/ws/20260917151247/output/20260917_151258_000/outline.md",
    "topic": "糖尿病防治指南",
    "p4_validate_status": "failed",
}

_FAILURE = "fallback 未达成节点 p4_content_plan 契约: fallback 输出未包含 JSON 契约声明"


def _make_handler(contract_output: str) -> DeepAgentFallbackHandler:
    adapter = MagicMock()
    adapter.spawn_fallback = AsyncMock(return_value=contract_output)
    return DeepAgentFallbackHandler(
        adapter, request_id="req-ut", channel_id="officeclaw", session_id="sess-ut"
    )


class TestPPTRootValidator:
    def test_rejects_partial_stage_success_without_pptx(self):
        """仅 P4 字段、无 pptx_path/delivery_status → 拒绝。"""
        root = PPTGenRootNode()
        contract_result = {
            "outline_path": "C:/x/outline.md",
            "p4_validate_status": "passed",
            "content_plan_status": "completed",
        }
        reason = root.validate_fallback_success(dict(_INCIDENT_INPUTS), contract_result)
        assert reason is not None
        assert "PPTX" in reason

    def test_rejects_pptx_path_pointing_to_missing_file(self, tmp_path):
        root = PPTGenRootNode()
        contract_result = {
            "pptx_path": str(tmp_path / "not_exist.pptx"),
            "delivery_status": "ok",
        }
        reason = root.validate_fallback_success({}, contract_result)
        assert reason is not None
        assert "PPTX" in reason

    def test_rejects_pptx_without_delivery(self, tmp_path):
        root = PPTGenRootNode()
        pptx = tmp_path / "diabetes.pptx"
        pptx.write_bytes(b"x" * 20)
        reason = root.validate_fallback_success(
            {}, {"pptx_path": str(pptx), "delivery_status": "failed"}
        )
        assert reason is not None
        assert "P10" in reason
        reason_missing = root.validate_fallback_success({}, {"pptx_path": str(pptx)})
        assert reason_missing is not None

    def test_accepts_delivered_pptx(self, tmp_path):
        root = PPTGenRootNode()
        pptx = tmp_path / "diabetes.pptx"
        pptx.write_bytes(b"x" * 20)
        assert (
            root.validate_fallback_success(
                {}, {"pptx_path": str(pptx), "delivery_status": "ok"}
            )
            is None
        )
        assert (
            root.validate_fallback_success(
                {"pptx_path": str(pptx)}, {"delivery_status": "partial"}
            )
            is None
        )

    def test_plan_node_default_validator_passes(self):
        node = PPTGenRootNode()
        assert isinstance(node, PlanNode)
        # 基类默认放行（非编排节点不受门禁影响）
        class _LeafNode(PlanNode):
            def __init__(self) -> None:
                super().__init__(plan_name="p_x", instruction="x")

            async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
                return {}

        assert _LeafNode().validate_fallback_success({}, {}) is None


class TestStreamFallbackHonorsValidator:
    async def test_incident_false_success_rejected(self):
        """流式：根级 fallback 虚假 success → FallbackContractError。"""
        handler = _make_handler(_INCIDENT_OUTPUT)
        root = PPTGenRootNode()
        inputs = dict(_INCIDENT_INPUTS)

        chunks: list[dict[str, Any]] = []
        with pytest.raises(FallbackContractError) as excinfo:
            async for chunk in handler.fallback_stream(
                FallbackCall(
                    node_name="ppt_gen_root",
                    instruction="PPT生成任务流根节点，串联P0-P10全流程",
                    inputs=inputs,
                    error=RuntimeError(_FAILURE),
                    parent_session=None,
                    result_validator=root.validate_fallback_success,
                )
            ):
                chunks.append(chunk)

        # 拒绝原因进入异常信息，便于降级链路诊断
        assert "PPTX" in str(excinfo.value)
        # 只允许生命周期事件流出，不得流出成功结果
        assert [c.get("event_type") for c in chunks] == [
            "fallback.started",
            "fallback.finished",
        ]
        # 契约字段不得回写共享上下文（与契约失败路径一致）
        assert inputs == _INCIDENT_INPUTS

    async def test_truthful_delivery_accepted_and_merged(self, tmp_path):
        pptx = tmp_path / "diabetes.pptx"
        pptx.write_bytes(b"x" * 20)
        output = (
            "PPT 已全流程生成并交付。\n"
            f'`{{"success": true, "result": {{"pptx_path": "{pptx.as_posix()}", '
            '"delivery_status": "ok"}}}`'
        )
        handler = _make_handler(output)
        root = PPTGenRootNode()
        inputs = {"topic": "糖尿病防治指南"}

        chunks = [
            chunk
            async for chunk in handler.fallback_stream(
                FallbackCall(
                    node_name="ppt_gen_root",
                    instruction="PPT生成任务流根节点，串联P0-P10全流程",
                    inputs=inputs,
                    error=RuntimeError(_FAILURE),
                    parent_session=None,
                    result_validator=root.validate_fallback_success,
                )
            )
        ]

        assert "fallback.finished" in [c.get("event_type") for c in chunks]
        # 交付字段回写共享上下文，供 finish_text / 产物账本消费
        assert inputs["pptx_path"] == pptx.as_posix()
        assert inputs["delivery_status"] == "ok"
        assert inputs["fallback"] is True


class TestNonStreamFallbackHonorsValidator:
    async def test_incident_false_success_rejected(self):
        handler = _make_handler(_INCIDENT_OUTPUT)
        root = PPTGenRootNode()
        inputs = dict(_INCIDENT_INPUTS)

        with pytest.raises(FallbackContractError) as excinfo:
            await handler.fallback(
                FallbackCall(
                    node_name="ppt_gen_root",
                    instruction="PPT生成任务流根节点，串联P0-P10全流程",
                    inputs=inputs,
                    error=RuntimeError(_FAILURE),
                    parent_session=None,
                    result_validator=root.validate_fallback_success,
                )
            )

        assert "PPTX" in str(excinfo.value)
        assert inputs == _INCIDENT_INPUTS

    async def test_validator_crash_fails_closed(self):
        """校验器自身异常也必须拒绝（fail-closed），不能放行虚假成功。"""
        handler = _make_handler(_INCIDENT_OUTPUT)
        inputs = {"topic": "x"}

        def _crashing_validator(
            inputs: dict[str, Any], contract_result: dict[str, Any]
        ) -> str | None:
            raise ValueError("validator boom")

        with pytest.raises(FallbackContractError) as excinfo:
            await handler.fallback(
                FallbackCall(
                    node_name="ppt_gen_root",
                    instruction="PPT生成任务流根节点，串联P0-P10全流程",
                    inputs=inputs,
                    error=RuntimeError(_FAILURE),
                    parent_session=None,
                    result_validator=_crashing_validator,
                )
            )

        assert "validator" in str(excinfo.value)


class TestFallbackQueryGuards:
    def test_query_states_orchestrator_success_criteria(self):
        query = DeepAgentFallbackHandler._build_fallback_query(
            "ppt_gen_root", "PPT生成任务流根节点，串联P0-P10全流程", {}, RuntimeError("x")
        )
        assert "编排类节点" in query
        assert "终端产物已生成并交付" in query
