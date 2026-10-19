# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""fallback 契约解析与预算守卫（S_07 降级体系，根因"失败信号传播与假成功"）。

防护意图：历史 14 个"假成功"类 PR 的共性是契约判定链脆弱——子代理输出
形态稍变（围栏/位置/多块/嵌套）即解析失败或误判。现有测试只覆盖
"末尾单行内联代码"标准形态（事故原文），本文件补齐 S_07 修改检查单
要求的四级候选全形态 + 预算三态 + handler 缺失分支。

对应历史事故：PR 7341（部分成功虚报全成功）、PR 6405（非流式兜底未
回写，兜底预算耗尽）、问题70。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.server.runtime.skill_turbo.executor import (
    ExecutorConfig,
    FallbackLimitExceededError,
    SkillTurboExecutor,
)
from jiuwenswarm.server.runtime.skill_turbo.fallback_handler import (
    DeepAgentFallbackHandler,
)
from jiuwenswarm.server.runtime.skill_turbo.plan_node import PlanNode

# ───────────────────── _parse_fallback_output 四级候选形态 ─────────────────────


class TestParseFallbackOutput:
    """契约块四种载体形态 × 位置 × 多块 × 嵌套（S_07 边界 6/8/12/14/15）。"""

    def test_json_fenced_contract(self):
        output = "分析：大纲已修复完成。\n```json\n{\"success\": true, \"result\": {\"outline\": \"v2\"}}\n```"
        success, result = DeepAgentFallbackHandler._parse_fallback_output(output)
        assert success is True
        assert result == {"outline": "v2"}

    def test_untagged_fenced_contract(self):
        output = "```\n{\"success\": true, \"result\": {\"k\": 1}}\n```"
        success, result = DeepAgentFallbackHandler._parse_fallback_output(output)
        assert success is True
        assert result == {"k": 1}

    def test_bare_contract_at_start(self):
        output = "{\"success\": false, \"result\": {\"reason\": \"缺交付物\"}}\n后续：请重试。"
        success, result = DeepAgentFallbackHandler._parse_fallback_output(output)
        assert success is False
        assert result == {"reason": "缺交付物"}

    def test_bare_contract_at_middle(self):
        output = "前言说明文字 {\"success\": true, \"result\": {\"mode\": \"rebuild\"}} 结尾补充说明"
        success, result = DeepAgentFallbackHandler._parse_fallback_output(output)
        assert success is True
        assert result == {"mode": "rebuild"}

    def test_multiple_json_blocks_last_wins(self):
        """多块契约从后往前取（契约声明约定在末尾）——前块不得遮蔽后块。"""
        output = (
            "中间汇报 `{\"success\": true, \"result\": {\"stage\": \"p4\"}}`\n"
            "最终结论 `{\"success\": false, \"result\": {\"reason\": \"p10 未交付\"}}`"
        )
        success, result = DeepAgentFallbackHandler._parse_fallback_output(output)
        assert success is False, "末块契约才是最终声明，前块成功不得虚报"
        assert result == {"reason": "p10 未交付"}

    def test_nested_result_preserved(self):
        """嵌套 JSON 不得被非贪婪正则截断（S_07 边界 15）。"""
        output = '`{"success": true, "result": {"nested": {"deep": [1, 2, {"x": "y"}]}}}`'
        success, result = DeepAgentFallbackHandler._parse_fallback_output(output)
        assert success is True
        assert result == {"nested": {"deep": [1, 2, {"x": "y"}]}}

    def test_non_dict_result_wrapped(self):
        """result 非 dict 时包装为 reason（S_07 边界 8）。"""
        output = '`{"success": true, "result": "已完成全部阶段"}`'
        success, result = DeepAgentFallbackHandler._parse_fallback_output(output)
        assert success is True
        assert result == {"reason": "已完成全部阶段"}

    def test_json_without_success_field_fails_closed(self):
        """候选 JSON 缺 success 字段 → fail-closed（S_07 边界 12）。"""
        output = '`{"status": "done", "outline": "v3"}`'
        success, _ = DeepAgentFallbackHandler._parse_fallback_output(output)
        assert success is False

    def test_empty_output_distinct_from_no_contract(self):
        """空产出与"有产出无契约"分支区分（S_07 边界 11）。"""
        success, result = DeepAgentFallbackHandler._parse_fallback_output("")
        assert success is False
        assert "未产出任何内容" in result["reason"]

        success, result = DeepAgentFallbackHandler._parse_fallback_output("子代理只输出了纯文字，没有契约。")
        assert success is False
        assert "未包含 JSON 契约声明" in result["reason"]

    def test_explicit_success_false_with_result(self):
        output = '`{"success": false, "result": {"reason": "大纲修复失败"}}`'
        success, result = DeepAgentFallbackHandler._parse_fallback_output(output)
        assert success is False
        assert result == {"reason": "大纲修复失败"}


# ───────────────────── fallback 预算与前置分支 ─────────────────────


def _make_executor(*, enable_fallback: bool = True, fallback_count: int = 0) -> SkillTurboExecutor:
    env = MagicMock()
    env.config = {}
    env.skill_code_import_prefixes = ("skill_codes",)
    with (
        patch.object(SkillTurboExecutor, "_build_ask_user_rail", return_value=None),
        patch.object(SkillTurboExecutor, "_build_llm_retry_rail", return_value=None),
    ):
        ex = SkillTurboExecutor(env, config=ExecutorConfig(enable_fallback=enable_fallback))
    ex._fallback_count = fallback_count
    return ex


def _node() -> PlanNode:
    class _StageNode(PlanNode):
        def __init__(self) -> None:
            super().__init__(plan_name="p4_outline", instruction="stage", sub_plans=[])

        async def _execute(self, inputs: dict[str, Any]) -> Any:
            return {"node": self.plan_name, "status": "ok"}

    return _StageNode()


class TestCheckFallbackLimit:
    """预算三态（S_07 边界 5 / 不变量 10/12）。"""

    def test_disabled_fallback_reraises_original_error(self):
        ex = _make_executor(enable_fallback=False)
        original = RuntimeError("原始业务错误")
        with pytest.raises(RuntimeError) as excinfo:
            ex._check_fallback_limit(original)
        assert excinfo.value is original, "fallback 关闭时必须 re-raise 原错误，不得制造新异常形态"

    def test_limit_exceeded_preserves_cause_chain(self):
        ex = _make_executor(fallback_count=3)
        original = RuntimeError("第 4 次失败的原始错误")
        with pytest.raises(FallbackLimitExceededError) as excinfo:
            ex._check_fallback_limit(original)
        assert excinfo.value.__cause__ is original, "FLEE 必须以 from error 保留因果链（不变量 12）"

    def test_under_limit_passes(self):
        ex = _make_executor(fallback_count=2)
        ex._check_fallback_limit(RuntimeError("尚有额度"))  # 不应抛出


class TestHandlerMissing:
    """handler None → re-raise 原错误（S_07 边界 5 之一）。"""

    @pytest.mark.asyncio
    async def test_fallback_without_handler_reraises_original(self):
        ex = _make_executor()
        ex._env.fallback_handler = None
        original = RuntimeError("handler 缺失场景的业务错误")
        with pytest.raises(RuntimeError) as excinfo:
            await ex.fallback(_node(), {}, original)
        assert excinfo.value is original

    @pytest.mark.asyncio
    async def test_fallback_stream_without_handler_reraises_original(self):
        ex = _make_executor()
        ex._env.fallback_handler = None
        original = RuntimeError("流式 handler 缺失场景")
        with pytest.raises(RuntimeError) as excinfo:
            async for _ in ex.fallback_stream(_node(), {}, original):
                pass
        assert excinfo.value is original


class TestFallbackCountPerRequest:
    """_fallback_count 请求内递增语义（每请求新建实例重置，不变量 10）。"""

    @pytest.mark.asyncio
    async def test_fallback_entry_records_call_count(self):
        """进入 fallback 入口即计数（供超限判定与 trace 使用）。"""
        ex = _make_executor()
        handler = MagicMock()
        handler.fallback = AsyncMock(return_value={"node": "p4", "status": "completed", "result": {}})
        ex._env.fallback_handler = handler

        await ex.fallback(_node(), {}, RuntimeError("第一次失败"))
        assert ex._fallback_count == 1

        await ex.fallback(_node(), {}, RuntimeError("第二次失败"))
        assert ex._fallback_count == 2
