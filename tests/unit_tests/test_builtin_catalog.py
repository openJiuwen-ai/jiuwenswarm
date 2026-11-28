"""Exercise catalog installation, real discovery boundaries and legacy migration."""

import json
import asyncio
import re
import shutil
import hashlib
import os
from pathlib import Path

import pytest

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


def _resource_path(path):
    if os.name == "nt" and not str(path).startswith("\\\\?\\"):
        return Path("\\\\?\\" + str(path.resolve()))
    return path


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


def test_catalog_capability_updates_known_release_and_preserves_user_edit(tmp_path):
    builtin, skills = _workspace(tmp_path)
    catalog = skills / BUILTIN_CATALOG_NAME
    target = catalog / "references/capabilities/sample"
    previous_digest = skill_tree_digest(target)
    versions = {"sample": [{"digest": previous_digest, "revision": "previous"}]}
    (builtin / BUILTIN_CATALOG_NAME / "legacy_versions.json").write_text(
        json.dumps(versions),
        encoding="utf-8",
    )
    source = builtin / BUILTIN_CATALOG_NAME / "references/capabilities/sample/SKILL.md"
    source.write_text(
        "---\nname: sample\ndescription: updated\n---\nnew rules\n",
        encoding="utf-8",
    )

    migrate_builtin_catalog(builtin, skills)

    assert target.joinpath("SKILL.md").read_bytes() == source.read_bytes()
    backups = list((catalog / ".legacy/catalog-capabilities").glob("sample-*"))
    assert len(backups) == 1
    assert skill_tree_digest(backups[0]) == previous_digest

    target.joinpath("SKILL.md").write_text("user customization\n", encoding="utf-8")
    source.write_text(
        "---\nname: sample\ndescription: newer\n---\nnewer rules\n",
        encoding="utf-8",
    )
    migrate_builtin_catalog(builtin, skills)
    assert target.joinpath("SKILL.md").read_text(encoding="utf-8") == "user customization\n"


def test_unreadable_catalog_capability_does_not_abort_legacy_migration(tmp_path, monkeypatch):
    from jiuwenswarm.common import builtin_skills

    builtin, skills = _workspace(tmp_path)
    catalog = skills / BUILTIN_CATALOG_NAME
    target = catalog / "references/capabilities/sample"
    previous_digest = skill_tree_digest(target)
    (builtin / BUILTIN_CATALOG_NAME / "legacy_versions.json").write_text(
        json.dumps({"sample": [{"digest": previous_digest, "revision": "previous"}]}),
        encoding="utf-8",
    )
    source = builtin / BUILTIN_CATALOG_NAME / "references/capabilities/sample/SKILL.md"
    source.write_text("---\nname: sample\ndescription: updated\n---\nnew rules\n", encoding="utf-8")
    original_digest = builtin_skills.skill_tree_digest

    def digest_with_unreadable_target(directory):
        if Path(directory).resolve() == target.resolve():
            raise OSError("unreadable capability")
        return original_digest(directory)

    monkeypatch.setattr(builtin_skills, "skill_tree_digest", digest_with_unreadable_target)

    migrate_builtin_catalog(builtin, skills)

    assert target.joinpath("SKILL.md").read_text(encoding="utf-8").endswith("sample\n---\n")
    assert not (skills / "sample").exists()
    state = json.loads((skills / "skills_state.json").read_text(encoding="utf-8"))
    assert "sample" in state["builtin_catalog_migrated"]


@pytest.mark.parametrize("customized", [None, "SKILL.md", "references/ai-roles.md"])
def test_app_builder_upgrade_installs_guides_before_refreshing_trusted_instructions(tmp_path, customized):
    builtin, skills = _workspace(tmp_path)
    catalog = skills / BUILTIN_CATALOG_NAME
    relative = Path("references/capabilities/workswarm-app-builder")
    source = builtin / BUILTIN_CATALOG_NAME / relative
    target = catalog / relative
    for leaf in (source, target):
        (leaf / "references").mkdir(parents=True)
    previous = b"old packaged app builder instructions\n"
    (target / "SKILL.md").write_bytes(previous)
    versions = {"_catalog_files": {f"{relative.as_posix()}/SKILL.md": [
        {"digest": hashlib.sha256(previous).hexdigest()},
    ]}}
    (builtin / BUILTIN_CATALOG_NAME / "legacy_versions.json").write_text(json.dumps(versions), encoding="utf-8")
    replacement = b"instructions linking both new guides\n"
    (source / "SKILL.md").write_bytes(replacement)
    for guide in ("single-npc.md", "ai-roles.md"):
        (source / "references" / guide).write_text(guide, encoding="utf-8")
    if customized:
        (target / customized).write_text("custom content", encoding="utf-8")

    migrate_builtin_catalog(builtin, skills)
    migrate_builtin_catalog(builtin, skills)

    if customized:
        assert (target / customized).read_text(encoding="utf-8") == "custom content"
        assert not (target / "references/single-npc.md").exists()
    else:
        assert (target / "SKILL.md").read_bytes() == replacement
        for guide in ("single-npc.md", "ai-roles.md"):
            assert (target / "references" / guide).read_text(encoding="utf-8") == guide
        archived, = (catalog / ".legacy/catalog-files" / relative).glob("SKILL.md-*")
        assert archived.read_bytes() == previous


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
    assert [s["name"] for s in listed["skills"] if s["source"] == "builtin"] == [
        BUILTIN_CATALOG_NAME,
    ]
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

    source = _resource_path(utils.get_builtin_skills_dir())
    skills = _resource_path(tmp_path / "skills")
    monkeypatch.setattr(utils, "get_builtin_skills_dir", lambda: source)
    monkeypatch.setattr(utils, "get_agent_skills_dir", lambda: skills)
    utils.ensure_default_builtin_skills()
    utils.ensure_default_builtin_skills()
    assert [p.name for p in skills.iterdir() if p.is_dir()] == [BUILTIN_CATALOG_NAME]
    assert len(scan_skill_directories([skills]).items) == 1
    assert [p.name for p, _ in SkillUseRail._discover_skill_dirs([skills])] == [BUILTIN_CATALOG_NAME]
    root = skills / BUILTIN_CATALOG_NAME
    catalog = root / "references" / "catalog.md"
    links = re.findall(r"\]\((capabilities/[^)]+/SKILL\.md)\)", catalog.read_text(encoding="utf-8"))
    legacy_names = set(json.loads((source / BUILTIN_CATALOG_NAME / "legacy_hashes.json").read_text()))
    linked_names = {Path(link).parts[1] for link in links}
    shipped_names = {
        path.parent.name
        for path in (source / BUILTIN_CATALOG_NAME / "references/capabilities").glob("*/SKILL.md")
    }
    assert linked_names == shipped_names
    assert legacy_names <= linked_names
    assert all((catalog.parent / link).is_file() for link in links)
    assert "capabilities/jiuwen-system-plugin-dev/SKILL.md" in links
    assert "capabilities/deepseek-harness-plugin-converter/SKILL.md" in links
    assert not (skills / "jiuwen-system-plugin-dev").exists()
    assert (root / "references" / "capabilities" / "workswarm-app-builder" / "scripts" / "publish.py").is_file()
    # 入口是唯一被注入的技能描述：要能读到每个内置能力的名称与用途，
    # 否则模型只能看到「按目录检索」，无法把用户提到的能力名映射过去。
    skill_md = (root / "SKILL.md").read_text(encoding="utf-8")
    description = re.search(r"^description:\s*(.+)$", skill_md, re.MULTILINE).group(1)
    entries = dict(re.findall(r"([A-Za-z0-9_-]+)（([^）]*)）", description))
    assert set(entries) == shipped_names
    assert f"（{len(shipped_names)} 项" in description or f"收录 {len(shipped_names)} 项" in description
    assert all(4 <= len(blurb) <= 24 for blurb in entries.values())
    # 只允许名称与短简介，不退回逐个注入完整描述的量级。
    assert len(description) < 1800


def test_catalog_description_carries_names_and_intros_into_the_prompt():
    """唯一注入的技能条目里要能直接读到内置能力名称与简介。"""
    from types import SimpleNamespace

    from openjiuwen.harness.rails import SkillUseRail

    class _Fs:
        async def read_file(self, path, **_kwargs):
            try:
                content = Path(path).read_text(encoding="utf-8")
            except OSError as exc:
                return SimpleNamespace(code=1, message=str(exc), data=None)
            return SimpleNamespace(code=0, message="", data=SimpleNamespace(content=content))

    skills = _resource_path(utils.get_builtin_skills_dir())
    rail = SkillUseRail(skills_dir=str(skills), skill_mode="all")
    rail.sys_operation = SimpleNamespace(fs=lambda: _Fs())
    rail.system_prompt_builder = SimpleNamespace(language="cn")
    asyncio.run(rail._prepare_skills())
    prompt = rail._build_all_mode_prompt()

    assert [skill.name for skill in rail.skills] == [BUILTIN_CATALOG_NAME]
    assert "xlsx（Excel、表格、公式与图表）" in prompt
    assert "swarmskill-creator（团队技能、SwarmFlow、多角色工作流）" in prompt
    assert len(prompt) < 1700


@pytest.mark.parametrize("customized", [False, True])
def test_release_system_plugin_copy_migrates_only_when_its_full_resources_match(tmp_path, monkeypatch, customized):
    builtin = _resource_path(utils.get_builtin_skills_dir())
    skills = _resource_path(tmp_path / "skills")
    name = "jiuwen-system-plugin-dev"
    source = builtin / BUILTIN_CATALOG_NAME / "references" / "capabilities" / name
    legacy = skills / name
    shutil.copytree(source, legacy)
    expected = json.loads((builtin / BUILTIN_CATALOG_NAME / "legacy_hashes.json").read_text())[name]
    assert skill_tree_digest(legacy) == expected
    assert len([
        path for path in legacy.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    ]) == 16
    if customized:
        with (legacy / "SKILL.md").open("a", encoding="utf-8") as page:
            page.write("\nUser-authored local instructions.\n")
    original = skill_tree_digest(legacy)
    state_path = skills / "skills_state.json"
    state_path.write_text(json.dumps({
        "installed_plugins": [{"name": name, "source": "builtin"}],
        "skill_configs": {name: {"enabled": False}},
    }), encoding="utf-8")
    monkeypatch.setattr(utils, "get_builtin_skills_dir", lambda: builtin)
    monkeypatch.setattr(utils, "get_agent_skills_dir", lambda: skills)

    utils.ensure_default_builtin_skills()

    state = json.loads(state_path.read_text(encoding="utf-8"))
    installed = {item["name"]: item for item in state["installed_plugins"]}
    assert installed[BUILTIN_CATALOG_NAME]["source"] == "builtin"
    if customized:
        assert skill_tree_digest(legacy) == original
        assert set(installed) == {BUILTIN_CATALOG_NAME, name}
        assert installed[name]["source"] == "local"
        assert installed[name]["legacy_builtin"] is True
        assert name not in state.get("builtin_catalog_migrated", [])
    else:
        assert not legacy.exists()
        archived = skills / BUILTIN_CATALOG_NAME / ".legacy" / name
        assert skill_tree_digest(archived) == original
        assert set(installed) == {BUILTIN_CATALOG_NAME}
        assert name in state["builtin_catalog_migrated"]
        assert name in state["builtin_catalog_disabled"]
    assert state["skill_configs"][name]["enabled"] is False


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


def _catalog_upgrade(tmp_path, monkeypatch):
    builtin, skills = _workspace(tmp_path)
    catalog = skills / BUILTIN_CATALOG_NAME
    old_navigation = "original capability navigation\n"
    (catalog / "references/catalog.md").write_text(old_navigation, encoding="utf-8")
    seed = builtin / BUILTIN_CATALOG_NAME
    (seed / "references/catalog.md").write_text("navigation with system plugin development\n", encoding="utf-8")
    capability = seed / "references/capabilities/jiuwen-system-plugin-dev"
    capability.mkdir()
    (capability / "SKILL.md").write_text("system plugin development\n", encoding="utf-8")
    (capability / "scripts").mkdir()
    (capability / "scripts/build_package.py").write_text("packaged script\n", encoding="utf-8")
    versions = {"_catalog_files": {"references/catalog.md": [{
        "digest": hashlib.sha256(old_navigation.encode()).hexdigest(),
    }]}}
    (seed / "legacy_versions.json").write_text(json.dumps(versions), encoding="utf-8")
    state_path = skills / "skills_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["installed_plugins"].append({"name": BUILTIN_CATALOG_NAME, "source": "builtin"})
    state_path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(utils, "get_builtin_skills_dir", lambda: builtin)
    monkeypatch.setattr(utils, "get_agent_skills_dir", lambda: skills)
    return seed, catalog


def test_existing_catalog_receives_new_capability_and_preserves_user_changes(tmp_path, monkeypatch):
    seed, catalog = _catalog_upgrade(tmp_path, monkeypatch)
    custom = catalog / "references/capabilities/sample/SKILL.md"
    custom.write_text("my existing capability edit\n", encoding="utf-8")
    utils.ensure_default_builtin_skills()
    installed = catalog / "references/capabilities/jiuwen-system-plugin-dev"
    assert (installed / "scripts/build_package.py").read_text() == "packaged script\n"
    assert (catalog / "references/catalog.md").read_bytes() == (seed / "references/catalog.md").read_bytes()
    assert custom.read_text() == "my existing capability edit\n"
    assert not list(installed.parent.glob(".builtin-capability-*"))
    (installed / "SKILL.md").write_text("my system plugin instructions\n", encoding="utf-8")
    (catalog / "references/catalog.md").write_text("my navigation\n", encoding="utf-8")
    (seed / "references/capabilities/jiuwen-system-plugin-dev/SKILL.md").write_text("new shipped instructions\n", encoding="utf-8")
    utils.ensure_default_builtin_skills()
    assert (installed / "SKILL.md").read_text() == "my system plugin instructions\n"
    assert (catalog / "references/catalog.md").read_text() == "my navigation\n"
    assert custom.read_text() == "my existing capability edit\n"


def test_failed_catalog_capability_copy_keeps_old_navigation_and_no_partial_leaf(tmp_path, monkeypatch):
    from jiuwenswarm.common import builtin_skills

    _, catalog = _catalog_upgrade(tmp_path, monkeypatch)
    def fail_copy(source, destination):
        Path(destination).mkdir()
        (Path(destination) / "partial.txt").write_text("interrupted")
        raise OSError("capability copy interrupted")
    monkeypatch.setattr(builtin_skills.shutil, "copytree", fail_copy)
    utils.ensure_default_builtin_skills()
    assert (catalog / "references/catalog.md").read_text() == "original capability navigation\n"
    assert not (catalog / "references/capabilities/jiuwen-system-plugin-dev").exists()
    assert not list((catalog / "references/capabilities").glob(".builtin-capability-*"))
