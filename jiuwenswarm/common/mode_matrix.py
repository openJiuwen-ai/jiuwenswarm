# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""统一的运行模式解析（Web 组合模式 + 历史完整模式）。

背景：仓库里长期存在两个正交概念。

- ``work_mode``：``work`` / ``code``，表示工作环境（项目归属、Git 上下文）。
- ``mode``：决定 Adapter（Deep / Code）、单 agent 还是集群、以及是否 plan。

Web 前端只表达 ``agent`` / ``team`` / ``agent.plan`` 三个值。其中 ``work_mode``
决定使用 work profile 还是 code profile，本模块负责把这两个字段组合成后端既有的
manager mode / sub_mode / canonical mode。Web 的 ``team + code`` 组合归一为
``code.team``（TUI 对外显示为 ``team.code``）。

TUI、CLI、IM、cron 等客户端可以直接发送完整模式串。本模块同时负责将正式别名
``team.plan`` 归一为 ``team.plan.normal``，并把两个 Team Plan profile 分别路由到
DeepAdapter（normal）和 CodeAdapter（code）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from jiuwenswarm.common.work_mode import SUPPORTED_WORK_MODES

# Web 组合模式覆盖单 agent 和 Team 的 work/code profile。其余取值一律按历史完整
# 模式处理。Team 不支持独立的 Web Plan，但 ``team + code`` 必须进入与 TUI
# ``team.code`` 相同的 code-team profile。
WEB_BASE_AGENT: str = "agent"
WEB_PLAN_AGENT: str = "agent.plan"

WEB_COMPOSABLE_MODES: frozenset[str] = frozenset(
    {WEB_BASE_AGENT, WEB_PLAN_AGENT, "team"}
)

# Team plan 的规范模式与兼容别名。
TEAM_PLAN_NORMAL_MODE: str = "team.plan.normal"
TEAM_PLAN_CODE_MODE: str = "team.plan.code"
MODE_ALIASES: dict[str, str] = {
    "team.plan": TEAM_PLAN_NORMAL_MODE,
    # TUI 的用户-facing 名称；运行时沿用历史 canonical ID。
    "team.code": "code.team",
}

# 所有表示"集群"的 canonical 模式。
TEAM_CANONICAL_MODES: frozenset[str] = frozenset(
    {"team", TEAM_PLAN_NORMAL_MODE, "code.team", TEAM_PLAN_CODE_MODE}
)

# 所有表示"正处于 plan"的 canonical 模式。
PLAN_CANONICAL_MODES: frozenset[str] = frozenset(
    {"agent.plan", "code.plan", TEAM_PLAN_NORMAL_MODE, TEAM_PLAN_CODE_MODE}
)

# canonical plan 模式退出 plan 后应回到的普通模式。
_PLAN_EXIT_MODES: dict[str, str] = {
    "agent.plan": "agent",
    "code.plan": "code.normal",
    TEAM_PLAN_NORMAL_MODE: "team",
    TEAM_PLAN_CODE_MODE: "code.team",
}

# (mode, work_mode) -> (manager_mode, sub_mode, canonical_mode)
_WEB_MODE_TABLE: dict[tuple[str, str], tuple[str, str | None, str]] = {
    (WEB_BASE_AGENT, "work"): ("agent", None, "agent"),
    (WEB_PLAN_AGENT, "work"): ("agent", "plan", "agent.plan"),
    (WEB_BASE_AGENT, "code"): ("code", "normal", "code.normal"),
    (WEB_PLAN_AGENT, "code"): ("code", "plan", "code.plan"),
    ("team", "work"): ("team", None, "team"),
    ("team", "code"): ("code", "team", "code.team"),
}


@dataclass(frozen=True)
class ResolvedMode:
    """一次请求解析出的完整运行模式视图。

    Attributes:
        manager_mode: AgentManager / adapter 选型用的一级模式。
        sub_mode: 子模式（``normal`` / ``plan`` / ``team`` / None）。
        canonical_mode: 写回 ``params["mode"]`` 的规范值。
        work_mode: 归一化后的 ``work`` / ``code``；历史请求为 None。
        is_plan: 本次请求是否要求处于 plan。
        is_team: 本次请求是否集群模式。
        is_code_profile: 是否使用 CodeAdapter / code 团队 profile。
        profile: 本次请求使用的 profile（``normal`` / ``code``）。
        normal_mode: 退出 plan 后应回到的 canonical 模式。
        from_web_composition: 是否由 Web 的 mode + work_mode 组合而来。
    """

    manager_mode: str
    sub_mode: str | None
    canonical_mode: str
    work_mode: str | None
    is_plan: bool
    is_team: bool
    is_code_profile: bool
    profile: str
    normal_mode: str
    from_web_composition: bool


def normalize_mode_text(raw_mode: Any) -> str:
    """把请求里的 mode 归一成小写字符串，空值回落到 ``agent``。"""
    raw_value = getattr(raw_mode, "value", raw_mode)
    text = raw_value.strip().lower() if isinstance(raw_value, str) else ""
    return text or "agent"


def canonicalize_mode_text(raw_mode: Any) -> str:
    """归一化 mode 文本并解析正式别名。"""
    text = normalize_mode_text(raw_mode)
    return MODE_ALIASES.get(text, text)


def normalize_work_mode(raw: Any) -> str | None:
    """把任意来源的 ``work_mode`` 归一成 ``work`` / ``code``；非法时返回 None。"""
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    return value if value in SUPPORTED_WORK_MODES else None


def read_request_work_mode(params: Mapping[str, Any] | None) -> str | None:
    """读取请求中显式携带的 ``work_mode``；缺失或非法时返回 None。

    只有 Web 会携带该字段，因此它同时充当"这是 Web 组合模式请求"的判据。
    TUI / CLI / IM / cron 都不发送 ``work_mode``，会走历史解析分支。
    """
    if not isinstance(params, Mapping):
        return None
    return normalize_work_mode(params.get("work_mode"))


def is_web_composable_mode(mode_text: str) -> bool:
    """给定 mode 是否参与 Web 的 mode + work_mode 组合。"""
    return mode_text in WEB_COMPOSABLE_MODES


def is_plan_mode(canonical_mode: Any) -> bool:
    """canonical 模式是否处于 plan。"""
    return canonicalize_mode_text(canonical_mode) in PLAN_CANONICAL_MODES


def is_team_mode(canonical_mode: Any) -> bool:
    """canonical 模式是否为集群。"""
    return canonicalize_mode_text(canonical_mode) in TEAM_CANONICAL_MODES


def is_team_plan_mode(mode: Any) -> bool:
    """Return whether *mode* is either normal or code Team Plan."""
    return canonicalize_mode_text(mode) in {
        TEAM_PLAN_NORMAL_MODE,
        TEAM_PLAN_CODE_MODE,
    }


def is_code_profile_mode(mode: Any) -> bool:
    """Return whether *mode* selects the code profile."""
    return canonicalize_mode_text(mode) in {
        "code.normal",
        "code.plan",
        "code.team",
        TEAM_PLAN_CODE_MODE,
    }


def base_mode_without_plan(canonical_mode: Any) -> str:
    """canonical plan 模式退出后应回到的普通模式。非 plan 模式原样返回。"""
    text = canonicalize_mode_text(canonical_mode)
    return _PLAN_EXIT_MODES.get(text, text)


def compose_web_mode(mode_text: str, work_mode: str) -> tuple[str, str | None, str] | None:
    """把 Web 的 ``mode`` + ``work_mode`` 组合成后端三元组。

    Args:
        mode_text: 归一化后的 Web mode（agent / agent.plan）。
        work_mode: 归一化后的 work / code。

    Returns:
        ``(manager_mode, sub_mode, canonical_mode)``；不是 Web 组合时返回 None。
    """
    return _WEB_MODE_TABLE.get((mode_text, work_mode))


def resolve_request_mode(
    params: Mapping[str, Any] | None,
    legacy_resolver: Callable[..., tuple[str, str | None, str]],
    *,
    work_mode: Any = None,
) -> ResolvedMode:
    """解析一次请求的运行模式。

    优先走 Web 组合分支；只有当请求同时满足"存在合法 ``work_mode``"和
    "mode 是 Web 支持的基础模式"时才生效。其余取值（``code.plan`` / ``code.team`` /
    ``team.plan.*`` / ``agent.fast`` 等完整模式串）都交给 *legacy_resolver*。

    Args:
        params: 请求 params。
        legacy_resolver: 历史解析函数（``resolve_agent_request_mode``）。
        work_mode: 调用方已解析出的 ``work_mode``（如来自 session metadata），
            优先于 ``params["work_mode"]``；同时透传给 *legacy_resolver*。

    Returns:
        解析后的 :class:`ResolvedMode`。
    """
    raw_mode = params.get("mode") if isinstance(params, Mapping) else None
    mode_text = canonicalize_mode_text(raw_mode)
    work_mode = normalize_work_mode(work_mode) or read_request_work_mode(params)

    if work_mode is not None and is_web_composable_mode(mode_text):
        composed = compose_web_mode(mode_text, work_mode)
        if composed is not None:
            manager_mode, sub_mode, canonical_mode = composed
            return ResolvedMode(
                manager_mode=manager_mode,
                sub_mode=sub_mode,
                canonical_mode=canonical_mode,
                work_mode=work_mode,
                is_plan=canonical_mode in PLAN_CANONICAL_MODES,
                is_team=canonical_mode in TEAM_CANONICAL_MODES,
                is_code_profile=work_mode == "code",
                profile="code" if work_mode == "code" else "normal",
                normal_mode=base_mode_without_plan(canonical_mode),
                from_web_composition=True,
            )

    manager_mode, sub_mode, canonical_mode = legacy_resolver(
        mode_text, work_mode=work_mode
    )
    return ResolvedMode(
        manager_mode=manager_mode,
        sub_mode=sub_mode,
        canonical_mode=canonical_mode,
        work_mode=work_mode,
        is_plan=canonical_mode in PLAN_CANONICAL_MODES,
        is_team=canonical_mode in TEAM_CANONICAL_MODES,
        is_code_profile=manager_mode == "code",
        profile="code" if manager_mode == "code" else "normal",
        normal_mode=base_mode_without_plan(canonical_mode),
        from_web_composition=False,
    )


__all__ = [
    "PLAN_CANONICAL_MODES",
    "MODE_ALIASES",
    "ResolvedMode",
    "TEAM_PLAN_CODE_MODE",
    "TEAM_PLAN_NORMAL_MODE",
    "TEAM_CANONICAL_MODES",
    "WEB_COMPOSABLE_MODES",
    "base_mode_without_plan",
    "canonicalize_mode_text",
    "compose_web_mode",
    "is_plan_mode",
    "is_code_profile_mode",
    "is_team_plan_mode",
    "is_team_mode",
    "is_web_composable_mode",
    "normalize_mode_text",
    "normalize_work_mode",
    "read_request_work_mode",
    "resolve_request_mode",
]
