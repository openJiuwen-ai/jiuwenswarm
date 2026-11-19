# VALIDATE formulas & integrity

Two layers: static checks (fast) and dynamic recalculation (authoritative).

## Static check
```bash
python3 SKILL_DIR/scripts/formula_check.py file.xlsx --report   # exit 0 = clean
python3 SKILL_DIR/scripts/formula_check.py file.xlsx --json     # machine-readable
```
Flags Excel error tokens (`#REF!`, `#DIV/0!`, `#VALUE!`, `#N/A`, `#NAME?`,
`#NULL!`, `#NUM!`) in both cached values and formula text.

## Dynamic recalculation
openpyxl and manual XML edits do NOT evaluate formulas. To populate/refresh
cached values:
```bash
python3 SKILL_DIR/scripts/libreoffice_recalc.py file.xlsx
```
Newly added formula cells (no `<v>`) are computed; then re-run `formula_check.py`
to confirm no errors surfaced after evaluation.

## Structural validation (after an EDIT)
```bash
python3 SKILL_DIR/scripts/xlsx_reader.py output.xlsx --diff-against input.xlsx
```
Confirms sheets, named ranges, macros, pivots, and charts survived, and lists a
sample of changed cells.

## Visual validation
```bash
python3 SKILL_DIR/scripts/xlsx_render.py output.xlsx --out review/
```
Confirms layout, CJK text (no □□□ / mojibake), number/date formats, and that
pivots/charts render. See references/visual-review.md.

## Definition of done
- [ ] `formula_check.py` exits 0
- [ ] recalc ran; cached values present
- [ ] `--diff-against` shows nothing precious was lost
- [ ] rendered preview looks correct (incl. Chinese)
