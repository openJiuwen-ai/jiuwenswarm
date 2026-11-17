# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for code search and pattern matching tool (grep_code)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from jiuwenswarm.agents.harness.common.tools.grep_tools import (
    _compile_pattern,
    _format_grep_results,
    _is_binary_file,
    grep_code,
)


def test_compile_pattern() -> None:
    """Test pattern compilation for regex and literal strings."""
    pat_literal = _compile_pattern("foo.bar", is_regex=False, case_sensitive=False)
    assert pat_literal.search("FOO.BAR")
    assert not pat_literal.search("foo-bar")

    pat_regex = _compile_pattern(r"def\s+\w+", is_regex=True, case_sensitive=True)
    assert pat_regex.search("def calculate_total():")
    assert not pat_regex.search("DEF calculate_total():")


def test_is_binary_file(tmp_path: Path) -> None:
    """Test binary file detection."""
    text_file = tmp_path / "normal.txt"
    text_file.write_text("Hello plain text file\nSecond line", encoding="utf-8")
    assert not _is_binary_file(text_file)

    binary_file = tmp_path / "sample.bin"
    binary_file.write_bytes(b"\x00\x01\x02\x03\x04")
    assert _is_binary_file(binary_file)


def test_format_grep_results_empty() -> None:
    """Test formatting when no matches are found."""
    res = _format_grep_results(
        [], "my_query", total_files_scanned=5, truncated=False, max_matches=50
    )
    assert "No matches found" in res
    assert "across 5 files" in res


@pytest.mark.asyncio
async def test_grep_code_literal_search(tmp_path: Path) -> None:
    """Test basic literal code search."""
    src_file = tmp_path / "sample.py"
    src_file.write_text(
        "import sys\n\ndef run_task():\n    return 42\n",
        encoding="utf-8",
    )

    with patch("openjiuwen.core.sys_operation.cwd.get_workspace", return_value=str(tmp_path)):
        result = await grep_code.invoke({
            "query": "run_task",
            "path": ".",
        })
    assert "Found 1 match" in result
    assert "sample.py" in result
    assert "def run_task():" in result


@pytest.mark.asyncio
async def test_grep_code_regex_and_context(tmp_path: Path) -> None:
    """Test regex pattern search with context lines."""
    src_file = tmp_path / "module.py"
    src_file.write_text(
        "line 1\nline 2\nTARGET_MATCH\nline 4\nline 5\n",
        encoding="utf-8",
    )

    with patch("openjiuwen.core.sys_operation.cwd.get_workspace", return_value=str(tmp_path)):
        result = await grep_code.invoke({
            "query": r"TARGET_\w+",
            "path": ".",
            "is_regex": True,
            "context_lines": 1,
        })
    assert "Found 1 match" in result
    assert "line 2" in result
    assert "TARGET_MATCH" in result
    assert "line 4" in result


@pytest.mark.asyncio
async def test_grep_code_file_pattern_filter(tmp_path: Path) -> None:
    """Test filtering by file glob pattern."""
    py_file = tmp_path / "a.py"
    py_file.write_text("common_keyword", encoding="utf-8")
    js_file = tmp_path / "b.js"
    js_file.write_text("common_keyword", encoding="utf-8")

    with patch("openjiuwen.core.sys_operation.cwd.get_workspace", return_value=str(tmp_path)):
        result = await grep_code.invoke({
            "query": "common_keyword",
            "path": ".",
            "file_pattern": "*.py",
        })
    assert "a.py" in result
    assert "b.js" not in result


@pytest.mark.asyncio
async def test_grep_code_skips_ignored_dirs(tmp_path: Path) -> None:
    """Test that ignored directories (.git, node_modules) are skipped."""
    valid_dir = tmp_path / "src"
    valid_dir.mkdir()
    (valid_dir / "app.py").write_text("secret_code = 1", encoding="utf-8")

    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("secret_code = 1", encoding="utf-8")

    with patch("openjiuwen.core.sys_operation.cwd.get_workspace", return_value=str(tmp_path)):
        result = await grep_code.invoke({
            "query": "secret_code",
            "path": ".",
        })
    assert "app.py" in result
    assert ".git" not in result


@pytest.mark.asyncio
async def test_grep_code_truncation(tmp_path: Path) -> None:
    """Test max matches limit truncation."""
    data_file = tmp_path / "data.txt"
    lines = [f"item_{i} = True\n" for i in range(10)]
    data_file.write_text("".join(lines), encoding="utf-8")

    with patch("openjiuwen.core.sys_operation.cwd.get_workspace", return_value=str(tmp_path)):
        result = await grep_code.invoke({
            "query": "item_",
            "path": ".",
            "max_matches": 3,
        })
    assert "Matches truncated at limit of 3" in result


@pytest.mark.asyncio
async def test_grep_code_exact_limit_not_truncated(tmp_path: Path) -> None:
    """Verify that reaching exactly max_matches does not flag truncation."""
    data_file = tmp_path / "exact.txt"
    lines = [f"key_{i} = True\n" for i in range(3)]
    data_file.write_text("".join(lines), encoding="utf-8")

    with patch("openjiuwen.core.sys_operation.cwd.get_workspace", return_value=str(tmp_path)):
        result = await grep_code.invoke({
            "query": "key_",
            "path": ".",
            "max_matches": 3,
        })
    assert "Found 3 match(es)" in result
    assert "Matches truncated" not in result


@pytest.mark.asyncio
async def test_grep_code_workspace_escape_rejected(tmp_path: Path) -> None:
    """Verify that paths outside workspace are strictly rejected."""
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()

    with patch("openjiuwen.core.sys_operation.cwd.get_workspace", return_value=str(ws_dir)):
        result = await grep_code.invoke({
            "query": "test",
            "path": str(outside_dir),
        })
    assert "[ERROR]" in result
    assert "Search path must be inside workspace" in result


@pytest.mark.asyncio
async def test_grep_code_oversized_file_skipped(tmp_path: Path) -> None:
    """Verify that oversized files exceeding limit are skipped."""
    sample_file = tmp_path / "large.py"
    sample_file.write_text("target_token = 1\n", encoding="utf-8")

    with patch("openjiuwen.core.sys_operation.cwd.get_workspace", return_value=str(tmp_path)):
        with patch(
            "jiuwenswarm.agents.harness.common.tools.grep_tools._MAX_FILE_SIZE_BYTES",
            5,
        ):
            result = await grep_code.invoke({
                "query": "target_token",
                "path": ".",
            })
    assert "No matches found" in result


@pytest.mark.asyncio
async def test_grep_code_invalid_regex(tmp_path: Path) -> None:
    """Test invalid regex error handling."""
    with patch("openjiuwen.core.sys_operation.cwd.get_workspace", return_value=str(tmp_path)):
        result = await grep_code.invoke({
            "query": "[unclosed bracket",
            "path": ".",
            "is_regex": True,
        })
    assert "[ERROR]" in result
    assert "Invalid regular expression" in result


@pytest.mark.asyncio
async def test_grep_code_empty_query(tmp_path: Path) -> None:
    """Test empty query validation."""
    with patch("openjiuwen.core.sys_operation.cwd.get_workspace", return_value=str(tmp_path)):
        result = await grep_code.invoke({
            "query": "",
            "path": ".",
        })
    assert "[ERROR]" in result
    assert "cannot be empty" in result
