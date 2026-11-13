# providers/google

The Google implementation of the provider contract: Docs, Sheets and Slides through
the Google API client, as a service account.

**Purpose.** `provider.py` holds `GoogleDocsProvider`: reads the document body and
flattens it into addressed text, edits against a base revision with `WriteControl`,
highlights what it changed, lists and answers comments, discovers shared files and
reports capabilities. `formats.py` holds the Sheets and Slides halves: reading a grid
or a deck into the text map, planning and writing region edits, adding a sheet or a
slide.

**Public names.** `GoogleDocsProvider`; in `formats`: `read_spreadsheet`,
`read_presentation`, `plan_sheet_edits`, `plan_slide_edits`, the region readers and
writers the provider calls.

**Imports.** `providers.base`, `providers.textmap`, `providers.formats` and the
Google client libraries; nothing above the providers layer.

**Dynamic client.** The Google client builds its resources at runtime, so
`files()`, `documents()` and the rest are invisible to static analysis; the module
disables pylint's `no-member` for that reason.
