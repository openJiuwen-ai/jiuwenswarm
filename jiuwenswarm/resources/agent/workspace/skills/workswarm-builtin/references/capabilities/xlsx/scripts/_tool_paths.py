#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_tool_paths.py - Resolve the external CLI tools this skill shells out to.

Every script that runs an external program resolves it here, so the diagnostic
verdict of ``doctor.py`` and the actual capability of ``libreoffice_recalc.py`` /
``xlsx_render.py`` can never disagree: a tool that is reachable through a
fallback path is reported as present, and one that is not is reported missing by
both sides.

Kept free of third-party imports on purpose. The render path deliberately avoids
``_xlsx_common`` (it pulls in lxml/openpyxl), and ``doctor.py`` must still be
able to say "lxml is missing" instead of failing to import itself.
"""
import os
import shutil

# Usual install locations, for when PATH is minimal: Windows installers do not
# add the ``program`` directory of LibreOffice to PATH, macOS dmg/cask installs
# put the binary inside the app bundle, and service / CI contexts often see only
# /usr/bin and /usr/local/bin.
SOFFICE_FALLBACKS = (
    "/usr/bin/soffice",
    "/usr/local/bin/soffice",
    "/usr/lib/libreoffice/program/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/opt/homebrew/bin/soffice",
    r"C:\Program Files\LibreOffice\program\soffice.exe",
)

PDFTOPPM_FALLBACKS = (
    "/usr/bin/pdftoppm",
    "/usr/local/bin/pdftoppm",
    "/opt/homebrew/bin/pdftoppm",
)

FC_LIST_FALLBACKS = (
    "/usr/bin/fc-list",
    "/usr/local/bin/fc-list",
    "/opt/homebrew/bin/fc-list",
)


def resolve(tool, fallbacks=()):
    """Absolute path of an external tool, or None when it is unavailable.

    ``shutil.which`` wins, so a tool that is actually on PATH keeps precedence
    over the install-location guesses in ``fallbacks``.
    """
    found = shutil.which(tool)
    if found:
        return os.path.abspath(found)
    for candidate in fallbacks:
        if os.path.isabs(candidate) and os.path.exists(candidate):
            return candidate
    return None
