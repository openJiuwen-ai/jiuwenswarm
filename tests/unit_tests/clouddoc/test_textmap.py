"""Flattening a grid or a deck into text, and editing it back (the text map).

Moved with the module from the co-scribe plugin tree: these tests touch the text
map and the provider contract types only.
"""
from __future__ import annotations

import pytest

from jiuwenswarm.clouddoc.providers import textmap
from jiuwenswarm.clouddoc.providers.base import DocSnapshot, ProviderError
from jiuwenswarm.clouddoc.providers.formats import parse_a1_region


def _sheet(rows):
    cells = [
        [
            textmap.Cell(
                address=f"S!{chr(65 + c)}{r + 1}",
                formatted=str(v[0]),
                formula=str(v[1]),
            )
            for c, v in enumerate(row)
        ]
        for r, row in enumerate(rows)
    ]
    text, segs = textmap.flatten_grid(cells)
    return DocSnapshot(doc_id="d", kind="spreadsheet", revision_id=None, text=text, segments=segs)


def test_a_grid_flattens_to_the_text_a_person_would_copy_out():
    """Tab between cells and newline between rows is the clipboard convention, and the
    quote a comment carries is what the person saw -- so the two have to agree or no
    quote ever matches.
    """
    snap = _sheet([[("Q1", "Q1"), ("42", "42")], [("Q2", "Q2"), ("7", "7")]])
    assert snap.text == "Q1\t42\nQ2\t7"


def test_every_cell_keeps_its_own_address():
    snap = _sheet([[("a", "a"), ("b", "b")]])
    assert [s.address for s in snap.segments] == ["S!A1", "S!B1"]


def test_notes_are_flattened_behind_a_divider():
    """A sentence appearing in both the slide and its notes would otherwise be two
    equally good matches, and the rail would refuse an edit it could have made.
    """
    text, _ = textmap.flatten_slides([
        ("p1", [
            textmap.Shape("p1", "body", "same words"),
            textmap.Shape("p1", "notes", "same words", is_notes=True),
        ])
    ])
    assert textmap.NOTES_DIVIDER in text
    assert text.index("same words") < text.index(textmap.NOTES_DIVIDER)


def test_a_formula_cell_refuses_the_write_that_would_flatten_it():
    """The whole reason IC-7 exists: the cell reads as 42, the comment quotes 42, and
    writing 42 back leaves a literal where =SUM(A1:A9) was -- with nothing on the page
    to show it happened.
    """
    snap = _sheet([[("Total", "Total"), ("42", "=SUM(A1:A9)")]])
    c0, c1 = textmap.locate_span(snap.text, "42")
    with pytest.raises(ProviderError) as exc:
        textmap.single_placement(snap, c0, c1)
    assert "公式" in str(exc.value)


def test_a_number_formatted_cell_is_not_mistaken_for_a_formula():
    """1234 displayed as 1,234 differs from what is stored without being computed.
    Comparing the two faces alone would refuse every formatted number in the sheet.
    """
    snap = _sheet([[("1,234", "1234")]])
    c0, c1 = textmap.locate_span(snap.text, "1,234")
    assert textmap.single_placement(snap, c0, c1).segment.address == "S!A1"


def test_an_ordinary_cell_still_writes():
    snap = _sheet([[("hello", "hello")]])
    c0, c1 = textmap.locate_span(snap.text, "hello")
    assert textmap.single_placement(snap, c0, c1).segment.readonly_reason == ""


def test_a_quote_matching_many_cells_is_refused_rather_than_guessed():
    """In a spreadsheet this is the common case, not the rare one, and refusing is the
    rail working: the alternative is writing to a cell nobody named.
    """
    snap = _sheet([[("42", "42"), ("42", "42")]])
    with pytest.raises(ProviderError) as exc:
        textmap.locate_span(snap.text, "42")
    assert "unique" in str(exc.value)


def test_a_span_crossing_two_cells_is_refused():
    """There is no single write that means it. Writing the first would drop half the
    edit; splitting it would stop being one change.
    """
    snap = _sheet([[("alpha", "alpha"), ("beta", "beta")]])
    c0, c1 = textmap.locate_span(snap.text, "alpha\tbeta")
    with pytest.raises(ProviderError) as exc:
        textmap.single_placement(snap, c0, c1)
    assert "跨越" in str(exc.value)


@pytest.mark.parametrize("needle", ["alpha\t", "\tbeta", "alpha\t\t"])
def test_a_span_reaching_into_a_separator_is_refused(needle):
    """One run intersected is not one run containing the span. The separator between
    cells belongs to no cell, so a span that takes it in clips to the cell it touches
    and the whole replacement would land there -- including, for ``alpha\\t`` replaced
    by ``X\\tY``, the text meant for the next cell.
    """
    snap = _sheet([[("alpha", "alpha"), ("", ""), ("beta", "beta")]])
    c0, c1 = textmap.locate_span(snap.text, needle)
    with pytest.raises(ProviderError) as exc:
        textmap.single_placement(snap, c0, c1)
    # Refused by the containment check, or by the two-run check when the span also
    # grazes the empty cell; both are the right answer, neither writes anything.
    assert "边界" in str(exc.value) or "跨越" in str(exc.value)
    with pytest.raises(ProviderError):
        textmap.plan_edits(snap, [(needle, "X\tY")])


def test_an_empty_cell_beside_a_full_one_is_not_absorbed():
    """A1 holds ``alpha``, B1 is empty: the only run that intersects ``alpha\\t`` is A1,
    and before the containment check the edit ``alpha\\t`` -> ``X\\tY`` was planned as a
    whole-cell write of ``X\\tY`` into A1.
    """
    snap = _sheet([[("alpha", "alpha"), ("", "")]])
    with pytest.raises(ProviderError):
        textmap.plan_edits(snap, [("alpha\t", "X\tY")])
    # The same text selected within the cell still edits in place.
    plan = textmap.plan_edits(snap, [("alpha", "X")])
    assert [(e.local_start, e.local_end, e.new) for e in plan["S!A1"]] == [(0, 5, "X")]


def test_a_cell_is_edited_in_place_not_replaced_wholesale():
    """A comment asking to change one word quotes the word. Writing only the word back
    would delete the rest of the sentence around it.
    """
    snap = _sheet([[("the quick fox", "the quick fox")]])
    c0, c1 = textmap.locate_span(snap.text, "quick")
    hit = textmap.single_placement(snap, c0, c1)
    cell = snap.text[hit.segment.char_start: hit.segment.char_end]
    assert cell[: hit.local_start] + "slow" + cell[hit.local_end:] == "the slow fox"


def test_edits_applied_to_text_see_each_other():
    """Uniqueness is judged against the text as it stands, not the original, or two
    overlapping edits would both look applicable and the second would land on text the
    first already rewrote.
    """
    assert textmap.apply_edits_to_text("a b c", [("a", "x"), ("x b", "y")]) == "y c"


def test_two_edits_to_one_cell_compose_instead_of_racing():
    """Each edit was located against the original text and each produced a whole-cell
    write to the same address. The platform applies them in order, so the last won, the
    first was erased, and the batch reported ``applied``.

    Grouping by address is what makes them compose. This is the one finding in the
    review that lost data.
    """
    snap = _sheet([[("alpha beta gamma", "alpha beta gamma")]])
    planned = textmap.plan_edits(snap, [("alpha", "A"), ("gamma", "G")])
    assert list(planned) == ["S!A1"], "同一格的两处修改必须归并成一次写入"
    folded = textmap.fold_edits("alpha beta gamma", planned["S!A1"])
    assert folded == "A beta G", "两处修改都要在最终值里"


def test_two_edits_over_the_same_characters_are_refused():
    """There is no order in which both are what the person approved, and applying
    either silently would be picking one for them.
    """
    snap = _sheet([[("alpha beta", "alpha beta")]])
    with pytest.raises(ProviderError) as exc:
        textmap.plan_edits(snap, [("alpha beta", "x"), ("beta", "y")])
    assert "同一段文字" in str(exc.value)


def test_the_approved_window_reaches_the_writer():
    """Without it this layer re-judges uniqueness across the whole body. In a document
    that is merely stricter; in a spreadsheet a quote of "42" matches every cell showing
    42, so a proposal the rail passed is refused *after a person approved it* -- the
    exact failure the document path's _locate warns about.
    """
    snap = _sheet([[("42", "42")], [("42", "42")]])
    with pytest.raises(ProviderError):
        textmap.plan_edits(snap, [("42", "43")])  # no window: ambiguous, correctly
    planned = textmap.plan_edits(snap, [("42", "43")], window=(0, 2))
    assert list(planned) == ["S!A1"], "带窗口时应当落在窗口所在的那一格"


def test_edits_applied_whole_file_respect_the_window_too():
    """A whole-text rewrite has no addressed form to bound it, so the window is the
    only thing keeping an edit inside the range the comment authorised.
    """
    text = "keep this\nchange this\nkeep this"
    with pytest.raises(ProviderError):
        textmap.apply_edits_to_text(text, [("keep this", "x")])
    out = textmap.apply_edits_to_text(text, [("keep this", "x")], window=(0, 9))
    assert out == "x\nchange this\nkeep this"


def test_a1_regions_parse_and_a_bad_one_is_refused():
    """A range this cannot read is a caller error. Resolving it to something plausible
    would write to cells nobody named.
    """
    assert parse_a1_region("Sheet1!A7:B7") == ("Sheet1", 6, 0, 6, 1)
    assert parse_a1_region("'Q3 Data'!B2") == ("Q3 Data", 1, 1, 1, 1)
    assert parse_a1_region("B7") == ("", 6, 1, 6, 1)
    for bad in ("", "sheet1", "A0:B", "B7:A1"):
        with pytest.raises(ProviderError):
            parse_a1_region(bad)


@pytest.mark.parametrize("region", ["A0", "A0:B1", "A1:B0", "Sheet1!A0", "C0:C0"])
def test_a_zero_row_is_refused_not_wrapped_to_the_last_row(region):
    """Rows are 1-based. ``A0`` used to come back as row index -1, which Python reads
    as the last row of whatever it indexes.
    """
    with pytest.raises(ProviderError) as exc:
        parse_a1_region(region)
    assert "行号" in str(exc.value) or "终点" in str(exc.value)
