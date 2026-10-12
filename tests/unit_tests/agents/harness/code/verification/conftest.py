# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fixtures for the verification-loop tests: tiny fake repositories."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.code.verification import clear_discovery_cache
from jiuwenswarm.agents.harness.code.verification import runner as runner_mod

# The real interpreter lookup, for tests that exercise it directly.
REAL_FIND_PYTHON = runner_mod.find_python


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _fresh_cache_and_python(monkeypatch):
    """Clear the discovery cache and run pytest with this interpreter (it has pytest)."""
    clear_discovery_cache()
    monkeypatch.setattr(runner_mod, "find_python", lambda *_a, **_k: sys.executable)
    yield
    clear_discovery_cache()


@pytest.fixture
def py_repo(tmp_path) -> Path:
    """A pytest project: calc/ops.py covered by tests/test_ops.py; util.py uncovered."""
    root = tmp_path / "repo"
    write(root, "pytest.ini", "[pytest]\n")
    write(root, "calc/__init__.py", "")
    write(root, "calc/ops.py", "def add(a, b):\n    return a + b\n")
    write(root, "calc/util.py", "def noop():\n    return None\n")
    write(
        root,
        "tests/test_ops.py",
        """
        from calc.ops import add


        def test_add():
            assert add(1, 2) == 3


        def test_add_zero():
            assert add(0, 0) == 0
        """,
    )
    return root
