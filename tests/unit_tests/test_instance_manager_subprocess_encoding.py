# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Guards against decoding Windows console tool output with the wrong codec.

``text=True`` without ``encoding`` decodes with ``io.text_encoding(None)``: the
locale codec, or ``utf-8`` once Python runs in UTF-8 mode (``uv run`` sets
PYTHONUTF8=1).  ``tasklist``/``netstat`` still emit ANSI/OEM bytes, so the
UnicodeDecodeError fires inside subprocess' reader thread, never reaches the
caller, and stdout comes back empty -- a live process looked dead and a
listening port looked free.  Both ``encoding`` and a tolerant ``errors`` are
asserted: either one missing leaves that failure reachable.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULES = [ROOT / "jiuwenswarm" / "instance_manager" / n for n in ("lock.py", "status.py")]
SAFE_ERRORS = frozenset({"replace", "ignore", "backslashreplace"})


def _kw(call: ast.Call, name: str):
    return next((k.value for k in call.keywords if k.arg == name), None)


@pytest.mark.unit
def test_text_mode_subprocess_calls_pin_encoding_and_errors() -> None:
    offenders: list[str] = []
    for path in MODULES:
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or getattr(node.func, "attr", None) != "run":
                continue
            text_kw = _kw(node, "text")
            if not (isinstance(text_kw, ast.Constant) and text_kw.value is True):
                continue
            if _kw(node, "encoding") is None:
                offenders.append(f"{rel}:{node.lineno} (missing encoding=)")
            elif getattr(_kw(node, "errors"), "value", None) not in SAFE_ERRORS:
                offenders.append(f"{rel}:{node.lineno} (errors= is not tolerant)")

    assert not offenders, (
        "text-mode subprocess calls must pin encoding= plus a tolerant errors= "
        "(one of " + ", ".join(sorted(SAFE_ERRORS)) + "): " + ", ".join(offenders)
    )
