# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Dependency-provenance guards (issue #5473).

Two unit-test modules used to prepend a sibling ``agent-core`` checkout to
``sys.path`` at import time. The entries outlived the importing module, and
interpreters started through ``multiprocessing.spawn`` re-resolved
``openjiuwen`` from that arbitrary checkout, breaking unrelated tests
depending on collection order.

Guards:

1. The imported ``openjiuwen`` package must resolve to the installed
   environment, never to a sibling source checkout.
2. No test module may mutate ``sys.path`` with a path that does not provably
   resolve inside this repository.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    raise AssertionError(f"cannot locate repository root from {__file__}")


def test_openjiuwen_resolves_to_installed_environment() -> None:
    """The pinned dependency wins; a sibling checkout must not shadow it."""
    import openjiuwen

    package_file = Path(getattr(openjiuwen, "__file__", "") or "")
    assert package_file.suffix or package_file.name, "openjiuwen has no __file__"
    resolved = package_file.resolve()
    repo_root = _repo_root()
    sibling_checkout = repo_root.parent / "agent-core"
    assert sibling_checkout not in resolved.parents, (
        f"openjiuwen resolves to sibling checkout {resolved}; "
        "test another revision via an explicit pyproject.toml pin instead"
    )
    assert repo_root not in resolved.parents, (
        f"openjiuwen resolves inside this repository ({resolved}); "
        "tests must exercise the installed dependency"
    )


class _Unevaluatable(Exception):
    """Raised when a path expression cannot be resolved statically."""


def _eval_test_path(node: ast.AST, test_file: Path) -> Path:
    """Statically evaluate a ``__file__``-rooted path expression.

    Supports the shapes used for legitimate in-repo insertions:
    ``Path(__file__)``, ``.resolve()``, ``.parent``, ``.parents[N]``,
    ``/ "segment"`` and ``str(...)``. Anything else is rejected so a new
    dynamic entry cannot slip past the guard unnoticed.
    """
    if isinstance(node, ast.Call):
        func = node.func
        if (
            isinstance(func, ast.Name)
            and func.id in {"str", "Path"}
            and len(node.args) == 1
            and not node.keywords
        ):
            return _eval_test_path(node.args[0], test_file)
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "resolve"
            and not node.args
            and not node.keywords
        ):
            return _eval_test_path(func.value, test_file).resolve()
        raise _Unevaluatable(f"unsupported call: {ast.dump(node)}")
    if isinstance(node, ast.Name):
        if node.id == "__file__":
            return test_file
        raise _Unevaluatable(f"unsupported name: {node.id}")
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        return _eval_test_path(node.value, test_file).parent
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "parents"
    ):
        index = node.slice
        if isinstance(index, ast.Constant) and isinstance(index.value, int):
            return _eval_test_path(node.value.value, test_file).parents[index.value]
        raise _Unevaluatable(f"unsupported parents index: {ast.dump(node)}")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _eval_test_path(node.left, test_file)
        right = node.right
        if isinstance(right, ast.Constant) and isinstance(right.value, str):
            return left / right.value
        raise _Unevaluatable(f"unsupported path segment: {ast.dump(node)}")
    raise _Unevaluatable(f"unsupported expression: {ast.dump(node)}")


def _sys_path_mutations(tree: ast.AST) -> list[ast.AST]:
    """Collect ``sys.path`` mutations where ``sys`` is the stdlib module."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sys":
                    bound.add(alias.asname or "sys")
        elif isinstance(node, ast.ImportFrom) and node.module == "sys":
            bound.update(alias.asname or alias.name for alias in node.names)
    if not bound:
        return []
    hits: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            receiver = node.func.value
            if (
                isinstance(receiver, ast.Attribute)
                and receiver.attr == "path"
                and isinstance(receiver.value, ast.Name)
                and receiver.value.id in bound
                and node.func.attr in {"insert", "append", "extend"}
            ):
                hits.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "path"
                    and isinstance(target.value, ast.Name)
                    and target.value.id in bound
                ):
                    hits.append(node)
    return hits


def _check_test_file(path: Path, repo_root: Path) -> list[str]:
    """Return violation messages for out-of-repo ``sys.path`` mutations."""
    violations: list[str] = []
    try:
        # utf-8-sig: some test modules carry a BOM.
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except SyntaxError as exc:
        return [f"{path.relative_to(repo_root)}: unparseable ({exc})"]
    for node in _sys_path_mutations(tree):
        lineno = getattr(node, "lineno", "?")
        where = f"{path.relative_to(repo_root)}:{lineno}"
        args = node.args if isinstance(node, ast.Call) else []
        if not args:
            violations.append(f"{where}: sys.path assignment is not allowed in tests")
            continue
        try:
            resolved = _eval_test_path(args[-1], path)
        except _Unevaluatable:
            violations.append(
                f"{where}: sys.path entry is not statically provable "
                "to resolve inside the repository"
            )
            continue
        if repo_root not in (resolved, *resolved.parents):
            violations.append(f"{where}: sys.path entry escapes repository -> {resolved}")
    return violations


def test_no_test_side_sys_path_escapes_repo() -> None:
    """Every ``tests/`` sys.path entry must provably resolve inside the repo."""
    repo_root = _repo_root()
    violations: list[str] = []
    for path in sorted((repo_root / "tests").rglob("*.py")):
        violations.extend(_check_test_file(path, repo_root))
    assert not violations, (
        "test-side sys.path entries escaping the repository:\n"
        + "\n".join(violations)
    )


def test_sys_path_guard_rejects_sibling_checkout() -> None:
    """The guard itself flags the removed ``agent-core`` pattern."""
    repo_root = Path("/repo").resolve()
    test_file = repo_root / "tests" / "unit_tests" / "test_example.py"
    bad = ast.parse(
        "import sys\n"
        "from pathlib import Path\n"
        '_R = Path(__file__).resolve().parents[3].parent / "agent-core"\n'
        "sys.path.insert(0, str(_R))\n"
    )
    # A dynamic name cannot be proven to resolve in-repo, so it is flagged.
    mutations = _sys_path_mutations(bad)
    assert len(mutations) == 1
    flagged = False
    try:
        _eval_test_path(mutations[0].args[-1], test_file)
    except _Unevaluatable:
        flagged = True
    assert flagged, "dynamic sibling-checkout entry must not be provable"

    good = ast.parse(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[2]))\n"
    )
    good_mutations = _sys_path_mutations(good)
    assert len(good_mutations) == 1
    resolved = _eval_test_path(good_mutations[0].args[-1], test_file)
    assert repo_root in (resolved, *resolved.parents)


def test_no_agent_core_backed_sys_path_entries() -> None:
    """No ``sys.path`` mutation may be backed by a sibling agent-core checkout.

    Unlike the pure substring check this stays green for the guard itself:
    only mutations whose own source segment references ``agent-core`` fail.
    """
    repo_root = _repo_root()
    offenders = []
    for path in sorted((repo_root / "tests").rglob("*.py")):
        # utf-8-sig: some test modules carry a BOM.
        source = path.read_text(encoding="utf-8-sig")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in _sys_path_mutations(tree):
            segment = ast.get_source_segment(source, node) or ""
            if "agent-core" in segment.replace("_", "-"):
                lineno = getattr(node, "lineno", "?")
                offenders.append(f"{path.relative_to(repo_root)}:{lineno}")
    assert not offenders, (
        "test modules still backing sys.path with a sibling agent-core "
        f"checkout: {offenders}"
    )


if __name__ == "__main__":
    sys.exit(f"run with pytest: {Path(__file__).name}")
