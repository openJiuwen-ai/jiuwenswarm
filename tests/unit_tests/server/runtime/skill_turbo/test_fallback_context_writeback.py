# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""节点兜底结果必须回写共享计划上下文。

事故背景（问题70，2026-09-08）：pptx-craft P2.4 节点 LLM 输出非有效 JSON，
非流式 fallback 兜底成功且 contract passed，但契约结果未回写共享 inputs，
下游 P4.1/P4.3 连续读到空 search_mode 失败，3 次兜底预算耗尽后
FallbackLimitExceededError 导致 stage6 规划执行终态失败。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.server.runtime.skill_turbo.fallback_handler import (
    DeepAgentFallbackHandler,
    FallbackContractError,
)
from jiuwenswarm.server.runtime.skill_turbo.plan_node import PlanNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.requirement_collect import (
    RequirementCollectError,
)

# 复刻事故中兜底子代理的真实输出形态：分析正文 + 末尾单行 JSON 契约
_CONTRACT_OUTPUT = (
    "我正在替代失败的 `p2_4_derive_params` 节点，根据已有输入推断三项派生参数。\n"
    "\n"
    "1. **search_mode** → `force_search`（用户要求找到高考题及解答，需检索真实素材）\n"
    "2. **source_type** → `topic`（无上传文档）\n"
    "3. **research_depth** → `L3`（越详细越好，30页以上）\n"
    "\n"
    '`{"success": true, "result": {"search_mode": "force_search", '
    '"source_type": "topic", "research_depth": "L3"}}`'
)

_FAILURE = "派生参数解析失败：LLM 未返回有效 JSON"


def _make_handler(contract_output: str) -> DeepAgentFallbackHandler:
    adapter = MagicMock()
    adapter.spawn_fallback = AsyncMock(return_value=contract_output)
    return DeepAgentFallbackHandler(
        adapter, request_id="req-ut", channel_id="officeclaw", session_id="sess-ut"
    )


class _FailingDeriveParamsNode(PlanNode):
    """复刻事故节点：p2_4_derive_params 原生执行抛 RequirementCollectError。"""

    def __init__(self) -> None:
        super().__init__(
            plan_name="p2_4_derive_params",
            instruction="## P2.4 派生参数推断",
            sub_plans=[],
            depth=2,
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        raise RequirementCollectError(_FAILURE)


class TestNonStreamFallbackWritesBackContext:
    @pytest.mark.asyncio
    async def test_nonstream_fallback_merges_contract_into_inputs(self):
        handler = _make_handler(_CONTRACT_OUTPUT)
        inputs = {"topic": "数学知识点", "search_mode": "", "page_count": 28}

        result = await handler.fallback(
            node_name="p2_4_derive_params",
            instruction="## P2.4 派生参数推断",
            inputs=inputs,
            error=RequirementCollectError(_FAILURE),
            parent_session=None,
        )

        # 返回值语义保持：契约字段完整（execute_plan 根路径消费）
        assert result["search_mode"] == "force_search"
        assert result["source_type"] == "topic"
        assert result["research_depth"] == "L3"
        # 关键回归断言：共享上下文必须被回写（事故根因：下游读到空 search_mode）
        assert inputs["search_mode"] == "force_search"
        assert inputs["source_type"] == "topic"
        assert inputs["research_depth"] == "L3"
        # 原有键不被破坏；协议键 node/status 不进共享上下文（与流式实现对齐）
        assert inputs["topic"] == "数学知识点"
        assert inputs["page_count"] == 28
        assert inputs["fallback"] is True
        assert "node" not in inputs
        assert "status" not in inputs

    @pytest.mark.asyncio
    async def test_plan_node_run_fallback_visible_to_parent_inputs(self):
        handler = _make_handler(_CONTRACT_OUTPUT)
        node = _FailingDeriveParamsNode()

        async def executor_fallback_cb(
            failing_node: PlanNode, inputs_cb: dict[str, Any], err: Exception
        ) -> Any:
            # 模拟 SkillTurboExecutor.fallback：把同一 inputs 引用透传给 handler
            return await handler.fallback(
                node_name=failing_node.plan_name,
                instruction=failing_node.instruction or "",
                inputs=inputs_cb,
                error=err,
                parent_session=None,
            )

        node.set_runtime_callbacks(fallback=executor_fallback_cb)

        # 复刻事故：父节点（RequirementCollectNode）丢弃 execute_subplan 返回值
        inputs = {"topic": "数学知识点", "search_mode": "", "page_count": 28}
        discarded = await node.run(inputs)

        assert discarded["status"] == "completed"
        # 下游节点（P4.1/P4.3）读取的共享上下文必须已含兜底参数
        assert inputs["search_mode"] == "force_search"
        assert inputs["research_depth"] == "L3"


class TestFallbackRegressionGuards:
    @pytest.mark.asyncio
    async def test_fallback_stream_still_merges_into_inputs(self):
        handler = _make_handler(_CONTRACT_OUTPUT)
        inputs = {"topic": "数学知识点", "search_mode": ""}

        chunks = [
            chunk
            async for chunk in handler.fallback_stream(
                node_name="p2_4_derive_params",
                instruction="## P2.4 派生参数推断",
                inputs=inputs,
                error=RequirementCollectError(_FAILURE),
                parent_session=None,
            )
        ]

        event_types = [chunk.get("event_type") for chunk in chunks]
        assert "fallback.started" in event_types
        assert "fallback.finished" in event_types
        assert inputs["search_mode"] == "force_search"
        assert inputs["research_depth"] == "L3"

    @pytest.mark.asyncio
    async def test_contract_failure_does_not_pollute_inputs(self):
        handler = _make_handler("任务未完成，缺少必要产出，无法自证达成节点契约。")
        inputs = {"topic": "数学知识点", "search_mode": ""}

        with pytest.raises(FallbackContractError):
            await handler.fallback(
                node_name="p2_4_derive_params",
                instruction="## P2.4 派生参数推断",
                inputs=inputs,
                error=RequirementCollectError(_FAILURE),
                parent_session=None,
            )

        assert inputs == {"topic": "数学知识点", "search_mode": ""}
