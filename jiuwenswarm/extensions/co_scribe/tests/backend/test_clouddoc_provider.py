"""Offline tests for the clouddoc provider.

Everything runs against **recorded documents.get JSON** and never touches the network.
The fixture was captured from a real document and contains tables, bullets, an inline
image, a footnote reference, CJK text and a surrogate pair -- exactly the cases
flattening is most likely to get wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jiuwenswarm.extensions.co_scribe.backend.host.watch.triggers import normalize

from jiuwenswarm.extensions.co_scribe.backend.toolkit import ProviderError
from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.google_provider import (
    GoogleDocsProvider,
    _classify,
    _utf16_len,
)

FIXTURES = Path(__file__).parent / "fixtures" / "clouddoc"


@pytest.fixture
def mixed_doc() -> dict:
    return json.loads((FIXTURES / "mixed_elements.documents_get.json").read_text(encoding="utf-8"))


@pytest.fixture
def provider() -> GoogleDocsProvider:
    # No network: these cases use only pure functions and flattening, never reaching
    # _clients()
    return GoogleDocsProvider("/nonexistent/credentials.json")


# --------------------------------------------------------------- flattening


def test_flatten_is_option_c(provider, mixed_doc):
    """Flattening must emit no characters for non-text elements and join table cells with
    newlines.

    Measurement settled this as the only shape that makes quotedFileContent a literal
    substring. Emitting a U+E907 placeholder, or joining cells with tabs, puts the quote
    out of alignment.
    """
    text, _ = provider._flatten(mixed_doc)

    # A non-text element emits not one character
    assert "" not in text
    assert "A 普通段落 with 中文." in text  # 内联图在「段落」和「with」之间，不留痕
    assert "F 脚注宿主句。" in text  # 脚注引用同样不留痕

    # Table cells enter the stream, joined by newlines
    assert "TA\nTB\nTC\nTD" in text
    assert "TA\tTB" not in text

    # Bullet glyphs stay out of the text
    assert "C 列表项甲" in text
    assert "•" not in text


def test_flatten_matches_recorded_quote(provider, mixed_doc):
    """For a selection spanning every element type, its quotedFileContent is a **unique**
    substring of the flat text.

    The quoted text is the real value recorded from Drive at the same moment. This
    assertion is the foundation of the range rail's exact-substring-plus-uniqueness rule.
    """
    quote = (
        "A 普通段落 with 中文.\n"
        "B 这句里的 加粗词 是粗体。\n"
        "C 列表项甲\n"
        "D 列表项乙\n"
        "E 含 emoji 🎯 代理对。\n"
        "F 脚注宿主句。\n"
        "G 表格前段落\n"
        "TA\nTB\nTC\nTD\n"
        "。"
    )
    text, _ = provider._flatten(mixed_doc)
    assert quote in text
    assert text.count(quote) == 1


# --------------------------------------------------------------- index mapping


def test_index_space_diverges_from_char_space(provider, mixed_doc):
    """Non-text elements occupy indices without contributing characters, so the two spaces
    necessarily diverge -- which is why segments exist."""
    text, segments = provider._flatten(mixed_doc)
    doc_end = mixed_doc["body"]["content"][-1]["endIndex"]
    assert doc_end > len(text), "若两者相等说明扁平化把非文本元素也算成了字符"

    # Without surrogate pairs, each segment's character length equals its index span
    for seg in segments:
        chars = text[seg.char_start : seg.char_end]
        assert seg.index_end - seg.index_start == _utf16_len(chars)


def test_char_to_index_across_surrogate_pair(provider, mixed_doc):
    """A surrogate pair is one character and two indices, so the conversion has to go through
    UTF-16."""
    text, segments = provider._flatten(mixed_doc)
    snap = type("S", (), {"text": text, "segments": segments})()

    needle = "代理对"  # 位于 emoji 之后
    c0 = text.index(needle)
    i0, i1 = GoogleDocsProvider._chars_to_indices(snap, c0, c0 + len(needle))
    assert i1 - i0 == _utf16_len(needle)

    emoji_at = text.index("🎯")
    e0, e1 = GoogleDocsProvider._chars_to_indices(snap, emoji_at, emoji_at + 1)
    assert e1 - e0 == 2, "代理对必须占两个文档索引"


def test_locate_requires_uniqueness(provider, mixed_doc):
    text, segments = provider._flatten(mixed_doc)
    snap = type("S", (), {"text": text, "segments": segments})()

    with pytest.raises(ProviderError) as ei:
        provider._locate(snap, "这段文字不存在")
    assert ei.value.kind == "invalid"

    with pytest.raises(ProviderError) as ei2:
        provider._locate(snap, "\n")  # 换行显然不唯一
    assert "not unique" in str(ei2.value)


# --------------------------------------------------------------- error classification


class _FakeResp:
    def __init__(self, status: int) -> None:
        self.status = status


class _FakeHttpError(Exception):
    def __init__(self, status: int, body: dict) -> None:
        super().__init__("boom")
        self.resp = _FakeResp(status)
        self.content = json.dumps(body).encode()


def test_stale_revision_and_invalid_request_are_both_400_but_distinguishable():
    """Both carry the same status code, so only the message text tells them apart.

    Getting it wrong issues a false "applied" confirmation for an edit that never landed
    -- this is the startup sweep's only criterion for its third class.
    """
    stale = _FakeHttpError(
        400, {"error": {"message": "The required revision ID 'AIroW3xyz' is not the latest"}}
    )
    malformed = _FakeHttpError(
        400, {"error": {"message": "Invalid requests[2].deleteContentRange: Index 999999 ..."}}
    )
    assert _classify(stale).kind == "conflict"
    assert _classify(malformed).kind == "invalid"


def test_rate_limit_is_not_a_permission_failure():
    """Drive reports throttling as a 403 userRateLimitExceeded.

    Classifying by status code would treat a throttled document as a permission failure
    and freeze it permanently -- and since quota is shared globally, one person hammering
    one document would freeze all of them.
    """
    throttled = _FakeHttpError(
        403, {"error": {"message": "User rate limit exceeded",
                        "errors": [{"reason": "userRateLimitExceeded"}]}}
    )
    denied = _FakeHttpError(
        403, {"error": {"message": "Insufficient permissions",
                        "errors": [{"reason": "insufficientFilePermissions"}]}}
    )
    assert _classify(throttled).kind == "rate_limited"
    assert _classify(denied).kind == "forbidden"
    assert _classify(_FakeHttpError(429, {"error": {"message": "too many"}})).kind == "rate_limited"


def test_drive_404_has_no_status_field():
    """Drive's error body carries no status field, and the classifier must not assume one."""
    assert _classify(_FakeHttpError(404, {"error": {"message": "Comment not found: abc"}})).kind == "not_found"


# --------------------------------------------------------------- other pure functions


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("同意", "同意"),
        ("  同意  ", "同意"),
        ("同意​", "同意"),   # 零宽空格
        ("同意 ", "同意"),   # NBSP
        ("ＯＫ", "ok"),            # 全角 + 大小写
        ("", ""),
    ],
)
def test_keyword_normalization(raw, expected):
    assert normalize(raw) == expected


def test_keyword_normalization_does_not_swallow_suffix():
    """An approval word with a tail must still differ from the word itself after
    normalization; failure points toward safety."""
    assert normalize("同意，但第二处再改改") != normalize("同意")


@pytest.mark.parametrize(
    "raw",
    [
        "https://docs.google.com/document/d/1AAAAAAAAAAAAAAAAAAAAA/edit#heading=h.x",
        "https://drive.google.com/open?id=1AAAAAAAAAAAAAAAAAAAAA",
        "1AAAAAAAAAAAAAAAAAAAAA",
    ],
)
def test_parse_doc_ref_forms(provider, raw):
    assert provider.parse_doc_ref(raw) == "1AAAAAAAAAAAAAAAAAAAAA"


def test_parse_doc_ref_rejects_garbage(provider):
    with pytest.raises(ProviderError) as ei:
        provider.parse_doc_ref("not a link")
    assert ei.value.kind == "invalid"


# ------------------------------------------------------------ the quote's coordinate system
#
# A production incident: Drive's quotedFileContent is HTML-escaped and the body is not.
# The two are used as one coordinate system -- anchor_quote does a literal substring
# match, ctx_hash takes a window by code point -- so any selection containing a quote mark
# fails to anchor. The failure reads "cannot be located uniquely, please comment again",
# and no amount of retrying changes it.


@pytest.mark.parametrize(
    "raw,want",
    [
        ("人维护静态本体&quot;设计", '人维护静态本体"设计'),
        ("A &amp; B", "A & B"),
        ("&lt;tag&gt;", "<tag>"),
        ("it&#39;s", "it's"),
        ("普通文本没有实体", "普通文本没有实体"),
    ],
)
def test_quoted_text_is_unescaped_into_the_body_coordinate_space(raw, want):
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.google_provider import (
        _unescape_quoted,
    )

    assert _unescape_quoted(raw) == want


def test_quoted_text_with_entities_anchors_after_unescape():
    """End to end: an escaped quote is not found in the body, and matches exactly once after
    unescaping."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.google_provider import (
        _unescape_quoted,
    )

    body = "前文。人维护静态本体\"设计\"，而 agent 需要运行时读取。后文。"
    quoted_from_api = "人维护静态本体&quot;设计&quot;"

    assert body.count(quoted_from_api) == 0, "前提：原样匹配必然失败"
    assert body.count(_unescape_quoted(quoted_from_api)) == 1


# ------------------------------------------------------------ write safety
#
# Flattening has non-text elements emit no characters, or quotedFileContent would not
# line up, but they **do occupy indices**. deleteContentRange deletes the whole index
# range, so rewriting text that spans them takes images, footnote references and people
# chips with it -- none of which the user can even see in the quote.


class _StubDocs:
    """Only needs to support the docs.documents().batchUpdate(...).execute chain."""

    def documents(self):
        return self

    def batchUpdate(self, **kwargs):
        self.last = kwargs
        return self

    def execute(self):
        return {"writeControl": {"requiredRevisionId": "rev2"}}


def _snap_from_fixture():
    import json
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.google_provider import (
        GoogleDocsProvider,
    )
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import DocSnapshot

    p = GoogleDocsProvider.__new__(GoogleDocsProvider)
    p._revision_cache = {}
    fixture = (
        "jiuwenswarm/extensions/co_scribe/tests/backend/fixtures/clouddoc/"
        "mixed_elements.documents_get.json"
    )
    with open(fixture, encoding="utf-8") as fh:
        doc = json.load(fh)
    text, segs = p._flatten(doc)
    return p, DocSnapshot(doc_id="d", kind="document", revision_id="r",
                          text=text, segments=segs)


def test_span_crossing_a_non_text_element_is_measurable():
    """A premise check: such a substring really does span more indices than its UTF-16 length.

    This assertion is what the guard below stands on. Should it stop holding -- if
    flattening changed, say -- that guard would become dead code that never fires.
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.google_provider import _utf16_len

    p, snap = _snap_from_fixture()
    crossing = None
    for a, b in zip(snap.segments, snap.segments[1:]):
        if b.index_start > a.index_end:
            crossing = snap.text[a.char_end - 3: b.char_start + 3]
            i0, i1 = p._chars_to_indices(snap, a.char_end - 3, b.char_start + 3)
            break
    assert crossing is not None, "录制数据里没有非文本元素，这组测试失去意义"
    assert (i1 - i0) > _utf16_len(crossing)


@pytest.mark.asyncio
async def test_edit_spanning_a_non_text_element_is_refused():
    """An edit spanning a non-text element must be refused rather than deleting those
    elements along with the text."""
    p, snap = _snap_from_fixture()
    crossing = None
    for a, b in zip(snap.segments, snap.segments[1:]):
        if b.index_start > a.index_end:
            crossing = snap.text[a.char_end - 3: b.char_start + 3]
            break

    result = await p._submit("d", snap, [(crossing, "替换")], "rev1")
    assert result.status == "invalid"
    assert "non-text" in result.detail


@pytest.mark.asyncio
async def test_overlapping_edits_are_refused():
    """The range rail guarantees each old_string is unique, not that two of them do not
    intersect.

    When they do, applying in descending order puts the second deletion where things have
    already shifted, cutting out unrelated text.
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import DocSnapshot, Segment

    text = "前言。这是一段可以被切坏的文字。结尾。"
    snap = DocSnapshot(doc_id="d", kind="document", revision_id="r", text=text,
                       segments=(Segment(0, len(text), 1, 1 + len(text)),))
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.google_provider import (
        GoogleDocsProvider,
    )
    p = GoogleDocsProvider.__new__(GoogleDocsProvider)
    p._revision_cache = {}

    result = await p._submit("d", snap, [("可以被切坏", "X"), ("被切坏的文字", "Y")], "rev1")
    assert result.status == "invalid"
    assert "overlap" in result.detail


@pytest.mark.asyncio
async def test_non_overlapping_edits_in_plain_text_still_pass_validation():
    """Neither guard may catch an ordinary multi-edit proposal."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import DocSnapshot, Segment

    text = "甲部分的文字。乙部分的文字。"
    snap = DocSnapshot(doc_id="d", kind="document", revision_id="r", text=text,
                       segments=(Segment(0, len(text), 1, 1 + len(text)),))
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.google_provider import (
        GoogleDocsProvider,
    )
    p = GoogleDocsProvider.__new__(GoogleDocsProvider)
    p._revision_cache = {}
    p._clients = lambda: (_StubDocs(), None)

    async def fake_call(fn, *a, **k):
        return {"writeControl": {"requiredRevisionId": "rev2"}}

    p._call = fake_call
    result = await p._submit("d", snap, [("甲部分", "A"), ("乙部分", "B")], "rev1")
    assert result.status == "applied", result.detail
    assert result.new_revision_id == "rev2"


@pytest.mark.asyncio
async def test_revision_conflict_is_never_reported_as_invalid():
    """A conflict and a malformed request must stay separate.

    Merging them issues a false "applied" confirmation for an edit that **never landed**:
    the startup sweep infers from a conflict that the first submission did succeed, so if
    invalid reached that branch too, a malformed request would be read as "already
    applied".
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.google_provider import (
        GoogleDocsProvider,
    )
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
        DocSnapshot, ProviderError, Segment,
    )

    text = "前言。这一句需要被改写。结尾。"
    snap = DocSnapshot(doc_id="d", kind="document", revision_id="r", text=text,
                       segments=(Segment(0, len(text), 1, 1 + len(text)),))
    p = GoogleDocsProvider.__new__(GoogleDocsProvider)
    p._revision_cache = {}
    p._clients = lambda: (_StubDocs(), None)

    for kind, want in (("conflict", "conflict"), ("invalid", "invalid")):
        async def boom(fn, *a, _k=kind, **kw):
            raise ProviderError(_k, f"模拟 {_k}")

        p._call = boom
        result = await p._submit("d", snap, [("需要被改写", "已改写")], "rev1")
        assert result.status == want, f"{kind} 被归成了 {result.status}"


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_writing_into_an_empty_document_is_an_insertion_not_a_replacement():
    """The empty-document path must not go through location at all.

    It nearly shipped broken: the tool sent ``("", body)`` down the ordinary edit path,
    where locating an empty needle in a document with no segments fails outright. The
    unit test meant to cover it used a fake provider and never reached this code -- a
    fake that skips the logic under test proves only that the call was made.
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
        DocSnapshot,
    )

    snap = DocSnapshot(doc_id="d", kind="document", revision_id="rev1", text="", segments=())
    stub = _StubDocs()
    p = GoogleDocsProvider.__new__(GoogleDocsProvider)
    p._revision_cache = {}
    p._clients = lambda: (stub, None)

    async def direct(fn, *a, **kw):
        return fn(*a, **kw)

    p._call = direct
    out = await p._submit("d", snap, [("", "初稿正文。")], "rev1")

    assert out.status == "applied", out.detail
    reqs = stub.last["body"]["requests"]
    assert len(reqs) == 1 and "insertText" in reqs[0], reqs
    assert reqs[0]["insertText"]["location"]["index"] == 1, "正文从索引 1 开始"
    assert all("deleteContentRange" not in r for r in reqs), "插入不该带删除"
    assert stub.last["body"]["writeControl"]["requiredRevisionId"] == "rev1"


@pytest.mark.asyncio
async def test_anchorless_write_into_a_non_empty_document_is_refused():
    """The wider capability -- insert anywhere -- must not be reachable by leaving the
    anchor out."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
        DocSnapshot,
        Segment,
    )

    snap = DocSnapshot(doc_id="d", kind="document", revision_id="rev1",
                       text="已有正文", segments=(Segment(0, 4, 1, 5),))
    p = GoogleDocsProvider.__new__(GoogleDocsProvider)
    out = await p._submit("d", snap, [("", "追加")], "rev1")
    assert out.status == "invalid"
    assert "empty document" in out.detail


async def test_unknown_provider_errors_propagate_rather_than_masquerade():
    """An error that is neither a conflict nor invalid must propagate rather than pose as
    some settled outcome."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.google_provider import (
        GoogleDocsProvider,
    )
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
        DocSnapshot, ProviderError, Segment,
    )

    text = "前言。这一句需要被改写。结尾。"
    snap = DocSnapshot(doc_id="d", kind="document", revision_id="r", text=text,
                       segments=(Segment(0, len(text), 1, 1 + len(text)),))
    p = GoogleDocsProvider.__new__(GoogleDocsProvider)
    p._revision_cache = {}
    p._clients = lambda: (_StubDocs(), None)

    async def boom(fn, *a, **kw):
        raise ProviderError("rate_limited", "quota")

    p._call = boom
    with pytest.raises(ProviderError) as ei:
        await p._submit("d", snap, [("需要被改写", "已改写")], "rev1")
    assert ei.value.kind == "rate_limited"


def test_an_unresolved_kind_falls_back_to_the_address_drive_actually_serves():
    """This assertion used to read ``/preview``, and that was wrong.

    The reasoning then was about the frame: both Drive viewers open any file, but
    ``/view`` carries ``X-Frame-Options: SAMEORIGIN`` and ``/preview`` does not
    (measured), so the fallback had to be the embeddable one or the workbench
    would show a blank frame.

    The premise was false. ``/preview`` is not an address Drive offers for every
    file: for a ``text/markdown`` file the API returns ``/view`` as its
    ``webViewLink``, and ``/preview`` answers with the "You need access" page even
    to the file's **owner** -- so the fix for a blank frame was a link accusing the
    reader of a permission problem they do not have. Both measured against a real
    file on a real account.

    The frame is no longer the reason to choose either way: every format the
    workbench embeds has an explicit entry, so this fallback is only reached by a
    format co-scribe does not serve, and those are not embedded at all.
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.google_provider import (
        GoogleDocsProvider,
    )

    prov = object.__new__(GoogleDocsProvider)
    prov._kind_cache = {}  # the cache a constructed provider would carry
    assert prov.doc_url("FILEID", "") == "https://drive.google.com/file/d/FILEID/view"

    # A kind that did resolve still gets its own editor, unchanged.
    assert prov.doc_url("FILEID", "document").endswith("/edit")


@pytest.mark.asyncio
async def test_the_first_write_into_an_empty_document_leaves_a_receipt():
    """A first write is a write, and the ledger's promise has no exception for it.

    Filling an empty document is an insertion, not a replacement, so it returns before
    the replacement path's receipt plumbing -- and used to return before recording
    anything at all. Measured on a live document: 1,668 characters landed and the ledger
    stayed empty, which makes "recorded at the write primitive so the model can neither
    skip nor disable it" false for the one write every fresh document receives, and the
    one a demo always performs.

    The pair is ``("", text)``: nothing was there, and now there is this -- which is what
    the receipt shows a reader who wants to put the document back by hand.
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.google_provider import (
        GoogleDocsProvider,
    )

    class _Sink:
        def __init__(self) -> None:
            self.begun: list[tuple] = []
            self.committed: list[str] = []

        def begin(self, *args, **kw):
            self.begun.append((args, kw))
            return "receipt-1"

        def commit(self, receipt_id, *args, **kw):
            self.committed.append(receipt_id)

        def abort(self, receipt_id, *args, **kw):  # pragma: no cover - not this path
            raise AssertionError("a successful write must not abort")

    p = GoogleDocsProvider.__new__(GoogleDocsProvider)
    p._revision_cache = {}
    p.receipt_sink = _Sink()
    p.receipt_meta = {}

    class _Execute:
        def execute(self):
            return {"writeControl": {"requiredRevisionId": "rev-2"}}

    class _Docs:
        def documents(self):
            return self

        def batchUpdate(self, **kw):  # noqa: N802 - the SDK's own spelling
            return _Execute()

    p._clients = lambda: (_Docs(), None)
    p._call = lambda fn: _run_sync(fn)

    out = await p._insert_first("doc-1", "第一版正文", None, highlight=True)

    assert out.status == "applied"
    assert p.receipt_sink.begun, "空文档首写必须先落一条待定回执"
    assert p.receipt_sink.committed == ["receipt-1"], "写成之后必须收口为已应用"
    assert out.receipt_id == "receipt-1", "回执号要回到调用方，人才能引用它"


async def _run_sync(fn):
    return fn()


# ------------------------- review: a link's origin never becomes the document's address


def test_a_foreign_origin_yields_the_id_and_a_canonical_address(provider):
    doc_id = provider.parse_doc_ref("https://evil.example/document/d/1AAAAAAAAAAAAAAAAAAAAA/edit")
    assert doc_id == "1AAAAAAAAAAAAAAAAAAAAA"
    url = provider.doc_url(doc_id, "document")
    assert url.startswith("https://docs.google.com/") and "evil" not in url
