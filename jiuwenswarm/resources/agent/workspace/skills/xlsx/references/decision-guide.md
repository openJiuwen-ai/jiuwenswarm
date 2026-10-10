# Decision Guide — which path do I take?

Use this flowchart before doing anything. Picking the wrong path is the #1 cause
of corrupted files (lost macros/pivots/charts) and wasted effort.

```
Is there an output workbook to produce?
├─ NO  → READ path. xlsx_reader.py + pandas. Never modify the source.
└─ YES
   ├─ Does the file already exist and must be changed?
   │   ├─ YES → EDIT/FIX path. unpack → edit XML → pack.
   │   │         NEVER openpyxl load_workbook(...).save() round-trip.
   │   └─ NO  → CREATE path. copy minimal template → edit XML → pack.
   └─ Are values stale / file "needs repair"?
       → recalc (libreoffice_recalc.py) + validate; if corrupt see troubleshooting.md
```

## Quick table

| The user says... | Path | Why |
|------------------|------|-----|
| "分析 / 总结 / 看一下这个表" (analyze/summarize) | READ | no output file needed |
| "填入 / 补全 / 加一列 / 插一行 / 改格式" on existing file | EDIT | must preserve the rest of the workbook |
| "修复公式 / 打不开 / 报错" | FIX | repair broken XML/formulas |
| "做一个新表 / 从零开始建模" | CREATE | brand-new workbook |
| "检查 / 验证公式对不对" | VALIDATE | static + recalc checks |
| "截图 / 预览 / 看看效果" | REVIEW | render PNG/PDF (CJK) |

## The format-preservation test

Run `xlsx_reader.py input.xlsx`. If ANY of these are true, you MUST stay on the
unpack/edit/pack path (openpyxl will silently destroy them):

- `has_vba_macros: True`  (.xlsm with VBA)
- `has_pivot_tables: True`
- `has_charts: True`
- conditional formatting / slicers / sparklines / named ranges present

When in doubt, assume the file is precious and use unpack/edit/pack.

## Why not openpyxl round-trip?

`openpyxl.load_workbook(f).save(f)` rewrites the entire package from openpyxl's
partial model. Anything openpyxl does not model — VBA, pivot caches, chart XML,
slicers, sparklines, some conditional formats — is **dropped**. Direct XML
editing touches only the bytes you change and leaves everything else intact.
