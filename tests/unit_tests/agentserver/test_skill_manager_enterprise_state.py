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

from jiuwenswarm.extensions.sdk.skill_source import (
    ArtifactDescriptor,
    ProviderInvocationContext,
    SkillRef,
    SkillSearchRequest,
    SkillSearchResult,
    SkillSourceProvider,
    SourceConfig,
)
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
from jiuwenswarm.server.runtime.skill.skill_prebuilt import (
    SkillPrebuiltItem,
    SkillPrebuiltSyncResult,
    SkillPrebuiltSynchronizer,
)


class _StubSkillSourceProvider(SkillSourceProvider):
    def __init__(self, source_id: str = "hub-a"):
        self.source_id = source_id
        self.provider_type = "stub"
        self.display_name = source_id
        self.capabilities = frozenset({"search", "get_artifact"})

    async def search(
        self,
        request: SkillSearchRequest,
        context: ProviderInvocationContext,
    ) -> SkillSearchResult:
        return SkillSearchResult(items=())

    async def get_artifact(
        self,
        skill_ref: SkillRef,
        version_id: str,
        context: ProviderInvocationContext,
    ) -> ArtifactDescriptor:
        raise NotImplementedError


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


def test_skills_list_exposes_installation_origin_from_installed_plugins(
    tmp_path: Path,
) -> None:
    """企业源只写 installed_plugins 时，skills.list 仍应带上 origin 供前端匹配。"""
    workspace = tmp_path / "tenant"
    manager = SkillManager(workspace_dir=str(workspace))
    _write_skill(workspace, "source-skill")
    manager.record_skill_installation(
        name="source-skill",
        source_type="user",
        origin="customer-skillhub:asset-9",
        source="customer-skillhub",
        source_id="customer-skillhub",
        skill_id="asset-9",
        version_id="v-1",
        version="1.0.0",
    )
    # 企业源安装不写 local_skills；列表必须从安装账本补 origin。
    assert manager.get_local_skills() == []

    payload = asyncio.run(manager.handle_skills_list({}))
    listed = next(
        skill
        for skill in payload["skills"]
        if skill.get("name") == "source-skill"
    )

    assert listed["origin"] == "customer-skillhub:asset-9"
    assert listed["source_id"] == "customer-skillhub"
    assert listed["skill_id"] == "asset-9"
    assert listed["version_id"] == "v-1"
    assert listed["source_type"] == "user"


def test_apply_skill_source_configs_empty_array_clears_registry(tmp_path: Path) -> None:
    """显式 skill_sources: [] 应清空已配置源；字段缺失则不碰现有 registry。"""
    workspace = tmp_path / "tenant"
    manager = SkillManager(workspace_dir=str(workspace))
    manager.register_skill_source_provider(
        SourceConfig(
            source_id="hub-a",
            provider_type="stub",
            capabilities=frozenset({"search", "get_artifact"}),
        ),
        _StubSkillSourceProvider("hub-a"),
    )
    before = asyncio.run(manager.handle_skills_source_providers({}))
    assert [item["source_id"] for item in before["providers"]] == ["hub-a"]

    untouched = asyncio.run(
        manager.apply_skill_source_configs(
            [{"custom_config": {"other": True}}]
        )
    )
    assert untouched["applied"] is False
    still_there = asyncio.run(manager.handle_skills_source_providers({}))
    assert [item["source_id"] for item in still_there["providers"]] == ["hub-a"]

    cleared = asyncio.run(
        manager.apply_skill_source_configs(
            [{"custom_config": {"skill_sources": []}}]
        )
    )
    assert cleared["applied"] is True
    after = asyncio.run(manager.handle_skills_source_providers({}))
    assert after["providers"] == []


def test_prebuilt_skill_cannot_be_changed_or_uninstalled(tmp_path: Path) -> None:
    """单机版：预置不可启停、不可卸载。"""
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


def test_enterprise_prebuilt_skill_can_toggle_but_not_uninstall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """企业预置（prebuilt）可启停；仍不可卸载。"""
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    workspace = tmp_path / "tenant"
    manager = SkillManager(workspace_dir=str(workspace))
    skill_dir = _write_skill(workspace, "managed-skill")
    record = _record_prebuilt(manager)

    toggle_off = asyncio.run(
        manager.handle_skills_toggle(
            {
                "name": "managed-skill",
                "origin": record["origin"],
                "enabled": False,
            }
        )
    )
    assert toggle_off["success"] is True
    assert toggle_off["enabled"] is False
    assert manager.get_skill_enabled("managed-skill") is False

    toggle_on = asyncio.run(
        manager.handle_skills_toggle(
            {
                "name": "managed-skill",
                "origin": record["origin"],
                "enabled": True,
            }
        )
    )
    assert toggle_on["success"] is True
    assert manager.get_skill_enabled("managed-skill") is True

    uninstall_result = asyncio.run(
        manager.handle_skills_uninstall(
            {"name": "managed-skill", "origin": record["origin"]}
        )
    )
    assert uninstall_result["error_code"] == "prebuilt_not_removable"
    assert skill_dir.is_dir()


def test_enterprise_builtin_skill_can_toggle_but_not_uninstall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """企业内置（builtin）可启停；仍不可卸载。"""
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    workspace = tmp_path / "tenant"
    manager = SkillManager(workspace_dir=str(workspace))
    skill_dir = _write_skill(workspace, "docx-pro")
    record = manager.record_skill_installation(
        name="docx-pro",
        source_type="builtin",
        source="builtin",
        origin="builtin:docx-pro",
        version="1.0.0",
    )

    toggle_off = asyncio.run(
        manager.handle_skills_toggle(
            {"name": "docx-pro", "origin": record["origin"], "enabled": False}
        )
    )
    assert toggle_off["success"] is True
    assert toggle_off["enabled"] is False
    assert manager.get_skill_enabled("docx-pro") is False

    toggle_on = asyncio.run(
        manager.handle_skills_toggle(
            {"name": "docx-pro", "origin": record["origin"], "enabled": True}
        )
    )
    assert toggle_on["success"] is True
    assert manager.get_skill_enabled("docx-pro") is True

    uninstall_result = asyncio.run(
        manager.handle_skills_uninstall(
            {"name": "docx-pro", "origin": record["origin"]}
        )
    )
    assert uninstall_result["error_code"] == "prebuilt_not_removable"
    assert skill_dir.is_dir()


def test_personal_builtin_skill_can_uninstall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """个人版内置（builtin）与 develop 一致：安装副本可卸载。"""
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    workspace = tmp_path / "tenant"
    manager = SkillManager(workspace_dir=str(workspace))
    skill_dir = _write_skill(workspace, "docx-pro")
    record = manager.record_skill_installation(
        name="docx-pro",
        source_type="builtin",
        source="builtin",
        origin="builtin:docx-pro",
        version="1.0.0",
    )

    uninstall_result = asyncio.run(
        manager.handle_skills_uninstall(
            {"name": "docx-pro", "origin": record["origin"]}
        )
    )
    assert uninstall_result.get("success") is True
    assert not skill_dir.is_dir()
    assert manager.list_skill_installations() == []


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


def test_enterprise_uninstall_allows_blank_source_type(tmp_path: Path) -> None:
    manager = SkillManager(workspace_dir=str(tmp_path), service_id="svc", agent_id="agent")
    skill_dir = _write_skill(tmp_path, "local-skill")
    manager.record_skill_installation(
        name="local-skill", source_type="user", origin="local:local-skill", source="local",
    )
    state_file = tmp_path / "skills" / "skills_state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["installed_plugins"][0]["source_type"] = ""
    state_file.write_text(json.dumps(state), encoding="utf-8")

    restored = SkillManager(workspace_dir=str(tmp_path), service_id="svc", agent_id="agent")
    result = asyncio.run(restored.handle_skills_web_uninstall({"name": "local-skill"}))

    assert result == {"success": True, "name": "local-skill"}
    assert not skill_dir.exists()
    assert restored.list_skill_installations() == []


def test_enterprise_web_uninstall_still_rejects_prebuilt(tmp_path: Path) -> None:
    manager = SkillManager(workspace_dir=str(tmp_path), service_id="svc", agent_id="agent")
    skill_dir = _write_skill(tmp_path, "managed-skill")
    _record_prebuilt(manager)

    result = asyncio.run(manager.handle_skills_web_uninstall({"name": "managed-skill"}))

    assert result["success"] is False
    assert result["error_code"] == "prebuilt_not_removable"
    assert skill_dir.is_dir()


def test_enterprise_web_uninstall_still_rejects_builtin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """企业版 web 卸载入口：builtin 与主路径一致，仍不可卸。"""
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    manager = SkillManager(workspace_dir=str(tmp_path), service_id="svc", agent_id="agent")
    skill_dir = _write_skill(tmp_path, "docx-pro")
    manager.record_skill_installation(
        name="docx-pro",
        source_type="builtin",
        source="builtin",
        origin="builtin:docx-pro",
        version="1.0.0",
    )

    result = asyncio.run(manager.handle_skills_web_uninstall({"name": "docx-pro"}))

    assert result["success"] is False
    assert result["error_code"] == "prebuilt_not_removable"
    assert skill_dir.is_dir()


def test_personal_web_uninstall_allows_builtin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """个人版 web 卸载入口：builtin 安装副本可卸（与 handle_skills_uninstall 对齐）。"""
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    manager = SkillManager(workspace_dir=str(tmp_path), service_id="svc", agent_id="agent")
    skill_dir = _write_skill(tmp_path, "docx-pro")
    manager.record_skill_installation(
        name="docx-pro",
        source_type="builtin",
        source="builtin",
        origin="builtin:docx-pro",
        version="1.0.0",
    )

    result = asyncio.run(manager.handle_skills_web_uninstall({"name": "docx-pro"}))

    assert result == {"success": True, "name": "docx-pro"}
    assert not skill_dir.is_dir()
    assert manager.list_skill_installations() == []


def test_enterprise_uninstall_does_not_fall_back_from_unknown_origin(tmp_path: Path) -> None:
    manager = SkillManager(workspace_dir=str(tmp_path), service_id="svc", agent_id="agent")
    skill_dir = _write_skill(tmp_path, "user-skill")
    manager.record_skill_installation(name="user-skill", source_type="user", origin="customhub:asset-1")
    result = asyncio.run(manager.handle_skills_web_uninstall(
        {"name": "user-skill", "origin": "customhub:missing"}
    ))
    assert result["success"] is False
    assert skill_dir.exists()


def test_enterprise_uninstall_resolves_origin_kept_only_on_local_skills(tmp_path: Path) -> None:
    """旧 skillhub 把 origin 只写在 local_skills，网页仍按该 origin 卸载。"""
    manager = SkillManager(workspace_dir=str(tmp_path), service_id="svc", agent_id="agent")
    skill_dir = _write_skill(tmp_path, "employment-rights-team")
    manager.record_skill_installation(
        name="employment-rights-team",
        source_type="user",
        origin="skillhub:8",
        source="skillhub",
    )
    state_file = tmp_path / "skills" / "skills_state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    plugin = state["installed_plugins"][0]
    plugin.pop("origin", None)
    plugin.pop("source_type", None)
    plugin["marketplace"] = "skillhub"
    state["local_skills"] = [{
        "name": "employment-rights-team",
        "origin": "skillhub:8",
        "source": "skillhub",
    }]
    state_file.write_text(json.dumps(state), encoding="utf-8")

    restored = SkillManager(workspace_dir=str(tmp_path), service_id="svc", agent_id="agent")
    result = asyncio.run(restored.handle_skills_web_uninstall({
        "name": "employment-rights-team",
        "origin": "skillhub:8",
    }))

    assert result == {"success": True, "name": "employment-rights-team"}
    assert not skill_dir.exists()
    assert restored.list_skill_installations() == []
    assert restored.get_local_skills() == []


def test_enterprise_uninstall_falls_back_when_only_local_skills_exist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """半安装只写了 local_skills、installed_plugins 没有同名行时仍能卸载。"""
    warnings: list[str] = []

    def _capture_warning(message: str, *args: object, **_kwargs: object) -> None:
        warnings.append(message % args if args else message)

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.logger.warning",
        _capture_warning,
    )
    SkillManager(workspace_dir=str(tmp_path), service_id="svc", agent_id="agent")
    skill_dir = _write_skill(tmp_path, "demo-skill")
    state_file = tmp_path / "skills" / "skills_state.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {
        "marketplaces": [],
        "installed_plugins": [],
        "local_skills": [],
    }
    state["installed_plugins"] = []
    state["local_skills"] = [{
        "name": "demo-skill",
        "origin": "local:demo-skill",
        "source": "local",
    }]
    state_file.write_text(json.dumps(state), encoding="utf-8")

    restored = SkillManager(workspace_dir=str(tmp_path), service_id="svc", agent_id="agent")
    result = asyncio.run(restored.handle_skills_web_uninstall({
        "name": "demo-skill",
        "origin": "local:demo-skill",
    }))

    assert result == {"success": True, "name": "demo-skill"}
    assert any("only local_skills has skill" in item for item in warnings)
    assert not skill_dir.exists()
    assert restored.list_skill_installations() == []
    assert restored.get_local_skills() == []


def test_enterprise_uninstall_fallback_keeps_other_origin_plugin_row(
    tmp_path: Path,
) -> None:
    """同名但 origin 不同时仍走兜底：清掉 local_skills，保留另一条插件记录。

    磁盘按技能名只有一份目录，卸载会删掉这个目录。
    """
    manager = SkillManager(workspace_dir=str(tmp_path), service_id="svc", agent_id="agent")
    skill_dir = _write_skill(tmp_path, "demo-skill")
    plugin = manager.record_skill_installation(
        name="demo-skill",
        source_type="user",
        origin="clawhub:owner/slug",
        source="clawhub",
    )
    state_file = tmp_path / "skills" / "skills_state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["local_skills"] = [{
        "name": "demo-skill",
        "origin": "local:demo-skill",
        "source": "local",
    }]
    state_file.write_text(json.dumps(state), encoding="utf-8")

    restored = SkillManager(workspace_dir=str(tmp_path), service_id="svc", agent_id="agent")
    result = asyncio.run(restored.handle_skills_web_uninstall({
        "name": "demo-skill",
        "origin": "local:demo-skill",
    }))

    assert result == {"success": True, "name": "demo-skill"}
    assert not skill_dir.exists()
    assert restored.get_local_skills() == []
    remaining = restored.list_skill_installations()
    assert len(remaining) == 1
    assert remaining[0]["origin"] == plugin["origin"]
    assert remaining[0]["name"] == "demo-skill"


def test_enterprise_uninstall_does_not_fallback_when_local_name_mismatches(
    tmp_path: Path,
) -> None:
    SkillManager(workspace_dir=str(tmp_path), service_id="svc", agent_id="agent")
    skill_dir = _write_skill(tmp_path, "demo-skill")
    state_file = tmp_path / "skills" / "skills_state.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {
        "marketplaces": [],
        "installed_plugins": [],
        "local_skills": [],
    }
    state["installed_plugins"] = []
    state["local_skills"] = [{
        "name": "other-skill",
        "origin": "skillhub:8",
        "source": "skillhub",
    }]
    state_file.write_text(json.dumps(state), encoding="utf-8")

    restored = SkillManager(workspace_dir=str(tmp_path), service_id="svc", agent_id="agent")
    result = asyncio.run(restored.handle_skills_web_uninstall({
        "name": "demo-skill",
        "origin": "skillhub:8",
    }))

    assert result["success"] is False
    assert result["error_code"] == "not_found"
    assert skill_dir.exists()


def test_enterprise_web_uninstall_logs_when_skill_is_not_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[str] = []

    def _capture_warning(message: str, *args: object, **_kwargs: object) -> None:
        warnings.append(message % args if args else message)

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.logger.warning",
        _capture_warning,
    )
    manager = SkillManager(workspace_dir=str(tmp_path), service_id="svc", agent_id="agent")
    result = asyncio.run(manager.handle_skills_web_uninstall({
        "name": "missing-skill",
        "origin": "local:missing-skill",
    }))

    assert result["success"] is False
    assert result["error_code"] == "not_found"
    assert any("web uninstall not found" in item and "missing-skill" in item for item in warnings)


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
