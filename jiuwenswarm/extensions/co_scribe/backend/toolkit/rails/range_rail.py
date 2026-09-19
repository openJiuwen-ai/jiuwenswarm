"""Bounding an edit to the passage a comment marks: windows, anchors, caps, scopes.

The governing rule is that **a comment authorizes the passage it quotes**. A summoned
turn edits directly, but only inside the window this rail derives from the quote --
the scope tier widens it to the sentence, line or paragraph the quote sits in, never
past that -- and an edit whose anchors fall outside, or that would grow the body past
the caps, is refused before the platform is asked. The chat path runs the same rail
over the same windows, so both paths judge one edit by one rule.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
    DocSnapshot,
)

CTX_WINDOW = 64


@dataclass(frozen=True)
class RangeRailConfig:
    # Retired as an authorization quantity by D23: windows come from the selection
    # and its declared structural scope, never from default padding. Kept so existing
    # configs still parse.
    adjacent_budget: int = 200
    max_quote_chars: int = 400
    # The platform's markless truncation point, from the provider's measurement
    # (None = the platform marks truncation itself, or none is known). A quote whose
    # length reaches this is treated as truncated -- see DocCapabilities.
    quote_hard_limit: int | None = None
    max_insert_chars: int = 2000
    max_edits: int = 10
    # Declared by the provider, not configured by the deployer: whether markup is
    # noise depends on the target document's format, not on anyone's preference.
    text_domain: str = "plain"


class RailVerdict(Enum):
    OK = "ok"
    QUOTE_TOO_LARGE = "quote_too_large"
    QUOTE_TRUNCATED = "quote_truncated"
    ANCHOR_AMBIGUOUS = "anchor_ambiguous"
    OUT_OF_RANGE = "out_of_range"
    TEXT_NOT_FOUND = "text_not_found"
    TOO_MANY_EDITS = "too_many_edits"
    INSERT_TOO_LARGE = "insert_too_large"
    MARKUP_INTRODUCED = "markup_introduced"
    RANGES_DISJOINT = "ranges_disjoint"


# In a plain-text domain these markers render as nothing and land in the body as
# **literal characters**. Observed: someone commented asking for bold, the model
# proposed `**this sentence**`, and approving it added four asterisks to the
# document. This cannot live in the prompt alone -- a model "knowing" not to emit
# markdown and what it actually does when asked for bold are two different things.
_MARKUP_MARKERS = ("**", "__", "~~", "`")


@dataclass(frozen=True)
class RangeCheck:
    verdict: RailVerdict
    start: int = 0
    end: int = 0
    ctx_hash: str = ""
    detail: str = ""
    # The character window [window_lo, window_hi) the rail approved. The applying
    # layer must locate old_string inside this window rather than in the whole
    # document, or the two layers judge by different rules: a proposal the rail
    # passed as "unique within the window" would be refused at apply time as "not
    # unique in the body" -- after a person had already approved it.
    window_lo: int = 0
    window_hi: int = 0

    @property
    def ok(self) -> bool:
        return self.verdict is RailVerdict.OK


# Drive truncates an over-long quote and ends it with an ellipsis (measured:
# selecting all of a 37,884-character body yielded a 419-character quote). That is
# **detectable**, which beats guessing a threshold; max_quote_chars is only a
# backstop.
TRUNCATION_MARK = "…"


# D23 (supersedes D22's vocabulary): the closed menu of structural scopes. The
# model reads the commenter's words and **chooses from this menu**; it never draws a
# span. Geometry is computed here, in code, from the anchored selection -- so the
# worst a steered model (or an injected comment) can do is widen to the enclosing
# paragraph: bounded, disclosed in the reply, revertible by receipt. The quoted body
# text still gets no vote (§16.2): nothing here reads it.
#
# A welcome corollary: Feishu's markless 128-character quote truncation stops being
# fatal for structural scopes -- the truncated prefix still anchors the selection's
# start, and the structure's bounds come from geometry, not from where the platform
# cut the string. Only ``exact`` depends on the quote being complete.
SCOPES = ("exact", "sentence", "line", "paragraph")
_SENTENCE_ENDS = "。！？!?.;；\n"


def scope_window(text: str, start: int, end: int, scope: str) -> tuple[int, int]:
    """The authorized window for one anchored selection at one declared scope (D23).

    ``exact`` is the selection itself. The structural scopes expand to the enclosing
    sentence (punctuation-bounded), line (newline-bounded) or paragraph (blank-line
    bounded). An unknown scope resolves to ``exact`` -- the fail direction must never
    be permissive.
    """
    if scope == "sentence":
        lo = start
        while lo > 0 and text[lo - 1] not in _SENTENCE_ENDS:
            lo -= 1
        hi = end
        while hi < len(text) and text[hi] not in _SENTENCE_ENDS:
            hi += 1
        if hi < len(text):
            hi += 1  # the terminator belongs to the sentence
        return lo, hi
    if scope == "line":
        lo = text.rfind("\n", 0, start) + 1
        nxt = text.find("\n", end)
        return lo, (len(text) if nxt < 0 else nxt)
    if scope == "paragraph":
        brk = text.rfind("\n\n", 0, start)
        lo = 0 if brk < 0 else brk + 2
        nxt = text.find("\n\n", end)
        return lo, (len(text) if nxt < 0 else nxt)
    return start, end


def ctx_hash(text: str, start: int, end: int, window: int = CTX_WINDOW) -> str:
    """A content fingerprint over ``window`` code points on each side of the range.

    It binds **adjacent context only** -- not the author, not the edits, not the
    revision. The re-anchoring done before apply uses it to answer "is this still
    the same place", never "has nothing changed".
    """
    before = unicodedata.normalize("NFC", text[max(0, start - window) : start])
    after = unicodedata.normalize("NFC", text[end : end + window])
    return hashlib.sha256(f"{before}\x00{after}".encode()).hexdigest()[:16]


def anchor_quote(snapshot: DocSnapshot, quoted: str) -> tuple[int, int] | None:
    """Re-anchor a comment's quoted text against the current body.

    **Exact substring plus uniqueness**, never fuzzy matching: introduce a threshold
    and an attacker only has to construct a closer-scoring counterfeit. Zero matches
    or two or more both fail closed.
    """
    first = snapshot.text.find(quoted)
    if first < 0:
        return None
    if snapshot.text.find(quoted, first + 1) >= 0:
        return None
    return first, first + len(quoted)


def _introduces_markup(old: str, new: str) -> str | None:
    """Whether the new text carries more markdown markers than the original.

    The test is on the **increase**, not on presence: a document may legitimately
    already contain `**` or backticks (code fragments, formulae), and a blanket rule
    would refuse ordinary rewrites too. Only a rising count for some marker is taken
    as the model having added it.
    """
    for marker in _MARKUP_MARKERS:
        if new.count(marker) > old.count(marker):
            return marker
    return None


def union_window(
    snapshot: DocSnapshot,
    quotes: Sequence[str],
    cfg: RangeRailConfig,
) -> tuple[tuple[int, int] | None, RangeCheck | None]:
    """The merged window for several comments on the same passage (D5).

    Merging is a property of the text, never of what the caller declared. Each quote is
    re-anchored to its exact span, and the spans are merged only where the code finds
    them **actually overlapping**. Declared ids whose ranges do not meet are refused as
    a batch rather than merged.

    That refusal is the whole point (IC-4). Taking min..max over whatever ids arrive
    would let one comment at the top of a document and one at the bottom be declared
    together, and their "union" is the entire body -- a bounded edit that covers
    everything. The union is therefore computed over spans the code proved adjacent,
    and a caller cannot widen its own window by naming another comment.

    Returns ``(window, None)`` on success, or ``(None, failure)``.
    """
    spans: list[tuple[int, int]] = []
    for quoted in quotes:
        if cfg.quote_hard_limit is not None and len(quoted) >= cfg.quote_hard_limit:
            return None, RangeCheck(
                RailVerdict.QUOTE_TRUNCATED,
                detail="the platform stored only the first part of this selection — select a smaller range and comment again, or handle it in Jiuwen chat",
            )
        if quoted.endswith(TRUNCATION_MARK):
            return None, RangeCheck(
                RailVerdict.QUOTE_TRUNCATED,
                detail="the selection exceeds the platform's quote limit — please handle this one in Jiuwen chat",
            )
        if len(quoted) > cfg.max_quote_chars:
            return None, RangeCheck(RailVerdict.QUOTE_TOO_LARGE, detail="the selected range is too large")
        anchored = anchor_quote(snapshot, quoted)
        if anchored is None:
            # The same per-format wording the single-comment path uses: on a grid the
            # generic "comment again" is the one instruction guaranteed to fail.
            return None, RangeCheck(
                RailVerdict.ANCHOR_AMBIGUOUS,
                detail=ambiguous_detail(snapshot, quoted),
            )
        start, end = anchored
        # Each comment's window is its own selection (exact): the merge condition
        # below asks whether the authorized windows actually meet.
        spans.append((start, end))

    spans.sort()
    lo, hi = spans[0]
    for nxt_lo, nxt_hi in spans[1:]:
        if nxt_lo > hi:
            # Disjoint. Reported rather than merged, and reported as a batch: the caller
            # asked for one edit to serve comments that are not about the same passage,
            # and there is no window that honours the request without covering text
            # neither comment selected.
            return None, RangeCheck(
                RailVerdict.RANGES_DISJOINT,
                detail=(
                    "the comments named together do not select overlapping text, so there "
                    "is no shared range to edit within — handle them as separate edits"
                ),
            )
        hi = max(hi, nxt_hi)
    return (lo, hi), None


def ambiguous_detail(snapshot: DocSnapshot, quoted: str) -> str:
    """Why a quote could not be anchored, said so the reader's next move is right.

    The generic wording is "please comment again", which on a grid sends the reader
    somewhere harmful. Observed 2026-09-10 on a live sheet: a value repeated down a
    column ("幻灯片" in C2 and C7) failed to anchor, and the agent -- reading only
    "comment again" -- told the person to marquee-select C2:C7 and re-comment. That
    is the one action guaranteed to fail: Google stores **no** quoted text for a
    multi-cell selection (measured the same day, alongside a workbook-range anchor
    whose id resolves through no public Sheets surface), so widening the selection
    removes the last bound the comment had rather than sharpening it.

    The same reply promised to "write both cells at once", which this rail also
    refuses: a spreadsheet window is clamped to the anchored cell, so one comment
    authorizes one cell however many the person names.

    A third correction, measured after the first two were written and costlier than
    both: "leave one comment on each cell" does not work either when the cells share a
    value. The person did exactly that -- one comment on C2, one on C7, each carrying
    the quote "幻灯片" -- and both refused, because the cell a comment marks is
    recoverable ONLY from its quoted text. The platform does distinguish them (the two
    comments carry different workbook-range ids) but that id appears nowhere in
    spreadsheets.get, includeGridData and all: not in namedRanges, developerMetadata,
    protectedRanges, nor on any cell, which carry value fields only. So a repeated
    value cannot be addressed from a comment at all, and telling someone to comment
    again spends their time on something that cannot succeed. All three corrections
    belong in the refusal, because the model has no other way to learn any of them.
    """
    if snapshot.kind != "spreadsheet":
        return "the quoted range can no longer be located uniquely in the current body — please comment again"
    n = snapshot.text.count(quoted) if quoted else 0
    where = f"appears {n} times in this sheet" if n else "is no longer present in this sheet"
    return (
        f"the comment's quoted text {where}, so the cell it marked cannot be identified. "
        "A comment's cell is recoverable only from its quoted text -- the platform's own "
        "anchor id resolves nowhere -- so re-commenting on the same cell will fail the same "
        "way, and a wider marquee selection is worse still: no quoted text is stored for a "
        "multi-cell selection at all. Do NOT ask for either. While two cells hold the same "
        "value, neither can be edited from a comment. Say so plainly, and offer the two "
        "things that do work: handle it in Jiuwen chat, where a person approves each write "
        "and cells are addressed by A1 (C2, C7), or comment on a cell whose value is unique "
        "in the sheet. One comment authorizes exactly ONE cell in any case"
    )


def check_range(
    snapshot: DocSnapshot,
    quoted: str,
    edits: list[tuple[str, str]],
    cfg: RangeRailConfig,
    *,
    window: tuple[int, int] | None = None,
    scope: str = "exact",
) -> RangeCheck:
    """The range rail. Enforced in code, not in the prompt.

    ``window`` overrides the span derived from ``quoted``, which is how a merged
    window from ``union_window`` is enforced: everything below -- uniqueness inside the
    window, the insert budget, the markup check -- applies unchanged, because merging
    changes which text is in range and nothing about what may be done to it.
    """
    # An unknown scope resolves to exact BEFORE any gate keys on the string: without
    # this, scope="Exact" walked past the truncation gates while receiving exact
    # geometry -- a bypass by a value the closed menu was supposed to exclude
    # (adversarial review F2b, reproduced).
    if scope not in SCOPES:
        scope = "exact"
    # Truncation matters only at ``exact``: a structural scope takes its bounds from
    # geometry around the anchored start, so a platform-shortened quote still yields
    # a well-defined window (D23).
    if scope == "exact" and cfg.quote_hard_limit is not None and len(quoted) >= cfg.quote_hard_limit:
        return RangeCheck(
            RailVerdict.QUOTE_TRUNCATED,
            detail="the platform stored only the first part of this selection — on the unattended path declare scope=sentence/line/paragraph if the comment asks for that unit; otherwise select a smaller range or handle it in Jiuwen chat",
        )
    if quoted.endswith(TRUNCATION_MARK):
        return RangeCheck(
            RailVerdict.QUOTE_TRUNCATED,
            detail="the selection exceeds the platform's quote limit — please handle this one in Jiuwen chat",
        )
    if len(quoted) > cfg.max_quote_chars:
        return RangeCheck(RailVerdict.QUOTE_TOO_LARGE, detail="the selected range is too large")
    if len(edits) > cfg.max_edits:
        return RangeCheck(RailVerdict.TOO_MANY_EDITS, detail=f"a batch may contain at most {cfg.max_edits} edits")

    if window is not None:
        lo, hi = window
        # The merged span is what the result reports and what ctx_hash covers: with
        # several comments there is no single anchored quote to describe the batch, and
        # the window is the range the edits were actually allowed to touch.
        start, end = lo, hi
    else:
        anchored = anchor_quote(snapshot, quoted)
        if anchored is None:
            return RangeCheck(
                RailVerdict.ANCHOR_AMBIGUOUS,
                detail=ambiguous_detail(snapshot, quoted),
            )
        start, end = anchored
        # D23: the selection is the authorization; the declared scope widens it to a
        # structural bound computed here. The structure is the ceiling.
        lo, hi = scope_window(snapshot.text, start, end, scope)
        # On the formats whose flat text is a grid of addressed runs, the newline
        # conventions the textual scopes read are separators between CELLS and
        # SHAPES, not between paragraphs -- a spreadsheet's flat text contains no
        # blank line anywhere, so "paragraph" computed textually was the whole
        # document, and "line" was the whole row (adversarial review F1, reproduced:
        # a one-cell selection authorized 869/869 characters). There the anchored
        # segment IS the structural unit, and no declared scope may cross it.
        if snapshot.kind in ("spreadsheet", "presentation"):
            seg = next(
                (g for g in (snapshot.segments or ())
                 if g.char_start <= start < g.char_end),
                None,
            )
            if seg is not None:
                lo = max(lo, seg.char_start)
                hi = min(hi, seg.char_end)
        elif (
            scope == "paragraph"
            and (lo, hi) == (0, len(snapshot.text))
            and "\n" in snapshot.text
        ):
            # A multi-line body with no blank-line structure gives "paragraph" no
            # bound at all -- the computed window would be the whole document, which
            # is exactly the widening this rail exists to refuse. A genuinely
            # single-paragraph body (no newline anywhere) stays allowed.
            return RangeCheck(
                RailVerdict.OUT_OF_RANGE,
                detail=(
                    "this document has no paragraph separators, so scope=paragraph "
                    "would cover the whole body — use 这句/这行 (sentence/line), or "
                    "select the larger range explicitly"
                ),
            )

    # In a markdown domain these markers are the correct output, and refusing them
    # would be the error.
    if cfg.text_domain == "plain":
        for old, new in edits:
            marker = _introduces_markup(old, new)
            if marker:
                return RangeCheck(
                    RailVerdict.MARKUP_INTRODUCED,
                    detail=(
                        f"the edit introduces {marker!r} into the body. This document is "
                        "plain text, so the markers would land as literal characters; "
                        "formatting such as bold or italics must be set by hand"
                    ),
                )

    total_insert = 0
    for old, new in edits:
        # old_string is **searched only inside the allowed window, and must be unique
        # there**. Searching the whole body and taking any match would write outside
        # the range; an identical string elsewhere in the document is irrelevant.
        window = snapshot.text[lo:hi]
        first = window.find(old)
        if first < 0:
            # Two different situations hide behind "not in the window", and telling a
            # model they are the same sends it down the wrong repair. Searching the
            # whole body separates them:
            #
            #   in the body, outside the window -> genuinely out of range; the fix is
            #       to narrow the edit
            #   nowhere in the body             -> the text was never there; the fix is
            #       to copy it exactly, usually a whitespace or line-break difference
            #
            # Observed: a model proposed an old_string with a line break where the body
            # has a space, was told "outside the allowed range", retried the identical
            # call, failed again and gave up. The pre-check exists to give a model
            # something it can act on, and a wrong diagnosis makes it merely fail
            # earlier.
            if old not in snapshot.text:
                return RangeCheck(
                    RailVerdict.TEXT_NOT_FOUND,
                    detail=(
                        f"{old[:60]!r} does not appear in the document at all. Copy the "
                        "text exactly as clouddoc_read returns it -- spaces and line "
                        "breaks included, and do not add a line break that is not there"
                    ),
                )
            # **Show the window, do not merely name it.** Saying "the window is the
            # comment's own selection" describes something the caller cannot see, and a
            # model that cannot see it searches for it: measured on a live document, an
            # edit for a 53-character quoted sentence was refused, then refused for the
            # paragraph, then for the next sentence, and finally accepted for two
            # characters -- five turns of binary search that ended in a parenthetical
            # bolted onto the sentence, because nothing ever said what the boundary was.
            # The window is a string this rail is holding. Printing it turns the search
            # into one retry.
            shown = window[:200] + ("…" if len(window) > 200 else "")
            return RangeCheck(
                RailVerdict.OUT_OF_RANGE,
                detail=(
                    f"{old[:30]!r} is outside the authorized window. The window is "
                    f"exactly this text, and an edit must sit inside it: {shown!r}. "
                    "Match it exactly -- a selection often stops just short of the "
                    "sentence's final punctuation, so copy the window rather than the "
                    "sentence you see in the body. Then narrow the old_string to a part "
                    "of it, or replace the whole window. The window cannot be widened "
                    "from here: the structural scopes (sentence/line/paragraph) are a "
                    "parameter of the unattended apply tool, not of this one"
                ),
            )
        if window.find(old, first + 1) >= 0:
            return RangeCheck(RailVerdict.OUT_OF_RANGE, detail=f"{old[:30]!r} is not unique within the allowed range")
        total_insert += len(new)

    # A size cap. Constraining where an edit lands but not how much it writes gets
    # "the selection is the boundary" only half right: a sentence-sized anchor could
    # otherwise carry arbitrarily long new text.
    if total_insert > (hi - lo) + cfg.max_insert_chars:
        return RangeCheck(RailVerdict.INSERT_TOO_LARGE, detail="the inserted text exceeds the size cap")

    return RangeCheck(
        RailVerdict.OK, start=start, end=end,
        ctx_hash=ctx_hash(snapshot.text, start, end),
        window_lo=lo, window_hi=hi,
    )
