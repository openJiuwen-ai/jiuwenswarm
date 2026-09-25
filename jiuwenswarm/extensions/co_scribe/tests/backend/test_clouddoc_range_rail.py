"""Offline tests for the range rail: windows, anchors, caps, scopes and merging."""

from __future__ import annotations

import pytest

from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import DocSnapshot, Segment
from jiuwenswarm.extensions.co_scribe.backend.toolkit.rails.range_rail import (
    RailVerdict, RangeRailConfig,
    check_range, ctx_hash,
    scope_window,
)

CFG = RangeRailConfig()
BODY = "前言。这一段是被评论的内容，里面有一个术语叫甲。后面还有别的话，也提到甲了。结尾。"


def S(text=BODY):
    return DocSnapshot(doc_id="d", kind="document", revision_id="r1", text=text)


# ------------------------------------------------------------ the range rail


def test_old_string_searched_only_inside_window():
    """An identical match outside the window is simply irrelevant.

    The body contains the same word twice, once inside the quoted range and once far
    away. Searching the whole body and taking either match would write outside the
    range.
    """
    quoted = "这一段是被评论的内容，里面有一个术语叫甲。"
    got = check_range(S(), quoted, [("甲", "乙")], RangeRailConfig())
    assert got.ok, got.detail


def test_out_of_window_edit_is_rejected():
    quoted = "前言。"
    got = check_range(S(), quoted, [("结尾", "尾声")], RangeRailConfig())
    assert got.verdict is RailVerdict.OUT_OF_RANGE


def test_the_refusal_shows_the_window_it_is_refusing_against():
    """A boundary the caller cannot see is a boundary it has to guess at.

    Measured on a live document: a comment selected one sentence, an edit was refused for
    being outside it, and the refusal said only that "the window is the comment's own
    selection" -- which names something the model has no way to read. It searched: the
    paragraph, refused; the sentence before, refused; two characters, accepted. Five turns
    to find a boundary that was a string this rail was holding, and the answer it never
    tried was the obvious one -- replace the quoted sentence itself.

    So the refusal carries the window's text. One retry instead of a search.
    """
    quoted = "前言。"
    got = check_range(S(), quoted, [("结尾", "尾声")], RangeRailConfig())
    assert got.verdict is RailVerdict.OUT_OF_RANGE
    assert quoted in got.detail, "拒绝必须出示窗口原文，而不只是命名它"


def test_ambiguous_anchor_fails_closed():
    """No fuzzy matching: introduce a threshold and an attacker only has to construct a
    closer-scoring counterfeit."""
    got = check_range(S("重复段。重复段。"), "重复段。", [("重", "轻")], CFG)
    assert got.verdict is RailVerdict.ANCHOR_AMBIGUOUS


def test_truncated_quote_is_detected_by_ellipsis():
    """The platform truncates an over-long quote and ends it with U+2026.

    That is the primary test and max_quote_chars is the backstop: the truncation point,
    around 418, sits far below any threshold we would pick ourselves, so a
    length comparison alone would never fire.
    """
    got = check_range(S(), "前言。这一段…", [("前言", "序")], CFG)
    assert got.verdict is RailVerdict.QUOTE_TRUNCATED
    assert "Jiuwen chat" in got.detail


def test_magnitude_cap_bounds_insertion():
    """Constrain where an edit lands but not how much it writes and a sentence-sized anchor
    can carry arbitrarily long new text."""
    quoted = "前言。"
    huge = "x" * 10_000
    got = check_range(S(), quoted, [("前言", huge)], RangeRailConfig(max_insert_chars=100))
    assert got.verdict is RailVerdict.INSERT_TOO_LARGE


def test_edit_count_cap():
    quoted = "前言。"
    edits = [("前", "x")] * 20
    assert check_range(S(), quoted, edits, RangeRailConfig(max_edits=3)).verdict is RailVerdict.TOO_MANY_EDITS


# ------------------------------------------------------------ the context fingerprint



def test_reanchor_survives_distant_edits_but_not_nearby_ones():
    """ctx_hash is a content-addressed identity rather than a recorded offset -- with a reach
    of 64 code points on each side.

    The real boundary, confirmed by measurement:
      * an edit beyond the context window shifts every absolute offset while the proposal
        stays valid;
      * **an edit inside the window, within 64 code points on either side, changes the
        fingerprint and invalidates the proposal**.

    The second fails closed, which is the safe direction, but happens more often than
    intuition suggests -- **especially in short documents**: when the quoted range sits
    less than 64 code points from the start or end, that side's context does not fill the
    window, and appending anything at that end changes the fingerprint. A deliberate
    trade.
    """
    cfg = RangeRailConfig()
    pad = "填充段落。" * 30          # 两侧各留足 >64 码点，使上下文完全内含
    quoted = "这一段是被评论的内容，里面有一个术语叫甲。"
    body = pad + "前言。" + quoted + "后面还有别的话。" + pad
    base = check_range(S(body), quoted, [("甲", "乙")], cfg)
    assert base.ok, base.detail
    proposal = {"quoted_text": quoted, "ctx_hash": base.ctx_hash,
                "edits": [{"old_string": "甲", "new_string": "乙"}]}

    # Checked through check_range rather than the retired reanchor wrapper: the
    # property under test is ctx_hash's locality, which the range rail still computes
    # on every call. The wrapper only compared the two and went when its one caller did.

    # A distant edit: content added at both ends, both beyond the 64-code-point window,
    # so the fingerprint is unchanged
    far = S("开头新增。" + body + "结尾新增。")
    assert check_range(far, quoted, [("甲", "乙")], cfg).ctx_hash == proposal["ctx_hash"]

    # An adjacent edit: the 10 characters before the quoted range are replaced, inside
    # the window, so the fingerprint moves and an apply built on the old one must not
    # be treated as still anchored
    near = S(body.replace("前言。" + quoted, "换掉的前言内容。" + quoted))
    assert check_range(near, quoted, [("甲", "乙")], cfg).ctx_hash != proposal["ctx_hash"]


def test_ctx_hash_binds_only_neighbourhood():
    text = "aaa" + "TARGET" + "bbb"
    h1 = ctx_hash(text, 3, 9)
    h2 = ctx_hash(text.replace("TARGET", "OTHER!"), 3, 9)
    assert h1 == h2, "改的是区间内部，上下文未变，指纹应相同"


# ------------------------------------------------------------ the plain-text domain
#
# What actually happened: a colleague commented asking for bold, the model proposed
# `**this sentence**`, and approving it added four literal asterisks to the document's
# first line. Saying "plain text" in the prompt is not enough -- a model knowing the rule
# and what it does when asked directly for bold are two different things.


@pytest.mark.parametrize("marker", ["**", "__", "~~", "`"])
def test_an_edit_introducing_markup_is_refused(marker):
    body = "填充。" * 40 + "这一句需要被改写。" + "尾巴。" * 40
    snap = DocSnapshot(doc_id="d", kind="document", revision_id="r", text=body)
    chk = check_range(
        snap, "这一句需要被改写。",
        [("需要被改写", f"{marker}需要被改写{marker}")],
        RangeRailConfig(),
    )
    assert chk.verdict is RailVerdict.MARKUP_INTRODUCED
    assert "plain text" in chk.detail


def test_markup_already_present_in_the_original_is_not_penalised():
    """The test is on the increase: a document may legitimately contain backticks, as in a
    code fragment, and a blanket rule would refuse ordinary rewrites."""
    body = "填充。" * 40 + "调用 `foo()` 即可。" + "尾巴。" * 40
    snap = DocSnapshot(doc_id="d", kind="document", revision_id="r", text=body)
    chk = check_range(
        snap, "调用 `foo()` 即可。",
        [("调用 `foo()` 即可", "请调用 `foo()` 方法")],
        RangeRailConfig(),
    )
    assert chk.ok, chk.detail


def test_removing_markup_is_allowed():
    """Removing markup is cleanup and must not be blocked."""
    body = "填充。" * 40 + "这是 **粗体** 文字。" + "尾巴。" * 40
    snap = DocSnapshot(doc_id="d", kind="document", revision_id="r", text=body)
    chk = check_range(
        snap, "这是 **粗体** 文字。",
        [("这是 **粗体** 文字", "这是粗体文字")],
        RangeRailConfig(),
    )
    assert chk.ok, chk.detail


def test_markdown_domain_allows_markup():
    """Whether markup is refused depends on **the target document's format**, not on a global
    preference.

    In a plain-text domain `**` is noise; in a markdown domain it is the correct output
    and refusing it is the error. Hard-coding this as a global rule would let one lesson
    measured here block a later provider.
    """
    body = "填充。" * 40 + "这一句需要被改写。" + "尾巴。" * 40
    snap = DocSnapshot(doc_id="d", kind="document", revision_id="r", text=body)
    edits = [("需要被改写", "**需要被改写**")]

    assert check_range(snap, "这一句需要被改写。", edits,
                       RangeRailConfig(text_domain="plain")).verdict is RailVerdict.MARKUP_INTRODUCED
    assert check_range(snap, "这一句需要被改写。", edits,
                       RangeRailConfig(text_domain="markdown")).ok


def test_unknown_domain_fails_closed_to_plain():
    """When a new provider forgets to declare its domain, failure points toward refusing one
    extra proposal rather than writing asterisks into a document."""
    assert RangeRailConfig().text_domain == "plain"


def test_rail_publishes_the_window_it_approved():
    """The rail must hand over the window it approved, and the applying layer locates within
    it.

    The two layers used to judge separately: the rail by uniqueness within the window, the
    provider by uniqueness in the whole body. The consequence of disagreeing is not that
    it becomes unsafe -- the applying layer still refuses -- but that **the refusal comes
    far too late**: the rail passes, the proposal is posted, a person reads and approves,
    and only at apply time are they told it cannot be done. A phrase occurring twice is
    ordinary in a long document.
    """
    body = "开头。这一段提到甲这个词。" + "填充。" * 200 + "很久以后又提到甲。"
    snap = DocSnapshot(doc_id="d", kind="document", revision_id="r", text=body)

    chk = check_range(snap, "这一段提到甲这个词。", [("甲", "乙")], RangeRailConfig())
    assert chk.ok, chk.detail
    assert chk.window_hi > chk.window_lo, "轨没有交出窗口"
    # One occurrence inside the window, two in the whole body -- exactly the case where
    # only the window makes location safe
    assert body[chk.window_lo:chk.window_hi].count("甲") == 1
    assert body.count("甲") == 2


@pytest.mark.asyncio
async def test_provider_locates_inside_the_approved_window():
    """The applying layer succeeds when it locates within the window, and fails on
    whole-body uniqueness when no window is given."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.google_provider import (
        GoogleDocsProvider,
    )
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import Segment

    body = "开头。这一段提到甲这个词。" + "填充。" * 200 + "很久以后又提到甲。"
    snap = DocSnapshot(doc_id="d", kind="document", revision_id="r", text=body,
                       segments=(Segment(0, len(body), 1, 1 + len(body)),))
    chk = check_range(snap, "这一段提到甲这个词。", [("甲", "乙")], RangeRailConfig())

    p = GoogleDocsProvider.__new__(GoogleDocsProvider)
    p._revision_cache = {}

    # No window: falls back to whole-body uniqueness and refuses
    assert (await p._submit("d", snap, [("甲", "乙")], "r")).status == "invalid"

    # With the rail's approved window: located successfully, on the occurrence inside it
    i0, i1 = p._locate(snap, "甲", (chk.window_lo, chk.window_hi))
    assert chk.window_lo < (i0 - 1) < chk.window_hi, "定位到了窗口之外"


def test_missing_text_is_not_reported_as_out_of_range():
    """"Not in the window" hides two situations, and conflating them misleads the model.

    Observed live: a model proposed an old_string carrying a line break where the body
    has a space, was told "outside the allowed range", retried the identical call,
    failed again and gave up. The pre-check exists to give the model something it can
    act on; a wrong diagnosis just makes it fail sooner.
    """
    snap = S("some text here and more text after it")
    out = check_range(snap, "some text here", [("text that was never there", "x")], RangeRailConfig())
    assert out.verdict is RailVerdict.TEXT_NOT_FOUND, out.verdict
    assert "does not appear in the document" in out.detail
    assert "line break" in out.detail, "the message must point at the usual cause"


def test_text_present_but_outside_the_window_stays_out_of_range():
    """The other half of the pair keeps its own verdict and its own advice."""
    far = "anchor here" + " padding " * 60 + "faraway phrase"
    out = check_range(S(far), "anchor here", [("faraway phrase", "x")], RangeRailConfig())
    assert out.verdict is RailVerdict.OUT_OF_RANGE, out.verdict
    assert "outside the authorized window" in out.detail


def test_globally_unique_edit_still_passes():
    """The guard must not catch ordinary edits."""
    body = "填充。" * 40 + "这一句需要被改写。" + "尾巴。" * 40
    snap = DocSnapshot(doc_id="d", kind="document", revision_id="r", text=body)
    assert check_range(snap, "这一句需要被改写。", [("需要被改写", "已改写")],
                       RangeRailConfig()).ok


def test_oversized_quote_is_refused_before_anything_else():
    """A cap on the selection. Drive truncates at around 418 code points and ends with an
    ellipsis, and the configured cap is the backstop. Both are needed: truncation is
    detectable, but a user can also select a large span that happens not to trigger it.
    """
    body = "填充。" * 500
    snap = DocSnapshot(doc_id="d", kind="document", revision_id="r", text=body)
    quoted = body[:401]            # 超过默认 max_quote_chars=400
    got = check_range(snap, quoted, [("填充", "替换")], RangeRailConfig())
    assert got.verdict is RailVerdict.QUOTE_TOO_LARGE

    # Exactly at the cap passes: a boundary has to be usable, not merely conservative
    ok_body = "前言。" + "甲" * 400 + "。后文。"
    ok_snap = DocSnapshot(doc_id="d", kind="document", revision_id="r", text=ok_body)
    assert check_range(ok_snap, "甲" * 400, [("甲" * 400, "乙")],
                       RangeRailConfig()).verdict is not RailVerdict.QUOTE_TOO_LARGE


# ------------------------------------------------------ same-anchor merging (D5, IC-4)

# Long enough that the two selections are disjoint: anything shorter merges
# legitimately and would not exercise the refusal.
LONG_BODY = (
    "开头这一段说的是甲事。" + "中间填充。" * 120 + "结尾这一段说的是乙事。"
)


def test_union_merges_two_comments_on_overlapping_text():
    """Two comments selecting the same sentence share one window, which is what lets a
    single edit answer both."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.rails.range_rail import union_window

    body = "第一句在这里。第二句紧随其后。第三句收尾。"
    window, failure = union_window(
        S(body), ["第一句在这里。第二句", "第二句紧随其后。"], CFG
    )
    assert failure is None
    lo, hi = window
    assert body[lo:hi].find("第一句") >= 0
    assert body[lo:hi].find("第二句紧随其后") >= 0


def test_union_refuses_comments_at_opposite_ends():
    """The attack IC-4 names: a comment at the top and one at the bottom, declared
    together, would make min..max the whole document -- a bounded edit covering
    everything. Disjoint spans are refused rather than merged."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.rails.range_rail import union_window

    window, failure = union_window(
        S(LONG_BODY), ["开头这一段说的是甲事。", "结尾这一段说的是乙事。"], CFG
    )
    assert window is None
    assert failure is not None
    assert failure.verdict is RailVerdict.RANGES_DISJOINT


def test_disjoint_refusal_does_not_depend_on_declaration_order():
    """The spans are sorted before merging, so naming the far comment first cannot slip
    a disjoint pair past the check."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.rails.range_rail import union_window

    window, failure = union_window(
        S(LONG_BODY), ["结尾这一段说的是乙事。", "开头这一段说的是甲事。"], CFG
    )
    assert window is None
    assert failure.verdict is RailVerdict.RANGES_DISJOINT


def test_merged_window_still_refuses_an_edit_outside_it():
    """Merging widens which text is in range and changes nothing about what may be done
    to it: an edit landing outside the merged window is refused as before."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.rails.range_rail import union_window

    body = "甲句在这里。乙句紧随其后。" + "无关内容。" * 40 + "丙句在很远处。"
    snap = S(body)
    window, failure = union_window(snap, ["甲句在这里。", "乙句紧随其后。"], CFG)
    assert failure is None

    check = check_range(snap, "甲句在这里。", [("丙句在很远处。", "改过的丙句。")], CFG, window=window)
    assert not check.ok
    assert check.verdict is RailVerdict.OUT_OF_RANGE


def test_merged_window_accepts_an_edit_inside_it():
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.rails.range_rail import union_window

    body = "甲句在这里。乙句紧随其后。收尾。"
    snap = S(body)
    window, failure = union_window(snap, ["甲句在这里。", "乙句紧随其后。"], CFG)
    assert failure is None

    check = check_range(snap, "甲句在这里。", [("乙句紧随其后", "乙句已改")], CFG, window=window)
    assert check.ok, check.detail


def test_union_fails_closed_on_an_anchor_that_no_longer_locates():
    """An anchor that cannot be placed uniquely stops the merge; the batch is refused
    rather than merged over whatever did anchor."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.rails.range_rail import union_window

    body = "重复句。重复句。"
    window, failure = union_window(S(body), ["重复句。"], CFG)
    assert window is None
    assert failure.verdict is RailVerdict.ANCHOR_AMBIGUOUS


# ------------------------------------------------- D23: the closed-menu scope
#
# The selection is the authorization; the model chooses a structural scope from a
# closed menu and the geometry is computed in code. The quoted body text gets no
# vote, and nothing widens past the declared structure. Supersedes D22's keyword
# vocabulary (measured live before either existed: a 44-character selection
# authorized a 444-character window and a whole section got rewritten).



def test_exact_scope_is_the_selection_itself():
    body = "第一句话。第二句目标词在这里。第三句话。"
    start = body.find("目标词")
    assert scope_window(body, start, start + 3, "exact") == (start, start + 3)


def test_sentence_scope_expands_to_the_enclosing_sentence():
    body = "第一句话。第二句目标词在这里。第三句话。"
    start = body.find("目标词")
    lo, hi = scope_window(body, start, start + 3, "sentence")
    assert body[lo:hi] == "第二句目标词在这里。"


def test_line_scope_expands_to_the_enclosing_line():
    body = "- 第一条。\n- 第二条目标词在这里。它还有下半句。\n- 第三条。"
    start = body.find("目标词")
    lo, hi = scope_window(body, start, start + 3, "line")
    assert body[lo:hi] == "- 第二条目标词在这里。它还有下半句。"


def test_paragraph_scope_expands_to_blank_line_bounds():
    body = "头段。\n\n第一行\n目标词所在行\n第三行\n\n尾段。"
    start = body.find("目标词")
    lo, hi = scope_window(body, start, start + 3, "paragraph")
    assert body[lo:hi] == "第一行\n目标词所在行\n第三行"


def test_an_unknown_scope_resolves_to_exact():
    """The fail direction must never be permissive."""
    body = "一句话里有目标词。"
    start = body.find("目标词")
    assert scope_window(body, start, start + 3, "whole_document") == (start, start + 3)


def test_check_range_refuses_an_edit_beyond_the_selection_at_exact():
    """The live incident, replayed: default scope, edit touching text beyond the
    quoted span -- refused, where the pre-D22 ±200 budget used to allow it."""
    body = "- 第一条内容。\n- 第二条内容。\n- 第三条目标内容。\n"
    quoted = "第三条目标内容"
    out = check_range(
        S(body), quoted,
        [("- 第一条内容。\n- 第二条内容。\n- 第三条目标内容。", "整节重写")],
        RangeRailConfig(),
    )
    assert out.verdict is RailVerdict.OUT_OF_RANGE


def test_check_range_allows_the_sentence_when_scope_says_so():
    body = "前一句。目标词所在的句子还有别的字。后一句。"
    out = check_range(
        S(body), "目标词",
        [("目标词所在的句子还有别的字。", "新句子。")],
        RangeRailConfig(), scope="sentence",
    )
    assert out.ok, out.detail


def test_a_markless_hard_truncated_quote_is_refused_at_exact_only():
    """Feishu cuts a quote at exactly 128 code points with no mark. At exact scope
    the fragment must not anchor as if complete; at a structural scope the geometry
    computes the bounds from the anchored start, so the cut stops mattering."""
    line = "".join(f"[{i:03d}]abcdefghijklmno" for i in range(0, 300, 20))[:300]
    body = line + "\n尾行。"
    quoted = line[:128]  # the platform-truncated prefix of a longer selection
    refused = check_range(
        S(body), quoted, [(line[:20], "yyyyy")],
        RangeRailConfig(quote_hard_limit=128),
    )
    assert refused.verdict is RailVerdict.QUOTE_TRUNCATED
    allowed = check_range(
        S(body), quoted, [(line, "整行重写")],
        RangeRailConfig(quote_hard_limit=128), scope="line",
    )
    assert allowed.ok, allowed.detail


# ----------------------------------------- review round: geometry and gate fixes



def _sheet_snap():
    body = "\n".join("\t".join(f"r{r}c{c}" for c in range(3)) for r in range(5))
    segs = []
    for r in range(5):
        for c in range(3):
            cell = f"r{r}c{c}"
            start = body.index(cell)
            segs.append(Segment(char_start=start, char_end=start+len(cell),
                                address=f"S!{chr(65+c)}{r+1}"))
    return DocSnapshot(doc_id="d", kind="spreadsheet", revision_id=None,
                       text=body, segments=tuple(segs))


def test_structural_scopes_never_cross_the_anchored_cell():
    """A sheet's flat text has no blank line anywhere, so textual paragraph bounds
    were the whole document (reproduced: 869/869 chars from a one-cell selection).
    On grid formats the anchored segment IS the structural unit."""
    snap = _sheet_snap()
    for scope in ("sentence", "line", "paragraph"):
        out = check_range(snap, "r2c1", [("r2c1", "x")], RangeRailConfig(), scope=scope)
        assert out.ok, (scope, out.detail)
        assert snap.text[out.window_lo:out.window_hi] == "r2c1", scope
    beyond = check_range(snap, "r2c1", [("r2c1\tr2c2", "x")],
                         RangeRailConfig(), scope="paragraph")
    assert beyond.verdict is RailVerdict.OUT_OF_RANGE


def test_a_junk_scope_string_keeps_every_exact_gate():
    """scope="Exact" walked past the truncation gates while getting exact geometry
    (reproduced). Unknown scopes normalize before any gate keys on the string."""
    body = "".join(f"[{i:03d}]abcdefghijklmno" for i in range(0, 300, 20))[:300] + "\n尾行。"
    out = check_range(S(body), body[:128], [(body[:20], "y")],
                      RangeRailConfig(quote_hard_limit=128), scope="Exact")
    assert out.verdict is RailVerdict.QUOTE_TRUNCATED


def test_marked_truncation_is_refused_at_every_scope():
    """A Google-marked quote ("…") anchors nothing; at structural scopes it used to
    fall through to an anchoring misdiagnosis telling the person to re-comment."""
    body = "第一段。\n\n第二段有内容。\n\n第三段。"
    out = check_range(S(body), "第二段有…", [("第二段", "x")],
                      RangeRailConfig(), scope="paragraph")
    assert out.verdict is RailVerdict.QUOTE_TRUNCATED


def test_paragraph_on_a_multiline_body_without_blank_lines_is_refused():
    """Structure unknown -> the window would be the whole document, which is the
    widening this rail exists to refuse. A single-line body stays allowed."""
    body = "第一行目标词内容。\n第二行。\n第三行。"
    out = check_range(S(body), "目标词", [("目标词", "x")],
                      RangeRailConfig(), scope="paragraph")
    assert out.verdict is RailVerdict.OUT_OF_RANGE
    single = "只有一段目标词的短文。"
    ok = check_range(S(single), "目标词", [(single, "全部重写。")],
                     RangeRailConfig(), scope="paragraph")
    assert ok.ok, ok.detail


def test_the_sentence_tier_reaches_the_full_stop_the_selection_stopped_short_of():
    """The chat path can declare a structural tier, and this is why it must.

    Measured on a live document: a person selected a sentence and commented 「这句要解释
    清楚」. The platform recorded the selection without its final full stop, so every
    rewrite the model offered was refused -- by exactly one character -- and it kept
    shrinking until two characters fit, then bolted a parenthetical onto them. The tier
    machinery existed the whole time; only the unattended tool could reach it.

    ``exact`` still refuses, because ``exact`` is the selection and the selection really
    does end before the stop. The person's own word -- 「这句」 -- is what buys the
    sentence, and the geometry comes from code, not from the model drawing a span.
    """
    body = ("agent 没有提供回退工具，也不会替用户去覆盖或撤销历史。"
            "正因为回退机制完全掌握在用户自己手里，所以即使 agent 的某次修改不符合预期，"
            "也不会造成不可挽回的后果。")
    quoted = ("正因为回退机制完全掌握在用户自己手里，所以即使 agent 的某次修改不符合预期，"
              "也不会造成不可挽回的后果")
    snap = S(body)
    new = "回退的主动权始终在用户手里：版本历史由平台保存，随时可恢复。"
    cfg = RangeRailConfig()

    tight = check_range(snap, quoted, [(quoted + "。", new)], cfg, scope="exact")
    assert tight.verdict is RailVerdict.OUT_OF_RANGE, "exact 就是选区，句号在它之外"

    widened = check_range(snap, quoted, [(quoted + "。", new)], cfg, scope="sentence")
    assert widened.ok, widened.detail

    # And it stops at the sentence: the preceding one stays out of reach.
    over = check_range(snap, quoted,
                       [("agent 没有提供回退工具，也不会替用户去覆盖或撤销历史。", new)],
                       cfg, scope="sentence")
    assert over.verdict is RailVerdict.OUT_OF_RANGE, "放宽到句，不是放宽到段"
