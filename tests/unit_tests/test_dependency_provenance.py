"""The tests must run against the dependency this project ships against.

Both guards here exist because of one line that lived in
``tests/unit_tests/agentserver/test_goal_runtime_adapter.py`` for seven weeks: it
prepended a sibling ``../agent-core`` source checkout to ``sys.path`` at import
time, so that a Goal surface not yet in the pinned release could be tested.

It cost more than it bought. ``sys.path`` is process-wide and the entry outlives
the module that added it; an interpreter started with multiprocessing's *spawn*
start method inherits ``sys.path`` but not ``sys.modules``, so the child resolves
every import from scratch and reaches the checkout first. A checkout older than
the pin then kills the child during import, and the failure surfaces in whatever
unrelated test spawned it -- as an order-dependent failure that passes when run
alone. It also never achieved its own purpose: in a full run the package is
already bound to site-packages before that module is collected.

The rule these guards encode: a test may not silently substitute a different
build of a dependency. Testing against something other than the pin is a
decision that belongs in ``pyproject.toml``, where it is reviewed.
"""

from __future__ import annotations

import ast
import sysconfig
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TESTS_ROOT = _REPO_ROOT / "tests"

# The rule, and why it is a shape rather than a list of blessed files: an
# expression built only out of ``__file__`` and its ancestors can be evaluated
# without executing test code. Two files use that form today to import this
# repository from source. The check resolves the expression for the file where
# it occurs and accepts it only if the result remains inside ``_REPO_ROOT``.
# This is a literal ``sys.path`` hygiene check, not an attempt to recognize every
# alias or deliberately obscured mutation. Unsupported expressions fail closed;
# ``/`` joins are accepted only when the complete resolved path stays in the repo.


def test_the_openjiuwen_under_test_is_the_installed_one() -> None:
    """The property the pin exists to give: what the tests import is what ships.

    Asserted on the package rather than on ``sys.path`` because that is the fact
    that matters -- a stray entry is harmless until something resolves through it.
    """
    import openjiuwen

    origin = Path(openjiuwen.__file__).resolve()
    installed_roots = {
        Path(p).resolve()
        for p in (
            sysconfig.get_paths().get("purelib"),
            sysconfig.get_paths().get("platlib"),
        )
        if p
    }
    assert any(root in origin.parents for root in installed_roots), (
        f"openjiuwen resolved to {origin}, outside the installed environment "
        f"{sorted(installed_roots)}. Something put another build ahead of the "
        "pinned release; testing against it makes a green run meaningless. "
        "If openjiuwen was deliberately installed editable (for example, "
        "`uv pip install -e ../agent-core`), restore the project pin with "
        "`uv sync` before running or pushing these tests."
    )


def _is_literal_sys_path(node: ast.AST) -> bool:
    """Return whether *node* is the literal expression ``sys.path``."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "path"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def _is_literal_sys_path_target(node: ast.AST) -> bool:
    """Also recognize writes through an index or slice of ``sys.path``."""
    return _is_literal_sys_path(node.value if isinstance(node, ast.Subscript) else node)


def _sys_path_writes(tree: ast.AST) -> list[ast.AST]:
    """Find direct calls, rebindings and item/slice writes to literal ``sys.path``."""
    found: list[ast.AST] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("insert", "append", "extend")
            and _is_literal_sys_path(node.func.value)
        ):
            found.append(node)
            continue
        if isinstance(node, ast.Assign) and any(
            _is_literal_sys_path_target(target) for target in node.targets
        ):
            found.append(node)
            continue
        if isinstance(
            node, (ast.AnnAssign, ast.AugAssign)
        ) and _is_literal_sys_path_target(node.target):
            found.append(node)
    return sorted(found, key=lambda node: node.lineno)


def _sys_path_call_arguments(call: ast.Call) -> list[ast.AST] | None:
    """Return path expressions for a supported call, or ``None`` if ambiguous."""
    if call.keywords or not isinstance(call.func, ast.Attribute):
        return None
    method = call.func.attr
    if method == "insert" and len(call.args) == 2:
        return [call.args[1]]
    if method == "append" and len(call.args) == 1:
        return [call.args[0]]
    if method == "extend" and len(call.args) == 1:
        values = call.args[0]
        if isinstance(values, (ast.List, ast.Tuple, ast.Set)):
            return list(values.elts)
    return None


def _evaluate_local_path(node: ast.AST, source_path: Path) -> Path | str:
    """Evaluate the small path-expression subset allowed in ``sys.path`` writes."""
    if isinstance(node, ast.Name) and node.id == "__file__":
        return source_path

    if isinstance(node, ast.Call):
        if node.keywords:
            raise ValueError("unsupported call")
        if (
            isinstance(node.func, ast.Name)
            and node.func.id in ("Path", "str")
            and len(node.args) == 1
        ):
            value = _evaluate_local_path(node.args[0], source_path)
            return Path(value) if node.func.id == "Path" else str(value)
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "resolve"
            and not node.args
        ):
            return Path(_evaluate_local_path(node.func.value, source_path)).resolve()
        raise ValueError("unsupported call")

    if isinstance(node, ast.Attribute) and node.attr == "parent":
        return Path(_evaluate_local_path(node.value, source_path)).parent

    if (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Div)
        and isinstance(node.right, ast.Constant)
        and isinstance(node.right.value, str)
    ):
        return Path(_evaluate_local_path(node.left, source_path)) / node.right.value

    if isinstance(node, ast.Subscript):
        value = node.value
        index = node.slice
        if (
            isinstance(value, ast.Attribute)
            and value.attr == "parents"
            and isinstance(index, ast.Constant)
            and isinstance(index.value, int)
            and index.value >= 0
        ):
            base = Path(_evaluate_local_path(value.value, source_path))
            return base.parents[index.value]

    raise ValueError("unsupported path expression")


def _names_a_path_inside_repo(node: ast.AST, source_path: Path) -> bool:
    """Return whether a restricted path expression resolves inside this repo."""
    return _path_location(node, source_path) == "inside"


def _path_location(node: ast.AST, source_path: Path) -> str:
    """Classify a path expression for actionable failure messages."""
    try:
        candidate = Path(_evaluate_local_path(node, source_path)).resolve()
    except (IndexError, TypeError, ValueError):
        return "unsupported"
    if candidate == _REPO_ROOT or _REPO_ROOT in candidate.parents:
        return "inside"
    return "outside"


def test_sys_path_write_scanner_covers_direct_mutation_forms() -> None:
    tree = ast.parse(
        """
sys.path.insert(0, candidate)
sys.path.append(candidate)
sys.path.extend([candidate])
sys.path = [candidate] + sys.path
sys.path[:0] = [candidate]
sys.path += [candidate]
"""
    )
    assert [node.lineno for node in _sys_path_writes(tree)] == [2, 3, 4, 5, 6, 7]


def test_local_path_evaluator_accepts_only_resolved_repo_paths() -> None:
    source_path = _TESTS_ROOT / "unit_tests" / "example.py"
    inside = ast.parse('Path(__file__).resolve().parents[2] / "src"', mode="eval").body
    outside = ast.parse(
        'Path(__file__).resolve().parents[3] / "agent-core"', mode="eval"
    ).body
    assert _names_a_path_inside_repo(inside, source_path)
    assert not _names_a_path_inside_repo(outside, source_path)
    assert _path_location(inside, source_path) == "inside"
    assert _path_location(outside, source_path) == "outside"
    assert _path_location(ast.Name(id="candidate"), source_path) == "unsupported"


def test_extend_arguments_are_checked_individually() -> None:
    tree = ast.parse(
        "sys.path.extend([str(Path(__file__).resolve().parents[2]), "
        'str(Path(__file__).resolve().parents[2] / "src")])'
    )
    call = _sys_path_writes(tree)[0]
    assert isinstance(call, ast.Call)
    arguments = _sys_path_call_arguments(call)
    assert arguments is not None
    source_path = _TESTS_ROOT / "unit_tests" / "example.py"
    assert all(_names_a_path_inside_repo(arg, source_path) for arg in arguments)


def test_no_test_module_puts_another_build_on_sys_path() -> None:
    """Caught where it is written, not where it detonates.

    The runtime guard above only fires if the substituted package is the one that
    wins the import race, and the spawn failure it caused fired in a file that had
    nothing to do with the cause. A static check names the line.
    """
    outside_repo: list[str] = []
    unsupported: list[str] = []
    for path in sorted(_TESTS_ROOT.rglob("*.py")):
        relative = path.relative_to(_TESTS_ROOT)
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:  # not this guard's business
            continue
        if "sys.path" not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:  # syntax validation belongs to Python/pytest
            continue
        for write in _sys_path_writes(tree):
            location = f"{relative}:{write.lineno}"
            if not isinstance(write, ast.Call):
                unsupported.append(location)
                continue
            arguments = _sys_path_call_arguments(write)
            if arguments is None:
                unsupported.append(location)
                continue
            locations = {_path_location(argument, path) for argument in arguments}
            if locations <= {"inside"}:
                continue
            if "outside" in locations:
                outside_repo.append(location)
            else:
                unsupported.append(location)

    failures: list[str] = []
    if outside_repo:
        failures.append(
            "these test modules put paths outside the repository on sys.path: "
            + ", ".join(outside_repo)
            + ". Change the dependency pin in pyproject.toml instead."
        )
    if unsupported:
        failures.append(
            "these test modules modify sys.path in a form this guard cannot "
            "statically verify: "
            + ", ".join(unsupported)
            + ". Use a literal sys.path.insert/append/extend call with a "
            "Path(__file__) expression that resolves inside the repository."
        )
    assert not failures, (
        " ".join(failures)
        + " sys.path is process-wide; a spawned child inherits it and resolves "
        "imports from scratch."
    )
