# Quickstart — the READ → ACT → VERIFY loop

This is the 60-second on-ramp for the skill. Lowers the learning curve: you do not
need to understand the whole XML internals before doing useful work.

## The loop

1. **READ** — never act blindly.
   ```bash
   python3 SKILL_DIR/scripts/doctor.py            # one-time: env + CJK font check
   python3 SKILL_DIR/scripts/xlsx_reader.py input.xlsx
   ```
   Look at: sheet names, dimensions, named ranges, and whether the file has
   **macros / pivots / charts** (these force the unpack/edit/pack path).

2. **ACT** — pick exactly one path (see `decision-guide.md`):
   - **Analyze only** → pandas, never modify the source.
   - **Create new** → copy `templates/minimal_xlsx/`, edit XML, `xlsx_pack.py`.
   - **Edit / fill / fix existing** → `xlsx_unpack.py` → edit → `xlsx_pack.py`.
     Never use an openpyxl load→save round-trip on an existing file.

3. **VERIFY** — prove it works before delivering:
   ```bash
   python3 SKILL_DIR/scripts/formula_check.py output.xlsx --report   # exit 0 = safe
   python3 SKILL_DIR/scripts/xlsx_reader.py output.xlsx --diff-against input.xlsx
   python3 SKILL_DIR/scripts/xlsx_render.py output.xlsx --out review/  # eyeball it (CJK-safe)
   ```

## Minimal recipes

**"Fill in / add formulas to an existing sheet"** → EDIT task:
```bash
python3 SKILL_DIR/scripts/xlsx_unpack.py input.xlsx /tmp/work/
# edit the target <c> cells in xl/worksheets/sheetN.xml (UTF-8!)
python3 SKILL_DIR/scripts/xlsx_pack.py /tmp/work/ output.xlsx
python3 SKILL_DIR/scripts/formula_check.py output.xlsx --report
```

**"Summarize this spreadsheet"** → READ task:
```bash
python3 SKILL_DIR/scripts/xlsx_reader.py input.xlsx
python3 - <<'PY'
import pandas as pd
xls = pd.read_excel("input.xlsx", sheet_name=None)   # all sheets
for name, df in xls.items():
    print(name, df.shape); print(df.head())
PY
```

**Chinese CSV** → mind the encoding (see `cjk-guide.md`):
```python
import pandas as pd
for enc in ("utf-8-sig", "gb18030", "utf-16"):
    try:
        df = pd.read_csv("data.csv", encoding=enc); break
    except UnicodeDecodeError:
        continue
```

## Golden rules (do not break)
- Always READ before you ACT, always VERIFY before you deliver.
- Every computed cell is a **formula**, not a hardcoded number.
- On existing files, **preserve everything you were not asked to change**.
- Keep all XML **UTF-8**; detect CSV encoding for Chinese data.
