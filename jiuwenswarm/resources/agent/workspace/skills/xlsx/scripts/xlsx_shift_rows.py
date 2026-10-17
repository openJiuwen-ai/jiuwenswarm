#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""xlsx_shift_rows.py - Low-level row shifter on an unpacked workbook dir.

Usage:
    python3 xlsx_shift_rows.py /tmp/work/ insert 5 1 [--sheet NAME]
    python3 xlsx_shift_rows.py /tmp/work/ delete 5 1 [--sheet NAME]

- insert: shift rows >= AT down by COUNT (makes room).
- delete: remove COUNT rows starting at AT, then shift the rest up.
Primitive used by xlsx_insert_row.py. Updates row@r / cell@r and the sheet
dimension. Does NOT rewrite formula text. Always validate afterwards.
"""
import argparse
import sys

import _xlsx_common as X


def main():
    ap = argparse.ArgumentParser(description="Shift rows in an unpacked workbook")
    ap.add_argument("workdir")
    ap.add_argument("op", choices=["insert", "delete"])
    ap.add_argument("at", type=int)
    ap.add_argument("count", type=int, nargs="?", default=1)
    ap.add_argument("--sheet")
    args = ap.parse_args()

    sheet = args.sheet or X.first_sheet_name(args.workdir)
    wspath = X.resolve_worksheet(args.workdir, sheet)
    tree = X.parse(wspath)
    root = tree.getroot()
    sd = X.get_sheetdata(root)

    if args.op == "insert":
        X.shift_rows(sd, args.at, args.count)
    else:
        X.remove_rows(sd, args.at, args.count)
        X.shift_rows(sd, args.at + args.count, -args.count)

    X.sort_sheetdata(sd)
    X.update_dimension(root)
    X.write(tree, wspath)
    print(args.op + " " + str(args.count) + " row(s) at " + str(args.at)
          + " on '" + sheet + "'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
