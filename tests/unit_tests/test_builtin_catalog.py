"""Exercise catalog installation, real discovery boundaries and legacy migration."""

import json
import asyncio
import re
import shutil
import hashlib
from pathlib import Path

from jiuwenswarm.common import utils
from jiuwenswarm.common.builtin_skills import (
    BUILTIN_CATALOG_NAME,
    migrate_builtin_catalog,
    skill_tree_digest,
    with_builtin_catalog,
)


def _workspace(tmp_path):
    builtin = tmp_path / "builtin"
    seed = builtin / BUILTIN_CATALOG_NAME
    leaf = seed / "references" / "capabilities" / "sample"
    leaf.mkdir(parents=True)
    (seed / "SKILL.md").write_text("---\nname: workswarm-builtin\ndescription: catalog\n---\n", encoding="utf-8")
    (leaf / "SKILL.md").write_text("---\nname: sample\ndescription: sample\n---\n", encoding="utf-8")
    (seed / "legacy_hashes.json").write_text(json.dumps({"sample": skill_tree_digest(leaf)}), encoding="utf-8")
    skills = tmp_path / "skills"
    shutil.copytree(seed, skills / BUILTIN_CATALOG_NAME)
    shutil.copytree(leaf, skills / "sample")
    state = {"installed_plugins": [{"name": "sample", "source": "builtin"}], "skill_configs": {"sample": {"enabled": False}}}
    (skills / "skills_state.json").write_text(json.dumps(state), encoding="utf-8")
    return builtin, skills


def test_migration_archives_unchanged_skills_and_preserves_disabled_choice(tmp_path):
    builtin, skills = _workspace(tmp_path)
    original = (skills / "sample" / "SKILL.md").read_bytes()
    migrate_builtin_catalog(builtin, skills)
    migrate_builtin_catalog(builtin, skills)
    assert not (skills / "sample").exists()
    assert (skills / BUILTIN_CATALOG_NAME / ".legacy" / "sample" / "SKILL.md").read_bytes() == original
    state = json.loads((skills / "skills_state.json").read_text())
    assert state["installed_plugins"] == []
    assert state["builtin_catalog_migrated"] == ["sample"]
    assert state["builtin_catalog_disabled"] == ["sample"]
    assert "`sample`" in (skills / BUILTIN_CATALOG_NAME / "references" / "migration.md").read_text(encoding="utf-8")
    assert with_builtin_catalog(["sample"], skills) == [BUILTIN_CATALOG_NAME, "sample"]


def test_migration_preserves_user_edits_and_same_named_local_skills(tmp_path):
    builtin, skills = _workspace(tmp_path)
    page = skills / "sample" / "SKILL.md"
    page.write_text("user customization", encoding="utf-8")
    migrate_builtin_catalog(builtin, skills)
    assert page.read_text() == "user customization"
    assert with_builtin_catalog(["sample"], skills) == ["sample"]
    state = json.loads((skills / "skills_state.json").read_text())
    assert state["installed_plugins"][0]["name"] == "sample"
    shutil.copyfile(builtin / BUILTIN_CATALOG_NAME / "references" / "capabilities" / "sample" / "SKILL.md", page)
    state["local_skills"] = [{"name": "sample", "source": "local"}]
    (skills / "skills_state.json").write_text(json.dumps(state))
    migrate_builtin_catalog(builtin, skills)
    assert page.is_file()


def test_migration_recovers_after_move_before_state_commit(tmp_path):
    builtin, skills = _workspace(tmp_path)
    archive = skills / BUILTIN_CATALOG_NAME / ".legacy" / "sample"
    archive.parent.mkdir()
    (skills / "sample").rename(archive)
    migrate_builtin_catalog(builtin, skills)
    assert json.loads((skills / "skills_state.json").read_text())["builtin_catalog_migrated"] == ["sample"]


def test_tree_digest_ignores_checkout_line_endings_and_detects_new_resources(tmp_path):
    page = tmp_path / "SKILL.md"
    page.write_bytes(b"a\nb\n")
    expected = skill_tree_digest(tmp_path)
    page.write_bytes(b"a\r\nb\r\n")
    assert skill_tree_digest(tmp_path) == expected
    (tmp_path / "user-note.txt").write_text("keep")
    assert skill_tree_digest(tmp_path) != expected


def test_known_historical_release_and_reinstalled_copy_are_both_archived(tmp_path):
    builtin, skills = _workspace(tmp_path)
    migrate_builtin_catalog(builtin, skills)
    source = skills / "sample"
    shutil.copytree(builtin / BUILTIN_CATALOG_NAME / "references/capabilities/sample", source)
    (source / "SKILL.md").write_text("trusted older release", encoding="utf-8")
    digest = skill_tree_digest(source)
    versions = {"sample": [{"revision": "verified-test-release", "digest": digest}]}
    (builtin / BUILTIN_CATALOG_NAME / "legacy_versions.json").write_text(json.dumps(versions))
    state_file = skills / "skills_state.json"
    state = json.loads(state_file.read_text())
    state["installed_plugins"] = [{"name": "sample", "source": "builtin"}]
    state_file.write_text(json.dumps(state))
    migrate_builtin_catalog(builtin, skills)
    migrate_builtin_catalog(builtin, skills)
    assert not source.exists()
    archives = list((skills / BUILTIN_CATALOG_NAME / ".legacy").glob("sample*"))
    assert len(archives) == 2
    assert any((a / "SKILL.md").read_text() == "trusted older release" for a in archives)
    assert json.loads(state_file.read_text())["installed_plugins"] == []


def test_catalog_navigation_updates_known_release_and_preserves_user_edit(tmp_path):
    builtin, skills = _workspace(tmp_path)
    catalog = skills / BUILTIN_CATALOG_NAME
    previous = (catalog / "SKILL.md").read_bytes()
    digest = hashlib.sha256(previous.replace(b"\r\n", b"\n")).hexdigest()
    versions = {"_catalog_files": {"SKILL.md": [{"digest": digest}]}}
    (builtin / BUILTIN_CATALOG_NAME / "legacy_versions.json").write_text(json.dumps(versions))
    seed = builtin / BUILTIN_CATALOG_NAME / "SKILL.md"
    seed.write_text("App Market navigation", encoding="utf-8")
    migrate_builtin_catalog(builtin, skills)
    assert (catalog / "SKILL.md").read_text() == "App Market navigation"
    assert list((catalog / ".legacy/catalog-files").iterdir())[0].read_bytes() == previous
    (catalog / "SKILL.md").write_text("my custom navigation", encoding="utf-8")
    seed.write_text("updated packaged navigation", encoding="utf-8")
    migrate_builtin_catalog(builtin, skills)
    assert (catalog / "SKILL.md").read_text() == "my custom navigation"


def test_skill_list_reconciles_stale_state_and_separates_customized_builtin(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.skill import skill_manager

    builtin, skills = _workspace(tmp_path)
    state_file = skills / "skills_state.json"
    state = json.loads(state_file.read_text())
    state["installed_plugins"].append({"name": BUILTIN_CATALOG_NAME, "source": "builtin"})
    state_file.write_text(json.dumps(state))
    monkeypatch.setattr(skill_manager, "get_builtin_skills_dir", lambda: builtin)
    monkeypatch.setattr(skill_manager, "get_agent_skills_dir", lambda: skills)
    manager = skill_manager.SkillManager(workspace_dir=str(tmp_path))
    manager._skills_dir = skills
    manager._state_file = state_file
    manager.reload_state()
    migrate_builtin_catalog(builtin, skills)
    # Simulate an old installer bringing back an identical top-level copy.
    shutil.copytree(builtin / BUILTIN_CATALOG_NAME / "references/capabilities/sample", skills / "sample")
    state_file.write_text(json.dumps(state))
    listed = asyncio.run(manager.handle_skills_list({"with_installed": True}))
    assert [s["name"] for s in listed["skills"] if s["source"] == "builtin"] == [BUILTIN_CATALOG_NAME]
    assert not (skills / "sample").exists()
    # Unknown modifications remain active assets, but are no longer builtins.
    (skills / "sample").mkdir()
    (skills / "sample/SKILL.md").write_text("---\nname: sample\ndescription: custom\n---\nmy custom rules")
    state_file.write_text(json.dumps(state))
    listed = asyncio.run(manager.handle_skills_list({}))
    customized = next(s for s in listed["skills"] if s["name"] == "sample")
    assert customized["source"] == "local"
    assert "my custom rules" in (skills / "sample/SKILL.md").read_text()


def test_shipped_catalog_has_one_discovery_entry_and_all_leaf_links_work(tmp_path, monkeypatch):
    from openjiuwen.harness.rails import SkillUseRail
    from openjiuwen.symphony.discovery import scan_skill_directories

    source = utils.get_builtin_skills_dir()
    skills = tmp_path / "skills"
    monkeypatch.setattr(utils, "get_agent_skills_dir", lambda: skills)
    utils.ensure_default_builtin_skills()
    utils.ensure_default_builtin_skills()
    assert [p.name for p in skills.iterdir() if p.is_dir()] == [BUILTIN_CATALOG_NAME]
    assert len(scan_skill_directories([skills]).items) == 1
    assert [p.name for p, _ in SkillUseRail._discover_skill_dirs([skills])] == [BUILTIN_CATALOG_NAME]
    root = skills / BUILTIN_CATALOG_NAME
    catalog = root / "references" / "catalog.md"
    links = re.findall(r"\]\((capabilities/[^)]+/SKILL\.md)\)", catalog.read_text(encoding="utf-8"))
    assert len(links) == len(json.loads((source / BUILTIN_CATALOG_NAME / "legacy_hashes.json").read_text())) == 35
    assert all((catalog.parent / link).is_file() for link in links)
    assert (root / "references" / "capabilities" / "workswarm-app-builder" / "scripts" / "publish.py").is_file()


def test_failed_seed_copy_does_not_hide_legacy_install(tmp_path, monkeypatch):
    builtin, skills = _workspace(tmp_path)
    # Simulate a missing/unusable catalog before the first successful install.
    catalog = skills / BUILTIN_CATALOG_NAME
    hidden = skills / ".interrupted"
    catalog.rename(hidden)
    monkeypatch.setattr(utils, "get_builtin_skills_dir", lambda: builtin)
    monkeypatch.setattr(utils, "get_agent_skills_dir", lambda: skills)
    def fail_copy(*args, **kwargs):
        raise OSError("copy failed")
    monkeypatch.setattr(utils.shutil, "copytree", fail_copy)
    utils.ensure_default_builtin_skills()
    assert (skills / "sample" / "SKILL.md").is_file()
    assert not catalog.exists()
    assert not json.loads((skills / "skills_state.json").read_text()).get("builtin_catalog_migrated")


def test_catalog_tool_exposes_prompt_then_reads_leaf_on_demand():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from openjiuwen.core.single_agent.skills.skill_manager import Skill
    from openjiuwen.core.sys_operation.sys_operation import SysOperation
    from openjiuwen.harness.tools import SkillTool

    root = utils.get_builtin_skills_dir() / BUILTIN_CATALOG_NAME
    reads = []
    async def read_file(path):
        reads.append(Path(path).relative_to(root).as_posix())
        return SimpleNamespace(code=0, data=SimpleNamespace(content=Path(path).read_text(encoding="utf-8")))
    operation = Mock(spec=SysOperation)
    operation.fs.return_value.read_file = read_file
    tool = SkillTool(operation=operation, get_skills=lambda: [Skill(name=BUILTIN_CATALOG_NAME, directory=root)])
    async def scenario():
        parent = await tool.invoke({"skill_name": BUILTIN_CATALOG_NAME})
        assert parent.success
        rendered = parent.data["content"]
        assert "code 模式" in rendered and "不构成应用发布授权" in rendered
        assert "persist_session:false" not in rendered
        assert reads == ["SKILL.md"]
        leaf = await tool.invoke({"skill_name": BUILTIN_CATALOG_NAME, "relative_file_path": "references/capabilities/workswarm-app-builder/SKILL.md"})
        assert leaf.success and "persist_session:false" in leaf.data["content"]
        assert len(reads) == 2
    asyncio.run(scenario())
