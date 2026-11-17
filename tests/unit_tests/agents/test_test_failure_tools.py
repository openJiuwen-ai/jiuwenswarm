# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest
from openjiuwen.core.common.exception.errors import ValidationError

from jiuwenswarm.agents.harness.common.tools.test_failure_tools import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_FAILURES,
    _clean_terminal_output,
    _normalize_request,
    parse_test_failure,
)


def test_parse_test_failure_schema_exposes_top_level_params() -> None:
    assert parse_test_failure.card.name == "parse_test_failure"
    schema = parse_test_failure.card.input_params
    properties = schema["properties"]

    assert set(properties.keys()) == {
        "raw_output",
        "framework",
        "max_failures",
        "max_chars",
    }
    assert "inputs" not in properties
    assert schema["required"] == ["raw_output"]
    assert properties["framework"]["default"] == "auto"
    assert properties["max_failures"]["default"] == DEFAULT_MAX_FAILURES
    assert properties["max_chars"]["default"] == DEFAULT_MAX_CHARS


def test_normalize_request_defaults_validation_and_clamping() -> None:
    req = _normalize_request({"raw_output": "FAILED tests/test_a.py::test_one"})
    assert req.framework == "auto"
    assert req.max_failures == DEFAULT_MAX_FAILURES
    assert req.max_chars == DEFAULT_MAX_CHARS

    clamped = _normalize_request(
        {
            "raw_output": "  some trace  ",
            "framework": "  PyTest ",
            "max_failures": 0,
            "max_chars": 999_999,
        }
    )
    assert clamped.raw_output == "  some trace  "
    assert clamped.framework == "pytest"
    assert clamped.max_failures == 1
    assert clamped.max_chars == 100_000

    clamped_hi = _normalize_request(
        {
            "raw_output": "err",
            "framework": "UNITTEST",
            "max_failures": 500,
            "max_chars": 50,
        }
    )
    assert clamped_hi.framework == "unittest"
    assert clamped_hi.max_failures == 100
    assert clamped_hi.max_chars == 1_000

    for bad_inputs in (
        {},
        {"raw_output": None},
        {"raw_output": 123},
        {"raw_output": "   \n\t  "},
        {"raw_output": "err", "framework": "mocha"},
        {"raw_output": "err", "framework": 42},
        {"raw_output": "err", "max_failures": True},
        {"raw_output": "err", "max_chars": False},
    ):
        with pytest.raises(ValueError):
            _normalize_request(bad_inputs)


def test_clean_terminal_output_strips_ansi_and_resolves_cr() -> None:
    raw = (
        "\x1b]8;;file:///C:/repo/test_a.py\x07\x1b[31mFAILED\x1b[0m\x1b]8;;\x07\r\n"
        "\x1b(B10% running\r\x1b[2K100% done\r\n"
        "\x1b[33mAssertionError: bad value\x1b[0m\n"
    )
    cleaned = _clean_terminal_output(raw)
    assert "\x1b" not in cleaned
    assert "\r" not in cleaned
    assert "10% running" not in cleaned
    assert "100% done" in cleaned
    assert "FAILED" in cleaned
    assert "AssertionError: bad value" in cleaned

    with pytest.raises(ValueError, match="no readable text"):
        _clean_terminal_output("\x1b[31m\x1b[0m\r\n   ")


@pytest.mark.asyncio
async def test_pytest_parses_parametrized_windows_paths_and_assertions() -> None:
    raw = (
        "\x1b[1m=== test session starts ===\x1b[0m\n"
        "platform win32 -- Python 3.12.1, pytest-8.2.0\n"
        "tests\\test_math.py .F [100%]\n\n"
        "================================== FAILURES ===================================\n"
        "_________________________ TestCalc.test_add[case-1] ___________________________\n\n"
        "self = <test_math.TestCalc object at 0x01>\n\n"
        "    def test_add(self):\n"
        ">       assert compute(1, 2) == 4\n"
        "E       assert 3 == 4\n"
        "E        +  where 3 = compute(1, 2)\n\n"
        "C:\\repo\\tests\\test_math.py:27: AssertionError\n"
        "=========================== short test summary info ===========================\n"
        "FAILED C:\\repo\\tests\\test_math.py::TestCalc::test_add[case 1] - assert 3 == 4\n"
        "========================= 1 failed, 1 passed in 0.14s =========================\n"
    )
    # Use matching space/hyphen parametrization so nodeid upgrades cleanly.
    raw = raw.replace("test_add[case 1]", "test_add[case-1]")
    result = await parse_test_failure.invoke({"raw_output": raw})

    assert "Framework: pytest" in result
    assert "Status: failed" in result
    assert "1 failed, 1 passed in 0.14s" in result
    assert "C:\\repo\\tests\\test_math.py::TestCalc::test_add[case-1]" in result
    assert "Location: C:\\repo\\tests\\test_math.py:27" in result
    assert "Code: assert compute(1, 2) == 4" in result
    assert "Exception: AssertionError: assert 3 == 4" in result


@pytest.mark.asyncio
async def test_pytest_parses_setup_teardown_and_collection_errors() -> None:
    raw = (
        "=================================== ERRORS ====================================\n"
        "_______________________ ERROR at setup of test_db_query _______________________\n\n"
        "    @pytest.fixture\n"
        "    def db():\n"
        ">       raise RuntimeError('db down')\n"
        "E       RuntimeError: db down\n\n"
        "tests/conftest.py:12: RuntimeError\n"
        "_____________________ ERROR at teardown of test_cleanup _______________________\n\n"
        "tests/conftest.py:22: in db\n"
        "    close_conn()\n"
        "E   OSError: handle closed\n"
        "_____________________ ERROR collecting tests/test_broken.py _____________________\n"
        "tests/test_broken.py:3: in <module>\n"
        "    import nonexistent_mod\n"
        "E   ModuleNotFoundError: No module named 'nonexistent_mod'\n"
        "=========================== short test summary info ===========================\n"
        "ERROR tests/test_db.py::test_db_query - RuntimeError: db down\n"
        "ERROR tests/test_db.py::test_cleanup - OSError: handle closed\n"
        "ERROR tests/test_broken.py - ModuleNotFoundError: No module named 'nonexistent_mod'\n"
        "============================== 3 errors in 0.08s ==============================\n"
    )
    result = await parse_test_failure.invoke({"raw_output": raw})

    assert "Status: failed" in result
    assert "ERROR (setup): tests/test_db.py::test_db_query" in result
    assert "Exception: RuntimeError: db down" in result
    assert "ERROR (teardown): tests/test_db.py::test_cleanup" in result
    assert "Exception: OSError: handle closed" in result
    assert "ERROR (collection): tests/test_broken.py" in result
    assert "ModuleNotFoundError: No module named 'nonexistent_mod'" in result


@pytest.mark.asyncio
async def test_pytest_filters_live_logs_captured_logs_and_warnings() -> None:
    raw = (
        "============================= test session starts =============================\n"
        "platform linux -- Python 3.12.0, pytest-8.0.0\n"
        "rootdir: /workspace\n"
        "tests/test_app.py F [100%]\n\n"
        "================================== FAILURES ===================================\n"
        "_________________________________ test_login __________________________________\n\n"
        "tests/test_app.py:45: in test_login\n"
        "    raise ValueError('bad token')\n"
        "E   ValueError: bad token\n"
        "------------------------------ Captured stdout call ---------------------------\n"
        "noisy debug print 1\n"
        "noisy debug print 2\n"
        "------------------------------ live log teardown ------------------------------\n"
        "INFO root:app.py:99 shutting down noisy pool\n"
        "============================== warnings summary ===============================\n"
        "tests/test_app.py:10: DeprecationWarning: noisy warning\n"
        "=========================== short test summary info ===========================\n"
        "FAILED tests/test_app.py::test_login - ValueError: bad token\n"
        "======================== 1 failed, 1 warning in 0.22s =========================\n"
    )
    result = await parse_test_failure.invoke({"raw_output": raw})

    assert "Noise filtered:" in result
    assert "noisy debug print" not in result
    assert "shutting down noisy pool" not in result
    assert "DeprecationWarning: noisy warning" not in result
    assert "FAILED (call): tests/test_app.py::test_login" in result
    assert "Exception: ValueError: bad token" in result


@pytest.mark.asyncio
async def test_pytest_tb_native_and_line_styles() -> None:
    native_raw = (
        "================================== FAILURES ===================================\n"
        "_________________________________ test_native _________________________________\n"
        "Traceback (most recent call last):\n"
        '  File "tests/test_native.py", line 18, in test_native\n'
        "    do_work()\n"
        "    ^^^^^^^\n"
        '  File "src/worker.py", line 9, in do_work\n'
        "    raise KeyError('missing')\n"
        "KeyError: 'missing'\n"
        "============================== 1 failed in 0.05s ==============================\n"
    )
    native_res = await parse_test_failure.invoke({"raw_output": native_raw})
    assert "Location: src/worker.py:9 in do_work" in native_res
    assert "Code: raise KeyError('missing')" in native_res
    assert "Exception: KeyError: 'missing'" in native_res

    line_raw = (
        "================================== FAILURES ===================================\n"
        "tests/test_line.py:31: ZeroDivisionError: division by zero\n"
        "=========================== short test summary info ===========================\n"
        "FAILED tests/test_line.py::test_div - ZeroDivisionError: division by zero\n"
        "============================== 1 failed in 0.02s ==============================\n"
    )
    line_res = await parse_test_failure.invoke({"raw_output": line_raw})
    assert "FAILED (call): tests/test_line.py::test_div" in line_res
    assert "Location: tests/test_line.py:31" in line_res
    assert "Exception: ZeroDivisionError: division by zero" in line_res


@pytest.mark.asyncio
async def test_unittest_parses_fail_error_subtest_docstring_and_carets() -> None:
    raw = (
        "======================================================================\n"
        "FAIL: test_eq (tests.test_u.TestUnit.test_eq)\n"
        "Check equality helper.\n"
        "----------------------------------------------------------------------\n"
        "Traceback (most recent call last):\n"
        '  File "tests/test_u.py", line 15, in test_eq\n'
        "    self.assertEqual(calc(1), 2)\n"
        "    ~~~~~~~~~~~~~~~~^^^^^^^^^^^^\n"
        "AssertionError: 1 != 2\n\n"
        "======================================================================\n"
        "ERROR: test_parse (tests.test_u.TestUnit.test_parse) [case=2]\n"
        "----------------------------------------------------------------------\n"
        "Traceback (most recent call last):\n"
        '  File "tests/test_u.py", line 28, in test_parse\n'
        "    parse_item(bad)\n"
        "ValueError: invalid payload\n"
        "  detail line two\n\n"
        "----------------------------------------------------------------------\n"
        "Ran 5 tests in 0.012s\n\n"
        "FAILED (failures=1, errors=1, skipped=1)\n"
    )
    result = await parse_test_failure.invoke({"raw_output": raw})

    assert "Framework: unittest" in result
    assert "Status: failed" in result
    assert "Ran 5 tests in 0.012s | FAILED (failures=1, errors=1, skipped=1)" in result
    assert "FAILED (call): test_eq (tests.test_u.TestUnit.test_eq)" in result
    assert "Location: tests/test_u.py:15 in test_eq" in result
    assert "Code: self.assertEqual(calc(1), 2)" in result
    assert "Exception: AssertionError: 1 != 2" in result
    assert "ERROR (call): test_parse (tests.test_u.TestUnit.test_parse) [case=2]" in result
    assert "detail line two" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("framework_expected", "raw"),
    [
        (
            "pytest",
            "tests/test_ok.py ... [100%]\n"
            "============================== 3 passed in 0.03s ==============================\n",
        ),
        (
            "unittest",
            "...\n"
            "----------------------------------------------------------------------\n"
            "Ran 3 tests in 0.004s\n\n"
            "OK (skipped=1)\n",
        ),
    ],
)
async def test_all_passing_outputs_pytest_and_unittest(
    framework_expected: str,
    raw: str,
) -> None:
    result = await parse_test_failure.invoke({"raw_output": raw})
    assert f"Framework: {framework_expected}" in result
    assert "Status: passed" in result
    assert "No test failures or errors found." in result


@pytest.mark.asyncio
async def test_truncated_incomplete_output_and_raw_traceback_fallback() -> None:
    partial_pytest = (
        "================================== FAILURES ===================================\n"
        "_________________________________ test_partial ________________________________\n"
        "tests/test_p.py:10: in test_partial\n"
        "    assert False\n"
        "E   AssertionError: assert False\n"
    )
    res_pytest = await parse_test_failure.invoke({"raw_output": partial_pytest})
    assert "Framework: pytest" in res_pytest
    assert "Status: incomplete" in res_pytest
    assert "test_partial" in res_pytest

    partial_unittest = (
        "======================================================================\n"
        "ERROR: test_cut (tests.test_cut.T.test_cut)\n"
        "----------------------------------------------------------------------\n"
        "Traceback (most recent call last):\n"
        '  File "tests/test_cut.py", line 7, in test_cut\n'
        "    1 / 0\n"
        "ZeroDivisionError: division by zero\n"
    )
    res_unittest = await parse_test_failure.invoke({"raw_output": partial_unittest})
    assert "Framework: unittest" in res_unittest
    assert "Status: incomplete" in res_unittest
    assert "ZeroDivisionError: division by zero" in res_unittest

    raw_tb = (
        "Traceback (most recent call last):\n"
        '  File "app/runner.py", line 42, in main\n'
        "    connect()\n"
        "TimeoutError: timed out\n"
    )
    res_unknown = await parse_test_failure.invoke({"raw_output": raw_tb})
    assert "Framework: unknown" in res_unknown
    assert "Status: incomplete" in res_unknown
    assert "Location: app/runner.py:42 in main" in res_unknown
    assert "Exception: TimeoutError: timed out" in res_unknown


@pytest.mark.asyncio
async def test_max_failures_and_max_chars_caps_and_disclosures() -> None:
    blocks = []
    for idx in range(5):
        blocks.append(
            f"_________________________________ test_fail_{idx} _________________________________\n"
            f"tests/test_many.py:{idx + 10}: in test_fail_{idx}\n"
            f"    assert {idx} == 99  # " + ("verbose " * 40) + "\n"
            f"E   AssertionError: item {idx} failed\n"
        )
    raw = (
        "================================== FAILURES ===================================\n"
        + "\n".join(blocks)
        + "============================== 5 failed in 0.25s ==============================\n"
    )

    capped_failures = await parse_test_failure.invoke(
        {"raw_output": raw, "max_failures": 2}
    )
    assert "test_fail_0" in capped_failures
    assert "test_fail_1" in capped_failures
    assert "test_fail_2" not in capped_failures
    assert "[Showing 2 of 5 failures" in capped_failures

    capped_chars = await parse_test_failure.invoke(
        {"raw_output": raw, "max_chars": 1000}
    )
    assert len(capped_chars) <= 1000
    assert "[Truncated at max_chars=1000]" in capped_chars


@pytest.mark.asyncio
async def test_parse_test_failure_invoke_error_paths_and_extra_kwargs() -> None:
    empty_res = await parse_test_failure.invoke({"raw_output": "   \n  "})
    assert empty_res.startswith("[ERROR]: parse_test_failure failed:")

    bad_fw_res = await parse_test_failure.invoke(
        {"raw_output": "FAILED test_a.py::test_x", "framework": "rspec"}
    )
    assert bad_fw_res.startswith("[ERROR]: parse_test_failure failed:")
    assert "Unsupported framework" in bad_fw_res

    with pytest.raises(ValidationError, match="raw_output"):
        await parse_test_failure.invoke({})

    with pytest.raises(ValidationError, match="unexpected_extra"):
        await parse_test_failure.invoke(
            {
                "raw_output": "=== 1 passed in 0.01s ===",
                "unexpected_extra": "ignored",
            }
        )

    ok_res = await parse_test_failure.invoke(
        {"raw_output": "=== 1 passed in 0.01s ==="}
    )
    assert "Status: passed" in ok_res
