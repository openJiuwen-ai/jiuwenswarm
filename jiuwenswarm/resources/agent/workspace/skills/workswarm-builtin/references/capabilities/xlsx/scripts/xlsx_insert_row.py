#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""xlsx_insert_row.py - Insert a row into an unpacked workbook dir.

Shifts existing rows down, best-effort extends SUM-style ranges so a totals row
below the insert keeps including the new row, then writes the new cells. Zero
format loss on untouched cells. Repack with xlsx_pack.py and validate after.

Example:
  python3 xlsx_insert_row.py /tmp/work/ --at 5 --sheet "Budget" \
      --text A=Utilities --values B=3000 C=3000 D=3500 E=3500 \
      --formula 'F=SUM(B{row}:E{row})' --copy-style-from 4

Notes:
- --at is the 1-based row index the new row will occupy.
- --text/--values/--formula take COL=VALUE pairs (column letter = content).
- {row} in a --formula value is replaced by --at.
- --copy-style-from copies each cell's style from that row's same column.
- The range extender only adjusts A1:B9 style ranges (not single-cell refs);
  always validate totals afterwards.
"""
import argparse
import sys

import _xlsx_common as X
from _xlsx_common import M


def parse_pairs(items):
    out = []
    for it in items or []:
        if "=" not in it:
            raise SystemExit("expected COL=VALUE, got: " + it)
        col, val = it.split("=", 1)
        out.append((X.split_ref(col + "1")[0], val))
    return out


def style_of(sd, col, rownum):
    for r in sd.findall(M + "row"):
        if int(r.get("r")) == rownum:
            for c in r.findall(M + "c"):
                cc, _ = X.split_ref(c.get("r"))
                if cc == col:
                    return c.get("s")
    return None


def main():
    ap = argparse.ArgumentParser(description="Insert a row")
    ap.add_argument("workdir")
    ap.add_argument("--at", type=int, required=True)
    ap.add_argument("--sheet")
    ap.add_argument("--text", nargs="*", default=[], help="COL=text pairs")
    ap.add_argument("--values", nargs="*", default=[], help="COL=number pairs")
    ap.add_argument("--formula", nargs="*", default=[], help="COL=formula pairs")
    ap.add_argument("--copy-style-from", type=int)
    args = ap.parse_args()

    sheet = args.sheet or X.first_sheet_name(args.workdir)
    wspath = X.resolve_worksheet(args.workdir, sheet)
    tree = X.parse(wspath)
    root = tree.getroot()
    sd = X.get_sheetdata(root)

    # make room and keep totals-below ranges inclusive of the new row
    X.shift_rows(sd, args.at, 1)
    X.extend_ranges(sd, args.at, 1)

    row = X.get_or_make_row(sd, args.at)
    src = args.copy_style_from

    def st_for(col):
        return style_of(sd, col, src) if src else None

    for col, val in parse_pairs(args.text):
        X.set_cell_in_row(row, X.make_cell(col + str(args.at), text=val,
                                           style=st_for(col)))
    for col, val in parse_pairs(args.values):
        X.set_cell_in_row(row, X.make_cell(col + str(args.at), value=val,
                                           style=st_for(col)))
    for col, val in parse_pairs(args.formula):
        f = val.replace("{row}", str(args.at))
        X.set_cell_in_row(row, X.make_cell(col + str(args.at), formula=f,
                                           style=st_for(col)))

    X.sort_sheetdata(sd)
    X.update_dimension(root)
    X.write(tree, wspath)
    print("inserted row at " + str(args.at) + " on '" + sheet + "'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
