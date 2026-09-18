import hashlib
import json
import re
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.tools.deepresearch_plugin import (
    markdown_rewrite_map as rewrite_map_module,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch_plugin.markdown_rewrite_map import (
    ProtectedAnchor,
    RewriteMapError,
    RewriteSlot,
    Utf8BoundaryTable,
    sha256_byte_range,
)


FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "deepresearch_rewrite_protocol_v2.json"
)
EXPECTED_CASE_IDS = {
    "duplicate_text_second_occurrence",
    "emoji_before_selection",
    "cjk_selection",
    "combining_character_selection",
    "lf_document",
    "crlf_document",
    "strong_content_only",
    "emphasis_content_only",
    "ordinary_link_label_only",
    "multiple_citations_between_endpoints",
    "soft_break_normalizes_to_space",
    "hard_break_between_endpoints",
    "heading_partial",
    "paragraph_partial",
    "same_level_list_items",
    "mixed_heading_paragraph_list",
    "partial_outer_units_complete_middle",
    "selected_text_mismatch_rejected",
    "nested_list_rejected",
    "table_rejected",
    "fenced_code_rejected",
    "html_block_rejected",
    "image_rejected",
    "inference_rejected",
}
STABLE_ERROR_CODES = {
    "INFERENCE_NOT_REWRITABLE",
    "PROTECTED_ANCHOR_ENDPOINT",
    "SELECTION_MAPPING_CONFLICT",
    "UNSUPPORTED_BLOCK_KIND",
    "UNSUPPORTED_NESTED_LIST",
}
VISIBLE_TEXT_CONVENTIONS = {
    "citation_link_labels",
    "hard_break_as_newline",
    "image_alt_text",
    "literal",
    "soft_break_as_space",
    "table_cells",
    "unit_separator_newline",
}
COMMON_CASE_KEYS = {
    "id",
    "markdown",
    "start_byte",
    "end_byte",
    "selected_text",
    "source_sha256",
    "accepted",
    "raw_selection",
    "visible_text_convention",
}
COMMON_UNIT_KEYS = {"kind", "index", "coverage"}


def _visible_selection(raw_selection: str, convention: str) -> str:
    if convention == "literal":
        return raw_selection
    if convention == "citation_link_labels":
        return re.sub(r"\[\[(\d+)\]\]\([^)]+\)", r"[\1]", raw_selection)
    if convention == "soft_break_as_space":
        return raw_selection.replace("\n", " ")
    if convention == "hard_break_as_newline":
        return raw_selection.replace("  \n", "\n")
    if convention == "unit_separator_newline":
        normalized = raw_selection.replace("\r\n", "\n")
        return re.sub(r"\n+(?:\s*[-+*]\s+)?", "\n", normalized)
    if convention == "table_cells":
        return re.sub(r"\s*\|\s*", " ", raw_selection)
    if convention == "image_alt_text":
        return raw_selection
    raise AssertionError(f"fixture uses unknown convention: {convention}")


def test_utf8_boundary_table_maps_codepoints_and_byte_boundaries():
    table = Utf8BoundaryTable("A😀中e\u0301")

    assert table.codepoint_to_byte == (0, 1, 5, 8, 9, 11)
    assert table.require_byte_boundary(5) == 2


def test_utf8_boundary_table_is_frozen_and_slotted():
    table = Utf8BoundaryTable("text")

    with pytest.raises(FrozenInstanceError):
        table.text = "changed"
    assert not hasattr(table, "__dict__")


@pytest.mark.parametrize("offset", [2, 3, 4])
def test_utf8_boundary_table_rejects_offsets_inside_multibyte_codepoint(offset):
    table = Utf8BoundaryTable("A😀中")

    with pytest.raises(RewriteMapError) as caught:
        table.require_byte_boundary(offset)

    assert caught.value.code == "SELECTION_MAPPING_CONFLICT"


@pytest.mark.parametrize("offset", [-1, 9, 1.0, True])
def test_utf8_boundary_table_rejects_out_of_range_offsets(offset):
    table = Utf8BoundaryTable("A😀中")

    with pytest.raises(RewriteMapError) as caught:
        table.require_byte_boundary(offset)

    assert caught.value.code == "SELECTION_MAPPING_CONFLICT"


def test_sha256_byte_range_hashes_utf8_half_open_range():
    assert sha256_byte_range("A😀中", 1, 8) == hashlib.sha256(
        "😀中".encode("utf-8")
    ).hexdigest()


def test_sha256_byte_range_allows_empty_boundary_aligned_range():
    assert sha256_byte_range("A😀中", 5, 5) == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize(
    ("start_byte", "end_byte"),
    [(5, 1), (-1, 1), (0, 9), (2, 8), (1, 7)],
)
def test_sha256_byte_range_rejects_invalid_ranges(start_byte, end_byte):
    with pytest.raises(RewriteMapError) as caught:
        sha256_byte_range("A😀中", start_byte, end_byte)

    assert caught.value.code == "SELECTION_MAPPING_CONFLICT"


def test_rewrite_map_public_api_exists():
    assert hasattr(rewrite_map_module, "RewriteUnit")
    assert hasattr(rewrite_map_module, "UnsupportedRegion")
    assert hasattr(rewrite_map_module, "MarkdownRewriteMap")
    assert hasattr(rewrite_map_module, "build_rewrite_map")


def test_build_rewrite_map_classifies_supported_blocks_and_preserves_raw_ranges():
    markdown = (
        "# First\n\n"
        "## Second\n\n"
        "paragraph line one\nparagraph line two\n\n"
        "- alpha\n- beta\n\n"
        "1. one\n2. two\n"
    )

    rewrite_map = rewrite_map_module.build_rewrite_map(markdown)

    assert rewrite_map.source == markdown
    assert rewrite_map.unsupported_regions == ()
    assert [unit.unit_type for unit in rewrite_map.units] == [
        "heading",
        "heading",
        "paragraph",
        "list_item",
        "list_item",
        "list_item",
        "list_item",
    ]
    assert [unit.level for unit in rewrite_map.units] == [1, 2, None, None, None, None, None]
    assert [unit.list_depth for unit in rewrite_map.units] == [
        None,
        None,
        None,
        0,
        0,
        0,
        0,
    ]
    assert [unit.list_marker for unit in rewrite_map.units] == [
        None,
        None,
        None,
        "-",
        "-",
        "1.",
        "2.",
    ]
    source_bytes = markdown.encode("utf-8")
    assert [source_bytes[unit.start_byte : unit.end_byte].decode() for unit in rewrite_map.units] == [
        "# First",
        "## Second",
        "paragraph line one\nparagraph line two",
        "- alpha",
        "- beta",
        "1. one",
        "2. two",
    ]
    assert [unit.unit_id for unit in rewrite_map.units] == [
        f"{unit.unit_type}_{ordinal}_{unit.start_byte}_{unit.end_byte}"
        for ordinal, unit in enumerate(rewrite_map.units)
    ]


def test_build_rewrite_map_uses_utf8_byte_offsets_after_unicode_prefix():
    markdown = "前言\n\n# 标题\n"

    rewrite_map = rewrite_map_module.build_rewrite_map(markdown)

    assert [(unit.start_byte, unit.end_byte) for unit in rewrite_map.units] == [
        (0, len("前言".encode("utf-8"))),
        (len("前言\n\n".encode("utf-8")), len("前言\n\n# 标题".encode("utf-8"))),
    ]


def test_build_rewrite_map_keeps_unicode_line_separator_inside_paragraph_range():
    markdown = "first\u2028second\n"

    rewrite_map = rewrite_map_module.build_rewrite_map(markdown)

    unit = rewrite_map.units[0]
    assert markdown.encode("utf-8")[unit.start_byte : unit.end_byte].decode() == (
        "first\u2028second"
    )


def test_build_rewrite_map_excludes_space_tab_only_list_separator_from_item_range():
    markdown = "- first\n \t \n- second\n"

    rewrite_map = rewrite_map_module.build_rewrite_map(markdown)

    source_bytes = markdown.encode("utf-8")
    assert [
        source_bytes[unit.start_byte : unit.end_byte].decode()
        for unit in rewrite_map.units
    ] == ["- first", "- second"]


@pytest.mark.parametrize(
    "markdown",
    ["first\r\n\r\nsecond\r\n", "first\r\rsecond\r"],
)
def test_build_rewrite_map_maps_crlf_and_lone_cr_source_lines(markdown):
    rewrite_map = rewrite_map_module.build_rewrite_map(markdown)

    source_bytes = markdown.encode("utf-8")
    assert [
        source_bytes[unit.start_byte : unit.end_byte].decode()
        for unit in rewrite_map.units
    ] == ["first", "second"]


def test_build_rewrite_map_keeps_paragraph_with_inline_image_for_later_slot_validation():
    rewrite_map = rewrite_map_module.build_rewrite_map(
        "text ![a](a.png) ![b](b.png) remains text\n"
    )

    assert [unit.unit_type for unit in rewrite_map.units] == ["paragraph"]
    assert rewrite_map.unsupported_regions == ()


def test_build_rewrite_map_rejects_image_only_heading():
    markdown = "# ![alt](x.png)\n"

    rewrite_map = rewrite_map_module.build_rewrite_map(markdown)

    assert rewrite_map.units == ()
    assert [region.kind for region in rewrite_map.unsupported_regions] == ["image_only"]


def test_build_rewrite_map_keeps_heading_with_visible_text_and_image():
    rewrite_map = rewrite_map_module.build_rewrite_map("# text ![alt](x.png)\n")

    assert [(unit.unit_type, unit.level) for unit in rewrite_map.units] == [
        ("heading", 1)
    ]
    assert rewrite_map.unsupported_regions == ()


def test_build_rewrite_map_rejects_multiple_images_separated_only_by_whitespace():
    markdown = "![a](a.png) ![b](b.png)\n"

    rewrite_map = rewrite_map_module.build_rewrite_map(markdown)

    assert rewrite_map.units == ()
    assert [
        (region.kind, region.start_byte, region.end_byte)
        for region in rewrite_map.unsupported_regions
    ] == [("image_only", 0, len(markdown.rstrip("\n").encode("utf-8")))]


def test_build_rewrite_map_rejects_list_item_of_images_separated_only_by_whitespace():
    markdown = "- ![a](a.png) ![b](b.png)\n"

    rewrite_map = rewrite_map_module.build_rewrite_map(markdown)

    assert rewrite_map.units == ()
    assert [
        (region.kind, region.start_byte, region.end_byte)
        for region in rewrite_map.unsupported_regions
    ] == [("image_only", 0, len(markdown.rstrip("\n").encode("utf-8")))]


@pytest.mark.parametrize(
    ("kind", "markdown", "raw_region"),
    [
        ("blockquote", "> quoted", "> quoted"),
        ("table", "| a | b |\n|---|---|\n| c | d |", "| a | b |\n|---|---|\n| c | d |"),
        ("fenced_code", "```python\nx = 1\n```", "```python\nx = 1\n```"),
        ("indented_code", "    x = 1", "    x = 1"),
        ("html_block", "<div>content</div>", "<div>content</div>"),
        ("image_only", "![alt](image.png)", "![alt](image.png)"),
        ("nested_list", "- outer\n  - inner", "- outer\n  - inner"),
        (
            "compound_list_item",
            "- first paragraph\n\n  second paragraph",
            "- first paragraph\n\n  second paragraph",
        ),
    ],
)
def test_build_rewrite_map_marks_unsupported_blocks_without_paragraph_fallback(
    kind, markdown, raw_region
):
    rewrite_map = rewrite_map_module.build_rewrite_map(markdown + "\n")

    assert rewrite_map.units == ()
    assert len(rewrite_map.unsupported_regions) == 1
    region = rewrite_map.unsupported_regions[0]
    assert region.kind == kind
    assert markdown.encode("utf-8")[region.start_byte : region.end_byte].decode() == raw_region


def test_build_rewrite_map_parses_sibling_after_unsupported_nested_list_item():
    markdown = "- outer\n  - nested\n- sibling\n"

    rewrite_map = rewrite_map_module.build_rewrite_map(markdown)

    assert [region.kind for region in rewrite_map.unsupported_regions] == ["nested_list"]
    assert [(unit.unit_type, unit.list_depth, unit.list_marker) for unit in rewrite_map.units] == [
        ("list_item", 0, "-")
    ]
    sibling = rewrite_map.units[0]
    assert markdown.encode("utf-8")[sibling.start_byte : sibling.end_byte].decode() == (
        "- sibling"
    )


def test_inline_slots_exclude_link_destination_and_citation():
    markdown = (
        "市场正在**快速增长**，[官方报告](https://example.com/report \"title\")"
        "已确认。[[1]](https://example.com/source)"
    )

    rewrite_map = rewrite_map_module.build_rewrite_map(markdown)
    unit = rewrite_map.units[0]

    assert [(slot.text, slot.formats, slot.link_id) for slot in unit.slots] == [
        ("市场正在", (), None),
        ("快速增长", ("strong",), None),
        ("，", (), None),
        ("官方报告", ("link",), f"{unit.unit_id}:link:0"),
        ("已确认。", (), None),
    ]
    assert [slot.slot_id for slot in unit.slots] == [
        f"{unit.unit_id}:slot:{ordinal}:{slot.start_byte}:{slot.end_byte}"
        for ordinal, slot in enumerate(unit.slots)
    ]
    assert [anchor.kind for anchor in unit.protected] == [
        "syntax",
        "syntax",
        "syntax",
        "link_destination",
        "citation",
    ]
    protected_source = "".join(anchor.source for anchor in unit.protected)
    assert "https://example.com/report" in protected_source
    assert "[[1]](https://example.com/source)" in protected_source
    assert all("http" not in slot.text for slot in unit.slots)


def test_nested_inline_formats_have_balanced_protected_markers():
    markdown = "**outer *inner ~~strike~~ tail* end**"

    unit = rewrite_map_module.build_rewrite_map(markdown).units[0]

    assert [(slot.text, slot.formats) for slot in unit.slots] == [
        ("outer ", ("strong",)),
        ("inner ", ("strong", "emphasis")),
        ("strike", ("strong", "emphasis", "strikethrough")),
        (" tail", ("strong", "emphasis")),
        (" end", ("strong",)),
    ]
    assert [anchor.source for anchor in unit.protected] == [
        "**", "*", "~~", "~~", "*", "**"
    ]
    assert all(anchor.kind == "syntax" for anchor in unit.protected)


def test_multiple_citations_are_atomic_and_keep_identity_order_and_ranges():
    markdown = "Claim [[12]](https://a.example) then [[3]](http://b.example)."

    unit = rewrite_map_module.build_rewrite_map(markdown).units[0]
    citations = [anchor for anchor in unit.protected if anchor.kind == "citation"]

    assert [anchor.anchor_id for anchor in citations] == [
        f"{unit.unit_id}:anchor:0:{len('Claim '.encode('utf-8'))}:31",
        f"{unit.unit_id}:anchor:1:37:60",
    ]
    assert [anchor.source for anchor in citations] == [
        "[[12]](https://a.example)",
        "[[3]](http://b.example)",
    ]
    source_bytes = markdown.encode("utf-8")
    assert [source_bytes[a.start_byte:a.end_byte].decode() for a in citations] == [
        a.source for a in citations
    ]


@pytest.mark.parametrize(
    "markdown,kind,source",
    [
        ("first\nsecond", "soft", " "),
        ("first  \nsecond", "hard", "  \n"),
        ("first\\\nsecond", "hard", "\\\n"),
    ],
)
def test_soft_break_is_editable_space_and_hard_breaks_are_protected(
    markdown, kind, source
):
    unit = rewrite_map_module.build_rewrite_map(markdown).units[0]

    if kind == "soft":
        whitespace = next(slot for slot in unit.slots if slot.text == " ")
        assert whitespace.visible_boundary_to_byte == (5, 6)
        assert unit.protected == ()
    else:
        hard_break = next(
            anchor for anchor in unit.protected if anchor.kind == "hard_break"
        )
        assert hard_break.source == source
        assert [slot.text for slot in unit.slots] == ["first", "second"]


def test_atomic_inline_constructs_are_protected_but_mixed_unit_remains():
    markdown = "before `code` ![alt](x.png) [claim](#inference:proof-1) after"

    rewrite_map = rewrite_map_module.build_rewrite_map(markdown)
    unit = rewrite_map.units[0]

    assert rewrite_map.unsupported_regions == ()
    assert [slot.text for slot in unit.slots] == ["before ", " ", " ", " after"]
    assert [(anchor.kind, anchor.source) for anchor in unit.protected] == [
        ("inline_code", "`code`"),
        ("image", "![alt](x.png)"),
        ("inference", "[claim](#inference:proof-1)"),
    ]


def test_inference_link_is_identified_by_destination_and_is_fully_protected():
    markdown = "before [evidence](#inference:claim-1) after"

    unit = rewrite_map_module.build_rewrite_map(markdown).units[0]

    assert [slot.text for slot in unit.slots] == ["before ", " after"]
    assert [(anchor.kind, anchor.source) for anchor in unit.protected] == [
        ("inference", "[evidence](#inference:claim-1)")
    ]


def test_inference_like_label_with_ordinary_href_remains_editable_link():
    markdown = "[#inference: documentation](https://ordinary.example)"

    unit = rewrite_map_module.build_rewrite_map(markdown).units[0]

    assert [(slot.text, slot.formats, slot.link_id) for slot in unit.slots] == [
        ("#inference: documentation", ("link",), f"{unit.unit_id}:link:0")
    ]
    assert [anchor.kind for anchor in unit.protected] == [
        "syntax",
        "link_destination",
    ]


def test_visible_boundary_map_handles_escape_emoji_cjk_and_combining_character():
    markdown = r"escaped \* 😀中文 é"

    slot = rewrite_map_module.build_rewrite_map(markdown).units[0].slots[0]

    assert slot.text == "escaped * 😀中文 é"
    raw_boundaries = Utf8BoundaryTable(markdown).codepoint_to_byte
    # The rendered '*' consumes both the source backslash and '*'.
    assert slot.visible_boundary_to_byte[8:10] == (
        raw_boundaries[8], raw_boundaries[10]
    )
    assert slot.visible_boundary_to_byte[-5:] == raw_boundaries[-5:]


def test_plain_ascii_punctuation_remains_editable_text():
    markdown = "Growth: 10% faster! snake_case."

    unit = rewrite_map_module.build_rewrite_map(markdown).units[0]

    assert [slot.text for slot in unit.slots] == [markdown]
    assert unit.protected == ()


@pytest.mark.parametrize(
    "markdown,break_source",
    [
        ("first\r\nsecond", None),
        ("first  \r\nsecond", "  \r\n"),
        ("first\\\r\nsecond", "\\\r\n"),
    ],
)
def test_crlf_inline_breaks_keep_exact_source_ranges(markdown, break_source):
    unit = rewrite_map_module.build_rewrite_map(markdown).units[0]

    if break_source is None:
        whitespace = next(slot for slot in unit.slots if slot.text == " ")
        assert whitespace.visible_boundary_to_byte == (5, 7)
    else:
        hard_break = next(
            anchor for anchor in unit.protected if anchor.kind == "hard_break"
        )
        assert hard_break.source == break_source


@pytest.mark.parametrize(
    "markdown",
    [
        "unterminated **strong",
        "<https://example.com>",
    ],
)
def test_ambiguous_inline_topology_fails_closed(markdown):
    rewrite_map = rewrite_map_module.build_rewrite_map(markdown)

    assert rewrite_map.units == ()
    assert [region.kind for region in rewrite_map.unsupported_regions] == [
        "unsupported_inline"
    ]


def test_broken_link_destination_stays_supported_as_literal_text():
    # A malformed angle-bracket destination emits no link tokens, so the whole
    # span is literal text; `<` alone is not an unmatched construct. The
    # relay-claw frontend accepts this paragraph, so the sidecar must too.
    markdown = "[label](<bad destination)"

    unit = rewrite_map_module.build_rewrite_map(markdown).units[0]

    assert [slot.text for slot in unit.slots] == [markdown]
    assert unit.protected == ()


@pytest.mark.parametrize(
    ("markdown", "expects_literal_stars"),
    [
        # With the frontend's CJK-friendly flanking rules, a CJK character
        # before the opener lets `**` pair even when the follower is a
        # non-CJK punctuation mark (the math symbol `<=`), so the markers are
        # stripped as strong syntax — matching the relay-claw frontend.
        ("每周**≤2次**。", False),
        # A Latin word before the opener still cannot open next to
        # punctuation, so markdown-it emits the `**` as literal text. The
        # engine must consume those literal markers as text instead of
        # demoting the whole paragraph.
        ("x**≤**y", True),
        # Intraword flanking succeeds and the markers are stripped from the
        # slot text; the paragraph is still supported.
        ("a**b≤c**d", False),
    ],
)
def test_balanced_literal_format_markers_stay_supported(markdown, expects_literal_stars):
    rewrite_map = rewrite_map_module.build_rewrite_map(markdown)

    assert len(rewrite_map.units) == 1
    assert rewrite_map.units[0].unit_type == "paragraph"
    assert rewrite_map.unsupported_regions == ()
    slot_texts = [slot.text for slot in rewrite_map.units[0].slots]
    assert any("**" in text for text in slot_texts) is expects_literal_stars


def test_inline_map_types_are_frozen_and_slotted():
    slot = RewriteSlot("slot", 0, 1, "x", (), None, (0, 1))
    anchor = ProtectedAnchor("anchor", "syntax", 0, 1, "*")

    for instance, attribute in [(slot, "text"), (anchor, "source")]:
        with pytest.raises(FrozenInstanceError):
            setattr(instance, attribute, getattr(instance, attribute))
        assert not hasattr(instance, "__dict__")


def test_structure_signature_ignores_editable_text_but_locks_inline_topology():
    first = rewrite_map_module.build_rewrite_map(
        "# **Alpha** [label](https://example.com) [[1]](https://source.example)"
    )
    second = rewrite_map_module.build_rewrite_map(
        "# **Bravo** [other](https://example.com) [[1]](https://source.example)"
    )
    changed_destination = rewrite_map_module.build_rewrite_map(
        "# **Bravo** [other](https://changed.example) [[1]](https://source.example)"
    )

    assert rewrite_map_module.structure_signature(first) == (
        rewrite_map_module.structure_signature(second)
    )
    assert rewrite_map_module.structure_signature(first) != (
        rewrite_map_module.structure_signature(changed_destination)
    )

    plain = rewrite_map_module.build_rewrite_map("Alpha text")
    escaped_and_broken = rewrite_map_module.build_rewrite_map("Al\\*pha\ntext")
    assert rewrite_map_module.structure_signature(plain) == (
        rewrite_map_module.structure_signature(escaped_and_broken)
    )


@pytest.mark.parametrize(
    "markdown",
    [
        "市场正在**快速增长**，"
        "[官方报告](https://example.com/report \"title\")已确认。",
        "first\nsecond",
        "first  \nsecond",
        r"escaped \* 😀中文 é",
        "text ![alt](x.png) remains",
    ],
)
def test_reconstruct_unchanged_slots_is_byte_identical(markdown):
    rewrite_map = rewrite_map_module.build_rewrite_map(markdown)
    unchanged = {
        slot.slot_id: slot.text
        for unit in rewrite_map.units
        for slot in unit.slots
    }

    assert rewrite_map_module.reconstruct_markdown(rewrite_map, unchanged) == markdown


def test_reconstruct_walks_escape_softbreak_link_and_citation_ranges():
    markdown = (
        "escaped \\* first\n"
        "[label](https://example.com) [[1]](https://source.example)"
    )
    rewrite_map = rewrite_map_module.build_rewrite_map(markdown)
    unchanged = {
        slot.slot_id: slot.text
        for unit in rewrite_map.units
        for slot in unit.slots
    }

    assert rewrite_map_module.reconstruct_markdown(rewrite_map, unchanged) == markdown


def _replace_first_unit(rewrite_map, **changes):
    return replace(
        rewrite_map,
        units=(replace(rewrite_map.units[0], **changes), *rewrite_map.units[1:]),
    )


@pytest.mark.parametrize(
    "tamper",
    ["source", "source_gap", "anchor_source", "slot_range", "boundary"],
)
def test_reconstruct_rejects_tampered_map(tamper):
    markdown = (
        "alpha\n \nbeta"
        if tamper == "source_gap"
        else "escaped \\* first\n[label](https://example.com)"
    )
    rewrite_map = rewrite_map_module.build_rewrite_map(markdown)
    unit = rewrite_map.units[0]
    unchanged = {
        slot.slot_id: slot.text
        for current_unit in rewrite_map.units
        for slot in current_unit.slots
    }

    if tamper == "source":
        damaged = replace(rewrite_map, source=markdown.replace("escaped", "altered"))
    elif tamper == "source_gap":
        damaged = replace(rewrite_map, source=markdown.replace(" ", "\t"))
    elif tamper == "anchor_source":
        anchors = (
            replace(unit.protected[0], source="{"),
            *unit.protected[1:],
        )
        damaged = _replace_first_unit(rewrite_map, protected=anchors)
    elif tamper == "slot_range":
        slots = (
            replace(unit.slots[0], end_byte=unit.slots[0].end_byte - 1),
            *unit.slots[1:],
        )
        damaged = _replace_first_unit(rewrite_map, slots=slots)
    else:
        boundaries = unit.slots[0].visible_boundary_to_byte
        slots = (
            replace(
                unit.slots[0],
                visible_boundary_to_byte=(boundaries[0], *boundaries[2:]),
            ),
            *unit.slots[1:],
        )
        damaged = _replace_first_unit(rewrite_map, slots=slots)

    with pytest.raises(RewriteMapError) as caught:
        rewrite_map_module.reconstruct_markdown(damaged, unchanged)

    assert caught.value.code == "SELECTION_MAPPING_CONFLICT"


@pytest.mark.parametrize("request_kind", ["missing", "extra"])
def test_reconstruct_requires_exact_slot_requests(request_kind):
    rewrite_map = rewrite_map_module.build_rewrite_map("alpha **beta**")
    unchanged = {
        slot.slot_id: slot.text
        for unit in rewrite_map.units
        for slot in unit.slots
    }
    if request_kind == "missing":
        unchanged.pop(next(iter(unchanged)))
    elif request_kind == "extra":
        unchanged["unknown-slot"] = "text"
    with pytest.raises(RewriteMapError) as caught:
        rewrite_map_module.reconstruct_markdown(rewrite_map, unchanged)

    assert caught.value.code == "SELECTION_MAPPING_CONFLICT"


def test_reconstruct_replaces_selected_utf8_subranges_from_back_to_front():
    markdown = "prefix 中文 middle 😀 suffix"
    rewrite_map = rewrite_map_module.build_rewrite_map(markdown)
    slot = rewrite_map.units[0].slots[0]
    ranges = {
        slot.slot_id: (
            slot.visible_boundary_to_byte[7],
            slot.visible_boundary_to_byte[-8],
        )
    }

    reconstructed = rewrite_map_module.reconstruct_markdown(
        rewrite_map,
        {slot.slot_id: "替换"},
        selected_ranges=ranges,
    )

    assert reconstructed == "prefix 替换 suffix"


def test_reconstruct_encodes_replacement_as_final_visible_text():
    markdown = "before plain after"
    rewrite_map = rewrite_map_module.build_rewrite_map(markdown)
    slot = rewrite_map.units[0].slots[0]
    start = slot.visible_boundary_to_byte[7]
    end = slot.visible_boundary_to_byte[12]
    replacement = r"# \ &copy; * _ [ ]"

    reconstructed = rewrite_map_module.reconstruct_markdown(
        rewrite_map,
        {slot.slot_id: replacement},
        selected_ranges={slot.slot_id: (start, end)},
    )

    assert reconstructed == r"before \# \\ \&copy\; \* \_ \[ \] after"
    reparsed = rewrite_map_module.build_rewrite_map(reconstructed)
    assert "".join(current.text for current in reparsed.units[0].slots) == (
        f"before {replacement} after"
    )


def test_build_rewrite_map_returns_empty_immutable_collections_for_empty_source():
    rewrite_map = rewrite_map_module.build_rewrite_map("")

    assert rewrite_map.source == ""
    assert rewrite_map.units == ()
    assert rewrite_map.unsupported_regions == ()


def test_build_and_reconstruct_construct_constant_utf8_boundary_tables(monkeypatch):
    real_boundary_table = rewrite_map_module.Utf8BoundaryTable
    construction_count = 0

    class CountingBoundaryTable(real_boundary_table):
        def __init__(self, text):
            nonlocal construction_count
            construction_count += 1
            super().__init__(text)

    monkeypatch.setattr(
        rewrite_map_module, "Utf8BoundaryTable", CountingBoundaryTable
    )
    markdown = "\n\n".join(
        f"Paragraph {index}: **growth** [source](https://example.com/{index})."
        for index in range(400)
    )

    rewrite_map = rewrite_map_module.build_rewrite_map(markdown)

    assert len(rewrite_map.units) == 400
    assert construction_count == 1

    unchanged = {
        slot.slot_id: slot.text
        for unit in rewrite_map.units
        for slot in unit.slots
    }
    assert rewrite_map_module.reconstruct_markdown(rewrite_map, unchanged) == markdown
    # One for the original build, one for reconstruction validation, and one
    # for reconstruction's integrity rebuild: all independent of unit count.
    assert construction_count == 3


def test_typical_multisection_report_builds_and_reconstructs_byte_identically():
    sections = []
    for index in range(80):
        sections.append(
            f"## Section {index}\n\n"
            f"Market **growth {index}** follows "
            f"[source](https://example.com/report/{index}) "
            f"[[{index + 1}]](https://example.com/citation/{index}).\n\n"
            f"- Primary finding {index}\n"
            f"- Secondary finding {index}"
        )
    markdown = "\n\n".join(sections)

    rewrite_map = rewrite_map_module.build_rewrite_map(markdown)
    unchanged = {
        slot.slot_id: slot.text
        for unit in rewrite_map.units
        for slot in unit.slots
    }

    assert len(markdown) >= 12_000
    assert rewrite_map.unsupported_regions == ()
    assert rewrite_map_module.reconstruct_markdown(rewrite_map, unchanged) == markdown


@pytest.mark.parametrize(
    ("class_name", "args", "attribute"),
    [
        ("RewriteUnit", ("paragraph_0_0_4", "paragraph", 0, 4, None, None, None), "start_byte"),
        ("UnsupportedRegion", ("blockquote", 0, 7), "kind"),
        ("MarkdownRewriteMap", ("text", (), ()), "source"),
    ],
)
def test_rewrite_map_types_are_frozen_and_slotted(class_name, args, attribute):
    instance = getattr(rewrite_map_module, class_name)(*args)
    with pytest.raises(FrozenInstanceError):
        setattr(instance, attribute, getattr(instance, attribute))
    assert not hasattr(instance, "__dict__")


def test_protocol_v2_fixture_uses_real_utf8_ranges_and_hashes():
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    assert set(fixture) == {
        "protocol_version",
        "offset_encoding",
        "range_semantics",
        "visible_text_normalization",
        "cases",
    }
    assert fixture["protocol_version"] == 2
    assert fixture["offset_encoding"] == "utf-8-bytes"
    assert fixture["range_semantics"] == "half-open"
    assert fixture["visible_text_normalization"] == {
        "soft_break": "space",
        "hard_break": "newline",
        "unit_separator": "newline",
        "citation": "visible link label",
    }
    assert type(fixture["cases"]) is list
    case_ids = [case["id"] for case in fixture["cases"]]
    assert set(case_ids) == EXPECTED_CASE_IDS
    assert len(case_ids) == len(EXPECTED_CASE_IDS)

    for case in fixture["cases"]:
        case_id = case["id"]
        assert type(case_id) is str and case_id, case_id
        for field_name in (
            "markdown",
            "selected_text",
            "raw_selection",
            "visible_text_convention",
        ):
            assert type(case[field_name]) is str, case_id
        assert case["visible_text_convention"] in VISIBLE_TEXT_CONVENTIONS, case_id
        assert type(case["accepted"]) is bool, case_id
        assert type(case["start_byte"]) is int, case_id
        assert type(case["end_byte"]) is int, case_id
        assert type(case["source_sha256"]) is str, case_id
        assert re.fullmatch(r"[0-9a-f]{64}", case["source_sha256"]), case_id

        markdown_bytes = case["markdown"].encode("utf-8")
        assert 0 <= case["start_byte"] <= case["end_byte"] <= len(markdown_bytes), case_id

        if case["accepted"]:
            assert set(case) == COMMON_CASE_KEYS | {"expected_units"}, case_id
            assert type(case["expected_units"]) is list and case["expected_units"], case_id
            for unit in case["expected_units"]:
                assert type(unit) is dict, case_id
                assert unit["kind"] in {"heading", "paragraph", "list_item"}, case_id
                assert unit["coverage"] in {"full", "partial"}, case_id
                assert type(unit["index"]) is int and unit["index"] >= 0, case_id
                if unit["kind"] == "heading":
                    assert set(unit) == COMMON_UNIT_KEYS | {"level"}, case_id
                    assert type(unit["level"]) is int and 1 <= unit["level"] <= 6, case_id
                elif unit["kind"] == "list_item":
                    assert set(unit) == COMMON_UNIT_KEYS | {"depth", "marker"}, case_id
                    assert type(unit["depth"]) is int and unit["depth"] >= 0, case_id
                    assert type(unit["marker"]) is str, case_id
                    assert re.fullmatch(r"(?:[-+*]|\d+[.)])", unit["marker"]), case_id
                else:
                    assert set(unit) == COMMON_UNIT_KEYS, case_id
        else:
            expected_keys = COMMON_CASE_KEYS | {"error_code"}
            if case_id == "selected_text_mismatch_rejected":
                expected_keys |= {"mismatch_kind"}
                assert case["mismatch_kind"] == "visible_text", case_id
                assert case["error_code"] == "SELECTION_MAPPING_CONFLICT", case_id
            else:
                assert "mismatch_kind" not in case, case_id
            assert set(case) == expected_keys, case_id
            assert type(case["error_code"]) is str, case_id
            assert case["error_code"] in STABLE_ERROR_CODES, case_id

        table = Utf8BoundaryTable(case["markdown"])
        table.require_byte_boundary(case["start_byte"])
        table.require_byte_boundary(case["end_byte"])
        raw_bytes = markdown_bytes[case["start_byte"] : case["end_byte"]]
        assert raw_bytes.decode("utf-8") == case["raw_selection"], case["id"]
        assert hashlib.sha256(raw_bytes).hexdigest() == case["source_sha256"], case["id"]
        assert sha256_byte_range(
            case["markdown"], case["start_byte"], case["end_byte"]
        ) == case["source_sha256"], case["id"]

        computed_visible = _visible_selection(
            case["raw_selection"], case["visible_text_convention"]
        )
        if (
            case.get("error_code") == "SELECTION_MAPPING_CONFLICT"
            and case.get("mismatch_kind") == "visible_text"
        ):
            assert computed_visible != case["selected_text"], case["id"]
        else:
            assert computed_visible == case["selected_text"], case["id"]

    partial_outer = next(
        case
        for case in fixture["cases"]
        if case["id"] == "partial_outer_units_complete_middle"
    )
    assert partial_outer["accepted"] is True
    assert [unit["coverage"] for unit in partial_outer["expected_units"]] == [
        "partial",
        "full",
        "partial",
    ]
