# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""skills.import_upload 对 SKILL.md 缺显式 name 的拒绝（回归 #6688）."""

import zipfile
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.skill.skill_manager import (
    ERROR_SKILL_INVALID_METADATA,
    SkillManager,
    SkillRpcError,
)


def _write_skill_md(skill_dir: Path, frontmatter: str, body: str = "正文。") -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\n{frontmatter}\n---\n{body}\n",
        encoding="utf-8",
    )


def _build_zip(target: Path, skill_md_text: str) -> Path:
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("SKILL.md", skill_md_text)
    return target


@pytest.mark.asyncio
async def test_import_upload_rejects_missing_name(manager: SkillManager, tmp_path: Path) -> None:
    """SKILL.md frontmatter 只有 description、缺 name：导入应返回
    SKILL_INVALID_METADATA（而非静默兜底 name="SKILL" 安装成功，#6688）。"""
    zip_path = _build_zip(
        tmp_path / "noname.zip",
        "---\ndescription: 元数据缺 name 的技能\n---\n正文。\n",
    )

    with pytest.raises(SkillRpcError) as exc_info:
        await manager.handle_skills_import_upload({"path": str(zip_path), "overwrite": False})

    assert exc_info.value.code == ERROR_SKILL_INVALID_METADATA
    assert "name" in str(exc_info.value)
    assert not (manager._skills_dir / "SKILL").exists()


@pytest.mark.asyncio
async def test_import_upload_rejects_blank_name(manager: SkillManager, tmp_path: Path) -> None:
    """name 显式为空白串：同样视为未声明，拒绝导入。"""
    zip_path = _build_zip(
        tmp_path / "blankname.zip",
        "---\nname: ''\ndescription: 空白 name 的技能\n---\n正文。\n",
    )

    with pytest.raises(SkillRpcError) as exc_info:
        await manager.handle_skills_import_upload({"path": str(zip_path), "overwrite": False})

    assert exc_info.value.code == ERROR_SKILL_INVALID_METADATA


@pytest.mark.asyncio
async def test_import_upload_accepts_explicit_name(manager: SkillManager, tmp_path: Path) -> None:
    """带显式 name + description 的包正常导入（正向对照）。"""
    zip_path = _build_zip(
        tmp_path / "ok.zip",
        "---\nname: named-skill\ndescription: 正常技能\n---\n正文。\n",
    )

    result = await manager.handle_skills_import_upload({"path": str(zip_path), "overwrite": False})

    assert result["success"] is True
    assert (manager._skills_dir / "named-skill" / "SKILL.md").is_file()


def test_assert_package_safe_distinguishes_fallback_name(manager: SkillManager, tmp_path: Path) -> None:
    """单元级对照：缺 name 的 frontmatter 拒绝；显式 name 通过。"""
    noname = tmp_path / "noname"
    _write_skill_md(noname, "description: 缺 name")
    with pytest.raises(SkillRpcError) as exc_info:
        manager._assert_skill_package_safe(noname, source_trusted=False, allow_skillpack=False)
    assert exc_info.value.code == ERROR_SKILL_INVALID_METADATA

    named = tmp_path / "named"
    _write_skill_md(named, "name: named\ndescription: 有 name")
    meta = manager._assert_skill_package_safe(named, source_trusted=False, allow_skillpack=False)
    assert meta["name"] == "named"


def test_skill_md_declares_explicit_name_edge_cases(tmp_path: Path) -> None:
    """辅助函数覆盖：无 frontmatter / 缺 name / 非字符串 name / 显式 name。"""
    bare = tmp_path / "barefile"
    bare.mkdir()
    (bare / "SKILL.md").write_text("# 无 frontmatter 正文\n", encoding="utf-8")
    assert SkillManager._skill_md_declares_explicit_name(bare / "SKILL.md") is False

    noname = tmp_path / "noname2"
    _write_skill_md(noname, "description: 缺 name")
    assert SkillManager._skill_md_declares_explicit_name(noname / "SKILL.md") is False

    weird = tmp_path / "weird"
    _write_skill_md(weird, "name:\n  - not\n  - a string\ndescription: 列表型 name")
    assert SkillManager._skill_md_declares_explicit_name(weird / "SKILL.md") is False

    named = tmp_path / "named2"
    _write_skill_md(named, "name: ok-name\ndescription: 正常")
    assert SkillManager._skill_md_declares_explicit_name(named / "SKILL.md") is True
