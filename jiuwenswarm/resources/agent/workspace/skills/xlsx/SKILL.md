---
name: xlsx
description: "Open, create, read, analyze, edit, repair, or validate spreadsheet files (.xlsx, .xlsm, .xltx, .csv, .tsv) with zero format loss, with first-class support for Chinese / CJK content. Use whenever the user asks to create, build, modify, fill, analyze, read, repair, validate, or format any spreadsheet, workbook, report, dashboard, budget, financial model, pivot table, or tabular data file. Handles Chinese text, encodings (UTF-8 / GB18030 / GBK), full-width characters, CJK fonts in rendering, and Chinese number/date conventions (¥, 万/亿, 年/月/日). Covers: creating new workbooks from scratch, reading and analyzing existing files, editing existing workbooks while preserving pivot tables / VBA macros / charts / sparklines / conditional formatting / named ranges, formula recalculation and validation, visual review of results, and applying professional formatting. Triggers on 'spreadsheet', 'Excel', 'workbook', '表格', '工作簿', '.xlsx', '.xlsm', '.csv', 'pivot table', '透视表', 'macro', '宏', 'chart', '图表', 'financial model', '财务模型', 'formula', '公式', or any request to produce tabular data."
license: MIT
metadata:
  version: "2.0"
  category: productivity
  locale: zh-CN-aware
  sources:
    - ECMA-376 Office Open XML File Formats
    - Microsoft Open XML SDK documentation
---

# XLSX Skill (Universal, CJK-aware)

Handle the request directly. Do NOT spawn sub-agents. Always write the output file the user requests, then verify it before delivering.

> This is a vendor-neutral, general-purpose spreadsheet skill. It is **not** limited to finance and is explicitly built to work well in **Chinese-language contexts**: Chinese sheet names, headers and data; mixed CJK + ASCII; encodings beyond UTF-8 (GB18030/GBK) for CSV; full-width punctuation; CJK fonts during rendering; and Chinese number/date conventions. Finance-specific conventions are optional and isolated in their own section.

---

## 0. Quick Start (read this first)

**The golden loop for every task: READ → ACT → VERIFY.** Never skip READ, never skip VERIFY.

```bash
# 0) One-time environment self-check (python, openpyxl, libreoffice, poppler, AND CJK fonts)
python3 SKILL_DIR/scripts/doctor.py

# 1) READ — always inspect structure before touching anything
python3 SKILL_DIR/scripts/xlsx_reader.py input.xlsx            # sheets, dims, named ranges, macros, pivots, charts

# 2) ACT — pick ONE path (see Decision Guide below)
#    - read-only analysis          -> pandas
#    - create new workbook         -> XML template + xlsx_pack.py
#    - edit / fill / fix existing  -> unpack -> edit -> pack  (zero format loss)

# 3) VERIFY — prove the file is correct and undamaged
python3 SKILL_DIR/scripts/formula_check.py output.xlsx --report
python3 SKILL_DIR/scripts/xlsx_render.py output.xlsx --out review/             # renders with CJK fonts
python3 SKILL_DIR/scripts/xlsx_reader.py output.xlsx --diff-against input.xlsx
```

If unsure which path to take, see the **Decision Guide** (section 2). If something breaks, see **Troubleshooting** (section 9). For Chinese-context specifics, see **section 10**.

---

## 1. Task Routing

| Task | When | Method | Guide |
|------|------|--------|-------|
| **READ** | analyze / summarize existing data, no changes | `xlsx_reader.py` + pandas | `references/read-analyze.md` |
| **CREATE** | brand-new workbook from scratch | XML template → `xlsx_pack.py` | `references/create.md` + `references/format.md` |
| **EDIT** | modify / fill an existing workbook | XML unpack → edit → pack | `references/edit.md` (+ `format.md`) |
| **FIX** | repair broken formulas / corruption | XML unpack → fix `<f>` nodes → pack | `references/fix.md` |
| **VALIDATE** | check formulas & integrity | `formula_check.py` (+ recalc) | `references/validate.md` |
| **REVIEW** | confirm result visually | `xlsx_render.py` → PNG/PDF/HTML | `references/visual-review.md` |

CSV / TSV: for pure data work use pandas directly (mind the **encoding** — see section 10). To deliver an `.xlsx`, build it via the CREATE path so formatting and formulas are preserved.

---

## 2. Decision Guide (which path do I take?)

1. **Only reading / analyzing, no output workbook?** → **READ** path. `xlsx_reader.py` then pandas. Never modify the source.
2. **File exists and I must change it (fill cells, add column/row, fix formulas, restyle)?** → **EDIT/FIX** path. ALWAYS unpack → edit → pack. **Never** `openpyxl.load_workbook(...).save()` round-trip — it silently destroys VBA macros, pivot tables, charts, slicers, sparklines.
3. **Building a new workbook from nothing?** → **CREATE** path. Copy the minimal XML template, edit XML, pack.
4. **Wrong/old values or a “needs repair” prompt?** → recalc (`libreoffice_recalc.py`) and re-validate; if corruption, go to **Troubleshooting**.

**Format-preservation rule of thumb:** if the file contains (or might contain) macros, pivots, charts, conditional formatting, named ranges, or sparklines, stay on the XML unpack/edit/pack path. `xlsx_reader.py` reports which of these are present.

---

## 3. READ — Analyze data (read `references/read-analyze.md` first)

Start with `xlsx_reader.py` for structure discovery, then pandas for custom analysis. Never modify the source file.

- **Encoding (critical for Chinese CSV)**: `.xlsx` is always UTF-8 internally, but `.csv`/`.tsv` exported from Chinese Excel is often **GB18030/GBK**. Detect and decode (see section 10); do not assume UTF-8.
- **Decimal/format rule**: when the user specifies decimal places (e.g. “2 位小数”), apply it to ALL numeric values — `f'{v:.2f}'`. Never print `12875` when `12875.00` is required.
- **Aggregation rule**: compute sums/means/counts directly from the DataFrame column, e.g. `df['营业收入'].sum()`. Never re-derive column values before aggregating.
- **Large files**: `pandas.read_excel(..., sheet_name=None)` to enumerate sheets; `.head()` before full loads.

---

## 4. CREATE — XML template (read `references/create.md` + `references/format.md`)

Copy `templates/minimal_xlsx/` → edit XML directly → pack with `xlsx_pack.py`.

- Every derived value MUST be an Excel formula (`<f>SUM(B2:B9)</f>`), never a hardcoded number.
- Use cell references over magic numbers (`=H6*(1+$B$3)`, not `=H6*1.04`).
- **Chinese text in cells**: store as inline strings and XML-escape `& < >`, e.g. `<c r="A1" t="inlineStr"><is><t>营业收入</t></is></c>`. Always write XML as UTF-8.
- **Column widths for CJK**: Chinese glyphs are roughly twice as wide as ASCII. Set wider columns (rough rule: width ≈ max CJK chars × 2.1 + 2) so headers like “本年累计金额” are not clipped.
- Apply number/date formats per section 10 and `format.md`.
- After packing, run VERIFY (validate + render).

---

## 5. EDIT — XML direct-edit (read `references/edit.md` first)

### CRITICAL — EDIT INTEGRITY RULES
1. **NEVER create a new `Workbook()`** for edit tasks. Always load/unpack the original file.
2. Output MUST contain the **same sheets** as the input (same names, order, data) unless the task explicitly says to add/remove.
3. Only modify the specific cells the task asks for — everything else stays byte-for-byte untouched.
4. **Preserve special parts**: `vbaProject.bin`, `pivotTables/`, `pivotCache/`, `charts/`, slicers, sparklines must survive unchanged.
5. **After saving output.xlsx, verify it** with `xlsx_reader.py --diff-against input.xlsx`: confirm original sheet names, named ranges, pivots/macros, and a sample of original data are present. If verification fails, fix it before delivering.

Never use an openpyxl round-trip on existing files. Instead: **unpack → use helper scripts / edit XML → repack.**

### “Fill cells” / “Add formulas to existing cells” = EDIT task
```bash
python3 SKILL_DIR/scripts/xlsx_unpack.py input.xlsx /tmp/xlsx_work/
# Find target sheet XML via xl/workbook.xml -> xl/_rels/workbook.xml.rels
#   <c r="B3"><f>SUM('销售数据'!D2:D13)</f><v></v></c>
python3 SKILL_DIR/scripts/xlsx_pack.py /tmp/xlsx_work/ output.xlsx
```

### Add a column (formulas, numfmt, styles auto-copied from adjacent column)
```bash
python3 SKILL_DIR/scripts/xlsx_unpack.py input.xlsx /tmp/xlsx_work/
python3 SKILL_DIR/scripts/xlsx_add_column.py /tmp/xlsx_work/ --col G \
    --sheet "Sheet1" --header "占比" \
    --formula '=F{row}/$F$10' --formula-rows 2:9 \
    --total-row 10 --total-formula '=SUM(G2:G9)' --numfmt '0.0%' \
    --border-row 10 --border-style medium
python3 SKILL_DIR/scripts/xlsx_pack.py /tmp/xlsx_work/ output.xlsx
```
`--border-row` applies a top border to ALL cells in that row (not just the new column). Use for accounting-style total rows.

### Insert a row (shifts rows, updates SUM formulas, fixes circular refs)
```bash
python3 SKILL_DIR/scripts/xlsx_unpack.py input.xlsx /tmp/xlsx_work/
# Locate the row by its LABEL text (works for Chinese labels too):
#   grep -n "办公租金" /tmp/xlsx_work/xl/worksheets/sheet*.xml
#   (Chinese labels usually live in xl/sharedStrings.xml — grep there as well)
python3 SKILL_DIR/scripts/xlsx_insert_row.py /tmp/xlsx_work/ --at 5 \
    --sheet "2025预算" --text A=水电费 \
    --values B=3000 C=3000 D=3500 E=3500 \
    --formula 'F=SUM(B{row}:E{row})' --copy-style-from 4
python3 SKILL_DIR/scripts/xlsx_pack.py /tmp/xlsx_work/ output.xlsx
```
**Row lookup rule**: when the task says “after row N (Label)”, find the row by searching for the label in `sharedStrings.xml`/worksheet XML and use the real row number + 1 for `--at`. `xlsx_insert_row.py` calls `xlsx_shift_rows.py` internally — do not call it separately.

### Row-wide borders (e.g. accounting line on a TOTAL row)
Append a new `<border>` in `xl/styles.xml`, append an `<xf>` clone in `<cellXfs>` setting the new `borderId`, then apply that style index to every `<c>` in the row via the `s` attribute. Iterate over ALL cells A through the last column.
```xml
<border><left/><right/><top style="medium"/><bottom/><diagonal/></border>
```

### Manual XML edit (anything helper scripts don’t cover)
```bash
python3 SKILL_DIR/scripts/xlsx_unpack.py input.xlsx /tmp/xlsx_work/
# ... edit XML (UTF-8) ...
python3 SKILL_DIR/scripts/xlsx_pack.py /tmp/xlsx_work/ output.xlsx
```

### Common corruption traps (handled by pack/fix scripts)
- Edited a string but didn’t update `sharedStrings.xml` `count`/`uniqueCount`.
- Edited a formula but left a stale `calcChain.xml` (delete it; the app rebuilds).
- Re-ordered ZIP parts or recompressed `vbaProject.bin` on pack (keep order; store the macro blob).
- Wrote XML in a non-UTF-8 encoding → Chinese turns into mojibake. Always UTF-8.

---

## 6. FIX — Repair broken formulas (read `references/fix.md` first)
EDIT task. Unpack → fix broken `<f>` nodes / rebuild `calcChain.xml` → pack. Preserve all original sheets and data. Re-validate after.

---

## 7. VALIDATE — Check formulas (read `references/validate.md` first)
- Static: `formula_check.py file.xlsx --report` (exit code 0 = safe). Flags `#REF!`, `#DIV/0!`, `#VALUE!`, `#N/A`, `#NAME?`, broken ranges, circular refs.
- Dynamic: `libreoffice_recalc.py file.xlsx` to recalculate and write back cached values (openpyxl does NOT evaluate formulas).

---

## 8. REVIEW — Visual verification (read `references/visual-review.md` first)
Closes the “no visual interface” gap so a human can confirm results. `xlsx_render.py` forces CJK-capable fonts so Chinese text renders instead of showing tofu boxes (□□□).
```bash
python3 SKILL_DIR/scripts/xlsx_render.py output.xlsx --out review/        # one PNG per sheet
python3 SKILL_DIR/scripts/xlsx_render.py --diff input.xlsx output.xlsx --html review/diff.html  # before/after
```
Review checklist: layout alignment, **CJK text not clipped/tofu**, number/date formats, error values, pivots/charts still render.

---

## 9. Troubleshooting
| Symptom | Cause | Fix |
|---------|-------|-----|
| App shows “needs repair” on open | stale `calcChain.xml`, bad `sharedStrings` count, re-ordered/recompressed parts | delete `calcChain.xml`; run `formula_check.py`; repack with `xlsx_pack.py` |
| Cells show old value or 0 after editing formula | openpyxl doesn’t calculate | run `libreoffice_recalc.py`, then re-render |
| Charts / pivots / macros vanished | openpyxl round-trip | redo via unpack/edit/pack; never `Workbook().save()` over existing file |
| Chinese text shows as 锟斤拷/锘/gibberish | wrong encoding | read/write XML as UTF-8; for CSV detect GB18030/GBK (section 10) |
| Chinese renders as □□□ boxes in PNG | missing CJK font | `xlsx_render.py` sets a CJK font; install Noto Sans CJK if `doctor.py` flags it |
| Wrong row edited | trusted prompt’s row number | locate by label text in the XML first |

---

## 10. Chinese / CJK Context Handbook

**Encoding**
- `.xlsx`/`.xlsm` internals are always UTF-8 — keep them UTF-8 on edit.
- CSV/TSV from Chinese Windows Excel is usually **GB18030/GBK** (sometimes UTF-8 with BOM). When reading:
  ```python
  import pandas as pd
  for enc in ("utf-8-sig", "gb18030", "utf-16"):
      try:
          df = pd.read_csv(path, encoding=enc); break
      except UnicodeDecodeError:
          continue
  ```
- When writing CSV for the user to open in Excel, use `encoding="utf-8-sig"` (BOM) so Excel shows Chinese correctly.

**Text / XML**
- Store Chinese cell text as inline strings and escape `& < >`. Sheet names, named ranges, and headers may be Chinese — quote sheet names in formulas: `SUM('销售数据'!D2:D13)`.
- Beware **full-width** characters (`，；：（）％` and full-width digits `１２３`). Normalize to half-width for numeric parsing when needed (`unicodedata.normalize('NFKC', s)`).

**Numbers & currency**
- RMB currency format: `"¥"#,##0.00` (or `"￥"` full-width). For accounting style use `_-"¥"* #,##0.00_-;-"¥"* #,##0.00`.
- 万/亿 scaling: Chinese reports often show 万 (10⁴) or 亿 (10⁸). Display via custom number format `0!.0,,"亿"` is unreliable; prefer a helper column that divides (`=B2/100000000`) with header “金额（亿元）”, keeping the source value intact.
- Thousands separator `#,##0`; percentage `0.0%`.

**Dates**
- Chinese date format: `yyyy"年"m"月"d"日"`; with weekday `aaaa` → 星期X. Keep the underlying value a real date serial, only the number format is Chinese.

**Rendering & fonts**
- Use `xlsx_render.py` (it selects an installed CJK font: Noto Sans CJK SC/TC, Source Han, WenQuanYi, or Microsoft YaHei if present). Verify with `doctor.py`.

**Layout**
- Widen columns for CJK (≈ chars × 2.1 + 2). Avoid wrapping headers awkwardly; set row height if wrapping Chinese text.
- Sorting Chinese text: default is by Unicode code point. If the user wants pinyin or stroke order, sort explicitly with `pypinyin` (pinyin) or document the limitation.

---

## Financial Color Standard (optional — only for financial models)
| Cell Role | Font Color | Hex |
|-----------|-----------|-----|
| Hard-coded input / assumption | Blue | `0000FF` |
| Formula / computed result | Black | `000000` |
| Cross-sheet reference formula | Green | `00B050` |

Finance display conventions (when applicable): zeros as `-`, negatives in red parentheses, multiples as `5.2x`, units in headers (e.g. `营业收入（亿元）`).

---

## Key Rules
1. **READ → ACT → VERIFY** every time. Never skip READ or VERIFY.
2. **Formula-First**: every calculated cell uses an Excel formula, not a hardcoded number.
3. **CREATE → XML template**: copy minimal template, edit XML, pack.
4. **EDIT/FIX → XML unpack/edit/pack**: never openpyxl round-trip on existing files.
5. **Preserve everything not targeted**: sheets, named ranges, pivots, macros, charts.
6. **UTF-8 always; detect CSV encoding**: never corrupt Chinese text.
7. **Always produce the output file** and validate before delivery (`formula_check.py` exit 0).
8. **Show your work**: render with CJK fonts whenever layout matters.

---

## Utility Scripts
```bash
python3 SKILL_DIR/scripts/doctor.py                                  # NEW: env + CJK-font self-check
python3 SKILL_DIR/scripts/xlsx_reader.py input.xlsx                  # structure discovery
python3 SKILL_DIR/scripts/xlsx_reader.py out.xlsx --diff-against in.xlsx  # NEW: structural diff for EDIT verification
python3 SKILL_DIR/scripts/formula_check.py file.xlsx --json          # formula validation (machine-readable)
python3 SKILL_DIR/scripts/formula_check.py file.xlsx --report        # formula validation (report)
python3 SKILL_DIR/scripts/libreoffice_recalc.py file.xlsx            # recalc & write back cached values
python3 SKILL_DIR/scripts/xlsx_render.py file.xlsx --out review/      # NEW: render PNG/PDF with CJK fonts
python3 SKILL_DIR/scripts/xlsx_render.py --diff in.xlsx out.xlsx --html review/diff.html  # NEW: before/after HTML
python3 SKILL_DIR/scripts/xlsx_unpack.py in.xlsx /tmp/work/           # unpack for XML editing
python3 SKILL_DIR/scripts/xlsx_pack.py /tmp/work/ out.xlsx            # repack (preserves part order & macros)
python3 SKILL_DIR/scripts/xlsx_shift_rows.py /tmp/work/ insert 5 1    # shift rows for insertion
python3 SKILL_DIR/scripts/xlsx_add_column.py /tmp/work/ --col G ...    # add column with formulas
python3 SKILL_DIR/scripts/xlsx_insert_row.py /tmp/work/ --at 6 ...     # insert row with data
```

## References
```
references/quickstart.md       — the READ→ACT→VERIFY loop (section 0)        [NEW]
references/decision-guide.md   — which path to take (section 2)             [NEW]
references/read-analyze.md     — reading & pandas analysis
references/create.md           — building new workbooks
references/format.md           — number formats, styles, colors, CJK
references/edit.md             — EDIT integrity rules & XML editing
references/fix.md              — repairing formulas / corruption
references/validate.md         — formula validation & recalculation
references/visual-review.md    — rendering & visual checks (CJK)            [NEW]
references/troubleshooting.md  — common failures & fixes (section 9)         [NEW]
references/cjk-guide.md        — Chinese / CJK handbook (section 10)         [NEW]
```
