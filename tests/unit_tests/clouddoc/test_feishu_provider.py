"""Offline tests for the Feishu provider's write path and document lifecycle.

What can be tested without a tenant is the part that is decisions rather than
observations: that a multi-edit batch is refused rather than half-applied, that
locating honours the window the rail approved and a document-wide replacement is not
mistaken for a scoped one, and that every write is written ahead as a receipt that is
committed only on a successful envelope. A stub stands in for the platform's shapes,
and passing these tests says nothing about whether those assumptions hold.
"""

from __future__ import annotations

import json

import pytest
from _feishu_fakes import DOC, _RefusingCli, _StubCli, provider_with

from jiuwenswarm.clouddoc.providers.base import ProviderError
from jiuwenswarm.clouddoc.providers.feishu.cli import LarkResult
from jiuwenswarm.clouddoc.providers.feishu.provider import FeishuDocsProvider


def _provider(replies: dict | None = None) -> tuple[FeishuDocsProvider, _StubCli]:
    return provider_with(FeishuDocsProvider, replies)


def _envelope(data) -> str:
    return json.dumps({"ok": True, "identity": "bot", "data": data})


class _Sink:
    def __init__(self) -> None:
        self.begun: list = []
        self.committed: list = []
        self.aborted: list = []

    def begin(self, doc_id, edits, *, highlight, source, executor=""):
        self.begun.append({"doc": doc_id, "edits": edits, "highlight": highlight,
                           "source": source, "executor": executor})
        return f"rcpt{len(self.begun)}"

    def commit(self, receipt_id, *, revision_after, highlighted=None):
        # highlighted corrects what begin() recorded: begin writes the request, and
        # only the platform knows the outcome.
        self.committed.append((receipt_id, revision_after))

    def abort(self, receipt_id, *, reason):
        self.aborted.append((receipt_id, reason))


# --------------------------------------------------------------------- writing


@pytest.mark.asyncio
async def test_a_multi_edit_batch_is_refused_at_the_provider_too():
    """The upstream check is what a person sees a sentence from; this one is the
    guarantee, so going around it cannot half-write a shared document.
    """
    p, stub = _provider()
    with pytest.raises(ProviderError) as e:
        await p.edit_batch(DOC, [("a", "b"), ("c", "d")], required_revision_id="")
    assert e.value.kind == "invalid"
    assert not stub.calls, "拒绝必须发生在任何写入之前"


@pytest.mark.asyncio
async def test_locating_honours_the_window_the_rail_approved():
    """The rail and the write must judge uniqueness by one rule, or an edit the rail
    passed is refused after a person approved it.
    """
    from jiuwenswarm.clouddoc.providers.base import (
        DocSnapshot,
        Segment,
    )

    body = "甲句。乙句。甲句。"
    snap = DocSnapshot(
        doc_id=DOC, kind="document", revision_id="", text=body,
        segments=(
            Segment(char_start=0, char_end=3, index_start=0, index_end=1),
            Segment(char_start=4, char_end=7, index_start=1, index_end=2),
            Segment(char_start=8, char_end=11, index_start=2, index_end=3),
        ),
    )
    # Ambiguous across the whole body.
    assert FeishuDocsProvider._unique_in_window(snap, "甲句", None) is False
    # Unique inside the approved window.
    assert FeishuDocsProvider._unique_in_window(snap, "甲句", (0, 4)) is True


@pytest.mark.asyncio
async def test_a_single_edit_replaces_the_block_wholesale():
    """The write says "make this block read exactly this", so its outcome does not
    depend on the platform's own matching rules.
    """
    p, stub = _provider({
        "docs +fetch": {"document": {"content": "这一句需要被改写。", "revision_id": 7}},
    })
    res = await p.edit_batch(
        DOC, [("需要被改写", "已经改好")], required_revision_id="7"
    )
    assert res.status == "applied"
    update = [c for c in stub.calls if c[:2] == ["docs", "+update"]]
    assert update, "应当发出一次 docs +update"
    assert "str_replace" in update[0]
    assert "需要被改写" in update[0] and "已经改好" in update[0]
    # And the revision must **not** travel with it. Pinning to the revision the body
    # was read at looks like the Google write control it was modelled on, and does the
    # opposite: it rebuilds from that snapshot, discarding anything newer. Leaving the
    # write at latest is what fails safe under either reading of the flag.
    assert "--revision-id" not in update[0]


@pytest.mark.asyncio
async def test_an_edit_that_cannot_be_located_reports_rather_than_guessing():
    p, _ = _provider({"docs +fetch": {"document": {"content": "完全不同的内容。"}}})
    res = await p.edit_batch(DOC, [("找不到的原文", "新文")], required_revision_id="")
    assert res.status == "locate_failed"


@pytest.mark.asyncio
async def test_the_write_lands_and_reports_that_it_could_not_highlight():
    """This used to assert a refusal, and the refusal was the wrong call.

    ``apply_for_comment`` asks for highlighting unconditionally, so raising here made
    every unattended apply on this platform fail -- the whole apply_scoped watch level
    was unusable on Feishu, from PR3 onwards, with nothing catching it because the
    suite's fake provider always accepts the flag.

    What ring ⑥ needs is that the reader can see what changed; a background colour is
    one way of showing them, not the requirement. So the write lands, the result says
    plainly that nothing was painted, and the caller spells the change out in its reply
    instead. Substituting an honest mechanism, not simulating a missing one.
    """
    p, _ = _provider({"docs +fetch": {"document": {"content": "一句话。"}}})
    res = await p.edit_batch(
        DOC, [("一句话", "改过了")], required_revision_id="", highlight=True
    )
    assert res.status == "applied"
    assert res.highlighted is False, "平台不能高亮时必须如实回报，而不是默认 True"


@pytest.mark.asyncio
async def test_discovery_degrades_instead_of_breaking_the_panel():
    """Whether a bot may enumerate is tenant policy. With no managed wiki node to
    derive a space from, or with the space listing refused (131006: the app is not a
    member), the answer is an empty list plus a reason the owner can act on -- never
    an exception that takes the panel down over a feature it does not need.

    (This used to refuse ``drive +list-files``, a verb that does not exist in the
    CLI; discovery walks ``wiki +node-get`` / ``wiki +node-list`` now.)
    """
    p, _ = _provider()
    assert await p.list_accessible_documents(known=[]) == []
    assert p._discovery_available is False
    assert "wiki" in p.discovery_reason

    p2, _ = _provider()
    p2.cli = _RefusingCli(
        "wiki +node-list", replies={"wiki +node-get": {"node": {"space_id": "7684"}}},
    )
    assert await p2.list_accessible_documents(known=["WikiNode1"]) == []
    assert p2._discovery_available is False
    assert "7684" in p2.discovery_reason and "131006" in p2.discovery_reason


@pytest.mark.asyncio
async def test_being_unable_to_own_a_document_is_explained_not_relayed():
    """Google reports this as "storage quota exceeded", which sends people to empty a
    trash folder that was never full. Whatever Feishu calls it, the useful sentence is
    the same one.
    """
    p, _ = _provider()
    p.cli = _RefusingCli("docs +create")
    with pytest.raises(ProviderError) as e:
        await p.create_document("标题")
    assert "共享给本应用" in str(e.value)
    assert p._can_own_documents is False


@pytest.mark.asyncio
async def test_a_write_records_a_receipt_before_touching_the_platform():
    """Tracking and acceptance both read receipts, so a provider that produces none
    silently removes the ability to see or undo what the agent did. Recording happens
    inside the write primitive, where a model cannot skip it.
    """
    p, _ = _provider({
        "docs +fetch": {"document": {"content": "这一句需要被改写。", "revision_id": 4}},
    })
    sink = _Sink()
    p.receipt_sink = sink
    p.receipt_meta = {"source": "batch_edit",
                      "for_comment_ids_by_old": {"需要被改写": ["c1", "c2"]}}

    res = await p.edit_batch(DOC, [("需要被改写", "已经改好")], required_revision_id="4")
    assert res.status == "applied"
    assert len(sink.begun) == 1
    assert sink.begun[0]["source"] == "batch_edit"
    # The comment map must survive into the receipt, or a revert cannot notify the
    # threads the edit answered.
    assert sink.begun[0]["edits"][0]["for_comment_ids"] == ["c1", "c2"]
    assert sink.committed and sink.committed[0][0] == "rcpt1"


@pytest.mark.asyncio
async def test_a_failed_write_aborts_its_receipt_rather_than_leaving_it_pending():
    """A pending entry means "we do not know whether this landed" and the sweep will
    chase it. A refusal is known, so it is recorded as one.
    """
    p, stub = _provider({"docs +fetch": {"document": {"content": "原文。"}}})
    sink = _Sink()
    p.receipt_sink = sink

    # Only the write fails; the read before it must still succeed, or the receipt is
    # never opened and the test would pass for the wrong reason.
    async def run_failing_update(args, *, timeout=None):
        if list(args[:2]) == ["docs", "+update"]:
            return LarkResult(stdout="", stderr='{"error":{"type":"permission"}}', code=1)
        return LarkResult(
            stdout=json.dumps({"ok": True, "identity": "bot",
                               "data": {"document": {"content": "原文。"}}}),
            stderr="", code=0,
        )

    stub.run = run_failing_update
    with pytest.raises(ProviderError):
        await p.edit_batch(DOC, [("原文", "新文")], required_revision_id="")
    assert sink.aborted, "失败的写入必须结账，不能留 pending"


@pytest.mark.asyncio
@pytest.mark.parametrize(("stderr", "code"), [
    ("request timed out", 1), ("something nobody predicted", 1), ("", -9),
], ids=["transport", "unknown", "killed-without-output"])
async def test_a_write_whose_outcome_is_unknown_leaves_its_receipt_pending(stderr, code):
    """A timeout after the request went out may have landed the edit. Aborting the
    receipt would record "nothing happened" over a change that is on the document;
    it stays pending for the sweep, which reads the document to settle it.
    """
    p, stub = _provider({"docs +fetch": {"document": {"content": "原文。"}}})
    sink = _Sink()
    p.receipt_sink = sink

    async def run_lost_update(args, *, timeout=None):
        if list(args[:2]) == ["docs", "+update"]:
            return LarkResult(stdout="", stderr=stderr, code=code)
        return LarkResult(
            stdout=json.dumps({"ok": True, "identity": "bot",
                               "data": {"document": {"content": "原文。"}}}),
            stderr="", code=0,
        )

    stub.run = run_lost_update
    with pytest.raises(ProviderError):
        await p.edit_batch(DOC, [("原文", "新文")], required_revision_id="")
    assert len(sink.begun) == 1 and sink.aborted == [] and sink.committed == []


@pytest.mark.asyncio
async def test_a_write_without_a_sink_still_goes_through():
    """Recording is unconditional when wired and refuses nothing when not: the
    fail-closed duty belongs to the caller that requires a sink, and a write must not
    die on its own bookkeeping.
    """
    p, _ = _provider({"docs +fetch": {"document": {"content": "原文。"}}})
    assert p.receipt_sink is None
    res = await p.edit_batch(DOC, [("原文", "新文")], required_revision_id="")
    assert res.status == "applied"


@pytest.mark.asyncio
async def test_clearing_a_highlight_answers_instead_of_raising():
    """Revert and the resolve-driven clearing call this unconditionally. Nothing was
    ever painted on this platform, so there is nothing to clear -- but an
    AttributeError would turn a successful undo into a crash over a decoration that
    does not exist here.
    """
    p, _ = _provider()
    out = await p.clear_highlight(DOC, ["某段文字"])
    assert out["cleared"] == 0
    assert out["missed"] == ["某段文字"]


@pytest.mark.asyncio
async def test_a_docx_has_no_region_form():
    p, _ = _provider()
    with pytest.raises(ProviderError) as err:
        await p.write_regions(DOC, [("Sheet1!A1", [["x"]])])
    assert err.value.kind == "unsupported"


@pytest.mark.asyncio
async def test_creating_a_document_teaches_the_bot_its_own_open_id():
    """whoami answers without an open_id on this tenant (spike 9's other leg), but the
    owner of a document this very call created is this identity -- learned
    mechanically, so unattended dispatch can recognise the bot's own comments.
    """
    p, _ = _provider({
        "docs +create": {"document": {"document_id": "NewTok"}},
        "drive metas": {"metas": [{"owner_id": "ou_learned"}]},
    })
    p._self_open_id = ""
    await p.create_document("t")
    assert p._is_self("ou_learned") is True


@pytest.mark.asyncio
async def test_identity_learning_never_fails_the_create():
    p, stub = _provider({"docs +create": {"document": {"document_id": "NewTok"}}})
    p._self_open_id = ""

    orig = stub.json

    async def flaky(args, *, timeout=None):
        if args[:2] == ["drive", "metas"]:
            raise ProviderError("unknown", "meta down")
        return await orig(args, timeout=timeout)
    stub.json = flaky

    assert await p.create_document("t") == "NewTok"


@pytest.mark.asyncio
async def test_a_failed_location_carries_a_reason():
    """The toolkit prints ``detail``; ``locate_failed`` with none read as "未应用
    （locate_failed）：" -- a refusal with no reason, which invites the same payload.
    """
    p, _ = _provider({"docs +fetch": {"document": {"content": "完全不同的内容。"}}})
    res = await p.edit_batch(DOC, [("找不到的原文", "新文")], required_revision_id="")
    assert res.status == "locate_failed"
    assert "找不到的原文" in res.detail


# ------------------------- review: a pasted origin is trusted only when it is the platform's


def test_a_foreign_origin_is_parsed_for_its_token_but_never_kept_as_the_address():
    p = FeishuDocsProvider()
    tok = p.parse_doc_ref("https://evil.example/docx/AbC123456789?x=1")
    assert tok == "AbC123456789"
    assert p.doc_url(tok) == tok, "外来域名不能成为文档地址"
    assert p._tenant_origin() == "", "外来域名不能成为后续创建复用的租户域"


def test_the_platforms_own_origin_is_kept():
    p = FeishuDocsProvider()
    tok = p.parse_doc_ref("https://acme.larksuite.com/docx/AbC123456789?x=1#h")
    assert p.doc_url(tok) == "https://acme.larksuite.com/docx/AbC123456789"
    assert p._tenant_origin() == "https://acme.larksuite.com"


# ------------------------- review: add_page settles its receipt only when the platform refused


def _page_provider():
    p, _ = _provider()
    p._kind_cache[DOC] = "spreadsheet"
    sink = _Sink()
    p.receipt_sink = sink
    return p, sink


@pytest.mark.asyncio
async def test_add_page_returns_the_receipt_with_the_address(monkeypatch):
    from jiuwenswarm.clouddoc.providers.base import PageAdded
    from jiuwenswarm.clouddoc.providers.feishu import formats as feishu_formats

    async def created(prov, doc_ref, title):
        return "台账"

    monkeypatch.setitem(feishu_formats.ADD_PAGE, "spreadsheet", created)
    p, sink = _page_provider()
    out = await p.add_page(DOC, "台账")
    assert out == PageAdded(address="台账", receipt_id="rcpt1")
    assert [c[0] for c in sink.committed] == ["rcpt1"] and sink.aborted == []


@pytest.mark.asyncio
async def test_add_page_aborts_the_receipt_on_a_refusal_the_platform_reported(monkeypatch):
    from jiuwenswarm.clouddoc.providers.feishu import formats as feishu_formats

    async def refused(prov, doc_ref, title):
        raise ProviderError("forbidden", "no edit permission")

    monkeypatch.setitem(feishu_formats.ADD_PAGE, "spreadsheet", refused)
    p, sink = _page_provider()
    with pytest.raises(ProviderError) as e:
        await p.add_page(DOC, "台账")
    assert e.value.kind == "forbidden"
    assert [a[0] for a in sink.aborted] == ["rcpt1"] and sink.committed == []


@pytest.mark.asyncio
async def test_add_page_leaves_the_receipt_pending_when_the_outcome_is_unknown(monkeypatch):
    """A timeout after the request went out may have created the page. Closing that
    receipt as aborted would say "nothing happened" and invite the retry that makes
    two pages; it stays pending for the sweep, as the write-ahead contract promises.
    """
    from jiuwenswarm.clouddoc.providers.feishu import formats as feishu_formats

    async def lost(prov, doc_ref, title):
        raise ProviderError("transport", "timed out")

    monkeypatch.setitem(feishu_formats.ADD_PAGE, "spreadsheet", lost)
    p, sink = _page_provider()
    with pytest.raises(ProviderError) as e:
        await p.add_page(DOC, "台账")
    assert e.value.kind == "transport"
    assert len(sink.begun) == 1 and sink.aborted == [] and sink.committed == []

    async def crashed(prov, doc_ref, title):
        raise RuntimeError("connection reset")

    monkeypatch.setitem(feishu_formats.ADD_PAGE, "spreadsheet", crashed)
    with pytest.raises(ProviderError) as e:
        await p.add_page(DOC, "台账")
    assert e.value.kind == "unknown"
    assert len(sink.begun) == 2 and sink.aborted == [] and sink.committed == []


# ------------------------- review: a persisted foreign link does not re-enter the cache


@pytest.mark.asyncio
async def test_note_url_refuses_a_foreign_origin_and_canonical_url_does_not_echo_it():
    """``prime_provider_kinds`` feeds every persisted link into ``note_url``; a foreign
    one that reached the store earlier must not come back through ``doc_url``,
    ``canonical_url`` or the tenant origin later creates reuse.
    """
    p, _ = _provider()
    p.note_url(DOC, "https://evil.example/docx/DocToken123456")
    assert p.doc_url(DOC) == DOC
    assert p._tenant_origin() == ""
    assert await p.canonical_url(DOC) == DOC, "缓存里没有它，元数据也没答，只剩 token"


def test_note_url_keeps_the_platforms_own_link():
    p, _ = _provider()
    p.note_url(DOC, "https://acme.feishu.cn/docx/DocToken123456?from=x")
    assert p.doc_url(DOC) == "https://acme.feishu.cn/docx/DocToken123456"
    assert p._tenant_origin() == "https://acme.feishu.cn"


@pytest.mark.asyncio
async def test_a_window_cannot_scope_a_document_wide_replacement():
    """``str_replace`` carries no position, so approving the second "old" would send
    the same command as approving the first. Text unique only inside the window is
    refused rather than written, and nothing is submitted.
    """
    p, stub = _provider({"docs +fetch": {"document": {"content": "old / old"}}})
    res = await p.edit_batch(DOC, [("old", "new")], required_revision_id="", window=(6, 9))
    assert res.status == "locate_failed"
    assert "全文" in res.detail
    assert not [c for c in stub.calls if c[:2] == ["docs", "+update"]]


@pytest.mark.asyncio
async def test_text_unique_in_the_body_and_inside_the_window_is_written():
    p, stub = _provider({"docs +fetch": {"document": {"content": "keep / old"}}})
    res = await p.edit_batch(DOC, [("old", "new")], required_revision_id="", window=(7, 10))
    assert res.status == "applied"
    assert [c for c in stub.calls if c[:2] == ["docs", "+update"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("stdout,aborted", [
    (json.dumps({"ok": False, "error": {"type": "permission", "message": "denied"}}), True),
    ("Updated.", False),
], ids=["ok-false-is-a-refusal", "no-envelope-is-unknown"])
async def test_a_write_is_committed_only_on_a_successful_envelope(stdout, aborted):
    """Exit 0 is not success by itself: ``ok: false`` on stdout is a refusal and
    closes the receipt as aborted; output that is no envelope at all says nothing
    about the edit and leaves it pending. Neither is committed as applied.
    """
    p, stub = _provider({"docs +fetch": {"document": {"content": "原文。"}}})
    sink = _Sink()
    p.receipt_sink = sink

    async def run(args, *, timeout=None):
        if list(args[:2]) == ["docs", "+update"]:
            return LarkResult(stdout=stdout, stderr="", code=0)
        return LarkResult(stdout=_envelope({"document": {"content": "原文。"}}),
                          stderr="", code=0)

    stub.run = run
    with pytest.raises(ProviderError):
        await p.edit_batch(DOC, [("原文", "新文")], required_revision_id="")
    assert not sink.committed
    assert bool(sink.aborted) is aborted


# ------------------------------- confirm_absent tells "gone" from "could not ask"


# --------------------------- review: confirm_absent tells "gone" from "could not ask"


@pytest.mark.asyncio
async def test_a_deleted_document_is_confirmed_absent():
    p, stub = _provider()
    stub.fail_with = ProviderError("not_found", "该文档已在回收站（平台：file has been deleted）")
    assert await p.confirm_absent(DOC) is True


@pytest.mark.asyncio
async def test_a_transport_failure_never_confirms_absence():
    """The panel retires on True. ``_resolve_meta`` answered ``{}`` for every failure,
    so a timeout or a missing binary read as "the token names nothing" and a live
    document was retired on a hiccup -- the destructive-on-ambiguous shape fixed at the
    panel, still open one layer down.
    """
    p, stub = _provider()
    stub.fail_with = ProviderError("transport", "lark-cli 超时（60s）")
    assert await p.confirm_absent(DOC) is False


@pytest.mark.asyncio
async def test_a_20008_is_not_a_confirmation_either():
    p, stub = _provider()
    stub.fail_with = ProviderError(
        "not_found", "平台错误码 20008：字面文本「The user does not exist」与实际原因无关。",
    )
    assert await p.confirm_absent(DOC) is False


@pytest.mark.asyncio
async def test_a_platform_answer_that_names_nothing_confirms_absence():
    p, _ = _provider({"drive metas": {"metas": [], "failed_list": [{"code": 970005}]}})
    assert await p.confirm_absent(DOC) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    None,
    {},
    {"metas": []},
    {"metas": [], "failed_list": [{"code": 970003}]},
    {"metas": [], "failed_list": [{"code": 970005}, {"code": 970003}]},
], ids=["none", "empty", "no-failures", "no-permission", "mixed"])
async def test_absence_needs_the_platforms_positive_not_found(answer):
    """``confirm_absent`` feeds an irreversible retirement. Only "no record under any
    type, and every listed failure is record-not-found" is a verdict; an empty,
    unfamiliar or permission-shaped answer keeps the document.
    """
    class _Answering(_StubCli):
        async def json(self, args, *, timeout=None):
            return answer

    p, _ = _provider()
    p.cli = _Answering()
    assert await p.confirm_absent(DOC) is False


@pytest.mark.asyncio
async def test_sharing_takes_the_contracts_keyword():
    # A caller written against DocProvider passes ``email=``; the Feishu override
    # must accept the same name, or that call fails on this provider only.
    p, _ = provider_with(FeishuDocsProvider, {})
    asked: list[list[str]] = []

    async def drive_json(doc_ref, make_args):
        asked.append(make_args("docx"))
        return {}

    p._drive_json = drive_json
    await p.share_document(DOC, email="ou_abc", role="writer")
    args = asked[0]
    assert args[args.index("--member-id") + 1] == "ou_abc"
    assert args[args.index("--member-type") + 1] == "openid"


@pytest.mark.asyncio
async def test_sharing_posture_lists_named_members_not_audience_types():
    # Pins the documented difference from the base contract: the first element is
    # a member, and nothing here stands for link sharing. A consumer wired later
    # must read it that way, not look for an "anyone" row.
    p, _ = provider_with(FeishuDocsProvider, {})

    async def drive_json(doc_ref, make_args):
        assert make_args("docx")[:2] == ["drive", "+member-list"]
        return {"members": [{"member_id": "ou_a", "perm": "edit"},
                            {"open_id": "ou_b", "role": "view"}, {"perm": "edit"}]}

    p._drive_json = drive_json
    assert await p.sharing_posture(DOC) == [("ou_a", "edit"), ("ou_b", "view")]


# --------------------------------------------------------------------- regions

SHEET_URL = "https://x.feishu.cn/sheets/SheetTok12345"


_SHEET_REPLIES = {
    "sheets +workbook-info": {
        "sheets": [{"title": "Sheet1",
                    "grid_properties": {"row_count": 3, "column_count": 2}}],
    },
    # The live shape (TENANT-VERIFY 2026-09-02): the grid nests inside ranges[0]
    # with the actual_range the platform really answered.
    "sheets +cells-get": {
        "ranges": [{
            "actual_range": "A1:B3",
            "cells": [["项目", "状态"], ["区域机械", "待验证"], ["回执链", "待验证"]],
        }],
    },
    "sheets +cells-set": {},
}


class _RegionSink(_Sink):
    def __init__(self) -> None:
        super().__init__()
        self.unverified: list = []

    def mark_unverified(self, receipt_id, *, detail):
        self.unverified.append((receipt_id, detail))


class _EchoingSheetCli(_StubCli):
    """After a write, the platform shows what was written; the stub does too, or the
    read-back verification under test would always disagree with itself.
    """

    async def json(self, args, *, timeout=None):
        data = await super().json(args, timeout=timeout)
        if args[:2] == ["sheets", "+cells-set"]:
            self._replies["sheets +cells-get"] = {
                "ranges": [{
                    "actual_range": "A1:B3",
                    "cells": [["项目", "状态"], ["区域机械", "已验证"], ["回执链", "待验证"]],
                }],
            }
        return data


@pytest.mark.asyncio
async def test_a_sheets_link_teaches_the_format():
    """A pasted link is the only place a spreadsheet announces itself: there is no
    listing on this platform to learn it from, and without the marker the token would
    read as a docx and fail on the fetch.
    """
    p, _ = _provider()
    ref = p.parse_doc_ref(SHEET_URL)
    assert ref == "SheetTok12345"
    assert await p.doc_kind(ref) == "spreadsheet"
    assert p._drive_type(ref) == "sheet"


@pytest.mark.asyncio
async def test_a_region_write_lands_with_a_sheet_qualified_receipt():
    p = FeishuDocsProvider(profile="p", self_open_id="ou_bot")
    stub = _EchoingSheetCli(dict(_SHEET_REPLIES))
    p.cli = stub
    sink = _RegionSink()
    p.receipt_sink = sink
    ref = p.parse_doc_ref(SHEET_URL)

    res = await p.write_regions(ref, [("Sheet1!B2", [["已验证"]])])
    assert res.status == "applied"
    written = next(c for c in stub.calls if c[:2] == ["sheets", "+cells-set"])
    assert "--sheet-name" in written and written[written.index("--sheet-name") + 1] == "Sheet1"
    assert written[written.index("--range") + 1] == "B2:B2"
    # The receipt's two ends are the region's content before and after, and the
    # address is sheet-qualified so the revert's anchor check can read it back.
    entry = sink.begun[0]["edits"][0]
    assert entry["region"] == "Sheet1!B2:B2"
    assert entry["old_grid"] == [["待验证"]]
    assert (entry["old"], entry["new"]) == ("待验证", "已验证")
    assert sink.committed and not sink.unverified


@pytest.mark.asyncio
async def test_a_multi_region_batch_is_refused_because_it_cannot_be_atomic():
    """The platform's only multi-range write is fail-fast without rollback, so a
    cross-rectangle move sent as one batch can land half. Declared, not simulated.
    """
    p, _ = _provider(dict(_SHEET_REPLIES))
    ref = p.parse_doc_ref(SHEET_URL)
    res = await p.write_regions(ref, [("Sheet1!A1", [["x"]]), ("Sheet1!B1", [["y"]])])
    assert res.status == "refused"
    assert "原子" in res.detail


@pytest.mark.asyncio
async def test_a_region_whose_shape_disagrees_is_refused():
    p, _ = _provider(dict(_SHEET_REPLIES))
    ref = p.parse_doc_ref(SHEET_URL)
    with pytest.raises(ProviderError) as err:
        await p.write_regions(ref, [("Sheet1!A1:B1", [["only one"]])])
    assert "形状" in str(err.value)


@pytest.mark.asyncio
async def test_a_formula_cell_anywhere_in_the_region_refuses_the_batch():
    """Writing over a formula replaces it with a literal that looks identical, and
    nothing on the document shows it happened (IC-7).
    """
    replies = dict(_SHEET_REPLIES)
    replies["sheets +cells-get"] = {
        "ranges": [{
            "actual_range": "A1:B3",
            "cells": [["项目", "状态"],
                      ["区域机械", {"value": "待验证", "formula": "=A1&\"!\""}],
                      ["回执链", "待验证"]],
        }],
    }
    p, stub = _provider(replies)
    ref = p.parse_doc_ref(SHEET_URL)
    with pytest.raises(ProviderError) as err:
        await p.write_regions(ref, [("Sheet1!A2:B2", [["区域机械", "已验证"]])])
    assert "公式" in str(err.value)
    assert not any(c[:2] == ["sheets", "+cells-set"] for c in stub.calls)


@pytest.mark.asyncio
async def test_a_read_back_that_disagrees_demotes_the_receipt():
    """D19 tier 3b: "the platform accepted the request" and "the document shows the
    result" are different facts, and acceptance must read the second.
    """
    p, _ = _provider(dict(_SHEET_REPLIES))  # the stub never echoes the write back
    sink = _RegionSink()
    p.receipt_sink = sink
    ref = p.parse_doc_ref(SHEET_URL)
    res = await p.write_regions(ref, [("Sheet1!B2", [["已验证"]])])
    assert res.status == "applied"
    assert sink.unverified and sink.unverified[0][0] == "rcpt1"


@pytest.mark.asyncio
async def test_region_reading_answers_in_receipt_form():
    p, _ = _provider(dict(_SHEET_REPLIES))
    ref = p.parse_doc_ref(SHEET_URL)
    got = await p.read_regions(ref, ["Sheet1!B2", "Sheet1!A1:B1"])
    assert got == ["待验证", "项目\t状态"]


# ------------------------------------------------------ presentation (slides)

_SLIDE_XML = (
    '<slide id="p1">\n  <style/>\n  <data>\n'
    '    <shape width="800" height="100" type="text" id="sTitle">'
    '<content textType="title"><p>标题形状</p></content></shape>\n'
    '    <shape width="800" height="200" type="text" id="sBody">'
    '<content fontSize="16"><p>正文甲行一</p><p>正文甲行二</p></content></shape>\n'
    '  </data>\n  <note id="sNote"><content><p>讲稿</p></content></note>\n</slide>'
)


_DECK_XML = (
    '<presentation xmlns="https://www.larkoffice.com/sml/2.0">' + _SLIDE_XML + "</presentation>"
)
_DECK_REPLIES = {
    "slides +xml-get": {"slide": {"content": _SLIDE_XML},
                        "xml_presentation": {"content": _DECK_XML}},
    "slides +replace-slide": {},
}


@pytest.mark.asyncio
async def test_a_slides_link_teaches_the_format():
    p, _ = _provider()
    ref = p.parse_doc_ref("https://x.feishu.cn/slides/DeckTok12345")
    assert await p.doc_kind(ref) == "presentation"
    assert p._drive_type(ref) == "slides"


@pytest.mark.asyncio
async def test_a_deck_flattens_one_run_per_shape_with_page_shape_addresses():
    p, _ = _provider(dict(_DECK_REPLIES))
    ref = p.parse_doc_ref("https://x.feishu.cn/slides/DeckTok12345")
    snap = await p.read(ref)
    by = {s.address: snap.text[s.char_start:s.char_end] for s in snap.segments}
    assert by["p1/sTitle"] == "标题形状"
    assert by["p1/sBody"] == "正文甲行一\n正文甲行二"
    assert by["p1/sNote"] == "讲稿"  # notes flatten too, behind the divider


@pytest.mark.asyncio
async def test_a_shape_region_write_swaps_text_and_keeps_the_element():
    """The shape's full XML is sent with only its text nodes swapped: styling and
    geometry survive, and the receipt carries the shape's before-text.
    """
    p = FeishuDocsProvider(profile="p", self_open_id="ou_bot")
    p.cli = _StubCli(dict(_DECK_REPLIES))
    sink = _RegionSink()
    p.receipt_sink = sink
    ref = p.parse_doc_ref("https://x.feishu.cn/slides/DeckTok12345")
    res = await p.write_regions(ref, [("p1/sTitle", [["新标题"]])])
    assert res.status == "applied"
    sent = next(c for c in p.cli.calls if c[:2] == ["slides", "+replace-slide"])
    parts = json.loads(sent[sent.index("--parts") + 1])
    assert parts[0]["target_id"] == "sTitle"
    assert "新标题" in parts[0]["shape"] and 'textType="title"' in parts[0]["shape"]
    entry = sink.begun[0]["edits"][0]
    assert entry["region"] == "p1/sTitle" and entry["old_grid"] == [["标题形状"]]


@pytest.mark.asyncio
async def test_cross_slide_regions_are_refused_as_unatomic():
    p, _ = _provider(dict(_DECK_REPLIES))
    ref = p.parse_doc_ref("https://x.feishu.cn/slides/DeckTok12345")
    res = await p.write_regions(ref, [("p1/sTitle", [["x"]]), ("p2/sOther", [["y"]])])
    assert res.status == "refused" and "同一页" in res.detail


@pytest.mark.asyncio
async def test_a_deck_text_replacement_refuses_with_directions():
    """Without this the docx fallback would receive a slides token and fail with the
    platform's document-flavored error -- the wrong repair path.
    """
    p, stub = _provider(dict(_DECK_REPLIES))
    ref = p.parse_doc_ref("https://x.feishu.cn/slides/DeckTok12345")
    res = await p.edit_batch(ref, [("标题形状", "新")], required_revision_id="")
    assert res.status == "refused" and "区域" in res.detail
    assert not any(c[:2] == ["docs", "+update"] for c in stub.calls)


@pytest.mark.asyncio
async def test_a_vanished_shape_refuses_the_revert_read():
    p, _ = _provider(dict(_DECK_REPLIES))
    ref = p.parse_doc_ref("https://x.feishu.cn/slides/DeckTok12345")
    with pytest.raises(ProviderError) as err:
        await p.read_regions(ref, ["p1/sGone"])
    assert "不在幻灯片" in str(err.value)


class _FailingWriteCli(_StubCli):
    """Answers every read, and fails the one write verb with the given kind."""

    def __init__(self, replies: dict, verb: list[str], kind: str) -> None:
        super().__init__(replies)
        self._verb, self._kind = verb, kind

    async def json(self, args, *, timeout=None):
        if list(args[:2]) == self._verb:
            self.calls.append(list(args))
            raise ProviderError(self._kind, "the write failed")
        return await super().json(args, timeout=timeout)


_FORMAT_WRITES = {
    "sheet-text": (SHEET_URL, _SHEET_REPLIES, ["sheets", "+cells-set"],
                   lambda p, ref: p.edit_batch(ref, [("区域机械", "已改")], required_revision_id="")),
    "sheet-region": (SHEET_URL, _SHEET_REPLIES, ["sheets", "+cells-set"],
                     lambda p, ref: p.write_regions(ref, [("Sheet1!B2", [["已验证"]])])),
    "slide-region": ("https://x.feishu.cn/slides/DeckTok12345", _DECK_REPLIES,
                     ["slides", "+replace-slide"],
                     lambda p, ref: p.write_regions(ref, [("p1/sTitle", [["新标题"]])])),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("write", sorted(_FORMAT_WRITES))
@pytest.mark.parametrize("kind,closed", [("forbidden", True), ("transport", False)])
async def test_a_format_write_closes_its_receipt_only_when_the_platform_refused(write, kind, closed):
    """A refusal is known not to have landed, so its receipt is aborted. A timeout
    after the request went out may have landed it, so the receipt stays pending for
    the sweep -- the rule the docx path and add_page follow.
    """
    url, replies, verb, do_write = _FORMAT_WRITES[write]
    p = FeishuDocsProvider(profile="p", self_open_id="ou_bot")
    p.cli = _FailingWriteCli(dict(replies), verb, kind)
    sink = _RegionSink()
    p.receipt_sink = sink
    with pytest.raises(ProviderError):
        await do_write(p, p.parse_doc_ref(url))
    assert len(sink.begun) == 1 and sink.committed == []
    assert bool(sink.aborted) is closed


# ------------------------------------------ review round: slides swap integrity

_COMPLEX_SLIDE = (
    '<slide id="p1"><data>'
    '<shape type="text" id="sTwo">'
    '<content textType="title"><p>头</p></content>'
    '<content><p>尾</p></content></shape>'
    '</data></slide>'
)


@pytest.mark.asyncio
async def test_a_shape_with_several_content_elements_refuses_the_swap():
    """The swap writes into the first content's direct <p> children; a shape holding
    text anywhere else would get its new text appended while the old stayed --
    visible duplication and an inverse that no longer anchors. Refused instead.
    """
    p = FeishuDocsProvider(profile="p", self_open_id="ou_bot")
    p.cli = _StubCli({"slides +xml-get": {"slide": {"content": _COMPLEX_SLIDE}},
                      "slides +replace-slide": {}})
    ref = p.parse_doc_ref("https://x.feishu.cn/slides/DeckTok99999")
    with pytest.raises(ProviderError) as err:
        await p.write_regions(ref, [("p1/sTwo", [["新文本"]])])
    assert "较复杂" in str(err.value)
    assert not any(c[:2] == ["slides", "+replace-slide"] for c in p.cli.calls)


@pytest.mark.asyncio
async def test_grouped_shapes_flatten_each_leaf_exactly_once():
    """A group's text lives in its child shapes; emitting the parent too duplicated
    every grouped text in the flat, and quotes on it failed the uniqueness anchor.
    """
    xml = ('<slide id="p1"><data>'
           '<shape type="group" id="g1">'
           '<shape type="text" id="child1"><content><p>组内文字</p></content></shape>'
           '</shape>'
           '</data></slide>')
    p = FeishuDocsProvider(profile="p", self_open_id="ou_bot")
    p.cli = _StubCli({"slides +xml-get": {
        "slide": {"content": xml},
        "xml_presentation": {"content": "<presentation>" + xml + "</presentation>"}}})
    ref = p.parse_doc_ref("https://x.feishu.cn/slides/DeckTok88888")
    snap = await p.read(ref)
    assert snap.text.count("组内文字") == 1
    assert [g.address for g in snap.segments] == ["p1/child1"]


@pytest.mark.asyncio
async def test_an_untitled_slide_is_refused_because_the_platform_folds_identical_pages():
    """Measured: three byte-identical ``+add-slide`` requests answered one slide id and
    added one page. An untitled page is the same request every time, so it is refused
    up front with the reason rather than answering an id that already existed.
    """
    p, stub = _provider(dict(_DECK_REPLIES))
    ref = p.parse_doc_ref("https://x.feishu.cn/slides/DeckTok12345")
    with pytest.raises(ProviderError) as err:
        await p.add_page(ref, "")
    assert "title" in str(err.value)
    assert not any(c[:2] == ["slides", "+add-slide"] for c in stub.calls)
