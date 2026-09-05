"""Tests for skill_display.md parsing and builtin UI whitelist."""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


def _make_skill_dir(skills_dir, name, *, description="English description", display=None):
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n",
        encoding="utf-8",
    )
    if display is not None:
        lines = ["---"]
        for key, value in display.items():
            lines.append(f"{key}: {value}")
        lines.append("---\n")
        (skill_dir / "skill_display.md").write_text("\n".join(lines), encoding="utf-8")
    return skill_dir


def _init_manager(monkeypatch, skills_dir, builtin_dir):
    skills_dir.mkdir(parents=True, exist_ok=True)
    builtin_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_agent_skills_dir",
        lambda: skills_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skilldev.state_utils.get_agent_skills_dir",
        lambda: skills_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_builtin_skills_dir",
        lambda: builtin_dir,
    )
    return SkillManager()


def test_apply_skill_display_from_file_overrides_ui_fields(tmp_path):
    skill_dir = _make_skill_dir(
        tmp_path,
        "demo-skill",
        description="Trigger description",
        display={
            "display_name_zh": "演示技能",
            "description_zh": "中文描述",
            "display_name_en": "Demo Skill",
            "description_en": "English UI description",
        },
    )
    meta = SkillManager._parse_skill_md(skill_dir / "SKILL.md")
    assert meta is not None
    SkillManager._apply_skill_display(meta, skill_dir)

    assert meta["display_name_zh"] == "演示技能"
    assert meta["description_zh"] == "中文描述"
    assert meta["display_name_en"] == "Demo Skill"
    assert meta["description_en"] == "English UI description"
    assert meta["display_name"] == "演示技能"
    assert meta["description"] == "中文描述"


def test_apply_skill_display_fallback_without_file(tmp_path):
    skill_dir = _make_skill_dir(
        tmp_path,
        "fallback-skill",
        description="From SKILL.md",
    )
    meta = SkillManager._parse_skill_md(skill_dir / "SKILL.md")
    assert meta is not None
    SkillManager._apply_skill_display(meta, skill_dir)

    assert meta["display_name_zh"] == "fallback-skill"
    assert meta["description_zh"] == "From SKILL.md"
    assert meta["display_name_en"] == ""
    assert meta["description_en"] == ""
    # No skill_display.md → do not overwrite response display_name/description
    assert meta.get("display_name") is None or meta.get("display_name") != "fallback-skill"
    assert meta["description"] == "From SKILL.md"


def test_apply_skill_display_falls_back_to_builtin_source(monkeypatch, tmp_path):
    """Older local installs without skill_display.md should reuse builtin display."""
    skills_dir = tmp_path / "skills"
    builtin_dir = tmp_path / "builtin"
    local = _make_skill_dir(
        skills_dir,
        "skill-creator",
        description="Create new skills in English.",
    )
    _make_skill_dir(
        builtin_dir,
        "skill-creator",
        description="Create new skills in English.",
        display={
            "display_name_zh": "Skill Creator",
            "description_zh": "创建、修改、优化和评估单智能体技能。",
            "display_name_en": "Skill Creator",
            "description_en": "Create new skills in English.",
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_builtin_skills_dir",
        lambda: builtin_dir,
    )

    meta = SkillManager._parse_skill_md(local / "SKILL.md")
    assert meta is not None
    SkillManager._apply_skill_display(meta, local)

    assert meta["description_zh"] == "创建、修改、优化和评估单智能体技能。"
    assert meta["description"] == "创建、修改、优化和评估单智能体技能。"
    assert meta["display_name"] == "Skill Creator"


def test_scan_local_skills_includes_display_fields(monkeypatch, tmp_path):
    skills_dir = tmp_path / "skills"
    builtin_dir = tmp_path / "builtin"
    _make_skill_dir(
        skills_dir,
        "local-demo",
        description="Trigger",
        display={
            "display_name_zh": "本地演示",
            "description_zh": "本地中文描述",
            "display_name_en": "Local Demo",
            "description_en": "Local English description",
        },
    )
    manager = _init_manager(monkeypatch, skills_dir, builtin_dir)

    listed = manager._scan_local_skills()
    skill = next(s for s in listed if s["name"] == "local-demo")
    assert skill["display_name_zh"] == "本地演示"
    assert skill["description_zh"] == "本地中文描述"
    assert skill["display_name"] == "本地演示"
    assert skill["description"] == "本地中文描述"


@pytest.mark.asyncio
async def test_skills_get_returns_display_fields(monkeypatch, tmp_path):
    skills_dir = tmp_path / "skills"
    builtin_dir = tmp_path / "builtin"
    _make_skill_dir(
        skills_dir,
        "detail-demo",
        description="Trigger",
        display={
            "display_name_zh": "详情演示",
            "description_zh": "详情中文描述",
            "display_name_en": "Detail Demo",
            "description_en": "Detail English description",
        },
    )
    manager = _init_manager(monkeypatch, skills_dir, builtin_dir)

    detail = await manager.handle_skills_get({"name": "detail-demo"})
    assert detail["display_name_zh"] == "详情演示"
    assert detail["description_zh"] == "详情中文描述"
    assert detail["display_name"] == "详情演示"
    assert detail["description"] == "详情中文描述"


def test_builtin_scan_whitelist_filters_non_listed(monkeypatch, tmp_path):
    skills_dir = tmp_path / "skills"
    builtin_dir = tmp_path / "builtin"
    _make_skill_dir(builtin_dir, "skill-creator", description="Creator")
    _make_skill_dir(builtin_dir, "hidden-builtin", description="Should be hidden")

    manager = _init_manager(monkeypatch, skills_dir, builtin_dir)
    names = {s["name"] for s in manager._scan_builtin_skills()}

    assert "skill-creator" in names
    assert "hidden-builtin" not in names


def test_try_find_skill_file_skips_skill_display(tmp_path):
    skill_dir = tmp_path / "only-display"
    skill_dir.mkdir()
    (skill_dir / "skill_display.md").write_text(
        "---\ndisplay_name_zh: X\n---\n",
        encoding="utf-8",
    )
    assert SkillManager._try_find_skill_file(skill_dir) is None

    (skill_dir / "SKILL.md").write_text(
        "---\nname: only-display\ndescription: d\n---\n",
        encoding="utf-8",
    )
    assert SkillManager._try_find_skill_file(skill_dir).name == "SKILL.md"
