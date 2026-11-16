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
    """辅助函数覆盖：无 frontmatter / 缺 name / 列表与日期型 name / 显式 name.

    判定链须与 ``_parse_skill_md_uncached`` 一致（审查 SF-1）：YAML 解析失败
    回退逐行解析、日期字面量经归一后非空即声明——避免"meta.name 非空却被拒"。
    """
    bare = tmp_path / "barefile"
    bare.mkdir()
    (bare / "SKILL.md").write_text("# 无 frontmatter 正文\n", encoding="utf-8")
    assert SkillManager._skill_md_declares_explicit_name(bare / "SKILL.md") is False

    noname = tmp_path / "noname2"
    _write_skill_md(noname, "description: 缺 name")
    assert SkillManager._skill_md_declares_explicit_name(noname / "SKILL.md") is False

    # 列表型 name 与 meta 链一致：str() 归一后非空 → 视为已声明
    weird = tmp_path / "weird"
    _write_skill_md(weird, "name:\n  - not\n  - a string\ndescription: 列表型 name")
    assert SkillManager._skill_md_declares_explicit_name(weird / "SKILL.md") is True

    # 日期字面量 name：经 _convert_yaml_date 归一为非空字符串 → 已声明
    dated = tmp_path / "dated"
    _write_skill_md(dated, "name: 2026-11-16\ndescription: 日期型 name")
    assert SkillManager._skill_md_declares_explicit_name(dated / "SKILL.md") is True

    named = tmp_path / "named2"
    _write_skill_md(named, "name: ok-name\ndescription: 正常")
    assert SkillManager._skill_md_declares_explicit_name(named / "SKILL.md") is True


@pytest.mark.asyncio
async def test_import_upload_accepts_lenient_yaml_with_explicit_name(
    manager: SkillManager, tmp_path: Path
) -> None:
    """非严格 YAML（未闭合列表 / 值内未加引号的冒号）但逐行可解析出 name 的包
    仍可安装——与 _parse_skill_md_uncached 的行级回退语义对齐（审查 SF-1）。"""
    skill_md = (
        "---\n"
        "name: fallback-skill\n"
        "foo: [1, 2\n"            # 未闭合列表：safe_load 抛异常
        "description: tips: hello\n"  # 值内未加引号的冒号：同样不是严格 YAML
        "---\n"
        "正文。\n"
    )
    zip_path = _build_zip(tmp_path / "lenient.zip", skill_md)

    result = await manager.handle_skills_import_upload({"path": str(zip_path), "overwrite": False})

    assert result["success"] is True
    assert (manager._skills_dir / "fallback-skill" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_import_upload_accepts_date_literal_name(
    manager: SkillManager, tmp_path: Path
) -> None:
    """``name: 2026-11-16``（YAML 日期字面量）经归一为非空字符串，仍可安装——
    与 base 的 meta.name 判定语义一致（审查 PoC 场景二）。"""
    skill_md = "---\nname: 2026-11-16\ndescription: 日期型 name\n---\n正文。\n"
    zip_path = _build_zip(tmp_path / "dated.zip", skill_md)

    result = await manager.handle_skills_import_upload({"path": str(zip_path), "overwrite": False})

    assert result["success"] is True
    assert (manager._skills_dir / "2026-11-16" / "SKILL.md").is_file()
