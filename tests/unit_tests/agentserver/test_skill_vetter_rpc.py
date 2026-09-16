import json
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


def _make_mgr(tmp_path, skill_dir) -> SkillManager:
    state_file = tmp_path / "skills_state.json"
    mgr = object.__new__(SkillManager)
    mgr._state = {}
    mgr._save_state = lambda: state_file.write_text(
        json.dumps(mgr._state, ensure_ascii=False), encoding="utf-8"
    )
    mgr._resolve_local_skill_dir = lambda name: skill_dir if name == "s" else None
    mgr._is_builtin_skill = lambda name: False
    return mgr


@pytest.fixture
def skill_dir(tmp_path) -> Path:
    skill = tmp_path / "s"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n", encoding="utf-8")
    return skill


@pytest.mark.asyncio
async def test_handle_skills_vet_returns_grade(skill_dir, tmp_path):
    mgr = _make_mgr(tmp_path, skill_dir)
    out = await SkillManager.handle_skills_vet(mgr, {"name": "s"})
    assert out["success"] is True
    assert "grade" in out
    assert "findings" in out


@pytest.mark.asyncio
async def test_handle_skills_vet_missing_name(skill_dir, tmp_path):
    mgr = _make_mgr(tmp_path, skill_dir)
    out = await SkillManager.handle_skills_vet(mgr, {"name": ""})
    assert out["success"] is False


@pytest.mark.asyncio
async def test_handle_skills_vet_approve_records_approval(skill_dir, tmp_path):
    mgr = _make_mgr(tmp_path, skill_dir)
    report = mgr._ensure_vet_report(skill_dir)
    out = await SkillManager.handle_skills_vet_approve(
        mgr, {"name": "s", "content_hash": report.content_hash}
    )
    assert out["success"] is True
    from jiuwenswarm.server.runtime.skill.skill_vetter.store import get_vet_approval
    assert get_vet_approval(mgr._state, report.content_hash) is not None
