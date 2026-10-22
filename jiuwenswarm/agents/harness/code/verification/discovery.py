# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Detect which test frameworks a workspace uses.

Called by :mod:`jiuwenswarm.agents.harness.code.verification.tool` before every
``run_tests`` call. It reads a handful of well-known project files and does a
bounded scan for Python test files named the way pytest discovers them by
default (``test_*.py`` / ``*_test.py``). It never imports project code and never
runs a subprocess. It returns the frameworks in the order the runner should
try them. Results are cached per workspace root and invalidated when any of
the probed files or the root directory changes; a cached result without a
Python framework is never reused, so a test file written later in the session
is picked up on the next call.

This module does not decide *which* tests to run; that is
:mod:`.mapping`. It also does not execute anything; that is :mod:`.runner`.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

PYTHON = "python"
TS_JS = "typescript/javascript"
GO = "go"
NATIVE_RUST = "native/rust"

# Language labels intentionally match ImplementationStateMachine's required
# language names so run_tests evidence can be recorded without translation.
LANGUAGES = (PYTHON, TS_JS, GO, NATIVE_RUST)

_PROBE_FILES = (
    "pytest.ini",
    "pyproject.toml",
    "tox.ini",
    "setup.cfg",
    "setup.py",
    "conftest.py",
    "package.json",
    "go.mod",
    "Cargo.toml",
)
_PY_TEST_DIRS = ("tests", "test", "testing")

# Bounded scan for config-free pytest projects (pytest's default discovery:
# ``test_*.py`` / ``*_test.py``). Directories pytest itself does not recurse
# into by default are skipped, as are virtual environments (a ``pyvenv.cfg``)
# and hidden directories. Symlinks are never followed.
SCAN_MAX_DEPTH = 4
_SCAN_MAX_DIRS = 400
_SCAN_MAX_MATCHES = 5
_SCAN_READ_BYTES = 64 * 1024
_SCAN_SKIP_DIRS = frozenset(
    {"node_modules", "venv", "build", "dist", "site-packages", "__pycache__", "target", "CVS", "_darcs"}
)
_UNITTEST_RE = re.compile(r"^\s*(?:import\s+unittest\b|from\s+unittest\b)|\bTestCase\)", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Framework:
    """One runnable test framework detected in the workspace."""

    name: str  # pytest | unittest | jest | vitest | npm | go | cargo
    language: str
    config_file: str = ""  # relative path of the file that revealed it ("" = heuristic)
    npm_script: str = ""  # for name == "npm": the package.json script to run
    # pytest found only by test-file names: the files also use unittest, so
    # ``python -m unittest`` can run them when pytest is not installed.
    unittest_compatible: bool = False


@dataclass(frozen=True, slots=True)
class Discovery:
    """Frameworks found under ``root`` (possibly empty)."""

    root: str
    frameworks: tuple[Framework, ...] = field(default_factory=tuple)

    def for_language(self, language: str) -> Framework | None:
        for fw in self.frameworks:
            if fw.language == language:
                return fw
        return None

    @property
    def languages(self) -> tuple[str, ...]:
        seen: list[str] = []
        for fw in self.frameworks:
            if fw.language not in seen:
                seen.append(fw.language)
        return tuple(seen)


_CACHE: dict[str, tuple[tuple, Discovery]] = {}
_CACHE_LOCK = threading.Lock()


def _fingerprint(root: Path) -> tuple:
    parts: list[tuple] = []
    try:
        parts.append(("<root>", root.stat().st_mtime_ns))
    except OSError:
        parts.append(("<root>", None))
    for name in _PROBE_FILES:
        path = root / name
        try:
            st = path.stat()
            parts.append((name, st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append((name, None, None))
    for name in _PY_TEST_DIRS:
        parts.append((name, (root / name).is_dir()))
    return tuple(parts)


def _read(path: Path, limit: int = 200_000) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def _skip_scan_dir(entry: os.DirEntry) -> bool:
    name = entry.name
    if name.startswith(".") or name in _SCAN_SKIP_DIRS or name.endswith((".egg", ".egg-info")):
        return True
    try:
        return os.path.isfile(os.path.join(entry.path, "pyvenv.cfg"))
    except OSError:
        return True


def scan_python_test_files(root: Path) -> list[str]:
    """Relative paths of up to a few pytest-named test files (breadth first, bounded)."""

    found: list[str] = []
    queue: list[tuple[Path, int]] = [(root, 0)]
    visited = 0
    while queue and visited < _SCAN_MAX_DIRS:
        directory, depth = queue.pop(0)
        visited += 1
        try:
            with os.scandir(directory) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth < SCAN_MAX_DEPTH and not _skip_scan_dir(entry):
                        queue.append((Path(entry.path), depth + 1))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            name = entry.name
            if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")):
                found.append(Path(entry.path).relative_to(root).as_posix())
                if len(found) >= _SCAN_MAX_MATCHES:
                    return found
    return found


def _uses_unittest(root: Path, rel_paths: list[str]) -> bool:
    return any(_UNITTEST_RE.search(_read(root / rel, _SCAN_READ_BYTES)) for rel in rel_paths)


def _python_framework(root: Path) -> Framework | None:
    if (root / "pytest.ini").is_file():
        return Framework("pytest", PYTHON, "pytest.ini")
    pyproject = _read(root / "pyproject.toml")
    if re.search(r"^\[tool\.pytest(\.ini_options)?\]", pyproject, re.MULTILINE):
        return Framework("pytest", PYTHON, "pyproject.toml")
    for name, section in (("tox.ini", r"^\[(pytest|tool:pytest)\]"), ("setup.cfg", r"^\[tool:pytest\]")):
        if re.search(section, _read(root / name), re.MULTILINE):
            return Framework("pytest", PYTHON, name)
    if (root / "conftest.py").is_file():
        return Framework("pytest", PYTHON, "conftest.py")
    has_python_project = any(
        (root / name).is_file() for name in ("pyproject.toml", "setup.py", "setup.cfg", "tox.ini")
    )
    test_files = scan_python_test_files(root)
    if test_files:
        return Framework("pytest", PYTHON, "", unittest_compatible=_uses_unittest(root, test_files))
    if has_python_project and "pytest" in pyproject:
        return Framework("pytest", PYTHON, "")
    if has_python_project:
        # No pytest signal at all: stdlib unittest discovery still works.
        return Framework("unittest", PYTHON, "")
    return None


def _node_framework(root: Path) -> Framework | None:
    raw = _read(root / "package.json")
    if not raw:
        return None
    try:
        pkg = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(pkg, dict):
        return None
    deps: dict = {}
    for key in ("dependencies", "devDependencies"):
        value = pkg.get(key)
        if isinstance(value, dict):
            deps.update(value)
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    if "vitest" in deps:
        return Framework("vitest", TS_JS, "package.json")
    if "jest" in deps or "ts-jest" in deps:
        return Framework("jest", TS_JS, "package.json")
    test_script = str(scripts.get("test") or "")
    if test_script and "no test specified" not in test_script:
        return Framework("npm", TS_JS, "package.json", npm_script="test")
    return None


def discover_tests(root: str | Path) -> Discovery:
    """Return the test frameworks detected under ``root`` (cached)."""

    base = Path(root).expanduser().resolve()
    key = str(base)
    fingerprint = _fingerprint(base)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        # A result without Python tests is recomputed: a test file created in a
        # nested directory does not change any fingerprinted mtime.
        if cached is not None and cached[0] == fingerprint and cached[1].for_language(PYTHON) is not None:
            return cached[1]
    frameworks: list[Framework] = []
    python = _python_framework(base)
    if python is not None:
        frameworks.append(python)
    node = _node_framework(base)
    if node is not None:
        frameworks.append(node)
    if (base / "go.mod").is_file():
        frameworks.append(Framework("go", GO, "go.mod"))
    if (base / "Cargo.toml").is_file():
        frameworks.append(Framework("cargo", NATIVE_RUST, "Cargo.toml"))
    result = Discovery(root=key, frameworks=tuple(frameworks))
    with _CACHE_LOCK:
        _CACHE[key] = (fingerprint, result)
    return result


def clear_discovery_cache() -> None:
    """Drop every cached discovery (tests use this)."""

    with _CACHE_LOCK:
        _CACHE.clear()


def language_of(path: str) -> str | None:
    """Map a file path to one of :data:`LANGUAGES` (None for non-source files)."""

    suffix = Path(path).suffix.lower()
    if suffix in {".py", ".pyi"}:
        return PYTHON
    if suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}:
        return TS_JS
    if suffix == ".go":
        return GO
    if suffix in {".rs", ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}:
        return NATIVE_RUST
    return None


__all__ = [
    "GO",
    "LANGUAGES",
    "NATIVE_RUST",
    "PYTHON",
    "SCAN_MAX_DEPTH",
    "TS_JS",
    "Discovery",
    "Framework",
    "clear_discovery_cache",
    "discover_tests",
    "language_of",
    "scan_python_test_files",
]
