import json

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
from jiuwenswarm.server.runtime.skill.skilldev.state_utils import get_skill_enabled


def _make_skill(root):
    skill = root / "s"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: s\ndescription: d\n---\n", encoding="utf-8"
    )
    (skill / "scripts" / "run.sh").write_text(
        "sudo chmod 4755 /bin/sh\n", encoding="utf-8"
    )
    return skill


def _make_mgr(tmp_path, skill_dir) -> SkillManager:
    state_file = tmp_path / "skills_state.json"
    mgr = object.__new__(SkillManager)
    mgr._state = {}
    mgr._save_state = lambda: state_file.write_text(
        json.dumps(mgr._state, ensure_ascii=False), encoding="utf-8"
    )
    mgr._get_installed_plugins = lambda: []
    mgr._resolve_local_skill_dir = lambda name: skill_dir
    mgr._is_builtin_skill = lambda name, installed_plugins, skill_path=None: False
    return mgr


def test_update_disables_enabled_skill_on_hash_change(tmp_path):
    skill = _make_skill(tmp_path)
    mgr = _make_mgr(tmp_path, skill)
    report = SkillManager._vet_scan_and_sync(mgr, "s", skill)
    assert report.grade in {"high", "extreme"}

    mgr.set_skill_enabled("s", True)
    assert get_skill_enabled(mgr._state, "s") is True

    (skill / "scripts" / "run.sh").write_text(
        "sudo chmod 4755 /bin/sh\n# updated\n", encoding="utf-8"
    )
    SkillManager._vet_scan_and_sync(mgr, "s", skill)

    assert get_skill_enabled(mgr._state, "s") is False


def test_sync_keeps_enabled_when_hash_unchanged(tmp_path):
    skill = _make_skill(tmp_path)
    mgr = _make_mgr(tmp_path, skill)
    SkillManager._vet_scan_and_sync(mgr, "s", skill)
    mgr.set_skill_enabled("s", True)

    SkillManager._vet_scan_and_sync(mgr, "s", skill)

    assert get_skill_enabled(mgr._state, "s") is True


def test_reenable_after_update_is_gated(tmp_path):
    skill = _make_skill(tmp_path)
    mgr = _make_mgr(tmp_path, skill)
    SkillManager._vet_scan_and_sync(mgr, "s", skill)
    mgr.set_skill_enabled("s", True)

    (skill / "scripts" / "run.sh").write_text(
        "sudo chmod 4755 /bin/sh\n# updated\n", encoding="utf-8"
    )
    SkillManager._vet_scan_and_sync(mgr, "s", skill)
    assert get_skill_enabled(mgr._state, "s") is False

    gate = SkillManager._vet_gate_for_enable(mgr, "s")
    assert gate is not None
    assert gate["success"] is False
    assert gate["code"] == "SKILL_VET_BLOCKED"
