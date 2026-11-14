# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Regression coverage for copying external members with deeply nested skills."""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from jiuwenswarm.common.filesystem_paths import extended_path
from jiuwenswarm.server.runtime import extension_package_manager as catalog


_MEMBER_ID = "agent-d641b281-2f9c-4486-9144-afa685e28efc"
_GROUP_ID = "agent-group-9b3b0abd-e4d0-4d4f-b3d5-916c64db3f91"


@pytest.fixture
def deep_member_group(extension_workspace):
    for package_id in ("planning-expert", _MEMBER_ID):
        package = Path(extended_path(
            extension_workspace / "plugins" / "agent_templates" / "local" / package_id
        ))
        (package / "persona").mkdir(parents=True)
        (package / "persona/expert.md").write_text("# Test expert", encoding="utf-8")
        manifest = {
            "package_type": "agent_template", "name": package_id, "description": "test expert",
            "persona": {"dir": "./persona"}, "skills": [],
        }
        if package_id == _MEMBER_ID:
            manifest["runtime"] = {"provider_name": "claudecode", "config": {}}
        (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    member = Path(extended_path(
        extension_workspace / "plugins" / "agent_templates" / "local" / _MEMBER_ID
    ))
    relative = Path("skills/baoyu-image-gen/references") / ("nested-reference-" * 5) / "guide.md"
    document = member / relative
    document.parent.mkdir(parents=True)
    document.write_text("deep skill content", encoding="utf-8")
    (member / "skills/baoyu-image-gen/SKILL.md").write_text(
        "---\nname: baoyu-image-gen\ndescription: Image generation\n---\n# Skill\n", encoding="utf-8",
    )
    manifest_path = member / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["skills"] = [{"dir": "./skills/baoyu-image-gen", "mode": "all"}]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    root = Path(extended_path(
        extension_workspace.parent.parent / ".agent_teams/agent_groups/local"
    ))
    params = {
        "id": _GROUP_ID, "name": "Deep external member team", "description": "test",
        "persona": "Coordinate members", "leaderId": "planning-expert",
        "memberIds": [_MEMBER_ID], "skills": [],
    }
    return params, root, relative, member


def test_create_and_install_group_preserves_deep_external_skills(deep_member_group):
    params, root, relative, _ = deep_member_group
    assert catalog.create_agent_group(params) == {"id": _GROUP_ID}
    package = root / _GROUP_ID
    copied = package / "agents" / _MEMBER_ID / relative
    assert len(str(copied)) > 260
    assert copied.read_text(encoding="utf-8") == "deep skill content"
    catalog.install_agent_group({"id": _GROUP_ID})
    assert catalog.is_agent_group_installed(_GROUP_ID)
    assert not list(root.glob(".ag_*"))
    catalog.uninstall_agent_group({"id": _GROUP_ID})
    assert not package.exists()


@pytest.mark.parametrize("failure", ["copy", "validation", "registration"])
def test_group_creation_cleans_deep_trees_after_failure(deep_member_group, monkeypatch, failure):
    import openjiuwen.agent_teams.experts as experts

    params, root, _, _ = deep_member_group

    def fail(*args, **kwargs):
        raise OSError("injected failure")

    if failure == "copy":
        original_copy = catalog._copytree_without_symlinks

        def partial_copy(src, dst):
            original_copy(src, dst)
            if Path(src).name == _MEMBER_ID:
                fail()

        monkeypatch.setattr(catalog, "_copytree_without_symlinks", partial_copy)
    elif failure == "validation":
        monkeypatch.setattr(experts, "load_agent_group_package", fail)
    else:
        monkeypatch.setattr(catalog, "upsert_agent_group_marketplace_entry", fail)
    with pytest.raises(OSError, match="injected failure"):
        catalog.create_agent_group(params)
    assert not (root / _GROUP_ID).exists()
    assert not list(root.glob(".ag_*"))
    assert not catalog.is_agent_group_installed(_GROUP_ID)


def test_group_creation_still_rejects_deep_symlinks(deep_member_group, tmp_path):
    params, root, relative, member = deep_member_group
    link = member / relative.parent / "linked.md"
    try:
        link.symlink_to(tmp_path / "outside.md")
    except OSError:
        pytest.skip("Symbolic link creation is unavailable")
    with pytest.raises(ValueError, match="symbolic links"):
        catalog.create_agent_group(params)
    assert not (root / _GROUP_ID).exists()
    assert not list(root.glob(".ag_*"))


@pytest.mark.skipif(sys.platform != "win32", reason="Requires native Windows filesystem APIs")
@pytest.mark.parametrize("failure", [None, "copy", "validation", "registration"])
def test_windows_group_copy_does_not_depend_on_long_paths_registry(deep_member_group, monkeypatch, failure):
    original_copy = shutil.copytree

    def check_path(path):
        value = os.fsdecode(path)
        if len(value) >= 248 and not value.startswith("\\\\?\\"):
            error = OSError("[WinError 206] File name too long")
            error.winerror = 206
            raise error

    def limited_copy(src, dst, *args, **kwargs):
        check_path(src)
        check_path(dst)
        return original_copy(src, dst, *args, **kwargs)

    def guarded_method(original):
        def guarded(path, *args, **kwargs):
            check_path(path)
            return original(path, *args, **kwargs)
        return guarded

    monkeypatch.setattr(shutil, "copytree", limited_copy)
    monkeypatch.setattr(shutil, "rmtree", guarded_method(shutil.rmtree))
    for name in ("mkdir", "read_text", "write_text", "rglob", "replace", "unlink"):
        monkeypatch.setattr(Path, name, guarded_method(getattr(Path, name)))
    if failure is None:
        test_create_and_install_group_preserves_deep_external_skills(deep_member_group)
    else:
        test_group_creation_cleans_deep_trees_after_failure(deep_member_group, monkeypatch, failure)
