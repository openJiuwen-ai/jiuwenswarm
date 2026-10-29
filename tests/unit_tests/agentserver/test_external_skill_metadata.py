# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""External skill metadata: display_name fallback and version/skill_type keys.

External skills are read in place from ``skills.external_dirs``. They must still
expose the same metadata shape as local skills so consumers do not see blank
display names or miss ``version`` / ``skill_type``.
"""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


def _write_skill(skill_dir, body: str = "BODY") -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_dir.name}\ndescription: test\n---\n{body}\n",
        encoding="utf-8",
    )


def test_scan_external_skills_sets_display_name_fallback(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    _write_skill(external / "ext-skill")

    manager = SkillManager(workspace_dir=str(workspace))
    manager._external_skill_dirs = [external]

    metas = manager._scan_external_skills()
    ext = next(m for m in metas if m["name"] == "ext-skill")
    assert ext["source"] == "external"
    assert ext["display_name"] == "ext-skill"


@pytest.mark.asyncio
async def test_skills_get_external_fallback_has_version_and_skill_type(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    _write_skill(external / "ext-skill")

    manager = SkillManager(workspace_dir=str(workspace))
    manager._external_skill_dirs = [external]

    detail = await manager.handle_skills_get({"name": "ext-skill"})
    assert detail["source"] == "external"
    assert detail["version"] is None
    assert detail["skill_type"]
