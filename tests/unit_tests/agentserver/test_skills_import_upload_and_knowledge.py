# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""POST /file-api/skills/import 与 create-from-knowledge 领域契约测试."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module
from jiuwenswarm.server.runtime.skill.archive_store import ARCHIVE_DIRNAME
from jiuwenswarm.server.runtime.skill.skill_manager import (
    ERROR_SKILL_ALREADY_EXISTS,
    ERROR_SKILL_INVALID_PACKAGE,
    ERROR_SKILL_KNOWLEDGE_INPUT_CONFLICT,
    ERROR_SKILL_RESERVED_PATH,
    SkillManager,
    SkillRpcError,
)
from jiuwenswarm.server.runtime.skill.skills_multipart_http import (
    handle_skills_create_from_knowledge_http,
    handle_skills_import_http,
    parse_multipart_form,
)


def _skill_md(name: str, description: str = "demo skill") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n# Body\n"


def _zip_bytes(name: str, *, with_archive: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{name}/SKILL.md", _skill_md(name))
        if with_archive:
            zf.writestr(f"{name}/{ARCHIVE_DIRNAME}/versions/index.json", "{}")
    return buf.getvalue()


def _multipart(fields: dict[str, Any], *, boundary: str = "----SkillBoundary") -> tuple[str, bytes]:
    parts: list[bytes] = []
    for name, value in fields.items():
        if isinstance(value, tuple):
            filename, content, content_type = value
            parts.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                    f"Content-Type: {content_type}\r\n\r\n"
                ).encode("utf-8")
                + content
                + b"\r\n"
            )
        else:
            parts.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode("utf-8")
            )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    content_type = f"multipart/form-data; boundary={boundary}"
    return content_type, body


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
async def test_import_upload_zip_success(manager: SkillManager, tmp_path: Path) -> None:
    zip_path = tmp_path / "document-review.zip"
    zip_path.write_bytes(_zip_bytes("document-review"))
    result = await manager.handle_skills_import_upload(
        {"path": str(zip_path), "overwrite": False}
    )
    assert result["success"] is True
    skill = result["skill"]
    assert skill["name"] == "document-review"
    assert skill["version"] is None
    assert skill["source"] == "local"
    assert skill["skill_type"]
    assert Path(skill["workspace_path"]).is_dir()


@pytest.mark.asyncio
async def test_import_upload_rejects_skill_ext(manager: SkillManager, tmp_path: Path) -> None:
    path = tmp_path / "x.skill"
    path.write_bytes(_zip_bytes("x"))
    with pytest.raises(SkillRpcError) as exc:
        await manager.handle_skills_import_upload({"path": str(path)})
    assert exc.value.code == ERROR_SKILL_INVALID_PACKAGE


@pytest.mark.asyncio
async def test_import_upload_already_exists(manager: SkillManager, tmp_path: Path) -> None:
    zip_path = tmp_path / "demo.zip"
    zip_path.write_bytes(_zip_bytes("demo-skill"))
    await manager.handle_skills_import_upload({"path": str(zip_path), "overwrite": False})
    with pytest.raises(SkillRpcError) as exc:
        await manager.handle_skills_import_upload({"path": str(zip_path), "overwrite": False})
    assert exc.value.code == ERROR_SKILL_ALREADY_EXISTS


@pytest.mark.asyncio
async def test_import_upload_rejects_root_archive(manager: SkillManager, tmp_path: Path) -> None:
    zip_path = tmp_path / "bad.zip"
    zip_path.write_bytes(_zip_bytes("bad-skill", with_archive=True))
    with pytest.raises(SkillRpcError) as exc:
        await manager.handle_skills_import_upload({"path": str(zip_path)})
    assert exc.value.code == ERROR_SKILL_RESERVED_PATH


@pytest.mark.asyncio
async def test_create_from_knowledge_xor(manager: SkillManager) -> None:
    with pytest.raises(SkillRpcError) as exc:
        await manager.handle_skills_create_from_knowledge({})
    assert exc.value.code == ERROR_SKILL_KNOWLEDGE_INPUT_CONFLICT

    with pytest.raises(SkillRpcError) as exc2:
        await manager.handle_skills_create_from_knowledge(
            {"link": "https://example.com", "file_path": "/tmp/a.pdf"}
        )
    assert exc2.value.code == ERROR_SKILL_KNOWLEDGE_INPUT_CONFLICT


@pytest.mark.asyncio
async def test_create_from_knowledge_link_routes_omni(manager: SkillManager) -> None:
    payload = await manager.handle_skills_create_from_knowledge(
        {
            "link": "https://example.com/guide",
            "skill_description": "输出风险等级",
        }
    )
    assert payload["result_type"] == "followup"
    assert payload["skills"] == ["skill-omni-creation"]
    assert "输出风险等级" in payload["followup_prompt"]
    assert Path(payload["output_dir"]).is_dir()


@pytest.mark.asyncio
async def test_create_from_knowledge_file_routes_router(
    manager: SkillManager, tmp_path: Path
) -> None:
    doc = tmp_path / "guide.pdf"
    doc.write_bytes(b"%PDF-1.4")
    payload = await manager.handle_skills_create_from_knowledge({"file_path": str(doc)})
    assert payload["skills"] == ["skill-creator-router"]
    assert str(doc) in payload["followup_prompt"] or str(doc.resolve()) in payload["followup_prompt"]


def test_finalize_create_from_knowledge_installs(
    manager: SkillManager, tmp_path: Path
) -> None:
    out = tmp_path / "gen"
    skill_root = out / "new-skill"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(_skill_md("new-skill"), encoding="utf-8")
    result = manager.finalize_create_from_knowledge(out)
    assert result["success"] is True
    assert result["skill"]["name"] == "new-skill"
    assert result["skill"]["version"] is None
    assert result["skill"]["source"] == "local"
    assert "---" in result["skill"]["content"]


def test_multipart_import_http_local(manager: SkillManager, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skills_multipart_http.SkillManager",
        lambda: manager,
    )
    content_type, body = _multipart(
        {
            "overwrite": "false",
            "file": ("document-review.zip", _zip_bytes("document-review"), "application/zip"),
        }
    )
    status, payload = handle_skills_import_http(
        content_type=content_type, body=body, use_local_manager=True
    )
    assert status == 200
    assert payload["success"] is True
    assert payload["skill"]["name"] == "document-review"


def test_multipart_knowledge_conflict_http() -> None:
    content_type, body = _multipart({"skill_description": "x"})
    status, payload = handle_skills_create_from_knowledge_http(
        content_type=content_type, body=body, use_local_manager=True
    )
    assert status == 400
    assert payload["code"] == ERROR_SKILL_KNOWLEDGE_INPUT_CONFLICT


@pytest.mark.asyncio
async def test_create_from_knowledge_silent_runs_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    impl_calls: list[dict[str, Any]] = []

    class FakeAdapter:
        async def process_message_stream_impl(self, request, inputs):
            impl_calls.append(
                {
                    "session_id": request.session_id,
                    "skills": (request.params or {}).get("skills"),
                    "metadata": dict(request.metadata or {}),
                    "query": (request.params or {}).get("query"),
                }
            )
            for _ in ():
                yield _

    out = tmp_path / "out"
    skill_root = out / "from-link"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(_skill_md("from-link"), encoding="utf-8")

    swarm = interface_module.JiuWenSwarm()
    swarm._skill_manager = MagicMock()
    swarm._skill_manager.handle_skills_create_from_knowledge = AsyncMock(
        return_value={
            "success": True,
            "result_type": "followup",
            "followup_prompt": "generate now",
            "skills": ["skill-omni-creation"],
            "output_dir": str(out),
            "trusted_dirs": [str(out)],
            "input_file": "",
        }
    )
    swarm._skill_manager.finalize_create_from_knowledge = MagicMock(
        return_value={
            "success": True,
            "skill": {
                "name": "from-link",
                "description": "demo skill",
                "content": _skill_md("from-link"),
                "skill_type": "skill",
                "version": None,
                "source": "local",
                "workspace_path": str(tmp_path / "skills" / "from-link"),
            },
        }
    )
    swarm._refresh_skill_rails_after_change = AsyncMock()
    swarm.create_instance = AsyncMock()
    swarm._refresh_team_shared_skill_links = MagicMock()

    monkeypatch.setattr(swarm, "_ensure_adapter", lambda **_kwargs: FakeAdapter())
    monkeypatch.setattr(
        swarm,
        "_build_inputs",
        lambda request: ({"query": request.params.get("query")}, "disabled", ""),
    )

    request = AgentRequest(
        request_id="req-knowledge",
        channel_id="web",
        session_id="user-session",
        req_method=ReqMethod.SKILLS_CREATE_FROM_KNOWLEDGE,
        params={"link": "https://example.com"},
    )
    resp = await swarm._handle_skills_request(request)
    assert resp is not None
    assert resp.ok is True
    assert resp.payload["success"] is True
    assert resp.payload["skill"]["name"] == "from-link"
    assert impl_calls
    assert impl_calls[0]["metadata"].get("skills_create_from_knowledge_silent") is True
    assert impl_calls[0]["skills"] == ["skill-omni-creation"]


def test_parse_multipart_roundtrip() -> None:
    content_type, body = _multipart(
        {"link": "https://a.example", "skill_description": "desc"}
    )
    fields = parse_multipart_form(content_type, body)
    assert fields["link"] == "https://a.example"
    assert fields["skill_description"] == "desc"
