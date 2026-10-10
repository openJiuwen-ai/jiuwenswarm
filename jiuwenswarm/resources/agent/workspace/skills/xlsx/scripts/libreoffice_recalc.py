#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""libreoffice_recalc.py - Recalculate formulas and write back cached values.

openpyxl and manual XML edits do NOT evaluate formulas. This runs LibreOffice
headless to load + recalculate + re-save the workbook, populating cached values
(especially for newly added formula cells that have no <v> yet).

Usage:
    python3 libreoffice_recalc.py file.xlsx            # recalc in place
    python3 libreoffice_recalc.py file.xlsx --out out.xlsx

Notes:
- Requires soffice (LibreOffice). Run doctor.py to verify.
- LibreOffice reconverts the file; for files with exotic features prefer to keep
  a backup and verify with xlsx_reader.py --diff-against afterwards.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile


def main():
    ap = argparse.ArgumentParser(description="Recalculate workbook via LibreOffice")
    ap.add_argument("file")
    ap.add_argument("--out", help="output path (defaults to recalc in place)")
    args = ap.parse_args()

    if shutil.which("soffice") is None:
        print("soffice (LibreOffice) not found; see doctor.py", file=sys.stderr)
        return 2
    src = os.path.abspath(args.file)
    if not os.path.exists(src):
        print("file not found: " + src, file=sys.stderr)
        return 1

    tmp = tempfile.mkdtemp(prefix="xlsx_recalc_")
    profile = tempfile.mkdtemp(prefix="lo_profile_")
    ext = os.path.splitext(src)[1].lower().lstrip(".") or "xlsx"
    flt = "xlsm:Calc MS Excel 2007 VBA" if ext == "xlsm" else "xlsx:Calc MS Excel 2007 XML"
    cmd = [
        "soffice", "--headless", "--norestore", "--calc",
        "--convert-to", flt, "--outdir", tmp, src,
        "-env:UserInstallation=file://" + profile,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=180)
    except subprocess.CalledProcessError as e:  # noqa: BLE001
        print("LibreOffice failed: " + (e.stderr or str(e)), file=sys.stderr)
        return 1
    base = os.path.splitext(os.path.basename(src))[0]
    produced = os.path.join(tmp, base + "." + ext)
    if not os.path.exists(produced):
        # LibreOffice may emit .xlsx regardless; find the single output
        cands = [os.path.join(tmp, n) for n in os.listdir(tmp)]
        if not cands:
            print("no output produced", file=sys.stderr)
            return 1
        produced = cands[0]
    dest = os.path.abspath(args.out) if args.out else src
    shutil.copyfile(produced, dest)
    print("recalculated -> " + dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
