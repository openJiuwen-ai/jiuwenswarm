# READ & Analyze

Goal: understand and summarize existing data **without modifying the source**.

## Step 1 - structure discovery
```bash
python3 SKILL_DIR/scripts/xlsx_reader.py input.xlsx
python3 SKILL_DIR/scripts/xlsx_reader.py input.xlsx --sheet "销售数据" --preview 20
```
Note sheet names, dimensions, named ranges, and whether macros/pivots/charts are
present (those matter if you later EDIT).

## Step 2 - analyze with pandas
```python
import pandas as pd
sheets = pd.read_excel("input.xlsx", sheet_name=None)   # dict of DataFrames
df = sheets["销售数据"]
print(df.head())
total = df["营业收入"].sum()        # aggregate straight from the column
```

### Rules
- **Decimals**: if the user asks for N decimal places, format every numeric value
  consistently, e.g. `f"{v:.2f}"`. Never show `12875` when `12875.00` is required.
- **Aggregation**: compute sums/means/counts directly from the DataFrame column.
  Do not re-derive values first.
- **Large files**: enumerate with `sheet_name=None`; `.head()` before full loads;
  use `usecols=` / `nrows=` to limit.
- **Never write to the source** during analysis.

## Chinese data (see references/cjk-guide.md)
- CSV/TSV from Chinese Excel is often **GB18030/GBK**. Detect encoding:
  ```python
  for enc in ("utf-8-sig", "gb18030", "utf-16"):
      try:
          df = pd.read_csv(path, encoding=enc); break
      except UnicodeDecodeError:
          continue
  ```
- Normalize full-width digits before numeric parsing:
  `import unicodedata; s = unicodedata.normalize("NFKC", s)`.
- Chinese column names are fine as DataFrame keys; index by exact string.
