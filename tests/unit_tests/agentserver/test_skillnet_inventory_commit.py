"""SkillNet background writes share the SkillPack inventory transaction boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from filelock import FileLock, Timeout

from jiuwenswarm.server.runtime.skill import skill_manager as manager_module
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
from jiuwenswarm.server.runtime.skill.skillpack_install import inventory_mutex
from tests.unit_tests.agentserver.test_skillpack_backend import _write_pack, _write_skill


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "data"))
    instance = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    monkeypatch.setattr(instance, "_get_mirror_skills_dirs", lambda: [])
    yield instance
    instance._skillnet_install_jobs.clear()


def _download_skill(monkeypatch, manager, *, name="first-skill", on_download=lambda: None):
    def download(_url, temporary, _mirror):
        on_download()
        root = Path(temporary) / name
        _write_skill(root, name, description="new downloaded content")
        return str(root)

    monkeypatch.setattr(manager, "_skillnet_download_sync", download)


@pytest.mark.asyncio
async def test_download_is_unlocked_but_files_state_and_index_commit_hold_one_lock(manager, monkeypatch):
    lock_path = inventory_mutex(manager._skills_dir).lock_file
    events = []

    def unlocked(stage):
        with FileLock(lock_path, timeout=0):
            events.append(stage)

    def locked(stage):
        with pytest.raises(Timeout):
            with FileLock(lock_path, timeout=0):
                pass
        events.append(stage)

    _download_skill(monkeypatch, manager, on_download=lambda: unlocked("download"))
    original_copy = manager_module.shutil.copytree

    def copy(source, destination, *args, **kwargs):
        if Path(destination) == manager._skills_dir / "first-skill":
            locked("copy")
        return original_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(manager_module.shutil, "copytree", copy)
    for method_name, stage in [
        ("_add_local_skill", "local"), ("_add_installed_plugin", "plugin"),
        ("_refresh_agent_data_indexes", "index"),
    ]:
        original = getattr(manager, method_name)

        def wrapped(*args, _original=original, _stage=stage, **kwargs):
            locked(_stage)
            return _original(*args, **kwargs)

        monkeypatch.setattr(manager, method_name, wrapped)

    async def complete():
        unlocked("hook")

    manager._skillnet_install_complete_hook = complete
    # Another manager can update the inventory while this instance is stale.
    _write_skill(manager._skills_dir / "previous-skill", "previous-skill")
    other = SkillManager(workspace_dir=str(manager._agent_root))
    other._add_local_skill({"name": "previous-skill", "source": "local"})
    await manager._skillnet_install_background("install", "https://example.test/skill", False)

    assert manager._skillnet_install_jobs["install"]["status"] == "done"
    assert events == ["download", "copy", "local", "plugin", "index", "hook"]
    state = json.loads(manager._state_file.read_text())
    assert {item["name"] for item in state["local_skills"]} == {"previous-skill", "first-skill"}
    assert any(item["name"] == "first-skill" for item in state["installed_plugins"])


@pytest.mark.asyncio
async def test_force_cannot_overwrite_a_member_used_by_standard_pack(manager, monkeypatch):
    _write_skill(manager._skills_dir / "first-skill", "first-skill", description="original")
    _write_skill(manager._skills_dir / "second-skill", "second-skill")
    _write_pack(manager._skills_dir / "demo-pack")
    original = (manager._skills_dir / "first-skill/SKILL.md").read_bytes()
    before_state = manager._state_file.read_bytes() if manager._state_file.exists() else None
    _download_skill(monkeypatch, manager)

    await manager._skillnet_install_background("protected", "https://example.test/skill", True)

    job = await manager.handle_skills_skillnet_install_status({"install_id": "protected"})
    assert job["status"] == "failed"
    assert job["code"] == "SKILL_REFERENCED_BY_PACK"
    assert "demo-pack" in job["detail"]
    assert (manager._skills_dir / "first-skill/SKILL.md").read_bytes() == original
    after_state = manager._state_file.read_bytes() if manager._state_file.exists() else None
    assert after_state == before_state


@pytest.mark.asyncio
async def test_force_keeps_existing_behavior_for_unreferenced_skill(manager, monkeypatch):
    _write_skill(manager._skills_dir / "first-skill", "first-skill", description="original")
    _download_skill(monkeypatch, manager)

    await manager._skillnet_install_background("replace", "https://example.test/skill", True)

    assert manager._skillnet_install_jobs["replace"]["status"] == "done"
    assert "new downloaded content" in (manager._skills_dir / "first-skill/SKILL.md").read_text()


@pytest.mark.asyncio
async def test_another_inventory_transaction_blocks_worker_commit_without_writes(manager, monkeypatch):
    downloads = []
    _download_skill(monkeypatch, manager, on_download=lambda: downloads.append(True))
    with inventory_mutex(manager._skills_dir):
        await manager._skillnet_install_background("busy", "https://example.test/skill", False)

    assert downloads == [True]
    assert manager._skillnet_install_jobs["busy"]["status"] == "failed"
    assert not (manager._skills_dir / "first-skill").exists()
    manager.reload_state()
    assert not any(item.get("name") == "first-skill" for item in manager._state.get("local_skills", []))
