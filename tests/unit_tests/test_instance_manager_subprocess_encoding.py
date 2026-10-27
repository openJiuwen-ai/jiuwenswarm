# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Guards against decoding Windows console output with the wrong codec.

``subprocess.run(..., text=True)`` without an explicit ``encoding`` decodes the
child's output with ``io.text_encoding(None)``.  That value is the locale
encoding (``cp936`` on a zh-CN console) *until* Python runs in UTF-8 mode, where
it silently becomes ``"utf-8"``.  ``uv run`` sets ``PYTHONUTF8=1``, so the
documented development flow enables UTF-8 mode by default.

Windows console tools such as ``netstat`` and ``tasklist`` still emit bytes in
the ANSI/OEM code page.  Decoding them as UTF-8 raises ``UnicodeDecodeError``
*inside subprocess' reader thread*, so the exception never reaches the caller:
the call simply returns an empty ``stdout``.  Callers that parse that output
conclude "nothing found" -- ``is_process_alive()`` reported live processes as
dead and ``_find_pid_by_port()`` returned ``None`` for a listening port.

Passing an explicit ``encoding`` plus a tolerant ``errors`` handler makes the
decode deterministic and independent of the interpreter's UTF-8 mode.  Both are
asserted below: an explicit ``encoding`` alone still raises when the bytes do
not match it, so either one missing leaves the crash reachable.
"""

from __future__ import annotations

import ast
import os
import platform
import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# Only the modules this change touches are guarded.  Scanning the whole
# ``instance_manager`` package would also assert on call sites this change does
# not modify (and therefore cannot fix), which turns the guard into a statement
# about merge order instead of about this change.
GUARDED_MODULES = (
    ROOT / "jiuwenswarm" / "instance_manager" / "status.py",
)


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


SAFE_ERROR_HANDLERS = frozenset({"replace", "ignore", "backslashreplace"})


def _constant_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


@pytest.mark.unit
def test_text_mode_probes_declare_an_explicit_encoding() -> None:
    """Every ``subprocess.run(..., text=True)`` in the guarded modules pins a codec.

    The error handler is asserted as well as the codec: an explicit
    ``encoding`` alone still raises from subprocess' reader thread when the
    bytes do not match it, and that is exactly the failure this test exists to
    prevent.
    """
    offenders: list[str] = []
    for path in GUARDED_MODULES:
        assert path.is_file(), f"guarded module is missing: {path}"
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
        "Windows console output is never decoded with a codec chosen by the "
        "interpreter's UTF-8 mode: " + ", ".join(offenders)
    )


@pytest.mark.unit
@pytest.mark.skipif(
    platform.system().lower() != "windows",
    reason="exercises the Windows netstat-based PID lookup",
)
def test_find_pid_by_port_resolves_own_listener() -> None:
    """A port we hold ourselves must resolve back to our own PID.

    This is the observable symptom of the decoding bug: when the output fails to
    decode, ``netstat``'s lines are lost, the lookup finds nothing and returns
    ``None`` even though the port is plainly listed as LISTENING.
    """
    from jiuwenswarm.instance_manager.status import _find_pid_by_port

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        assert _find_pid_by_port(port) == os.getpid()
