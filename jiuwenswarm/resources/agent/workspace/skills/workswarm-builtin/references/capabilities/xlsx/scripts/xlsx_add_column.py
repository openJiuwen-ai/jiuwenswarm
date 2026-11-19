#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""xlsx_add_column.py - Add a computed column to an unpacked workbook dir.

Operates on a directory produced by xlsx_unpack.py (edit XML in place, then
repack with xlsx_pack.py). Zero format loss: existing cells are untouched.

Example:
  python3 xlsx_add_column.py /tmp/work/ --col G --sheet "Sheet1" \
      --header "Share" --header-row 1 \
      --formula '=F{row}/$G$11' --formula-rows 2:10 \
      --total-row 11 --total-formula '=SUM(G2:G10)' \
      --numfmt '0.0%' --border-row 11 --border-style medium

Notes:
- {row} in --formula is replaced by each target row number.
- --numfmt applies to the new column's formula/total cells.
- --border-row puts a top border across that row, preserving each existing
  cell's number format.
"""
import argparse
import sys

import _xlsx_common as X
from _xlsx_common import M


def numfmt_of(st, sidx):
    if sidx is None:
        return 0
    cx = st.root.find(M + "cellXfs")
    if cx is None:
        return 0
    xfs = cx.findall(M + "xf")
    i = int(sidx)
    if 0 <= i < len(xfs):
        return int(xfs[i].get("numFmtId", "0"))
    return 0


def style_of(sd, col, rownum):
    for r in sd.findall(M + "row"):
        if int(r.get("r")) == rownum:
            for c in r.findall(M + "c"):
                cc, _ = X.split_ref(c.get("r"))
                if cc == col:
                    return c.get("s")
    return None


def main():
    ap = argparse.ArgumentParser(description="Add a computed column")
    ap.add_argument("workdir")
    ap.add_argument("--col", required=True, help="target column letter, e.g. G")
    ap.add_argument("--sheet")
    ap.add_argument("--header")
    ap.add_argument("--header-row", type=int, default=1)
    ap.add_argument("--formula", help="formula template; {row} -> row number")
    ap.add_argument("--formula-rows", help="inclusive range like 2:9")
    ap.add_argument("--total-row", type=int)
    ap.add_argument("--total-formula")
    ap.add_argument("--numfmt")
    ap.add_argument("--border-row", type=int)
    ap.add_argument("--border-style", default="medium")
    args = ap.parse_args()

    col = X.split_ref(args.col + "1")[0]
    sheet = args.sheet or X.first_sheet_name(args.workdir)
    wspath = X.resolve_worksheet(args.workdir, sheet)
    tree = X.parse(wspath)
    root = tree.getroot()
    sd = X.get_sheetdata(root)

    st = X.Styles(args.workdir)
    nfid = st.add_numfmt(args.numfmt) if args.numfmt else 0
    data_xf = st.add_xf(numFmtId=nfid) if nfid else None
    bid = st.add_top_border(args.border_style) if args.border_row else 0
    total_xf = st.add_xf(numFmtId=nfid, borderId=bid) if args.border_row else data_xf

    # header
    if args.header is not None:
        hstyle = style_of(sd, X.num_to_col(max(1, X.col_to_num(col) - 1)),
                          args.header_row)
        row = X.get_or_make_row(sd, args.header_row)
        X.set_cell_in_row(row, X.make_cell(col + str(args.header_row),
                                           text=args.header, style=hstyle))

    # formula cells
    if args.formula and args.formula_rows:
        a, b = args.formula_rows.split(":")
        for rn in range(int(a), int(b) + 1):
            f = args.formula.replace("{row}", str(rn))
            row = X.get_or_make_row(sd, rn)
            X.set_cell_in_row(row, X.make_cell(col + str(rn), formula=f,
                                               style=data_xf))

    # total cell
    if args.total_row and args.total_formula:
        row = X.get_or_make_row(sd, args.total_row)
        X.set_cell_in_row(row, X.make_cell(col + str(args.total_row),
                                           formula=args.total_formula,
                                           style=total_xf))

    # row-wide top border, preserving each cell's number format
    if args.border_row:
        row = X.get_or_make_row(sd, args.border_row)
        for c in row.findall(M + "c"):
            keep = numfmt_of(st, c.get("s"))
            c.set("s", str(st.add_xf(numFmtId=keep, borderId=bid)))

    X.sort_sheetdata(sd)
    X.update_dimension(root)
    st.save()
    X.write(tree, wspath)
    print("added column " + col + " on '" + sheet + "'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
