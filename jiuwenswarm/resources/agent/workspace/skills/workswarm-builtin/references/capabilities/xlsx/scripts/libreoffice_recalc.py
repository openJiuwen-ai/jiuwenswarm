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
import logging
import os
import shutil
import subprocess
import sys
import tempfile

from _tool_paths import SOFFICE_FALLBACKS, resolve

# All console output goes through logging (G.LOG.02): info stays on stdout,
# warnings/errors on stderr, messages unchanged so callers keep parsing them.
log = logging.getLogger("xlsx.recalc")


def _setup_logging():
    if log.handlers:
        return
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(logging.Formatter("%(message)s"))
    out.addFilter(lambda rec: rec.levelno < logging.WARNING)
    err = logging.StreamHandler(sys.stderr)
    err.setFormatter(logging.Formatter("%(message)s"))
    err.addFilter(lambda rec: rec.levelno >= logging.WARNING)
    log.addHandler(out)
    log.addHandler(err)
    log.setLevel(logging.INFO)
    log.propagate = False


def _drop(*scratch):
    """Delete the scratch dirs held by TemporaryDirectory handles (G.FIO.04)."""
    for handle in scratch:
        handle.cleanup()


def main():
    ap = argparse.ArgumentParser(description="Recalculate workbook via LibreOffice")
    ap.add_argument("file")
    ap.add_argument("--out", help="output path (defaults to recalc in place)")
    args = ap.parse_args()

    _setup_logging()

    soffice = resolve("soffice", SOFFICE_FALLBACKS)
    if soffice is None:
        log.error("soffice (LibreOffice) not found; see doctor.py")
        return 2
    src = os.path.abspath(args.file)
    if not os.path.exists(src):
        log.error("file not found: %s", src)
        return 1

    # The output dir and the throwaway LibreOffice profile are temporary. They
    # are removed by _drop() on every exit path, including the returns below.
    scratch = [tempfile.TemporaryDirectory(prefix="xlsx_recalc_"),
               tempfile.TemporaryDirectory(prefix="lo_profile_")]
    tmp, profile = (handle.name for handle in scratch)
    ext = os.path.splitext(src)[1].lower().lstrip(".") or "xlsx"
    flt = "xlsm:Calc MS Excel 2007 VBA" if ext == "xlsm" else "xlsx:Calc MS Excel 2007 XML"
    cmd = [
        soffice, "--headless", "--norestore", "--calc",
        "--convert-to", flt, "--outdir", tmp, src,
        "-env:UserInstallation=file://" + profile,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    except subprocess.CalledProcessError as e:  # noqa: BLE001
        log.error("LibreOffice failed: %s", e.stderr or str(e))
        _drop(*scratch)
        return 1
    base = os.path.splitext(os.path.basename(src))[0]
    produced = os.path.join(tmp, base + "." + ext)
    if not os.path.exists(produced):
        # LibreOffice may emit .xlsx regardless; find the single output
        cands = [os.path.join(tmp, n) for n in os.listdir(tmp)]
        if not cands:
            log.error("no output produced")
            _drop(*scratch)
            return 1
        produced = cands[0]
    dest = os.path.abspath(args.out) if args.out else src
    shutil.copyfile(produced, dest)
    _drop(*scratch)
    log.info("recalculated -> %s", dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
