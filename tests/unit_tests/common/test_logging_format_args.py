# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Guard against ``logging`` calls whose arguments cannot be formatted.

``logging`` interpolates lazily with printf-style ``%`` formatting. When the
format string and the positional arguments disagree -- ``{}`` placeholders
copied from ``str.format``, a missing argument, or a stray comma that turns an
intended implicit string concatenation into extra arguments -- ``Logger.emit``
raises ``TypeError`` internally. ``Handler.handleError`` swallows it, prints
``--- Logging error ---`` plus a traceback to stderr, and the intended record
never reaches any handler. Because these calls live in ``except`` blocks, the
diagnostic is lost exactly when it is needed.

The mismatch is invisible until the branch executes, so it is pinned here by
scanning the package source rather than by exercising each call site.
"""

import ast
import re
from pathlib import Path

import jiuwenswarm

PACKAGE_ROOT = Path(jiuwenswarm.__file__).parent

LEVEL_METHODS = frozenset({"debug", "info", "warning", "error", "critical", "exception"})

# printf-style conversion specifiers; the ``%%`` escape is not a placeholder.
_CONVERSION = re.compile(r"%[-#0 +]*[0-9*]*(?:\.[0-9*]+)?[hlL]?[diouxXeEfFgGcrsa%]")


def _count_placeholders(fmt: str) -> int:
    return sum(1 for m in _CONVERSION.finditer(fmt) if not m.group(0).endswith("%"))


def _iter_logger_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in LEVEL_METHODS:
            continue
        owner = func.value
        name = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
        if "log" in name.lower():
            yield node


def _mismatches(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in _iter_logger_calls(tree):
        if not node.args:
            continue
        fmt_node = node.args[0]
        if not (isinstance(fmt_node, ast.Constant) and isinstance(fmt_node.value, str)):
            continue
        fmt = fmt_node.value
        # ``%(name)s`` consumes a single mapping argument, so counting does not apply.
        if "%(" in fmt:
            continue
        args = node.args[1:]
        if any(isinstance(a, ast.Starred) for a in args):
            continue
        expected = _count_placeholders(fmt)
        if expected != len(args):
            rel = path.relative_to(PACKAGE_ROOT.parent)
            found.append(
                f"{rel}:{node.lineno}: {expected} placeholder(s) but {len(args)} "
                f"argument(s) -- {fmt!r}"
            )
    return found


def test_logging_calls_have_matching_format_arguments():
    mismatches = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        try:
            mismatches.extend(_mismatches(path))
        except SyntaxError:
            # Templates and vendored snippets are not always importable Python.
            continue

    assert mismatches == [], "logging format/argument mismatch:\n" + "\n".join(mismatches)


def test_detector_flags_a_known_bad_call(tmp_path):
    """The scan must actually catch the shapes this module is guarding against."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "logger.warning('braces instead of percent: {}', exc)\n"
        "logger.warning('two %s slots %s', only_one)\n"
        "logger.info('prefix(%s):', 'stray', 'commas')\n"
        "logger.info('fine %s and %%s literal', value)\n",
        encoding="utf-8",
    )
    # ``_mismatches`` reports paths relative to the package parent, so point it there.
    tree = ast.parse(sample.read_text(encoding="utf-8"))
    bad = [
        node.lineno
        for node in _iter_logger_calls(tree)
        if node.args
        and isinstance(node.args[0], ast.Constant)
        and _count_placeholders(node.args[0].value) != len(node.args[1:])
    ]
    assert bad == [3, 4, 5]
