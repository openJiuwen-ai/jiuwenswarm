---
name: tables
description: Read before creating or restructuring a table in a Word document, for its layout on the page.
---

**Header.** When the document has no tables to match, use a built-in table style with a header row, and set the header row to repeat on each page for a long table.

**Fit the page.** Size the table to the text width and give columns widths that suit their content, so short values do not wrap and long text does not run off the page. Avoid merged cells unless the layout needs them; they make later edits fragile.

**Content.** One kind of value per column; numbers right-aligned with the same number of decimals, text left-aligned. Keep headers short.

**Editing an existing table.** Change cell text in place, and add or remove rows and columns rather than rebuilding the table, which loses the comments and tracked changes in it. After changing its size or columns, run `verify_doc` for the row shapes and `verify_doc_visual` for reflow.
