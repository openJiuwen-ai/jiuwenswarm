# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""skills.import_local download_token / 响应字段契约测试."""

from __future__ import annotations

import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.tools.web_file_download import (
    WebFileDownloadManager,
    generate_file_download_token,
)
from jiuwenswarm.server.runtime.skill.archive_store import (
    ARCHIVE_DIRNAME,
    CONTENT_DIRNAME,
    INDEX_FILENAME,
    VERSIONS_DIRNAME,
)
from jiuwenswarm.server.runtime.skill.skill_manager import (
    ERROR_SKILL_DOWNLOAD_TOKEN_INVALID,
    ERROR_SKILL_IMPORT_OVERWRITE_REQUIRED,
    ERROR_SKILL_RESERVED_PATH,
    SkillManager,
    SkillRpcError,
)


def _skill_md(name: str, description: str = "demo skill") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n# Body\n"


def _zip_skill(tmp_path: Path, *, name: str, filename: str = "pkg.zip") -> Path:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{name}/SKILL.md", _skill_md(name))
    out = tmp_path / filename
    out.write_bytes(buf.getvalue())
    return out


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
    WebFileDownloadManager.reset_instance()
    return SkillManager()


@pytest.mark.asyncio
async def test_import_local_requires_xor_token_and_path(manager: SkillManager) -> None:
    with pytest.raises(SkillRpcError) as exc:
        await manager.handle_skills_import_local({})
    assert exc.value.code == "SKILL_INVALID_PACKAGE"

    with pytest.raises(SkillRpcError) as exc2:
        await manager.handle_skills_import_local(
            {"path": "/tmp/x", "download_token": "abc"}
        )
    assert exc2.value.code == "SKILL_INVALID_PACKAGE"


@pytest.mark.asyncio
async def test_import_local_from_download_token(
    manager: SkillManager, tmp_path: Path
) -> None:
    pkg = _zip_skill(tmp_path, name="document-review", filename="document-review.skill")
    token = generate_file_download_token(str(pkg), session_id="session-abc")

    result = await manager.handle_skills_import_local(
        {"download_token": token, "_session_id": "session-abc"}
    )

    assert result["success"] is True
    skill = result["skill"]
    assert skill["name"] == "document-review"
    assert skill["description"] == "demo skill"
    assert skill["version"] is None
    assert skill["skill_type"] == "skill"
    assert skill["source"] == "local"
    assert skill["workspace_path"].endswith("document-review")
    assert (manager._skills_dir / "document-review" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_import_local_token_rejects_session_mismatch(
    manager: SkillManager, tmp_path: Path
) -> None:
    pkg = _zip_skill(tmp_path, name="document-review")
    token = generate_file_download_token(str(pkg), session_id="session-a")

    with pytest.raises(SkillRpcError) as exc:
        await manager.handle_skills_import_local(
            {"download_token": token, "_session_id": "session-b"}
        )
    assert exc.value.code == ERROR_SKILL_DOWNLOAD_TOKEN_INVALID


@pytest.mark.asyncio
async def test_import_local_rejects_root_archive_in_package(
    manager: SkillManager, tmp_path: Path
) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("document-review/SKILL.md", _skill_md("document-review"))
        zf.writestr("document-review/.archive/versions/index.json", "{}")
    pkg = tmp_path / "bad.zip"
    pkg.write_bytes(buf.getvalue())
    token = generate_file_download_token(str(pkg), session_id="sid")

    with pytest.raises(SkillRpcError) as exc:
        await manager.handle_skills_import_local(
            {"download_token": token, "_session_id": "sid"}
        )
    assert exc.value.code == ERROR_SKILL_RESERVED_PATH


@pytest.mark.asyncio
async def test_import_local_overwrite_preserves_archive(
    manager: SkillManager, tmp_path: Path
) -> None:
    skill_dir = manager._skills_dir / "document-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        _skill_md("document-review", "old"), encoding="utf-8"
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
                        "storage_id": "ver-1",
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
        ),
        encoding="utf-8",
    )
    content = skill_dir / ARCHIVE_DIRNAME / VERSIONS_DIRNAME / CONTENT_DIRNAME / "ver-1"
    content.mkdir(parents=True)
    (content / "SKILL.md").write_text(
        _skill_md("document-review", "version body"), encoding="utf-8"
    )
    manager._add_local_skill(
        {"name": "document-review", "source": "teamskillshub", "origin": "hub"}
    )

    with pytest.raises(SkillRpcError) as exc:
        await manager.handle_skills_import_local({"path": str(_zip_skill(tmp_path, name="document-review"))})
    assert exc.value.code == ERROR_SKILL_IMPORT_OVERWRITE_REQUIRED

    pkg = _zip_skill(tmp_path, name="document-review", filename="new.zip")
    # 更新包描述
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "document-review/SKILL.md",
            _skill_md("document-review", "updated description"),
        )
    pkg.write_bytes(buf.getvalue())

    result = await manager.handle_skills_import_local(
        {"path": str(pkg), "force": True}
    )
    assert result["success"] is True
    assert result["skill"]["version"] == "2.0.0"
    assert result["skill"]["source"] == "teamskillshub"
    assert result["skill"]["description"] == "updated description"
    assert (skill_dir / ARCHIVE_DIRNAME / VERSIONS_DIRNAME / INDEX_FILENAME).is_file()
    assert "updated description" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "updated description" in (content / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_import_local_overwrite_failure_keeps_original(
    manager: SkillManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """覆盖导入失败时必须保留原 Skill 目录与内容."""
    skill_dir = manager._skills_dir / "keep-me"
    skill_dir.mkdir(parents=True)
    original_md = _skill_md("keep-me", "original body")
    (skill_dir / "SKILL.md").write_text(original_md, encoding="utf-8")
    (skill_dir / "notes.txt").write_text("do-not-delete", encoding="utf-8")
    manager._add_local_skill({"name": "keep-me", "source": "local", "origin": "local"})

    def _boom(*_args, **_kwargs):
        raise OSError(3, "系统找不到指定的路径")

    monkeypatch.setattr(shutil, "copytree", _boom)

    pkg = _zip_skill(tmp_path, name="keep-me", filename="fail.zip")
    result = await manager.handle_skills_import_local({"path": str(pkg), "force": True})

    assert result["success"] is False
    assert skill_dir.is_dir()
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == original_md
    assert (skill_dir / "notes.txt").read_text(encoding="utf-8") == "do-not-delete"


def test_chat_send_schema_documents_skill_scene_fields() -> None:
    from jiuwenswarm.common.schema import chat_send as chat_send_mod
    import typing

    hints = typing.get_type_hints(chat_send_mod.ChatSendParams)
    assert "message" in hints
    assert "metadata" in hints
    assert "skills" in hints


def test_skill_creator_router_resource_exists() -> None:
    from jiuwenswarm.common.utils import get_builtin_skills_dir

    router = get_builtin_skills_dir() / "skill-creator" / "SKILL.md"
    assert router.is_file()
    text = router.read_text(encoding="utf-8")
    assert "skill-creator-normal" in text
    assert "swarmskill-creator" in text
    assert "skill-omni-creation" in text
    assert "send_file_to_user" in text
    assert "target_skill_type" in text
    assert "统一入口" in text
    assert "分发器" not in text

    normal = get_builtin_skills_dir() / "skill-creator-normal" / "SKILL.md"
    assert normal.is_file()
    assert "name: skill-creator-normal" in normal.read_text(encoding="utf-8")


def test_repair_skill_creator_normal_frontmatter_name(tmp_path) -> None:
    from jiuwenswarm.common.utils import (
        _migrate_skill_creator_router_rename,
        _repair_skill_creator_normal_frontmatter,
    )

    skills_dir = tmp_path / "skills"
    normal = skills_dir / "skill-creator-normal"
    normal.mkdir(parents=True)
    (normal / "SKILL.md").write_text(
        "---\nname: skill-creator\ndescription: legacy monadic creator\n---\nbody\n",
        encoding="utf-8",
    )
    # 新路由入口同名目录并存时，仅靠 frontmatter 会扫出两个 skill-creator
    router = skills_dir / "skill-creator"
    router.mkdir()
    (router / "SKILL.md").write_text(
        "---\nname: skill-creator\ndescription: Unified entry point\n---\nbody\n",
        encoding="utf-8",
    )

    _repair_skill_creator_normal_frontmatter(normal)
    assert "name: skill-creator-normal" in (normal / "SKILL.md").read_text(encoding="utf-8")

    # 迁移入口也应幂等修复
    (normal / "SKILL.md").write_text(
        "---\nname: skill-creator\ndescription: legacy monadic creator\n---\nbody\n",
        encoding="utf-8",
    )
    _migrate_skill_creator_router_rename(skills_dir)
    assert "name: skill-creator-normal" in (normal / "SKILL.md").read_text(encoding="utf-8")
    assert "name: skill-creator" in (router / "SKILL.md").read_text(encoding="utf-8")
