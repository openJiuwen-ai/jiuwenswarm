---
name: table-formatting
description: Read before laying out and formatting a new table or report sheet, for a clean default style.
---

Apply this to tables and sheets you create. An existing sheet keeps its own style; match it when you add to it.

- **Structure.** One header row, one record per row, no blank rows or columns inside the block, no merged cells in the data (use "Center Across Selection" for a title over columns). Turn a data block into an Excel table when the user will add rows or filter it, and give it a meaningful name.
- **Header.** Bold, a light fill, a bottom border; wrap long header text rather than widening the column a lot. Freeze the header row on a sheet longer than one screen.
- **Numbers.** Right-aligned with a consistent format per column: thousands separators, a fixed number of decimals, negatives in parentheses or with a minus sign but not both, percentages as `0.0%`, dates as one format throughout. Text left-aligned.
- **Widths.** Autofit, then cap very wide text columns and wrap them; no column so narrow it shows `###`.
- **Totals.** A total row below the data, separated by a top border, in bold, with formulas.
- **Restraint.** At most one accent colour, for headers or highlights; no gridline-like borders around every cell when the sheet's gridlines already show them; no fills on data cells except to mark inputs or exceptions.
- **Title and source.** A short title above the table when the sheet is a report, and a source note under it or on the header cell when the data came from outside.
