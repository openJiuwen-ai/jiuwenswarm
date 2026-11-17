# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""内置技能名单按目录 mtime 缓存；默认工作目录存在时不再反复 mkdir。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jiuwenswarm.common import utils


@pytest.fixture(name="skills_dir")
def _skills_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "skills"
    (root / "alpha").mkdir(parents=True)
    (root / "readme.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(utils, "get_builtin_skills_dir", lambda: root)
    utils._BUILTIN_SKILL_NAMES_CACHE.clear()
    yield root
    utils._BUILTIN_SKILL_NAMES_CACHE.clear()


def test_builtin_skill_names_scan_once(skills_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    real_iterdir = Path.iterdir

    def _counting(self):
        calls["n"] += 1
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", _counting)
    assert utils.get_builtin_skill_names() == {"alpha"}
    names = utils.get_builtin_skill_names()
    names.add("mutated")
    assert utils.get_builtin_skill_names() == {"alpha"}
    assert calls["n"] == 1


def test_builtin_skill_names_rescan_when_directory_changes(skills_dir: Path) -> None:
    assert utils.get_builtin_skill_names() == {"alpha"}
    (skills_dir / "beta").mkdir()
    # 同一时钟刻度内的增删可能不改变目录 mtime，这里显式推进以验证失效逻辑。
    stat = skills_dir.stat()
    os.utime(skills_dir, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert utils.get_builtin_skill_names() == {"alpha", "beta"}


def test_builtin_skill_names_missing_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(utils, "get_builtin_skills_dir", lambda: tmp_path / "absent")
    assert utils.get_builtin_skill_names() == set()


def test_builtin_skills_dir_follows_primary_layout_presence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(utils, "_find_package_root", lambda: tmp_path)
    fallback = tmp_path / "resources" / "agent" / "skills"
    (fallback / "foo").mkdir(parents=True)
    primary = tmp_path / "resources" / "agent" / "workspace" / "skills"
    (primary / "bar").mkdir(parents=True)
    utils._BUILTIN_SKILL_NAMES_CACHE.clear()
    assert utils.get_builtin_skills_dir() == primary
    assert utils.get_builtin_skill_names() == {"bar"}
    (primary / "bar").rmdir()
    primary.rmdir()
    assert utils.get_builtin_skills_dir() == fallback
    assert utils.get_builtin_skill_names() == {"foo"}
    utils._BUILTIN_SKILL_NAMES_CACHE.clear()


def test_ensure_dir_creates_once_and_keeps_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "projects" / "s1"
    utils._ensure_dir(target)
    assert target.is_dir()

    def _fail(*_args, **_kwargs):
        raise AssertionError("mkdir should not run for an existing directory")

    monkeypatch.setattr(Path, "mkdir", _fail)
    utils._ensure_dir(target)


def test_ensure_dir_rejects_file_like_mkdir(tmp_path: Path) -> None:
    target = tmp_path / "occupied"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(FileExistsError):
        utils._ensure_dir(target)
