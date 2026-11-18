"""The formats beyond a Feishu docx: spreadsheets and slide decks.

Same split as the Google side and for the same reason: a connection reaches documents
of several kinds, so the format belongs to the document rather than to the provider.
Each handler answers only how to flatten and how to write back; the rails above are
shared and untouched.

The provider already recognises spreadsheets and decks by their links and their Drive
type, and dispatches through the tables below. Until a format has handlers here it is
refused rather than read as a docx.
"""

from __future__ import annotations

# What ``drive +list-files`` calls each type. A markdown file is its own type here,
# unlike Drive where it is a plain file that happens to be named .md.
TYPE_DOCX = "docx"
TYPE_SHEET = "sheet"
TYPE_MARKDOWN = "markdown"
TYPE_SLIDES = "slides"

KIND_BY_TYPE: dict[str, str] = {
    TYPE_DOCX: "document",
    TYPE_SHEET: "spreadsheet",
    TYPE_SLIDES: "presentation",
}

# Reported to a person as something co-scribe can see but not work on, with the reason
# attached rather than left to be guessed. Slides left this list on 2026-09-02:
# element-level replacement was measured live and the format is served above.
# Markdown joined it: the format has no addressed form on either platform, so an
# unattended turn on one had nothing to bound its edit to.
UNSUPPORTED_REASON: dict[str, str] = {
    TYPE_MARKDOWN: "云文档不支持这个格式。",
}

READERS: dict = {}

WRITERS: dict = {}

TEXT_DOMAIN: dict[str, str] = {
    "document": "markdown",
}

# The formats with an addressed form. A docx has none, and the provider answers it
# with the base class's declared refusal.
REGION_READERS: dict = {}

ADD_PAGE: dict = {}

REGION_WRITERS: dict = {}
