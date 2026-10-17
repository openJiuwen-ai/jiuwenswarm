# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""skills.get 图片改写 / download 图片预览 / evolution.get 规范化."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.tools.web_file_download import (
    PURPOSE_SKILL_CONTENT_IMAGE,
    WebFileDownloadManager,
    generate_skill_content_image_token,
    validate_file_download_token,
)
from jiuwenswarm.server.runtime.skill.archive_store import (
    ARCHIVE_DIRNAME,
    CONTENT_DIRNAME,
    INDEX_FILENAME,
    VERSIONS_DIRNAME,
)
from jiuwenswarm.server.runtime.skill.skill_content_images import (
    resolve_skill_content_image_file,
    rewrite_skill_markdown_images,
    validate_skill_content_image_payload,
)
from jiuwenswarm.server.runtime.skill.skill_files import SkillFilesError
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


def _write_skill(skill_dir: Path, *, name: str, body: str) -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: demo\n---\n{body}\n",
        encoding="utf-8",
    )


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
        "jiuwenswarm.server.runtime.skill.skill_content_images.get_agent_skills_dir",
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
    WebFileDownloadManager._instance = WebFileDownloadManager(secret="a" * 32)
    return SkillManager()


@pytest.mark.asyncio
async def test_skills_get_rewrites_relative_image_keeps_external(
    manager: SkillManager, tmp_path: Path
) -> None:
    skill_dir = manager._skills_dir / "visual-doc"
    assets = skill_dir / "assets"
    assets.mkdir(parents=True)
    (assets / "flow.png").write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
    )
    _write_skill(
        skill_dir,
        name="visual-doc",
        body=(
            "# Body\n\n"
            "![flow](assets/flow.png)\n\n"
            "![remote](https://example.com/a.png)\n\n"
            "![missing](assets/nope.png)\n"
        ),
    )
    manager._add_local_skill({"name": "visual-doc", "source": "local"})

    detail = await manager.handle_skills_get(
        {"name": "visual-doc", "_session_id": "sess-1"}
    )
    assert "/file-api/download?token=" in detail["content"]
    assert "session_id=sess-1" in detail["content"]
    assert "https://example.com/a.png" in detail["content"]
    assert "assets/nope.png" in detail["content"]
    # 磁盘原文未改
    disk = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "assets/flow.png" in disk
    assert "/file-api/download" not in disk


@pytest.mark.asyncio
async def test_skills_get_version_image_isolated_from_workspace(
    manager: SkillManager,
) -> None:
    skill_dir = manager._skills_dir / "visual-doc"
    _write_skill(skill_dir, name="visual-doc", body="![ws](assets/ws-only.png)")
    (skill_dir / "assets").mkdir()
    (skill_dir / "assets" / "ws-only.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)

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
                        "storage_id": "ver-img",
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
    content = skill_dir / ARCHIVE_DIRNAME / VERSIONS_DIRNAME / CONTENT_DIRNAME / "ver-img"
    _write_skill(content, name="visual-doc", body="![ver](assets/ver-only.png)")
    (content / "assets").mkdir()
    (content / "assets" / "ver-only.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    manager._add_local_skill({"name": "visual-doc", "source": "teamskillshub"})

    by_version = await manager.handle_skills_get(
        {"name": "visual-doc", "version": "2.0.0", "_session_id": "sess-v"}
    )
    assert "/file-api/download?token=" in by_version["content"]
    # token 绑定 version=2.0.0，且不得回退到 workspace 图
    token = by_version["content"].split("token=")[1].split("&")[0].split(")")[0]
    payload = validate_file_download_token(token)
    assert payload is not None
    assert payload.get("purpose") == PURPOSE_SKILL_CONTENT_IMAGE
    assert payload.get("version") == "2.0.0"
    assert payload.get("relative_path") == "assets/ver-only.png"
    assert "path" not in payload or not payload.get("path")
    assert "session_id=sess-v" in by_version["content"]


def test_skill_content_image_token_and_resolve(manager: SkillManager) -> None:
    skill_dir = manager._skills_dir / "visual-doc"
    _write_skill(skill_dir, name="visual-doc", body="# x")
    assets = skill_dir / "assets"
    assets.mkdir()
    png = assets / "a.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)

    token = generate_skill_content_image_token(
        name="visual-doc",
        version=None,
        relative_path="assets/a.png",
        session_id="sess-img",
    )
    payload = validate_file_download_token(token)
    assert payload is not None
    assert (
        validate_skill_content_image_payload(payload, request_session_id="")
        == "invalid_or_expired_token"
    )
    assert validate_skill_content_image_payload(payload, request_session_id="sess-img") is None
    assert (
        validate_skill_content_image_payload(payload, request_session_id="other")
        == "invalid_or_expired_token"
    )
    path, mime = resolve_skill_content_image_file(
        name="visual-doc",
        version=None,
        relative_path="assets/a.png",
        skills_dir=manager._skills_dir,
    )
    assert path == png.resolve()
    assert mime.startswith("image/")

    with pytest.raises(SkillFilesError):
        resolve_skill_content_image_file(
            name="visual-doc",
            version=None,
            relative_path="assets/a.svg",
            skills_dir=manager._skills_dir,
        )


def test_rewrite_skips_svg(manager: SkillManager) -> None:
    skill_dir = manager._skills_dir / "visual-doc"
    _write_skill(skill_dir, name="visual-doc", body="x")
    (skill_dir / "assets").mkdir()
    (skill_dir / "assets" / "x.svg").write_text("<svg></svg>", encoding="utf-8")
    out = rewrite_skill_markdown_images(
        "![s](assets/x.svg)",
        skill_name="visual-doc",
        version=None,
        content_root=skill_dir,
        session_id="sess",
    )
    assert out == "![s](assets/x.svg)"


def test_rewrite_skips_fenced_code_block_images(manager: SkillManager) -> None:
    skill_dir = manager._skills_dir / "visual-doc"
    _write_skill(skill_dir, name="visual-doc", body="x")
    (skill_dir / "assets").mkdir()
    (skill_dir / "assets" / "flow.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    md = (
        "See ![ok](assets/flow.png)\n\n"
        "```md\n![code](assets/flow.png)\n```\n"
    )
    out = rewrite_skill_markdown_images(
        md,
        skill_name="visual-doc",
        version=None,
        content_root=skill_dir,
        session_id="sess-fence",
    )
    assert "/file-api/download?token=" in out
    assert "session_id=sess-fence" in out
    assert "```md\n![code](assets/flow.png)\n```" in out


@pytest.mark.asyncio
async def test_evolution_get_normalizes_messy_log(manager: SkillManager) -> None:
    skill_dir = manager._skills_dir / "document-review"
    _write_skill(skill_dir, name="document-review", body="# body")
    (skill_dir / "evolutions.json").write_text(
        json.dumps(
            {
                "skill_id": None,
                "version": 1,
                "updated_at": None,
                "entries": [
                    {
                        "id": "exp-1",
                        "change": {"content": "Always check paths."},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    manager._add_local_skill({"name": "document-review", "source": "local"})

    data = await manager.handle_skills_evolution_get({"name": "document-review"})
    assert data["exists"] is True
    assert data["valid"] is True
    assert data["skill_id"] == "document-review"
    assert data["version"] == "1.0.0"
    assert data["updated_at"] == ""
    assert len(data["entries"]) == 1
    entry = data["entries"][0]
    assert entry["id"] == "exp-1"
    assert entry["score"] == 0.6
    assert entry["change"]["content"] == "Always check paths."
    assert entry["change"]["target"] == "body"
    assert entry["usage_stats"]["times_presented"] == 0


@pytest.mark.asyncio
async def test_evolution_get_missing_file(manager: SkillManager) -> None:
    skill_dir = manager._skills_dir / "empty-evo"
    _write_skill(skill_dir, name="empty-evo", body="# body")
    manager._add_local_skill({"name": "empty-evo", "source": "local"})
    data = await manager.handle_skills_evolution_get({"name": "empty-evo"})
    assert data == {
        "name": "empty-evo",
        "exists": False,
        "valid": True,
        "skill_id": "empty-evo",
        "version": "1.0.0",
        "updated_at": "",
        "entries": [],
    }
