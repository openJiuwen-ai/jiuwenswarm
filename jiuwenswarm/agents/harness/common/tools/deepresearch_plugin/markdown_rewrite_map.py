# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""UTF-8 byte boundary helpers for Markdown rewrite selections."""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import unquote

from markdown_it import MarkdownIt
from markdown_it.common.entities import entities
from markdown_it.common.utils import fromCodePoint, isValidEntityCode
from markdown_it.token import Token

from jiuwenswarm.agents.harness.common.tools.deepresearch_plugin.cjk_friendly_emphasis import (
    apply_cjk_friendly_rules,
)


def _new_rewrite_parser() -> MarkdownIt:
    """Parser matching the relay-claw frontend's Markdown semantics.

    The rewrite map must classify exactly the paragraphs the frontend
    selection code treats as rewritable, so this parser carries the
    frontend's CJK-friendly emphasis rules (instance-scoped; all other
    MarkdownIt instances keep stock CommonMark flanking). See
    cjk_friendly_emphasis for details.
    """
    return apply_cjk_friendly_rules(
        MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"])
    )


UnitType = Literal["heading", "paragraph", "list_item"]
ProtectedKind = Literal[
    "syntax",
    "link_destination",
    "citation",
    "hard_break",
    "inline_code",
    "image",
    "inference",
]


@dataclass(frozen=True, slots=True)
class RewriteSlot:
    """An editable rendered-text span with exact source byte boundaries."""

    slot_id: str
    start_byte: int
    end_byte: int
    text: str
    formats: tuple[str, ...]
    link_id: str | None = None
    visible_boundary_to_byte: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ProtectedAnchor:
    """An immutable inline source span whose topology must be preserved."""

    anchor_id: str
    kind: ProtectedKind
    start_byte: int
    end_byte: int
    source: str


@dataclass(frozen=True, slots=True)
class RewriteUnit:
    """A supported source block that can contain rewrite slots."""

    unit_id: str
    unit_type: UnitType
    start_byte: int
    end_byte: int
    level: int | None
    list_depth: int | None
    list_marker: str | None
    slots: tuple["RewriteSlot", ...] = ()
    protected: tuple["ProtectedAnchor", ...] = ()


@dataclass(frozen=True, slots=True)
class UnsupportedRegion:
    """A source block that must not be treated as rewritable prose."""

    kind: str
    start_byte: int
    end_byte: int


@dataclass(frozen=True, slots=True)
class MarkdownRewriteMap:
    """Immutable block-level classification of a Markdown source document."""

    source: str
    units: tuple[RewriteUnit, ...]
    unsupported_regions: tuple[UnsupportedRegion, ...]
    source_sha256: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not self.source_sha256:
            object.__setattr__(
                self,
                "source_sha256",
                hashlib.sha256(self.source.encode("utf-8")).hexdigest(),
            )


@dataclass(frozen=True, slots=True)
class DocumentHeading:
    """A rendered heading discovered anywhere in the current Markdown."""

    text: str


@dataclass(frozen=True, slots=True)
class DocumentInternalLink:
    """An exact same-document link destination in the current Markdown."""

    target: str
    start_byte: int
    end_byte: int
    source: str


@dataclass(frozen=True, slots=True)
class DocumentAnchorIndex:
    """Whole-document headings and internal links with fail-closed alignment."""

    headings: tuple[DocumentHeading, ...]
    links: tuple[DocumentInternalLink, ...]
    parsed_targets: tuple[str, ...] = ()
    ambiguous: bool = False


class RewriteMapError(ValueError):
    """A stable selection-to-source mapping failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Utf8BoundaryTable:
    """Map Python codepoint indexes to UTF-8 byte offsets and back."""

    text: str
    codepoint_to_byte: tuple[int, ...] = field(init=False)
    _byte_to_codepoint: dict[int, int] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        offsets = [0]
        byte_offset = 0
        for character in self.text:
            byte_offset += len(character.encode("utf-8"))
            offsets.append(byte_offset)
        codepoint_to_byte = tuple(offsets)
        object.__setattr__(self, "codepoint_to_byte", codepoint_to_byte)
        object.__setattr__(
            self,
            "_byte_to_codepoint",
            {offset: index for index, offset in enumerate(codepoint_to_byte)},
        )

    def require_byte_boundary(self, byte_offset: int) -> int:
        """Return the codepoint index at ``byte_offset`` or reject the offset."""
        if type(byte_offset) is not int:
            raise RewriteMapError(
                "SELECTION_MAPPING_CONFLICT",
                f"byte offset {byte_offset!r} must be an integer",
            )
        try:
            return self._byte_to_codepoint[byte_offset]
        except (KeyError, TypeError) as exc:
            raise RewriteMapError(
                "SELECTION_MAPPING_CONFLICT",
                f"byte offset {byte_offset!r} is outside the text or splits a UTF-8 codepoint",
            ) from exc


def sha256_byte_range(text: str, start_byte: int, end_byte: int) -> str:
    """Hash a valid half-open UTF-8 byte range, including an empty range.

    This byte-level helper permits ``start_byte == end_byte``. Higher-level
    rewrite preparation is responsible for requiring a non-empty user selection.
    """
    table = Utf8BoundaryTable(text)
    table.require_byte_boundary(start_byte)
    table.require_byte_boundary(end_byte)
    if start_byte > end_byte:
        raise RewriteMapError(
            "SELECTION_MAPPING_CONFLICT",
            "start byte offset must not exceed end byte offset",
        )
    return hashlib.sha256(text.encode("utf-8")[start_byte:end_byte]).hexdigest()


class _SourceLines:
    """Translate markdown-it line maps to exact UTF-8 source byte ranges."""

    def __init__(self, source: str):
        lines = []
        line_start = 0
        index = 0
        while index < len(source):
            character = source[index]
            if character == "\n":
                index += 1
                lines.append(source[line_start:index])
                line_start = index
            elif character == "\r":
                index += 2 if source[index + 1:index + 2] == "\n" else 1
                lines.append(source[line_start:index])
                line_start = index
            else:
                index += 1
        if line_start < len(source):
            lines.append(source[line_start:])
        self.lines = lines
        starts = [0]
        for line in self.lines:
            starts.append(starts[-1] + len(line.encode("utf-8")))
        self.starts = tuple(starts)

    @staticmethod
    def _content(line: str) -> str:
        if line.endswith("\r\n"):
            return line[:-2]
        if line.endswith(("\n", "\r")):
            return line[:-1]
        return line

    def _invalid_line_map(self, line_map: list[int] | None) -> bool:
        if line_map is None or len(line_map) != 2:
            return True
        start_line, end_line = line_map
        return start_line < 0 or start_line >= end_line or end_line > len(self.lines)

    def byte_range(self, line_map: list[int] | None) -> tuple[int, int] | None:
        if self._invalid_line_map(line_map):
            return None
        start_line, end_line = line_map
        while end_line > start_line and not self._content(
            self.lines[end_line - 1]
        ).strip(" \t"):
            end_line -= 1
        if end_line == start_line:
            return None
        start_byte = self.starts[start_line]
        end_byte = self.starts[end_line]
        final_line = self.lines[end_line - 1]
        if final_line.endswith("\r\n"):
            end_byte -= 2
        elif final_line.endswith(("\n", "\r")):
            end_byte -= 1
        return start_byte, end_byte

    def line(self, line_number: int) -> str | None:
        if not 0 <= line_number < len(self.lines):
            return None
        return self.lines[line_number]


_LIST_OPEN_TYPES = {"bullet_list_open", "ordered_list_open"}
_LIST_CLOSE_TYPES = {"bullet_list_close", "ordered_list_close"}
_UNSUPPORTED_BLOCK_KINDS = {
    "blockquote_open": "blockquote",
    "table_open": "table",
    "fence": "fenced_code",
    "code_block": "indented_code",
    "html_block": "html_block",
}
_LIST_MARKER = re.compile(r"^[ \t]*(?P<marker>[-+*]|\d+[.)])(?=[ \t])")
_LIST_PREFIX = re.compile(r"^[ \t]*(?:[-+*]|\d+[.)])[ \t]+")
_HEADING_PREFIX = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+")
_HEADING_SUFFIX = re.compile(r"(?:[ \t]+#+)?[ \t]*")
_CITATION = re.compile(r"\[\[(?P<number>\d+)\]\]\((?P<href>https?://[^\s)]+)\)")
_INTERNAL_LINK_SOURCE = re.compile(
    r"(?<!!)\[[^\]\r\n]*\]\(\s*(?P<destination>#[^\s)>]+)"
    r"(?:\s+(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|\([^\r\n)]*\)))?\s*\)"
)
_ESCAPABLE = frozenset(r'!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~')


class _InlineTopologyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _SlotDraft:
    start: int
    end: int
    text: str
    boundaries: list[int]
    formats: tuple[str, ...]
    link_id: str | None


def _encode_markdown_literal(text: str) -> str:
    """Encode final visible inline text without introducing Markdown syntax."""
    encoded: list[str] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character == "\r":
            if text[index + 1:index + 2] == "\n":
                index += 1
            encoded.append("\\\n")
        elif character == "\n":
            encoded.append("\\\n")
        elif character in _ESCAPABLE:
            encoded.append("\\" + character)
        else:
            encoded.append(character)
        index += 1
    return "".join(encoded)


_UNMATCHED_CONSTRUCT_PREFIXES = ("**", "__", "~~", "[", "]", "<", "`", "![")

# Constructs that are ordinary literal text whenever markdown-it left them
# inside a text token — reference-list labels (`[1]. `), LaTeX-ish math
# (`$β<0.6$`), unpaired brackets or backticks. The relay-claw frontend
# accepts such paragraphs as rewritable, so an odd count must not reject the
# whole unit; the per-character raw/visible alignment below already
# guarantees the token is an exact source slice. Emphasis markers keep the
# odd-count fail-closed rule above.
_LITERAL_TEXT_CONSTRUCTS = frozenset({"[", "]", "<", "`", "!["})

# CommonMark character references — same shapes markdown-it's own entity rule
# decodes (rules_inline/entity.py) before `text_join` merges them into text
# tokens, which is why rendered text can carry a character whose raw source
# form is multi-byte (e.g. `&#91;` for `[` in scraped reference titles).
_ENTITY_DIGITAL_RE = re.compile(r"&#((?:x[a-f0-9]{1,6}|[0-9]{1,7}));", re.IGNORECASE)
_ENTITY_NAMED_RE = re.compile(r"&([a-z][a-z0-9]{1,31});", re.IGNORECASE)


def _decode_entity_at(raw: str, index: int) -> tuple[str, int] | None:
    """Decode a single-character CommonMark reference at ``raw[index:]``.

    Returns ``(decoded character, raw length)``, or ``None`` when the source
    does not hold a valid entity there (mirrors rules_inline/entity.py) or
    the entity decodes to more than one code point (HTML5 named ligatures
    and combining sequences such as ``&fjlig;`` -> "fj"; those fail closed
    so consumers never align only the first code point of an entity).
    """
    if index >= len(raw) or raw[index] != "&":
        return None
    tail = raw[index:]
    if len(tail) > 1 and tail[1] == "#":
        match = _ENTITY_DIGITAL_RE.match(tail)
        if match is not None:
            digits = match.group(1)
            code = int(digits[1:], 16) if digits[0].lower() == "x" else int(digits, 10)
            char = fromCodePoint(code) if isValidEntityCode(code) else fromCodePoint(0xFFFD)
            return char, match.end()
    else:
        match = _ENTITY_NAMED_RE.match(tail)
        if match is not None and match.group(1) in entities:
            decoded = entities[match.group(1)]
            if len(decoded) == 1:
                return decoded, match.end()
    return None


def _matched_unmatched_construct(raw: str, index: int) -> str | None:
    tail = raw[index:]
    for marker in _UNMATCHED_CONSTRUCT_PREFIXES:
        if tail.startswith(marker):
            return marker
    return None


def _looks_like_unmatched_construct(raw: str, index: int) -> bool:
    return _matched_unmatched_construct(raw, index) is not None


def _token_identity(token: Token) -> tuple[object, ...]:
    return (
        token.type,
        token.nesting,
        token.content,
        token.markup,
        tuple(sorted(token.attrs.items())),
    )


class _InlineScanner:
    """Losslessly align one inline source span with markdown-it child tokens."""

    def __init__(
        self,
        raw: str,
        raw_start: int,
        unit_id: str,
        children: list[Token],
        parser: MarkdownIt,
        boundary_table: Utf8BoundaryTable,
    ):
        self.raw = raw
        self.raw_start = raw_start
        self.unit_id = unit_id
        self.children = children
        self.parser = parser
        self.cursor = 0
        self.slots: list[RewriteSlot] = []
        self.protected: list[ProtectedAnchor] = []
        self.link_count = 0
        self.source_boundaries = boundary_table.codepoint_to_byte

    def _byte(self, local_index: int) -> int:
        return self.source_boundaries[self.raw_start + local_index]

    def _add_slot(self, draft: _SlotDraft) -> None:
        if not draft.text:
            return
        start_byte, end_byte = self._byte(draft.start), self._byte(draft.end)
        ordinal = len(self.slots)
        self.slots.append(
            RewriteSlot(
                f"{self.unit_id}:slot:{ordinal}:{start_byte}:{end_byte}",
                start_byte,
                end_byte,
                draft.text,
                draft.formats,
                draft.link_id,
                tuple(draft.boundaries),
            )
        )

    def _add_anchor(self, kind: ProtectedKind, start: int, end: int) -> None:
        start_byte, end_byte = self._byte(start), self._byte(end)
        ordinal = len(self.protected)
        self.protected.append(
            ProtectedAnchor(
                f"{self.unit_id}:anchor:{ordinal}:{start_byte}:{end_byte}",
                kind,
                start_byte,
                end_byte,
                self.raw[start:end],
            )
        )

    def _consume_text(
        self,
        rendered: str,
        formats: tuple[str, ...],
        link_id: str | None,
    ) -> None:
        if not rendered:
            return
        # Flanking rules can leave a balanced, paired `**`/`__`/`~~` as
        # literal text inside a text token (e.g. `x**≤**y`, where the Latin
        # opener is followed by the punctuation `≤`). markdown-it has already
        # decided these are literal, so consuming them as text preserves the
        # lossless invariant. Only genuinely unbalanced markers (odd count,
        # e.g. a stray unterminated `**`) keep failing closed.
        balanced_double_markers = {
            marker
            for marker in ("**", "__", "~~")
            if rendered.count(marker) % 2 == 0
        }
        start = self.cursor
        boundaries = [self._byte(start)]
        for visible in rendered:
            is_escaped_visible = (
                self.cursor + 1 < len(self.raw)
                and self.raw[self.cursor] == "\\"
                and self.raw[self.cursor + 1] in _ESCAPABLE
                and self.raw[self.cursor + 1] == visible
            )
            entity = _decode_entity_at(self.raw, self.cursor)
            if is_escaped_visible:
                self.cursor += 2
            elif entity is not None and entity[0] == visible:
                # Character references: one visible char spanning several raw
                # bytes (`&#91;` renders as `[`). Consumed whole so slots stay
                # exact source slices.
                self.cursor += entity[1]
            elif self.cursor < len(self.raw) and self.raw[self.cursor] == visible:
                construct = _matched_unmatched_construct(self.raw, self.cursor)
                if (
                    construct is not None
                    and construct not in _LITERAL_TEXT_CONSTRUCTS
                    and construct not in balanced_double_markers
                ):
                    raise _InlineTopologyError("unmatched inline marker")
                self.cursor += 1
            else:
                raise _InlineTopologyError("rendered text does not align with source")
            boundaries.append(self._byte(self.cursor))
        self._add_slot(
            _SlotDraft(start, self.cursor, rendered, boundaries, formats, link_id)
        )

    def _parsed_children(self, candidate: str) -> list[Token]:
        parsed = self.parser.parseInline(candidate)
        if len(parsed) != 1 or parsed[0].type != "inline":
            return []
        return parsed[0].children or []

    def _find_atomic_end(self, start: int, expected: list[Token]) -> int:
        expected_identity = [_token_identity(token) for token in expected]
        for end in range(start + 1, len(self.raw) + 1):
            candidate = self.raw[start:end]
            actual = self._parsed_children(candidate)
            if [_token_identity(token) for token in actual] == expected_identity:
                return end
        raise _InlineTopologyError("atomic inline source does not align")

    def _matching_child_close(self, open_index: int) -> int:
        depth = 0
        for index in range(open_index, len(self.children)):
            depth += self.children[index].nesting
            if depth == 0:
                return index
        raise _InlineTopologyError("unbalanced inline child tokens")

    def _scan_link(
        self,
        index: int,
        formats: tuple[str, ...],
    ) -> int:
        close_index = self._matching_child_close(index)
        expected = self.children[index:close_index + 1]
        link_open = self.children[index]
        href = link_open.attrGet("href") or ""
        link_start = self.cursor

        citation = _CITATION.match(self.raw, self.cursor)
        if citation is not None:
            expected_citation = (
                close_index == index + 2
                and self.children[index + 1].type == "text"
                and self.children[index + 1].content == f"[{citation.group('number')}]"
                and href == citation.group("href")
            )
            if not expected_citation:
                raise _InlineTopologyError("citation disagrees with inline tokens")
            self.cursor = citation.end()
            self._add_anchor("citation", link_start, self.cursor)
            return close_index + 1

        if not self.raw.startswith("[", self.cursor):
            raise _InlineTopologyError("only explicit inline links are supported")
        link_end = self._find_atomic_end(link_start, expected)
        if href.startswith("#inference:"):
            self.cursor = link_end
            self._add_anchor("inference", link_start, link_end)
            return close_index + 1

        link_id = f"{self.unit_id}:link:{self.link_count}"
        self.link_count += 1
        self.cursor += 1
        self._add_anchor("syntax", link_start, self.cursor)
        next_index = self._scan_range(
            index + 1,
            close_index,
            formats + ("link",),
            link_id,
        )
        if (
            next_index != close_index
            or self.cursor >= link_end
            or self.raw[self.cursor] != "]"
        ):
            raise _InlineTopologyError("link label source does not align")
        destination_start = self.cursor
        self.cursor = link_end
        self._add_anchor("link_destination", destination_start, link_end)
        return close_index + 1

    def _scan_range(
        self,
        index: int,
        stop: int,
        formats: tuple[str, ...],
        link_id: str | None,
    ) -> int:
        format_types = {
            "strong_open": "strong",
            "em_open": "emphasis",
            "s_open": "strikethrough",
        }
        while index < stop:
            token = self.children[index]
            if token.type == "text":
                self._consume_text(token.content, formats, link_id)
                index += 1
                continue
            if token.type in format_types:
                close_index = self._matching_child_close(index)
                marker = token.markup
                if not marker or not self.raw.startswith(marker, self.cursor):
                    raise _InlineTopologyError("format marker does not align")
                marker_start = self.cursor
                self.cursor += len(marker)
                self._add_anchor("syntax", marker_start, self.cursor)
                reached = self._scan_range(
                    index + 1,
                    close_index,
                    formats + (format_types[token.type],),
                    link_id,
                )
                close = self.children[close_index]
                if reached != close_index or close.markup != marker:
                    raise _InlineTopologyError("format topology is unbalanced")
                if not self.raw.startswith(marker, self.cursor):
                    raise _InlineTopologyError("closing format marker does not align")
                marker_start = self.cursor
                self.cursor += len(marker)
                self._add_anchor("syntax", marker_start, self.cursor)
                index = close_index + 1
                continue
            if token.type == "link_open":
                index = self._scan_link(index, formats)
                continue
            if token.type == "softbreak":
                start = self.cursor
                while self.cursor < len(self.raw) and self.raw[self.cursor] in " \t":
                    self.cursor += 1
                if self.raw.startswith("\r\n", self.cursor):
                    self.cursor += 2
                elif self.cursor < len(self.raw) and self.raw[self.cursor] in "\r\n":
                    self.cursor += 1
                else:
                    raise _InlineTopologyError("soft break does not align")
                self._add_slot(
                    _SlotDraft(
                        start,
                        self.cursor,
                        " ",
                        [self._byte(start), self._byte(self.cursor)],
                        formats,
                        link_id,
                    )
                )
                index += 1
                continue
            if token.type == "hardbreak":
                start = self.cursor
                if self.raw.startswith("\\\r\n", self.cursor):
                    self.cursor += 3
                elif self.raw.startswith("\\\n", self.cursor) or self.raw.startswith(
                    "\\\r", self.cursor
                ):
                    self.cursor += 2
                else:
                    while self.cursor < len(self.raw) and self.raw[self.cursor] == " ":
                        self.cursor += 1
                    if self.cursor - start < 2:
                        raise _InlineTopologyError("hard break marker is missing")
                    if self.raw.startswith("\r\n", self.cursor):
                        self.cursor += 2
                    elif (
                        self.cursor < len(self.raw)
                        and self.raw[self.cursor] in "\r\n"
                    ):
                        self.cursor += 1
                    else:
                        raise _InlineTopologyError("hard break newline is missing")
                self._add_anchor("hard_break", start, self.cursor)
                index += 1
                continue
            if token.type in {"code_inline", "image"}:
                end = self._find_atomic_end(self.cursor, [token])
                start = self.cursor
                self.cursor = end
                self._add_anchor(
                    "inline_code" if token.type == "code_inline" else "image",
                    start,
                    end,
                )
                index += 1
                continue
            raise _InlineTopologyError(f"unsupported inline token: {token.type}")
        return index

    def scan(self) -> tuple[tuple[RewriteSlot, ...], tuple[ProtectedAnchor, ...]]:
        reached = self._scan_range(0, len(self.children), (), None)
        if reached != len(self.children) or self.cursor != len(self.raw):
            raise _InlineTopologyError("inline source was not consumed exactly")
        return tuple(self.slots), tuple(self.protected)


def _inline_source_span(
    markdown: str,
    boundary_table: Utf8BoundaryTable,
    unit: RewriteUnit,
    inline: Token,
) -> tuple[str, int]:
    unit_type = unit.unit_type
    start_byte = unit.start_byte
    end_byte = unit.end_byte
    start = boundary_table.require_byte_boundary(start_byte)
    end = boundary_table.require_byte_boundary(end_byte)
    unit_source = markdown[start:end]
    if unit_type == "paragraph":
        prefix_end = 0
    elif unit_type == "heading":
        prefix = _HEADING_PREFIX.match(unit_source)
        if prefix is None:
            raise _InlineTopologyError("heading prefix does not align")
        prefix_end = prefix.end()
    else:
        prefix = _LIST_PREFIX.match(unit_source)
        if prefix is None:
            raise _InlineTopologyError("list prefix does not align")
        prefix_end = prefix.end()
    if unit_type == "heading":
        if not unit_source.startswith(inline.content, prefix_end):
            raise _InlineTopologyError("inline content is not an exact source slice")
        suffix = unit_source[prefix_end + len(inline.content):]
        if _HEADING_SUFFIX.fullmatch(suffix) is None:
            raise _InlineTopologyError("heading suffix does not align")
        raw = inline.content
    else:
        raw = unit_source[prefix_end:]
        normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
        if normalized != inline.content:
            raise _InlineTopologyError("inline content is not an exact source slice")
    return raw, start + prefix_end


def _scan_unit_inline(
    markdown: str,
    boundary_table: Utf8BoundaryTable,
    unit: RewriteUnit,
    inline: Token,
    parser: MarkdownIt,
) -> tuple[tuple[RewriteSlot, ...], tuple[ProtectedAnchor, ...]]:
    raw, raw_start = _inline_source_span(
        markdown,
        boundary_table,
        unit,
        inline,
    )
    return _InlineScanner(
        raw,
        raw_start,
        unit.unit_id,
        inline.children or [],
        parser,
        boundary_table,
    ).scan()


def _matching_close(tokens: list[Token], open_index: int) -> int | None:
    nesting = 0
    for index in range(open_index, len(tokens)):
        nesting += tokens[index].nesting
        if nesting == 0:
            return index
    return None


def _is_image_only(inline: Token) -> bool:
    children = inline.children or []
    has_image = any(child.type == "image" for child in children)
    non_text_topology = {
        "image",
        "link_open",
        "link_close",
        "softbreak",
        "hardbreak",
    }
    return has_image and all(
        child.type in non_text_topology
        or (child.type == "text" and not child.content.strip())
        for child in children
    )


def _list_item_kind(tokens: list[Token], open_index: int, close_index: int) -> str | None:
    contents = tokens[open_index + 1:close_index]
    if any(token.type in _LIST_OPEN_TYPES for token in contents):
        return "nested_list"
    paragraph_count = sum(token.type == "paragraph_open" for token in contents)
    allowed = {"paragraph_open", "inline", "paragraph_close"}
    if paragraph_count != 1 or any(token.type not in allowed for token in contents):
        return "compound_list_item"
    inline = next((token for token in contents if token.type == "inline"), None)
    if inline is None or _is_image_only(inline):
        return "image_only"
    return None


def build_rewrite_map(markdown: str) -> MarkdownRewriteMap:
    """Classify blocks and map supported inline text to exact source bytes."""
    boundary_table = Utf8BoundaryTable(markdown)
    parser = _new_rewrite_parser()
    tokens = parser.parse(markdown)
    source_lines = _SourceLines(markdown)
    units: list[RewriteUnit] = []
    unsupported: list[UnsupportedRegion] = []
    list_stack: list[str] = []

    def add_unit(
        unit_type: UnitType,
        byte_range: tuple[int, int],
        inline: Token,
        *,
        level: int | None = None,
        list_depth: int | None = None,
        list_marker: str | None = None,
    ) -> None:
        start_byte, end_byte = byte_range
        ordinal = len(units)
        unit = RewriteUnit(
            unit_id=f"{unit_type}_{ordinal}_{start_byte}_{end_byte}",
            unit_type=unit_type,
            start_byte=start_byte,
            end_byte=end_byte,
            level=level,
            list_depth=list_depth,
            list_marker=list_marker,
        )
        try:
            slots, protected = _scan_unit_inline(
                markdown, boundary_table, unit, inline, parser
            )
        except _InlineTopologyError:
            unsupported.append(
                UnsupportedRegion("unsupported_inline", start_byte, end_byte)
            )
            return
        units.append(
            RewriteUnit(
                unit_id=unit.unit_id,
                unit_type=unit.unit_type,
                start_byte=unit.start_byte,
                end_byte=unit.end_byte,
                level=unit.level,
                list_depth=unit.list_depth,
                list_marker=unit.list_marker,
                slots=slots,
                protected=protected,
            )
        )

    def add_unsupported(kind: str, byte_range: tuple[int, int] | None) -> None:
        if byte_range is not None:
            unsupported.append(UnsupportedRegion(kind, *byte_range))

    index = 0
    while index < len(tokens):
        token = tokens[index]

        if token.type in _LIST_OPEN_TYPES:
            list_stack.append(token.type)
            index += 1
            continue
        if token.type in _LIST_CLOSE_TYPES:
            if list_stack:
                list_stack.pop()
            index += 1
            continue

        if token.type == "list_item_open":
            close_index = _matching_close(tokens, index)
            byte_range = source_lines.byte_range(token.map)
            if close_index is None or byte_range is None:
                add_unsupported("ambiguous_list_item", byte_range)
                index += 1
                continue
            depth = len(list_stack) - 1
            unsupported_kind = _list_item_kind(tokens, index, close_index)
            inline = next(
                (
                    candidate
                    for candidate in tokens[index + 1:close_index]
                    if candidate.type == "inline"
                ),
                None,
            )
            if depth > 0:
                unsupported_kind = "nested_list"
            if inline is None:
                unsupported_kind = "ambiguous_list_item"
            if unsupported_kind is not None:
                add_unsupported(unsupported_kind, byte_range)
            else:
                source_line = source_lines.line(token.map[0]) if token.map else None
                marker_match = _LIST_MARKER.match(source_line or "")
                if marker_match is None:
                    add_unsupported("ambiguous_list_item", byte_range)
                else:
                    add_unit(
                        "list_item",
                        byte_range,
                        inline,
                        list_depth=depth,
                        list_marker=marker_match.group("marker"),
                    )
            index = close_index + 1
            continue

        if token.level != 0:
            index += 1
            continue

        byte_range = source_lines.byte_range(token.map)
        if token.type == "heading_open":
            close_index = _matching_close(tokens, index)
            inline = tokens[index + 1] if index + 1 < len(tokens) else None
            valid_shape = (
                close_index == index + 2
                and inline is not None
                and inline.type == "inline"
                and tokens[close_index].type == "heading_close"
            )
            valid_tag = token.tag.startswith("h") and token.tag[1:].isdigit()
            if byte_range is None or not valid_shape or not valid_tag:
                add_unsupported("ambiguous_heading", byte_range)
            elif _is_image_only(inline):
                add_unsupported("image_only", byte_range)
            else:
                add_unit(
                    "heading", byte_range, inline, level=int(token.tag[1:])
                )
            index = close_index + 1 if close_index is not None else index + 1
            continue

        if token.type == "paragraph_open":
            close_index = _matching_close(tokens, index)
            inline = tokens[index + 1] if index + 1 < len(tokens) else None
            valid_shape = (
                close_index is not None
                and byte_range is not None
                and inline is not None
                and inline.type == "inline"
            )
            if not valid_shape:
                add_unsupported("ambiguous_paragraph", byte_range)
            elif _is_image_only(inline):
                add_unsupported("image_only", byte_range)
            else:
                add_unit("paragraph", byte_range, inline)
            index = close_index + 1 if close_index is not None else index + 1
            continue

        unsupported_kind = _UNSUPPORTED_BLOCK_KINDS.get(token.type)
        if unsupported_kind is not None:
            add_unsupported(unsupported_kind, byte_range)
            close_index = _matching_close(tokens, index) if token.nesting == 1 else None
            index = close_index + 1 if close_index is not None else index + 1
            continue

        if token.map is not None and token.nesting >= 0:
            add_unsupported(token.type, byte_range)
        index += 1

    return MarkdownRewriteMap(markdown, tuple(units), tuple(unsupported))


def _heading_visible_text(inline: Token) -> tuple[str, bool]:
    visible: list[str] = []
    ambiguous = False
    for child in inline.children or []:
        if child.type in {"text", "code_inline"}:
            visible.append(child.content)
        elif child.type in {"softbreak", "hardbreak"}:
            visible.append(" ")
        elif child.type == "image":
            visible.append(child.content)
        elif child.type not in {
            "link_open",
            "link_close",
            "strong_open",
            "strong_close",
            "em_open",
            "em_close",
            "s_open",
            "s_close",
        }:
            ambiguous = True
    return "".join(visible), ambiguous


def build_document_anchor_index(markdown: str) -> DocumentAnchorIndex:
    """Index headings and same-document links across supported and unsupported blocks."""
    parser = _new_rewrite_parser()
    tokens = parser.parse(markdown)
    source_lines = _SourceLines(markdown)
    boundaries = Utf8BoundaryTable(markdown)
    headings: list[DocumentHeading] = []
    expected_links: list[str] = []
    candidates: dict[tuple[int, int], DocumentInternalLink] = {}
    ambiguous = False

    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            inline = tokens[index + 1] if index + 1 < len(tokens) else None
            if inline is None or inline.type != "inline":
                ambiguous = True
            else:
                text, heading_ambiguous = _heading_visible_text(inline)
                headings.append(DocumentHeading(text))
                ambiguous = ambiguous or heading_ambiguous
        if token.type != "inline":
            continue
        internal_hrefs = []
        for child in token.children or []:
            if child.type != "link_open":
                continue
            href = unquote(child.attrGet("href") or "")
            if href.startswith("#"):
                internal_hrefs.append(href[1:])
        if not internal_hrefs:
            continue
        expected_links.extend(internal_hrefs)
        byte_range = source_lines.byte_range(token.map)
        if byte_range is None:
            ambiguous = True
            continue
        start_byte, end_byte = byte_range
        start_codepoint = boundaries.require_byte_boundary(start_byte)
        end_codepoint = boundaries.require_byte_boundary(end_byte)
        raw = markdown[start_codepoint:end_codepoint]
        for match in _INTERNAL_LINK_SOURCE.finditer(raw):
            destination = match.group("destination")
            destination_start = start_byte + len(
                raw[: match.start("destination")].encode("utf-8")
            )
            destination_end = destination_start + len(destination.encode("utf-8"))
            candidates[(destination_start, destination_end)] = DocumentInternalLink(
                target=unquote(destination)[1:],
                start_byte=destination_start,
                end_byte=destination_end,
                source=destination,
            )

    links = tuple(candidates[key] for key in sorted(candidates))
    if Counter(link.target for link in links) != Counter(expected_links):
        ambiguous = True
    return DocumentAnchorIndex(
        tuple(headings), links, tuple(expected_links), ambiguous
    )


def structure_signature(rewrite_map: MarkdownRewriteMap) -> tuple[object, ...]:
    """Return topology and protected identities, excluding editable text."""

    units: list[object] = []
    for unit in rewrite_map.units:
        format_topology: list[tuple[str, ...]] = []
        for slot in unit.slots:
            if not format_topology or format_topology[-1] != slot.formats:
                format_topology.append(slot.formats)
        anchors = []
        for anchor in unit.protected:
            identity: str | None = None
            if anchor.kind in {"link_destination", "citation"}:
                identity = hashlib.sha256(anchor.source.encode("utf-8")).hexdigest()
            anchors.append((anchor.kind, identity))
        units.append(
            (
                unit.unit_type,
                unit.level,
                unit.list_depth,
                unit.list_marker,
                tuple(format_topology),
                tuple(anchors),
            )
        )
    return tuple(units)


def reconstruct_markdown(
    rewrite_map: MarkdownRewriteMap,
    slot_texts: dict[str, str] | None = None,
    *,
    selected_ranges: dict[str, tuple[int, int]] | None = None,
) -> str:
    """Validate a map and replace requested slot byte ranges from back to front."""

    def conflict(message: str) -> None:
        raise RewriteMapError("SELECTION_MAPPING_CONFLICT", message)

    if not isinstance(rewrite_map.source, str):
        conflict("rewrite map source must be text")
    if rewrite_map.source_sha256 != hashlib.sha256(
        rewrite_map.source.encode("utf-8")
    ).hexdigest():
        conflict("rewrite map source digest does not match its source")
    if rewrite_map.unsupported_regions and selected_ranges is None:
        conflict("cannot reconstruct a map containing unsupported regions")

    expected = {
        slot.slot_id: slot.text
        for unit in rewrite_map.units
        for slot in unit.slots
    }
    requests = expected if slot_texts is None else slot_texts
    if not isinstance(requests, dict) or any(
        not isinstance(slot_id, str) or not isinstance(text, str)
        for slot_id, text in requests.items()
    ):
        conflict("inline reconstruction requests must map slot IDs to text")
    if selected_ranges is None:
        if set(requests) != set(expected):
            conflict("inline reconstruction requires every slot exactly once")
    elif (
        not isinstance(selected_ranges, dict)
        or set(requests) != set(selected_ranges)
        or not set(requests).issubset(expected)
    ):
        conflict("selected reconstruction ranges must match known slot requests")

    source_bytes = rewrite_map.source.encode("utf-8")
    boundaries = Utf8BoundaryTable(rewrite_map.source)
    segments: list[tuple[int, int, Literal["slot", "protected"], object]] = []
    for unit in rewrite_map.units:
        for slot in unit.slots:
            segments.append((slot.start_byte, slot.end_byte, "slot", slot))
        for anchor in unit.protected:
            segments.append(
                (anchor.start_byte, anchor.end_byte, "protected", anchor)
            )
    segments.sort(key=lambda segment: (segment[0], segment[1]))

    output = bytearray()
    previous_end = 0
    for start_byte, end_byte, segment_kind, segment in segments:
        try:
            boundaries.require_byte_boundary(start_byte)
            boundaries.require_byte_boundary(end_byte)
        except RewriteMapError:
            conflict("inline range is not on a UTF-8 boundary")
        if start_byte < previous_end or start_byte > end_byte:
            conflict("inline ranges must be ordered and non-overlapping")
        output.extend(source_bytes[previous_end:start_byte])

        if segment_kind == "protected":
            anchor = segment
            if not isinstance(anchor, ProtectedAnchor):
                conflict("invalid protected anchor")
            anchor_bytes = anchor.source.encode("utf-8")
            if source_bytes[start_byte:end_byte] != anchor_bytes:
                conflict("protected anchor source does not match its byte range")
            output.extend(anchor_bytes)
        else:
            slot = segment
            if not isinstance(slot, RewriteSlot):
                conflict("invalid rewrite slot")
            visible_boundaries = slot.visible_boundary_to_byte
            covers_slot_range = (
                visible_boundaries
                and visible_boundaries[0] == start_byte
                and visible_boundaries[-1] == end_byte
            )
            if len(visible_boundaries) != len(slot.text) + 1 or not covers_slot_range:
                conflict("slot visible boundaries do not cover its byte range")
            for index, character in enumerate(slot.text):
                visible_start = visible_boundaries[index]
                visible_end = visible_boundaries[index + 1]
                try:
                    boundaries.require_byte_boundary(visible_start)
                    boundaries.require_byte_boundary(visible_end)
                except RewriteMapError:
                    conflict("slot visible range is not on a UTF-8 boundary")
                if not start_byte <= visible_start < visible_end <= end_byte:
                    conflict("slot visible boundaries must be strictly ordered")
                raw_visible = source_bytes[visible_start:visible_end].decode("utf-8")
                literal = raw_visible == character
                escaped = (
                    len(raw_visible) == 2
                    and raw_visible[0] == "\\"
                    and raw_visible[1] == character
                    and character in _ESCAPABLE
                )
                soft_break = character == " " and re.fullmatch(
                    r"[ \t]*(?:\r\n|\r|\n)", raw_visible
                )
                entity = (
                    _decode_entity_at(raw_visible, 0)
                    if raw_visible[:1] == "&"
                    else None
                )
                entity_visible = (
                    entity is not None
                    and entity[0] == character
                    and entity[1] == len(raw_visible)
                )
                if not (literal or escaped or soft_break or entity_visible):
                    conflict("slot visible text does not match its source bytes")
            output.extend(source_bytes[start_byte:end_byte])
        previous_end = end_byte

    output.extend(source_bytes[previous_end:])
    try:
        reconstructed = output.decode("utf-8")
    except UnicodeDecodeError:
        conflict("reconstructed markdown is not valid UTF-8")
    if reconstructed != rewrite_map.source:
        conflict("reconstructed markdown differs from the original source")
    rebuilt = build_rewrite_map(rewrite_map.source)
    if rebuilt != rewrite_map:
        conflict("rewrite map no longer matches its source")

    slots_by_id = {
        slot.slot_id: slot
        for unit in rewrite_map.units
        for slot in unit.slots
    }
    replacements: list[tuple[int, int, bytes]] = []
    syntax_anchors = []
    for unit in rewrite_map.units:
        for anchor in unit.protected:
            if anchor.kind == "syntax" and anchor.source in {"*", "_", "**", "__"}:
                syntax_anchors.append(anchor)
    for slot_id, replacement in requests.items():
        slot = slots_by_id[slot_id]
        if selected_ranges is None:
            start_byte, end_byte = slot.start_byte, slot.end_byte
            original_visible = slot.text
        else:
            byte_range = selected_ranges[slot_id]
            valid_range_shape = (
                isinstance(byte_range, tuple)
                and len(byte_range) == 2
                and type(byte_range[0]) is int
                and type(byte_range[1]) is int
            )
            if not valid_range_shape:
                conflict("selected reconstruction range must contain two byte offsets")
            start_byte, end_byte = byte_range
            try:
                visible_start = slot.visible_boundary_to_byte.index(start_byte)
                visible_end = slot.visible_boundary_to_byte.index(end_byte)
            except ValueError:
                conflict("selected reconstruction range is not on a visible boundary")
            if visible_start > visible_end:
                conflict("selected reconstruction range is reversed")
            original_visible = slot.text[visible_start:visible_end]
        if replacement != original_visible:
            replacements.append(
                (
                    start_byte,
                    end_byte,
                    _encode_markdown_literal(replacement).encode("utf-8"),
                )
            )
        removes_formatted_slot = (
            selected_ranges is not None
            and not replacement
            and start_byte == slot.start_byte
            and end_byte == slot.end_byte
        )
        simple_inline_formats = (
            bool(slot.formats) and set(slot.formats) <= {"strong", "emphasis"}
        )
        if removes_formatted_slot and simple_inline_formats:
            left = []
            cursor = start_byte
            for anchor in reversed(syntax_anchors):
                if anchor.end_byte == cursor:
                    left.append(anchor)
                    cursor = anchor.start_byte
                    if len(left) == len(slot.formats):
                        break
            right = []
            cursor = end_byte
            for anchor in syntax_anchors:
                if anchor.start_byte == cursor:
                    right.append(anchor)
                    cursor = anchor.end_byte
                    if len(right) == len(slot.formats):
                        break
            if len(left) == len(right) == len(slot.formats):
                for anchor in (*left, *right):
                    replacements.append((anchor.start_byte, anchor.end_byte, b""))

    replacements.sort(key=lambda item: (item[0], item[1]))
    if any(
        left[1] > right[0]
        for left, right in zip(replacements, replacements[1:])
    ):
        conflict("selected reconstruction ranges overlap")
    result = source_bytes
    for start_byte, end_byte, replacement in reversed(replacements):
        result = result[:start_byte] + replacement + result[end_byte:]
    try:
        return result.decode("utf-8")
    except UnicodeDecodeError:
        conflict("reconstructed markdown is not valid UTF-8")


def visible_slot_byte_ranges(
    source: str,
    slot: RewriteSlot,
    visible_start: int,
    visible_end: int,
) -> tuple[tuple[int, int], ...]:
    """Return source byte ranges for visible slot characters, excluding escapes."""
    if not 0 <= visible_start <= visible_end <= len(slot.text):
        raise RewriteMapError(
            "SELECTION_MAPPING_CONFLICT", "visible slot range is invalid"
        )
    source_bytes = source.encode("utf-8")
    ranges: list[tuple[int, int]] = []
    for index in range(visible_start, visible_end):
        start = slot.visible_boundary_to_byte[index]
        end = slot.visible_boundary_to_byte[index + 1]
        raw = source_bytes[start:end]
        if raw.startswith(b"\\") and len(raw) > 1:
            start += 1
            raw = raw[1:]
        if raw in {b"\n", b"\r", b"\r\n"} or not raw:
            continue
        if ranges and ranges[-1][1] == start:
            ranges[-1] = (ranges[-1][0], end)
        else:
            ranges.append((start, end))
    return tuple(ranges)
