# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Frozen executable's ``-c`` dispatch and explicit-failure contract.

Regression coverage for the frozen-env fake-success family: the exe used to
exit 0 without doing anything for ``-c`` commands and unknown argument
shapes, and dispatched children lost the parent-provided cwd.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


def _load_entry():
    entry_path = Path(__file__).resolve().parents[2] / "scripts" / "jiuwenswarm_exe_entry.py"
    spec = importlib.util.spec_from_file_location("test_exe_dash_c_dispatch_entry", entry_path)
    assert spec is not None and spec.loader is not None
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    return entry


def _freeze(monkeypatch, argv: list[str]) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", argv)


def test_dash_c_code_executes_with_cpython_argv_contract(monkeypatch, tmp_path) -> None:
    entry = _load_entry()
    marker = tmp_path / "marker.json"
    code = (
        "import json, sys\n"
        f"open(r'{marker}', 'w').write(json.dumps(sys.argv))\n"
    )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", ["workswarm.exe", "-I", "-c", code, "--flag", "v"])
    monkeypatch.setattr(entry, "_ensure_stdio", lambda: None)

    assert entry._dispatch() == 0
    assert json.loads(marker.read_text()) == ["-c", "--flag", "v"]
    assert sys.argv == ["workswarm.exe", "-I", "-c", code, "--flag", "v"]


def test_dash_c_takes_priority_over_py_argument(monkeypatch, tmp_path) -> None:
    """`exe -c CODE script.py` must run CODE; the .py token is its argument."""
    entry = _load_entry()
    marker = tmp_path / "marker.json"
    phantom_script = tmp_path / "missing_guard_script.py"
    code = (
        "import json, sys\n"
        f"open(r'{marker}', 'w').write(json.dumps(sys.argv))\n"
    )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", ["workswarm.exe", "-c", code, str(phantom_script)])
    monkeypatch.setattr(entry, "_ensure_stdio", lambda: None)

    assert entry._dispatch() == 0
    assert json.loads(marker.read_text()) == ["-c", str(phantom_script)]
    assert not phantom_script.exists()


def test_script_argument_beats_later_dash_c_token(monkeypatch, tmp_path) -> None:
    """`exe script.py -c CODE` runs the script; CODE is a script argument."""
    entry = _load_entry()
    marker = tmp_path / "marker.json"
    script = tmp_path / "real_script.py"
    script.write_text(
        "import json, sys\n"
        f"open(r'{marker}', 'w').write(json.dumps(sys.argv))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", ["workswarm.exe", str(script), "-c", "SHOULD_NOT_RUN = True"])
    monkeypatch.setattr(entry, "_ensure_stdio", lambda: None)

    assert entry._dispatch() == 0
    assert json.loads(marker.read_text()) == [str(script), "-c", "SHOULD_NOT_RUN = True"]


def test_child_dispatch_restores_parent_cwd(monkeypatch, tmp_path) -> None:
    entry = _load_entry()
    parent_cwd = tmp_path / "parent_workspace"
    parent_cwd.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(entry, "_ORIGINAL_CWD", str(parent_cwd), raising=False)
    code = "import os; open('cwd_marker.txt', 'w').write(os.getcwd())"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", ["workswarm.exe", "-c", code])
    monkeypatch.setattr(entry, "_ensure_stdio", lambda: None)

    assert entry._dispatch() == 0
    assert (parent_cwd / "cwd_marker.txt").read_text() == str(parent_cwd)


def test_unknown_child_arguments_fail_loudly(monkeypatch) -> None:
    """The old catch-all silently returned 0 and masked fake successes."""
    entry = _load_entry()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", ["workswarm.exe", "--totally-unknown"])
    monkeypatch.setattr(entry, "_ensure_stdio", lambda: None)

    try:
        entry._dispatch()
    except ValueError as exc:
        assert "unsupported child arguments" in str(exc)
    else:
        raise AssertionError("unknown child arguments must not exit 0")


def test_dash_c_exception_propagates_for_nonzero_exit(monkeypatch, tmp_path) -> None:
    """Errors in -c code must reach main() and exit non-zero, not exit 0."""
    entry = _load_entry()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", ["workswarm.exe", "-c", "raise RuntimeError('boom')"])
    monkeypatch.setattr(entry, "_ensure_stdio", lambda: None)

    try:
        entry._dispatch()
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("-c failures must propagate, not exit 0")
