#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""xlsx_unpack.py - Unpack a workbook (ZIP of XML parts) into a directory.

Usage:
    python3 xlsx_unpack.py input.xlsx /tmp/work/

The directory can then be edited directly (UTF-8 XML) and repacked with
xlsx_pack.py. This is the format-preserving alternative to openpyxl round-trips.
"""
import os
import sys
import zipfile


def main():
    if len(sys.argv) != 3:
        print("usage: xlsx_unpack.py INPUT.xlsx OUTDIR/", file=sys.stderr)
        return 2
    src, outdir = sys.argv[1], sys.argv[2]
    if not zipfile.is_zipfile(src):
        print("not a valid xlsx/zip: " + src, file=sys.stderr)
        return 1
    os.makedirs(outdir, exist_ok=True)
    with zipfile.ZipFile(src) as z:
        z.extractall(outdir)
        names = z.namelist()
    print("unpacked " + str(len(names)) + " parts to " + outdir)
    flags = []
    if "xl/vbaProject.bin" in names:
        flags.append("VBA macros")
    if any("pivotTable" in n for n in names):
        flags.append("pivot tables")
    if any(n.startswith("xl/charts/") for n in names):
        flags.append("charts")
    if flags:
        print("NOTE: preserve these on repack -> " + ", ".join(flags))
    return 0


if __name__ == "__main__":
    sys.exit(main())
