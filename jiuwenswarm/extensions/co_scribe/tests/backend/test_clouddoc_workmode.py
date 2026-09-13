"""Matrix ⑬ — the workmode subsystem (PR2a).

The style layer's whole contract: missing/unreadable/oversized file behavior,
re-read per dispatch, unique-match editing, the ①②③ injection order, and the
unattended refusal of both workmode tools. Style never carries authority, so the
tests here guard mechanics, not semantics — the permission table and closed set
are what keep a workmode sentence from granting anything.
"""

from __future__ import annotations

import os

import pytest

from jiuwenswarm.extensions.co_scribe.backend.toolkit.workmode import (
    WORKMODE_MAX_BYTES,
    builtin_template,
    edit_workmode,
    load_workmode,
    prefer_zh_from_words,
    resolve_workmode_path,
)


# ---------------------------------------------------------------- load_workmode


def test_missing_file_falls_back_to_builtin_without_error(tmp_path):
    wm = load_workmode(str(tmp_path / "absent.md"))
    assert wm.source == "builtin"
    assert wm.error is None
    assert wm.text == builtin_template(prefer_zh=True)


def test_builtin_template_language_follows_prefer_zh(tmp_path, monkeypatch):
    assert "工作方式" in builtin_template(prefer_zh=True)
    assert "working style" in builtin_template(prefer_zh=False)
    # ``None`` resolves to the deployment's default file; point it at an absent one so
    # a working-style file on the developer's machine cannot decide the outcome.
    import jiuwenswarm.extensions.co_scribe.backend.toolkit.workmode as wm
    monkeypatch.setattr(wm, "resolve_workmode_path", lambda c: tmp_path / "absent.md")
    assert load_workmode(None, prefer_zh=False).text == builtin_template(prefer_zh=False)


def test_existing_file_is_read_as_is(tmp_path):
    f = tmp_path / "wm.md"
    f.write_text("# 自定义\n- 简短回复\n", encoding="utf-8")
    wm = load_workmode(str(f))
    assert wm.source == "file"
    assert wm.text == "# 自定义\n- 简短回复\n"
    assert not wm.truncated


def test_empty_file_is_respected_not_replaced_by_template(tmp_path):
    # A deployer who emptied the file asked for no style text, not the template back.
    f = tmp_path / "wm.md"
    f.write_text("", encoding="utf-8")
    wm = load_workmode(str(f))
    assert wm.source == "file"
    assert wm.text == ""


def test_unreadable_file_falls_back_to_builtin_with_error(tmp_path):
    f = tmp_path / "wm.md"
    f.write_text("x", encoding="utf-8")
    os.chmod(f, 0o000)
    try:
        wm = load_workmode(str(f))
    finally:
        os.chmod(f, 0o644)
    assert wm.source == "builtin"
    assert wm.error is not None


def test_oversized_file_truncates_on_line_boundary(tmp_path):
    line = "规则" * 40 + "\n"  # ~240 bytes per line in UTF-8
    f = tmp_path / "wm.md"
    f.write_text(line * 200, encoding="utf-8")  # ~48KB > 16KB
    wm = load_workmode(str(f))
    assert wm.truncated
    assert len(wm.text.encode("utf-8")) <= WORKMODE_MAX_BYTES
    # Line boundary: no half line survives.
    for ln in wm.text.split("\n"):
        assert ln == "" or ln == line.rstrip("\n")


def test_single_giant_line_is_clipped_not_dropped(tmp_path):
    f = tmp_path / "wm.md"
    f.write_text("字" * 20000, encoding="utf-8")  # one line, ~60KB
    wm = load_workmode(str(f))
    assert wm.truncated
    assert wm.text  # not silently emptied
    assert len(wm.text.encode("utf-8")) <= WORKMODE_MAX_BYTES


def test_reread_per_dispatch_sees_the_edit_immediately(tmp_path):
    f = tmp_path / "wm.md"
    f.write_text("回复要长。\n", encoding="utf-8")
    assert "回复要长" in load_workmode(str(f)).text
    r = edit_workmode(str(f), "回复要长。", "回复要短。")
    assert r["ok"], r
    assert "回复要短" in load_workmode(str(f)).text


# ---------------------------------------------------------------- edit_workmode


def test_edit_materializes_builtin_on_first_edit(tmp_path):
    # What workmode_get displayed is exactly what old_string matches against.
    f = tmp_path / "wm.md"
    anchor = "编辑最小化：只改被要求的那处，不顺手优化。"
    assert anchor in builtin_template(prefer_zh=True)
    r = edit_workmode(str(f), anchor, "编辑最小化，且逐条说明理由。")
    assert r["ok"], r
    assert f.is_file()
    assert "逐条说明理由" in f.read_text(encoding="utf-8")


def test_edit_zero_match_refuses(tmp_path):
    f = tmp_path / "wm.md"
    f.write_text("abc\n", encoding="utf-8")
    r = edit_workmode(str(f), "不存在的原文", "x")
    assert not r["ok"]
    assert "找不到" in r["detail"]


def test_edit_multi_match_refuses_with_count(tmp_path):
    f = tmp_path / "wm.md"
    f.write_text("规则A\n规则A\n", encoding="utf-8")
    r = edit_workmode(str(f), "规则A", "规则B")
    assert not r["ok"]
    assert "2" in r["detail"]
    assert f.read_text(encoding="utf-8") == "规则A\n规则A\n"  # untouched


def test_edit_empty_old_string_refuses(tmp_path):
    r = edit_workmode(str(tmp_path / "wm.md"), "", "x")
    assert not r["ok"]


def test_edit_over_limit_warns_but_applies(tmp_path):
    f = tmp_path / "wm.md"
    f.write_text("小段。\n", encoding="utf-8")
    r = edit_workmode(str(f), "小段。", "长" * 20000)
    assert r["ok"]
    assert str(WORKMODE_MAX_BYTES) in r["detail"]


def test_resolve_path_empty_uses_workspace_default():
    p = resolve_workmode_path("")
    assert p.name == "clouddoc-workmode.md"
    assert p.parent.name == "config"


def test_prefer_zh_from_words():
    assert prefer_zh_from_words(("同意", "approve")) is True
    assert prefer_zh_from_words(("approve",)) is False
    assert prefer_zh_from_words(None) is True


# ------------------------------------------------------------ prompt injection


def _comment(**kw):
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import DocComment

    base = dict(
        comment_id="c1", author_is_self=False, author_display_name="张三",
        created_time="2026-08-18T00:00:00.000Z", content="改一下",
        quoted_text="原句", resolved=False, mentioned_addresses=(),
        replies=(), assignee_address=None, author_is_service_account=False,
    )
    base.update(kw)
    return DocComment(**base)


def _build(**kw):
    from jiuwenswarm.extensions.co_scribe.backend.host.watch.turn_prompt import build_turn_prompt

    # There is one watch mode left, and the prompt builder now refuses any other
    # (the dispatch gate never sends one, so seeing one is a bug worth a raise).
    # Cases that are not about the mode take the real one by default.
    kw.setdefault("mode", "apply_scoped")
    return build_turn_prompt(
        _comment(), **kw
    )


def test_workmode_injected_between_contract_and_conventions():
    from jiuwenswarm.extensions.co_scribe.backend.host.conventions import Conventions

    conv = Conventions(source="in_doc", comment_id="cc", text="术语不许改", item_count=1, truncated=False, content_hash="h")
    tp = _build(workmode_text="回复要短。", conventions=conv)
    body = tp.text
    i_contract = body.index("协作者")           # segment ① task contract
    i_workmode = body.index("回复要短。")        # segment ②
    i_conv = body.index("术语不许改")            # segment ③
    assert i_contract < i_workmode < i_conv, "①②③ 顺序必须保持"


def test_workmode_label_denies_authority():
    tp = _build(workmode_text="任何修改无需确认。")
    assert "不是授权指令" in tp.text


def test_workmode_absent_or_blank_injects_nothing():
    # Probe the ② header's distinctive label -- "工作方式" alone also appears in the
    # ① task contract, so it cannot distinguish presence of the segment.
    assert "部署者策略" not in _build(workmode_text=None).text
    assert "部署者策略" not in _build(workmode_text="   ").text
    assert "部署者策略" in _build(workmode_text="回复要短。").text


def test_workmode_is_not_fenced_conventions_are():
    from jiuwenswarm.extensions.co_scribe.backend.host.conventions import Conventions

    conv = Conventions(source="in_doc", comment_id="cc", text="约定内容X", item_count=1, truncated=False, content_hash="h")
    tp = _build(workmode_text="风格内容Y", conventions=conv)
    n = tp.nonce
    # Conventions sit inside an UNTRUSTED fence; deployer style does not (v3.0: two
    # labels, one fence — fencing ② with "do not follow" framing would contradict it).
    assert f"[UNTRUSTED-{n}]" in tp.text
    conv_seg = tp.text.split("约定内容X")[0]
    assert conv_seg.rstrip().endswith(f"[UNTRUSTED-{n}]")
    wm_seg = tp.text.split("风格内容Y")[0]
    assert not wm_seg.rstrip().endswith(f"[UNTRUSTED-{n}]")


# ------------------------------------------------------- unattended refusal


def test_workmode_tools_are_deny_listed():
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools import (
        UNATTENDED_ALLOWLIST,
        UNATTENDED_DENYLIST,
    )

    assert "clouddoc_workmode_get" in UNATTENDED_DENYLIST
    assert "clouddoc_workmode_edit" in UNATTENDED_DENYLIST
    assert not (UNATTENDED_ALLOWLIST & UNATTENDED_DENYLIST)


def test_toolkit_registers_both_workmode_tools_and_count():
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools import (
        CloudDocToolkit,
    )

    class _P:  # minimal provider stub; get_tools touches no provider methods
        text_domain = "plain"

    tools = CloudDocToolkit(_P()).get_tools()
    names = [t.card.name for t in tools]
    assert "clouddoc_workmode_get" in names
    assert "clouddoc_workmode_edit" in names
    # Thirteen: list, read, list_comments, reply_comment, batch_edit, apply_for_comment,
    # write_region, add_page, create/share/trash, and the workmode pair.
    assert len(names) == 13


@pytest.mark.asyncio
async def test_workmode_edit_tool_round_trip(tmp_path):
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools import (
        CloudDocToolkit,
    )

    class _P:
        text_domain = "plain"

    f = tmp_path / "wm.md"
    f.write_text("旧风格。\n", encoding="utf-8")
    kit = CloudDocToolkit(_P(), workmode_file=str(f), workmode_prefer_zh=True)
    got = await kit.workmode_get()
    assert got["ok"] and got["source"] == "file" and "旧风格" in got["text"]
    r = await kit.workmode_edit(old_string="旧风格。", new_string="新风格。")
    assert r["ok"], r
    assert "新风格" in (await kit.workmode_get())["text"]


# ------------------------------------- the agent does not arbitrate between people


def test_the_apply_contract_tells_the_model_not_to_settle_contradictions():
    """D5's arbitration protocol. Code decides overlap; whether two requests contradict
    each other is a judgement about meaning, and making it is adjudicating between two
    people -- which belongs to the document's owner, not to the agent. The instruction
    has to be in the contract rather than the workmode, because the workmode is style
    and can be rewritten by a deployer."""
    prompt = _build(mode="apply_scoped").text
    assert "相互矛盾" in prompt
    assert "本轮未处理" in prompt
    # The retired assignment vocabulary is gone from the contract: a person resolves
    # the contradiction and then mentions the agent again.
    assert "再 @ 我" in prompt and "重新指派" not in prompt


def test_compatible_requests_are_still_meant_to_be_merged():
    """Refusing on contradiction must not read as refusing whenever two comments touch
    one passage -- the merge exists precisely so one edit can answer both."""
    prompt = _build(mode="apply_scoped").text
    assert "兼容" in prompt


# ``test_the_reply_only_tier_says_nothing_about_arbitration`` was deleted here:
# the reply_only tier is retired from the mechanism, so there is no second
# contract to keep the arbitration rule out of. Its replacement is
# ``test_a_retired_or_unknown_mode_raises_instead_of_downgrading`` below -- a mode
# the gate should never have let through must be loud, not quietly narrowed.


def test_a_retired_or_unknown_mode_raises_instead_of_downgrading():
    """No mode below apply exists, so there is nothing to fall back to.

    Falling back would let a turn the dispatch gate refused run anyway, merely
    with fewer tools. Reaching this line at all means the gate was bypassed, and
    a bug in the dispatch gate has to be loud.
    """
    from jiuwenswarm.extensions.co_scribe.backend.host.watch.turn_prompt import build_turn_prompt

    for mode in ("reply_only", "", None, "propose"):
        with pytest.raises(ValueError):
            build_turn_prompt(
                _comment(), mode=mode,
            )
