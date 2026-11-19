#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""xlsx_pack.py - Repack a directory into a valid .xlsx/.xlsm.

Usage:
    python3 xlsx_pack.py /tmp/work/ output.xlsx

Preserves package integrity:
- [Content_Types].xml is written first, then _rels, then the rest.
- vbaProject.bin is stored uncompressed (ZIP_STORED) so macros stay valid.
- All other parts are deflated.

Does NOT recompute sharedStrings counts or rebuild calcChain - edit those in the
unpacked dir if needed (see references/fix.md). Newly added formula cells without
a cached <v> are recalculated by Excel on open or via libreoffice_recalc.py.
"""
import os
import sys
import zipfile


def _order_key(arc):
    if arc == "[Content_Types].xml":
        return (0, arc)
    if arc.startswith("_rels/"):
        return (1, arc)
    return (2, arc)


def main():
    if len(sys.argv) != 3:
        print("usage: xlsx_pack.py SRCDIR/ OUTPUT.xlsx", file=sys.stderr)
        return 2
    srcdir, out = sys.argv[1], sys.argv[2]
    if not os.path.isdir(srcdir):
        print("not a directory: " + srcdir, file=sys.stderr)
        return 1
    arcs = []
    for root, _dirs, files in os.walk(srcdir):
        for n in files:
            full = os.path.join(root, n)
            arc = os.path.relpath(full, srcdir).replace(os.sep, "/")
            arcs.append(arc)
    if "[Content_Types].xml" not in arcs:
        print("WARNING: [Content_Types].xml missing - output may be invalid",
              file=sys.stderr)
    arcs.sort(key=_order_key)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for arc in arcs:
            full = os.path.join(srcdir, arc.replace("/", os.sep))
            comp = zipfile.ZIP_STORED if arc.endswith("vbaProject.bin") else zipfile.ZIP_DEFLATED
            z.write(full, arc, compress_type=comp)
    print("packed " + str(len(arcs)) + " parts -> " + out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
