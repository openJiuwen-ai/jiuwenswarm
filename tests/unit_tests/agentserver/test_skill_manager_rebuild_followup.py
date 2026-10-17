# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""skills.rebuild prepare 层与 evolve_rebuild slash 对齐的契约测试.

对外 RPC 由 interface 同步静默 Agent 后收敛为 {success:true}；
本文件覆盖 SkillManager 返回 followup payload 的领域准备契约。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

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
async def test_rebuild_returns_slash_followup(manager: SkillManager, monkeypatch: pytest.MonkeyPatch) -> None:
    skill_dir = manager._skills_dir / "local-doc"
    _write_skill(skill_dir, name="local-doc", body="# original")
    (skill_dir / "evolutions.json").write_text(
        json.dumps(_pending_evolution("local-doc"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    manager._add_local_skill({"name": "local-doc", "source": "local"})

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
    assert result["followup_prompt"] == "Please rebuild local-doc"
    assert result["skill_name"] == "local-doc"
    # prepare 层不直接改写 SKILL.md（交给 interface 静默 Agent）
    assert "# original" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert not (skill_dir / "evolutions.json").exists()


@pytest.mark.asyncio
async def test_rebuild_without_evolutions_fails(manager: SkillManager) -> None:
    skill_dir = manager._skills_dir / "empty-evo"
    _write_skill(skill_dir, name="empty-evo")
    manager._add_local_skill({"name": "empty-evo", "source": "local"})

    with pytest.raises(SkillRpcError) as exc_info:
        await manager.handle_skills_rebuild({"name": "empty-evo", "version": None})
    assert exc_info.value.code == "SKILL_REBUILD_FAILED"


@pytest.mark.asyncio
async def test_rebuild_default_version_prepares_and_returns_followup(
    manager: SkillManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_dir = manager._skills_dir / "document-review"
    _write_skill(skill_dir, name="document-review", body="# workspace")
    (skill_dir / "evolutions.json").write_text(
        json.dumps(_pending_evolution("document-review"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    index_dir = skill_dir / ARCHIVE_DIRNAME / VERSIONS_DIRNAME
    index_dir.mkdir(parents=True)
    (index_dir / INDEX_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "current_version": "2.0.0",
                "installed_asset_id": "asset-1",
                "versions": [
                    {
                        "version": "2.0.0",
                        "storage_id": "ver-8f31d0",
                        "source": "skillhub",
                        "checksum_sha256": "abc",
                        "created_at": "2026-08-04T10:00:00Z",
                        "updated_at": "2026-08-04T10:00:00Z",
                    }
                ],
                "remote_asset_id": None,
                "last_published_version": None,
                "updated_at": "2026-08-04T10:00:00Z",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    content = skill_dir / ARCHIVE_DIRNAME / VERSIONS_DIRNAME / CONTENT_DIRNAME / "ver-8f31d0"
    _write_skill(content, name="document-review", body="# version")
    manager._add_local_skill({"name": "document-review", "source": "teamskillshub"})

    async def _fake_prepare(store, skill_name, **kwargs):
        # 模拟 prepare：清空 staged evolutions
        staged = Path(store.base_dir) / skill_name
        evo = staged / "evolutions.json"
        if evo.is_file():
            data = json.loads(evo.read_text(encoding="utf-8"))
            data["entries"] = []
            evo.write_text(json.dumps(data), encoding="utf-8")
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
    # 默认版本 mid-state 同步后清除 evolutions，has_evolutions=false
    assert not (skill_dir / "evolutions.json").exists()
    detail = await manager.handle_skills_get({"name": "document-review"})
    assert detail["has_evolutions"] is False
