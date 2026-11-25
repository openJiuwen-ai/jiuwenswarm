# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""skill 资产适配器（locate_export / sniff / import）与编排层导出集成单测。

目录均以 monkeypatch 隔离到 tmp_path：本地 skills 目录、内置目录
（扁平与 workswarm-builtin 目录型）互不污染真实用户数据。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.assets import service
from jiuwenswarm.server.runtime.assets.adapters import install_default_adapters
from jiuwenswarm.server.runtime.assets.adapters.skill import SkillAssetAdapter
from jiuwenswarm.server.runtime.assets.contract import AssetKind
from jiuwenswarm.server.runtime.assets.errors import AssetError, AssetErrorCode
from jiuwenswarm.server.runtime.assets.registry import get_asset_adapter

SKILL_MD = """---
name: {name}
description: demo skill for export tests
version: "{version}"
---

# {name}

body
"""


def _make_skill(root: Path, name: str, *, version: str | None = "1.2.0") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    frontmatter = (
        f"---\nname: {name}\ndescription: demo skill for export tests\n"
        + (f'version: "{version}"\n' if version else "")
        + "---\n\nbody\n"
    )
    (skill_dir / "SKILL.md").write_text(frontmatter, encoding="utf-8")
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")
    return skill_dir


@pytest.fixture
def skill_dirs(tmp_path, monkeypatch):
    """隔离的三类安装位置：本地 skills、扁平内置、目录型内置。"""
    local = tmp_path / "skills"
    builtin = tmp_path / "builtin"
    builtin_catalog = builtin / "workswarm-builtin"
    local.mkdir(parents=True)
    builtin_catalog.mkdir(parents=True)

    import jiuwenswarm.server.runtime.assets.adapters.skill as skill_adapter

    monkeypatch.setattr(skill_adapter, "_skills_dir", lambda: local)
    monkeypatch.setattr(skill_adapter, "_builtin_skills_dir", lambda: builtin)
    install_default_adapters()

    from types import SimpleNamespace

    return SimpleNamespace(local=local, builtin=builtin, catalog=builtin_catalog)


def test_adapter_is_registered_for_skill():
    install_default_adapters()
    adapter = get_asset_adapter(AssetKind.SKILL)
    assert isinstance(adapter, SkillAssetAdapter)


def test_locate_export_finds_local_skill_with_version(skill_dirs):
    _make_skill(skill_dirs.local, "web-search")
    source = SkillAssetAdapter().locate_export("web-search", None)
    assert source.source_dir == skill_dirs.local / "web-search"
    assert source.asset_id == "web-search"
    assert source.version == "1.2.0"


def test_locate_export_version_omitted_when_frontmatter_has_none(skill_dirs):
    _make_skill(skill_dirs.local, "plain-skill", version=None)
    source = SkillAssetAdapter().locate_export("plain-skill", None)
    assert source.version is None


def test_locate_export_version_mismatch_rejects(skill_dirs):
    _make_skill(skill_dirs.local, "web-search")
    with pytest.raises(AssetError) as exc:
        SkillAssetAdapter().locate_export("web-search", "9.9.9")
    assert exc.value.code == AssetErrorCode.NOT_FOUND


def test_locate_export_missing_skill_rejects(skill_dirs):
    with pytest.raises(AssetError) as exc:
        SkillAssetAdapter().locate_export("absent", None)
    assert exc.value.code == AssetErrorCode.NOT_FOUND


def test_locate_export_finds_builtin_flat_and_catalog(skill_dirs):
    _make_skill(skill_dirs.builtin, "builtin-flat")
    _make_skill(skill_dirs.catalog, "builtin-cataloged")
    adapter = SkillAssetAdapter()
    assert adapter.locate_export("builtin-flat", None).source_dir == (
        skill_dirs.builtin / "builtin-flat"
    )
    assert adapter.locate_export("builtin-cataloged", None).source_dir == (
        skill_dirs.catalog / "builtin-cataloged"
    )


def test_locate_export_local_shadows_builtin(skill_dirs):
    _make_skill(skill_dirs.builtin, "shadowed")
    local_dir = _make_skill(skill_dirs.local, "shadowed")
    assert SkillAssetAdapter().locate_export("shadowed", None).source_dir == local_dir


def test_locate_export_broken_frontmatter_rejects(skill_dirs):
    broken = skill_dirs.local / "broken"
    broken.mkdir()
    (broken / "SKILL.md").write_text("no frontmatter here\n", encoding="utf-8")
    with pytest.raises(AssetError) as exc:
        SkillAssetAdapter().locate_export("broken", None)
    assert exc.value.code == AssetErrorCode.INVALID_PACKAGE


def test_sniff_package_requires_skill_md(tmp_path):
    adapter = SkillAssetAdapter()
    assert adapter.sniff_package(_make_skill(tmp_path, "sniff-me")) is True
    empty = tmp_path / "empty"
    empty.mkdir()
    assert adapter.sniff_package(empty) is False


def test_import_package_copies_into_local_skills(skill_dirs, tmp_path):
    package = _make_skill(tmp_path / "pkg", "imported-skill")
    outcome = SkillAssetAdapter().import_package(package)
    assert outcome.asset_id == "imported-skill"
    assert (skill_dirs.local / "imported-skill" / "SKILL.md").is_file()
    assert (skill_dirs.local / "imported-skill" / "scripts" / "run.py").is_file()


def test_import_package_recovers_from_partial_leftover(skill_dirs, tmp_path):
    """前次失败导入残留的无 SKILL.md 目录：清理后整体重拷，不与残留混合。"""
    leftover = skill_dirs.local / "imported-skill"
    leftover.mkdir()
    (leftover / "junk.bin").write_bytes(b"partial")
    package = _make_skill(tmp_path / "pkg", "imported-skill")
    outcome = SkillAssetAdapter().import_package(package)
    assert outcome.asset_id == "imported-skill"
    installed = skill_dirs.local / "imported-skill"
    assert (installed / "SKILL.md").is_file()
    assert (installed / "scripts" / "run.py").is_file()
    assert not (installed / "junk.bin").exists()


def test_import_package_rejects_existing_local(skill_dirs, tmp_path):
    _make_skill(skill_dirs.local, "imported-skill")
    package = _make_skill(tmp_path / "pkg", "imported-skill")
    with pytest.raises(AssetError) as exc:
        SkillAssetAdapter().import_package(package)
    assert exc.value.code == AssetErrorCode.ALREADY_EXISTS


def test_import_package_rejects_builtin_name_conflict(skill_dirs, tmp_path):
    _make_skill(skill_dirs.catalog, "builtin-cataloged")
    package = _make_skill(tmp_path / "pkg", "builtin-cataloged")
    with pytest.raises(AssetError) as exc:
        SkillAssetAdapter().import_package(package)
    assert exc.value.code == AssetErrorCode.BUILTIN_READ_ONLY


def test_import_package_rejects_name_directory_mismatch(skill_dirs, tmp_path):
    package = _make_skill(tmp_path / "pkg", "dir-name")
    skill_md = package / "SKILL.md"
    skill_md.write_text(
        "---\nname: other-name\ndescription: mismatch\n---\nbody\n", encoding="utf-8"
    )
    with pytest.raises(AssetError) as exc:
        SkillAssetAdapter().import_package(package)
    assert exc.value.code == AssetErrorCode.INVALID_PACKAGE


def test_export_asset_skill_end_to_end(skill_dirs, monkeypatch, tmp_path):
    """编排层集成：export(skill) 产出单根 zip + 短效下载 URL。"""
    _make_skill(skill_dirs.local, "web-search", version="2.1.0")
    export_root = tmp_path / "asset_exports"
    export_root.mkdir()
    import jiuwenswarm.server.runtime.assets.service as service_module

    monkeypatch.setattr(
        service_module, "assets_export_root", lambda: export_root
    )
    monkeypatch.setattr(service_module, "prune_stale_entries", lambda root: None)
    monkeypatch.setattr(
        service_module,
        "build_file_download_info",
        lambda *a, **k: {"download_url": "/file-api/download?token=stub"},
    )
    result = service.export_asset(
        {"kind": "skill", "local_id": "web-search"}, session_id="s1", user_id="u1"
    )
    assert result["download_url"] == "/file-api/download?token=stub"
    assert result["filename"] == "web-search-2.1.0.zip"
    zips = list(export_root.rglob("web-search-2.1.0.zip"))
    assert len(zips) == 1
    with zipfile.ZipFile(zips[0]) as archive:
        assert sorted(archive.namelist()) == [
            "web-search/SKILL.md",
            "web-search/scripts/run.py",
        ]


def test_export_asset_skill_not_installed(skill_dirs, monkeypatch, tmp_path):
    import jiuwenswarm.server.runtime.assets.service as service_module

    monkeypatch.setattr(
        service_module, "assets_export_root", lambda: tmp_path / "exports"
    )
    monkeypatch.setattr(service_module, "prune_stale_entries", lambda root: None)
    with pytest.raises(AssetError) as exc:
        service.export_asset({"kind": "skill", "local_id": "absent"})
    assert exc.value.code == AssetErrorCode.NOT_FOUND
