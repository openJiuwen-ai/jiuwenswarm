#!/usr/bin/env python3
"""
Validate TRACE_POINT naming and B/E pairing quality for operator source files.
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import re
import sys
from collections import defaultdict
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

TP_RE = re.compile(r'TRACE_POINT\s*\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)')
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\- ]*[a-z0-9]$")


def iter_targets(path: pathlib.Path) -> List[pathlib.Path]:
    if path.is_file():
        return [path]
    exts = {".h", ".hpp", ".c", ".cc", ".cpp"}
    return [p for p in path.rglob("*") if p.is_file() and p.suffix in exts]


def check_file(path: pathlib.Path) -> Tuple[int, List[str]]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    errs: List[str] = []
    pairs: Dict[str, Dict[str, int]] = defaultdict(lambda: {"B": 0, "E": 0})

    for m in TP_RE.finditer(text):
        label = m.group(1)
        event = m.group(2).upper()
        line = text.count("\n", 0, m.start()) + 1

        if not NAME_RE.match(label):
            errs.append(f"{path}:{line}: invalid label style '{label}'")
        if event not in {"B", "E"}:
            errs.append(f"{path}:{line}: unsupported event type '{m.group(2)}', only B/E allowed")
            continue
        pairs[label][event] += 1

    for label, cnt in pairs.items():
        if cnt["B"] != cnt["E"]:
            errs.append(f"{path}: unbalanced pair label='{label}' B={cnt['B']} E={cnt['E']}")

    return len(pairs), errs


def main() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Validate TRACE_POINT labels and B/E pairing.")
    parser.add_argument("target", help="source file or directory")
    args = parser.parse_args()

    target = pathlib.Path(args.target).resolve()
    files = iter_targets(target)
    if not files:
        logger.info("no source files found")
        return

    total_labels = 0
    all_errs: List[str] = []
    for f in files:
        labels, errs = check_file(f)
        total_labels += labels
        all_errs.extend(errs)

    logger.info("checked files: %d", len(files))
    logger.info("labels: %d", total_labels)

    if all_errs:
        logger.info("validation failed:")
        for e in all_errs:
            logger.info("  - %s", e)
        sys.exit(1)

    logger.info("validation passed")


if __name__ == "__main__":
    main()
