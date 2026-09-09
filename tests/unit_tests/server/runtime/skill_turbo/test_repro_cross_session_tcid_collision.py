# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""两个独立 session 运行同一 skill 时 ask_user tool_call_id 必须跨会话唯一。

回放 officeclaw_1371562656b1933e94b6d785 卡死根因：_next_tool_call_id 仅由
``(tool_name, canonical_args_hash, call_index)`` 决定，不含 session 维度。
当两个会话并行运行同一 pptx-craft 流水线、第二次 ask_user（风格选择）的
questions/Options 完全相同时，args_hash 与 idx 都相同，于是两个会话对各自的
第二次 ask_user 中断发出完全相同的 ``skill_turbo-tc-ask_user-<hash>-0`` 卡片
request_id。前端按 request_id 路由作答，先到的会话卡片占住该 id，后到会话
的卡片被覆盖/合并；用户在第二个卡片上作答后，答案被发到先到的会话，
后到会话永远等不到 resume_ctx 命中 → 卡死。

修复：``_next_tool_call_id`` 将当前 request_id（``_request_id_var``）混入
args_hash，使不同请求对相同 ask_user 参数生成不同 id。resume 重放时 request_id
变化导致精确匹配失效，由 ``_consume_pending_resume_input`` 的 idx 回退命中
（与「重放时非确定性 ask_user 参数」同一既有路径）兜底。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from jiuwenswarm.server.runtime.skill_turbo.executor import (
    SkillTurboExecutor,
    _request_id_var,
)

_STYLE_ASK_USER_KWARGS = {
    "questions": [
        {
            "header": "风格",
            "question": "请选择演示文稿的视觉风格",
            "multi_select": False,
            "options": [
                {"label": "商务经典", "description": "企业汇报、红色主题、严谨专业"},
                {"label": "科技极简", "description": "产品发布、黑白调性、极简设计"},
                {"label": "典雅叙事", "description": "文化主题、温暖质感、有机插图"},
                {"label": "工业科技", "description": "硬核场景、高对比度、工业科技感"},
                {"label": "自由发挥", "description": "由 AI 根据主题自动设计"},
            ],
        }
    ]
}


def _make_executor() -> SkillTurboExecutor:
    env = MagicMock()
    env.config = {}
    env.skill_code_import_prefixes = (
        "jiuwenswarm.server.runtime.skill_turbo.skill_codes",
    )
    return SkillTurboExecutor(environment=env)


def test_same_ask_user_args_produce_distinct_tool_call_id_across_executors() -> None:
    ex_a = _make_executor()
    ex_b = _make_executor()

    tok_a = _request_id_var.set("req-a")
    try:
        tcid_a = ex_a._next_tool_call_id("ask_user", _STYLE_ASK_USER_KWARGS)
    finally:
        _request_id_var.reset(tok_a)
    tok_b = _request_id_var.set("req-b")
    try:
        tcid_b = ex_b._next_tool_call_id("ask_user", _STYLE_ASK_USER_KWARGS)
    finally:
        _request_id_var.reset(tok_b)

    assert tcid_a != tcid_b, (
        "跨会话 ask_user tool_call_id 碰撞：两个 executor 对相同 questions "
        "生成了相同的 id，前端按 request_id 路由作答时会串台。"
    )


def test_second_ask_user_also_distinct_across_executors() -> None:
    ex_a = _make_executor()
    ex_b = _make_executor()

    tok_a = _request_id_var.set("req-a")
    try:
        ex_a._next_tool_call_id("ask_user", {"questions": [{"header": "受众", "question": "目标受众是谁？", "options": []}]})
        tcid_a2 = ex_a._next_tool_call_id("ask_user", _STYLE_ASK_USER_KWARGS)
    finally:
        _request_id_var.reset(tok_a)
    tok_b = _request_id_var.set("req-b")
    try:
        ex_b._next_tool_call_id("ask_user", {"questions": [{"header": "受众", "question": "目标受众是谁？", "options": []}]})
        tcid_b2 = ex_b._next_tool_call_id("ask_user", _STYLE_ASK_USER_KWARGS)
    finally:
        _request_id_var.reset(tok_b)

    assert tcid_a2 != tcid_b2


def test_same_request_id_still_deterministic_within_request() -> None:
    """同一请求内重放同名同参调用：request_id 不变 → idx 递增区分，不串台。"""
    ex = _make_executor()

    tok = _request_id_var.set("req-same")
    try:
        first = ex._next_tool_call_id("ask_user", _STYLE_ASK_USER_KWARGS)
        second = ex._next_tool_call_id("ask_user", _STYLE_ASK_USER_KWARGS)
    finally:
        _request_id_var.reset(tok)

    assert first != second
