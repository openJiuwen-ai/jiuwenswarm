"""Turn a quoted string into the place a provider should write, format by format.

Every cloud document co-scribe can touch arrives as one thing: flat text plus a list
of runs saying where each stretch of it lives. That shape is not a convenience. It is
forced by what the platforms return for a comment.

Measured on both, across every file type they support:

* Feishu ``drive.file.comments.list`` carries ``quote`` and ``is_whole`` and no
  anchor field. On a spreadsheet the quote is prefixed with the cell's own A1
  address (``"D2 16oAxy"``), which the provider splits off.
* Google's Drive ``Comment`` carries ``quotedFileContent`` and an ``anchor``. The
  anchor decodes for exactly one shape: a comment left on a deck shape as a whole
  is ``{"type":"shape","page":…,"targets":[…]}``, which is a region address
  (D18). A spreadsheet's ``workbook-range`` id resolves through no public
  surface, and a multi-cell selection stores no quote at all (measured
  2026-09-10).

So the anchor a trigger can supply is quoted text -- plus, on a deck, the shape
the person clicked (§18.1). One locator serves every format, and the per-format
work shrinks to flattening -- which is where the differences that matter actually
live, and where this module's helpers stop and the providers begin.
"""

from __future__ import annotations

from dataclasses import dataclass

from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
    DocSnapshot,
    ProviderError,
    Segment,
)

# What separates one cell from the next when a sheet is flattened. Tab and newline are
# the spreadsheet clipboard convention, so a person copying a row out of the sheet and
# a person reading the flattened text see the same thing.
CELL_SEP = "\t"
ROW_SEP = "\n"
# Placed between a slide's body and its speaker notes. Without a divider the same
# sentence appearing in both would be two equally good matches and the range rail
# would refuse an edit it could have made (§18.4.3).
NOTES_DIVIDER = "\n--- notes ---\n"


@dataclass(frozen=True)
class Placement:
    """One run a character span lands in, and how far into it."""

    segment: Segment
    # Offsets within the run, not within the document.
    local_start: int
    local_end: int


def locate_span(
    text: str, needle: str, window: tuple[int, int] | None = None
) -> tuple[int, int]:
    """Find ``needle`` in ``text`` and return its character span.

    ``window`` is the range the caller's rail already approved; given it, the search
    runs only inside it, by exactly the rail's rule. Both layers must judge by one
    rule: a proposal the rail passed as unique within its window, re-judged here as
    unique across the whole body, would be refused after a person already approved it,
    and a phrase occurring twice is ordinary in a long document.
    """
    lo, hi = window if window else (0, len(text))
    lo = max(0, lo)
    hi = min(len(text), hi)
    hay = text[lo:hi]
    first = hay.find(needle)
    where = "the allowed range" if window else "the body"
    if first < 0:
        raise ProviderError("invalid", f"{needle[:40]!r} not found in {where}")
    if hay.find(needle, first + 1) >= 0:
        # In a spreadsheet this is the common case rather than the rare one: a quote of
        # "42" matches every cell showing 42. Refusing is the rail working, not failing
        # -- writing to a cell nobody named is the outcome being prevented.
        raise ProviderError("invalid", f"{needle[:40]!r} is not unique within {where}")
    return lo + first, lo + first + len(needle)


def placements(snap: DocSnapshot, c0: int, c1: int) -> list[Placement]:
    """The runs a character span touches, in order.

    Empty is not returned: a span that maps to no run is a flattening bug, and letting
    it through would send a write at whatever the caller defaulted to.
    """
    out: list[Placement] = []
    for seg in snap.segments:
        if seg.char_end <= c0 or seg.char_start >= c1:
            continue
        out.append(
            Placement(
                segment=seg,
                local_start=max(c0, seg.char_start) - seg.char_start,
                local_end=min(c1, seg.char_end) - seg.char_start,
            )
        )
    if not out:
        raise ProviderError("invalid", "字符区间无法映射到文档中的任何位置")
    return out


def refuse_readonly(hits: list[Placement]) -> None:
    """IC-7. Refuse a write that would destroy something the flat text does not show.

    The formula case is the reason this exists and is worth stating plainly: a cell
    holding ``=SUM(A1:A9)`` reads as ``42``, the comment quotes the 42, and writing the
    edited 42 back leaves a literal where the formula was. Nothing about the document
    looks different afterwards -- the number is still a number -- so the damage is
    found weeks later, if at all.

    Enforced here in code rather than asked for in a prompt: a safety rule that a model
    can decline to follow is not a safety rule.
    """
    for hit in hits:
        if hit.segment.readonly_reason:
            raise ProviderError("invalid", hit.segment.readonly_reason)


def single_placement(snap: DocSnapshot, c0: int, c1: int) -> Placement:
    """The one run a span lands in, refusing a span that crosses several.

    Spreadsheets and decks address a write to one cell or one shape. A span reaching
    across two of them has no single write that means it, and the alternatives -- write
    the first, or split into several writes -- are both worse than saying so: the first
    silently drops half the edit, and the second is no longer one atomic change.
    """
    hits = placements(snap, c0, c1)
    refuse_readonly(hits)
    if len(hits) > 1:
        raise ProviderError(
            "invalid",
            "这处修改跨越了两个以上的单元格或文本框，无法作为一次写入提交。"
            "请针对其中一处提出修改。",
        )
    return hits[0]


def apply_edits_to_text(
    text: str, edits: list[tuple[str, str]], window: tuple[int, int] | None = None
) -> str:
    """Apply every edit to a flat string, for the formats written back whole.

    Each ``old`` must occur exactly once **within the window the range rail approved**,
    and the check runs against the text as it stands after the previous edits, not
    against the original: two edits whose ranges overlap would otherwise both look
    applicable and the second would land on text the first had already rewritten.

    The window is carried through rather than dropped because the rail and this layer
    must judge by one rule. Re-judging uniqueness across the whole body would refuse a
    proposal after a person had already approved it (§13.10). The window is clamped
    after each edit, since a replacement of a different length moves everything after
    it.
    """
    out = text
    lo, hi = window if window else (0, len(text))
    for old, new in edits:
        c0, c1 = locate_span(out, old, (lo, min(hi, len(out))))
        out = out[:c0] + new + out[c1:]
        hi += len(new) - (c1 - c0)
    return out


# ------------------------------------------------------------------ flatteners
#
# Below: the per-format flattening, written once against a normalised input so that
# both platforms share it. What a provider still owns is fetching, and translating its
# platform's response into these shapes -- not the layout rules, which have to agree
# across providers or the same document would read differently depending on who read
# it.


@dataclass(frozen=True)
class Cell:
    """One spreadsheet cell, as both of its faces.

    ``formatted`` is what a person sees and therefore what a comment quotes.
    ``formula`` is what is actually stored. They differ exactly when the cell is
    computed, which is the signal IC-7 reads.
    """

    address: str  # A1 notation, sheet-qualified: "Sheet1!B7"
    formatted: str
    formula: str

    @property
    def is_computed(self) -> bool:
        # A formula always starts with "=", and a literal never does. Comparing the two
        # faces alone would call every number-formatted cell computed -- 1234 displayed
        # as "1,234" differs from its stored value without being a formula.
        return self.formula.startswith("=") and self.formula != self.formatted


def flatten_grid(rows: list[list[Cell]]) -> tuple[str, tuple[Segment, ...]]:
    """A spreadsheet to flat text plus one run per cell.

    Row-major, cells joined by tab and rows by newline: the clipboard convention, so
    the flattened text is the text a person gets by copying the range out.

    A cell's own content may contain newlines. It is written literally and the run
    records its true length, so the flat text stays a faithful haystack even where it
    stops looking like a grid.
    """
    chunks: list[str] = []
    segments: list[Segment] = []
    base = 0
    for r, row in enumerate(rows):
        if r:
            chunks.append(ROW_SEP)
            base += len(ROW_SEP)
        for c, cell in enumerate(row):
            if c:
                chunks.append(CELL_SEP)
                base += len(CELL_SEP)
            body = cell.formatted
            segments.append(
                Segment(
                    char_start=base,
                    char_end=base + len(body),
                    address=cell.address,
                    readonly_reason=(
                        f"{cell.address} 是公式格（{cell.formula[:60]}），"
                        "写入会把公式替换成字面量，且文档上看不出发生过什么。"
                        "如需改动请改公式引用的源数据，或请人手工修改该格。"
                        if cell.is_computed
                        else ""
                    ),
                )
            )
            chunks.append(body)
            base += len(body)
    return "".join(chunks), tuple(segments)


@dataclass(frozen=True)
class Shape:
    """One run of text on a slide, with the shape it lives in."""

    page_id: str
    object_id: str
    text: str
    # Speaker notes are a separate page and a comment can sit on them, so they are
    # flattened too -- behind a divider, so a sentence appearing in both body and notes
    # does not become two equally good matches.
    is_notes: bool = False


def flatten_slides(
    pages: list[tuple[str, list[Shape]]],
) -> tuple[str, tuple[Segment, ...]]:
    """A deck to flat text plus one run per shape.

    ``pages`` is ordered, and each page's shapes are in the order the platform reports
    them, which is the order they are drawn. Body shapes come first, then the divider,
    then notes.
    """
    chunks: list[str] = []
    segments: list[Segment] = []
    base = 0

    def emit(text: str) -> None:
        nonlocal base
        chunks.append(text)
        base += len(text)

    for p, (_page_id, shapes) in enumerate(pages):
        if p:
            emit(ROW_SEP)
        body = [s for s in shapes if not s.is_notes]
        notes = [s for s in shapes if s.is_notes]
        for group, divider in ((body, ""), (notes, NOTES_DIVIDER)):
            if not group:
                continue
            if divider:
                emit(divider)
            for i, shape in enumerate(group):
                if i:
                    emit(ROW_SEP)
                segments.append(
                    Segment(
                        char_start=base,
                        char_end=base + len(shape.text),
                        address=f"{shape.page_id}/{shape.object_id}",
                        role="notes" if shape.is_notes else "",
                    )
                )
                emit(shape.text)
    return "".join(chunks), tuple(segments)


@dataclass(frozen=True)
class PlannedEdit:
    """One edit resolved to a place inside one run, before anything is sent."""

    address: str
    local_start: int
    local_end: int
    new: str


def plan_edits(
    snap: DocSnapshot,
    edits: list[tuple[str, str]],
    window: tuple[int, int] | None = None,
) -> dict[str, list[PlannedEdit]]:
    """Resolve every edit to a run, grouped by run, ordered back to front.

    Three things go wrong without this, and the first two did:

    * **Two edits in one cell.** Each was located against the original text and each
      produced a whole-cell write to the same address. The platform applies them in
      order, so the last one won and the first was erased -- while the batch reported
      ``applied``. Grouping by address is what makes them compose instead of race.
    * **The approved window was dropped.** ``window`` is the range the range rail
      already passed, and searching without it re-judges uniqueness across the whole
      body. In a document that is merely stricter; in a spreadsheet it is fatal, because
      a quote of ``42`` matches every cell showing 42 and the edit is refused *after a
      person approved it*. Both layers must judge by one rule (§13.10).
    * **Two edits over the same characters.** Refused: there is no order in which both
      are what the person approved, and applying either silently would pick one.

    Back to front within a run so that an earlier edit's length change cannot move a
    later edit's offsets.
    """
    planned: dict[str, list[PlannedEdit]] = {}
    for old, new in edits:
        c0, c1 = locate_span(snap.text, old, window)
        hit = single_placement(snap, c0, c1)
        planned.setdefault(hit.segment.address, []).append(
            PlannedEdit(hit.segment.address, hit.local_start, hit.local_end, new)
        )
    for address, group in planned.items():
        group.sort(key=lambda e: e.local_start)
        for a, b in zip(group, group[1:]):
            if b.local_start < a.local_end:
                raise ProviderError(
                    "invalid",
                    f"{address} 上有两处修改落在同一段文字上，无法同时应用。"
                    "请把它们合并成一处，或分别提交。",
                )
        group.reverse()
    return planned


def fold_edits(text: str, group: list[PlannedEdit]) -> str:
    """Apply one run's planned edits to its text. ``group`` is back-to-front already."""
    out = text
    for e in group:
        out = out[: e.local_start] + e.new + out[e.local_end :]
    return out
