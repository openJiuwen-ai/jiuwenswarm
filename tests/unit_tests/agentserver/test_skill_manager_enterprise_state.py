from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


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
