# FIX broken formulas / corruption

This is an EDIT task: unpack -> repair -> pack. Preserve all original sheets/data.

## Common causes & repairs
| Problem | Repair |
|---------|--------|
| `#REF!` after row/col delete | rewrite the broken `<f>` ranges to valid refs |
| Stale `calcChain.xml` -> "needs repair" | delete `xl/calcChain.xml` AND its `<Override>` in `[Content_Types].xml`; the app rebuilds it |
| `sharedStrings` count mismatch | fix `count` / `uniqueCount` to match `<si>` entries |
| Reordered / recompressed parts | repack with `xlsx_pack.py` (orders parts, stores vbaProject.bin) |
| Mojibake Chinese | the part was saved non-UTF-8; rewrite as UTF-8 |

## Workflow
```bash
python3 SKILL_DIR/scripts/xlsx_unpack.py broken.xlsx /tmp/fix/
python3 SKILL_DIR/scripts/formula_check.py broken.xlsx --report   # locate errors
# edit the offending <f> nodes in /tmp/fix/xl/worksheets/sheetN.xml (UTF-8)
# if calcChain is stale:
rm -f /tmp/fix/xl/calcChain.xml
#   then remove its <Override PartName="/xl/calcChain.xml" .../> from
#   /tmp/fix/[Content_Types].xml
python3 SKILL_DIR/scripts/xlsx_pack.py /tmp/fix/ fixed.xlsx
python3 SKILL_DIR/scripts/formula_check.py fixed.xlsx --report    # expect exit 0
python3 SKILL_DIR/scripts/libreoffice_recalc.py fixed.xlsx
python3 SKILL_DIR/scripts/xlsx_reader.py fixed.xlsx --diff-against broken.xlsx
```

## Recovery procedure
1. Read what survived (`xlsx_reader.py`).
2. Prefer redoing the change from the ORIGINAL input over patching a damaged file.
3. Validate to exit 0, recalc, render, diff. Only then deliver.
