# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""run_framework / classify / render_summary with real pytest subprocesses."""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.code.verification.discovery import Framework
from jiuwenswarm.agents.harness.code.verification.runner import (
    RunResult,
    classify,
    overall_outcome,
    render_summary,
    run_framework,
)

from .conftest import write

PYTEST = Framework("pytest", "python", "pytest.ini")


@pytest.mark.asyncio
async def test_passing_run_counts_and_spills(py_repo, tmp_path):
    result = await run_framework(PYTEST, py_repo, ["tests/test_ops.py"], [], spill_dir=tmp_path / "spill")
    assert result.outcome == "passed" and result.counts.get("passed") == 2
    assert Path(result.spill_path).read_text().count("passed") >= 1


@pytest.mark.asyncio
async def test_failing_run_lists_failure_with_traceback_tail(py_repo, tmp_path):
    write(py_repo, "calc/ops.py", "def add(a, b):\n    return a - b\n")
    result = await run_framework(PYTEST, py_repo, [], [], spill_dir=tmp_path / "spill")
    assert result.outcome == "failed" and result.counts.get("failed") == 1
    failure = result.failures[0]
    assert failure.test_id == "tests/test_ops.py::test_add"
    assert any("assert" in line for line in failure.tail)
    text = render_summary([result])
    assert text.splitlines()[0] == "run_tests: outcome=failed passed_languages=-"
    assert "tests/test_ops.py::test_add" in text and "full output:" in text


@pytest.mark.asyncio
async def test_no_tests_and_collection_error(tmp_path):
    empty = tmp_path / "empty"
    write(empty, "pytest.ini", "[pytest]\n")
    write(empty, "pkg/a.py", "")
    assert (await run_framework(PYTEST, empty, [], [])).outcome == "no_tests"
    broken = tmp_path / "broken"
    write(broken, "pytest.ini", "[pytest]\n")
    write(broken, "tests/test_bad.py", "import does_not_exist_anywhere\n\ndef test_x():\n    pass\n")
    result = await run_framework(PYTEST, broken, [], [])
    assert result.outcome == "error"
    assert "test_bad.py" in render_summary([result])


@pytest.mark.asyncio
async def test_timeout_kills_the_run(tmp_path):
    root = tmp_path / "slow"
    write(root, "pytest.ini", "[pytest]\n")
    write(root, "tests/test_slow.py", "import time\n\ndef test_slow():\n    time.sleep(30)\n")
    result = await run_framework(PYTEST, root, [], [], timeout=2)
    assert result.outcome == "timeout" and result.duration_s < 20


@pytest.mark.asyncio
async def test_leaked_safepath_env_does_not_break_project_imports(py_repo, monkeypatch):
    # A launcher that exported PYTHONSAFEPATH=1 must not stop `import calc` from the repo root.
    monkeypatch.setenv("PYTHONSAFEPATH", "1")
    write(py_repo, "tests/test_cwd_import.py", "import subprocess, sys\n\ndef test_cwd():\n"
          "    subprocess.check_call([sys.executable, '-c', 'import calc'])\n")
    result = await run_framework(PYTEST, py_repo, ["tests/test_cwd_import.py"], [])
    assert result.outcome == "passed", render_summary([result])


@pytest.mark.asyncio
async def test_unittest_framework(tmp_path):
    root = tmp_path / "ut"
    write(root, "tests/__init__.py", "")
    write(root, "tests/test_u.py", "import unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(1, 1)\n")
    result = await run_framework(Framework("unittest", "python"), root, [], [])
    assert result.outcome == "passed", render_summary([result])


def test_classify_go_cargo_js():
    go_out = "--- FAIL: TestAdd (0.00s)\n    add_test.go:9: got 1\nFAIL\nFAIL\tex/calc\t0.1s\n"
    outcome, counts, failures, _ = classify(Framework("go", "go"), 1, go_out)
    assert outcome == "failed" and failures[0].test_id == "TestAdd"
    cargo = "test tests::a ... FAILED\ntest result: FAILED. 3 passed; 1 failed; 0 ignored\n"
    outcome, counts, failures, _ = classify(Framework("cargo", "native/rust"), 101, cargo)
    assert outcome == "failed" and counts == {"passed": 3, "failed": 1} and failures[0].test_id == "tests::a"
    jest = "  ● math › adds\nTests:       1 failed, 4 passed, 5 total\n"
    outcome, counts, failures, _ = classify(Framework("jest", "typescript/javascript"), 1, jest)
    assert outcome == "failed" and counts["passed"] == 4 and failures[0].test_id == "math › adds"
    outcome, *_ = classify(Framework("go", "go"), 1, "go: cannot find main module")
    assert outcome == "error"


def test_summary_is_capped_and_keeps_header():
    big = RunResult("pytest", "python", ["py", "-m", "pytest"], "failed", 1, 1.0, counts={"failed": 200})
    from jiuwenswarm.agents.harness.code.verification.runner import Failure

    big.failures = [Failure(f"tests/test_{i}.py::test_x", "AssertionError " + "x" * 200, ["line"] * 12) for i in range(200)]
    text = render_summary([big], max_chars=1500)
    assert len(text) <= 1500 and text.startswith("run_tests: outcome=failed")
    assert "summary truncated" in text


def test_overall_outcome_worst_wins():
    def r(o):
        return RunResult("x", "python", [], o, 0, 0.0)

    assert overall_outcome([r("passed"), r("failed")]) == "failed"
    assert overall_outcome([r("passed"), r("timeout")]) == "timeout"
    assert overall_outcome([r("passed"), r("passed")]) == "passed"
    assert overall_outcome([]) == "no_tests"



# ─── review fixes: false passes, attribution, interpreter ─────────────────


@pytest.mark.asyncio
async def test_all_skipped_pytest_run_is_no_tests(tmp_path):
    root = tmp_path / "skips"
    write(root, "pytest.ini", "[pytest]\n")
    write(root, "tests/test_s.py", """
        import pytest


        @pytest.mark.skip(reason="later")
        def test_a():
            pass


        @pytest.mark.skip(reason="later")
        def test_b():
            pass
        """)
    result = await run_framework(Framework("pytest", "python"), root, [], [], timeout=60)
    assert result.exit_code == 0 and result.outcome == "no_tests"
    assert "skipped" in result.note


def test_js_runner_with_exit_zero_and_no_counts_is_no_tests():
    outcome, counts, *_ = classify(Framework("npm", "typescript/javascript", npm_script="test"), 0, "lint-ok\n")
    assert outcome == "no_tests" and counts == {}
    outcome, *_ = classify(Framework("jest", "typescript/javascript"), 0, "Tests:       3 skipped, 3 total\n")
    assert outcome == "no_tests"
    outcome, *_ = classify(Framework("jest", "typescript/javascript"), 0, "Tests:       2 passed, 2 total\n")
    assert outcome == "passed"


def test_unittest_all_skipped_is_no_tests():
    text = "ss\n----------------------------------------------------------------------\nRan 2 tests in 0.000s\n\nOK (skipped=2)\n"
    outcome, counts, _, _ = classify(Framework("unittest", "python"), 0, text)
    assert outcome == "no_tests" and counts["skipped"] == 2


def test_failure_tail_matches_the_exact_test_before_a_prefix():
    from jiuwenswarm.agents.harness.code.verification.runner import parse_pytest

    text = (
        "___________________ test_ab ___________________\n"
        "    assert ab() == 1\nE   AssertionError: from test_ab\n"
        "___________________ test_a ___________________\n"
        "    assert a() == 1\nE   AssertionError: from test_a\n"
        "FAILED tests/test_x.py::test_ab - AssertionError: from test_ab\n"
        "FAILED tests/test_x.py::test_a - AssertionError: from test_a\n"
        "2 failed in 0.01s\n"
    )
    _, failures = parse_pytest(text)
    by_id = {f.test_id.split("::")[-1]: f for f in failures}
    assert any("from test_a" in line and "test_ab" not in line for line in by_id["test_a"].tail)
    assert any("from test_ab" in line for line in by_id["test_ab"].tail)


def _shim(path, tmp_path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def test_interpreter_prefers_project_venvs_then_virtual_env_then_path(tmp_path, monkeypatch):
    from .conftest import REAL_FIND_PYTHON as find_python

    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    # Isolate from the host interpreter (often a checkout `.venv` on PATH).
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    fallback = find_python(root)
    assert fallback is None or ".venv" not in fallback

    active = _shim(tmp_path / "active" / "bin" / "python", tmp_path)
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "active"))
    assert find_python(root) == str(active)

    plain = _shim(root / "venv" / "bin" / "python", tmp_path)
    assert find_python(root) == str(plain)

    dot = _shim(root / ".venv" / "bin" / "python", tmp_path)
    assert find_python(root) == str(dot)

    override = _shim(tmp_path / "custom" / "python3.12", tmp_path)
    assert find_python(root, str(override)) == str(override)
