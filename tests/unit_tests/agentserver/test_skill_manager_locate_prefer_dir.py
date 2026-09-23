"""skills.get / files 定位：优先目录名，再回退 frontmatter name。"""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


def _write_skill(skills_dir, folder: str, frontmatter_name: str, body: str) -> None:
    skill_dir = skills_dir / folder
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {frontmatter_name}\ndescription: test\nversion: 1.0.0\n---\n{body}\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_skills_get_prefers_directory_name_over_shared_frontmatter(tmp_path):
    workspace = tmp_path / "workspace"
    skills_dir = workspace / "skills"
    _write_skill(skills_dir, "free-weather-api", "weather", "FREE_WEATHER_BODY")
    _write_skill(skills_dir, "weather", "weather", "WEATHER_DIR_BODY")
    _write_skill(skills_dir, "shaojie66-weather", "weather", "SHAOJIE_BODY")

    manager = SkillManager(workspace_dir=str(workspace))
    detail = await manager.handle_skills_get({"name": "weather"})

    assert detail["name"] == "weather"
    assert "WEATHER_DIR_BODY" in detail["content"]
    assert "FREE_WEATHER_BODY" not in detail["content"]


@pytest.mark.asyncio
async def test_skills_files_list_prefers_directory_name_over_shared_frontmatter(tmp_path):
    workspace = tmp_path / "workspace"
    skills_dir = workspace / "skills"
    _write_skill(skills_dir, "free-weather-api", "weather", "FREE_WEATHER_BODY")
    _write_skill(skills_dir, "weather", "weather", "WEATHER_DIR_BODY")

    manager = SkillManager(workspace_dir=str(workspace))
    listed = await manager.handle_skills_files_list({"name": "weather"})

    assert listed["name"] == "weather"
    weather_dir = skills_dir / "weather"
    assert weather_dir.is_dir()
    # 文件树应来自 weather/，而非 free-weather-api/
    assert (weather_dir / "SKILL.md").read_text(encoding="utf-8").find("WEATHER_DIR_BODY") >= 0


@pytest.mark.asyncio
async def test_skills_get_falls_back_to_frontmatter_when_directory_missing(tmp_path):
    workspace = tmp_path / "workspace"
    skills_dir = workspace / "skills"
    _write_skill(skills_dir, "skill-creator-normal", "skill-creator", "ALIAS_BODY")

    manager = SkillManager(workspace_dir=str(workspace))
    detail = await manager.handle_skills_get({"name": "skill-creator"})

    assert detail["name"] == "skill-creator-normal"
    assert "ALIAS_BODY" in detail["content"]


def test_resolve_local_skill_dir_prefers_directory_name(tmp_path):
    workspace = tmp_path / "workspace"
    skills_dir = workspace / "skills"
    _write_skill(skills_dir, "free-weather-api", "weather", "FREE")
    _write_skill(skills_dir, "weather", "weather", "DIRECT")

    manager = SkillManager(workspace_dir=str(workspace))
    resolved = manager._resolve_local_skill_dir("weather")

    assert resolved is not None
    assert resolved.name == "weather"
