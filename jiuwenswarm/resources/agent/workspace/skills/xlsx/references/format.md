# Formatting: number formats, styles, colors, CJK

Styles live in `xl/styles.xml`. A cell references a style via `s="<index>"`, which
points into `<cellXfs>`. The helper `Styles` class in `scripts/_xlsx_common.py`
appends number formats, borders, and cell formats for you.

## Number formats (custom format codes)
| Purpose | Format code |
|---------|-------------|
| Integer w/ thousands | `#,##0` |
| 2 decimals | `0.00` |
| Percent 1 decimal | `0.0%` |
| RMB currency | `"¥"#,##0.00` |
| RMB accounting | `_-"¥"* #,##0.00_-;-"¥"* #,##0.00;_-"¥"* "-"??_-` |
| Chinese date | `yyyy"年"m"月"d"日"` |
| Negatives in red | `#,##0;[Red](#,##0)` |
| Zero as dash | `#,##0;-#,##0;"-"` |

Apply via the scripts, e.g. `--numfmt '0.0%'` on `xlsx_add_column.py`, or in code:
```python
import _xlsx_common as X
st = X.Styles(workdir)
xf = st.add_xf(numFmtId=st.add_numfmt('"¥"#,##0.00'))
st.save()   # then set s="<xf>" on the target <c>
```

## Borders
```python
xf = st.add_xf(borderId=st.add_top_border("medium"))  # accounting total line
```
Apply the resulting index to every `<c>` in the row (row-wide line).

## Colors (financial model convention - optional)
- Inputs/assumptions: blue font `0000FF`
- Formulas: black `000000`
- Cross-sheet references: green `00B050`

## CJK layout
- Column width approx `max_CJK_chars x 2.1 + 2` to avoid clipped headers.
- Keep the underlying value typed (number/date); only the **number format** is
  Chinese. Never store numbers/dates as text.
- For 万/亿 presentation prefer a helper column (`=A2/100000000`) with a unit in
  the header rather than fragile scaling format codes. See cjk-guide.md.
