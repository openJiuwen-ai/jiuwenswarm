"""The helpers shared by both platforms' format modules."""
from __future__ import annotations

import pytest

from jiuwenswarm.clouddoc.providers.base import ProviderError
from jiuwenswarm.clouddoc.providers.formats import (
    _col_index,
    _grid_flat,
    _trim_common_affixes,
    _utf16_len,
    parse_a1_region,
)


def test_common_affixes_are_trimmed_to_the_part_that_changed():
    """A whole-paragraph rewrite where one word changed must shrink to that word,
    so the platform request and the highlight cover only what moved.
    """
    assert _trim_common_affixes("Hello brave world", "Hello bright world") == (8, "ave", "ight")
    assert _trim_common_affixes("aaa", "aa") == (2, "a", "")
    assert _trim_common_affixes("aa", "aaa") == (2, "", "a")
    assert _trim_common_affixes("same", "same") == (4, "", "")


def test_utf16_length_counts_surrogate_pairs_twice():
    assert _utf16_len("ab") == 2
    assert _utf16_len("𝄞") == 2
    assert _utf16_len("𝄞ab") == 4


def test_column_letters_map_to_zero_based_indexes():
    assert [_col_index(c) for c in ("A", "B", "Z", "AA", "AZ", "BA")] == [0, 1, 25, 26, 51, 52]


def test_a_grid_flattens_with_the_clipboard_separators():
    assert _grid_flat([["a", "b"], ["c", "d"]]) == "a\tb\nc\td"


@pytest.mark.parametrize("region, expected", [
    ("Sheet1!A1", ("Sheet1", 0, 0, 0, 0)),
    ("'My Sheet'!B2:D4", ("My Sheet", 1, 1, 3, 3)),
    ("C3", ("", 2, 2, 2, 2)),
])
def test_a1_regions_parse(region, expected):
    assert parse_a1_region(region) == expected


@pytest.mark.parametrize("region", ["", "Sheet1!", "A", "Sheet1!B2:A1"])
def test_a_bad_region_is_refused_rather_than_guessed(region):
    with pytest.raises(ProviderError):
        parse_a1_region(region)
