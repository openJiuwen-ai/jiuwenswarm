# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Naming rules shared by document and image upload persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.gateway.upload_storage import (
    safe_session_dirname,
    safe_upload_filename,
    unique_upload_path,
)

_FALLBACK = "document-1.md"


@pytest.mark.parametrize(
    "filename",
    [
        "需求文档.md",
        "Q3财报分析.xlsx",
        "設計書.docx",
        "évaluation.pdf",
        "report_v2.md",
        "设计方案(终版).docx",
    ],
)
def test_safe_upload_filename_keeps_readable_names(filename: str):
    assert safe_upload_filename(filename, fallback=_FALLBACK) == filename


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("..\\..\\evil.md", ".._.._evil.md"),
        ("file:with|bad*chars?.md", "file_with_bad_chars_.md"),
        ("tab\there.md", "tab_here.md"),
        ("trailing .", "trailing"),
    ],
)
def test_safe_upload_filename_strips_unsafe_parts(filename: str, expected: str):
    assert safe_upload_filename(filename, fallback=_FALLBACK) == expected


def test_safe_upload_filename_result_is_a_single_path_component():
    for filename in ("../../etc/passwd", "a/b/c.md", "..\\..\\evil.md"):
        result = safe_upload_filename(filename, fallback=_FALLBACK)
        assert Path(result).name == result
        assert "/" not in result


@pytest.mark.parametrize("filename", ["", "   ", ".", "..", "CON.md", "nul", "COM1.txt"])
def test_safe_upload_filename_falls_back_on_unusable_names(filename: str):
    assert safe_upload_filename(filename, fallback=_FALLBACK) == _FALLBACK


def test_safe_upload_filename_strips_bidi_override():
    # U+202E would render "evil<RLO>gnp.md" as "evil.md" in a file picker.
    assert safe_upload_filename("evil‮gnp.md", fallback=_FALLBACK) == "evilgnp.md"


def test_safe_upload_filename_truncates_on_a_byte_budget():
    result = safe_upload_filename("中" * 100 + ".md", fallback=_FALLBACK)

    assert len(result.encode("utf-8")) <= 180
    assert result.endswith(".md")
    assert result.startswith("中")


def test_safe_upload_filename_truncation_keeps_valid_utf8():
    result = safe_upload_filename("漢" * 200 + ".pdf", fallback=_FALLBACK)

    # Round-trips without replacement chars: no split multi-byte sequence.
    assert result.encode("utf-8").decode("utf-8") == result


def test_safe_session_dirname_keeps_ascii_allowlist():
    assert safe_session_dirname("web_19fcd201464_0101e859669a") == "web_19fcd201464_0101e859669a"
    assert safe_session_dirname("a/../b") == "a_.._b"
    assert safe_session_dirname(None) == "default"
    assert safe_session_dirname("   ") == "default"


def test_unique_upload_path_suffixes_existing_files(tmp_path: Path):
    target = tmp_path / "需求文档.md"
    target.write_text("first", encoding="utf-8")

    assert unique_upload_path(target) == tmp_path / "需求文档-1.md"

    (tmp_path / "需求文档-1.md").write_text("second", encoding="utf-8")
    assert unique_upload_path(target) == tmp_path / "需求文档-2.md"


def test_unique_upload_path_returns_free_path_unchanged(tmp_path: Path):
    target = tmp_path / "报告.md"

    assert unique_upload_path(target) == target
