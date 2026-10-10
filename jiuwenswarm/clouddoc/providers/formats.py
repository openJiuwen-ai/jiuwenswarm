"""Format helpers shared by both platforms' format modules and by the toolkit.

They operate on parse results and snapshot segments, never on a platform client,
so the receipt form of a region's content stays one convention across providers.
Each used to live in the Google module that first needed it and was imported from
there by the Feishu module and the toolkit; shared code sits below both now, so
neither platform depends on the other. Names keep their original spelling so the
modules that move after this one import them unchanged.
"""

from __future__ import annotations

import logging
import re

from jiuwenswarm.clouddoc.providers import textmap
from jiuwenswarm.clouddoc.providers.base import ProviderError

logger = logging.getLogger(__name__)

_A1_REGION = re.compile(
    r"^(?:(?P<sheet>'[^']+'|[^!]+)!)?"
    r"(?P<c1>[A-Z]+)(?P<r1>\d+)(?::(?P<c2>[A-Z]+)(?P<r2>\d+))?$"
)


def _col_index(letters: str) -> int:
    """A1 column letters to a 0-based index."""
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


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
    rows = [int(m.group("r1"))] + ([int(m.group("r2"))] if m.group("r2") else [])
    if min(rows) < 1:
        # Row numbers are 1-based; row 0 does not exist, and subtracting one from it
        # would hand the caller index -1, which Python reads as the last row.
        raise ProviderError("invalid", f"区域 {region!r} 的行号必须从 1 开始。")
    c1, r1 = _col_index(m.group("c1")), rows[0] - 1
    c2 = _col_index(m.group("c2")) if m.group("c2") else c1
    r2 = rows[1] - 1 if m.group("r2") else r1
    if r2 < r1 or c2 < c1:
        raise ProviderError("invalid", f"区域 {region!r} 的终点在起点之前。")
    return sheet, r1, c1, r2, c2


def _cell_texts(snap) -> dict[tuple[str, int, int], str]:
    """Each cell's current text keyed by (sheet, row, col), from a spreadsheet snapshot."""
    out: dict[tuple[str, int, int], str] = {}
    for seg in snap.segments:
        try:
            sheet, r, c, _, _ = parse_a1_region(seg.address)
        except ProviderError:
            continue
        out[(sheet, r, c)] = snap.text[seg.char_start: seg.char_end]
    return out


def _grid_flat(content: list[list[str]]) -> str:
    """A row-major grid as one string, rows and cells joined by the grid separators.

    The receipt form of a region's content. One convention shared by the write (what
    was sent), the read (what stands now) and the revert (what to put back), so the
    anchor check is a string comparison rather than a re-interpretation.
    """
    return textmap.ROW_SEP.join(textmap.CELL_SEP.join(row) for row in content)


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


def _utf16_len(s: str) -> int:
    """A string's length in UTF-16 code units, a surrogate pair counting as two. That is
    the unit document indices are measured in.
    """
    return len(s.encode("utf-16-le")) // 2


def _trim_common_affixes(old: str, new: str) -> tuple[int, str, str]:
    """(prefix_len_utf16, old_middle, new_middle) with the shared prefix and suffix
    removed.

    Locating always uses the **full** old_string -- a trimmed one is shorter and can
    stop being unique -- so trimming happens at request construction, on the located
    span. The prefix length is returned in UTF-16 code units because it offsets a
    document index.

    Prefix first, then suffix on what remains, so the two never overlap when old and
    new share a run (e.g. "aaa" → "aa").
    """
    a, b = old, new
    pre = 0
    while pre < len(a) and pre < len(b) and a[pre] == b[pre]:
        pre += 1
    suf = 0
    while suf < len(a) - pre and suf < len(b) - pre and a[-1 - suf] == b[-1 - suf]:
        suf += 1
    return (
        _utf16_len(a[:pre]),
        a[pre: len(a) - suf],
        b[pre: len(b) - suf],
    )


async def _verify_written_regions(prov, doc_ref, regions, receipt) -> None:
    """D19 tier 3b: the declarative write carries its own postcondition -- "this
    region should now read exactly like that" is verifiable by reading it. One
    extra read per write buys the difference between "the platform accepted the
    request" and "the document shows the result": a mismatch demotes the receipt
    to ``applied_unverified`` with what differed, so acceptance reads the truth.

    Fail-soft in both directions that are not the mismatch itself: an unreadable
    read-back proves nothing and changes nothing (the write already landed), and a
    missing sink leaves nothing to mark.
    """
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
