"""Feishu Docs, Sheets and Slides formats: what the Feishu modules alone decide.

Moved from the plugin's formats tests; the cases that reach the toolkit move with it.
"""
from __future__ import annotations


def test_feishu_does_not_claim_revision_control():
    """``--revision-id`` is a base revision, not a lock: pinning an older one rebuilds
    from that snapshot and discards newer edits. Claiming a lock here would let
    admission rely on protection that does not exist, and the failure mode is a
    destroyed edit reported as applied.

    This flag was True and no test noticed when it flipped, which is why it has one.
    """
    from jiuwenswarm.clouddoc.providers.feishu.provider import (
        FeishuDocsProvider,
    )
    import inspect

    src = inspect.getsource(FeishuDocsProvider)
    assert "has_revision_control=True" not in src


def test_every_google_format_declares_its_markers_are_literal():
    """All three Google formats are plain: an asterisk typed into any of them lands
    as an asterisk. The markdown domain now belongs to the Feishu docx alone, whose
    transport round-trips markers as content.
    """
    from jiuwenswarm.clouddoc.providers.google.formats import TRAITS
    from jiuwenswarm.clouddoc.providers.feishu.formats import (
        TEXT_DOMAIN,
    )

    assert {t.text_domain for t in TRAITS.values()} == {"plain"}
    assert TEXT_DOMAIN["document"] == "markdown"
    assert TEXT_DOMAIN["spreadsheet"] == "plain"


def test_a_feishu_cell_quote_carries_the_cell_address():
    """This platform prefixes a cell comment's quote with the cell's A1 address.

    Measured 2026-09-10 on a live sheet: the quote came back as
    ``"D2 16oAxy-nBNTqKsNVGolJ118OUsEV833R4Jgac_QoQFqQ"`` while the body holds only the
    value. The rail searched for the whole string, found it nowhere, and refused an
    edit to a cell whose value was unique -- reported as "cannot be located uniquely",
    which was doubly wrong: it was not ambiguous, it was absent, and it was absent
    because of a prefix we put there ourselves. Google adds no such prefix, so this
    went unnoticed until a Feishu sheet was driven end to end.
    """
    from jiuwenswarm.clouddoc.providers.feishu.comments import (
        _split_cell_quote,
    )

    assert _split_cell_quote("D2 16oAxy-nB") == ("D2", "16oAxy-nB")
    assert _split_cell_quote("B2 google") == ("B2", "google")
    assert _split_cell_quote("AA123\tvalue") == ("AA123", "value")
    # Prose keeps every character: a document's quote is not addressed, and eating a
    # leading word from one would silently move the edit window.
    assert _split_cell_quote("这句话要改") == ("", "这句话要改")
    assert _split_cell_quote("Note this line") == ("", "Note this line")
    # An address with nothing after it is not a prefix -- it is the whole quote.
    assert _split_cell_quote("D2") == ("", "D2")
    assert _split_cell_quote("") == ("", "")
