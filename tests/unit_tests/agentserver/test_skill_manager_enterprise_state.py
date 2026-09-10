from __future__ import annotations

import asyncio
import io
import json
import threading
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
from jiuwenswarm.server.runtime.skill.skill_prebuilt import (
    SkillPrebuiltItem,
    SkillPrebuiltSyncResult,
    SkillPrebuiltSynchronizer,
)


def _write_skill(workspace: Path, name: str) -> Path:
    skill_dir = workspace / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: managed skill\n---\n# {name}\n",
        encoding="utf-8",
    )
    return skill_dir


def _record_prebuilt(manager: SkillManager, name: str = "managed-skill") -> dict:
    return manager.record_skill_installation(
        name=name,
        source_type="prebuilt",
        origin="https://artifacts.example/managed-skill.zip",
        source="customer-skillhub",
        source_id="customer-skillhub",
        skill_id="asset-123",
        version_id="version-456",
        version="1.2.0",
    )


def _skill_archive(name: str, version: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            f"{name}/SKILL.md",
            f"---\nname: {name}\nversion: {version}\n---\n# {name}\n",
        )
    return output.getvalue()


def test_record_skill_installation_persists_stable_enterprise_fields(tmp_path: Path) -> None:
    workspace = tmp_path / "tenant"
    manager = SkillManager(workspace_dir=str(workspace))
    _write_skill(workspace, "managed-skill")

    first = _record_prebuilt(manager)
    second = _record_prebuilt(manager)

    assert first["installation_id"]
    assert second["installation_id"] == first["installation_id"]
    assert second["installed_at"] == first["installed_at"]
    assert second["source_type"] == "prebuilt"
    assert second["source_id"] == "customer-skillhub"
    assert second["skill_id"] == "asset-123"
    assert second["version_id"] == "version-456"
    assert second["version"] == "1.2.0"
    assert second["enabled"] is True

    state = json.loads(
        (workspace / "skills" / "skills_state.json").read_text(encoding="utf-8")
    )
    assert state["installed_plugins"] == [second]

    reloaded = SkillManager(workspace_dir=str(workspace))
    assert reloaded.list_skill_installations() == [second]


def test_skills_installed_adds_workspace_skill_dto_and_keeps_plugins(tmp_path: Path) -> None:
    workspace = tmp_path / "tenant"
    manager = SkillManager(workspace_dir=str(workspace))
    _write_skill(workspace, "managed-skill")
    record = _record_prebuilt(manager)

    payload = asyncio.run(manager.handle_skills_installed({}))

    assert isinstance(payload["plugins"], list)
    assert payload["skills"] == [
        {
            **record,
            "installed": True,
            "removable": False,
            "sync_status": "synced",
            "consistency": "ok",
        }
    ]


def test_prebuilt_skill_cannot_be_changed_or_uninstalled(tmp_path: Path) -> None:
    workspace = tmp_path / "tenant"
    manager = SkillManager(workspace_dir=str(workspace))
    skill_dir = _write_skill(workspace, "managed-skill")
    record = _record_prebuilt(manager)

    uninstall_result = asyncio.run(
        manager.handle_skills_uninstall(
            {"name": "managed-skill", "origin": record["origin"]}
        )
    )
    toggle_result = asyncio.run(
        manager.handle_skills_toggle(
            {
                "name": "managed-skill",
                "origin": record["origin"],
                "enabled": False,
            }
        )
    )

    assert uninstall_result["error_code"] == "prebuilt_not_removable"
    assert toggle_result["error_code"] == "prebuilt_not_toggleable"
    assert skill_dir.is_dir()
    assert manager.get_skill_enabled("managed-skill") is True
    assert manager.list_skill_installations() == [record]


def test_enterprise_web_install_and_uninstall_use_workspace_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "tenant"
    manager = SkillManager(
        workspace_dir=str(workspace),
        service_id="service-a",
        agent_id="agent-a",
    )
    source_url = "https://skills.example/user-skill.zip"
    monkeypatch.setattr(
        manager,
        "_download_web_skill_bytes",
        lambda _url: _skill_archive("user-skill", "1.0.0"),
    )

    installed = asyncio.run(manager.handle_skills_web_install({"url": source_url}))

    assert installed["success"] is True
    assert (workspace / "skills" / "user-skill" / "SKILL.md").is_file()
    record = manager.list_skill_installations()[0]
    assert record["source_type"] == "user"
    assert record["origin"] == source_url
    assert record["version"] == "1.0.0"

    removed = asyncio.run(manager.handle_skills_web_uninstall({"name": "user-skill"}))

    assert removed == {"success": True, "name": "user-skill"}
    assert not (workspace / "skills" / "user-skill").exists()
    assert manager.list_skill_installations() == []


def test_enterprise_uninstall_uses_origin_and_persisted_entity_directory(tmp_path: Path) -> None:
    manager = SkillManager(workspace_dir=str(tmp_path), service_id="svc", agent_id="agent")
    skill_dir = _write_skill(tmp_path, "package-directory")
    manager.record_skill_installation(
        name="internal-name", source_type="user", origin="customhub:asset-1",
        source="customhub", entity_dir="package-directory", market_display_name="中文技能",
    )
    # 模拟刷新后重建管理器，市场名、登记名、磁盘目录名彼此不同。
    restored = SkillManager(workspace_dir=str(tmp_path), service_id="svc", agent_id="agent")
    result = asyncio.run(restored.handle_skills_web_uninstall(
        {"name": "中文技能", "origin": "customhub:asset-1"}
    ))
    assert result == {"success": True, "name": "internal-name"}
    assert not skill_dir.exists()
    assert restored.list_skill_installations() == []


def test_enterprise_uninstall_does_not_fall_back_from_unknown_origin(tmp_path: Path) -> None:
    manager = SkillManager(workspace_dir=str(tmp_path), service_id="svc", agent_id="agent")
    skill_dir = _write_skill(tmp_path, "user-skill")
    manager.record_skill_installation(name="user-skill", source_type="user", origin="customhub:asset-1")
    result = asyncio.run(manager.handle_skills_web_uninstall(
        {"name": "user-skill", "origin": "customhub:missing"}
    ))
    assert result["success"] is False
    assert skill_dir.exists()


def test_enterprise_skills_list_does_not_wait_for_marketplace_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing the enterprise guard must make this request time out."""
    manager = SkillManager(workspace_dir=str(tmp_path / "tenant"))

    async def never_finishes() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.is_enterprise",
        lambda: True,
    )
    monkeypatch.setattr(manager, "_sync_marketplace_repos", never_finishes)

    payload = asyncio.run(
        asyncio.wait_for(
            manager.handle_skills_list({"refresh_marketplaces": True}),
            timeout=0.1,
        )
    )

    assert isinstance(payload["skills"], list)


def test_user_skill_toggle_does_not_overwrite_another_pod_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "tenant"
    builtin_dir = tmp_path / "builtin"
    builtin_dir.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.is_enterprise",
        lambda: True,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_builtin_skills_dir",
        lambda: builtin_dir,
    )
    first = SkillManager(workspace_dir=str(workspace))
    first.record_skill_installation(
        name="first-user-skill", source_type="user", origin="skillhub:first"
    )
    stale = SkillManager(workspace_dir=str(workspace))
    another_pod = SkillManager(workspace_dir=str(workspace))
    another_pod.record_skill_installation(
        name="second-user-skill", source_type="user", origin="skillhub:second"
    )

    result = asyncio.run(
        stale.handle_skills_toggle(
            {"name": "first-user-skill", "origin": "skillhub:first", "enabled": False}
        )
    )

    assert result["success"] is True
    persisted = SkillManager(workspace_dir=str(workspace)).list_skill_installations()
    assert {row["name"] for row in persisted} == {
        "first-user-skill",
        "second-user-skill",
    }


def test_prebuilt_sync_never_promotes_or_replaces_same_name_user_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "tenant"
    builtin_dir = tmp_path / "builtin"
    builtin_dir.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.is_enterprise",
        lambda: True,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_builtin_skills_dir",
        lambda: builtin_dir,
    )
    manager = SkillManager(workspace_dir=str(workspace))
    _write_skill(workspace, "user-skill")
    user_record = manager.record_skill_installation(
        name="user-skill",
        source_type="user",
        source="swarmskillhub",
        origin="swarmskillhub:user-asset",
        source_id="swarmskillhub",
        skill_id="user-asset",
        version_id="user-version",
        version="1.0.0",
    )
    synchronizer = SkillPrebuiltSynchronizer(
        workspace,
        service_id="service-a",
        agent_id="agent-a",
        skill_manager=manager,
    )
    skill_md = workspace / "skills" / "user-skill" / "SKILL.md"
    original_content = skill_md.read_text(encoding="utf-8")
    monkeypatch.setattr(
        manager,
        "_assert_skill_download_url_allowed",
        lambda _url: None,
    )
    monkeypatch.setattr(
        manager,
        "_download_http_archive_bytes_sync",
        lambda _url: _skill_archive("user-skill", "2.0.0"),
    )
    outcome = asyncio.run(
        synchronizer._ensure_prebuilt_installed(
            SkillPrebuiltItem(
                id="prebuilt-asset",
                source="https://skills.example/prebuilt.zip",
                version="2.0.0",
            ),
            {"user-skill": user_record},
        )
    )

    assert outcome["ok"] is False
    assert outcome["error_code"] == "skill_name_conflict"
    assert manager.list_skill_installations() == [user_record]
    assert skill_md.read_text(encoding="utf-8") == original_content


def test_update_status_commit_preserves_another_pod_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "tenant"
    builtin_dir = tmp_path / "builtin"
    builtin_dir.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.is_enterprise",
        lambda: True,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_builtin_skills_dir",
        lambda: builtin_dir,
    )
    first = SkillManager(workspace_dir=str(workspace))
    first_record = first.record_skill_installation(
        name="first-user-skill",
        source_type="user",
        source_id="swarmskillhub",
        skill_id="first",
        version_id="v1",
    )
    stale = SkillManager(workspace_dir=str(workspace))
    another_pod = SkillManager(workspace_dir=str(workspace))
    another_pod.record_skill_installation(
        name="second-user-skill",
        source_type="user",
        source_id="swarmskillhub",
        skill_id="second",
        version_id="v1",
    )

    stale._commit_skill_update_statuses(
        {first_record["installation_id"]: {"updatable": True}}
    )

    persisted = SkillManager(workspace_dir=str(workspace)).list_skill_installations()
    assert {row["name"] for row in persisted} == {
        "first-user-skill",
        "second-user-skill",
    }
    assert next(row for row in persisted if row["name"] == "first-user-skill")[
        "updatable"
    ] is True


def test_stale_prebuilt_removal_does_not_delete_same_name_user_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "tenant"
    builtin_dir = tmp_path / "builtin"
    builtin_dir.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.is_enterprise",
        lambda: True,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_builtin_skills_dir",
        lambda: builtin_dir,
    )
    manager = SkillManager(workspace_dir=str(workspace))
    skill_dir = _write_skill(workspace, "shared-name")
    user_record = manager.record_skill_installation(
        name="shared-name",
        source_type="user",
        source_id="swarmskillhub",
        skill_id="user-asset",
        version_id="v1",
    )
    synchronizer = SkillPrebuiltSynchronizer(
        workspace,
        service_id="service-a",
        agent_id="agent-a",
        skill_manager=manager,
    )
    stale_prebuilt_row = {
        "name": "shared-name",
        "source_type": "prebuilt",
        "origin": "https://skills.example/old-prebuilt.zip",
    }
    result = SkillPrebuiltSyncResult()

    asyncio.run(
        synchronizer._remove_prebuilt_not_in_template(
            {"shared-name": stale_prebuilt_row},
            set(),
            result,
        )
    )

    assert skill_dir.is_dir()
    assert manager.list_skill_installations() == [user_record]
    assert result.succeeded == []


def test_async_skill_handler_acquires_state_lock_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "tenant"
    builtin_dir = tmp_path / "builtin"
    builtin_dir.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.is_enterprise",
        lambda: True,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_builtin_skills_dir",
        lambda: builtin_dir,
    )
    manager = SkillManager(workspace_dir=str(workspace))
    _write_skill(workspace, "user-skill")
    manager.record_skill_installation(
        name="user-skill",
        source_type="user",
    )
    original_transaction = manager.state_transaction
    lock_threads: list[int] = []

    @contextmanager
    def observed_transaction() -> Iterator[None]:
        lock_threads.append(threading.get_ident())
        with original_transaction():
            yield

    monkeypatch.setattr(manager, "state_transaction", observed_transaction)
    event_loop_thread = threading.get_ident()

    async def exercise_handlers() -> dict:
        await manager.handle_skills_list({})
        return await manager.handle_skills_toggle(
            {"name": "user-skill", "enabled": False}
        )

    result = asyncio.run(exercise_handlers())

    assert result["success"] is True
    assert len(lock_threads) >= 2
    assert all(thread_id != event_loop_thread for thread_id in lock_threads)
