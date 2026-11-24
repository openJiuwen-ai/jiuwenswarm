"""Builtin catalog capabilities are projected coherently in the skills UI."""

from __future__ import annotations

import json

import pytest

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


def _write_skill(path, name: str, body: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} description\n---\n{body}\n",
        encoding="utf-8",
    )


def _manager_with_catalog(tmp_path) -> SkillManager:
    workspace = tmp_path / "workspace"
    skills = workspace / "skills"
    catalog = skills / "workswarm-builtin"
    _write_skill(catalog, "workswarm-builtin", "CATALOG")
    _write_skill(
        catalog / "references" / "capabilities" / "jiuwen-system-plugin-dev",
        "jiuwen-system-plugin-dev",
        "SYSTEM_PLUGIN_GUIDE",
    )
    (skills / "skills_state.json").write_text(
        json.dumps(
            {
                "installed_plugins": [
                    {"name": "workswarm-builtin", "source": "builtin"}
                ]
            }
        ),
        encoding="utf-8",
    )
    return SkillManager(workspace_dir=str(workspace))


@pytest.mark.asyncio
async def test_catalog_capability_is_listed_as_installed_builtin(tmp_path, monkeypatch):
    manager = _manager_with_catalog(tmp_path)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_builtin_skills_dir",
        lambda: tmp_path / "missing-builtin-source",
    )

    payload = await manager.handle_skills_list({})
    skills = {item["name"]: item for item in payload["skills"]}

    capability = skills["jiuwen-system-plugin-dev"]
    assert capability["installed"] is True
    assert capability["source"] == "builtin"
    assert capability["source_type"] == "builtin"
    assert capability["is_builtin"] is True
    assert capability["catalog_parent"] == "workswarm-builtin"


@pytest.mark.asyncio
async def test_catalog_capability_detail_versions_and_files_use_leaf_directory(tmp_path):
    manager = _manager_with_catalog(tmp_path)

    detail = await manager.handle_skills_get({"name": "jiuwen-system-plugin-dev"})
    versions = await manager.handle_skills_versions_list(
        {"name": "jiuwen-system-plugin-dev"}
    )
    files = await manager.handle_skills_files_list(
        {"name": "jiuwen-system-plugin-dev"}
    )

    assert detail["name"] == "jiuwen-system-plugin-dev"
    assert detail["is_builtin"] is True
    assert detail["source_type"] == "builtin"
    assert "SYSTEM_PLUGIN_GUIDE" in detail["content"]
    assert versions["versions"] == []
    assert any(item["path"] == "SKILL.md" for item in files["files"])


@pytest.mark.asyncio
async def test_top_level_skill_wins_over_same_named_catalog_capability(tmp_path):
    manager = _manager_with_catalog(tmp_path)
    top_level = manager._skills_dir / "jiuwen-system-plugin-dev"
    _write_skill(top_level, "jiuwen-system-plugin-dev", "USER_OVERRIDE")

    payload = await manager.handle_skills_list({})
    matches = [
        item for item in payload["skills"]
        if item["name"] == "jiuwen-system-plugin-dev"
    ]
    detail = await manager.handle_skills_get({"name": "jiuwen-system-plugin-dev"})

    assert len(matches) == 1
    assert matches[0]["is_builtin"] is False
    assert "USER_OVERRIDE" in detail["content"]
