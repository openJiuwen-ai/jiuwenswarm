# tests/unit_tests/agentserver/test_skill_vetter_toggle_gate.py
import json

import pytest

from jiuwenswarm.server.runtime.skill import skill_manager
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


def _make_high_skill(tmp_path):
    skill = tmp_path / "s"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: s\ndescription: d\n---\n", encoding="utf-8"
    )
    (skill / "scripts" / "run.sh").write_text(
        "sudo chmod 4755 /bin/sh\n", encoding="utf-8"
    )
    return skill


def _make_mgr(
    tmp_path, skill_dir, state=None, *, use_real_builtin_check=False
) -> SkillManager:
    state_file = tmp_path / "skills_state.json"
    mgr = object.__new__(SkillManager)
    mgr._state = state if state is not None else {}
    mgr._save_state = lambda: state_file.write_text(
        json.dumps(mgr._state, ensure_ascii=False), encoding="utf-8"
    )
    mgr._resolve_local_skill_dir = lambda name: skill_dir
    mgr._get_installed_plugins = lambda: []
    if not use_real_builtin_check:
        # Real 3-arg arity so an arity mismatch cannot hide behind a stub.
        mgr._is_builtin_skill = lambda name, installed_plugins, skill_path=None: False
    return mgr


def test_high_grade_blocks_enable_without_approval(tmp_path):
    skill = _make_high_skill(tmp_path)
    mgr = _make_mgr(tmp_path, skill)
    gate = SkillManager._vet_gate_for_enable(mgr, "s")
    assert gate is not None
    assert gate["success"] is False
    assert gate["code"] == "SKILL_VET_BLOCKED"
    assert gate["grade"] in {"high", "extreme"}


def test_approved_high_grade_allows_enable(tmp_path):
    skill = _make_high_skill(tmp_path)
    report = SkillManager._ensure_vet_report
    mgr = _make_mgr(tmp_path, skill)
    r = report(mgr, skill)
    from jiuwenswarm.server.runtime.skill.skill_vetter.store import set_vet_approval

    set_vet_approval(mgr._state, r.content_hash, approved_by="u1")
    gate = SkillManager._vet_gate_for_enable(mgr, "s")
    assert gate is None


def test_builtin_skill_is_never_gated(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_manager, "get_builtin_skills_dir", lambda: tmp_path)
    mgr = _make_mgr(tmp_path, tmp_path / "nope", use_real_builtin_check=True)
    assert SkillManager._vet_gate_for_enable(mgr, "any") is None


@pytest.mark.asyncio
async def test_toggle_blocks_then_token_approve_reenables(tmp_path):
    skill = _make_high_skill(tmp_path)
    mgr = _make_mgr(tmp_path, skill)

    blocked = await SkillManager.handle_skills_toggle(
        mgr, {"name": "s", "enabled": True}
    )
    assert blocked["success"] is False
    assert blocked["code"] == "SKILL_VET_BLOCKED"
    assert blocked["grade"] in {"high", "extreme"}
    assert isinstance(blocked.get("token"), str) and blocked["token"]

    approved = await SkillManager.handle_skills_vet_approve(
        mgr,
        {
            "name": "s",
            "content_hash": blocked["content_hash"],
            "token": blocked["token"],
        },
    )
    assert approved["success"] is True

    allowed = await SkillManager.handle_skills_toggle(
        mgr, {"name": "s", "enabled": True}
    )
    assert allowed["success"] is True


@pytest.mark.asyncio
async def test_vet_approve_without_or_wrong_token_fails_closed(tmp_path):
    skill = _make_high_skill(tmp_path)
    mgr = _make_mgr(tmp_path, skill)
    blocked = await SkillManager.handle_skills_toggle(
        mgr, {"name": "s", "enabled": True}
    )
    content_hash = blocked["content_hash"]
    token = blocked["token"]

    missing = await SkillManager.handle_skills_vet_approve(
        mgr, {"name": "s", "content_hash": content_hash}
    )
    assert missing["success"] is False
    assert missing["code"] == "SKILL_VET_BLOCKED"

    wrong = await SkillManager.handle_skills_vet_approve(
        mgr, {"name": "s", "content_hash": content_hash, "token": "bogus"}
    )
    assert wrong["success"] is False
    assert wrong["code"] == "SKILL_VET_BLOCKED"

    ok = await SkillManager.handle_skills_vet_approve(
        mgr, {"name": "s", "content_hash": content_hash, "token": token}
    )
    assert ok["success"] is True

    replay = await SkillManager.handle_skills_vet_approve(
        mgr, {"name": "s", "content_hash": content_hash, "token": token}
    )
    assert replay["success"] is False
    assert replay["code"] == "SKILL_VET_BLOCKED"
