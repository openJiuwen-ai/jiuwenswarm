# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""根级 fallback 交付物门禁：虚假 success=true 必须被拒绝。

根级 fallback subagent 若仅完成部分阶段（如只修复大纲）便声明
success=true，后续导出与交付阶段会被整体跳过。本套测试守护
validate_fallback_success 校验缝：PPTX 真实存在且交付完成才放行。
"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.server.runtime.skill_turbo.fallback_handler import (
    DeepAgentFallbackHandler,
    FallbackCall,
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


class TestPPTRootValidator:
    def test_rejects_partial_stage_success_without_pptx(self):
        """仅 P4 字段、无 pptx_path/delivery_status → 拒绝。"""
        root = PPTGenRootNode()
        contract_result = {
            "outline_path": "C:/x/outline.md",
            "p4_validate_status": "passed",
            "content_plan_status": "completed",
        }
        reason = root.validate_fallback_success(dict(INCIDENT_INPUTS), contract_result)
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
        handler = make_handler(INCIDENT_OUTPUT)
        root = PPTGenRootNode()
        inputs = dict(INCIDENT_INPUTS)

        chunks: list[dict[str, Any]] = []
        with pytest.raises(FallbackContractError) as excinfo:
            async for chunk in handler.fallback_stream(
                FallbackCall(
                    node_name="ppt_gen_root",
                    instruction="PPT生成任务流根节点，串联P0-P10全流程",
                    inputs=inputs,
                    error=RuntimeError(INCIDENT_FAILURE),
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
        assert inputs == INCIDENT_INPUTS

    async def test_truthful_delivery_accepted_and_merged(self, tmp_path):
        pptx = tmp_path / "diabetes.pptx"
        pptx.write_bytes(b"x" * 20)
        output = (
            "PPT 已全流程生成并交付。\n"
            f'`{{"success": true, "result": {{"pptx_path": "{pptx.as_posix()}", '
            '"delivery_status": "ok"}}}`'
        )
        handler = make_handler(output)
        root = PPTGenRootNode()
        inputs = {"topic": "糖尿病防治指南"}

        chunks = [
            chunk
            async for chunk in handler.fallback_stream(
                FallbackCall(
                    node_name="ppt_gen_root",
                    instruction="PPT生成任务流根节点，串联P0-P10全流程",
                    inputs=inputs,
                    error=RuntimeError(INCIDENT_FAILURE),
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
        handler = make_handler(INCIDENT_OUTPUT)
        root = PPTGenRootNode()
        inputs = dict(INCIDENT_INPUTS)

        with pytest.raises(FallbackContractError) as excinfo:
            await handler.fallback(
                FallbackCall(
                    node_name="ppt_gen_root",
                    instruction="PPT生成任务流根节点，串联P0-P10全流程",
                    inputs=inputs,
                    error=RuntimeError(INCIDENT_FAILURE),
                    parent_session=None,
                    result_validator=root.validate_fallback_success,
                )
            )

        assert "PPTX" in str(excinfo.value)
        assert inputs == INCIDENT_INPUTS

    async def test_validator_crash_fails_closed(self):
        """校验器自身异常也必须拒绝（fail-closed），不能放行虚假成功。"""
        handler = make_handler(INCIDENT_OUTPUT)
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
                    error=RuntimeError(INCIDENT_FAILURE),
                    parent_session=None,
                    result_validator=_crashing_validator,
                )
            )

        assert "validator" in str(excinfo.value)

    async def test_validator_abort_error_propagates(self):
        """校验器抛出 AbortError（HITL 中断）必须原样上抛，不得吞成契约失败。"""
        from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError

        handler = make_handler(INCIDENT_OUTPUT)
        inputs = {"topic": "x"}

        def _aborting_validator(
            inputs: dict[str, Any], contract_result: dict[str, Any]
        ) -> str | None:
            raise AbortError("user interrupted")

        with pytest.raises(AbortError):
            await handler.fallback(
                FallbackCall(
                    node_name="ppt_gen_root",
                    instruction="PPT生成任务流根节点，串联P0-P10全流程",
                    inputs=inputs,
                    error=RuntimeError(INCIDENT_FAILURE),
                    parent_session=None,
                    result_validator=_aborting_validator,
                )
            )


class TestFallbackQueryGuards:
    def test_query_states_orchestrator_success_criteria(self):
        query = DeepAgentFallbackHandler._build_fallback_query(
            "ppt_gen_root", "PPT生成任务流根节点，串联P0-P10全流程", {}, RuntimeError("x")
        )
        assert "编排类节点" in query
        assert "终端产物已生成并交付" in query
