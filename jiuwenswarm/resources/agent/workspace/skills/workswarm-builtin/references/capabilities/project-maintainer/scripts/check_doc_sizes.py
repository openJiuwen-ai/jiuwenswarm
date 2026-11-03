#!/usr/bin/env python3
"""Check .doc_project_maintainer files against path-based size budgets."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


DEFAULT_LIMIT_KB = 8

CODE_DETAIL_DOCS = {
    "actual-behavior.md",
    "contracts.md",
    "side-effects.md",
    "health.md",
    "risks.md",
    "tests.md",
}


def limit_for(relative_path: Path) -> int:
    normalized = relative_path.as_posix()
    name = relative_path.name

    if normalized == "INDEX.md":
        return 5
    if normalized == "README.md":
        return 8
    if normalized == "manifest.yaml":
        return 20
    if normalized == "project/source-symbol-inventory.json":
        return 256
    if normalized == "project/coverage-map.json":
        return 256
    if normalized == "project/symbol-audit-map.json":
        return 512
    if normalized.startswith("project/"):
        return 8
    if normalized.startswith("modules/"):
        return 8
    if normalized.startswith("directories/"):
        return 6
    if normalized.startswith("code/"):
        if name in CODE_DETAIL_DOCS:
            return 6
        if name.endswith(".md") and relative_path.parent.name and name == f"{relative_path.parent.name}.md":
            return 6
        if name.endswith((".md", ".yaml", ".yml")):
            return 4
        return 0
    if normalized.startswith("changes/records/"):
        return 6
    if normalized.startswith("changes/by-module/") or normalized.startswith("changes/by-directory/"):
        return 10
    if normalized.startswith("decisions/"):
        return 6
    if name.endswith((".md", ".yaml", ".yml")):
        return DEFAULT_LIMIT_KB
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_root", type=Path, help="Path to .doc_project_maintainer")
    args = parser.parse_args()

    root = args.artifact_root.resolve()
    if not root.exists():
        print(f"Artifact root does not exist: {root}", file=sys.stderr)
        return 2
    if not root.is_dir():
        print(f"Artifact root is not a directory: {root}", file=sys.stderr)
        return 2

    failures: list[str] = []
    checked = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        limit_kb = limit_for(relative)
        if limit_kb <= 0:
            continue
        checked += 1
        size = path.stat().st_size
        limit = limit_kb * 1024
        if size > limit:
            failures.append(f"{relative.as_posix()}: {size} bytes > {limit_kb} KB")

    if failures:
        print("Oversized project-maintainer docs:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print(f"Checked {checked} files. All files are within size budgets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
