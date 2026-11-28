# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Workspace listing resilience and bounded UTF-8 text previews."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.skill.skill_files import (
    ERROR_NOT_FOUND,
    SkillFilesError,
    list_skill_workspace_files,
    read_text_preview,
)


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {type(exc).__name__}")


@pytest.mark.parametrize("exists", [True, False])
def test_external_symlink_does_not_break_listing(tmp_path, exists):
    root = tmp_path / "skill"
    root.mkdir()
    (root / "SKILL.md").write_text("demo", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    if exists:
        outside.write_text("outside", encoding="utf-8")
    _symlink(root / "external.txt", outside)
    entries = list_skill_workspace_files(root)
    assert [entry["path"] for entry in entries] == ["SKILL.md"]


def test_listing_is_sorted_and_hides_archive(tmp_path):
    (tmp_path / "z.txt").write_text("z", encoding="utf-8")
    (tmp_path / "A.txt").write_text("a", encoding="utf-8")
    archive = tmp_path / ".archive/versions"
    archive.mkdir(parents=True)
    (archive / "hidden.json").write_text("{}", encoding="utf-8")
    assert [entry["path"] for entry in list_skill_workspace_files(tmp_path)] == ["A.txt", "z.txt"]


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_text_preview_has_no_bom(tmp_path, encoding):
    path = tmp_path / "SKILL.md"
    path.write_text("示例 Skill\n", encoding=encoding, newline="\n")
    assert read_text_preview(path) == "示例 Skill\n"


def test_preview_enforces_limit_even_when_stat_is_stale(tmp_path, monkeypatch):
    path = tmp_path / "growing.log"
    path.write_bytes(b"abcdef")
    monkeypatch.setattr(Path, "stat", lambda *args, **kwargs: SimpleNamespace(st_size=1))
    assert read_text_preview(path, max_bytes=3) is None


def test_preview_accepts_exact_byte_limit(tmp_path):
    path = tmp_path / "exact.txt"
    path.write_bytes(b"abc")
    assert read_text_preview(path, max_bytes=3) == "abc"


@pytest.mark.parametrize("data", [b"x\x00y", b"\xff\xfe"])
def test_binary_and_invalid_utf8_are_not_previewed(tmp_path, data):
    path = tmp_path / "binary.txt"
    path.write_bytes(data)
    assert read_text_preview(path) is None


def test_negative_preview_limit_is_rejected(tmp_path):
    path = tmp_path / "test.txt"
    path.write_text("text", encoding="utf-8")
    with pytest.raises(ValueError, match="max_bytes"):
        read_text_preview(path, max_bytes=-1)


def test_missing_file_keeps_business_error(tmp_path):
    with pytest.raises(SkillFilesError) as error:
        read_text_preview(tmp_path / "missing.md")
    assert error.value.code == ERROR_NOT_FOUND


def test_preview_limit_counts_bom_bytes(tmp_path):
    path = tmp_path / "bom.md"
    path.write_bytes(b"\xef\xbb\xbfabc")
    assert read_text_preview(path, max_bytes=3) is None
    assert read_text_preview(path, max_bytes=6) == "abc"


def test_zero_byte_limit_accepts_only_empty_file(tmp_path):
    path = tmp_path / "empty.md"
    path.write_bytes(b"")
    assert read_text_preview(path, max_bytes=0) == ""
    path.write_bytes(b"x")
    assert read_text_preview(path, max_bytes=0) is None
