# Visual Review — confirm results with your eyes (CJK-safe)

Closes the "no visual interface" gap. Pure XML editing is blind; rendering lets a
human (or you) confirm the output actually looks right, with Chinese text
displaying correctly instead of tofu boxes (□□□).

## Render a workbook to images
```bash
python3 SKILL_DIR/scripts/xlsx_render.py output.xlsx --out review/
# -> review/output.pdf and review/output_p-1.png, _p-2.png, ... (one per page)
```
PDF only (faster, vector):
```bash
python3 SKILL_DIR/scripts/xlsx_render.py output.xlsx --pdf-only --out review/
```

## Before / after comparison (great for EDIT tasks)
```bash
python3 SKILL_DIR/scripts/xlsx_render.py --diff input.xlsx output.xlsx --html review/diff.html
```
Produces a single self-contained HTML with original on the left and edited on the
right (images embedded as base64, so it is portable). Headings are bilingual
(原始 / 修改后).

## How it works
1. LibreOffice (`soffice --headless --convert-to pdf`) renders the real workbook.
2. poppler (`pdftoppm -png`) rasterizes each PDF page to PNG.
3. The script forces a **CJK-capable font** so Chinese/Japanese/Korean text
   renders. It probes `fc-list` for Noto Sans CJK / Source Han / WenQuanYi /
   Microsoft YaHei. If none is found it warns you — run `doctor.py` and install
   one (e.g. `google-noto-sans-cjk-fonts`).

## Review checklist
- [ ] Layout/alignment correct; columns wide enough.
- [ ] **Chinese text not clipped and not shown as □□□.**
- [ ] Number formats right (¥, thousands, %, 万/亿 units, decimals).
- [ ] Date formats right (yyyy年m月d日 if requested).
- [ ] No visible error values (#REF!, #DIV/0!, ...).
- [ ] Pivot tables and charts still render.
- [ ] Totals/subtotals match expectations.

## Requirements
`soffice` (LibreOffice) and `pdftoppm` (poppler-utils) must be installed, plus a
CJK font. Verify all three with `python3 SKILL_DIR/scripts/doctor.py`.
