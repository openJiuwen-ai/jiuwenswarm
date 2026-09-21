# Chinese / CJK Context Handbook

Everything needed to make spreadsheets work correctly with Chinese (and broader
CJK) data. The skill itself can be written in English; the *handling* must be
Chinese-aware.

## 1. Encoding
- `.xlsx` / `.xlsm` internals are **always UTF-8**. Keep them UTF-8 when editing
  XML. Writing XML in any other encoding turns Chinese into mojibake.
- `.csv` / `.tsv` exported from Chinese Windows Excel is usually **GB18030/GBK**
  (sometimes UTF-8 with BOM). Detect before reading:
  ```python
  import pandas as pd
  df = None
  for enc in ("utf-8-sig", "gb18030", "utf-16"):
      try:
          df = pd.read_csv(path, encoding=enc); break
      except UnicodeDecodeError:
          continue
  ```
- When writing CSV for the user to open in Excel, use `encoding="utf-8-sig"`
  (UTF-8 with BOM) so Excel detects it and shows Chinese correctly.

## 2. Text & XML
- Store Chinese cell text as **inline strings** and XML-escape `& < >`:
  ```xml
  <c r="A1" t="inlineStr"><is><t>营业收入</t></is></c>
  ```
- Sheet names, named ranges, and headers may be Chinese. In formulas, **quote**
  sheet names that contain non-ASCII or spaces: `SUM('销售数据'!D2:D13)`.
- **Full-width vs half-width**: watch for full-width punctuation `，；：（）％`
  and full-width digits `１２３`. Normalize before numeric parsing:
  ```python
  import unicodedata
  half = unicodedata.normalize("NFKC", s)
  ```

## 3. Numbers & currency
- RMB currency: `"¥"#,##0.00` (half-width ¥) or `"￥"#,##0.00` (full-width).
- Accounting style: `_-"¥"* #,##0.00_-;-"¥"* #,##0.00;_-"¥"* "-"??_-`.
- **万 / 亿 scaling**: Chinese reports often show 万 (10^4) or 亿 (10^8). Custom
  formats for this are fragile; prefer a helper column that divides and a unit in
  the header:
  | 金额（元） | 金额（亿元） |
  |-----------|--------------|
  | `=A2`     | `=A2/100000000` |
  Keep the raw value intact; only present the scaled view.
- Thousands `#,##0`; percent `0.0%`; keep decimals consistent (`f'{v:.2f}'`).

## 4. Dates
- Chinese date format string: `yyyy"年"m"月"d"日"`; weekday via `aaaa` → 星期X.
- Keep the **underlying value a real date serial** — only the number format is
  Chinese. Do not store dates as text.

## 5. Rendering & fonts
- The sandbox ships Noto Sans CJK (SC/TC/JP/KR). `xlsx_render.py` auto-selects a
  CJK font so previews show Chinese correctly.
- Verify availability: `python3 SKILL_DIR/scripts/doctor.py` (the `font:CJK`
  check). If missing, install e.g. `google-noto-sans-cjk-fonts`.

## 6. Layout
- CJK glyphs are ~2x the width of ASCII. Widen columns: rough rule
  `width ≈ max_CJK_chars × 2.1 + 2`. This prevents clipped headers like
  “本年累计金额”.
- If wrapping Chinese text, also set a larger row height.

## 7. Sorting
- Default string sort is by Unicode code point (not pinyin). For pinyin order use
  `pypinyin`:
  ```python
  from pypinyin import lazy_pinyin
  rows.sort(key=lambda r: lazy_pinyin(r["名称"]))
  ```
- For stroke-count or radical order, document the limitation unless a dedicated
  table is available.

## 8. Quick checklist for Chinese deliverables
- [ ] XML written UTF-8; CSV encoding detected/declared.
- [ ] Headers/labels escaped and not clipped.
- [ ] ¥ / 万 / 亿 / % / decimals formatted per request.
- [ ] Dates are real serials with Chinese number format.
- [ ] Rendered preview shows no □□□ and no mojibake.
