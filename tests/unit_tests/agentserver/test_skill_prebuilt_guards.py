"""预置技能租户守卫与 workspace 状态同步回归。"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from jiuwenswarm.common.utils import (
    get_agent_skills_dir,
    get_multi_tenant_skill_dirs,
    get_tenant_agent_workspace_dir,
)
from jiuwenswarm.server.runtime.skill.skill_prebuilt import is_skill_prebuilt_tenant


@pytest.fixture
def agent_runtime_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    yield
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)


def test_is_skill_prebuilt_tenant_rejects_empty_ids(agent_runtime_env) -> None:
    assert is_skill_prebuilt_tenant("", "") is False
    assert is_skill_prebuilt_tenant("  ", "bot") is False
    assert is_skill_prebuilt_tenant("bot", None) is False


def test_is_skill_prebuilt_tenant_legacy_tenants(agent_runtime_env) -> None:
    assert is_skill_prebuilt_tenant("default", "default") is False
    assert is_skill_prebuilt_tenant("acp", "global_acp") is False
    assert is_skill_prebuilt_tenant("real-agent", "real-svc") is True


def test_is_skill_prebuilt_tenant_requires_agent_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    assert is_skill_prebuilt_tenant("real-agent", "real-svc") is False


def test_tenant_workspace_defaults_without_key() -> None:
    from jiuwenswarm.server.runtime.tenant_context import clear_tenant_bindings

    clear_tenant_bindings()
    path = get_tenant_agent_workspace_dir()
    assert path.name == "jiuwenclaw_workspace"
    # 个人版固定 service_default/agent_default；未设 enterprise 时走该路径。
    assert "service_default" in str(path)
    assert "agent_default" in str(path)


def test_multi_tenant_skill_dirs_single_tenant_fallback() -> None:
    assert get_multi_tenant_skill_dirs() == [get_agent_skills_dir()]


def test_parse_agent_skill_prebuilt_identity_fields() -> None:
    from jiuwenswarm.server.runtime.skill.skill_prebuilt import parse_agent_skill_prebuilt

    config = parse_agent_skill_prebuilt(
        "bot-1",
        "my-svc",
        [{
            "skill_id": "asset-1",
            "package_url": "https://artifacts.example/pkg.zip",
            "source_id": "customer-skillhub",
            "version_id": "version-001",
        }],
    )
    item = config.skills[0]
    assert (item.id, item.source_id, item.version_id, item.package_url) == (
        "asset-1", "customer-skillhub", "version-001", "https://artifacts.example/pkg.zip"
    )
    assert item.install_mode() == "provider"
    assert len(config.items_with_source) == 1


def test_parse_agent_skill_prebuilt_skips_invalid_items() -> None:
    from jiuwenswarm.server.runtime.skill.skill_prebuilt import parse_agent_skill_prebuilt

    assert parse_agent_skill_prebuilt("bot-1", "my-svc", []).skills == []
    config = parse_agent_skill_prebuilt(
        "bot-1", "my-svc", [{"skill_id": "only-id"}]
    )
    assert config.items_with_source == []


def _write_managed_skill(workspace: Path, name: str) -> None:
    skill_dir = workspace / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: managed\n---\n# {name}\n",
        encoding="utf-8",
    )


def test_workspace_state_sync_records_prebuilt_and_skips_same_version(
    tmp_path: Path,
) -> None:
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
    from jiuwenswarm.server.runtime.skill.skill_prebuilt import (
        AgentSkillPrebuiltConfig,
        SkillPrebuiltItem,
        SkillPrebuiltSynchronizer,
    )

    workspace = tmp_path / "tenant_ws"
    manager = SkillManager(workspace_dir=str(workspace))
    calls = {"count": 0}

    async def _provider_install(
        *, source_id: str, skill_id: str, version_id: str, force: bool = True
    ) -> dict[str, Any]:
        del force
        calls["count"] += 1
        _write_managed_skill(workspace, "managed-skill")
        row = manager.record_skill_installation(
            name="managed-skill",
            source_type="prebuilt",
            source=source_id,
            origin=f"{source_id}:{skill_id}",
            version="1.0.0",
            skill_id=skill_id,
            source_id=source_id,
            version_id=version_id,
            replace_by_name=True,
        )
        return {
            "ok": True,
            "skill_name": "managed-skill",
            "row": row,
            "version": "1.0.0",
        }

    def _url_must_not_run(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        pytest.fail("provider triple must not fall back to package_url install")

    manager.install_prebuilt_from_provider = _provider_install  # type: ignore[method-assign]
    manager.install_skill_sync = _url_must_not_run  # type: ignore[method-assign]
    synchronizer = SkillPrebuiltSynchronizer(
        workspace, "svc", "bot", skill_manager=manager
    )
    # source_id + skill_id + version_id → provider；package_url 不得改写路径。
    config = AgentSkillPrebuiltConfig(
        agent_id="bot",
        service_id="svc",
        skills=[SkillPrebuiltItem(
            id="asset-1",
            version="1.0.0",
            source="https://example.com/managed.zip",
            source_id="customer-skillhub",
            version_id="version-1",
        )],
    )

    first = asyncio.run(synchronizer.sync(config))
    second = asyncio.run(synchronizer.sync(config))
    records = manager.list_skill_installations()

    assert first.ok is True, first.errors
    assert second.ok is True, second.errors
    assert records, "provider sync must write an installation record"
    record = records[0]
    assert calls["count"] == 1
    assert record["name"] == "managed-skill"
    assert record["source_type"] == "prebuilt"
    assert record["source_id"] == "customer-skillhub"
    assert record["skill_id"] == "asset-1"
    assert record["version_id"] == "version-1"
    assert record["version"] == "1.0.0"
    assert second.enabled_skill_dirs == ["managed-skill"]


def test_workspace_state_sync_backfills_matching_prebuilt_without_download(
    tmp_path: Path,
) -> None:
    """Catch regressions that re-download an already provisioned prebuilt dir."""
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
    from jiuwenswarm.server.runtime.skill.skill_prebuilt import (
        AgentSkillPrebuiltConfig,
        SkillPrebuiltItem,
        SkillPrebuiltSynchronizer,
    )

    workspace = tmp_path / "tenant_ws"
    skill_dir = workspace / "skills" / "managed-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: managed-skill\n"
        "skill_id: asset-1\n"
        "version: 1.0.0\n"
        "description: managed\n"
        "---\n"
        "# managed-skill\n",
        encoding="utf-8",
    )
    manager = SkillManager(workspace_dir=str(workspace))

    def _unexpected_download(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        pytest.fail("matching prebuilt directory must be adopted without download")

    async def _unexpected_provider(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        pytest.fail("matching prebuilt directory must be adopted without download")

    manager.install_skill_sync = _unexpected_download  # type: ignore[method-assign]
    manager.install_prebuilt_from_provider = _unexpected_provider  # type: ignore[method-assign]
    # 盘→账本回填仅 url 路径：完整 provider 三元组会改走下载，故不填 version_id。
    config = AgentSkillPrebuiltConfig(
        agent_id="bot",
        service_id="svc",
        skills=[SkillPrebuiltItem(
            id="asset-1",
            version="1.0.0",
            source="https://example.com/managed.zip",
            source_id="customer-skillhub",
        )],
    )

    result = asyncio.run(
        SkillPrebuiltSynchronizer(
            workspace, "svc", "bot", skill_manager=manager
        ).sync(config)
    )

    assert result.ok is True, result.errors
    assert result.succeeded == ["managed-skill"]
    assert result.enabled_skill_dirs == ["managed-skill"]
    records = manager.list_skill_installations()
    assert records, "url sync must adopt the existing dir into the ledger"
    assert records == [{
        "installation_id": records[0]["installation_id"],
        "name": "managed-skill",
        "declared_name": "managed-skill",
        "entity_dir": "managed-skill",
        "source_type": "prebuilt",
        "source": "customer-skillhub",
        "origin": "https://example.com/managed.zip",
        "version": "1.0.0",
        "installed_at": records[0]["installed_at"],
        "updated_at": records[0]["updated_at"],
        "enabled": True,
        "source_id": "customer-skillhub",
        "skill_id": "asset-1",
    }]


def test_workspace_state_failed_refresh_preserves_previous_record(
    tmp_path: Path,
) -> None:
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
    from jiuwenswarm.server.runtime.skill.skill_prebuilt import (
        AgentSkillPrebuiltConfig,
        SkillPrebuiltItem,
        SkillPrebuiltSynchronizer,
    )

    workspace = tmp_path / "tenant_ws"
    manager = SkillManager(workspace_dir=str(workspace))
    _write_managed_skill(workspace, "managed-skill")
    old = manager.record_skill_installation(
        name="managed-skill",
        source_type="prebuilt",
        origin="https://example.com/managed.zip",
        skill_id="asset-1",
        version="1.0.0",
    )
    manager.install_skill_sync = lambda *_args: {  # type: ignore[method-assign]
        "ok": False, "detail": "network failed"
    }
    # url 路径按 origin/package_url 决定是否刷新，同 URL 不会因 version 字段重下。
    config = AgentSkillPrebuiltConfig(
        agent_id="bot",
        service_id="svc",
        skills=[SkillPrebuiltItem(
            id="asset-1",
            version="2.0.0",
            source="https://example.com/managed-v2.zip",
        )],
    )

    result = asyncio.run(
        SkillPrebuiltSynchronizer(
            workspace, "svc", "bot", skill_manager=manager
        ).sync(config)
    )

    assert result.ok is False
    assert (workspace / "skills" / "managed-skill" / "SKILL.md").is_file()
    assert manager.list_skill_installations() == [old]
    assert result.enabled_skill_dirs == ["managed-skill"]


# ---------------------------------------------------------------------------
# 管理面 enabled 字段 + builtin 只填真空（不改写 prebuilt/user）
# ---------------------------------------------------------------------------

def test_parse_agent_skill_prebuilt_skips_disabled_items() -> None:
    """管理面禁用的模板项不得下发到租户；缺省 enabled 视为启用."""
    from jiuwenswarm.server.runtime.skill.skill_prebuilt import parse_agent_skill_prebuilt

    config = parse_agent_skill_prebuilt(
        "bot-1",
        "my-svc",
        [
            {
                "skill_id": "asset-disabled",
                "package_url": "https://example.com/disabled.zip",
                "enabled": False,
            },
            {
                "skill_id": "asset-enabled",
                "package_url": "https://example.com/enabled.zip",
                "enabled": True,
            },
            {
                "skill_id": "asset-default",
                "package_url": "https://example.com/default.zip",
            },
        ],
    )

    assert [item.id for item in config.skills] == ["asset-enabled", "asset-default"]
    assert [item.id for item in config.items_with_source] == [
        "asset-enabled",
        "asset-default",
    ]


def _prepare_builtin_repo(tmp_path: Path, name: str) -> Path:
    """构造仓库内置技能目录，返回可作 ``get_builtin_skills_dir()`` 的根路径."""
    builtin_root = tmp_path / "builtin_repo"
    skill_dir = builtin_root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: builtin copy\n---\n# {name}\n",
        encoding="utf-8",
    )
    return builtin_root


def test_register_builtin_skills_does_not_flip_prebuilt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """白名单 sync 已落 prebuilt 后，重启跑 builtin 登记不得改写其类型与实体."""
    from jiuwenswarm.server.runtime.skill import skill_manager as sm
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

    builtin_root = _prepare_builtin_repo(tmp_path, "shared-skill")
    monkeypatch.setattr(sm, "get_builtin_skills_dir", lambda: builtin_root)
    # 第一阶段：个人模式构造（builtin 登记不生效），模拟白名单 sync 落 prebuilt。
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    workspace = tmp_path / "tenant_ws"
    manager = SkillManager(workspace_dir=str(workspace))
    _write_managed_skill(workspace, "shared-skill")
    record = manager.record_skill_installation(
        name="shared-skill",
        source_type="prebuilt",
        origin="https://example.com/shared.zip",
        source="customer-skillhub",
        skill_id="asset-9",
        version="2.0.0",
    )

    # 第二阶段：企业模式重启（AgentManager 重建 → 新 SkillManager 构造）。
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    reloaded = SkillManager(workspace_dir=str(workspace))

    records = reloaded.list_skill_installations()
    assert len(records) == 1
    assert records[0]["source_type"] == "prebuilt"
    assert records[0]["skill_id"] == "asset-9"
    assert records[0]["installation_id"] == record["installation_id"]
    # 内置副本不得覆盖管理面下发的实体内容。
    skill_md = (workspace / "skills" / "shared-skill" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "description: managed" in skill_md
    assert "builtin copy" not in skill_md


def test_workspace_state_cleanup_removes_only_prebuilt(tmp_path: Path) -> None:
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
    from jiuwenswarm.server.runtime.skill.skill_prebuilt import (
        AgentSkillPrebuiltConfig,
        SkillPrebuiltSynchronizer,
    )

    workspace = tmp_path / "tenant_ws"
    manager = SkillManager(workspace_dir=str(workspace))
    for name, source_type in (("managed-skill", "prebuilt"), ("user-skill", "user")):
        _write_managed_skill(workspace, name)
        manager.record_skill_installation(
            name=name,
            source_type=source_type,
            origin=f"https://example.com/{name}.zip",
        )

    result = asyncio.run(
        SkillPrebuiltSynchronizer(
            workspace, "svc", "bot", skill_manager=manager
        ).sync(AgentSkillPrebuiltConfig(agent_id="bot", service_id="svc"))
    )

    assert result.ok is True
    assert not (workspace / "skills" / "managed-skill").exists()
    assert (workspace / "skills" / "user-skill" / "SKILL.md").is_file()
    assert [row["name"] for row in manager.list_skill_installations()] == ["user-skill"]
    assert result.enabled_skill_dirs == ["user-skill"]
