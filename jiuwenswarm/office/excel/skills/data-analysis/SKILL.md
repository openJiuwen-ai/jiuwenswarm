---
name: data-analysis
description: Read before analysing or cleaning a data set in a workbook.
---

**Look before you compute.** Read the header and a sample with `get_range_as_csv`, check `<initial_state>` for the sheet's size and Excel tables, and confirm what each column holds (types, blanks, units, duplicates) before drawing conclusions.

**Cleaning.** Clean into a new column or sheet and keep the original, unless the user asks to replace it. Trim spaces, fix text that should be numbers or dates, and report how many rows you changed or dropped and why. Never delete rows silently.

**Report.** Give the answer first, then the range it came from and any caveat (rows excluded, blanks, assumptions). Do not round differently from the source.
