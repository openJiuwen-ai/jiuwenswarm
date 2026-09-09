"""Web 组合模式解析与 TUI 历史模式直通的回归测试。"""

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import Mode
from jiuwenswarm.common.mode_matrix import (
    NEW_AGENT_WORK_NORMAL,
    NEW_AGENT_WORK_PLAN,
    NEW_AGENT_CODE_NORMAL,
    NEW_AGENT_CODE_PLAN,
    NEW_TEAM_WORK_NORMAL,
    NEW_TEAM_WORK_PLAN,
    NEW_TEAM_CODE_NORMAL,
    NEW_TEAM_CODE_PLAN,
    base_mode_without_plan,
    deprecate_mode,
    is_plan_mode,
    is_single_agent_mode,
    is_team_mode,
    resolve_request_mode,
)
from jiuwenswarm.server.agent_ws_server import (
    _apply_resolved_mode_to_request,
    resolve_agent_request_mode,
)


def _resolve(params):
    return resolve_request_mode(params, resolve_agent_request_mode)


def test_request_preserves_original_mode_when_web_composition_rewrites_it():
    request = AgentRequest(
        request_id="req-1",
        params={"mode": "agent.plan", "work_mode": "code"},
    )

    assert _apply_resolved_mode_to_request(request, work_mode="code") == ("code", "plan")
    assert request.params["mode"] == "code.plan"
    assert getattr(request, "_original_mode") == "agent.plan"


# ── Web 组合：work_mode 决定 profile，mode 决定是否 plan / team ─────────────


@pytest.mark.parametrize(
    ("mode", "work_mode", "expected"),
    [
        ("agent", "work", ("agent", None, "agent")),
        ("agent.plan", "work", ("agent", "plan", "agent.plan")),
        ("agent", "code", ("code", "normal", "code.normal")),
        ("agent.plan", "code", ("code", "plan", "code.plan")),
        ("team", "work", ("team", None, "team")),
        ("team", "code", ("code", "team", "code.team")),
    ],
)
def test_web_composition_covers_all_supported_combinations(mode, work_mode, expected):
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


@pytest.mark.parametrize(
    ("work_mode", "expected_manager", "expected_canonical"),
    [("work", "team", "team"), ("code", "code", "code.team")],
)
def test_web_team_uses_matching_profile(work_mode, expected_manager, expected_canonical):
    resolved = _resolve({"mode": "team", "work_mode": work_mode})

    assert resolved.from_web_composition is True
    assert (resolved.manager_mode, resolved.sub_mode, resolved.canonical_mode) == (
        expected_manager,
        None if work_mode == "work" else "team",
        expected_canonical,
    )
    assert resolved.is_team is True
    assert resolved.is_code_profile is (work_mode == "code")


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
    """Team Plan 不参与 Web 组合，正式别名始终选择 normal profile。"""
    resolved = _resolve({"mode": "team.plan", "work_mode": work_mode})

    assert resolved.from_web_composition is False
    assert (resolved.manager_mode, resolved.sub_mode, resolved.canonical_mode) == (
        "team",
        "plan",
        "team.plan.normal",
    )
    assert resolved.profile == "normal"


# ── P6.4：新三段命名串 agent.work.normal / agent.work.plan 的 Web 组合分支 ────


@pytest.mark.parametrize(
    ("mode", "work_mode", "expected"),
    [
        (NEW_AGENT_WORK_NORMAL, "work", ("agent", None,   NEW_AGENT_WORK_NORMAL)),
        (NEW_AGENT_WORK_PLAN,   "work", ("agent", "plan", NEW_AGENT_WORK_PLAN)),
    ],
)
def test_web_new_three_segment_modes_hit_composition_branch(mode, work_mode, expected):
    """P6.4：前端 wireMode.ts 产出的新三段命名串必须命中组合分支。

    回归 P6.1 改 wireMode.ts、P6.4 扩 ``WEB_COMPOSABLE_MODES`` 的接通点：
    前端发 ``agent.work.plan`` + 后端读到 work_mode=work（来自 session metadata），
    组合分支应直接产出 ``(agent, plan, agent.work.plan)`` 三元组，
    而不是落 legacy 默认 ``agent``。
    """
    resolved = _resolve({"mode": mode, "work_mode": work_mode})

    assert resolved.from_web_composition is True
    assert (resolved.manager_mode, resolved.sub_mode, resolved.canonical_mode) == expected


def test_web_new_plan_string_is_plan_flag_true():
    resolved = _resolve({"mode": NEW_AGENT_WORK_PLAN, "work_mode": "work"})

    assert resolved.is_plan is True
    assert resolved.is_team is False
    assert resolved.is_code_profile is False
    assert resolved.profile == "normal"


def test_web_new_normal_string_is_plan_flag_false():
    resolved = _resolve({"mode": NEW_AGENT_WORK_NORMAL, "work_mode": "work"})

    assert resolved.is_plan is False
    assert resolved.is_team is False
    assert resolved.is_code_profile is False
    assert resolved.profile == "normal"


def test_web_new_plan_normal_mode_uses_three_segment_exit():
    """P6.4：plan 退出后应回到新三段命名的 normal 变体。"""
    resolved = _resolve({"mode": NEW_AGENT_WORK_PLAN, "work_mode": "work"})

    assert resolved.normal_mode == NEW_AGENT_WORK_NORMAL


def test_web_new_strings_take_work_mode_from_kwarg_when_params_omit_it():
    """P6.2 退场后，前端不再 spread work_mode；后端从 kwarg（session metadata）取。

    模拟 ``resolve_request_mode(work_mode="work", params={"mode": "agent.work.plan"})``
    调用方不传 params 里的 work_mode 字段，组合分支仍应触发。
    """
    resolved = resolve_request_mode(
        {"mode": NEW_AGENT_WORK_PLAN},
        resolve_agent_request_mode,
        work_mode="work",
    )

    assert resolved.from_web_composition is True
    assert resolved.canonical_mode == NEW_AGENT_WORK_PLAN
    assert resolved.work_mode == "work"


def test_web_new_strings_without_work_mode_fall_to_legacy():
    """没带 work_mode（也无 kwarg）的新串仍按 legacy 处理。

    前端 wireMode.ts 总会产出 work profile 的串，但若 session metadata 没存
    work_mode（极端边界），后端应落 legacy 不报错。新串落 legacy 也不能被折叠：
    此处钉住 legacy 短路后仍解析出 agent.work.plan 而非 code.normal。
    """
    resolved = _resolve({"mode": NEW_AGENT_WORK_PLAN})

    assert resolved.from_web_composition is False
    assert (resolved.manager_mode, resolved.sub_mode, resolved.canonical_mode) == (
        "agent", "plan", NEW_AGENT_WORK_PLAN,
    )
    assert resolved.is_plan is True


# ── P6.5：新三段命名 canonical 的完整解析（TUI 直发，不经 Web 组合）───────────


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (NEW_AGENT_WORK_NORMAL, ("agent", None,   NEW_AGENT_WORK_NORMAL)),
        (NEW_AGENT_WORK_PLAN,   ("agent", "plan", NEW_AGENT_WORK_PLAN)),
        (NEW_AGENT_CODE_NORMAL, ("code", "normal", NEW_AGENT_CODE_NORMAL)),
        (NEW_AGENT_CODE_PLAN,   ("code", "plan",   NEW_AGENT_CODE_PLAN)),
        (NEW_TEAM_WORK_NORMAL,  ("team", None,   NEW_TEAM_WORK_NORMAL)),
        (NEW_TEAM_WORK_PLAN,    ("team", "plan", NEW_TEAM_WORK_PLAN)),
        (NEW_TEAM_CODE_NORMAL,  ("code", "team", NEW_TEAM_CODE_NORMAL)),
        (NEW_TEAM_CODE_PLAN,    ("code", "team", NEW_TEAM_CODE_PLAN)),
    ],
)
def test_new_canonical_resolves_self_describing(mode, expected):
    """P6.5：8 个新 canonical 按串自身语义解析，不依赖 work_mode。

    铁律：environment/state 段内嵌在串里，manager/sub 三元组与 Web 组合分支
    产出的结果一一对应，绝不回退到 legacy 归并。
    """
    resolved = _resolve({"mode": mode})

    assert (resolved.manager_mode, resolved.sub_mode, resolved.canonical_mode) == expected
    assert resolved.from_web_composition is False


@pytest.mark.parametrize(
    ("mode", "work_mode", "expected"),
    [
        (NEW_AGENT_WORK_PLAN,   "code", ("agent", "plan", NEW_AGENT_WORK_PLAN)),
        (NEW_AGENT_WORK_NORMAL, "code", ("agent", None,   NEW_AGENT_WORK_NORMAL)),
        (NEW_AGENT_CODE_PLAN,   "work", ("code", "plan",  NEW_AGENT_CODE_PLAN)),
        (NEW_AGENT_CODE_NORMAL, "work", ("code", "normal", NEW_AGENT_CODE_NORMAL)),
        (NEW_TEAM_WORK_PLAN,    "code", ("team", "plan",  NEW_TEAM_WORK_PLAN)),
        (NEW_TEAM_CODE_PLAN,    "work", ("code", "team",  NEW_TEAM_CODE_PLAN)),
    ],
)
def test_new_canonical_ignores_supplemented_work_mode(mode, work_mode, expected):
    """P6.5 回归：TUI 会话被补上 work_mode="code" 时不得覆盖新 canonical。

    这就是本 bug 的复现路径：TUI 发送 ``agent.work.plan``，``_prepare_code_mode_chat_turn``
    从 session metadata 补 work_mode="code"，修复前 legacy 把 ``agent.work.plan``
    折叠成 ``code.normal``，导致模型自述 code.normal 而 TUI 显示 agent.work.plan。
    新 canonical 必须按串自身语义解析，忽略补充的 work_mode。
    """
    resolved = resolve_request_mode(
        {"mode": mode},
        resolve_agent_request_mode,
        work_mode=work_mode,
    )

    assert (resolved.manager_mode, resolved.sub_mode, resolved.canonical_mode) == expected
    assert resolved.canonical_mode != "code.normal"
    assert resolved.from_web_composition is False


# ── TUI / CLI / cron：不带 work_mode 时必须完全走历史解析 ───────────────────


@pytest.mark.parametrize(
    ("raw_mode", "expected"),
    [
        ("agent", ("agent", None, "agent")),
        ("agent.plan", ("agent", "plan", "agent.plan")),
        ("agent.fast", ("agent", None, "agent")),
        ("plan", ("agent", None, "agent")),
        ("code.normal", ("code", "normal", "code.normal")),
        ("code.plan", ("code", "plan", "code.plan")),
        ("code.team", ("code", "team", "code.team")),
        ("team.code", ("code", "team", "code.team")),
        ("team", ("team", None, "team")),
        ("team.plan", ("team", "plan", "team.plan.normal")),
        ("team.plan.normal", ("team", "plan", "team.plan.normal")),
        ("team.plan.code", ("code", "team", "team.plan.code")),
    ],
)
def test_legacy_modes_are_untouched_without_work_mode(raw_mode, expected):
    resolved = _resolve({"mode": raw_mode})

    assert (resolved.manager_mode, resolved.sub_mode, resolved.canonical_mode) == expected
    assert resolved.from_web_composition is False


@pytest.mark.parametrize(
    "raw_mode", ["code.plan", "code.team", "team.plan.normal", "team.plan.code", "agent.fast"]
)
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

    # 非法 work_mode 退历史解析：agent.plan 按真实 plan 模式落 legacy canonical。
    assert resolved.from_web_composition is False
    assert (resolved.manager_mode, resolved.sub_mode, resolved.canonical_mode) == (
        "agent", "plan", "agent.plan",
    )
    assert resolved.is_plan is True


def test_missing_mode_defaults_to_agent():
    assert _resolve({}).canonical_mode == "agent"


# ── 纯函数 ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("agent.plan", True),
        ("code.plan", True),
        ("team.plan", True),
        ("team.plan.normal", True),
        ("team.plan.code", True),
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
        ("team.plan.normal", True),
        ("team.plan.code", True),
        ("code.team", True),
        ("agent", False),
        ("agent.plan", False),
        ("code.plan", False),
    ],
)
def test_is_team_mode(mode, expected):
    assert is_team_mode(mode) is expected


@pytest.mark.parametrize(
    "mode",
    [
        NEW_AGENT_WORK_NORMAL,
        NEW_AGENT_WORK_PLAN,
        NEW_AGENT_CODE_NORMAL,
        NEW_AGENT_CODE_PLAN,
    ],
)
def test_single_agent_modes_use_new_canonical_names(mode):
    assert is_single_agent_mode(mode)


@pytest.mark.parametrize(
    "mode",
    ["agent", "agent.fast", "agent.plan", "code", "code.normal", "code.plan"],
)
def test_single_agent_mode_rejects_legacy_names(mode):
    assert not is_single_agent_mode(mode)


@pytest.mark.parametrize(
    "mode",
    [NEW_TEAM_WORK_NORMAL, NEW_TEAM_WORK_PLAN, NEW_TEAM_CODE_NORMAL, NEW_TEAM_CODE_PLAN],
)
def test_single_agent_mode_rejects_new_team_canonical_names(mode):
    assert not is_single_agent_mode(mode)


def test_base_mode_without_plan_is_identity_for_normal_modes():
    assert base_mode_without_plan("agent") == "agent"
    assert base_mode_without_plan("code.team") == "code.team"


def test_unknown_mode_passes_through():
    """铁律 3：未知值 / None / 空串原样返回，不破坏未识别输入。

    ``deprecate_mode("") == ""`` 不变式（review 修复前空串被 normalize 成
    ``agent`` 再映射为 ``agent.work.normal``，违反 PLAN 文档契约）。
    ``deprecate_mode(None) is None`` 不变式（review 修复前 None 走兜底
    返回 ``agent.work.normal``，与 docstring 契约不符）。
    """
    assert deprecate_mode("unknown_mode") == "unknown_mode"
    assert deprecate_mode("") == ""
    assert deprecate_mode("  ") == "  "
    assert deprecate_mode(None) is None


def test_deprecate_mode_hits_legacy_canonicals():
    """DEPRECATION_MAP 命中路径：旧 canonical（字符串与枚举入参）映射到新 canonical。

    与 :func:`test_unknown_mode_passes_through` 互补——那边钉 unknown / 空值
    原样返回，这边钉已知旧串必须命中映射表。
    """
    assert deprecate_mode("agent.plan") == NEW_AGENT_WORK_PLAN
    assert deprecate_mode("code.plan") == NEW_AGENT_CODE_PLAN
    assert deprecate_mode("team") == NEW_TEAM_WORK_NORMAL
    # team.plan → team.plan.normal → team.work.plan（MODE_ALIASES + DEPRECATION_MAP 两步）
    assert deprecate_mode("team.plan") == NEW_TEAM_WORK_PLAN
    # 枚举入参同样走映射（先取 .value 再查表）
    assert deprecate_mode(Mode.AGENT_PLAN) == NEW_AGENT_WORK_PLAN
    assert deprecate_mode(Mode.AGENT_FAST) == NEW_AGENT_WORK_NORMAL


@pytest.mark.parametrize(
    "mode", ["team", "team.plan", "team.plan.normal", "team.plan.code", "code.team"]
)
def test_team_modes_are_team_params(mode):
    from jiuwenswarm.server.utils.utils import is_team_params

    assert is_team_params({"mode": mode})
