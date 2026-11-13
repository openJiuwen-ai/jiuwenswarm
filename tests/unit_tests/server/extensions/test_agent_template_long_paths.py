# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Expert create/edit operations preserve deep skills and clean failed copies."""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from jiuwenswarm.common.filesystem_paths import extended_path
from jiuwenswarm.server.runtime import extension_package_manager as catalog


@pytest.fixture
def deep_expert(extension_workspace, monkeypatch):
    skills = Path(extended_path(extension_workspace / "skills"))
    relative = Path("references") / ("deep-directory-" * 7) / "guide.md"
    for name in ("old-skill", "new-skill"):
        skill = skills / name
        document = skill / relative
        document.parent.mkdir(parents=True)
        document.write_text(name, encoding="utf-8")
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Test skill\n---\n# Test\n", encoding="utf-8",
        )
    monkeypatch.setattr(catalog, "get_agent_skills_dir", lambda: Path(str(skills)))
    package_id = "agent-" + "long-name-" * 16
    package = Path(extended_path(
        extension_workspace / "plugins/agent_templates/local" / package_id
    ))
    params = {
        "id": package_id, "name": "Deep expert", "description": "Test expert",
        "persona": "Original persona", "skills": ["old-skill"],
    }
    return params, package, relative


@pytest.mark.parametrize("provider", [None, "claudecode"])
def test_create_update_and_delete_expert_with_long_paths(deep_expert, provider):
    params, package, relative = deep_expert
    if provider:
        params["runtime"] = {"provider_name": provider, "config": {}}
    catalog.create_agent_template(params)
    document = package / "skills/old-skill" / relative
    assert len(str(document)) > 260
    assert document.read_text(encoding="utf-8") == "old-skill"
    with pytest.raises(ValueError, match="already exists"):
        catalog.create_agent_template(params)

    catalog.update_agent_template({**params, "persona": "Updated persona", "skills": ["new-skill"]})
    assert not (package / "skills/old-skill").exists()
    assert (package / "skills/new-skill" / relative).read_text(encoding="utf-8") == "new-skill"
    assert (package / "persona" / f"{params['id']}.md").read_text(encoding="utf-8") == "Updated persona"
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["skills"] == [{"dir": "./skills/new-skill", "mode": "all"}]
    if provider:
        assert manifest["runtime"]["provider_name"] == provider
    catalog.update_agent_template({**params, "skills": []})
    assert not (package / "skills").exists()
    catalog.delete_agent_template({"id": params["id"]})
    assert not package.exists()


@pytest.mark.parametrize("operation", ["create", "update"])
def test_expert_partial_copy_failure_cleans_temporary_skills(deep_expert, monkeypatch, operation):
    params, package, relative = deep_expert
    if operation == "update":
        catalog.create_agent_template(params)
    original_copy = shutil.copytree

    def partial_copy(src, dst, *args, **kwargs):
        result = original_copy(src, dst, *args, **kwargs)
        if Path(src).name == "new-skill":
            raise OSError("injected copy failure")
        return result

    monkeypatch.setattr(shutil, "copytree", partial_copy)
    action = catalog.create_agent_template if operation == "create" else catalog.update_agent_template
    with pytest.raises(OSError, match="injected copy failure"):
        action({**params, "skills": ["new-skill"]})
    assert not (package / ".skills.tmp").exists()
    if operation == "create":
        assert not package.exists()
    else:
        assert (package / "skills/old-skill" / relative).read_text(encoding="utf-8") == "old-skill"
        assert not (package / "skills/new-skill").exists()


def test_expert_manifest_write_failure_removes_new_package(deep_expert, monkeypatch):
    params, package, _ = deep_expert

    def fail_write(*args):
        raise OSError("injected write failure")

    monkeypatch.setattr(catalog, "_write_json", fail_write)
    with pytest.raises(OSError, match="injected write failure"):
        catalog.create_agent_template(params)
    assert not package.exists()


@pytest.fixture
def legacy_windows_paths(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Requires native Windows filesystem APIs")

    def check_path(path):
        value = os.fsdecode(path)
        if len(value) >= 248 and not value.startswith("\\\\?\\"):
            error = OSError("[WinError 206] File name too long")
            error.winerror = 206
            raise error

    def guarded(original):
        def call(path, *args, **kwargs):
            check_path(path)
            return original(path, *args, **kwargs)
        return call

    original_copy = shutil.copytree

    def limited_copy(src, dst, *args, **kwargs):
        check_path(src)
        check_path(dst)
        return original_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(shutil, "copytree", limited_copy)
    monkeypatch.setattr(shutil, "rmtree", guarded(shutil.rmtree))
    for name in ("mkdir", "read_text", "write_text", "rename", "exists", "is_dir", "is_file", "iterdir"):
        monkeypatch.setattr(Path, name, guarded(getattr(Path, name)))


@pytest.mark.parametrize("operation", ["round-trip", "create", "update", "write"])
def test_windows_expert_operations_with_legacy_path_limits(
    deep_expert, monkeypatch, legacy_windows_paths, operation,
):
    if operation == "round-trip":
        test_create_update_and_delete_expert_with_long_paths(deep_expert, "claudecode")
    elif operation == "write":
        test_expert_manifest_write_failure_removes_new_package(deep_expert, monkeypatch)
    else:
        test_expert_partial_copy_failure_cleans_temporary_skills(deep_expert, monkeypatch, operation)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [None, "claudecode"])
async def test_created_long_path_expert_is_visible_and_can_join_group(deep_expert, provider):
    params, package, relative = deep_expert
    if provider:
        params["runtime"] = {"provider_name": provider, "config": {}}
    catalog.create_agent_template(params)
    assert len(str(package / "manifest.json")) > 260
    cards = await catalog.list_agent_templates({"include_team_compatibility": True})
    card = next(item for item in cards if item["id"] == params["id"])
    assert card["teamCompatible"]["member"] is True
    details = catalog.show_agent_template(params["id"])
    assert details is not None
    assert details["persona"] == params["persona"]
    assert details["skills"][0]["id"] == "old-skill"

    catalog.install_agent_template({"id": params["id"]})
    assert catalog.resolve_agent_template_dir(params["id"]).name == params["id"]
    catalog.create_agent_template({
        "id": "native-leader", "name": "Leader", "description": "Coordinate",
        "persona": "Lead the team", "skills": [],
    })
    group_id = "long-path-expert-team"
    assert catalog.create_agent_group({
        "id": group_id, "name": "Long path team", "description": "Team",
        "persona": "Coordinate", "leaderId": "native-leader",
        "memberIds": [params["id"]], "skills": [],
    }) == {"id": group_id}
    group = catalog.show_agent_group(group_id)
    assert group is not None
    assert params["id"] in [member["agentTemplateId"] for member in group["members"]]
    catalog.uninstall_agent_template({"id": params["id"]})
    assert catalog.show_agent_template(params["id"]) is None


@pytest.mark.asyncio
async def test_windows_created_expert_remains_visible(deep_expert, legacy_windows_paths):
    await test_created_long_path_expert_is_visible_and_can_join_group(deep_expert, "claudecode")


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["built_in", "resources"])
async def test_long_path_package_sources_preserve_conflict_checks(deep_expert, monkeypatch, source):
    params, package, _ = deep_expert
    catalog.create_agent_template(params)
    if source == "built_in":
        root = catalog._built_in_root("agent_templates")
    else:
        root = package.parent.parent.parent / "resource-shelf"
        monkeypatch.setattr(catalog, "get_equipment_resources_agent_templates_dir", lambda: root)
    destination = Path(extended_path(root / params["id"]))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(package, destination)
    with pytest.raises(ValueError, match="package conflict"):
        await catalog.list_agent_templates()
    with pytest.raises(ValueError, match="package conflict"):
        catalog.show_agent_template(params["id"])

    catalog.delete_agent_template({"id": params["id"]})
    details = catalog.show_agent_template(params["id"])
    assert details is not None
    assert details["source"] == "builtin"
    assert details["persona"] == params["persona"]
    if source == "resources":
        cards = await catalog.list_agent_templates()
        assert params["id"] in [card["id"] for card in cards]


def test_package_enumeration_accepts_long_roots(tmp_path, monkeypatch):
    root = tmp_path / ("deep-root-" * 10) / ("deep-root-" * 10) / ("deep-root-" * 10)
    package = Path(extended_path(root / "expert"))
    package.mkdir(parents=True)
    (package / "manifest.json").write_text('{"package_type": "agent_template"}', encoding="utf-8")
    assert len(str(root)) > 260
    assert [entry.name for entry in catalog._iter_local_package_dirs(root)] == ["expert"]
    monkeypatch.setattr(catalog, "get_equipment_resources_agent_templates_dir", lambda: root)
    assert [entry.name for entry in catalog._iter_resource_package_dirs("agent_templates")] == ["expert"]


def test_windows_package_enumeration_accepts_long_roots(tmp_path, monkeypatch, legacy_windows_paths):
    test_package_enumeration_accepts_long_roots(tmp_path, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["built_in", "resources"])
async def test_windows_long_package_sources_preserve_conflicts(
    deep_expert, monkeypatch, legacy_windows_paths, source,
):
    await test_long_path_package_sources_preserve_conflict_checks(deep_expert, monkeypatch, source)
