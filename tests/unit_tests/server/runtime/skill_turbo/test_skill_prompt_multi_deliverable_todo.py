# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""多交付物待办清单强制引导——指南文本契约测试（F_03）。

单轮多交付物（多次 skill_acceleration_exec）必须先 todo_create 建外层待办清单、
逐项执行逐项 todo_modify 标记、全部完成后统一总结。锁三类契约：

1. 新引导要素齐备（中英双语）：例外链挂入「同样例外」、强制段五要素
   （todo_create 建清单 / 每份交付物一项 / action=update 状态流转 /
   不中途最终总结 / 一次性统一总结）；
2. 串行契约（B_03 竞态收敛）：todo_create / todo_modify 等返回成功后再
   进入加速通道，严禁同批并行调用——同轮并行会让入口 outer_todo 检测
   竞态误判；
3. 既有措辞契约不回退：成功段"先单独调 todo_modify 标 completed"与
   todo_close_nudge / _PPT_DELIVERY_SUMMARY_*_HINT 对齐（另一处断言在
   tests/unit_tests/agentserver/rails/test_task_execution_todo_close_nudge.py），
   且"标 completed 后无条件立即总结"的旧措辞不得复活。
"""

from jiuwenswarm.server.runtime.skill_turbo.rails.skill_prompt_rail import (
    _build_skill_turbo_guide_text,
)


def test_cn_guide_mandates_todo_list_before_multi_deliverable_runs() -> None:
    cn = _build_skill_turbo_guide_text("cn", ["pptx-craft"])

    # 段落 1 例外链挂入"同样例外"——避免与"第一个工具调用必须是"矛盾
    assert "同样例外" in cn
    assert "todo_create" in cn
    assert "多份交付物" in cn

    # 强制段：标题 + 五要素
    assert "📋 **多交付物待办清单（强制）**" in cn
    assert "每份交付物一项" in cn
    assert "无外层待办" in cn and "禁止" in cn
    assert "第一项自动为 `in_progress`" in cn
    assert "action=update" in cn
    assert "不要中途向用户做最终总结" in cn
    assert "一次性统一总结" in cn


def test_en_guide_mandates_todo_list_before_multi_deliverable_runs() -> None:
    en = _build_skill_turbo_guide_text("en", ["pptx-craft"])

    assert "Also excepted" in en
    assert "todo_create" in en
    assert "multiple deliverables" in en

    assert "📋 **Multiple-deliverable todo list (mandatory)**" in en
    assert "one item per deliverable" in en
    assert "outer todo list" in en
    assert "automatically becomes `in_progress`" in en
    assert "action=update" in en
    assert "do NOT give the user a final summary midway" in en
    assert "one unified summary" in en


def test_success_paragraph_keeps_standalone_todo_modify_wording() -> None:
    """成功段保留"先单独调 todo_modify"措辞（todo_close_nudge 契约），
    多交付物场景只改尾部流向：未清空前继续下一项，全部完成后才总结。"""
    cn = _build_skill_turbo_guide_text("cn", ["pptx-craft"])
    en = _build_skill_turbo_guide_text("en", ["pptx-craft"])

    assert "先单独调 `todo_modify`" in cn
    assert "first call `todo_modify` alone" in en

    # 旧措辞（标 completed 后无条件立即总结）不得复活
    assert "`completed`，再向用户总结结果" not in cn
    assert "completed`, then summarize the result to the user" not in en

    # 新流向：清单未清空 → 继续下一项
    assert "全部完成后再向用户总结结果" in cn
    assert "only summarize to the user after all items are completed" in en


def test_guide_mandates_sequential_todo_before_acceleration() -> None:
    """串行契约（B_03 竞态收敛）：todo_create / todo_modify 必须等返回成功后
    再进入加速通道，严禁同批并行——同轮并行会让入口 outer_todo 检测竞态误判
    （rail 的 _ACTIVE_TASK_ID 尚未设置），turbo Stage 快照顶掉外层 todo 槽位
    且续跑入口不再补发终态（实证 case 见 B_03）。"""
    cn = _build_skill_turbo_guide_text("cn", ["pptx-craft"])
    en = _build_skill_turbo_guide_text("en", ["pptx-craft"])

    # 强制段：串行总则 + 两处"等返回成功"
    assert "严禁同批并行调用" in cn
    assert "等 `todo_create` 返回成功后" in cn
    assert "等 `todo_modify` 返回成功后" in cn
    assert "NEVER batch these calls in parallel" in en
    assert "after `todo_create` returns success" in en
    assert "after `todo_modify` returns success" in en

    # 段落 1 例外链同步收紧（模型先读到的是段落 1，不能只靠强制段兜底）
    assert "不要把 `todo_create` 和 `skill_acceleration_exec` 放在同一批并行调用" in cn
    assert (
        "do NOT batch `todo_create` and `skill_acceleration_exec` "
        "as parallel calls" in en
    )


def test_guide_bilingual_paragraph_structure_aligned() -> None:
    """中英指南结构对齐：标题 + 6 个正文段（默认优先 / 误触 / 成功 /
    多交付物清单 / 暂停 / 选区排除），新增段落在两种语言中位置一致。"""
    cn = _build_skill_turbo_guide_text("cn", ["pptx-craft"])
    en = _build_skill_turbo_guide_text("en", ["pptx-craft"])

    for text, markers in (
        (cn, ["⚠️", "若你已先调用", "产物已生成", "📋 **多交付物待办清单", "⏸️", "🚫"]),
        (en, ["⚠️", "mistakenly called", "artifact is already generated", "📋 **Multiple-deliverable", "⏸️", "🚫"]),
    ):
        positions = [text.index(m) for m in markers]
        assert positions == sorted(positions), f"段落顺序异常: {markers}"
