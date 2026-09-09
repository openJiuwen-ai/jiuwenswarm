# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""WorkModeProfile 注册表（common/mode_profiles.py）映射矩阵测试。"""

from __future__ import annotations

from jiuwenswarm.common.mode_matrix import TEAM_CANONICAL_MODES, is_team_mode
from jiuwenswarm.common.mode_profiles import (
    WORK_MODE_PROFILES,
    adapter_kind_for_manager_mode,
    member_profile_for_canonical,
    profile_for_work_mode,
    single_canonical_for_work_mode,
    team_canonical_for_work_mode,
    team_canonical_modes,
)


def test_registry_covers_three_work_modes() -> None:
    assert set(WORK_MODE_PROFILES) == {"work", "code", "design"}


def test_team_canonical_matrix() -> None:
    """装团收敛：3 work_mode × 团队 canonical。"""
    assert team_canonical_for_work_mode("work") == "team"
    assert team_canonical_for_work_mode("code") == "code.team"
    assert team_canonical_for_work_mode("design") == "design.team"


def test_single_canonical_matrix() -> None:
    """退团恢复：3 work_mode × 单 agent canonical。"""
    assert single_canonical_for_work_mode("work") == "agent"
    assert single_canonical_for_work_mode("code") == "code.normal"
    assert single_canonical_for_work_mode("design") == "design"


def test_invalid_work_mode_falls_back_to_work_profile() -> None:
    assert team_canonical_for_work_mode("nonsense") == "team"
    assert single_canonical_for_work_mode(None) == "agent"
    assert profile_for_work_mode("").work_mode == "work"


def test_member_profile_routing() -> None:
    """团队成员 profile 路由：含历史 design 单 agent 系与 team.plan。"""
    assert member_profile_for_canonical("team") == "work"
    assert member_profile_for_canonical("code.team") == "code"
    assert member_profile_for_canonical("team.plan") == "code"
    assert member_profile_for_canonical("design.team") == "design"
    assert member_profile_for_canonical("design") == "design"
    assert member_profile_for_canonical("design.normal") == "design"
    assert member_profile_for_canonical("design.plan") == "design"
    # 非团队/未知 mode
    assert member_profile_for_canonical("agent") is None
    assert member_profile_for_canonical("code.normal") is None
    assert member_profile_for_canonical(None) is None


def test_adapter_kind_matrix() -> None:
    assert adapter_kind_for_manager_mode("agent") == "deep"
    assert adapter_kind_for_manager_mode("team") == "deep"
    assert adapter_kind_for_manager_mode("code") == "code"
    assert adapter_kind_for_manager_mode("design") == "code"
    # 未知 manager mode 回落 deep（与 create_adapter 历史行为一致）
    assert adapter_kind_for_manager_mode("mystery") == "deep"


def test_default_project_ids() -> None:
    assert WORK_MODE_PROFILES["work"].default_project_id == "default"
    assert WORK_MODE_PROFILES["code"].default_project_id == "default_code"
    assert WORK_MODE_PROFILES["design"].default_project_id == "default_design"


def test_team_canonical_modes_derives_into_mode_matrix() -> None:
    """mode_matrix.TEAM_CANONICAL_MODES 由注册表派生，含新增 design.team。"""
    assert team_canonical_modes() == frozenset(
        {"team", "code.team", "design.team", "team.plan"}
    )
    assert TEAM_CANONICAL_MODES == team_canonical_modes()
    assert is_team_mode("design.team")
    assert is_team_mode("code.team")
    assert not is_team_mode("design")


def test_expert_policy_defaults() -> None:
    """默认口径：plan 白名单不并入专家工具；语言跟随 profile。"""
    for profile in WORK_MODE_PROFILES.values():
        assert profile.expert_policy.plan_tools_merge is False
        assert profile.expert_policy.prompt_language == "profile"
