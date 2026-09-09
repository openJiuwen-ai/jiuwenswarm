"""Web 组合模式解析与 TUI 历史模式直通的回归测试。"""

import pytest

from jiuwenswarm.common.mode_matrix import (
    base_mode_without_plan,
    is_plan_mode,
    is_team_mode,
    resolve_request_mode,
)
from jiuwenswarm.server.agent_ws_server import resolve_agent_request_mode


def _resolve(params):
    return resolve_request_mode(params, resolve_agent_request_mode)


# ── Web 组合：只覆盖单 agent，work_mode 决定 profile，mode 决定是否 plan ─────


@pytest.mark.parametrize(
    ("mode", "work_mode", "expected"),
    [
        ("agent", "work", ("agent", None, "agent")),
        ("agent.plan", "work", ("agent", "plan", "agent.plan")),
        ("agent", "code", ("code", "normal", "code.normal")),
        ("agent.plan", "code", ("code", "plan", "code.plan")),
    ],
)
def test_web_composition_covers_all_single_agent_combinations(mode, work_mode, expected):
    resolved = _resolve({"mode": mode, "work_mode": work_mode})

    assert (resolved.manager_mode, resolved.sub_mode, resolved.canonical_mode) == expected
    assert resolved.from_web_composition is True
    assert resolved.is_code_profile is (work_mode == "code")


@pytest.mark.parametrize(
    ("mode", "work_mode", "expected_plan"),
    [
        ("agent", "work", False),
        ("agent.plan", "work", True),
        ("agent.plan", "code", True),
    ],
)
def test_web_composition_plan_flag(mode, work_mode, expected_plan):
    resolved = _resolve({"mode": mode, "work_mode": work_mode})

    assert resolved.is_plan is expected_plan
    assert resolved.is_team is False


@pytest.mark.parametrize("work_mode", ["work"])
def test_web_team_work_mode_unchanged(work_mode):
    """work 桶的集群请求保持历史解析（逐字节不变）。

    mode=team + code/design work_mode 才组合出 code.team/design.team；
    work 桶（及无 work_mode 的历史客户端）行为与改造前完全一致。
    """
    resolved = _resolve({"mode": "team", "work_mode": work_mode})

    assert resolved.from_web_composition is False
    assert (resolved.manager_mode, resolved.sub_mode, resolved.canonical_mode) == (
        "team",
        None,
        "team",
    )
    assert resolved.is_team is True


@pytest.mark.parametrize(
    ("work_mode", "expected"),
    [
        ("code", ("code", "team", "code.team")),
        ("design", ("design", "team", "design.team")),
    ],
)
def test_web_team_composes_with_code_design_work_mode(work_mode, expected):
    """Web 集群 mode=team + code/design work_mode → 注册表组合团队 canonical。"""
    resolved = _resolve({"mode": "team", "work_mode": work_mode})

    assert resolved.from_web_composition is False
    assert (resolved.manager_mode, resolved.sub_mode, resolved.canonical_mode) == expected
    assert resolved.is_team is True


@pytest.mark.parametrize(
    ("mode", "work_mode", "expected_normal"),
    [
        ("agent.plan", "work", "agent"),
        ("agent.plan", "code", "code.normal"),
    ],
)
def test_plan_exit_mode_is_profile_aware(mode, work_mode, expected_normal):
    assert _resolve({"mode": mode, "work_mode": work_mode}).normal_mode == expected_normal


@pytest.mark.parametrize("work_mode", ["work", "code"])
def test_web_team_plan_is_not_composable(work_mode):
    """集群不支持 Plan：``team.plan`` 不再是 Web 组合值，落回历史解析。"""
    resolved = _resolve({"mode": "team.plan", "work_mode": work_mode})

    assert resolved.from_web_composition is False
    assert (resolved.manager_mode, resolved.sub_mode, resolved.canonical_mode) == (
        "code",
        "team",
        "team.plan",
    )


# ── TUI / CLI / cron：不带 work_mode 时必须完全走历史解析 ───────────────────


@pytest.mark.parametrize(
    ("raw_mode", "expected"),
    [
        ("agent", ("agent", None, "agent")),
        ("agent.plan", ("agent", None, "agent")),
        ("agent.fast", ("agent", None, "agent")),
        ("plan", ("agent", None, "agent")),
        ("code.normal", ("code", "normal", "code.normal")),
        ("code.plan", ("code", "plan", "code.plan")),
        ("code.team", ("code", "team", "code.team")),
        ("team", ("team", None, "team")),
        ("team.plan", ("code", "team", "team.plan")),
    ],
)
def test_legacy_modes_are_untouched_without_work_mode(raw_mode, expected):
    resolved = _resolve({"mode": raw_mode})

    assert (resolved.manager_mode, resolved.sub_mode, resolved.canonical_mode) == expected
    assert resolved.from_web_composition is False


@pytest.mark.parametrize("raw_mode", ["code.plan", "code.team", "agent.fast"])
def test_legacy_full_modes_ignore_work_mode(raw_mode):
    """即便某个客户端同时带了 work_mode，完整模式串仍按历史语义解析。

    ``code.normal`` 不在此列：它是 ``resolve_agent_request_mode`` 早就会按
    ``work_mode`` 改写的"可归属"模式，见
    :func:`test_legacy_neutral_modes_still_follow_work_mode`。
    """
    with_work = _resolve({"mode": raw_mode, "work_mode": "work"})
    without_work = _resolve({"mode": raw_mode})

    assert with_work.canonical_mode == without_work.canonical_mode
    assert with_work.manager_mode == without_work.manager_mode
    assert with_work.from_web_composition is False


@pytest.mark.parametrize(
    ("raw_mode", "work_mode", "expected"),
    [
        ("code.normal", "work", ("agent", None, "agent")),
        ("code.normal", "code", ("code", "normal", "code.normal")),
        ("code", "work", ("agent", None, "agent")),
        ("agent", "code", ("code", "normal", "code.normal")),
    ],
)
def test_legacy_neutral_modes_still_follow_work_mode(raw_mode, work_mode, expected):
    """``agent`` / ``code`` / ``code.normal`` 由 work_mode 决定归属（历史行为）。

    这三个取值只表达"普通单 agent"，不表达工作环境，因此 ``resolve_agent_request_mode``
    在 Web 组合模式引入之前就会用 ``work_mode``（通常来自会话 metadata）改写它们。
    组合分支不接管这些请求（``code.normal`` 等不是 Web 组合值，``agent`` 则由
    组合分支给出同样的结果），此处把这条历史约定钉住。
    """
    resolved = _resolve({"mode": raw_mode, "work_mode": work_mode})

    assert (resolved.manager_mode, resolved.sub_mode, resolved.canonical_mode) == expected


def test_invalid_work_mode_falls_back_to_legacy():
    resolved = _resolve({"mode": "agent.plan", "work_mode": "nonsense"})

    assert resolved.from_web_composition is False
    assert resolved.canonical_mode == "agent"


def test_missing_mode_defaults_to_agent():
    assert _resolve({}).canonical_mode == "agent"


# ── 纯函数 ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("agent.plan", True),
        ("code.plan", True),
        ("team.plan", True),
        ("agent", False),
        ("team", False),
        ("code.normal", False),
        ("code.team", False),
    ],
)
def test_is_plan_mode(mode, expected):
    assert is_plan_mode(mode) is expected


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("team", True),
        ("team.plan", True),
        ("code.team", True),
        ("agent", False),
        ("agent.plan", False),
        ("code.plan", False),
    ],
)
def test_is_team_mode(mode, expected):
    assert is_team_mode(mode) is expected


def test_base_mode_without_plan_is_identity_for_normal_modes():
    assert base_mode_without_plan("agent") == "agent"
    assert base_mode_without_plan("code.team") == "code.team"


@pytest.mark.parametrize("mode", ["team", "team.plan", "code.team"])
def test_team_modes_are_team_params(mode):
    from jiuwenswarm.server.utils.utils import is_team_params

    assert is_team_params({"mode": mode})
