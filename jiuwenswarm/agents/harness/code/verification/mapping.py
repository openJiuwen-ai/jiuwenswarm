# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Map changed source files to the tests most likely to exercise them.

Used by ``run_tests`` when the model passes ``files`` instead of explicit
test ids. The heuristics are cheap and deterministic (a bounded directory
walk plus one regex scan of test files per call); they aim for "the focused
tests a developer would run first", not for completeness:

* a file that is itself a test is selected as-is;
* same-name tests anywhere in the tree (``test_foo.py`` / ``foo_test.py`` for
  ``foo.py``; ``foo.test.ts`` / ``foo.spec.ts`` / ``__tests__/foo.ts``;
  Go packages are tested per directory);
* Python only: a reverse import index — test files whose ``import`` /
  ``from ... import`` lines name the changed module.

When nothing maps, the caller decides whether to run the whole suite.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

from jiuwenswarm.agents.harness.code.verification.discovery import (
    GO,
    NATIVE_RUST,
    PYTHON,
    TS_JS,
    language_of,
)

# Directories never worth walking for tests (VCS, dependency caches, venvs).
SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
        ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea",
        ".vscode", ".eggs", "site-packages", "target", "dist",
    }
)
MAX_WALK_FILES = 20_000
MAX_SCAN_FILES = 2_000
MAX_SCAN_BYTES = 200_000
MAX_SELECTED_PER_LANGUAGE = 20

_TS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")


def is_test_file(rel_path: str) -> bool:
    """Return whether a relative path names a test file by common convention."""

    p = PurePosixPath(rel_path.replace(os.sep, "/"))
    name = p.name
    if name.endswith(".py"):
        return name.startswith("test_") or name.endswith("_test.py")
    if name.endswith("_test.go"):
        return True
    if name.endswith(_TS_SUFFIXES):
        stem = name.rsplit(".", 1)[0]
        return stem.endswith((".test", ".spec")) or "__tests__" in p.parts
    return False


def iter_files(root: Path, limit: int = MAX_WALK_FILES):
    """Yield relative POSIX paths under ``root`` (bounded, skipping :data:`SKIP_DIRS`)."""

    count = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info"))
        for name in sorted(filenames):
            count += 1
            if count > limit:
                return
            full = Path(dirpath) / name
            yield full.relative_to(root).as_posix()


def _go_package(rel_path: str) -> str:
    """``go test`` target for the package containing ``rel_path`` (``./`` or ``./dir``)."""

    parent = PurePosixPath(rel_path).parent.as_posix()
    return "./" if parent in ("", ".") else f"./{parent}"


def _python_module_names(rel_path: str) -> list[str]:
    """Dotted module names a test could import ``rel_path`` by (``src/`` layout aware)."""

    parts = list(PurePosixPath(rel_path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    names = []
    if parts:
        names.append(".".join(parts))
        for anchor in ("src", "lib"):
            if anchor in parts[:-1]:
                idx = parts.index(anchor)
                tail = parts[idx + 1:]
                if tail:
                    names.append(".".join(tail))
    return names


def _import_regex(module: str) -> re.Pattern[str]:
    parent, _, leaf = module.rpartition(".")
    alternatives = [rf"(?:from|import)\s+{re.escape(module)}(?:\s|\.|,|$)"]
    if parent:
        alternatives.append(rf"from\s+{re.escape(parent)}\s+import\s+[^\n]*\b{re.escape(leaf)}\b")
    return re.compile(r"^\s*(?:" + "|".join(alternatives) + ")", re.MULTILINE)


def map_files_to_tests(root: str | Path, files: list[str]) -> dict[str, list[str]]:
    """Return ``{language: [test targets]}`` for ``files`` (paths relative to ``root``).

    Targets are paths relative to ``root`` (Go: package directories as
    ``./dir``). Languages with nothing mapped are omitted; Rust/C changes map
    to nothing here (``cargo test`` runs the crate).
    """

    base = Path(root).expanduser().resolve()
    wanted: dict[str, list[str]] = {}

    def add(language: str, target: str) -> None:
        bucket = wanted.setdefault(language, [])
        if target not in bucket and len(bucket) < MAX_SELECTED_PER_LANGUAGE:
            bucket.append(target)

    rel_files: list[str] = []
    for raw in files or []:
        text = str(raw or "").strip()
        if not text:
            continue
        path = Path(text)
        candidate = path if path.is_absolute() else base / path
        try:
            # Resolve symlinks and "..": anything that ends up outside the root
            # is ignored (the tool already rejects such entries with a message).
            text = candidate.resolve().relative_to(base).as_posix()
        except (OSError, RuntimeError, ValueError):
            continue
        if not text or text == ".":
            continue
        rel_files.append(text)
    if not rel_files:
        return {}

    all_files: list[str] | None = None

    def tree() -> list[str]:
        nonlocal all_files
        if all_files is None:
            all_files = list(iter_files(base))
        return all_files

    py_modules: list[tuple[str, str]] = []
    for rel in rel_files:
        language = language_of(rel)
        if language is None:
            continue
        if is_test_file(rel):
            add(language, _go_package(rel) if language == GO else rel)
            continue
        stem = PurePosixPath(rel).stem
        if language == PYTHON:
            for candidate in tree():
                name = PurePosixPath(candidate).name
                if name in (f"test_{stem}.py", f"{stem}_test.py"):
                    add(PYTHON, candidate)
            py_modules.extend((rel, m) for m in _python_module_names(rel))
        elif language == TS_JS:
            for candidate in tree():
                cp = PurePosixPath(candidate)
                if not cp.name.endswith(_TS_SUFFIXES) or not is_test_file(candidate):
                    continue
                cstem = cp.name.rsplit(".", 1)[0]
                if cstem in (f"{stem}.test", f"{stem}.spec") or (
                    "__tests__" in cp.parts and cstem == stem
                ):
                    add(TS_JS, candidate)
        elif language == GO:
            add(GO, _go_package(rel))
        elif language == NATIVE_RUST:
            continue

    if py_modules:
        patterns = [(_import_regex(module)) for _, module in py_modules]
        scanned = 0
        for candidate in tree():
            if not candidate.endswith(".py") or not is_test_file(candidate):
                continue
            scanned += 1
            if scanned > MAX_SCAN_FILES:
                break
            try:
                with (base / candidate).open("r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read(MAX_SCAN_BYTES)
            except OSError:
                continue
            if any(p.search(text) for p in patterns):
                add(PYTHON, candidate)
    return wanted


__all__ = ["SKIP_DIRS", "is_test_file", "iter_files", "map_files_to_tests"]
