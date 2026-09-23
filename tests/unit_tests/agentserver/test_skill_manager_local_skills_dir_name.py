"""local_skills 登记/清理以目录名为准，避免同 frontmatter name 误删。"""

from __future__ import annotations

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


def _write_skill(skills_dir, folder: str, frontmatter_name: str) -> None:
    skill_dir = skills_dir / folder
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {frontmatter_name}\ndescription: test\nversion: 1.0.0\n---\nbody\n",
        encoding="utf-8",
    )


def test_collect_existing_local_skill_names_uses_directory_name(tmp_path):
    workspace = tmp_path / "workspace"
    skills_dir = workspace / "skills"
    _write_skill(skills_dir, "free-weather-api", "weather")
    _write_skill(skills_dir, "shaojie66-weather", "weather")
    _write_skill(skills_dir, "weather", "weather")

    manager = SkillManager(workspace_dir=str(workspace))
    names = manager._collect_existing_local_skill_names()

    assert names == {"free-weather-api", "shaojie66-weather", "weather"}


def test_normalize_keeps_slug_records_when_frontmatter_name_collides(tmp_path):
    workspace = tmp_path / "workspace"
    skills_dir = workspace / "skills"
    _write_skill(skills_dir, "free-weather-api", "weather")
    _write_skill(skills_dir, "shaojie66-weather", "weather")
    _write_skill(skills_dir, "weather", "weather")

    manager = SkillManager(workspace_dir=str(workspace))
    manager._add_local_skill(
        {
            "name": "weather",
            "origin": "clawhub:steipete/weather",
            "source": "clawhub",
        }
    )
    manager._add_local_skill(
        {
            "name": "free-weather-api",
            "origin": "clawhub:other/free-weather-api",
            "source": "clawhub",
        }
    )
    manager._add_local_skill(
        {
            "name": "shaojie66-weather",
            "origin": "clawhub:shaojie66/shaojie66-weather",
            "source": "clawhub",
        }
    )

    # 模拟卸载 weather/：删目录并去掉对应登记
    import shutil

    shutil.rmtree(skills_dir / "weather")
    manager._remove_local_skill("weather")

    manager._normalize_state(manager._state)
    local = {item["name"]: item for item in manager.get_local_skills()}

    assert "weather" not in local
    assert local["free-weather-api"]["origin"] == "clawhub:other/free-weather-api"
    assert local["shaojie66-weather"]["origin"] == "clawhub:shaojie66/shaojie66-weather"


def test_unmanaged_register_uses_directory_name_not_frontmatter(tmp_path):
    workspace = tmp_path / "workspace"
    skills_dir = workspace / "skills"
    _write_skill(skills_dir, "free-weather-api", "weather")
    _write_skill(skills_dir, "shaojie66-weather", "weather")

    manager = SkillManager(workspace_dir=str(workspace))
    # SkillManager.__init__ 可能已触发登记；再显式跑一遍确认语义
    manager._register_unmanaged_local_skills()

    local = {item["name"]: item for item in manager.get_local_skills()}
    assert set(local) == {"free-weather-api", "shaojie66-weather"}
    assert local["free-weather-api"]["origin"] == "local"
    assert "weather" not in local
