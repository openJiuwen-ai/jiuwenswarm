---
name: financial-modeling
description: Read before building or extending a financial model or forecast, for its layout and the checks before you report.
---

**Layout.** Inputs and assumptions sit on their own sheet or block, each in one labelled cell with its unit; calculations refer to them and never repeat a number. Time runs left to right, one column per period, with the same formula across a row. Each schedule (revenue, costs, headcount, debt) sits in its own block with a total row, and the summary links to those totals. Keep a check row where the model has one (the balance sheet balances, cash ties out), showing zero when it holds.

**Formulas.** One formula per row, filled across with `copy_paste_range`. Prefer `SUMIFS`, `INDEX`/`MATCH` or `XLOOKUP` to nested `IF`. Use `IFERROR` only where a zero denominator or a missing match is legitimate, never to hide a broken reference.

**Checks before you report.** Trace one number end to end, from input to summary. Change an input and confirm the outputs move, then restore it. Read `formula_results` for errors and look for typed numbers in rows of formulas.
