# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""jiuwenswarm.gateway.slash_command 单元测试."""

import importlib.util
from pathlib import Path
import sys
import pytest

# 避免 `import jiuwenswarm.gateway.slash_command` 触发 `jiuwenswarm.gateway.__init__`
# 进而级联导入 channel/wecom/lark_oapi，在开启 warning->error 的 CI 中导致 collection 失败。
_MODULE_PATH = (
        Path(__file__).resolve().parents[
            3] / "jiuwenswarm" / "gateway" / "message_handler" / "command_parser" / "slash_command.py"
)
_SPEC = importlib.util.spec_from_file_location("ut_gateway_slash_command", _MODULE_PATH)
assert _SPEC and _SPEC.loader
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MOD
_SPEC.loader.exec_module(_MOD)

CONTROL_MESSAGE_TEXTS = _MOD.CONTROL_MESSAGE_TEXTS
FIRST_BATCH_REGISTRY = _MOD.FIRST_BATCH_REGISTRY
ParsedControlAction = _MOD.ParsedControlAction
VALID_MODE_LINES = _MOD.VALID_MODE_LINES
VALID_SWITCH_LINES = _MOD.VALID_SWITCH_LINES
format_skills_list_for_notice = _MOD.format_skills_list_for_notice
is_control_like_for_im_batching = _MOD.is_control_like_for_im_batching
parse_channel_control_text = _MOD.parse_channel_control_text


@pytest.mark.parametrize(
    ("text", "action", "subcommand", "branch_name", "rewind_turn"),
    [
        ("", ParsedControlAction.NONE, None, None, None),
        ("hello", ParsedControlAction.NONE, None, None, None),
        ("/new_session", ParsedControlAction.NEW_SESSION_OK, None, None, None),
        ("/new_session x", ParsedControlAction.NEW_SESSION_BAD, None, None, None),
        ("/mode agent", ParsedControlAction.MODE_OK, ("agent", None), None, None),
        ("/mode code", ParsedControlAction.MODE_OK, ("code", None), None, None),
        ("/mode team", ParsedControlAction.MODE_OK, ("team", None), None, None),
        ("/mode agent.plan", ParsedControlAction.MODE_OK, ("agent.plan", None), None, None),
        ("/mode agent.fast", ParsedControlAction.MODE_OK, ("agent.fast", None), None, None),
        ("/mode code.plan", ParsedControlAction.MODE_OK, ("code.plan", None), None, None),
        ("/mode code.normal", ParsedControlAction.MODE_OK, ("code.normal", None), None, None),
        ("/mode code.team", ParsedControlAction.MODE_OK, ("code.team", None), None, None),
        ("/mode plan", ParsedControlAction.MODE_BAD, (None, None), None, None),
        ("/mode", ParsedControlAction.MODE_BAD, (None, None), None, None),
        ("/switch plan", ParsedControlAction.SWITCH_OK, (None, "plan"), None, None),
        ("/switch fast", ParsedControlAction.SWITCH_OK, (None, "fast"), None, None),
        ("/switch normal", ParsedControlAction.SWITCH_OK, (None, "normal"), None, None),
        ("/switch team", ParsedControlAction.SWITCH_OK, (None, "team"), None, None),
        ("/switch code", ParsedControlAction.SWITCH_BAD, (None, None), None, None),
        ("/switch", ParsedControlAction.SWITCH_BAD, (None, None), None, None),
        ("/skills", ParsedControlAction.NONE, None, None, None),
        ("/skills list", ParsedControlAction.SKILLS_OK, None, None, None),
        ("/skills   list", ParsedControlAction.SKILLS_OK, None, None, None),
        ("/skills extra", ParsedControlAction.NONE, None, None, None),
        ("line1\nline2", ParsedControlAction.NONE, None, None, None),
        ("/branch", ParsedControlAction.BRANCH_OK, None, "", None),
        ("/branch fix-login", ParsedControlAction.BRANCH_OK, None, "fix-login", None),
        ("/branch  multi word name", ParsedControlAction.BRANCH_OK, None, "multi word name", None),
        ("/rewind 3", ParsedControlAction.REWIND_OK, None, None, 3),
        ("/rewind 1", ParsedControlAction.REWIND_OK, None, None, 1),
        ("/rewind", ParsedControlAction.REWIND_BAD, None, None, None),
        ("/rewind abc", ParsedControlAction.REWIND_BAD, None, None, None),
        ("/rewind 0", ParsedControlAction.REWIND_BAD, None, None, None),
        ("/rewind -1", ParsedControlAction.REWIND_BAD, None, None, None),
        ("/rewind confirm 3", ParsedControlAction.REWIND_CONFIRM, None, None, 3),
        ("/rewind confirm 1", ParsedControlAction.REWIND_CONFIRM, None, None, 1),
        ("/rewind confirm 0", ParsedControlAction.REWIND_BAD, None, None, None),
        ("/rewind confirm abc", ParsedControlAction.REWIND_BAD, None, None, None),
        ("/rewind cancel", ParsedControlAction.REWIND_CANCEL, None, None, None),
        ("/review", ParsedControlAction.REVIEW_OK, None, None, None),
        ("/review 123", ParsedControlAction.REVIEW_OK, None, None, None),
        (
            "/review https://github.com/org/repo/pull/123",
            ParsedControlAction.REVIEW_OK,
            None,
            None,
            None,
        ),
        ("/review abc", ParsedControlAction.REVIEW_OK, None, None, None),
        ("/review 123 focus on security", ParsedControlAction.REVIEW_OK, None, None, None),
        ("/review bb37e71c33b87199", ParsedControlAction.REVIEW_OK, None, None, None),
        ("/review bad-arg", ParsedControlAction.REVIEW_OK, None, None, None),
        ("/security-review", ParsedControlAction.SECURITY_REVIEW_OK, None, None, None),
        (
            "/security-review focus on auth",
            ParsedControlAction.SECURITY_REVIEW_OK,
            None,
            None,
            None,
        ),
    ],
)
def test_parse_channel_control_text(
    text: str,
    action: ParsedControlAction,
    subcommand: tuple[str | None, str | None] | None,
    branch_name: str | None,
    rewind_turn: int | None,
) -> None:
    p = parse_channel_control_text(text)
    assert p.action is action
    if subcommand:
        assert p.mode_subcommand == subcommand[0]
        assert p.switch_subcommand == subcommand[1]
    else:
        assert p.mode_subcommand is None
        assert p.switch_subcommand is None
    assert p.branch_name == branch_name
    assert p.rewind_turn == rewind_turn
    if action is ParsedControlAction.REVIEW_OK:
        if text == "/review":
            assert p.pr_arg == ""
        elif text.startswith("/review "):
            assert p.pr_arg == text[len("/review "):].strip()
    elif action is ParsedControlAction.REVIEW_BAD:
        assert p.pr_arg is None
    elif action is ParsedControlAction.SECURITY_REVIEW_OK:
        if text == "/security-review":
            assert p.security_review_arg == ""
        elif text.startswith("/security-review "):
            assert p.security_review_arg == text[len("/security-review "):].strip()
    elif action is ParsedControlAction.SECURITY_REVIEW_BAD:
        assert p.security_review_arg is None


def test_parse_channel_control_text_review_rejects_unsafe_args() -> None:
    too_long = "x" * 2049
    p = parse_channel_control_text(f"/review {too_long}")
    assert p.action is ParsedControlAction.REVIEW_BAD

    p = parse_channel_control_text("/review bad\x00arg")
    assert p.action is ParsedControlAction.REVIEW_BAD


def test_parse_channel_control_text_security_review_rejects_unsafe_args() -> None:
    too_long = "x" * 2049
    p = parse_channel_control_text(f"/security-review {too_long}")
    assert p.action is ParsedControlAction.SECURITY_REVIEW_BAD

    p = parse_channel_control_text("/security-review bad\x00arg")
    assert p.action is ParsedControlAction.SECURITY_REVIEW_BAD


def test_control_message_texts_contains_mode_variants_and_skills() -> None:
    assert "/new_session" in CONTROL_MESSAGE_TEXTS
    assert "/skills list" in CONTROL_MESSAGE_TEXTS
    assert VALID_MODE_LINES <= CONTROL_MESSAGE_TEXTS
    assert VALID_SWITCH_LINES <= CONTROL_MESSAGE_TEXTS
    assert "/mode team" in CONTROL_MESSAGE_TEXTS
    assert "/mode code" in CONTROL_MESSAGE_TEXTS
    assert "/mode agent.plan" in CONTROL_MESSAGE_TEXTS
    assert "/mode code.normal" in CONTROL_MESSAGE_TEXTS
    assert "/mode code.team" in CONTROL_MESSAGE_TEXTS
    assert "/switch normal" in CONTROL_MESSAGE_TEXTS
    assert "/switch team" in CONTROL_MESSAGE_TEXTS
    assert "/branch" in CONTROL_MESSAGE_TEXTS
    assert "/rewind" in CONTROL_MESSAGE_TEXTS


def test_is_control_like_for_im_batching() -> None:
    assert is_control_like_for_im_batching("/new_session")
    assert is_control_like_for_im_batching("/mode agent")
    assert is_control_like_for_im_batching("/mode agent.plan")
    assert is_control_like_for_im_batching("/mode foo")
    assert is_control_like_for_im_batching("/switch plan")
    assert is_control_like_for_im_batching("/switch foo")
    assert is_control_like_for_im_batching("/new_sessionoops")
    assert is_control_like_for_im_batching("/skills list")
    assert is_control_like_for_im_batching("/skills   list")
    assert is_control_like_for_im_batching("/branch")
    assert is_control_like_for_im_batching("/branch fix-login")
    assert is_control_like_for_im_batching("/rewind 3")
    assert is_control_like_for_im_batching("/rewind")
    assert is_control_like_for_im_batching("/review")
    assert is_control_like_for_im_batching("/review 123")
    assert is_control_like_for_im_batching("/review bad-arg")
    assert is_control_like_for_im_batching("/security-review")
    assert is_control_like_for_im_batching("/security-review focus on auth")
    assert not is_control_like_for_im_batching("/skills")
    assert not is_control_like_for_im_batching("/skills extra")
    assert not is_control_like_for_im_batching("")
    assert not is_control_like_for_im_batching("a\nb")


def test_format_skills_list_for_notice() -> None:
    out = format_skills_list_for_notice(
        {
            "skills": [
                {"name": "a", "description": "d1", "source": "local"},
                {"name": "b"},
            ]
        }
    )
    assert "【技能列表】" in out
    assert "a" in out
    assert "b" in out


def test_format_skills_list_for_notice_im_invariants() -> None:
    """IM 通道（微信等）仅从 payload.content 取文本，skills.list 载荷必须能渲染出非空 content。

    /skills list 在 IM 端无返回的根因：skills.list 响应 ``{"skills": [...]}`` 不含
    ``content``，被通道当作空消息丢弃。这里锁定 _skills_slash_notice 依赖的渲染入口
    在成功/空/错误三态下都产出可下发文本。
    """
    # 成功：有技能
    ok_text = format_skills_list_for_notice(
        {"skills": [{"name": "a", "description": "d", "source": "local"}]}
    )
    assert ok_text and ok_text.strip()
    assert "【技能列表】" in ok_text

    # 空：无技能
    empty_text = format_skills_list_for_notice({"skills": []})
    assert empty_text and empty_text.strip()

    # 错误：上游返回 error 字段
    err_text = format_skills_list_for_notice({"error": "boom"})
    assert err_text and err_text.strip()
    assert "boom" in err_text

    # 异常：载荷为 None / 非 dict
    assert format_skills_list_for_notice(None).strip()
    assert format_skills_list_for_notice({}).strip()


def test_format_skills_list_for_notice_groups_like_tui() -> None:
    """与 TUI skills.ts listSkills 对齐：按 installed 分组、标注来源标签。

    后端 handle_skills_list 对本地已装技能置 installed=True、内置未装置
    installed=False；渲染须据此分"已安装/可安装"两组，并给每项打
    [builtin]/[local]/[project] 标签，使 IM 端 /skills list 与 TUI 一致。
    """
    out = format_skills_list_for_notice(
        {
            "skills": [
                {"name": "merge-pr", "source": "local", "installed": True, "description": "合并 PR"},
                {"name": "daily-report", "is_builtin_source": True, "installed": False, "description": "日报"},
                {"name": "my-proj", "source": "project", "installed": True},
            ]
        }
    )
    # 分组标题
    assert "已安装" in out
    assert "可安装" in out
    # 标签：local→[local]，builtin_source→[builtin]，project→[project]
    assert "[local]" in out
    assert "[builtin]" in out
    assert "[project]" in out
    # 名字均出现
    assert "merge-pr" in out and "daily-report" in out and "my-proj" in out
    # 已安装组出现在可安装组之前
    assert out.index("已安装") < out.index("可安装")
    # 编号各组独立从 1 开始（不跨组连续）：可安装组首项 daily-report 的编号是 1，
    # 而非接着已安装组的 2。已安装组有 2 项，若跨组连续 daily-report 应为 3。
    avail_section = out.split("可安装", 1)[1]
    assert "\n1. daily-report" in avail_section


def test_format_skills_list_for_notice_max_items_truncates() -> None:
    """max_items 形参切实限制输出条目数，超限时显示截断提示。

    回归：重写分组渲染时一度让 for item in skills 全量遍历、底部提示条件
    shown < len(skills) 恒 False，导致 max_items 成为摆设。本用例锁定：
    超过 max_items 时只渲染前 max_items 项、分组标题计数反映实际显示数、
    底部出现"...共 N 项，仅显示前 max_items 项"提示。
    """
    skills = [
        *[{"name": f"ins-{i}", "installed": True} for i in range(3)],  # 3 已安装
        *[{"name": f"avail-{i}", "installed": False} for i in range(10)],  # 10 可安装
    ]
    out = format_skills_list_for_notice({"skills": skills}, max_items=5)
    # 总数 13 > max_items 5 → 出现截断提示
    assert "共 13 项" in out
    assert "仅显示前 5 项" in out
    # 已安装组 3 项全部显示（配额先满足已安装组）
    assert "已安装（3）" in out
    # 可安装组只剩 2 项配额，标题计数应为 2 而非 10
    assert "可安装（2）" in out
    # 可安装组只渲染到 avail-1（avail-2..9 不应出现）
    assert "avail-0" in out
    assert "avail-1" in out
    assert "avail-2" not in out


def test_truncate_desc_by_bytes_byte_budget_and_word_boundary() -> None:
    """按 UTF-8 字节预算截断：中英文视觉长度一致，英文不在单词中间断。

    旧实现按字符数(len)截断到 200：中文 200 字符≈600 字节很长很完整，英文 200
    字符≈200 字节一句话没说完就被切在单词中间(如 immediatel…)。现按 600 字节
    预算截断，且英文截断点落在词界(空格)。
    """
    from jiuwenswarm.gateway.message_handler.command_parser.slash_command import (
        _truncate_desc_by_bytes,
    )
    # 短描述原样返回
    assert _truncate_desc_by_bytes("短描述") == "短描述"
    assert _truncate_desc_by_bytes("short desc") == "short desc"
    assert _truncate_desc_by_bytes("") == ""

    # 英文超长：截断点落在空格(词界)，不把单词劈成两半；以 … 结尾。
    # "word " 每段 5 字符；600 字节恰好容纳若干完整段，截断后不含半截 "wor"/"wo"。
    long_en = "word " * 200  # 1000 字符，远超 600 字节
    cut_en = _truncate_desc_by_bytes(long_en)
    assert cut_en.endswith("…")
    body = cut_en.rstrip("…").rstrip()
    # 截断处应落在词界：body 末尾是一个完整 "word"，而非被劈开的 "wor"/"wo"
    assert body.endswith("word")
    # 反向验证：若硬截在单词中间会得到 "wor…"/"wo…"，这里不应发生
    assert not body.endswith("wor")
    assert not body.endswith("wo")

    # 中文超长：200 汉字 = 600 字节，刚好不超；201 汉字超预算被截
    exactly_200_cn = "字" * 200
    assert _truncate_desc_by_bytes(exactly_200_cn) == exactly_200_cn  # 边界：刚好不截
    over_201_cn = "字" * 201
    cut_cn = _truncate_desc_by_bytes(over_201_cn)
    assert cut_cn.endswith("…")
    # 截断后字节数 <= 600 + "…"(3字节)
    assert len(cut_cn.encode("utf-8")) <= 600 + 3

    # 中英视觉长度一致：600 字节预算下，英文≈600 字符、中文≈200 字符触发截断
    assert _truncate_desc_by_bytes("a" * 600) == "a" * 600  # 600 字节，边界不截
    assert _truncate_desc_by_bytes("a" * 601) != "a" * 601  # 超过即截
    assert _truncate_desc_by_bytes("字" * 201) != "字" * 201  # 201 汉字=603 字节，超即截


def test_first_batch_registry_ids() -> None:
    ids = {e.id for e in FIRST_BATCH_REGISTRY}
    expected = {
        "new_session", "mode", "switch", "skills", "resume",
        "workspace_dir", "branch", "rewind", "recap", "agents", "review", "security-review",
        "goal",
    }
    assert ids == expected


def test_exit_parse_rejects_short_form_requires_full_team_session_ref() -> None:
    """/exit 简化格式不再允许：缺 team_name 维度无法做一致性校验。

    - /exit（无参）→ EXIT_OK，不校验 team_name（handler 用当前 session 兜底）
    - /exit team_<name>_session_<id>（完整）→ EXIT_OK，带 session_ref，handler 校验
    - /exit <session_id>（简化）→ EXIT_BAD，引导用户用完整格式或无参 /exit
    """
    # 无参：EXIT_OK，无 session_ref
    p = parse_channel_control_text("/exit")
    assert p.action is ParsedControlAction.EXIT_OK
    assert p.session_ref is None

    # 完整格式：EXIT_OK，session_ref 原样回填
    p = parse_channel_control_text(
        "/exit team_jiuwen_team_sess_19f4b147e5a_session_sess_19f4b147e5a"
    )
    assert p.action is ParsedControlAction.EXIT_OK
    assert p.session_ref == "team_jiuwen_team_sess_19f4b147e5a_session_sess_19f4b147e5a"

    # 简化格式：EXIT_BAD（无论 session_id 是否合法形态）
    assert parse_channel_control_text("/exit sess_19f4b147e5a").action \
        is ParsedControlAction.EXIT_BAD
    assert parse_channel_control_text("/exit someplainid").action \
        is ParsedControlAction.EXIT_BAD

    # /join 简化格式不再允许（缺 team_name 维度无法做一致性校验）
    assert parse_channel_control_text("/join sess_19f4b147e5a as auditor").action \
        is ParsedControlAction.JOIN_BAD
    # 完整格式
    pj = parse_channel_control_text("/join team_jiuwen_sess_19f4b147e5a_session_sess_19f4b147e5a as auditor")
    assert pj.action is ParsedControlAction.JOIN_OK
    assert pj.session_ref == "team_jiuwen_sess_19f4b147e5a_session_sess_19f4b147e5a"
    assert pj.member_name == "auditor"

    # 中文 team name：完整格式应放行
    p = parse_channel_control_text(
        "/exit team_我的团队_sess_abc123_session_sess_abc123"
    )
    assert p.action is ParsedControlAction.EXIT_OK
    assert p.session_ref == "team_我的团队_sess_abc123_session_sess_abc123"

    pj = parse_channel_control_text(
        "/join team_我的团队_sess_abc123_session_sess_abc123 as 审核员"
    )
    assert pj.action is ParsedControlAction.JOIN_OK
    assert pj.session_ref == "team_我的团队_sess_abc123_session_sess_abc123"
    assert pj.member_name == "审核员"
