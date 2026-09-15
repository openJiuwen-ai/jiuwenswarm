# Troubleshooting — common failures & fixes

Centralizes the failure modes that were previously scattered across reference
files. Scan the symptom column first.

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Excel says **"We found a problem... needs repair"** on open | stale `calcChain.xml`; wrong `sharedStrings` count/uniqueCount; ZIP parts reordered or recompressed | delete `xl/calcChain.xml`; fix `sharedStrings` counts; repack with `xlsx_pack.py` (preserves order); run `formula_check.py` |
| Edited a formula but cell shows **old value / 0 / blank** | openpyxl & manual edits do not recalculate | `python3 scripts/libreoffice_recalc.py file.xlsx` then re-render |
| **Charts / pivot tables / macros disappeared** | used openpyxl `load_workbook().save()` round-trip | redo via unpack → edit → pack; never round-trip an existing file. Confirm with `xlsx_reader.py --diff-against` |
| **Chinese text = 锟斤拷 / mojibake** in cells | XML or CSV written in wrong encoding | always read/write XML as **UTF-8**; for CSV detect `gb18030`/`gbk`; write CSV as `utf-8-sig` |
| **Chinese renders as □□□ boxes** in PNG/PDF | no CJK font available to LibreOffice | `xlsx_render.py` auto-selects a CJK font; if missing, install Noto Sans CJK (`doctor.py` flags it) |
| **Wrong row/column edited** | trusted the prompt's row number literally | locate the row by its **label text** (grep `sharedStrings.xml` / worksheet XML), then compute the real index |
| `#REF!` after inserting/deleting rows | ranges not shifted | use `xlsx_insert_row.py` (calls `xlsx_shift_rows.py`); it updates SUM ranges |
| Full-width digits `１２３` not parsed as numbers | full-width chars | normalize with `unicodedata.normalize('NFKC', s)` before parsing |
| Columns too narrow, headers clipped | CJK glyphs ~2x ASCII width | widen columns (~ chars × 2.1 + 2) |
| `soffice` hangs or "source file could not be loaded" | profile lock / bad path | `xlsx_render.py` uses an isolated `UserInstallation` profile; ensure the input path is correct and the file is valid |
| pivot values look stale | pivot cache not refreshed | pivots refresh on open in Excel; for a rendered preview, recalc first, note cache may differ |

## Diagnostic commands
```bash
python3 SKILL_DIR/scripts/doctor.py                              # environment + fonts
python3 SKILL_DIR/scripts/xlsx_reader.py file.xlsx               # structure + features
python3 SKILL_DIR/scripts/xlsx_reader.py new.xlsx --diff-against old.xlsx  # what changed/broke
python3 SKILL_DIR/scripts/formula_check.py file.xlsx --report    # formula errors
unzip -l file.xlsx                                               # inspect package parts
```

## Golden recovery procedure
1. `xlsx_reader.py` the broken file (if it still opens) to see what survived.
2. Re-do the change from the **original** input via unpack → edit → pack.
3. Delete `calcChain.xml`; let the app rebuild it.
4. `formula_check.py --report` until exit code 0.
5. `libreoffice_recalc.py` to refresh cached values.
6. `xlsx_render.py` to eyeball the result.
7. `xlsx_reader.py --diff-against` the original to confirm nothing precious was lost.
