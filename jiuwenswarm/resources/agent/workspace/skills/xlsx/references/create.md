# CREATE a new workbook

Build from the XML template so formulas and formatting are first-class.

## Workflow
```bash
cp -r SKILL_DIR/templates/minimal_xlsx /tmp/new_wb
# edit /tmp/new_wb/xl/worksheets/sheet1.xml (and styles.xml, workbook.xml) as UTF-8
python3 SKILL_DIR/scripts/xlsx_pack.py /tmp/new_wb output.xlsx
python3 SKILL_DIR/scripts/formula_check.py output.xlsx --report
python3 SKILL_DIR/scripts/libreoffice_recalc.py output.xlsx   # fill cached values
python3 SKILL_DIR/scripts/xlsx_render.py output.xlsx --out review/
```
For simple, style-light workbooks you may also build with `openpyxl` or
`xlsxwriter` directly - but if you need pivots/macros/charts, use the XML path.

## Cell XML cheatsheet
```xml
<!-- number -->         <c r="B2"><v>12875.5</v></c>
<!-- formula -->        <c r="D2"><f>B2+C2</f></c>
<!-- inline text (Chinese-safe) -->
<c r="A1" t="inlineStr"><is><t xml:space="preserve">营业收入</t></is></c>
<!-- styled cell (s = index into styles.xml cellXfs) -->
<c r="B2" s="3"><v>0.25</v></c>
```

## Rules
- **Formula-first**: every derived value is a formula (`<f>SUM(B2:B9)</f>`), never
  a hardcoded number.
- Use cell references, not magic numbers (`=H6*(1+$B$3)` not `=H6*1.04`).
- XML-escape `& < >`; write files as UTF-8.
- Quote Chinese/space-containing sheet names in formulas: `'销售数据'!D2`.
- Set sensible column widths (CJK ~ chars x 2.1 + 2) - see format.md / cjk-guide.md.
- New formula cells need no `<v>`; `libreoffice_recalc.py` fills cached values.
