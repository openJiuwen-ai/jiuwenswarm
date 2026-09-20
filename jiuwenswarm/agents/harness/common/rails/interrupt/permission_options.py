# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""权限/确认审批选项的统一词表。

审批提示的选项文案同时充当协议取值：渲染端拿到 ``label``/``value``，回答时
web、CLI 回传 ``option.value || option.label``，TUI 一律回传 ``option.label``，
后端再把这个字符串解回 ``approved`` / ``auto_confirm`` / ``persist_allow``。
出题端与解题端因此必须共用一份词表，否则任何一端改动文案都会让回答落到
"未知选项"，而未知选项是按拒绝处理的——用户点了同意，工具却被拒。

本模块只放稳定标识和它们的别名，不放任何展示文案：展示文案归出题端所有、可以
改，而这里的取值不随之变化。除标准库外不依赖任何东西，出题端与解题端都能引。
"""

from __future__ import annotations

import re

# ── 稳定标识 ────────────────────────────────────────────────────────────────
# 四个动作的规范取值。出题端把它们写进选项的 ``value``，解题端解回同一个动作。
ALLOW_ONCE = "allow_once"
SESSION_ALLOW = "session_allow"
ALWAYS_ALLOW = "always_allow"
REJECT = "reject"

# 归一化：首尾空白、大小写、以及空格/下划线/连字符的混用都不应影响解析。
# ``Allow Once``、``allow_once``、``Allow-once`` 是同一个取值的不同写法，历史上
# 只有部分写法被接受。CJK 文案没有词间分隔，归一化对其无影响。
_SEPARATORS = re.compile(r"[\s_\-]+")


def normalize_option_value(value: object) -> str:
    """把回传的选项字符串归一化成可比较的形式。"""
    text = str(value or "").strip()
    if not text:
        return ""
    return _SEPARATORS.sub("_", text).casefold()


# ── 别名 → 动作 ─────────────────────────────────────────────────────────────
# 只收录各出题端确实下发过的写法。这段映射与计划审批共用同一条 if/elif，因此
# 不要加入 "skip"/"跳过"/"ok" 这类通用词：别的确认流出现同名按钮会被误判。
_ALIAS_SOURCES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        ALLOW_ONCE,
        ("allow_once", "approve", "accept", "proceed", "本次允许", "批准", "开始执行", "接收", "接受"),
    ),
    (SESSION_ALLOW, ("session_allow", "会话内记住")),
    (ALWAYS_ALLOW, ("always_allow", "allow_always", "永久记住", "总是允许")),
    (
        # ``reject_once`` 是 ACP 的写法（``reject-once``）。ACP 走的是
        # ``session/request_permission``，正常不会回到这里，收下只是兜底。
        REJECT,
        ("reject", "reject_once", "拒绝", "继续规划", "其他意见", "keep_planning"),
    ),
)

_OPTION_ALIASES: dict[str, str] = {
    normalize_option_value(alias): action
    for action, aliases in _ALIAS_SOURCES
    for alias in aliases
}

# 归一化后仍表示"继续规划"而非"拒绝"的取值，用于挑选写给模型的反馈文案。
_KEEP_PLANNING_VALUES: frozenset[str] = frozenset(
    normalize_option_value(alias) for alias in ("keep_planning", "继续规划", "其他意见")
)


def resolve_permission_action(value: object) -> str | None:
    """解出选项对应的动作，无法识别时返回 ``None``。

    返回 ``None`` 表示"没认出来"，与"用户拒绝"是两回事：调用方应当记录告警后
    再按拒绝兜底，这样一次解析失败是可诊断的，而不是静默变成一次拒绝。
    """
    return _OPTION_ALIASES.get(normalize_option_value(value))


def is_keep_planning_value(value: object) -> bool:
    """判断拒绝类取值是否属于"继续规划"。"""
    return normalize_option_value(value) in _KEEP_PLANNING_VALUES


__all__ = [
    "ALLOW_ONCE",
    "ALWAYS_ALLOW",
    "REJECT",
    "SESSION_ALLOW",
    "is_keep_planning_value",
    "normalize_option_value",
    "resolve_permission_action",
]
