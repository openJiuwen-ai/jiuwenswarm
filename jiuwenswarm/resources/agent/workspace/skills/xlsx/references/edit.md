# EDIT an existing workbook

**The cardinal rule: unpack -> edit XML -> pack. Never openpyxl round-trip an
existing file** (it silently drops macros, pivots, charts, slicers, sparklines).

## EDIT INTEGRITY RULES
1. NEVER create a new `Workbook()`. Always start from the original file.
2. Output has the SAME sheets (names, order, data) as input unless told otherwise.
3. Modify ONLY the target cells; leave everything else byte-for-byte.
4. Preserve `vbaProject.bin`, `pivotTables/`, `pivotCache/`, `charts/`, slicers.
5. After saving, verify: `xlsx_reader.py output.xlsx --diff-against input.xlsx`.

## Workflow
```bash
python3 SKILL_DIR/scripts/xlsx_unpack.py input.xlsx /tmp/work/
# ... edit XML, or use the helper scripts below ...
python3 SKILL_DIR/scripts/xlsx_pack.py /tmp/work/ output.xlsx
python3 SKILL_DIR/scripts/formula_check.py output.xlsx --report
python3 SKILL_DIR/scripts/libreoffice_recalc.py output.xlsx
python3 SKILL_DIR/scripts/xlsx_reader.py output.xlsx --diff-against input.xlsx
```

## "Fill cells" = EDIT
Find the sheet XML (xl/workbook.xml -> xl/_rels/workbook.xml.rels) and set cells:
```xml
<c r="B3"><f>SUM('销售数据'!D2:D13)</f></c>
```

## Add a column
```bash
python3 SKILL_DIR/scripts/xlsx_add_column.py /tmp/work/ --col G --sheet "Sheet1" \
    --header "占比" --formula '=F{row}/$F$10' --formula-rows 2:9 \
    --total-row 10 --total-formula '=SUM(G2:G9)' --numfmt '0.0%' \
    --border-row 10 --border-style medium
```

## Insert a row (shifts rows, extends SUM ranges)
```bash
# Locate the row by LABEL text first (Chinese labels live in sharedStrings.xml):
grep -n "办公租金" /tmp/work/xl/sharedStrings.xml /tmp/work/xl/worksheets/sheet*.xml
python3 SKILL_DIR/scripts/xlsx_insert_row.py /tmp/work/ --at 5 --sheet "2025预算" \
    --text A=水电费 --values B=3000 C=3000 D=3500 E=3500 \
    --formula 'F=SUM(B{row}:E{row})' --copy-style-from 4
```
**Row lookup rule**: never trust a prompt's row number literally; find the label
in the XML and compute the real index.

## After any structural edit
- The SUM-range extender is best-effort (ranges only, not single-cell refs).
  Always re-validate and visually review totals.
- Run `libreoffice_recalc.py` so formula cells get cached values.
