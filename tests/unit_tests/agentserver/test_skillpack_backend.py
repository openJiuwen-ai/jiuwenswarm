# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SDD-0010 SkillPack backend contracts."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from filelock import FileLock, Timeout

from jiuwenswarm.server.runtime.marketplace.hub_models import HubArtifact
from jiuwenswarm.server.runtime.marketplace.hub_package_downloader import HubDownloadError, HubPackageDownloader
from jiuwenswarm.server.runtime.skill import skill_manager as manager_module
from jiuwenswarm.server.runtime.skill.skill_manager import (
    ERROR_SKILL_INVALID_METADATA,
    ERROR_SKILL_OPERATION_UNSUPPORTED,
    SkillManager,
    SkillRpcError,
)
from jiuwenswarm.server.runtime.skill.skill_type import (
    SKILL_TYPE_SKILLPACK,
    detect_skill_type,
)
from jiuwenswarm.server.runtime.skill.skilldev.state_utils import (
    load_execution_disabled_skills,
)
from jiuwenswarm.server.runtime.skill.skillpack import (
    SkillPackOperationUnsupportedError,
    SkillPackService,
    SkillPackValidationError,
    load_skillpack,
    read_skill_kind,
)
from jiuwenswarm.server.runtime.skill.skillpack_install import (
    SkillPackInstaller,
    inventory_mutex,
    read_hub_provenance,
)
from jiuwenswarm.server.runtime.skill.skillpack_inventory import skillpack_inventory_snapshot
from jiuwenswarm.server.runtime.skill.skillpack_lock import LOCK_SCHEMA, load_skillpack_lock


_SECTIONS = """# Demo Pack

## When to use

Use for the complete task.

## Do not use

Do not use for one step.

## Required inputs

The user goal.

## Side effects and confirmation

Follow member permissions.

## Included Skills

- `first-skill`
- `second-skill`

## Execution Process

Run the members in order.

## Failure handling

Return partial results.

## Final output

Return the combined result.
"""


def _write_skill(
    root: Path,
    name: str,
    *,
    enabled_kind: str = "",
    description: str = "member",
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    kind = f"kind: {enabled_kind}\n" if enabled_kind else ""
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{kind}---\n# {name}\n",
        encoding="utf-8",
    )


def _write_pack(
    root: Path,
    *,
    name: str = "demo-pack",
    members: tuple[str, ...] = ("first-skill", "second-skill"),
    workflow_graph: dict | None = None,
    body: str = _SECTIONS,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    member_yaml = "\n".join(f"  - {member}" for member in members)
    graph_section = ""
    if workflow_graph is not None:
        graph_section = (
            "\n## Workflow Graph\n\n```json\n" + json.dumps(workflow_graph) + "\n```\n"
        )
    (root / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "kind: skillpack\n"
        "description: complete demo workflow\n"
        "skills:\n"
        f"{member_yaml}\n"
        "---\n"
        f"{body}{graph_section}",
        encoding="utf-8",
    )


def _workflow() -> dict:
    return {
        "graph": {
            "id": "demo",
            "type": "skillpack_workflow",
            "directed": True,
            "nodes": {
                "first": {"metadata": {"skill": "first-skill"}},
                "second": {"metadata": {"skill": "second-skill"}},
            },
            "edges": [{"source": "first", "target": "second", "relation": "can_feed"}],
        }
    }


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SkillManager:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    state_file = skills_dir / "skills_state.json"
    state_file.write_text(
        json.dumps(
            {
                "marketplaces": [],
                "installed_plugins": [],
                "local_skills": [],
                "skill_configs": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_agent_skills_dir",
        lambda: skills_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_builtin_skills_dir",
        lambda: tmp_path / "builtin_missing",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager._get_agent_root_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager._get_marketplace_dir",
        lambda: skills_dir / "_marketplace",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager._get_state_file",
        lambda: state_file,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skilldev.state_utils.get_state_file",
        lambda: state_file,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skilldev.state_utils.get_agent_skills_dir",
        lambda: skills_dir,
    )
    return SkillManager()


def _install_demo_pack(manager: SkillManager, *, graph: bool = True) -> None:
    _write_skill(manager._skills_dir / "first-skill", "first-skill")
    _write_skill(manager._skills_dir / "second-skill", "second-skill")
    _write_pack(
        manager._skills_dir / "demo-pack",
        workflow_graph=_workflow() if graph else None,
    )


def _install_container_pack(manager: SkillManager) -> None:
    """写入容器型技能包（zip 解压形态：根下无 SKILL.md，成员在 skills/ 下）。"""
    container = manager._skills_dir / "container-pack"
    _write_skill(container / "skills" / "first-skill", "first-skill")
    _write_skill(container / "skills" / "second-skill", "second-skill")


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ("---\nname: ordinary\ndescription: 普通技能\n---\n# Skill\n", ""),
        ("---\nkind: skill\n---\n# Skill\n", "skill"),
        ("---\nkind: '  SkIlLpAcK  '\n---\n# 技能包\n", "skillpack"),
        ("# Ordinary Skill\n普通 Markdown，无 YAML frontmatter。\n", ""),
    ],
)
def test_read_skill_kind_accepts_readable_roots(
    tmp_path: Path, strict: bool, document: str, expected: str,
) -> None:
    (tmp_path / "SKILL.md").write_text(document, encoding="utf-8")

    assert read_skill_kind(tmp_path, strict=strict) == expected


@pytest.mark.parametrize("target", ["none", "missing_directory", "missing_root", "file"])
def test_read_skill_kind_strict_rejects_missing_roots(tmp_path: Path, target: str) -> None:
    skill_dir = tmp_path
    if target == "none":
        skill_dir = None
    elif target == "missing_directory":
        skill_dir = tmp_path / "missing"
    elif target == "file":
        skill_dir = tmp_path / "not-a-directory"
        skill_dir.write_text("not a skill root", encoding="utf-8")

    assert read_skill_kind(skill_dir) == ""
    with pytest.raises(SkillPackValidationError, match="SKILL.md"):
        read_skill_kind(skill_dir, strict=True)


def test_read_skill_kind_strict_rejects_unreadable_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_skill(tmp_path, "ordinary")
    original_read = Path.read_text

    def read_text(path: Path, *args, **kwargs):
        if path == tmp_path / "SKILL.md":
            raise PermissionError("permission denied")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    assert read_skill_kind(tmp_path) == ""
    with pytest.raises(SkillPackValidationError, match="无法读取 SKILL.md") as failure:
        read_skill_kind(tmp_path, strict=True)
    assert isinstance(failure.value.__cause__, PermissionError)


def test_read_skill_kind_strict_rejects_invalid_utf8(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_bytes(b"\xff\xfe")

    assert read_skill_kind(tmp_path) == ""
    with pytest.raises(SkillPackValidationError, match="无法读取 SKILL.md") as failure:
        read_skill_kind(tmp_path, strict=True)
    assert isinstance(failure.value.__cause__, UnicodeDecodeError)


@pytest.mark.parametrize(
    ("frontmatter", "legacy_kind", "error"),
    [
        ("kind: skillpack\nskills: [", "skillpack", "YAML 无效"),
        ("[skillpack]", "", "必须是 YAML 对象"),
        ("null", "", "必须是 YAML 对象"),
        ("ordinary", "", "必须是 YAML 对象"),
    ],
)
def test_read_skill_kind_strict_rejects_invalid_yaml_without_changing_legacy_fallback(
    tmp_path: Path, frontmatter: str, legacy_kind: str, error: str,
) -> None:
    (tmp_path / "SKILL.md").write_text(
        f"---\n{frontmatter}\n---\n# Skill\n", encoding="utf-8",
    )

    assert read_skill_kind(tmp_path) == legacy_kind
    with pytest.raises(SkillPackValidationError, match=error):
        read_skill_kind(tmp_path, strict=True)


def test_read_skill_kind_strict_rejects_unclosed_frontmatter(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text("---\nkind: skillpack\n", encoding="utf-8")

    assert read_skill_kind(tmp_path) == ""
    with pytest.raises(SkillPackValidationError, match="缺少合法 YAML frontmatter"):
        read_skill_kind(tmp_path, strict=True)


def test_skillpack_service_reads_current_enabled_state(manager: SkillManager) -> None:
    _install_demo_pack(manager)
    pack_dir = manager._skills_dir / "demo-pack"
    enabled: dict[str, bool] = {}
    service = SkillPackService(
        manager._skills_dir,
        enabled_for=lambda name: enabled.get(name, True),
        resolve_skill_dir=manager._resolve_local_skill_dir,
    )

    _, initial = service.definition_status(pack_dir, expected_name="demo-pack")
    assert initial.enabled is True
    enabled["first-skill"] = False
    payload: dict[str, object] = {"name": "demo-pack"}
    service.apply_projection(payload, pack_dir, include_members=True)
    assert payload["enabled"] is False
    assert payload["requested_enabled"] is True
    assert payload["blocked_members"] == [{"name": "first-skill", "reason": "disabled"}]
    enabled["first-skill"] = True
    assert service.impacts(["demo-pack"]) == [
        {"name": "demo-pack", "blocked_members": []}
    ]


def test_skillpack_service_impacts_preserve_invalid_and_missing_cases(
    manager: SkillManager,
) -> None:
    _write_pack(manager._skills_dir / "demo-pack", members=("first-skill",))

    assert manager._skillpacks.impacts(["missing-pack", "demo-pack"]) == [
        {
            "name": "demo-pack",
            "blocked_members": [{"name": "demo-pack", "reason": "invalid"}],
        }
    ]


def test_skillpack_service_rejects_only_package_operations(
    manager: SkillManager,
) -> None:
    _install_demo_pack(manager)

    with pytest.raises(
        SkillPackOperationUnsupportedError,
        match="SkillPack 暂不支持 skills.rebuild: demo-pack",
    ):
        manager._skillpacks.ensure_operation_supported("demo-pack", "skills.rebuild")
    manager._skillpacks.ensure_operation_supported("first-skill", "skills.rebuild")
    manager._skillpacks.ensure_operation_supported("missing-skill", "skills.rebuild")


@pytest.mark.asyncio
async def test_list_and_get_project_skillpack_contract(manager: SkillManager) -> None:
    _install_demo_pack(manager)

    result = await manager.handle_skills_list({})
    pack = next(item for item in result["skills"] if item["name"] == "demo-pack")

    assert detect_skill_type(manager._skills_dir / "demo-pack") == SKILL_TYPE_SKILLPACK
    assert pack["skill_type"] == "skillpack"
    assert pack["member_count"] == 2
    assert pack["requested_enabled"] is True
    assert pack["enabled"] is True
    assert pack["blocked_members"] == []
    assert "first-skill" not in manager.list_execution_disabled_skills()
    assert "second-skill" not in manager.list_execution_disabled_skills()

    detail = await manager.handle_skills_get({"name": "demo-pack"})
    assert [item["name"] for item in detail["skillpack"]["members"]] == [
        "first-skill",
        "second-skill",
    ]
    assert detail["skillpack"]["workflow_graph"] == _workflow()


@pytest.mark.asyncio
async def test_member_and_package_toggles_preserve_package_intent(
    manager: SkillManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_info = Mock()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.logger.info",
        log_info,
    )
    _install_demo_pack(manager, graph=False)

    await manager.handle_skills_toggle({"name": "first-skill", "enabled": False})
    detail = await manager.handle_skills_get({"name": "demo-pack"})
    assert detail["requested_enabled"] is True
    assert detail["enabled"] is False
    assert detail["blocked_members"] == [{"name": "first-skill", "reason": "disabled"}]
    assert "demo-pack" in manager.list_execution_disabled_skills()
    assert "demo-pack" in load_execution_disabled_skills()
    assert "demo-pack" in str(log_info.call_args_list)
    assert "disabled" in str(log_info.call_args_list)

    toggled = await manager.handle_skills_toggle(
        {"name": "demo-pack", "enabled": False}
    )
    assert toggled["requested_enabled"] is False
    assert toggled["enabled"] is False
    assert manager.get_skill_enabled("first-skill") is False

    await manager.handle_skills_toggle({"name": "first-skill", "enabled": True})
    detail = await manager.handle_skills_get({"name": "demo-pack"})
    assert detail["requested_enabled"] is False
    assert detail["enabled"] is False


@pytest.mark.asyncio
async def test_missing_and_invalid_members_block_all_referencing_packs(
    manager: SkillManager,
) -> None:
    _write_skill(
        manager._skills_dir / "first-skill",
        "first-skill",
        enabled_kind="team-skill",
    )
    _write_pack(manager._skills_dir / "demo-pack", workflow_graph=None)
    _write_pack(
        manager._skills_dir / "other-pack",
        name="other-pack",
        workflow_graph=None,
    )

    result = await manager.handle_skills_list({})
    packs = {
        item["name"]: item
        for item in result["skills"]
        if item.get("skill_type") == "skillpack"
    }
    expected = [
        {"name": "first-skill", "reason": "invalid"},
        {"name": "second-skill", "reason": "missing"},
    ]
    assert packs["demo-pack"]["blocked_members"] == expected
    assert packs["other-pack"]["blocked_members"] == expected
    assert set(manager.list_execution_disabled_skills()) >= {
        "demo-pack",
        "other-pack",
    }

    _write_skill(manager._skills_dir / "first-skill", "first-skill")
    _write_skill(manager._skills_dir / "second-skill", "second-skill")
    restored = await manager.handle_skills_list({})
    restored_packs = {
        item["name"]: item
        for item in restored["skills"]
        if item.get("skill_type") == "skillpack"
    }
    assert restored_packs["demo-pack"]["enabled"] is True
    assert restored_packs["other-pack"]["enabled"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("registered", [False, True])
async def test_uninstalling_referenced_member_is_blocked(
    manager: SkillManager,
    monkeypatch: pytest.MonkeyPatch,
    registered: bool,
) -> None:
    log_info = Mock()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.logger.info",
        log_info,
    )
    _install_demo_pack(manager, graph=False)
    await manager.handle_skills_list({})
    if registered:
        original = manager._find_skill_installation

        def installation(*, name, origin=None):
            if name == "first-skill":
                return {"name": name, "entity_dir": name, "source_type": "user", "origin": "local:test"}
            return original(name=name, origin=origin)

        monkeypatch.setattr(manager, "_find_skill_installation", installation)

    result = await manager.handle_skills_uninstall({"name": "first-skill"})

    assert result["success"] is False
    assert result["code"] == "SKILL_REFERENCED_BY_PACK"
    assert result["referencing_skillpacks"] == ["demo-pack"]
    assert (manager._skills_dir / "demo-pack").is_dir()
    assert (manager._skills_dir / "first-skill").is_dir()
    detail = await manager.handle_skills_get({"name": "demo-pack"})
    assert detail["blocked_members"] == []

    await manager.handle_skills_uninstall({"name": "demo-pack"})
    assert (await manager.handle_skills_uninstall({"name": "first-skill"}))["success"]


@pytest.mark.asyncio
async def test_uninstalling_pack_keeps_members(manager: SkillManager) -> None:
    _install_demo_pack(manager, graph=False)

    result = await manager.handle_skills_uninstall({"name": "demo-pack"})

    assert result["success"] is True
    assert not (manager._skills_dir / "demo-pack").exists()
    assert (manager._skills_dir / "first-skill" / "SKILL.md").is_file()
    assert (manager._skills_dir / "second-skill" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_skillpack_files_and_versions_stay_at_package_root(
    manager: SkillManager,
) -> None:
    _install_demo_pack(manager, graph=False)

    files = await manager.handle_skills_files_list({"name": "demo-pack"})
    versions = await manager.handle_skills_versions_list({"name": "demo-pack"})

    assert {item["path"] for item in files["files"]} == {"SKILL.md"}
    assert versions["name"] == "demo-pack"
    assert versions["versions"] == []


@pytest.mark.parametrize(
    ("name", "members"),
    [
        ("demo-pack", ("first-skill",)),
        ("demo-pack", ("first-skill", "first-skill")),
        ("demo-pack", ("demo-pack", "first-skill")),
    ],
)
def test_skillpack_rejects_invalid_member_declarations(
    tmp_path: Path,
    name: str,
    members: tuple[str, ...],
) -> None:
    pack_dir = tmp_path / name
    _write_pack(pack_dir, name=name, members=members, workflow_graph=None)

    with pytest.raises(SkillPackValidationError):
        load_skillpack(pack_dir, expected_name=name)


@pytest.mark.asyncio
async def test_invalid_workflow_is_not_listed_and_get_returns_metadata_error(
    manager: SkillManager,
) -> None:
    _install_demo_pack(manager, graph=False)
    invalid = _workflow()
    invalid["graph"]["edges"][0]["target"] = "missing"
    _write_pack(manager._skills_dir / "demo-pack", workflow_graph=invalid)

    result = await manager.handle_skills_list({})
    assert "demo-pack" not in {item["name"] for item in result["skills"]}
    with pytest.raises(SkillRpcError) as exc:
        await manager.handle_skills_get({"name": "demo-pack"})
    assert exc.value.code == ERROR_SKILL_INVALID_METADATA
    assert "demo-pack" in manager.list_execution_disabled_skills()


@pytest.mark.asyncio
async def test_non_scalar_workflow_refs_fail_closed_without_losing_disabled_skills(
    manager: SkillManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_demo_pack(manager)
    manager._state["local_skills"] = [{"name": "first-skill"}]
    manager._save_state()
    await manager.handle_skills_toggle({"name": "first-skill", "enabled": False})
    invalid = _workflow()
    invalid["graph"]["nodes"]["first"]["metadata"]["skill"] = []
    invalid["graph"]["edges"][0]["source"] = []
    _write_pack(manager._skills_dir / "demo-pack", workflow_graph=invalid)

    with pytest.raises(SkillPackValidationError):
        load_skillpack(manager._skills_dir / "demo-pack")
    assert load_execution_disabled_skills() == ["demo-pack", "first-skill"]

    def _fail_aggregation(*args, **kwargs):
        raise OSError("unavailable SkillPack scan failed")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skillpack.unavailable_skillpacks",
        _fail_aggregation,
    )
    assert load_execution_disabled_skills() == ["first-skill"]


@pytest.mark.asyncio
async def test_skillpack_import_rebuild_and_evolution_are_unsupported(
    manager: SkillManager,
    tmp_path: Path,
    allow_macos_pytest_temp_sources,
) -> None:
    _install_demo_pack(manager, graph=False)
    local_source = tmp_path / "local-pack"
    _write_pack(
        local_source,
        name="local-pack",
        workflow_graph=None,
    )
    archive = tmp_path / "demo-pack.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr(
            "demo-pack/SKILL.md",
            (manager._skills_dir / "demo-pack" / "SKILL.md").read_text(
                encoding="utf-8"
            ),
        )
    archive.write_bytes(buffer.getvalue())

    with pytest.raises(SkillRpcError) as local_imported:
        await manager.handle_skills_import_local({"path": str(local_source)})
    assert local_imported.value.code == ERROR_SKILL_OPERATION_UNSUPPORTED

    with pytest.raises(SkillRpcError) as imported:
        await manager.handle_skills_import_upload({"path": str(archive)})
    assert imported.value.code == ERROR_SKILL_OPERATION_UNSUPPORTED

    with pytest.raises(SkillRpcError) as rebuilt:
        await manager.handle_skills_rebuild({"name": "demo-pack", "version": None})
    assert rebuilt.value.code == ERROR_SKILL_OPERATION_UNSUPPORTED

    with pytest.raises(SkillRpcError) as evolved:
        await manager.handle_skills_evolution_status({"name": "demo-pack"})
    assert evolved.value.code == ERROR_SKILL_OPERATION_UNSUPPORTED


@pytest.mark.asyncio
async def test_container_pack_disabled_member_blocks_pack(manager: SkillManager) -> None:
    """容器型技能包：禁用任一成员 → 整包禁用，与标准包语义一致。"""
    _install_container_pack(manager)

    result = await manager.handle_skills_list({})
    pack = next(
        item for item in result["skills"] if item["name"] == "container-pack"
    )
    assert pack["skill_type"] == "skillpack"
    assert pack["enabled"] is True
    assert pack["blocked_members"] == []
    assert "container-pack" not in manager.list_execution_disabled_skills()

    await manager.handle_skills_toggle({"name": "first-skill", "enabled": False})

    detail = await manager.handle_skills_get({"name": "container-pack"})
    assert detail["requested_enabled"] is True
    assert detail["enabled"] is False
    assert detail["blocked_members"] == [
        {"name": "first-skill", "reason": "disabled"}
    ]
    assert "container-pack" in manager.list_execution_disabled_skills()

    await manager.handle_skills_toggle({"name": "first-skill", "enabled": True})
    restored = await manager.handle_skills_get({"name": "container-pack"})
    assert restored["enabled"] is True
    assert restored["blocked_members"] == []


@pytest.mark.asyncio
async def test_toggle_dry_run_reports_parent_skillpacks_without_side_effects(
    manager: SkillManager,
) -> None:
    """skills.toggle dry_run：实时报告关联技能包，且不落任何状态。"""
    _install_demo_pack(manager)

    # 成员技能：探测返回所属技能包
    probe = await manager.handle_skills_toggle(
        {"name": "first-skill", "enabled": False, "dry_run": True}
    )
    assert probe["success"] is True
    assert probe["parent_skillpacks"] == ["demo-pack"]

    # 无关联的独立技能：探测返回空列表
    _write_skill(manager._skills_dir / "solo-skill", "solo-skill")
    manager._state["local_skills"] = [{"name": "solo-skill"}]
    manager._save_state()
    solo = await manager.handle_skills_toggle(
        {"name": "solo-skill", "enabled": False, "dry_run": True}
    )
    assert solo["success"] is True
    assert solo["parent_skillpacks"] == []

    # 探测不改变状态：成员与技能包均保持启用
    detail = await manager.handle_skills_get({"name": "demo-pack"})
    assert detail["enabled"] is True
    assert detail["blocked_members"] == []
    member = await manager.handle_skills_get({"name": "first-skill"})
    assert member["enabled"] is True


# Thin Pack installation and recovery contracts.


def _zip(name: str, content: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{name}/plugin.yaml", "runtime:\n  type: skill\n")
        for filename, text in content.items():
            archive.writestr(f"{name}/{name}/{filename}", text)
    return buffer.getvalue()


def _root(name: str = "release-pack") -> str:
    sections = (
        "When to use", "Do not use", "Required inputs", "Side effects and confirmation",
        "Included Skills", "Execution Process", "Failure handling", "Final output",
    )
    return (
        f"---\nname: {name}\nkind: skillpack\ndescription: release\nskills: [summary, risk]\n---\n"
        + "\n".join(f"## {section}\nDetails for {section}.\n" for section in sections)
    )


class HubFixture:
    def __init__(self) -> None:
        self.bodies = {}
        self.artifacts = {}
        self.requests = []
        self.hidden = set()
        for name in ("summary", "risk"):
            self.add(name, _zip(name, {"SKILL.md": f"---\nname: {name}\ndescription: ordinary\n---\n"}))
        self.lock = {
            "schema_version": LOCK_SCHEMA, "pack": {"name": "release-pack", "version": "1.0.0"},
            "members": [
                {"name": name, "asset_id": name, "version": "1.0.0", "artifact_sha256": self.artifacts[name].checksum_sha256}
                for name in ("summary", "risk")
            ],
        }
        self.build_pack()

    def add(self, name: str, body: bytes) -> None:
        self.bodies[f"https://localhost/{name}.zip"] = body
        self.artifacts[name] = HubArtifact(
            asset_id=name, plugin_type="skill", version="1.0.0", name=name,
            download_url=f"https://localhost/{name}.zip", checksum_sha256=hashlib.sha256(body).hexdigest(),
        )

    def build_pack(self) -> None:
        self.add("release-pack", _zip("release-pack", {"SKILL.md": _root(), "skillpack.lock.json": json.dumps(self.lock)}))

    async def get_version(self, asset_id, version):
        if asset_id in self.hidden:
            raise ValueError("not public/approved")
        artifact = self.artifacts[asset_id]
        assert artifact.version == version
        return SimpleNamespace(plugin_type=artifact.plugin_type, name=artifact.name)

    async def get_artifact(self, asset_id, *, version, is_cli_download=False):
        assert is_cli_download is True
        assert version == self.artifacts[asset_id].version
        self.requests.append((asset_id, version, is_cli_download))
        if asset_id in self.hidden:
            raise ValueError("not public/approved")
        return self.artifacts[asset_id]

    async def fetch(self, url):
        return self.bodies[url]


@pytest.fixture
def environment(tmp_path):
    hub = HubFixture()
    skills = tmp_path / "skills"
    state = skills / "skills_state.json"
    installer = SkillPackInstaller(
        skills, "https://localhost", hub, state_files=(state,),
        downloader=HubPackageDownloader(fetch_bytes=hub.fetch),
    )
    return hub, skills, state, installer


async def _prepare(hub, installer):
    artifact = hub.artifacts["release-pack"]
    return await installer.prepare(artifact, hub.bodies[artifact.download_url])


@pytest.mark.asyncio
@pytest.mark.parametrize("digest", ["", "invalid", "0" * 64, None])
async def test_prepare_rejects_invalid_root_digest_before_resolving_members(environment, digest):
    hub, skills, _, installer = environment
    artifact = replace(hub.artifacts["release-pack"], checksum_sha256=digest)
    with pytest.raises(HubDownloadError, match="SHA256"):
        await installer.prepare(artifact, hub.bodies[artifact.download_url])
    assert not hub.requests
    assert not (installer.work_dir / "plans").exists()
    assert not (skills / "release-pack").exists()


@pytest.mark.asyncio
async def test_prepare_accepts_normalized_root_digest(environment):
    hub, _, _, installer = environment
    artifact = hub.artifacts["release-pack"]
    normalized = replace(artifact, checksum_sha256=f" {artifact.checksum_sha256.upper()} ")
    assert (await installer.prepare(normalized, hub.bodies[artifact.download_url]))["can_install"]


@pytest.mark.asyncio
@pytest.mark.parametrize("created_at", [True, None, "123", [], float("nan"), float("inf")])
async def test_commit_rejects_invalid_plan_timestamp_before_downloading(environment, created_at):
    hub, skills, _, installer = environment
    plan = await _prepare(hub, installer)
    path = installer.work_dir / "plans" / plan["plan_id"] / "plan.json"
    stored = json.loads(path.read_text())
    stored["created_at"] = created_at
    path.write_text(json.dumps(stored))
    hub.requests.clear()
    with pytest.raises(SkillPackValidationError, match="Invalid SkillPack install plan"):
        await installer.commit(plan["plan_id"], on_committed=lambda records: None)
    assert not hub.requests
    assert not (skills / "release-pack").exists()


@pytest.mark.asyncio
async def test_prepare_and_commit_create_independent_archived_members(environment):
    hub, skills, state, installer = environment
    plan = await _prepare(hub, installer)
    assert plan["confirmation_required"] and plan["can_install"]
    assert not (skills / "release-pack").exists()
    assert not state.exists()
    result = await installer.commit(plan["plan_id"], on_committed=lambda records: state.write_text("registered"))
    assert result["success"]
    assert load_skillpack(skills / "release-pack").members == ("summary", "risk")
    assert load_skillpack_lock(skills / "release-pack").members[0].asset_id == "summary"
    for name in ("release-pack", "summary", "risk"):
        assert (skills / name / ".archive/versions/index.json").exists()
        assert read_hub_provenance(skills / name)["artifact_sha256"] == hub.artifacts[name].checksum_sha256
    assert not (skills / "release-pack/summary").exists()
    second = await _prepare(hub, installer)
    assert {record["action"] for record in second["install_plan"]} == {"reuse"}


@pytest.mark.asyncio
async def test_registration_failure_rolls_back_all_directories_and_state(environment):
    hub, skills, state, installer = environment
    state.write_text("before")
    plan = await _prepare(hub, installer)

    def fail(records):
        state.write_text("partial")
        raise RuntimeError("index refresh failed")

    with pytest.raises(RuntimeError, match="index refresh"):
        await installer.commit(plan["plan_id"], on_committed=fail)
    assert state.read_text() == "before"
    assert not any((skills / name).exists() for name in ("release-pack", "summary", "risk"))
    assert (await installer.commit(plan["plan_id"], on_committed=lambda records: None))["success"]


@pytest.mark.asyncio
async def test_failed_rollback_retains_all_directories_and_original_error(environment):
    hub, skills, state, installer = environment
    state.write_text("before")
    plan = await _prepare(hub, installer)

    def fail(records):
        (skills / "risk/.archive/install-transaction").write_text("foreign-transaction")
        raise RuntimeError("registration failed")

    with pytest.raises(RuntimeError, match="registration failed") as error:
        await installer.commit(plan["plan_id"], on_committed=fail)
    assert "changed SkillPack destination" in str(error.value.__cause__)
    assert all((skills / name).is_dir() for name in ("summary", "risk", "release-pack"))
    transaction = next((installer.work_dir / "transactions").iterdir())
    assert (transaction / "state-0").read_text() == "before"
    with pytest.raises(RuntimeError, match="retained transaction"):
        installer.recover()
    assert transaction.is_dir() and (skills / "summary").is_dir()


@pytest.mark.parametrize("journal", [
    "{broken", "[]", '{}', '{"state":[]}',
    '{"state":"committing","new_names":["../outside"],"state_existed":[false]}',
    '{"state":"committing","new_names":[],"state_existed":[]}',
    '{"state":"committing","new_names":[],"state_existed":[1]}',
    '{"state":"committing","new_names":[null],"state_existed":[false]}',
    '{"state":"committing","new_names":[["summary"]],"state_existed":[false]}',
    '{"state":"committing","new_names":["summary","summary"],"state_existed":[false]}',
])
def test_corrupt_journal_is_retained_without_deleting_inventory(environment, journal):
    _, skills, state, installer = environment
    state.write_text("untouched")
    transaction = installer.work_dir / "transactions/broken"
    transaction.mkdir(parents=True)
    (transaction / "journal.json").write_text(journal)
    with pytest.raises(RuntimeError, match="retained transaction"):
        installer.recover()
    assert state.read_text() == "untouched"
    assert (transaction / "journal.json").read_text() == journal


@pytest.mark.asyncio
async def test_inventory_mutation_holds_lock_in_worker_thread(tmp_path):
    import asyncio
    import threading
    from filelock import Timeout
    from jiuwenswarm.server.runtime.skill.skillpack_install import inventory_mutation, inventory_mutex

    started, release = threading.Event(), threading.Event()

    class Manager:
        skills_dir = tmp_path

        def reload_state(self):
            pass

        @inventory_mutation
        def change(self, params):
            started.set()
            assert release.wait(5)

    task = asyncio.create_task(asyncio.to_thread(Manager().change, {}))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        with pytest.raises(Timeout):
            with inventory_mutex(tmp_path):
                pytest.fail("Concurrent install must not enter the inventory")
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_member_download_corruption_never_writes_inventory(environment):
    hub, skills, _, installer = environment
    plan = await _prepare(hub, installer)
    hub.bodies[hub.artifacts["risk"].download_url] += b"changed"
    with pytest.raises(Exception, match="SHA256"):
        await installer.commit(plan["plan_id"], on_committed=lambda records: None)
    assert not (skills / "release-pack").exists()
    assert not (skills / "summary").exists()


@pytest.mark.asyncio
async def test_member_removed_after_confirmation_blocks(environment):
    hub, skills, _, installer = environment
    plan = await _prepare(hub, installer)
    hub.hidden.add("risk")
    with pytest.raises(ValueError, match="approved"):
        await installer.commit(plan["plan_id"], on_committed=lambda records: None)
    assert not (skills / "release-pack").exists()


@pytest.mark.asyncio
async def test_local_name_conflict_and_changed_inventory_are_not_overwritten(environment):
    hub, skills, _, installer = environment
    plan = await _prepare(hub, installer)
    (skills / "risk").mkdir()
    (skills / "risk/SKILL.md").write_text("user content")
    with pytest.raises(SkillPackValidationError, match="inventory changed") as error:
        await installer.commit(plan["plan_id"], on_committed=lambda records: None)
    assert error.value.code == "SKILLPACK_PLAN_CHANGED"
    assert (skills / "risk/SKILL.md").read_text() == "user content"
    new_plan = await _prepare(hub, installer)
    assert new_plan["can_install"] is False
    assert new_plan["install_plan"][-1]["action"] == "blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expired", "inventory", "integrity", "missing_plan", "malformed", "wrong_hub"])
async def test_manager_confirmation_returns_typed_failure_without_writing(environment, monkeypatch, change):
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

    hub, skills, _, installer = environment
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: {
        "symphony": {"evolution": {"flow": {"enabled": True}}},
    })
    plan = await _prepare(hub, installer)
    directory = installer.work_dir / "plans" / plan["plan_id"]
    if change == "expired":
        stored = json.loads((directory / "plan.json").read_text())
        stored["created_at"] = 0
        (directory / "plan.json").write_text(json.dumps(stored))
    elif change == "inventory":
        (skills / "risk").mkdir()
    elif change == "integrity":
        (directory / "root/SKILL.md").write_text(_root() + "\nchanged\n")
    elif change == "missing_plan":
        (directory / "plan.json").unlink()
    elif change == "malformed":
        (directory / "plan.json").write_text("[]")
    else:
        stored = json.loads((directory / "plan.json").read_text())
        stored["hub_url"] = "https://different.example"
        (directory / "plan.json").write_text(json.dumps(stored))
    manager = SkillManager(workspace_dir=str(skills.parent / "manager"))
    monkeypatch.setattr(manager, "_skillpack_installer", lambda _: installer)
    result = await manager.handle_skills_team_skills_hub_install({
        "asset_id": "release-pack", "plan_id": plan["plan_id"], "confirmed": True,
    })
    assert result["success"] is False
    invalid = change in ("integrity", "malformed", "wrong_hub")
    assert result["code"] == ("SKILLPACK_VALIDATION_FAILED" if invalid else "SKILLPACK_PLAN_CHANGED")
    assert result["detail_key"] == ("skillPackInstall.validationFailed" if invalid else "skillPackInstall.changed")
    assert not (skills / "release-pack").exists()
    assert not (skills / "summary").exists()


@pytest.mark.asyncio
async def test_discovery_off_blocks_new_confirmation_but_not_accepted_install(environment, monkeypatch):
    hub, skills, _, installer = environment
    config = {"symphony": {"evolution": {"flow": {"enabled": False}}}}
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: config)
    # Old deployment settings cannot bypass the persisted UI switch.
    monkeypatch.setenv("SKILLPACK_PRODUCTIZATION_ENABLED", "true")
    plan = await _prepare(hub, installer)
    manager = SkillManager(workspace_dir=str(skills.parent / "manager"))
    monkeypatch.setattr(manager, "_skillpack_installer", lambda _: installer)
    monkeypatch.setattr(manager, "_register_skillpack_install", lambda records: None)
    params = {"asset_id": "release-pack", "plan_id": plan["plan_id"], "confirmed": True}
    refused = await manager.handle_skills_team_skills_hub_install(params)
    assert refused["success"] is False
    assert not (skills / "release-pack").exists()

    entered, resume = asyncio.Event(), asyncio.Event()
    original_commit = installer.commit

    async def delayed_commit(*args, **kwargs):
        entered.set()
        await resume.wait()
        return await original_commit(*args, **kwargs)

    monkeypatch.setattr(installer, "commit", delayed_commit)
    config["symphony"]["evolution"]["flow"]["enabled"] = True
    task = asyncio.create_task(manager.handle_skills_team_skills_hub_install(params))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        config["symphony"]["evolution"]["flow"]["enabled"] = False
    finally:
        resume.set()
    result = await task
    assert result["success"] is True
    assert (skills / "release-pack/SKILL.md").exists()
    assert (skills / "summary/SKILL.md").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["skillpack", "swarm-skill", "team-skill", "tool"])
async def test_actual_member_root_must_be_ordinary_even_with_hub_skill_type(environment, kind):
    hub, skills, _, installer = environment
    hub.add("risk", _zip("risk", {"SKILL.md": f"---\nname: risk\ndescription: risk\nkind: {kind}\n---\n"}))
    hub.lock["members"][1]["artifact_sha256"] = hub.artifacts["risk"].checksum_sha256
    hub.build_pack()
    plan = await _prepare(hub, installer)
    with pytest.raises(ValueError, match="ordinary"):
        await installer.commit(plan["plan_id"], on_committed=lambda records: None)
    assert not (skills / "summary").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["order", "digest", "duplicate", "range", "wildcard", "version"])
async def test_invalid_lock_blocks_before_confirmation(environment, mutation):
    hub, skills, _, installer = environment
    if mutation == "order":
        hub.lock["members"].reverse()
    elif mutation == "digest":
        hub.lock["members"][0]["artifact_sha256"] = "invalid"
    elif mutation == "duplicate":
        hub.lock["members"][1]["asset_id"] = "summary"
    elif mutation == "range":
        hub.lock["members"][0]["version"] = "latest"
    elif mutation == "wildcard":
        hub.lock["members"][0]["version"] = "1.*"
    else:
        hub.lock["pack"]["version"] = "2.0.0"
    hub.build_pack()
    with pytest.raises(ValueError):
        await _prepare(hub, installer)
    assert not (skills / "release-pack").exists()


@pytest.mark.asyncio
async def test_local_edit_changes_inventory_revision_and_prevents_reuse(environment):
    hub, skills, _, installer = environment
    plan = await _prepare(hub, installer)
    await installer.commit(plan["plan_id"], on_committed=lambda records: None)
    before = skillpack_inventory_snapshot(skills, lambda name: True)
    (skills / "risk/SKILL.md").write_text("changed")
    after = skillpack_inventory_snapshot(skills, lambda name: True)
    assert before["inventory_revision"] != after["inventory_revision"]
    assert next(record for record in after["skills"] if record["id"] == "release-pack")["enabled"] is False
    plan = await _prepare(hub, installer)
    assert plan["can_install"] is False


def test_recover_interrupted_transaction_restores_before_state(environment):
    _, skills, state, installer = environment
    transaction = skills / ".skillpack/transactions/interrupted"
    transaction.mkdir(parents=True)
    (transaction / "state-0").write_text("original")
    state.write_text("partial")
    (transaction / "journal.json").write_text(json.dumps({
        "state": "committing", "new_names": ["summary"], "state_existed": [True],
    }))
    (skills / "summary/.archive").mkdir(parents=True)
    (skills / "summary/.archive/install-transaction").write_text("interrupted")
    installer.recover()
    assert state.read_text() == "original"
    assert not (skills / "summary").exists()
    installer.recover()


@pytest.mark.asyncio
async def test_confirmation_cannot_be_used_for_another_asset(environment):
    hub, _, _, installer = environment
    plan = await _prepare(hub, installer)
    with pytest.raises(ValueError, match="confirmation asset"):
        await installer.commit(plan["plan_id"], on_committed=lambda records: None, expected_asset_id="other")


@pytest.mark.asyncio
async def test_manager_marketplace_confirm_and_shared_member_lifecycle(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: {
        "symphony": {"evolution": {"flow": {"enabled": True}}},
    })
    monkeypatch.setenv("TEAM_SKILLS_HUB_BASE_URL", "https://localhost")
    hub = HubFixture()
    manager = SkillManager(workspace_dir=str(tmp_path))
    monkeypatch.setattr(manager, "_get_mirror_skills_dirs", lambda: [])

    def installer(base_url):
        return SkillPackInstaller(
            manager._skills_dir, base_url, hub,
            state_files=(manager._state_file, tmp_path / "agent-data.json"),
            downloader=HubPackageDownloader(fetch_bytes=hub.fetch),
            enabled_for=manager.get_skill_enabled,
        )

    async def get_data(path, *, params, timeout, base_url):
        assert params["is_cli_download"] is True
        return asdict(hub.artifacts[path.rsplit("/", 1)[-1]])

    async def download(url, *, checksum_sha256):
        return await hub.fetch(url)

    monkeypatch.setattr(manager, "_skillpack_installer", installer)
    monkeypatch.setattr(manager, "_team_skills_hub_http_get_data", get_data)
    monkeypatch.setattr(manager, "_download_zip_and_verify", download)
    replaced = await manager.handle_skills_online_search_install({
        "source": "teamskillshub", "identifier": "summary", "expected_skill_type": "skillpack",
    })
    assert replaced["code"] == "SKILLPACK_VALIDATION_FAILED"
    assert not (manager._skills_dir / "summary").exists()
    params = {"source": "teamskillshub", "identifier": "release-pack"}
    plan = await manager.handle_skills_online_search_install(params)
    assert plan["confirmation_required"]
    assert not (manager._skills_dir / "release-pack").exists()
    assert not (await manager.handle_skills_online_search_install(dict(params, plan_id=plan["plan_id"])))["success"]
    result = await manager.handle_skills_online_search_install(dict(params, plan_id=plan["plan_id"], confirmed=True))
    assert result["success"]
    details = await manager.handle_skills_get({"name": "release-pack"})
    assert details["skill_type"] == "skillpack"
    assert details["skillpack"]["members"][0]["version"] == "1.0.0"
    assert details["enabled"]
    second_lock = dict(hub.lock, pack={"name": "other-pack", "version": "1.0.0"})
    hub.add("other-pack", _zip("other-pack", {"SKILL.md": _root("other-pack"), "skillpack.lock.json": json.dumps(second_lock)}))
    second_params = {"source": "teamskillshub", "identifier": "other-pack"}
    second_plan = await manager.handle_skills_online_search_install(second_params)
    assert [item["action"] for item in second_plan["install_plan"]] == ["download", "reuse", "reuse"]
    await manager.handle_skills_online_search_install(dict(second_params, plan_id=second_plan["plan_id"], confirmed=True))
    assert (await manager.handle_skills_uninstall({"name": "release-pack"}))["success"]
    denied = await manager.handle_skills_uninstall({"name": "summary"})
    assert denied["referencing_skillpacks"] == ["other-pack"]
    assert (await manager.handle_skills_uninstall({"name": "other-pack"}))["success"]
    assert (manager._skills_dir / "summary/SKILL.md").exists()
    assert (await manager.handle_skills_uninstall({"name": "summary"}))["success"]


# SkillNet background writes share the inventory transaction boundary.


@pytest.fixture
def inventory_commit_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "data"))
    instance = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    monkeypatch.setattr(instance, "_get_mirror_skills_dirs", lambda: [])
    yield instance
    instance._skillnet_install_jobs.clear()


def _download_skill(
    monkeypatch, inventory_commit_manager, *, name="first-skill", on_download=lambda: None,
):
    def download(_url, temporary, _mirror):
        on_download()
        root = Path(temporary) / name
        _write_skill(root, name, description="new downloaded content")
        return str(root)

    monkeypatch.setattr(inventory_commit_manager, "_skillnet_download_sync", download)


@pytest.mark.asyncio
async def test_download_is_unlocked_but_files_state_and_index_commit_hold_one_lock(
    inventory_commit_manager, monkeypatch,
):
    lock_path = inventory_mutex(inventory_commit_manager._skills_dir).lock_file
    events = []

    def unlocked(stage):
        with FileLock(lock_path, timeout=0):
            events.append(stage)

    def locked(stage):
        with pytest.raises(Timeout):
            with FileLock(lock_path, timeout=0):
                pass
        events.append(stage)

    _download_skill(monkeypatch, inventory_commit_manager, on_download=lambda: unlocked("download"))
    original_copy = manager_module.shutil.copytree

    def copy(source, destination, *args, **kwargs):
        if Path(destination) == inventory_commit_manager._skills_dir / "first-skill":
            locked("copy")
        return original_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(manager_module.shutil, "copytree", copy)
    for method_name, stage in [
        ("_add_local_skill", "local"), ("_add_installed_plugin", "plugin"),
        ("_refresh_agent_data_indexes", "index"),
    ]:
        original = getattr(inventory_commit_manager, method_name)

        def wrapped(*args, _original=original, _stage=stage, **kwargs):
            locked(_stage)
            return _original(*args, **kwargs)

        monkeypatch.setattr(inventory_commit_manager, method_name, wrapped)

    async def complete():
        unlocked("hook")

    inventory_commit_manager._skillnet_install_complete_hook = complete
    # Another manager can update the inventory while this instance is stale.
    _write_skill(inventory_commit_manager._skills_dir / "previous-skill", "previous-skill")
    other = SkillManager(workspace_dir=str(inventory_commit_manager._agent_root))
    other._add_local_skill({"name": "previous-skill", "source": "local"})
    await inventory_commit_manager._skillnet_install_background(
        "install", "https://example.test/skill", False,
    )

    assert inventory_commit_manager._skillnet_install_jobs["install"]["status"] == "done"
    assert events == ["download", "copy", "local", "plugin", "index", "hook"]
    state = json.loads(inventory_commit_manager._state_file.read_text())
    assert {item["name"] for item in state["local_skills"]} == {"previous-skill", "first-skill"}
    assert any(item["name"] == "first-skill" for item in state["installed_plugins"])


@pytest.mark.asyncio
async def test_force_cannot_overwrite_a_member_used_by_standard_pack(
    inventory_commit_manager, monkeypatch,
):
    _write_skill(
        inventory_commit_manager._skills_dir / "first-skill", "first-skill", description="original",
    )
    _write_skill(inventory_commit_manager._skills_dir / "second-skill", "second-skill")
    _write_pack(inventory_commit_manager._skills_dir / "demo-pack")
    original = (inventory_commit_manager._skills_dir / "first-skill/SKILL.md").read_bytes()
    before_state = (
        inventory_commit_manager._state_file.read_bytes()
        if inventory_commit_manager._state_file.exists() else None
    )
    _download_skill(monkeypatch, inventory_commit_manager)

    await inventory_commit_manager._skillnet_install_background(
        "protected", "https://example.test/skill", True,
    )

    job = await inventory_commit_manager.handle_skills_skillnet_install_status({"install_id": "protected"})
    assert job["status"] == "failed"
    assert job["code"] == "SKILL_REFERENCED_BY_PACK"
    assert "demo-pack" in job["detail"]
    assert (inventory_commit_manager._skills_dir / "first-skill/SKILL.md").read_bytes() == original
    after_state = (
        inventory_commit_manager._state_file.read_bytes()
        if inventory_commit_manager._state_file.exists() else None
    )
    assert after_state == before_state


@pytest.mark.asyncio
async def test_force_keeps_existing_behavior_for_unreferenced_skill(
    inventory_commit_manager, monkeypatch,
):
    _write_skill(
        inventory_commit_manager._skills_dir / "first-skill", "first-skill", description="original",
    )
    _download_skill(monkeypatch, inventory_commit_manager)

    await inventory_commit_manager._skillnet_install_background(
        "replace", "https://example.test/skill", True,
    )

    assert inventory_commit_manager._skillnet_install_jobs["replace"]["status"] == "done"
    assert "new downloaded content" in (
        inventory_commit_manager._skills_dir / "first-skill/SKILL.md"
    ).read_text()


@pytest.mark.asyncio
async def test_another_inventory_transaction_blocks_worker_commit_without_writes(
    inventory_commit_manager, monkeypatch,
):
    downloads = []
    _download_skill(monkeypatch, inventory_commit_manager, on_download=lambda: downloads.append(True))
    with inventory_mutex(inventory_commit_manager._skills_dir):
        await inventory_commit_manager._skillnet_install_background(
            "busy", "https://example.test/skill", False,
        )

    assert downloads == [True]
    assert inventory_commit_manager._skillnet_install_jobs["busy"]["status"] == "failed"
    assert inventory_commit_manager._skillnet_install_jobs["busy"]["code"] == "SKILL_INVENTORY_BUSY"
    assert inventory_commit_manager._skillnet_install_jobs["busy"]["detail_key"] == "skills.inventoryBusy"
    assert "inventory.lock" not in inventory_commit_manager._skillnet_install_jobs["busy"]["detail"]
    assert not (inventory_commit_manager._skills_dir / "first-skill").exists()
    inventory_commit_manager.reload_state()
    assert not any(
        item.get("name") == "first-skill" for item in inventory_commit_manager._state.get("local_skills", [])
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("enterprise", [False, True])
@pytest.mark.parametrize("operation", ["toggle", "uninstall"])
async def test_skill_mutations_run_off_event_loop_and_report_lock_contention(
    inventory_commit_manager, monkeypatch, enterprise, operation,
):
    import threading

    monkeypatch.setattr(manager_module, "is_enterprise", lambda: enterprise)
    loop_thread = threading.get_ident()
    worker_threads = []
    target = "_toggle_skills_locked" if operation == "toggle" else "_handle_enterprise_skills_uninstall"
    original = getattr(inventory_commit_manager, target)

    def record_thread(params):
        worker_threads.append(threading.get_ident())
        return original(params)

    monkeypatch.setattr(inventory_commit_manager, target, record_thread)
    _write_skill(inventory_commit_manager._skills_dir / "first-skill", "first-skill")
    before = (inventory_commit_manager._skills_dir / "first-skill/SKILL.md").read_bytes()
    with inventory_mutex(inventory_commit_manager._skills_dir):
        result = await getattr(inventory_commit_manager, f"handle_skills_{operation}")(
            {"name": "first-skill", "enabled": False}
        )
    assert worker_threads and worker_threads[0] != loop_thread
    assert result["success"] is False
    assert result["code"] == "SKILL_INVENTORY_BUSY"
    assert result["detail_key"] == "skills.inventoryBusy"
    assert "inventory.lock" not in result["detail"]
    assert (inventory_commit_manager._skills_dir / "first-skill/SKILL.md").read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["toggle", "uninstall"])
async def test_nonenterprise_mutation_does_not_block_event_loop(
    inventory_commit_manager, monkeypatch, operation,
):
    import asyncio
    import threading

    monkeypatch.setattr(manager_module, "is_enterprise", lambda: False)
    started, release = threading.Event(), threading.Event()

    def slow_mutation(params):
        started.set()
        assert release.wait(5)
        return {"success": True}

    target = "_toggle_skills_locked" if operation == "toggle" else "_handle_enterprise_skills_uninstall"
    monkeypatch.setattr(inventory_commit_manager, target, slow_mutation)
    task = asyncio.create_task(
        getattr(inventory_commit_manager, f"handle_skills_{operation}")({"name": "first-skill"})
    )
    try:
        assert await asyncio.to_thread(started.wait, 3)
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        result = await task
    assert result["success"] is True
