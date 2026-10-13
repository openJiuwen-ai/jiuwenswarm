# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""skill_acceleration_exec 入口拦截与收尾诚实性（S_02 边界 9 / S_07 收尾三分支）。

防护意图：
- 两块入口拦截（[TEMP-TEMPLATE-BYPASS] / [REGION-EDIT-BYPASS]）此前无任何
  单测（S_02 边界 9）。拦截失效的后果是"从 Stage 1 全量重跑丢失原 PPT"
  （历史线上事故形态）。
- workspace_base"只认 output_dir、禁止 agent workspace 兜底"是 PR 5036→5133
  反复（加入兜底→次日删除）的直接防线，无回归测试会再次反复。
- _wrap_skill_turbo_result 失败分支"不追加停止提示"是收尾诚实性契约。
"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.server.runtime.skill_turbo import skill_turbo_tools
from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
    _wrap_skill_turbo_result,
    skill_turbo,
)


class TestTemplateBypassGate:
    @pytest.mark.asyncio
    async def test_template_directory_with_path_intercepted(self):
        """query 携带"模板目录 + 绝对路径"→ 拦截并引导走 skill_tool。"""
        result = await skill_turbo.invoke({"query": "使用 模板目录：D:\\templates\\pack1 生成 PPT"})
        assert isinstance(result, dict)
        assert result["success"] is False
        assert "模板" in result["error"]

    @pytest.mark.asyncio
    async def test_template_field_keyword_intercepted(self):
        """固定字段名（pack_dir 等）→ 拦截（前端注入形态）。"""
        result = await skill_turbo.invoke({"query": "pack_dir 指向用户上传的模板包"})
        assert isinstance(result, dict)
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_template_mention_without_path_not_intercepted(self):
        """仅提"模板"但不带路径 → 不拦截（正常进入后续流程，表现为未初始化）。"""
        result = await skill_turbo.invoke({"query": "请用商务风格模板生成 10 页 PPT"})
        assert isinstance(result, dict)
        assert "未初始化" in str(result.get("error", "")), "无模板路径的请求不得被拦截"


class TestRegionEditBypassGate:
    @pytest.mark.asyncio
    async def test_region_edit_keyword_intercepted(self):
        """选区/编辑类关键词 → 拦截（防 Stage 1 全量重跑丢失原 PPT）。"""
        result = await skill_turbo.invoke({"query": "修改 PPT选区 原文中的标题字体为加粗"})
        assert isinstance(result, dict)
        assert result["success"] is False
        assert "选区" in result["error"] or "编辑" in result["error"]

    @pytest.mark.asyncio
    async def test_layout_optimization_keyword_intercepted(self):
        """布局优化（REGION_EDIT_KEYWORDS 成员）→ 拦截。"""
        result = await skill_turbo.invoke({"query": "对选中页做布局优化"})
        assert isinstance(result, dict)
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_fresh_generation_not_intercepted(self):
        """全新生成请求（无选区词）→ 不拦截。"""
        result = await skill_turbo.invoke({"query": "生成一份关于新能源汽车的 8 页 PPT"})
        assert "未初始化" in str(result.get("error", "")), "全新生成请求不得被拦截"


class TestWrapResultHonesty:
    def test_failure_keeps_error_and_no_stop_hint(self):
        """失败分支：error 原样保留、不追加停止提示（失败须回退 skill_tool，不得引导结束轮次）。"""
        result = _wrap_skill_turbo_result({"success": False, "error": "PPT 生成失败：导出异常"})
        assert result["success"] is False
        assert "PPT 生成失败" in result["error"]
        assert "finish" not in result["error"].lower()

    def test_failure_clears_pending_delivery_summary(self):
        """失败分支必须清 pending 交付骨架（防失败后仍发成功骨架）。"""
        skill_turbo_tools.set_pending_ppt_delivery_summary("# 交付总结（测试）")
        try:
            result = _wrap_skill_turbo_result({"success": False, "error": "失败"})
            assert result["success"] is False
            # take 同时消费 ContextVar：断言失败也不会把 pending 泄漏给后续用例
            assert skill_turbo_tools.take_pending_ppt_delivery_summary() == "", (
                "失败分支未清除 pending 交付骨架——后续轮次会误发成功骨架"
            )
        finally:
            skill_turbo_tools.clear_pending_ppt_delivery_summary()

    def test_success_with_confirmed_delivery_uses_confirmed_hint(self):
        """通用交付确认（send_file_status=sent）→ 用"已确认"停止提示（case_3 docx D8 修复）。"""
        holder: dict[str, Any] = {
            "deliveries": [
                {"tool": "send_file", "file_name": "report.docx", "status": "sent"}
            ]
        }
        result = _wrap_skill_turbo_result({"success": True, "result": "文档已生成"}, artifact_holder=holder)
        assert result["success"] is True
        assert "文档已生成" in result["result"]
        # 已确认收尾：不再出现"未确认"中立措辞
        assert "未确认" not in result["result"]

    def test_success_without_delivery_signal_keeps_neutral(self):
        """无骨架且无确认信号 → 中立短句（不宣称文件已发送）。"""
        result = _wrap_skill_turbo_result({"success": True, "result": "处理完成"}, artifact_holder={})
        assert "未能确认文件已生成并发送" in result["result"]
        assert "不要声称文件已发送" in result["result"]
