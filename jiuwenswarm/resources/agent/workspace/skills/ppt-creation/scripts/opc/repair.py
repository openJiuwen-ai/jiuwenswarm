#!/usr/bin/env python3
"""Normalize generator quirks in unpacked OOXML before packing.

PptxGenJS writes several constructs that PowerPoint either rejects outright or
offers to "repair" on open. Each rule below fixes one observed failure and is
keyed to what actually goes wrong — none of them are cosmetic.

Run standalone to inspect a directory, or import `repair_all`.

    python3 repair.py unpacked/
"""
import logging
import argparse
import re
import sys
from pathlib import Path

# Program output goes to stdout, diagnostics to stderr. Both travel through
# logging, with a bare "%(message)s" format so the text is unchanged and the two
# streams stay separate for anything parsing this tool's output.
_OUT = logging.getLogger("repair.out")
_OUT.propagate = False
_OUT.setLevel(logging.INFO)
_out_handler = logging.StreamHandler(sys.stdout)
_out_handler.setFormatter(logging.Formatter("%(message)s"))
_OUT.addHandler(_out_handler)

LOGGER = logging.getLogger("repair")
LOGGER.propagate = False
LOGGER.setLevel(logging.INFO)
_err_handler = logging.StreamHandler(sys.stderr)
_err_handler.setFormatter(logging.Formatter("%(message)s"))
LOGGER.addHandler(_err_handler)


def emit(line):
    """Program output on stdout."""
    _OUT.info(line)

# --- Rule 1: bullet size percentages -------------------------------------
# ST_TextBulletSizePercent is a percentage STRING ("100%"), but PptxGenJS emits
# thousandths ("100000") the way it writes every other OOXML percentage. The
# schema pattern accepts 25%..400%.
_BU_SZ = re.compile(r'(<a:buSzPct\b[^>]*\bval=")(\d+)(")')

# --- Rule 2: negative extents --------------------------------------------
# A shape drawn right-to-left or bottom-to-top gets a negative <a:ext>. OOXML
# requires non-negative sizes and expresses direction with flipH/flipV.
_XFRM = re.compile(
    r'(<a:xfrm)([^>]*)(>\s*<a:off x=")(-?\d+)("\s+y=")(-?\d+)'
    r'("\s*/>\s*<a:ext cx=")(-?\d+)("\s+cy=")(-?\d+)("\s*/>)',
    re.S,
)

# --- Rule 3: shadow values out of range -----------------------------------
# PptxGenJS rewrites options.shadow IN PLACE while emitting XML, so one shadow
# object reused across shapes gets re-scaled once per shape: blurRad grows by
# x12700 each time and dir blows past the 21600000 ceiling of
# ST_PositiveFixedAngle. PowerPoint then refuses to open the deck.
_SHADOW_CEILINGS = {
    "blurRad": (2147483647, 38100),      # ST_PositiveCoordinate; 3pt default
    "dist": (2147483647, 38100),
    "dir": (21600000, 5400000),          # ST_PositiveFixedAngle (0..360°)
}
_SHADOW_TAG = re.compile(r"<a:outerShdw\b[^>]*/?>")
_ALPHA = re.compile(r'(<a:alpha\b[^>]*\bval=")(-?\d+)(")')

# --- Rule 4: duplicate shape ids ------------------------------------------
# Every <p:cNvPr id> must be unique within a slide; duplicates make PowerPoint
# report the file as corrupt.
_CNVPR_ID = re.compile(r'(<p:cNvPr\b[^>]*\bid=")(\d+)(")')


def fix_bullet_sizes(text: str) -> tuple[str, int]:
    fixed = 0

    def repl(m):
        nonlocal fixed
        raw = int(m.group(2))
        if raw <= 400:                    # already a plain percent
            return m.group(0)
        fixed += 1
        return f"{m.group(1)}{max(25, min(400, round(raw / 1000)))}%{m.group(3)}"

    return _BU_SZ.sub(repl, text), fixed


def fix_negative_extents(text: str) -> tuple[str, int]:
    fixed = 0

    def toggle(attrs: str, name: str) -> str:
        return (attrs.replace(f' {name}="1"', "")
                if f'{name}="1"' in attrs else attrs + f' {name}="1"')

    def repl(m):
        nonlocal fixed
        attrs = m.group(2)
        x, y, cx, cy = (int(m.group(i)) for i in (4, 6, 8, 10))
        if cx >= 0 and cy >= 0:
            return m.group(0)
        if cx < 0:
            x, cx, attrs = x + cx, -cx, toggle(attrs, "flipH")
        if cy < 0:
            y, cy, attrs = y + cy, -cy, toggle(attrs, "flipV")
        fixed += 1
        return (f"{m.group(1)}{attrs}{m.group(3)}{x}{m.group(5)}{y}"
                f"{m.group(7)}{cx}{m.group(9)}{cy}{m.group(11)}")

    return _XFRM.sub(repl, text), fixed


def fix_shadow_values(text: str) -> tuple[str, int]:
    fixed = 0

    def repl(m):
        nonlocal fixed
        tag = m.group(0)
        for attr, (ceiling, fallback) in _SHADOW_CEILINGS.items():
            def clamp(am, attr=attr, ceiling=ceiling, fallback=fallback):
                nonlocal fixed
                value = int(am.group(2))
                if 0 <= value <= ceiling:
                    return am.group(0)
                fixed += 1
                return f"{am.group(1)}{fallback}{am.group(3)}"

            tag = re.sub(rf'(\b{attr}=")(-?\d+)(")', clamp, tag)
        return tag

    text = _SHADOW_TAG.sub(repl, text)

    # Alpha is a percentage in thousandths: 0..100000.
    def clamp_alpha(m):
        nonlocal fixed
        value = int(m.group(2))
        if 0 <= value <= 100000:
            return m.group(0)
        fixed += 1
        return f"{m.group(1)}{max(0, min(100000, value))}{m.group(3)}"

    return _ALPHA.sub(clamp_alpha, text), fixed


def fix_duplicate_shape_ids(text: str) -> tuple[str, int]:
    seen: set[int] = set()
    fixed = 0

    def repl(m):
        nonlocal fixed
        value = int(m.group(2))
        if value not in seen:
            seen.add(value)
            return m.group(0)
        nxt = max(seen) + 1
        seen.add(nxt)
        fixed += 1
        return f"{m.group(1)}{nxt}{m.group(3)}"

    return _CNVPR_ID.sub(repl, text), fixed


# Whitespace-only runs lose their leading/trailing spaces unless the element
# says so explicitly. Applied to text runs across the package.
_T_WITH_SPACE = re.compile(r"<((?:\w+:)?t)>(\s[^<]*|[^<]*\s)</\1>")


def fix_whitespace_preservation(text: str) -> tuple[str, int]:
    fixed = 0

    def repl(m):
        nonlocal fixed
        fixed += 1
        return f'<{m.group(1)} xml:space="preserve">{m.group(2)}</{m.group(1)}>'

    return _T_WITH_SPACE.sub(repl, text), fixed


RULES = (
    ("bullet size", fix_bullet_sizes),
    ("negative extent", fix_negative_extents),
    ("shadow value", fix_shadow_values),
    ("duplicate shape id", fix_duplicate_shape_ids),
    ("whitespace preservation", fix_whitespace_preservation),
)


def repair_all(unpacked: Path) -> dict[str, int]:
    """Apply every rule to every XML part. Returns {rule name: fixes}."""
    totals = {name: 0 for name, _ in RULES}
    for part in sorted(unpacked.rglob("*.xml")):
        original = part.read_text(encoding="utf-8")
        text = original
        for name, rule in RULES:
            # Shape ids must be unique per part, so state cannot leak across files.
            text, count = rule(text)
            totals[name] += count
        if text != original:
            part.write_text(text, encoding="utf-8")
    return totals


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("unpacked", help="unpacked OOXML directory")
    args = ap.parse_args()

    totals = repair_all(Path(args.unpacked))
    total = sum(totals.values())
    if total:
        detail = ", ".join(f"{n} x{c}" for n, c in totals.items() if c)
        emit(f"Repaired {total} issue(s): {detail}")
    else:
        emit("No repairs needed")


if __name__ == "__main__":
    sys.exit(main())
