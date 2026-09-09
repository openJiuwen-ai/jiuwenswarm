# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""WorkModeProfile 注册表：work / code / design 三模式的单源声明。

work_mode（工作环境）与 mode（adapter 选型 / 单 agent / 集群 / plan）
是两个正交维度。

本模块把三模式的 canonical 映射、adapter 选型、团队成员 profile、
默认项目桶、专家策略收敛为一张注册表：

- 装团收敛（``expert_service._load_expert_team``）查 :func:`team_canonical_for_work_mode`；
- 退团恢复（``_unload_expert_team``）查 :func:`single_canonical_for_work_mode`；
- 团队成员 profile 路由（``config_specs._is_code_mode/_is_design_mode``）查
  :func:`member_profile_for_canonical`；
- 集群判定（``mode_matrix.TEAM_CANONICAL_MODES``）由 :func:`team_canonical_modes`
  派生。

新增第 4 种 work_mode = 在 ``WORK_MODE_PROFILES`` 加一行 + 各消费点自动跟随。
与 ``common/work_mode.py`` 的关系：work_mode.py 是底层常量/归一化层
（不 import 本模块，避免循环），本模块引用其常量组装 profile。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jiuwenswarm.common.work_mode import (
    DEFAULT_DESIGN_WORK_MODE,
    DEFAULT_PROJECT_ID_CODE,
    DEFAULT_PROJECT_ID_DESIGN,
    DEFAULT_PROJECT_ID_WORK,
    DEFAULT_TUI_WORK_MODE,
    DEFAULT_WEB_WORK_MODE,
    normalize_work_mode,
)


@dataclass(frozen=True)
class ExpertPolicy:
    """单专家在某 work_mode 下的装配策略差异项。

    Attributes:
        plan_tools_merge: plan 模式下是否把专家工具并入只读白名单
            （``_CODE_PLAN_ALLOWED_TOOLS``）。默认 False——plan 保持只读语义。
        prompt_language: 专家 prompt section 的语言跟随策略。
            ``"profile"`` = 跟随该 profile 的运行时语言（code/design 为 en）。
    """

    plan_tools_merge: bool = False
    prompt_language: str = "profile"


@dataclass(frozen=True)
class WorkModeProfile:
    """一种 work_mode 的完整模式声明。

    Attributes:
        work_mode: ``work`` / ``code`` / ``design``。
        single_canonical: 单 agent canonical mode（退团恢复目标）。
        team_canonical: 专家团 canonical mode（装团收敛目标）。
        manager_mode: AgentManager / 请求解析用的一级模式。
        adapter_kind: adapter 选型——``"deep"``（JiuWenSwarmDeepAdapter）或
            ``"code"``（JiuwenSwarmCodeAdapter；design 复用 code adapter）。
        member_profile: 团装配的成员 profile 路由键
            （``"work"`` / ``"code"`` / ``"design"``，config_specs 消费）。
        default_project_id: 该模式的虚拟默认项目桶。
        expert_policy: 单专家策略差异项。
    """

    work_mode: str
    single_canonical: str
    team_canonical: str
    manager_mode: str
    adapter_kind: str
    member_profile: str
    default_project_id: str
    expert_policy: ExpertPolicy = field(default_factory=ExpertPolicy)


WORK_MODE_PROFILES: dict[str, WorkModeProfile] = {
    profile.work_mode: profile
    for profile in (
        WorkModeProfile(
            work_mode=DEFAULT_WEB_WORK_MODE,
            single_canonical="agent",
            team_canonical="team",
            manager_mode="agent",
            adapter_kind="deep",
            member_profile="work",
            default_project_id=DEFAULT_PROJECT_ID_WORK,
        ),
        WorkModeProfile(
            work_mode=DEFAULT_TUI_WORK_MODE,
            single_canonical="code.normal",
            team_canonical="code.team",
            manager_mode="code",
            adapter_kind="code",
            member_profile="code",
            default_project_id=DEFAULT_PROJECT_ID_CODE,
        ),
        WorkModeProfile(
            work_mode=DEFAULT_DESIGN_WORK_MODE,
            single_canonical="design",
            team_canonical="design.team",
            manager_mode="design",
            adapter_kind="code",
            member_profile="design",
            default_project_id=DEFAULT_PROJECT_ID_DESIGN,
        ),
    )
}

# TUI 的 code 集群 plan：不走 work_mode 组合的历史完整模式串，
# 成员 profile 归 code，但不属于任何 work_mode 的 team_canonical。
_TEAM_PLAN_CANONICAL = "team.plan"

# canonical mode → 团队成员 profile。除注册表三模式的 team_canonical 外，
# 还覆盖历史完整模式串：design 单 agent 系（design/design.normal/design.plan
# 历史上可作为 team 请求 mode 传入 config_specs）与 team.plan。
_MEMBER_PROFILE_BY_CANONICAL: dict[str, str] = {
    **{p.team_canonical: p.member_profile for p in WORK_MODE_PROFILES.values()},
    _TEAM_PLAN_CANONICAL: "code",
    "design": "design",
    "design.normal": "design",
    "design.plan": "design",
}

# manager mode → adapter 选型。注册表三模式之外，团队历史 manager mode
# ``team`` 走 deep（与 create_adapter 既有行为一致）。
_ADAPTER_KIND_BY_MANAGER_MODE: dict[str, str] = {
    **{p.manager_mode: p.adapter_kind for p in WORK_MODE_PROFILES.values()},
    "team": "deep",
}

# canonical mode → manager mode（AgentManager 缓存键用 manager mode +
# collapse 后的 sub_mode 注册）。供「拿 metadata 里的 canonical 反查活 agent」
# 的路径使用（expert_service._locate_session_adapter 等）——直接拿 canonical
# 查会错过（"code.normal" 命不中按 "code" 注册的 agent）。
_MANAGER_MODE_BY_CANONICAL: dict[str, str] = {
    **{p.single_canonical: p.manager_mode for p in WORK_MODE_PROFILES.values()},
    # 团队 canonical 的 manager mode：code 系 adapter 的团（code.team/design.team）
    # 取 profile 的 manager_mode；work 团历史 manager mode 是 "team"。
    **{
        p.team_canonical: (p.manager_mode if p.adapter_kind == "code" else "team")
        for p in WORK_MODE_PROFILES.values()
    },
    # 历史/别名 canonical
    "code": "code",
    "code.plan": "code",
    "team.plan": "code",
    "design.normal": "design",
    "design.plan": "design",
    "agent.plan": "agent",
    "agent.fast": "agent",
    "plan": "agent",
    "fast": "agent",
}


def manager_mode_for_canonical(canonical_mode: Any) -> str | None:
    """canonical mode → manager mode；未知返回 None（调用方保留原值或走兜底）。"""
    if not isinstance(canonical_mode, str):
        return None
    return _MANAGER_MODE_BY_CANONICAL.get(canonical_mode.strip().lower())


def profile_for_work_mode(work_mode: Any) -> WorkModeProfile:
    """按 work_mode 取 profile；非法/缺省回落到 work profile（防御性）。"""
    normalized = normalize_work_mode(work_mode, default=DEFAULT_WEB_WORK_MODE)
    return WORK_MODE_PROFILES[normalized]


def team_canonical_for_work_mode(work_mode: Any) -> str:
    """装团收敛：work_mode → 团队 canonical mode（``team``/``code.team``/``design.team``）。"""
    return profile_for_work_mode(work_mode).team_canonical


def single_canonical_for_work_mode(work_mode: Any) -> str:
    """退团恢复：work_mode → 单 agent canonical mode（``agent``/``code.normal``/``design``）。"""
    return profile_for_work_mode(work_mode).single_canonical


def member_profile_for_canonical(canonical_mode: Any) -> str | None:
    """canonical mode → 团队成员 profile 路由键；非团队/未知 mode 返回 None。"""
    if not isinstance(canonical_mode, str):
        return None
    return _MEMBER_PROFILE_BY_CANONICAL.get(canonical_mode.strip().lower())


def adapter_kind_for_manager_mode(manager_mode: Any) -> str:
    """manager mode → adapter 选型（``"deep"`` / ``"code"``）；未知回落 deep。"""
    if not isinstance(manager_mode, str):
        return "deep"
    return _ADAPTER_KIND_BY_MANAGER_MODE.get(manager_mode.strip().lower(), "deep")


def team_canonical_modes() -> frozenset[str]:
    """全部集群 canonical mode（含历史 ``team.plan``）——is_swarm 白名单单源。"""
    return frozenset(
        {p.team_canonical for p in WORK_MODE_PROFILES.values()}
        | {_TEAM_PLAN_CANONICAL}
    )


__all__ = [
    "ExpertPolicy",
    "WORK_MODE_PROFILES",
    "WorkModeProfile",
    "adapter_kind_for_manager_mode",
    "manager_mode_for_canonical",
    "member_profile_for_canonical",
    "profile_for_work_mode",
    "single_canonical_for_work_mode",
    "team_canonical_for_work_mode",
    "team_canonical_modes",
]
