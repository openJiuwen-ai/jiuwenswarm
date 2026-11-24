# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Change-based pytest target selection for unit tests.

Selects ``tests/`` targets from the files changed in the current branch /
working tree, plus a fixed baseline (smoke) set, so the default unit-test run
does not execute all ~28k cases.

For each changed source module the selector maps two layers to tests:

* the module itself (tests that import it directly);
* its first-layer imports (repo-internal modules it re-exports or depends on),
  so a compatibility shim that re-exports a real implementation also pulls in
  the real implementation's tests.

Output is newline-separated pytest paths, or the single sentinel ``FULL`` when
the change set is too broad (or unknown) to narrow safely.

The git/grep subprocess calls are executed concurrently with asyncio.

Environment overrides:
  TEST_BASE_REF    git ref to diff against (default: origin/develop)
  TEST_BASELINE    extra baseline paths, space-separated
  TEST_FULL=1      force ``FULL`` selection

CLI overrides:
  --base REF       same as TEST_BASE_REF
  --changed LIST   comma/space separated explicit changed paths (bypasses git;
                   useful for testing or when CI already computed the diff)
  --full           force ``FULL``
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Fixed smoke set that always runs regardless of the change set. Tune this to
# the team's foundational contracts (common utils/schema, wire protocol, ...).
BASELINE = [
    "tests/unit_tests/common/",
    "tests/unit_tests/test_protocol_cron_contract.py",
    "tests/unit_tests/agentserver/test_workflow_schema.py",
]

# Any changed path under one of these prefixes invalidates change-based
# narrowing: the result is ``FULL`` so nothing is silently skipped. Keep this
# scoped to test/build-config files, not the whole ``scripts/`` directory.
FULL_TRIGGERS = (
    "pyproject.toml",
    "pytest.ini",
    "uv.lock",
    "run_tests.sh",
    "scripts/select_tests.py",
    "tests/conftest.py",
    "conftest.py",
)

TEST_PREFIXES = ("tests/",)
SOURCE_PREFIXES = ("jiuwenswarm/", "sdks/", "jiuwenbox/", "packages/")

# Dotted prefixes of this repo's own packages; only these are followed when
# resolving a changed file's imports (stdlib/third-party imports are ignored).
REPO_MODULE_PREFIXES = ("jiuwenswarm.", "sdks.", "jiuwenbox.", "packages.")

# ``from X import ...`` / ``import X`` where X is a dotted absolute module.
_ABS_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import|import\s+([A-Za-z_][\w.]*))",
    re.MULTILINE,
)
# ``from .foo import ...`` / ``from .. import ...`` (relative imports).
_REL_IMPORT_RE = re.compile(r"^\s*from\s+(\.+)([\w.]*)\s+import", re.MULTILINE)


async def _run(*args: str) -> str:
    """Run a subprocess in the repo root, returning stdout ('' on failure)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=REPO,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return ""
        return stdout.decode("utf-8", "replace")
    except (OSError, asyncio.SubprocessError):
        return ""


async def changed_files(base: str) -> list[str]:
    """Paths changed on the branch since ``base`` plus uncommitted worktree edits."""
    diff_out, status_out = await asyncio.gather(
        _run("git", "diff", "--name-only", f"{base}...HEAD"),
        _run("git", "status", "--porcelain"),
    )

    files: set[str] = set(diff_out.split())

    # git status --porcelain: "XY path" (or "XY old -> new" on rename).
    for line in status_out.splitlines():
        path = line[3:].strip()
        if not path:
            continue
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.add(path)

    return sorted(files)


def module_of(path: str) -> str | None:
    """Map a source .py path to its dotted import path."""
    if not path.endswith(".py"):
        return None
    if path.endswith("__init__.py"):
        return path[: -len("__init__.py")].rstrip("/").replace("/", ".")
    return path[:-3].replace("/", ".")


def _package_of(path: str) -> list[str]:
    """Parent package components of a source path."""
    parent = path.rsplit("/", 1)[0]
    return parent.replace("/", ".").split(".") if parent else []


def _resolve_relative(pkg: list[str], dots: str, suffix: str) -> str | None:
    """Resolve ``from <dots><suffix> import ...`` against a file's package."""
    levels = len(dots)
    if levels - 1 > len(pkg):
        return None
    base = pkg if levels == 1 else pkg[: -(levels - 1)]
    parts = list(base)
    if suffix:
        parts.extend(suffix.split("."))
    return ".".join(parts) if parts else None


def imports_of(path: str) -> set[str]:
    """First-layer repo-internal modules imported by a source file."""
    try:
        with open(os.path.join(REPO, path), encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        return set()

    modules: set[str] = set()
    for match in _ABS_IMPORT_RE.finditer(content):
        module = match.group(1) or match.group(2)
        if module and module.startswith(REPO_MODULE_PREFIXES):
            modules.add(module)

    pkg = _package_of(path)
    for match in _REL_IMPORT_RE.finditer(content):
        resolved = _resolve_relative(pkg, match.group(1), match.group(2))
        if resolved and resolved.startswith(REPO_MODULE_PREFIXES):
            modules.add(resolved)

    return modules


async def tests_importing(module: str) -> list[str]:
    """Test files that reference the given dotted module path."""
    out = await _run(
        "grep",
        "-rlE",
        "--include=test_*.py",
        "--include=*_test.py",
        "-e",
        re.escape(module),
        "tests/",
    )
    return out.split()


async def select(
    base: str, changed_override: list[str] | None, full: bool
) -> list[str] | str:
    if full or os.environ.get("TEST_FULL") == "1":
        return "FULL"

    changed = changed_override if changed_override is not None else await changed_files(base)
    if not changed:
        # Clean checkout with no detectable diff: run only the fixed baseline.
        return sorted(set(BASELINE))

    for path in changed:
        if any(path.startswith(t) for t in FULL_TRIGGERS):
            return "FULL"

    targets = set(BASELINE)
    targets.update(
        p for p in changed if p.startswith(TEST_PREFIXES) and p.endswith(".py")
    )

    # Direct module path + first-layer imports for each changed source file.
    modules: set[str] = set()
    for path in changed:
        if not (path.startswith(SOURCE_PREFIXES) and path.endswith(".py")):
            continue
        module = module_of(path)
        if module:
            modules.add(module)
        modules.update(imports_of(path))

    if modules:
        matched = await asyncio.gather(*(tests_importing(m) for m in sorted(modules)))
        for files in matched:
            targets.update(files)

    targets.update(os.environ.get("TEST_BASELINE", "").split())
    return sorted(targets)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=None, help="git ref to diff against")
    parser.add_argument(
        "--changed",
        default=None,
        help="explicit changed paths (comma/space separated), bypasses git",
    )
    parser.add_argument("--full", action="store_true", help="force FULL selection")
    args = parser.parse_args()

    base = args.base or os.environ.get("TEST_BASE_REF", "origin/develop")
    changed: list[str] | None = None
    if args.changed:
        changed = [c for c in re.split(r"[,\s]+", args.changed.strip()) if c]

    result = await select(base, changed, args.full)
    if result == "FULL":
        print("FULL")
        return 0
    print("\n".join(result))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
