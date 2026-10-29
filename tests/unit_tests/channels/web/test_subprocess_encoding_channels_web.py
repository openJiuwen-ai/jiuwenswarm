# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Guards against decoding picker subprocess output with the wrong codec.

``subprocess.run(..., text=True)`` without an explicit ``encoding`` decodes the
child's output with ``io.text_encoding(None)``.  That value is the locale
encoding (``cp936`` on a zh-CN console) *until* Python runs in UTF-8 mode, where
it silently becomes ``"utf-8"``.  ``uv run`` sets ``PYTHONUTF8=1``, so the
documented development flow enables UTF-8 mode by default.

The pickers under ``jiuwenswarm/channels/web`` spawn a second interpreter to run
their tkinter dialogs and then read the selected path back from its stdout.  A
path is arbitrary user data, so a mismatch between the decoding codec and the
bytes actually written raises ``UnicodeDecodeError`` *inside subprocess' reader
thread*: the exception never reaches the caller, the call simply returns an
empty ``stdout`` and the dialog looks like it was cancelled.

Passing an explicit ``encoding`` plus a tolerant ``errors`` handler makes the
decode deterministic and independent of the interpreter's UTF-8 mode.  Both are
asserted below: an explicit ``encoding`` alone still raises when the bytes do
not match it, so either one missing leaves the failure reachable.  The child
side is pinned as well (``PYTHONIOENCODING=utf-8``), so both ends of the pipe
agree even when the parent's locale is an ANSI code page.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
CHANNELS_WEB_DIR = ROOT / "jiuwenswarm" / "channels" / "web"

SAFE_ERROR_HANDLERS = frozenset({"replace", "ignore", "backslashreplace"})


def _subprocess_run_targets(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Collect the names that refer to ``subprocess`` and to ``subprocess.run``.

    Handles ``import subprocess``, ``import subprocess as sp`` and
    ``from subprocess import run`` so a rename cannot hide a call site.
    """
    module_aliases = {"subprocess"}
    direct_run_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name == "subprocess":
                    module_aliases.add(item.asname or item.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for item in node.names:
                if item.name == "run":
                    direct_run_names.add(item.asname or item.name)
    return module_aliases, direct_run_names


def _iter_subprocess_run_calls(tree: ast.AST):
    module_aliases, direct_run_names = _subprocess_run_targets(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in direct_run_names:
            yield node
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "run"
            and isinstance(func.value, ast.Name)
            and func.value.id in module_aliases
        ):
            yield node


def _keyword(call: ast.Call, name: str):
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _constant_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


@pytest.mark.unit
def test_text_mode_picker_calls_declare_an_explicit_encoding() -> None:
    """Every ``subprocess.run(..., text=True)`` in channels/web pins a codec.

    The error handler is asserted as well as the codec: an explicit
    ``encoding`` alone still raises from subprocess' reader thread when the
    bytes do not match it, and that is exactly the failure this test exists to
    prevent.
    """
    offenders: list[str] = []
    for path in sorted(CHANNELS_WEB_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in _iter_subprocess_run_calls(tree):
            text_kw = _keyword(call, "text")
            if not (isinstance(text_kw, ast.Constant) and text_kw.value is True):
                continue
            rel = path.relative_to(ROOT).as_posix()
            if _keyword(call, "encoding") is None:
                offenders.append(f"{rel}:{call.lineno} (missing encoding=)")
                continue
            errors = _constant_str(_keyword(call, "errors"))
            if errors not in SAFE_ERROR_HANDLERS:
                offenders.append(f"{rel}:{call.lineno} (errors={errors!r})")

    assert not offenders, (
        "subprocess.run(..., text=True) must pin 'encoding=' plus a tolerant "
        "'errors=' (one of " + ", ".join(sorted(SAFE_ERROR_HANDLERS)) + "), so "
        "picker output is never decoded with a codec chosen by the "
        "interpreter's UTF-8 mode: " + ", ".join(offenders)
    )
