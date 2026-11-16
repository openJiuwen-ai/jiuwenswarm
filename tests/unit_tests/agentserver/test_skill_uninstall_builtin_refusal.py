# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""skills.uninstall 对未装入用户目录的内置技能的拒绝语义（回归 #6690）."""

from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


def _write_skill_dir(root: Path, *, folder: str, name: str, body: str = "# Body") -> Path:
    skill_dir = root / folder
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: demo\n---\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


@pytest.mark.asyncio
async def test_uninstall_builtin_not_installed_reports_builtin_refusal(
    manager: SkillManager,
) -> None:
    """未装入用户目录的内置技能（skills.list 中 is_builtin=true 卡片）卸载应返回
    "内置技能不允许删除"，而非误导性的"未找到 skill"（#6690）。"""
    builtin_root = manager._skills_dir.parent / "builtin_missing"
    _write_skill_dir(builtin_root, folder="advanced-daily-report", name="advanced-daily-report")

    result = await manager.handle_skills_uninstall({"name": "advanced-daily-report"})

    assert result == {"success": False, "detail": "内置技能不允许删除"}


@pytest.mark.asyncio
async def test_uninstall_builtin_indirect_name_match_also_refused(
    manager: SkillManager,
) -> None:
    """目录名与 SKILL.md name 不一致时也应命中内置拒绝。"""
    builtin_root = manager._skills_dir.parent / "builtin_missing"
    _write_skill_dir(builtin_root, folder="daily-report-pack", name="advanced-daily-report")

    result = await manager.handle_skills_uninstall({"name": "advanced-daily-report"})

    assert result == {"success": False, "detail": "内置技能不允许删除"}


@pytest.mark.asyncio
async def test_uninstall_unknown_skill_keeps_missing_detail(
    manager: SkillManager,
) -> None:
    """非内置且未安装的技能保持原有"未找到 skill"文案。"""
    result = await manager.handle_skills_uninstall({"name": "never-installed"})

    assert result == {"success": False, "detail": "未找到 skill: never-installed"}


@pytest.mark.asyncio
async def test_uninstall_user_copy_of_builtin_named_skill_still_succeeds(
    manager: SkillManager,
) -> None:
    """同名内置技能的用户目录安装副本仍可卸载（个人版既有契约不变）。

    builtin 与用户目录各有一份同名技能：用户副本存在时 dest 解析成功，
    不落入"未找到/内置拒绝"分支，删除用户副本后 builtin 原目录保留。
    """
    builtin_root = manager._skills_dir.parent / "builtin_missing"
    _write_skill_dir(builtin_root, folder="docx-pro", name="docx-pro")
    _write_skill_dir(manager._skills_dir, folder="docx-pro", name="docx-pro")
    manager._add_local_skill({"name": "docx-pro", "source": "local"})

    result = await manager.handle_skills_uninstall({"name": "docx-pro"})

    assert result["success"] is True
    assert not (manager._skills_dir / "docx-pro").exists()
    assert (builtin_root / "docx-pro" / "SKILL.md").is_file()
