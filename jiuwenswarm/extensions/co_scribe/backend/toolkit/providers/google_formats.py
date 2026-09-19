"""Reading and writing the formats beyond a Google Doc: sheets and slides.

One provider serves a whole connection, and a connection's service account reaches
documents of every kind, so the format cannot be chosen when the provider is built --
it is a property of the document. Each handler below answers the two questions §18.3
reduced a format to:

1. how to flatten this document so that a platform-quoted string is a **literal
   substring** of the result, and
2. how to turn a character span in that result back into one write.

Everything else -- the range rail, the union window, the quote anchor, receipts, the
mandate machinery -- is shared and untouched, because none of it reads anything but
character offsets into ``DocSnapshot.text``.

The three formats are not equally safe, and the differences are recorded on the
handlers rather than in prose someone has to remember:

* **sheets** have no revision id anywhere in the API (D12), and a cell showing ``42``
  may be a formula. Writing the edited 42 back leaves a literal where the formula was
  and the document looks unchanged -- refused in code, see IC-7 in ``textmap``.
* **slides** keep full optimistic locking and are structurally the same as documents.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.extensions.co_scribe.backend.toolkit.rails import textmap
from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
    DocRef,
    DocSnapshot,
    EditResult,
    ProviderError,
)

logger = logging.getLogger(__name__)

MIME_DOCUMENT = "application/vnd.google-apps.document"
MIME_SPREADSHEET = "application/vnd.google-apps.spreadsheet"
MIME_PRESENTATION = "application/vnd.google-apps.presentation"


def kind_for(mime: str) -> str:
    """The format name for a Drive MIME type, or "" when co-scribe cannot handle it.

    Everything else in the Drive -- a PDF, an uploaded Office file, a markdown file
    -- answers "" and lands on the panel's "shared but not supported" list. This
    function and ``provider.SUPPORTED_KINDS`` are the same statement said twice, once
    in Drive's vocabulary and once in ours.
    """
    if mime == MIME_DOCUMENT:
        return "document"
    if mime == MIME_SPREADSHEET:
        return "spreadsheet"
    if mime == MIME_PRESENTATION:
        return "presentation"
    return ""


@dataclass(frozen=True)
class FormatTraits:
    """What the shared machinery needs to know about one format, per §18.6."""

    text_domain: str
    atomic_batch: bool
    # Whether the platform can refuse a write that would land on changed content.
    # False here is a declaration, never a workaround: C9 forbids simulating a
    # capability, so a handler that compensates with a content check still says False.
    has_revision_control: bool


TRAITS = {
    "document": FormatTraits("plain", True, True),
    "spreadsheet": FormatTraits("plain", True, False),
    "presentation": FormatTraits("plain", True, True),
}


# ---------------------------------------------------------------- spreadsheet


async def read_spreadsheet(prov: Any, doc_ref: DocRef) -> DocSnapshot:
    """Flatten every sheet, reading each cell twice.

    Twice, because the two readings answer different questions and both are needed:
    ``FORMATTED_VALUE`` is what a person sees and therefore what a comment quotes, and
    ``FORMULA`` is what is actually stored. Where they differ, the cell is computed and
    must not be written (IC-7).
    """
    sheets = prov._sheets_client()
    meta = await prov._call(
        sheets.spreadsheets()
        .get(spreadsheetId=doc_ref, fields="sheets(properties(title))")
        .execute
    )
    titles = [
        (s.get("properties") or {}).get("title") or ""
        for s in meta.get("sheets") or []
    ]
    if not titles:
        return DocSnapshot(doc_id=doc_ref, kind="spreadsheet", revision_id=None, text="")

    async def _grid(render: str) -> list[list[list[str]]]:
        got = await prov._call(
            sheets.spreadsheets()
            .values()
            .batchGet(
                spreadsheetId=doc_ref,
                ranges=[_quote_sheet(t) for t in titles],
                valueRenderOption=render,
            )
            .execute
        )
        return [r.get("values") or [] for r in got.get("valueRanges") or []]

    shown = await _grid("FORMATTED_VALUE")
    stored = await _grid("FORMULA")

    rows: list[list[textmap.Cell]] = []
    for si, title in enumerate(titles):
        sheet_shown = shown[si] if si < len(shown) else []
        sheet_stored = stored[si] if si < len(stored) else []
        for ri, row in enumerate(sheet_shown):
            stored_row = sheet_stored[ri] if ri < len(sheet_stored) else []
            cells = []
            for ci, value in enumerate(row):
                raw = stored_row[ci] if ci < len(stored_row) else value
                cells.append(
                    textmap.Cell(
                        # Quoted here as well as in the read range: this address goes
                        # straight back out as the write's A1 range, so a title needing
                        # quotes would read fine and then fail on write.
                        address=f"{_quote_sheet(title)}!{_a1(ci)}{ri + 1}",
                        formatted="" if value is None else str(value),
                        formula="" if raw is None else str(raw),
                    )
                )
            rows.append(cells)

    text, segments = textmap.flatten_grid(rows)
    return DocSnapshot(
        doc_id=doc_ref,
        kind="spreadsheet",
        # The Sheets API carries no revision id or etag anywhere -- measured against
        # the full Spreadsheet schema. There is nothing to return and nothing to
        # pretend with (D12).
        revision_id=None,
        text=text,
        segments=segments,
    )


def _quote_sheet(title: str) -> str:
    """A sheet title as an A1 range reference.

    A title with a space, a quote, or any of A1 notation's own punctuation is illegal
    unquoted, and the request fails for the whole spreadsheet rather than for that
    sheet -- so a workbook with one sheet called "Q3 Data" could not be read at all.
    Single quotes inside the name double, as A1 notation requires.
    """
    if title.replace("_", "").isalnum() and not title[:1].isdigit():
        return title
    return "'" + title.replace("'", "''") + "'"


def _a1(col: int) -> str:
    """0-based column index to its A1 letters."""
    out = ""
    col += 1
    while col:
        col, rem = divmod(col - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


async def write_spreadsheet(
    prov: Any,
    doc_ref: DocRef,
    snap: DocSnapshot,
    edits: list[tuple[str, str]],
    _revision: str | None,
    window: tuple[int, int] | None = None,
) -> EditResult:
    """Rewrite whole cells, **one write per cell** rather than one per edit.

    Per cell, not per edit, because a cell write carries the cell's whole new value: two
    edits to one cell sent as two writes to one address means the platform applies them
    in order and the first is erased. ``plan_edits`` groups them so they compose.

    The new value is the old cell with each edit applied inside it, not the replacement
    text alone -- a comment asking to change one word quotes the word, and writing only
    the word would delete the sentence around it.
    """
    if not edits:
        return EditResult(status="applied")
    planned = textmap.plan_edits(snap, edits, window)
    by_address = {
        seg.address: snap.text[seg.char_start : seg.char_end] for seg in snap.segments
    }
    data = [
        {"range": address, "values": [[textmap.fold_edits(by_address[address], group)]]}
        for address, group in planned.items()
    ]

    receipt = prov._receipt_begin(doc_ref, edits, highlight=False)
    try:
        sheets = prov._sheets_client()
        await prov._call(
            sheets.spreadsheets()
            .values()
            .batchUpdate(
                spreadsheetId=doc_ref,
                body={"valueInputOption": "USER_ENTERED", "data": data},
            )
            .execute
        )
    except ProviderError as exc:
        prov._receipt_abort(receipt, f"{exc.kind}: {exc}")
        raise
    prov._receipt_commit(receipt, None)
    return EditResult(status="applied", receipt_id=receipt)


# ---------------------------------------------------------------- slides


async def read_presentation(prov: Any, doc_ref: DocRef) -> DocSnapshot:
    slides = prov._slides_client()
    pres = await prov._call(
        slides.presentations().get(presentationId=doc_ref).execute
    )
    pages: list[tuple[str, list[textmap.Shape]]] = []
    for page in pres.get("slides") or []:
        page_id = page.get("objectId") or ""
        shapes = _shapes_of(page, page_id, notes=False)
        notes_page = (page.get("slideProperties") or {}).get("notesPage") or {}
        if notes_page:
            shapes += _shapes_of(notes_page, page_id, notes=True)
        pages.append((page_id, shapes))
    text, segments = textmap.flatten_slides(pages)
    return DocSnapshot(
        doc_id=doc_ref,
        kind="presentation",
        revision_id=pres.get("revisionId"),
        text=text,
        segments=segments,
    )


def _shapes_of(page: dict, page_id: str, *, notes: bool) -> list[textmap.Shape]:
    """Every shape on a page that carries text, in draw order.

    Groups are walked into; a grouped text box is still a text box, and a comment can
    quote what is in it.
    """
    out: list[textmap.Shape] = []

    def walk(elements: list[dict]) -> None:
        for el in elements or []:
            if "elementGroup" in el:
                walk((el["elementGroup"] or {}).get("children") or [])
                continue
            shape = el.get("shape") or {}
            body = "".join(
                (item.get("textRun") or {}).get("content") or ""
                for item in (shape.get("text") or {}).get("textElements") or []
            )
            # **Empty text boxes are included.** They were skipped, on the reasoning
            # that a shape with no text contributes nothing to flatten -- true for the
            # flat text, and wrong for the addresses.
            #
            # Measured on a real deck: a new slide's title and subtitle placeholders are
            # both empty, so the read came back with no elements at all and a comment
            # saying "write a title" had nothing to aim at. The box exists; it is where
            # the title goes; the agent has to be able to see it.
            #
            # A shape with no text *and* no place for text is still skipped: an image or
            # a line would only be noise in a list of writable spots.
            if body or (shape.get("text") is not None) or shape.get("shapeType") == "TEXT_BOX":
                out.append(
                    textmap.Shape(
                        page_id=page_id,
                        object_id=el.get("objectId") or "",
                        text=body,
                        is_notes=notes,
                    )
                )

    walk(page.get("pageElements") or [])
    return out


async def write_presentation(
    prov: Any,
    doc_ref: DocRef,
    snap: DocSnapshot,
    edits: list[tuple[str, str]],
    revision: str | None,
    window: tuple[int, int] | None = None,
) -> EditResult:
    """Delete and re-insert inside one shape, per edit.

    Not ``replaceAllText``: that rewrites every match in the deck, which reaches past
    whatever the comment authorised. Mandate is granted per matter (§16), and a
    primitive whose scope is "the whole presentation" cannot express a grant whose
    scope is one sentence on slide three.

    ``plan_edits`` orders the requests back-to-front within each shape so that an
    earlier edit's length change cannot move a later edit's offsets.
    """
    if not edits:
        return EditResult(status="applied")
    planned = textmap.plan_edits(snap, edits, window)

    requests: list[dict] = []
    for address, group in planned.items():
        object_id = address.split("/", 1)[-1]
        for e in group:
            if e.local_end > e.local_start:
                requests.append({
                    "deleteText": {
                        "objectId": object_id,
                        "textRange": {
                            "type": "FIXED_RANGE",
                            "startIndex": e.local_start,
                            "endIndex": e.local_end,
                        },
                    }
                })
            if e.new:
                requests.append({
                    "insertText": {
                        "objectId": object_id,
                        "insertionIndex": e.local_start,
                        "text": e.new,
                    }
                })

    body: dict[str, Any] = {"requests": requests}
    if revision:
        body["writeControl"] = {"requiredRevisionId": revision}

    receipt = prov._receipt_begin(doc_ref, edits, highlight=False)
    try:
        slides = prov._slides_client()
        got = await prov._call(
            slides.presentations()
            .batchUpdate(presentationId=doc_ref, body=body)
            .execute
        )
    except ProviderError as exc:
        prov._receipt_abort(receipt, f"{exc.kind}: {exc}")
        if exc.kind == "conflict":
            return EditResult(status="conflict", detail=str(exc))
        raise
    new_rev = got.get("presentationId") and got.get("writeControl", {}).get(
        "requiredRevisionId"
    )
    prov._receipt_commit(receipt, new_rev)
    return EditResult(status="applied", new_revision_id=new_rev, receipt_id=receipt)


READERS = {
    "spreadsheet": read_spreadsheet,
    "presentation": read_presentation,
}

WRITERS = {
    "spreadsheet": write_spreadsheet,
    "presentation": write_presentation,
}


# ---------------------------------------------------------------- region write


_A1_REGION = re.compile(
    r"^(?:(?P<sheet>'[^']+'|[^!]+)!)?"
    r"(?P<c1>[A-Z]+)(?P<r1>\d+)(?::(?P<c2>[A-Z]+)(?P<r2>\d+))?$"
)


def parse_a1_region(region: str) -> tuple[str, int, int, int, int]:
    """An A1 range to (sheet, first row, first col, last row, last col), 0-based.

    A single cell is a region of one. Refusing to guess at anything else: a range this
    cannot read is a caller error, and resolving it to something plausible would write
    to cells nobody named.
    """
    m = _A1_REGION.match((region or "").strip())
    if not m:
        raise ProviderError("invalid", f"无法解析区域 {region!r}；应为 A1 记法，如 Sheet1!A1:B2。")
    sheet = (m.group("sheet") or "").strip("'")
    c1, r1 = _col_index(m.group("c1")), int(m.group("r1")) - 1
    c2 = _col_index(m.group("c2")) if m.group("c2") else c1
    r2 = int(m.group("r2")) - 1 if m.group("r2") else r1
    if r2 < r1 or c2 < c1:
        raise ProviderError("invalid", f"区域 {region!r} 的终点在起点之前。")
    return sheet, r1, c1, r2, c2


def _col_index(letters: str) -> int:
    """A1 column letters to a 0-based index."""
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def _grid_flat(content: list[list[str]]) -> str:
    """A row-major grid as one string, rows and cells joined by the grid separators.

    The receipt form of a region's content. One convention shared by the write (what
    was sent), the read (what stands now) and the revert (what to put back), so the
    anchor check is a string comparison rather than a re-interpretation.
    """
    return textmap.ROW_SEP.join(textmap.CELL_SEP.join(row) for row in content)


def _cell_texts(snap) -> dict[tuple[str, int, int], str]:
    """Each cell's current text keyed by (sheet, row, col), from a spreadsheet snapshot."""
    out: dict[tuple[str, int, int], str] = {}
    for seg in snap.segments:
        try:
            sheet, r, c, _, _ = parse_a1_region(seg.address)
        except ProviderError:
            continue
        out[(sheet, r, c)] = snap.text[seg.char_start : seg.char_end]
    return out


def _region_current_grid(snap, region: str) -> list[list[str]]:
    """What one A1 region reads like right now, as a row-major grid.

    Cells the snapshot does not carry are empty cells: the flattening only stores what
    exists, and "not stored" and "empty" read the same on the platform.
    """
    sheet, r1, c1, r2, c2 = parse_a1_region(region)
    cells = _cell_texts(snap)
    if not sheet:
        # A bare region names the snapshot's (single) sheet the way the formula check
        # does: no sheet constraint means the first one that has the cell.
        sheets = {k[0] for k in cells}
        sheet = next(iter(sorted(sheets)), "") if len(sheets) != 1 else next(iter(sheets))
    return [
        [cells.get((sheet, r, c), "") for c in range(c1, c2 + 1)]
        for r in range(r1, r2 + 1)
    ]


def _region_current_flat(snap, region: str) -> str:
    """What one A1 region reads like right now, in receipt form."""
    return _grid_flat(_region_current_grid(snap, region))


def sheet_anchor(region: str, gids: dict[str, int]) -> str:
    """The URL fragment Sheets opens on a range: ``#gid=<sheet id>&range=<A1>``.
    The region is ``'Title'!B3:D7`` (or ``Title!B3:D7``); an unknown title yields
    nothing rather than a wrong sheet."""
    if "!" not in region:
        return ""
    title, rng = region.rsplit("!", 1)
    title = title.strip()
    if len(title) >= 2 and title[0] == title[-1] == "'":
        title = title[1:-1].replace("''", "'")
    gid = gids.get(title)
    if gid is None or not rng:
        return ""
    return f"#gid={gid}&range={rng}"


def slide_anchor(address: str) -> str:
    """The URL fragment Slides opens on a page: ``#slide=id.<page id>``. A deck region
    is ``pageId/objectId``, so the page id is the part before the slash."""
    page = address.split("/", 1)[0].strip()
    return f"#slide=id.{page}" if page else ""


async def _sheet_gids(prov: Any, doc_ref: str) -> dict[str, int]:
    """Sheet title -> gid, for the receipt's anchors. Fail-soft: an anchor is a
    convenience for a person's eye, never a reason to refuse a write."""
    try:
        sheets = prov._sheets_client()
        meta = await prov._call(
            sheets.spreadsheets()
            .get(spreadsheetId=doc_ref, fields="sheets(properties(title,sheetId))")
            .execute
        )
        out: dict[str, int] = {}
        for s in (meta or {}).get("sheets") or []:
            p = s.get("properties") or {}
            if p.get("title") is not None and p.get("sheetId") is not None:
                out[str(p["title"])] = int(p["sheetId"])
        return out
    except Exception:  # noqa: BLE001
        return {}


async def write_regions_spreadsheet(
    prov: Any,
    doc_ref: DocRef,
    regions: list[tuple[str, list[list[str]]]],
    *,
    required_revision_id: str = "",
) -> EditResult:
    """Make each named rectangle read exactly like its content, in one atomic write.

    Several regions rather than one, because a move whose source and destination are not
    in the same rectangle is two of them. Done as two calls it leaves the value in both
    places in between -- measured live, and the window lasted six minutes while the
    model worked out that a second call was needed. ``values.batchUpdate`` takes them
    together, so the window does not have to exist.

    Every region is checked before anything is sent: shape against the region, and IC-7
    against the document as it stands. A formula cell anywhere in any of them refuses
    **the whole batch**, because a half-applied move is worse than a refused one.
    """
    if not regions:
        return EditResult(status="applied")

    parsed = []
    for region, content in regions:
        sheet, r1, c1, r2, c2 = parse_a1_region(region)
        rows, cols = r2 - r1 + 1, c2 - c1 + 1
        if len(content) != rows or any(len(r) != cols for r in content):
            got = f"{len(content)}x{len(content[0]) if content else 0}"
            raise ProviderError(
                "invalid",
                f"内容形状 {got} 与区域 {region} 的 {rows}x{cols} 不符；"
                "形状不符时无法判断该填哪些格。",
            )
        parsed.append((region, sheet, r1, c1, r2, c2))

    # The formula check reads the document as it stands, not a snapshot the caller may
    # be holding: a cell that became a formula since then must still be protected.
    snap = await read_spreadsheet(prov, doc_ref)
    blocked: list[str] = []
    for seg in snap.segments:
        if not seg.readonly_reason:
            continue
        try:
            s2, rr, cc, _, _ = parse_a1_region(seg.address)
        except ProviderError:
            continue
        for _region, sheet, r1, c1, r2, c2 in parsed:
            if r1 <= rr <= r2 and c1 <= cc <= c2 and (not sheet or s2 == sheet):
                blocked.append(seg.address)
                break
    if blocked:
        raise ProviderError(
            "invalid",
            f"区域内有公式格：{', '.join(blocked[:5])}。写入会把公式替换成字面量，"
            "且文档上看不出发生过什么，因此整批拒绝。这条没有强制选项，不要向用户提议覆盖；"
            "要改公式格只能由用户在表格里亲自改。",
        )

    # The receipt's two ends are the region's content before and after -- the pair the
    # inverse is materialized from. The snapshot read for the formula check doubles as
    # the before-image, so recording it costs no extra round trip.
    old_grids = [_region_current_grid(snap, region) for region, _ in regions]
    located = [
        (_grid_flat(old), _grid_flat(content))
        for old, (_, content) in zip(old_grids, regions)
    ]
    gids = await _sheet_gids(prov, doc_ref)
    receipt = prov._receipt_begin(
        doc_ref, located, highlight=False,
        regions=[(r, old) for (r, _), old in zip(regions, old_grids)],
        anchors={r: sheet_anchor(r, gids) for r, _ in regions},
    )
    try:
        sheets = prov._sheets_client()
        await prov._call(
            sheets.spreadsheets()
            .values()
            .batchUpdate(
                spreadsheetId=doc_ref,
                body={
                    "valueInputOption": "USER_ENTERED",
                    "data": [
                        {"range": region, "values": content}
                        for region, content in regions
                    ],
                },
            )
            .execute
        )
    except ProviderError as exc:
        prov._receipt_abort(receipt, f"{exc.kind}: {exc}")
        raise
    prov._receipt_commit(receipt, None, highlighted=False)
    await _verify_written_regions(prov, doc_ref, regions, receipt)
    return EditResult(status="applied", receipt_id=receipt)




def _shape_text(flat: str) -> str:
    """A shape's text as a region operand.

    The Slides API keeps a terminal newline on every text box, so the flattened
    read of a shape ends in ``\n`` while the text a caller writes never does. Compared
    raw, every deck write read back as drift and every receipt was demoted to
    ``applied_unverified`` (measured 2026-09-03: written ``标题（E2E）``, read
    ``标题（E2E）\n``), and the revert anchor check would have refused for the same
    reason. Both ends of the comparison drop that one newline; the platform puts it
    back on the next write.
    """
    return flat[:-1] if flat.endswith("\n") else flat


async def write_regions_presentation(
    prov: Any,
    doc_ref: DocRef,
    regions: list[tuple[str, list[list[str]]]],
    *,
    required_revision_id: str = "",
) -> EditResult:
    """Make each named shape read exactly like its content, in one atomic write.

    A deck's region is a shape, addressed as ``pageId/objectId`` -- the same address a
    read reports. Its content is one string, so the grid is a single cell; the shape of
    the operand differs per format, which is what D15 says, and the operation does not.

    The reason this exists rather than leaving decks to text replacement is the same one
    that forced it for spreadsheets, and shows up sooner here: **a new slide's title and
    subtitle are empty boxes**. There is no text in them to anchor a replacement on, so
    "write a title" cannot be said as a replacement at all -- while the box is right
    there, named, waiting.

    Delete-then-insert per shape, in one batchUpdate: the delete is skipped where the
    shape is already empty, because deleting an empty range is an error rather than a
    no-op.
    """
    if not regions:
        return EditResult(status="applied")

    snap = await read_presentation(prov, doc_ref)
    current = {
        seg.address: _shape_text(snap.text[seg.char_start : seg.char_end])
        for seg in snap.segments
    }
    # The operand is normalized the same way as the reading: a caller handing back
    # a before-text recorded with the platform's newline (a revert of a receipt from
    # before this rule) must not insert that newline as a second paragraph.
    regions = [
        (a, [[_shape_text(c[0][0])]] if len(c) == 1 and len(c[0]) == 1 else c)
        for a, c in regions
    ]

    requests: list[dict] = []
    for address, content in regions:
        if address not in current:
            raise ProviderError(
                "invalid",
                f"{address} 不是这份幻灯片里的文本框；可写的位置见 clouddoc_read 的 cells。",
            )
        if len(content) != 1 or len(content[0]) != 1:
            raise ProviderError(
                "invalid",
                f"幻灯片的一个区域是一个文本框，内容应为 [[\"文字\"]]，而不是 "
                f"{len(content)}x{len(content[0]) if content else 0}。",
            )
        object_id = address.split("/", 1)[-1]
        if current[address]:
            requests.append({
                "deleteText": {"objectId": object_id, "textRange": {"type": "ALL"}}
            })
        text = content[0][0]
        if text:
            requests.append({
                "insertText": {"objectId": object_id, "insertionIndex": 0, "text": text}
            })

    if not requests:
        return EditResult(status="applied")

    body: dict[str, Any] = {"requests": requests}
    if required_revision_id:
        body["writeControl"] = {"requiredRevisionId": required_revision_id}

    # A shape region's content is one string; before and after go into the receipt as
    # they stand, and the inverse is writing the before back through the same address.
    located = [(current[a], c[0][0]) for a, c in regions]
    receipt = prov._receipt_begin(
        doc_ref, located, highlight=False,
        regions=[(a, [[current[a]]]) for a, _ in regions],
        anchors={a: slide_anchor(a) for a, _ in regions},
    )
    try:
        slides = prov._slides_client()
        await prov._call(
            slides.presentations().batchUpdate(presentationId=doc_ref, body=body).execute
        )
    except ProviderError as exc:
        prov._receipt_abort(receipt, f"{exc.kind}: {exc}")
        if exc.kind == "conflict":
            return EditResult(status="conflict", detail=str(exc))
        raise
    prov._receipt_commit(receipt, None, highlighted=False)
    await _verify_written_regions(prov, doc_ref, regions, receipt)
    return EditResult(status="applied", receipt_id=receipt)


async def _verify_written_regions(prov, doc_ref, regions, receipt) -> None:
    """D19 tier 3b: the declarative write carries its own postcondition -- "this
    region should now read exactly like that" is verifiable by reading it. One
    extra read per write buys the difference between "the platform accepted the
    request" and "the document shows the result": a mismatch demotes the receipt
    to ``applied_unverified`` with what differed, so acceptance reads the truth.

    Fail-soft in both directions that are not the mismatch itself: an unreadable
    read-back proves nothing and changes nothing (the write already landed), and a
    missing sink leaves nothing to mark."""
    sink = getattr(prov, "receipt_sink", None)
    if receipt is None or sink is None:
        return
    try:
        current = await prov.read_regions(doc_ref, [r for r, _ in regions])
        drifted = [
            f"{region}: 现读作 {cur[:40]!r}，非所写内容"
            for (region, content), cur in zip(regions, current)
            if cur != _grid_flat(content)
        ]
        if drifted:
            sink.mark_unverified(receipt, detail="; ".join(drifted[:3]))
    except Exception:  # noqa: BLE001 - verification must not kill a landed write
        logger.warning("[clouddoc] 区域回读校验未完成 doc=%s", doc_ref)


async def read_regions_spreadsheet(
    prov: Any, doc_ref: DocRef, regions: list[str]
) -> list[str]:
    """Each region's current content in receipt form, for the revert anchor check."""
    snap = await read_spreadsheet(prov, doc_ref)
    return [_region_current_flat(snap, r) for r in regions]


async def read_regions_presentation(
    prov: Any, doc_ref: DocRef, regions: list[str]
) -> list[str]:
    """Each shape's current text, for the revert anchor check.

    A shape that no longer exists is not read as empty: the region the receipt names
    is gone, and the caller's anchor check must refuse rather than compare against a
    guess.
    """
    snap = await read_presentation(prov, doc_ref)
    current = {
        seg.address: _shape_text(snap.text[seg.char_start : seg.char_end])
        for seg in snap.segments
    }
    missing = [r for r in regions if r not in current]
    if missing:
        raise ProviderError(
            "invalid",
            f"这些区域已不在幻灯片里：{', '.join(missing[:5])}。",
        )
    return [current[r] for r in regions]


async def add_page_spreadsheet(prov: Any, doc_ref: str, title: str) -> str:
    """Add a worksheet, and answer with the name a region address can use.

    The address form is ``<sheet title>!A1``, so the title IS the address: a caller
    that asked for one and got a different one back (the platform de-duplicates by
    appending a suffix) would write into the wrong place, which is why the created
    title is read off the response rather than echoed from the request.

    Answered in the same form ``read`` reports cell addresses in -- quoted where A1
    notation needs it (``'Q3 Data'!A1``). The region writer sends the caller's
    address to the API verbatim, so a bare title with a space would hand the model
    an address that fails on its first write.
    """
    sheets = prov._sheets_client()
    want = (title or "").strip()
    if not want:
        raise ProviderError("invalid", "工作表名不能为空。")
    got = await prov._call(
        sheets.spreadsheets()
        .batchUpdate(
            spreadsheetId=doc_ref,
            body={"requests": [{"addSheet": {"properties": {"title": want}}}]},
        )
        .execute
    )
    replies = (got or {}).get("replies") or []
    props = ((replies[0] if replies else {}) or {}).get("addSheet", {}).get("properties", {})
    return _quote_sheet(str(props.get("title") or want))


async def add_page_presentation(prov: Any, doc_ref: str, title: str) -> str:
    """Add a slide, and answer with its page id -- the first half of a region address.

    ``title`` is not a slide's identity on this platform (a deck addresses pages by
    an opaque object id), so it is not sent. The page id comes back from the reply and
    is what the caller needs: region writes address ``<pageId>/<objectId>``.

    **The layout is stated, not left to the platform.** Without
    ``slideLayoutReference`` Google picks one from the deck's current state, and what
    it picked was a single placeholder -- so a three-page deck came out with page one
    holding a title box and a body box (it predated the agent) while pages two and
    three crammed the heading and its bullets into one box each. The pages read as
    three different decks. ``TITLE_AND_BODY`` is the shape a page of "a heading and
    some points" actually has, and it matches what a deck's first slide already is.
    """
    slides = prov._slides_client()
    got = await prov._call(
        slides.presentations()
        .batchUpdate(
            presentationId=doc_ref,
            body={"requests": [{"createSlide": {
                "slideLayoutReference": {"predefinedLayout": "TITLE_AND_BODY"},
            }}]},
        )
        .execute
    )
    replies = (got or {}).get("replies") or []
    page_id = ((replies[0] if replies else {}) or {}).get("createSlide", {}).get("objectId")
    if not page_id:
        raise ProviderError("unknown", "新建幻灯片未返回页面 id，无法给出区域地址。")
    return str(page_id)


ADD_PAGE = {
    "spreadsheet": add_page_spreadsheet,
    "presentation": add_page_presentation,
}

REGION_WRITERS = {
    "spreadsheet": write_regions_spreadsheet,
    "presentation": write_regions_presentation,
}

REGION_READERS = {
    "spreadsheet": read_regions_spreadsheet,
    "presentation": read_regions_presentation,
}
