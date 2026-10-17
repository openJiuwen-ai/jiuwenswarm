# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""skills.files.* / evolution.save / rebuild 契约测试."""

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


def _write_skill(skill_dir: Path, *, name: str, body: str = "# Body") -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: demo\n---\n{body}\n",
        encoding="utf-8",
    )


def _write_version_bundle(
    skill_dir: Path,
    *,
    name: str,
    version: str = "2.0.0",
    storage_id: str = "ver-8f31d0",
    body: str = "# version body",
) -> Path:
    _write_version_index(skill_dir, version=version, storage_id=storage_id)
    content = (
        skill_dir / ARCHIVE_DIRNAME / VERSIONS_DIRNAME / CONTENT_DIRNAME / storage_id
    )
    _write_skill(content, name=name, body=body)
    return content


def _write_version_index(
    skill_dir: Path,
    *,
    version: str,
    storage_id: str,
) -> None:
    index_dir = skill_dir / ARCHIVE_DIRNAME / VERSIONS_DIRNAME
    index_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 2,
        "current_version": version,
        "installed_asset_id": "asset-1",
        "versions": [
            {
                "version": version,
                "storage_id": storage_id,
                "source": "skillhub",
                "checksum_sha256": "abc",
                "created_at": "2026-08-04T10:00:00Z",
                "updated_at": "2026-08-04T10:00:00Z",
            }
        ],
        "remote_asset_id": None,
        "last_published_version": None,
        "updated_at": "2026-08-04T10:00:00Z",
    }
    (index_dir / INDEX_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _pending_evolution(skill_id: str, content: str = "Always check paths first.") -> dict:
    return {
        "skill_id": skill_id,
        "version": "1.0.0",
        "updated_at": "2026-08-04T10:00:00Z",
        "entries": [
            {
                "id": "exp-001",
                "source": "user",
                "timestamp": "2026-08-04T10:00:00Z",
                "context": "demo",
                "applied": False,
                "score": 0.9,
                "change": {
                    "section": "Troubleshooting",
                    "action": "append",
                    "content": content,
                    "target": "body",
                },
            }
        ],
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
    return SkillManager()


@pytest.mark.asyncio
async def test_skills_files_list_hides_archive(manager: SkillManager) -> None:
    skill_dir = manager._skills_dir / "document-review"
    _write_skill(skill_dir, name="document-review")
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "checklist.md").write_text("# ok", encoding="utf-8")
    archive_secret = skill_dir / ARCHIVE_DIRNAME / "secret.txt"
    archive_secret.parent.mkdir(parents=True)
    archive_secret.write_text("nope", encoding="utf-8")
    manager._add_local_skill({"name": "document-review", "source": "local"})

    result = await manager.handle_skills_files_list({"name": "document-review"})
    paths = {item["path"] for item in result["files"]}
    assert "SKILL.md" in paths
    assert "references" in paths
    assert "references/checklist.md" in paths
    assert not any(p == ARCHIVE_DIRNAME or p.startswith(f"{ARCHIVE_DIRNAME}/") for p in paths)


@pytest.mark.asyncio
async def test_skills_files_get_text_and_reject_archive(manager: SkillManager) -> None:
    skill_dir = manager._skills_dir / "document-review"
    _write_skill(skill_dir, name="document-review")
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "checklist.md").write_text("# checklist", encoding="utf-8")
    manager._add_local_skill({"name": "document-review", "source": "local"})

    data = await manager.handle_skills_files_get(
        {"name": "document-review", "path": "references/checklist.md"}
    )
    assert data["path"] == "references/checklist.md"
    assert data["type"] == "file"
    assert data["encoding"] == "utf-8"
    assert "checklist" in data["content"]

    with pytest.raises(SkillRpcError) as exc_info:
        await manager.handle_skills_files_get(
            {"name": "document-review", "path": ".archive/secret.txt"}
        )
    assert exc_info.value.code == "SKILL_UNSAFE_PATH"

    with pytest.raises(SkillRpcError):
        await manager.handle_skills_files_get(
            {"name": "document-review", "path": "../outside.md"}
        )


@pytest.mark.asyncio
async def test_skills_files_get_binary_returns_download_url(
    manager: SkillManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_dir = manager._skills_dir / "document-review"
    _write_skill(skill_dir, name="document-review")
    bin_path = skill_dir / "clip.png"
    bin_path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
    manager._add_local_skill({"name": "document-review", "source": "local"})

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.web_file_download.build_file_download_info",
        lambda *args, **kwargs: {
            "download_url": "/file-api/download?token=demo",
            "download_token": "demo",
            "name": "clip.png",
            "size": 8,
            "mime_type": "image/png",
        },
    )

    data = await manager.handle_skills_files_get(
        {"name": "document-review", "path": "clip.png", "session_id": "s1"}
    )
    assert "content" not in data
    assert data["download_url"] == "/file-api/download?token=demo"


@pytest.mark.asyncio
async def test_evolution_save_syncs_default_version_copy(manager: SkillManager) -> None:
    skill_dir = manager._skills_dir / "document-review"
    _write_skill(skill_dir, name="document-review", body="# workspace")
    content = _write_version_bundle(skill_dir, name="document-review")
    manager._add_local_skill({"name": "document-review", "source": "teamskillshub"})

    entries = [
        {
            "id": "exp-001",
            "change": {"content": "Check governing law first."},
        }
    ]
    result = await manager.handle_skills_evolution_save(
        {"name": "document-review", "entries": entries}
    )
    assert result["success"] is True
    assert result["entry_count"] == 1

    ws_evo = json.loads((skill_dir / "evolutions.json").read_text(encoding="utf-8"))
    ver_evo = json.loads((content / "evolutions.json").read_text(encoding="utf-8"))
    assert ws_evo["entries"][0]["change"]["content"] == "Check governing law first."
    assert ver_evo["entries"][0]["change"]["content"] == "Check governing law first."


@pytest.mark.asyncio
async def test_rebuild_unversioned_returns_followup(
    manager: SkillManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_dir = manager._skills_dir / "local-doc"
    _write_skill(skill_dir, name="local-doc", body="# original")
    (skill_dir / "evolutions.json").write_text(
        json.dumps(_pending_evolution("local-doc"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    manager._add_local_skill({"name": "local-doc", "source": "local"})

    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        manager,
        "_prepare_evolve_rebuild_followup",
        AsyncMock(
            return_value={
                "result_type": "followup",
                "action": "run_rebuild_followup",
                "followup_prompt": "Please rebuild local-doc",
                "skill_name": "local-doc",
            }
        ),
    )

    result = await manager.handle_skills_rebuild({"name": "local-doc", "version": None})
    assert result["success"] is True
    assert result["result_type"] == "followup"
    assert result["action"] == "run_rebuild_followup"
    assert "Please rebuild local-doc" in result["followup_prompt"]
    # prepare 层不改写 SKILL.md，交给 interface 静默 Agent follow-up
    assert "# original" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    # rebuild 后清除经验文件，详情不再展示经验入口
    assert not (skill_dir / "evolutions.json").exists()
    listed = await manager.handle_skills_list({})
    card = next(s for s in listed["skills"] if s["name"] == "local-doc")
    assert card["has_evolutions"] is False


@pytest.mark.asyncio
async def test_rebuild_default_version_returns_followup_and_syncs_mid_state(
    manager: SkillManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_dir = manager._skills_dir / "document-review"
    _write_skill(skill_dir, name="document-review", body="# workspace original")
    content = _write_version_bundle(
        skill_dir, name="document-review", body="# version original"
    )
    (skill_dir / "evolutions.json").write_text(
        json.dumps(_pending_evolution("document-review"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    manager._add_local_skill({"name": "document-review", "source": "teamskillshub"})

    async def _fake_prepare(store, skill_name, **kwargs):
        staged = Path(store.base_dir) / skill_name
        evo = staged / "evolutions.json"
        if evo.is_file():
            data = json.loads(evo.read_text(encoding="utf-8"))
            data["entries"] = []
            evo.write_text(json.dumps(data), encoding="utf-8")
        (staged / "archive").mkdir(exist_ok=True)
        return {
            "result_type": "followup",
            "action": "run_rebuild_followup",
            "followup_prompt": "rebuild document-review",
            "skill_name": skill_name,
        }

    monkeypatch.setattr(
        manager,
        "_prepare_evolve_rebuild_followup",
        _fake_prepare,
    )

    result = await manager.handle_skills_rebuild(
        {"name": "document-review", "version": "2.0.0"}
    )
    assert result["success"] is True
    assert result["result_type"] == "followup"
    assert result["rebuild_target"]["is_default"] is True
    assert result["rebuild_target"]["swap_workspace"] is False

    assert not (skill_dir / "evolutions.json").exists()
    assert not (content / "evolutions.json").exists()
    detail = await manager.handle_skills_get({"name": "document-review"})
    assert detail["has_evolutions"] is False


@pytest.mark.asyncio
async def test_skills_files_list_rejects_version_param(manager: SkillManager) -> None:
    skill_dir = manager._skills_dir / "document-review"
    _write_skill(skill_dir, name="document-review")
    manager._add_local_skill({"name": "document-review", "source": "local"})

    with pytest.raises(ValueError, match="不接受 version"):
        await manager.handle_skills_files_list(
            {"name": "document-review", "version": "2.0.0"}
        )


@pytest.mark.asyncio
async def test_rebuild_non_default_sets_swap_workspace(
    manager: SkillManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_dir = manager._skills_dir / "multi-ver"
    _write_skill(skill_dir, name="multi-ver", body="# workspace untouched")
    (skill_dir / "evolutions.json").write_text(
        json.dumps(_pending_evolution("multi-ver"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    index_dir = skill_dir / ARCHIVE_DIRNAME / VERSIONS_DIRNAME
    index_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 2,
        "current_version": "2.0.0",
        "installed_asset_id": "asset-1",
        "versions": [
            {
                "version": "1.0.0",
                "storage_id": "ver-old",
                "source": "skillhub",
                "checksum_sha256": "old",
                "created_at": "2026-08-01T10:00:00Z",
                "updated_at": "2026-08-01T10:00:00Z",
            },
            {
                "version": "2.0.0",
                "storage_id": "ver-new",
                "source": "skillhub",
                "checksum_sha256": "new",
                "created_at": "2026-08-04T10:00:00Z",
                "updated_at": "2026-08-04T10:00:00Z",
            },
        ],
        "remote_asset_id": None,
        "last_published_version": None,
        "updated_at": "2026-08-04T10:00:00Z",
    }
    (index_dir / INDEX_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    old_content = (
        skill_dir / ARCHIVE_DIRNAME / VERSIONS_DIRNAME / CONTENT_DIRNAME / "ver-old"
    )
    new_content = (
        skill_dir / ARCHIVE_DIRNAME / VERSIONS_DIRNAME / CONTENT_DIRNAME / "ver-new"
    )
    _write_skill(old_content, name="multi-ver", body="# old version")
    _write_skill(new_content, name="multi-ver", body="# default version")
    manager._add_local_skill({"name": "multi-ver", "source": "teamskillshub"})

    async def _fake_prepare(store, skill_name, **kwargs):
        staged = Path(store.base_dir) / skill_name
        evo = staged / "evolutions.json"
        if evo.is_file():
            data = json.loads(evo.read_text(encoding="utf-8"))
            data["entries"] = []
            evo.write_text(json.dumps(data), encoding="utf-8")
        return {
            "result_type": "followup",
            "action": "run_rebuild_followup",
            "followup_prompt": "rebuild multi-ver",
            "skill_name": skill_name,
        }

    monkeypatch.setattr(
        manager,
        "_prepare_evolve_rebuild_followup",
        _fake_prepare,
    )

    result = await manager.handle_skills_rebuild(
        {"name": "multi-ver", "version": "1.0.0"}
    )
    assert result["success"] is True
    assert result["result_type"] == "followup"
    assert result["rebuild_target"]["swap_workspace"] is True
    assert result["rebuild_target"]["is_default"] is False
    # prepare 后目标版本与 workspace 的 evolutions 均已清除
    assert "# workspace untouched" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert not (old_content / "evolutions.json").exists()
    assert not (skill_dir / "evolutions.json").exists()


@pytest.mark.asyncio
async def test_rebuild_without_evolutions_fails(manager: SkillManager) -> None:
    skill_dir = manager._skills_dir / "empty-evo"
    _write_skill(skill_dir, name="empty-evo")
    manager._add_local_skill({"name": "empty-evo", "source": "local"})

    with pytest.raises(SkillRpcError) as exc_info:
        await manager.handle_skills_rebuild({"name": "empty-evo", "version": None})
    assert exc_info.value.code == "SKILL_REBUILD_FAILED"


def test_req_methods_registered() -> None:
    from jiuwenswarm.common.schema.message import ReqMethod

    assert ReqMethod.SKILLS_FILES_LIST.value == "skills.files.list"
    assert ReqMethod.SKILLS_FILES_GET.value == "skills.files.get"
    assert ReqMethod.SKILLS_REBUILD.value == "skills.rebuild"
