# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""send_file looks_like_skill_package 轻量探测契约."""

from __future__ import annotations

import zipfile
from pathlib import Path

from jiuwenswarm.agents.harness.common.tools.send_file_to_user import (
    looks_like_skill_package,
)


def _write_zip(path: Path, entries: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)


def test_skill_extension_is_skill_package(tmp_path: Path) -> None:
    skill = tmp_path / "demo.skill"
    skill.write_bytes(b"PK\x03\x04not-a-real-zip-but-extension-wins")
    assert looks_like_skill_package(skill) is True
    assert looks_like_skill_package(tmp_path / "demo.skill.zip") is True


def test_zip_with_root_skill_md(tmp_path: Path) -> None:
    path = tmp_path / "pack.zip"
    _write_zip(path, {"SKILL.md": "---\nname: demo\n---\n"})
    assert looks_like_skill_package(path) is True


def test_zip_with_nested_one_level_skill_md(tmp_path: Path) -> None:
    path = tmp_path / "nested.zip"
    _write_zip(path, {"demo-skill/SKILL.md": "---\nname: demo-skill\n---\n"})
    assert looks_like_skill_package(path) is True


def test_zip_without_skill_md_is_ordinary(tmp_path: Path) -> None:
    path = tmp_path / "artifact.zip"
    _write_zip(path, {"readme.txt": "hello", "data/out.csv": "a,b\n"})
    assert looks_like_skill_package(path) is False


def test_zip_deep_skill_md_not_treated_as_package(tmp_path: Path) -> None:
    path = tmp_path / "deep.zip"
    _write_zip(path, {"a/b/SKILL.md": "---\nname: deep\n---\n"})
    assert looks_like_skill_package(path) is False


def test_bad_zip_returns_false(tmp_path: Path) -> None:
    path = tmp_path / "broken.zip"
    path.write_bytes(b"not a zip")
    assert looks_like_skill_package(path) is False


def test_non_archive_extension_false(tmp_path: Path) -> None:
    path = tmp_path / "note.md"
    path.write_text("hello", encoding="utf-8")
    assert looks_like_skill_package(path) is False


def test_missing_path_false(tmp_path: Path) -> None:
    assert looks_like_skill_package(tmp_path / "missing.zip") is False
