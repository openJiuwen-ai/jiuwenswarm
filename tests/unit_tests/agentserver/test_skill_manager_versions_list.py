# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""skills.list / installed / get / versions.list 契约测试."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.skill.archive_store import (
    ARCHIVE_DIRNAME,
    CONTENT_DIRNAME,
    INDEX_FILENAME,
    VERSIONS_DIRNAME,
)
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager, SkillRpcError
from jiuwenswarm.server.runtime.skill.skill_type import (
    SKILL_TYPE_MULTIMODAL,
    SKILL_TYPE_SKILL,
    SKILL_TYPE_SWARM,
    detect_skill_type,
)


def _write_skill(skill_dir: Path, *, name: str, description: str = "desc", body: str = "# Hi") -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\nversion: yaml-should-be-ignored\n---\n{body}\n",
        encoding="utf-8",
    )


def _write_version_index(
    skill_dir: Path,
    *,
    current_version: str | None,
    versions: list[dict],
    installed_asset_id: str | None = None,
) -> None:
    index_dir = skill_dir / ARCHIVE_DIRNAME / VERSIONS_DIRNAME
    index_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 2,
        "current_version": current_version,
        "installed_asset_id": installed_asset_id
        if installed_asset_id is not None
        else ("asset-1" if current_version else None),
        "versions": versions,
        "remote_asset_id": None,
        "last_published_version": None,
        "updated_at": "2026-08-04T10:00:00Z",
    }
    (index_dir / INDEX_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _write_version_content(skill_dir: Path, storage_id: str, *, name: str, body: str) -> Path:
    content_root = (
        skill_dir / ARCHIVE_DIRNAME / VERSIONS_DIRNAME / CONTENT_DIRNAME / storage_id
    )
    _write_skill(content_root, name=name, body=body)
    return content_root


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
    # skilldev state_utils 也走同一 state 文件
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skilldev.state_utils.get_state_file",
        lambda: state_file,
    )
    return SkillManager()


@pytest.mark.asyncio
async def test_skills_list_returns_null_version_and_skill_type(manager: SkillManager) -> None:
    skill_dir = manager._skills_dir / "document-review"
    _write_skill(skill_dir, name="document-review")
    manager._add_local_skill({"name": "document-review", "source": "local"})

    result = await manager.handle_skills_list({})
    skills = {s["name"]: s for s in result["skills"]}
    assert "document-review" in skills
    card = skills["document-review"]
    assert card["version"] is None
    assert card["skill_type"] == SKILL_TYPE_SKILL
    assert card["has_evolutions"] is False


@pytest.mark.asyncio
async def test_skills_list_reads_current_version_from_archive(manager: SkillManager) -> None:
    skill_dir = manager._skills_dir / "video-inspector"
    _write_skill(skill_dir, name="video-inspector")
    (skill_dir / "clip.png").write_bytes(b"png")
    _write_version_index(
        skill_dir,
        current_version="2.0.0",
        versions=[
            {
                "version": "2.0.0",
                "storage_id": "ver-8f31d0",
                "source": "skillhub",
                "checksum_sha256": "abc",
                "created_at": "2026-08-04T10:00:00Z",
                "updated_at": "2026-08-04T10:00:00Z",
            }
        ],
    )
    _write_version_content(skill_dir, "ver-8f31d0", name="video-inspector", body="# v2")
    manager._add_local_skill({"name": "video-inspector", "source": "teamskillshub"})

    result = await manager.handle_skills_list({})
    card = next(s for s in result["skills"] if s["name"] == "video-inspector")
    assert card["version"] == "2.0.0"
    assert card["skill_type"] == SKILL_TYPE_MULTIMODAL


@pytest.mark.asyncio
async def test_skills_installed_drops_plugin_version_and_exposes_skill_versions(
    manager: SkillManager,
) -> None:
    skill_dir = manager._skills_dir / "video-inspector"
    _write_skill(skill_dir, name="video-inspector")
    _write_version_index(
        skill_dir,
        current_version="2.0.0",
        versions=[
            {
                "version": "2.0.0",
                "storage_id": "ver-8f31d0",
                "source": "skillhub",
                "checksum_sha256": "abc",
                "created_at": "2026-08-04T10:00:00Z",
                "updated_at": "2026-08-04T10:00:00Z",
            }
        ],
    )
    _write_version_content(skill_dir, "ver-8f31d0", name="video-inspector", body="# v2")
    manager._add_installed_plugin(
        {
            "name": "video-inspector",
            "marketplace": "teamskillshub",
            "version": "should-be-removed",
            "commit": "",
            "source": "teamskillshub",
            "installed_at": "2026-08-04T10:00:00Z",
            "skills": ["video-inspector"],
        }
    )

    result = await manager.handle_skills_installed({})
    plugin = result["plugins"][0]
    assert "version" not in plugin
    assert plugin["plugin_name"] == "video-inspector"
    assert plugin["marketplace"] == "teamskillshub"
    assert plugin["spec"] is None
    assert plugin["installed_at"] == "2026-08-04T10:00:00Z"
    assert plugin["git_commit"] is None
    assert plugin["skills"] == [{"name": "video-inspector", "version": "2.0.0"}]
    # 持久化也不再保留 version
    raw = json.loads(manager._state_file.read_text(encoding="utf-8"))
    assert "version" not in raw["installed_plugins"][0]


@pytest.mark.asyncio
async def test_skills_get_workspace_and_version_copy(manager: SkillManager) -> None:
    skill_dir = manager._skills_dir / "document-review"
    _write_skill(skill_dir, name="document-review", body="# workspace body")
    _write_version_index(
        skill_dir,
        current_version="2.0.0",
        versions=[
            {
                "version": "2.0.0",
                "storage_id": "ver-8f31d0",
                "source": "skillhub",
                "checksum_sha256": "abc",
                "created_at": "2026-08-04T10:00:00Z",
                "updated_at": "2026-08-04T10:00:00Z",
            }
        ],
    )
    content = _write_version_content(
        skill_dir, "ver-8f31d0", name="document-review", body="# version body"
    )
    manager._add_local_skill({"name": "document-review", "source": "teamskillshub"})

    workspace = await manager.handle_skills_get({"name": "document-review"})
    assert "workspace body" in workspace["content"]
    assert workspace["version"] == "2.0.0"
    assert workspace["skill_type"] == SKILL_TYPE_SKILL
    assert Path(workspace["file_path"]) == skill_dir / "SKILL.md"

    by_version = await manager.handle_skills_get(
        {"name": "document-review", "version": "2.0.0"}
    )
    assert "version body" in by_version["content"]
    assert by_version["version"] == "2.0.0"
    assert Path(by_version["file_path"]) == content / "SKILL.md"
    # 只读：workspace 不被替换
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8").count("workspace body") == 1


@pytest.mark.asyncio
async def test_skills_get_missing_version_does_not_fallback(manager: SkillManager) -> None:
    skill_dir = manager._skills_dir / "document-review"
    _write_skill(skill_dir, name="document-review", body="# workspace")
    manager._add_local_skill({"name": "document-review", "source": "local"})

    with pytest.raises(SkillRpcError) as exc_info:
        await manager.handle_skills_get({"name": "document-review", "version": "9.9.9"})
    assert exc_info.value.code == "SKILL_VERSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_skills_versions_list_empty_and_populated(manager: SkillManager) -> None:
    local_dir = manager._skills_dir / "local-document-review"
    _write_skill(local_dir, name="local-document-review")
    manager._add_local_skill({"name": "local-document-review", "source": "local"})

    empty = await manager.handle_skills_versions_list({"name": "local-document-review"})
    assert empty == {
        "success": True,
        "name": "local-document-review",
        "default_version": None,
        "versions": [],
    }

    hub_dir = manager._skills_dir / "document-review"
    _write_skill(hub_dir, name="document-review")
    _write_version_index(
        hub_dir,
        current_version="2.0.0",
        versions=[
            {
                "version": "2.0.0",
                "storage_id": "ver-8f31d0",
                "source": "skillhub",
                "checksum_sha256": "abc",
                "created_at": "2026-08-04T10:00:00Z",
                "updated_at": "2026-08-04T10:00:00Z",
            }
        ],
    )
    _write_version_content(hub_dir, "ver-8f31d0", name="document-review", body="# v2")
    manager._add_local_skill({"name": "document-review", "source": "teamskillshub"})

    listed = await manager.handle_skills_versions_list({"name": "document-review"})
    assert listed["success"] is True
    assert listed["default_version"] == "2.0.0"
    assert len(listed["versions"]) == 1
    assert listed["versions"][0]["version"] == "2.0.0"
    assert listed["versions"][0]["is_default"] is True
    assert listed["versions"][0]["source"] == "skillhub"
    assert listed["versions"][0]["available"] is True


@pytest.mark.asyncio
async def test_skills_versions_list_corrupt_index_errors(manager: SkillManager) -> None:
    skill_dir = manager._skills_dir / "broken"
    _write_skill(skill_dir, name="broken")
    index_path = skill_dir / ARCHIVE_DIRNAME / VERSIONS_DIRNAME / INDEX_FILENAME
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("{not-json", encoding="utf-8")
    manager._add_local_skill({"name": "broken", "source": "local"})

    with pytest.raises(SkillRpcError) as exc_info:
        await manager.handle_skills_versions_list({"name": "broken"})
    assert exc_info.value.code == "SKILL_VERSION_CONTENT_INVALID"


def test_detect_skill_type_priority(tmp_path: Path) -> None:
    swarm = tmp_path / "swarm"
    swarm.mkdir()
    (swarm / "SKILL.md").write_text(
        "---\nname: swarm-demo\nkind: swarm-skill\n---\nbody\n",
        encoding="utf-8",
    )
    (swarm / "a.png").write_bytes(b"1")
    assert detect_skill_type(swarm) == SKILL_TYPE_SWARM

    # kind: team-skill 与 swarm-skill 等价，判为 swarm_skill
    team_kind = tmp_path / "team-kind"
    team_kind.mkdir()
    (team_kind / "SKILL.md").write_text(
        "---\nname: team-demo\nkind: team-skill\n---\nbody\n",
        encoding="utf-8",
    )
    assert detect_skill_type(team_kind) == SKILL_TYPE_SWARM

    # 仅有 workflow.md / roles 目录、无 swarm-skill kind 时，不再判为 swarm
    legacy_layout = tmp_path / "legacy-layout"
    legacy_layout.mkdir()
    (legacy_layout / "workflow.md").write_text("x", encoding="utf-8")
    (legacy_layout / "roles").mkdir()
    (legacy_layout / "SKILL.md").write_text(
        "---\nname: legacy\n---\nbody\n",
        encoding="utf-8",
    )
    assert detect_skill_type(legacy_layout) == SKILL_TYPE_SKILL

    multi = tmp_path / "multi"
    multi.mkdir()
    (multi / "a.mp4").write_bytes(b"1")
    assert detect_skill_type(multi) == SKILL_TYPE_MULTIMODAL

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "SKILL.md").write_text("x", encoding="utf-8")
    # .archive 内媒体不应影响类型
    archive_media = plain / ARCHIVE_DIRNAME / "x.png"
    archive_media.parent.mkdir(parents=True)
    archive_media.write_bytes(b"1")
    assert detect_skill_type(plain) == SKILL_TYPE_SKILL


def test_req_method_versions_list_registered() -> None:
    from jiuwenswarm.common.schema.message import ReqMethod

    assert ReqMethod.SKILLS_VERSIONS_LIST.value == "skills.versions.list"
