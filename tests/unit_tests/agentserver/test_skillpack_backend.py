# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SDD-0010 SkillPack backend contracts."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest

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
)


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
async def test_uninstalling_member_keeps_pack_and_blocks_it(
    manager: SkillManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_info = Mock()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.logger.info",
        log_info,
    )
    _install_demo_pack(manager, graph=False)
    await manager.handle_skills_list({})

    result = await manager.handle_skills_uninstall({"name": "first-skill"})

    assert result["success"] is True
    assert (manager._skills_dir / "demo-pack").is_dir()
    detail = await manager.handle_skills_get({"name": "demo-pack"})
    assert detail["blocked_members"] == [{"name": "first-skill", "reason": "missing"}]
    assert "demo-pack" in str(log_info.call_args_list)
    assert "missing" in str(log_info.call_args_list)


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
