# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Plain pytest projects with no pytest configuration (found in the desktop test).

The desktop practice project had ``calc/__init__.py``, a root-level
``test_calc.py`` and a ``.venv`` with pytest, but no ``pytest.ini`` /
``pyproject.toml`` / ``conftest.py``. ``run_tests`` answered "no test framework
detected" and the model fell back to bash. These tests use real temp projects,
the real interpreter lookup and the real runner.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.code.verification import runner as runner_mod
from jiuwenswarm.agents.harness.code.verification.discovery import (
    PYTHON,
    discover_tests,
)
from jiuwenswarm.agents.harness.code.verification.tool import RunTestsTool

from .conftest import REAL_FIND_PYTHON, write

_CALC = """
def add(a, b):
    return a + b


def mean(values):
    return sum(values) / (len(values) + 1)
"""

_PYTEST_STYLE = """
from calc import add, mean


def test_add():
    assert add(2, 3) == 5


def test_mean():
    assert mean([2, 4, 6]) == 4
"""

_UNITTEST_STYLE = """
import unittest

from calc import add, mean


class CalcTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_mean(self):
        self.assertEqual(mean([2, 4, 6]), 4)
"""


def _project(
    root: Path, test_rel: str = "test_calc.py", test_text: str = _PYTEST_STYLE
) -> Path:
    write(root, "calc/__init__.py", _CALC)
    write(root, test_rel, test_text)
    return root


def _venv_with_pytest(root: Path) -> Path:
    """``<root>/.venv/bin/python`` that runs this interpreter (which has pytest)."""
    bindir = root / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    bindir.mkdir(parents=True)
    python = bindir / "python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    python.chmod(0o755)
    return python


def _venv_without_pytest(root: Path) -> Path:
    """A real ``<root>/.venv`` (stdlib only, so pytest cannot be imported)."""
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(root / ".venv")], check=True
    )
    python = root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / "python"
    probe = subprocess.run(
        [str(python), "-c", "import pytest"], capture_output=True, check=False
    )
    assert probe.returncode != 0, "the fresh venv unexpectedly has pytest"
    return python


@pytest.fixture
def real_python_lookup(monkeypatch):
    monkeypatch.setattr(runner_mod, "find_python", REAL_FIND_PYTHON)


def _tool(root: Path) -> RunTestsTool:
    return RunTestsTool(root_resolver=lambda: str(root), spill=False)


# ─── discovery ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "test_rel",
    [
        "test_calc.py",
        "calc_test.py",
        "tests/test_calc.py",
        "pkg/tests/test_calc.py",
        "src/pkg/test_calc.py",
    ],
)
def test_config_free_test_files_are_detected_as_pytest(tmp_path, test_rel):
    root = _project(tmp_path / "p", test_rel)
    fw = discover_tests(root).for_language(PYTHON)
    assert fw is not None and fw.name == "pytest" and fw.config_file == ""


@pytest.mark.parametrize(
    "hidden",
    [
        ".venv/lib/test_x.py",
        "node_modules/pkg/test_x.py",
        ".git/test_x.py",
        "venv/test_x.py",
        "build/test_x.py",
        "env/lib/test_x.py",
    ],
)
def test_test_files_in_environments_and_build_dirs_are_ignored(tmp_path, hidden):
    root = tmp_path / "p"
    write(root, "calc/__init__.py", _CALC)
    write(root, hidden, "def test_x():\n    pass\n")
    if hidden.startswith("env/"):
        write(root, "env/pyvenv.cfg", "home = /usr/bin\n")
    assert discover_tests(root).for_language(PYTHON) is None


def test_unittest_style_files_are_marked(tmp_path):
    root = _project(tmp_path / "p", test_text=_UNITTEST_STYLE)
    fw = discover_tests(root).for_language(PYTHON)
    assert fw is not None and fw.name == "pytest" and fw.unittest_compatible
    plain = _project(tmp_path / "q")
    assert not discover_tests(plain).for_language(PYTHON).unittest_compatible


def test_a_test_file_added_later_is_picked_up(tmp_path):
    root = tmp_path / "p"
    write(root, "calc/__init__.py", _CALC)
    assert discover_tests(root).for_language(PYTHON) is None
    write(root, "test_calc.py", _PYTEST_STYLE)
    fw = discover_tests(root).for_language(PYTHON)
    assert fw is not None and fw.name == "pytest"


# ─── the tool end to end ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_desktop_layout_runs_pytest_from_the_project_venv(
    tmp_path, real_python_lookup
):
    root = _project(tmp_path / "calc-project")
    python = _venv_with_pytest(root)
    out = await _tool(root).invoke({"files": ["calc/__init__.py"]})
    assert out.success, out.error
    data = out.data
    assert data["outcome"] == "failed"
    [result] = data["results"]
    assert result["framework"] == "pytest" and result["language"] == "python"
    assert result["counts"].get("failed") == 1 and result["counts"].get("passed") == 1
    assert result["command"].startswith(str(python))
    assert any("test_mean" in t for t in result["failing"])


@pytest.mark.asyncio
async def test_without_pytest_unittest_style_tests_run_with_unittest(
    tmp_path, real_python_lookup
):
    root = _project(tmp_path / "p", test_text=_UNITTEST_STYLE)
    _venv_without_pytest(root)
    out = await _tool(root).invoke({"tests": ["test_calc.py"]})
    assert out.success, out.error
    [result] = out.data["results"]
    assert result["framework"] == "unittest"
    assert out.data["outcome"] == "failed"
    assert "pytest is not installed" in out.data["content"]


@pytest.mark.asyncio
async def test_without_pytest_plain_tests_explain_what_to_do(
    tmp_path, real_python_lookup
):
    root = _project(tmp_path / "p")
    python = _venv_without_pytest(root)
    out = await _tool(root).invoke({"tests": ["test_calc.py"]})
    assert out.success, out.error
    [result] = out.data["results"]
    assert result["outcome"] == "error"
    content = out.data["content"]
    assert "pytest is not installed" in content and str(python) in content
    assert out.data["passed_languages"] == []


@pytest.mark.asyncio
async def test_no_framework_message_says_what_was_checked(tmp_path):
    root = tmp_path / "empty"
    write(root, "README.md", "hello\n")
    out = await _tool(root).invoke({})
    assert not out.success
    assert "test_*.py" in out.error and "*_test.py" in out.error
