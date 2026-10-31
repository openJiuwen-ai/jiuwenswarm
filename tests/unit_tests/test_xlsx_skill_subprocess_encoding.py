# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Guards the `xlsx` skill scripts against decoding with an ambient codec.

``subprocess.run(..., text=True)`` without an explicit ``encoding`` decodes via
``io.text_encoding(None)``, which becomes ``"utf-8"`` as soon as Python runs in
UTF-8 mode (``PYTHONUTF8=1``; ``uv run`` sets it).  Windows console tools still
emit ANSI/OEM bytes, so the decode raises ``UnicodeDecodeError`` inside
subprocess' reader thread: the caller never sees the exception and silently gets
an empty ``stdout``.  ``fc-list`` output disappeared this way, so the render
path concluded that no CJK font was installed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

RESOURCES = Path(__file__).resolve().parents[2] / "jiuwenswarm" / "resources"
SCRIPTS = RESOURCES / "agent" / "workspace" / "skills" / "xlsx" / "scripts"
SAFE_ERRORS = frozenset({"replace", "ignore", "backslashreplace"})


def _subprocess_run_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "run"
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        ):
            yield node


@pytest.mark.unit
def test_text_mode_calls_pin_an_explicit_codec() -> None:
    """Every ``subprocess.run(..., text=True)`` in the skill pins a codec."""
    offenders: list[str] = []
    for path in sorted(SCRIPTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in _subprocess_run_calls(tree):
            kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}
            text = kwargs.get("text")
            if not (isinstance(text, ast.Constant) and text.value is True):
                continue
            where = f"{path.name}:{call.lineno}"
            if "encoding" not in kwargs:
                offenders.append(f"{where} (missing encoding=)")
                continue
            errors = kwargs.get("errors")
            if not (isinstance(errors, ast.Constant) and errors.value in SAFE_ERRORS):
                offenders.append(f"{where} (missing tolerant errors=)")

    assert not offenders, (
        "subprocess.run(..., text=True) must pin 'encoding=' plus a tolerant "
        "'errors=', so the decode never depends on the interpreter's UTF-8 "
        "mode: " + ", ".join(offenders)
    )
