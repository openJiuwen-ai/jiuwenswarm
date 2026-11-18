# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""POST /file-api/skills/import 与 create-from-knowledge 领域契约测试."""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
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
    assert "禁止写入 workspace/skills" in payload["followup_prompt"]
    assert "跳过 code-writer" in payload["followup_prompt"]
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
    result = manager.finalize_create_from_knowledge(out, existing_skill_names=set())
    assert result["success"] is True
    assert result["skill"]["name"] == "new-skill"
    assert result["skill"]["version"] is None
    assert result["skill"]["source"] == "local"
    assert "---" in result["skill"]["content"]


def test_finalize_create_from_knowledge_new_this_run_no_conflict(
    manager: SkillManager, tmp_path: Path
) -> None:
    """运行前没有同名时，即使目标目录已由本轮写入，也应直接成功。"""
    dest = manager._skills_dir / "matplotlib_line_plot"
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text(
        _skill_md("matplotlib_line_plot", "from this run"), encoding="utf-8"
    )
    result = manager.finalize_create_from_knowledge(
        dest,
        workspace_candidates=[dest],
        existing_skill_names=set(),
    )
    assert result["success"] is True
    assert result["skill"]["name"] == "matplotlib_line_plot"


def test_finalize_create_from_knowledge_historical_rejects(
    manager: SkillManager, tmp_path: Path
) -> None:
    existing = manager._skills_dir / "matplotlib_line_plot"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text(
        _skill_md("matplotlib_line_plot", "old desc"), encoding="utf-8"
    )
    out = tmp_path / "gen"
    skill_root = out / "matplotlib_line_plot"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        _skill_md("matplotlib_line_plot", "new desc"), encoding="utf-8"
    )
    result = manager.finalize_create_from_knowledge(
        out,
        existing_skill_names={"matplotlib_line_plot"},
    )
    assert result["success"] is False
    assert result["code"] == ERROR_SKILL_ALREADY_EXISTS
    assert result["skill_name"] == "matplotlib_line_plot"
    assert "pending_id" not in result
    old = (existing / "SKILL.md").read_text(encoding="utf-8")
    assert "old desc" in old


def test_parse_overwrite_rejects_false_string() -> None:
    assert SkillManager._parse_overwrite("false") is False
    assert SkillManager._parse_overwrite("0") is False
    assert SkillManager._parse_overwrite("true") is True
    assert SkillManager._parse_overwrite(True) is True
    assert SkillManager._parse_overwrite(False) is False


@pytest.mark.asyncio
async def test_import_upload_overwrite_false_string_does_not_force(
    manager: SkillManager, tmp_path: Path
) -> None:
    existing = manager._skills_dir / "document-review"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text(
        _skill_md("document-review", "old"), encoding="utf-8"
    )
    zip_path = tmp_path / "document-review.zip"
    zip_path.write_bytes(_zip_bytes("document-review"))
    with pytest.raises(SkillRpcError) as exc:
        await manager.handle_skills_import_upload(
            {"path": str(zip_path), "overwrite": "false"}
        )
    assert exc.value.code == ERROR_SKILL_ALREADY_EXISTS
    assert "old" in (existing / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_create_from_knowledge_early_rejects_existing_name(
    manager: SkillManager,
) -> None:
    existing = manager._skills_dir / "simple_plot"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text(_skill_md("simple_plot"), encoding="utf-8")

    payload = await manager.handle_skills_create_from_knowledge(
        {"link": "https://matplotlib.org/stable/gallery/simple_plot.html"}
    )
    assert payload["success"] is False
    assert payload["code"] == ERROR_SKILL_ALREADY_EXISTS
    assert payload["skill_name"] == "simple_plot"
    assert payload.get("result_type") != "followup"


@pytest.mark.asyncio
async def test_create_from_knowledge_prompt_lists_occupied_names(
    manager: SkillManager,
) -> None:
    existing = manager._skills_dir / "keep-out"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text(_skill_md("keep-out"), encoding="utf-8")

    payload = await manager.handle_skills_create_from_knowledge(
        {"link": "https://example.com/guide"}
    )
    assert payload["result_type"] == "followup"
    assert "keep-out" in payload["followup_prompt"]
    assert "禁止复用" in payload["followup_prompt"]


def _set_preferred_language(monkeypatch: pytest.MonkeyPatch, lang: str) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"preferred_language": lang},
    )


@pytest.mark.asyncio
async def test_create_from_knowledge_prompt_follows_english_preference(
    manager: SkillManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#4899: preferred_language=en must not send the hidden agent a Chinese prompt."""
    _set_preferred_language(monkeypatch, "en")
    existing = manager._skills_dir / "keep-out"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text(_skill_md("keep-out"), encoding="utf-8")

    payload = await manager.handle_skills_create_from_knowledge(
        {
            "link": "https://example.com/guide",
            "skill_name": "risk-grader",
            "skill_description": "output a risk level",
        }
    )
    prompt = payload["followup_prompt"]
    assert payload["result_type"] == "followup"
    assert payload["skills"] == ["skill-omni-creation"]
    assert prompt.startswith("Generate a complete, installable Skill")
    assert str(payload["output_dir"]) in prompt
    assert "never write to workspace/skills/<name>" in prompt
    assert "skip code-writer/code-verifier" in prompt
    assert "Installed Skill names (do not reuse): keep-out" in prompt
    assert "risk-grader" in prompt
    assert "output a risk level" in prompt
    assert "Knowledge source link: https://example.com/guide" in prompt
    assert re.search(r"[\u4e00-\u9fff]", prompt) is None


@pytest.mark.asyncio
async def test_create_from_knowledge_file_prompt_english(
    manager: SkillManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_preferred_language(monkeypatch, "en")
    doc = tmp_path / "guide.pdf"
    doc.write_bytes(b"%PDF-1.4")
    payload = await manager.handle_skills_create_from_knowledge({"file_path": str(doc)})
    assert payload["skills"] == ["skill-creator-router"]
    assert "Absolute path of the local knowledge document: " in payload["followup_prompt"]


@pytest.mark.asyncio
async def test_create_from_knowledge_prompt_stays_chinese_for_zh(
    manager: SkillManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_preferred_language(monkeypatch, "zh")
    payload = await manager.handle_skills_create_from_knowledge(
        {"link": "https://example.com/guide"}
    )
    assert payload["followup_prompt"].startswith("请根据以下知识来源生成")


@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        ("en", "No valid Skill was found in the output directory (SKILL.md is missing)"),
        ("zh", "未在输出目录找到有效 Skill（缺少 SKILL.md）"),
    ],
)
def test_finalize_create_from_knowledge_missing_skill_md_follows_language(
    manager: SkillManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lang: str,
    expected: str,
) -> None:
    _set_preferred_language(monkeypatch, lang)
    out = tmp_path / "empty-gen"
    out.mkdir()
    with pytest.raises(SkillRpcError) as exc:
        manager.finalize_create_from_knowledge(out, existing_skill_names=set())
    assert exc.value.code == ERROR_SKILL_INVALID_PACKAGE
    assert expected in str(exc.value)


def test_knowledge_language_falls_back_to_chinese_when_config_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.skill import skill_manager as sm

    def _boom():
        raise OSError("config missing")

    monkeypatch.setattr("jiuwenswarm.common.config.get_config", _boom)
    assert sm._knowledge_skill_language() == "zh"


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
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

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
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    swarm = interface_module.JiuWenSwarm()
    swarm._skill_manager = MagicMock()
    swarm._skill_manager.skills_dir = skills_dir
    swarm._skill_manager.list_installed_skill_dir_names = MagicMock(return_value=set())
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
                "workspace_path": str(skills_dir / "from-link"),
            },
        }
    )
    swarm._refresh_skill_rails_after_change = AsyncMock()
    swarm.create_instance = AsyncMock()
    swarm._reload_team_skill_rails = AsyncMock()

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
    assert impl_calls[0]["session_id"] == "skills-knowledge-req-knowledge"
    assert ":" not in impl_calls[0]["session_id"]
    swarm.create_instance.assert_awaited_once()
    swarm._reload_team_skill_rails.assert_awaited_once_with("user-session")
    swarm._skill_manager.finalize_create_from_knowledge.assert_called_once()
    finalize_kwargs = swarm._skill_manager.finalize_create_from_knowledge.call_args.kwargs
    assert "overwrite" not in finalize_kwargs
    assert "pending_id" not in finalize_kwargs
    assert finalize_kwargs.get("existing_skill_names") == set()


def test_build_skills_knowledge_followup_session_id_is_windows_safe() -> None:
    """临时 session_id 不得含 ':'，否则 Windows 下 sessions 目录 mkdir 会 WinError 267."""
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    request = AgentRequest(
        request_id="req_mttf7ps6_21",
        channel_id="web",
        session_id="user-session",
        req_method=ReqMethod.SKILLS_CREATE_FROM_KNOWLEDGE,
        params={"link": "https://example.com"},
    )
    followup = interface_module.JiuWenSwarm._build_skills_knowledge_followup_request(
        request,
        followup="generate",
        skills=["skill-omni-creation"],
        trusted_dirs=[],
        input_file="",
    )
    assert followup.session_id == "skills-knowledge-req_mttf7ps6_21"
    assert ":" not in followup.session_id
    assert re.fullmatch(
        r"^[A-Za-z0-9_](?:[A-Za-z0-9_.-]{0,78}[A-Za-z0-9_])?$",
        followup.session_id,
    )


def test_parse_multipart_roundtrip() -> None:
    content_type, body = _multipart(
        {"link": "https://a.example", "skill_description": "desc"}
    )
    fields = parse_multipart_form(content_type, body)
    assert fields["link"] == "https://a.example"
    assert fields["skill_description"] == "desc"


async def _serve_skill_rpc_mock_server(handler: Any) -> Any:
    """在空闲端口起一个协议兼容的 Mock AgentServer，返回 (server, uri)."""
    import socket

    from websockets.legacy.server import serve as legacy_serve

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = await legacy_serve(handler, "127.0.0.1", port)
    return server, f"ws://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_skill_rpc_client_skips_ack_and_decoy_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最小 WS 客户端须先消费 connection.ack，并按 request_id 丢弃无关帧。"""
    from e2a.wire_codec import encode_agent_response_for_wire
    from jiuwenswarm.server.runtime.skill import skills_multipart_http as mod

    seen: list[dict[str, Any]] = []

    async def handler(ws: Any) -> None:
        await ws.send(json.dumps({"type": "event", "event": "connection.ack", "params": {}}))
        request = json.loads(await ws.recv())
        seen.append(request)
        # 先发一条 request_id 不匹配的帧，再发迟到的事件帧，最后才是真正的响应。
        decoy = encode_agent_response_for_wire(
            AgentResponse(request_id="someone-else", channel_id="web", ok=True, payload={}),
            response_id="someone-else",
        )
        await ws.send(json.dumps(decoy, ensure_ascii=False))
        await ws.send(json.dumps({"type": "event", "event": "connection.ack", "params": {}}))
        good = encode_agent_response_for_wire(
            AgentResponse(
                request_id=request["request_id"],
                channel_id="web",
                ok=True,
                payload={"success": True, "skill": {"name": "from-ws"}},
            ),
            response_id=request["request_id"],
        )
        await ws.send(json.dumps(good, ensure_ascii=False))

    server, uri = await _serve_skill_rpc_mock_server(handler)
    monkeypatch.setenv("AGENT_SERVER_URL", uri)
    try:
        payload = await mod._call_agent_skill_rpc(
            method=ReqMethod.SKILLS_IMPORT_UPLOAD,
            params={"path": "/tmp/x.zip", "overwrite": False},
            timeout_s=10.0,
        )
    finally:
        server.close()
        await server.wait_closed()

    assert payload["success"] is True
    assert payload["skill"]["name"] == "from-ws"
    assert seen and seen[0]["request_id"].startswith("file-api-")


@pytest.mark.asyncio
async def test_skill_rpc_client_rejects_missing_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未收到 connection.ack 时应直接失败，避免把非 ack 帧当作响应。"""
    from jiuwenswarm.server.runtime.skill import skills_multipart_http as mod

    async def handler(ws: Any) -> None:
        await ws.send(json.dumps({"type": "event", "event": "unexpected"}))

    server, uri = await _serve_skill_rpc_mock_server(handler)
    monkeypatch.setenv("AGENT_SERVER_URL", uri)
    try:
        with pytest.raises(SkillRpcError) as excinfo:
            await mod._call_agent_skill_rpc(
                method=ReqMethod.SKILLS_IMPORT_UPLOAD,
                params={"path": "/tmp/x.zip"},
                timeout_s=10.0,
            )
    finally:
        server.close()
        await server.wait_closed()

    assert "connection.ack" in excinfo.value.message


@pytest.mark.asyncio
@pytest.mark.parametrize("name_line", ["", "name: ''\n", "name: '   '\n", "name: 42\n", "name: [foo]\n", "name: null\n"])
async def test_import_upload_requires_explicit_string_name(manager: SkillManager, tmp_path: Path, name_line: str) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("pkg/SKILL.md", f"---\n{name_line}description: demo\n---\nBody")
    path = tmp_path / "invalid.zip"
    path.write_bytes(buf.getvalue())
    before = set(manager._skills_dir.iterdir())
    with pytest.raises(SkillRpcError) as error:
        await manager.handle_skills_import_upload({"path": str(path)})
    assert error.value.code == "SKILL_INVALID_METADATA"
    assert set(manager._skills_dir.iterdir()) == before


@pytest.mark.asyncio
async def test_import_upload_allows_explicit_skill_filename_name(manager: SkillManager, tmp_path: Path) -> None:
    path = tmp_path / "valid.zip"
    path.write_bytes(_zip_bytes("SKILL"))
    result = await manager.handle_skills_import_upload({"path": str(path)})
    assert result["success"] is True
    assert result["skill"]["name"] == "SKILL"


@pytest.mark.asyncio
async def test_import_upload_rejects_original_nul_filename(manager: SkillManager, tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("pkg/SKILL.md", _skill_md("nul-test"))
        archive.writestr("pkg/evXool.md", "payload")
    path = tmp_path / "nul.zip"
    path.write_bytes(buf.getvalue().replace(b"evXool.md", b"ev\x00ool.md"))
    with zipfile.ZipFile(path) as archive:
        assert archive.infolist()[1].orig_filename == "pkg/ev\x00ool.md"
        assert archive.infolist()[1].filename == "pkg/ev"
        extraction = tmp_path / "extraction"
        extraction.mkdir()
        with pytest.raises(SkillRpcError) as error:
            manager._safe_extract_zip_members_into(archive, extraction.resolve())
        assert error.value.code == "SKILL_UNSAFE_PATH"
        assert list(extraction.iterdir()) == []
    with pytest.raises(SkillRpcError) as error:
        await manager.handle_skills_import_upload({"path": str(path)})
    assert error.value.code == "SKILL_UNSAFE_PATH"
    assert not (manager._skills_dir / "nul-test").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("as_directory", [False, True])
async def test_import_local_requires_declared_name(
    manager: SkillManager, tmp_path: Path, as_directory: bool,
) -> None:
    source = tmp_path / "local-input"
    source.mkdir()
    md = source / "SKILL.md"
    md.write_text("---\ndescription: demo\n---\nBody", encoding="utf-8")
    result = await manager.handle_skills_import_local({"path": str(source if as_directory else md)})
    assert result["success"] is False
    assert result["code"] == "SKILL_INVALID_METADATA"
    assert "非空字符串 name" in result["detail"]
    assert not (manager._skills_dir / "SKILL").exists()


@pytest.mark.asyncio
async def test_import_upload_still_rejects_zip_slip(manager: SkillManager, tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("pkg/SKILL.md", _skill_md("unsafe-test"))
        archive.writestr("../escape.md", "payload")
    path = tmp_path / "unsafe.zip"
    path.write_bytes(buf.getvalue())
    with pytest.raises(SkillRpcError) as error:
        await manager.handle_skills_import_upload({"path": str(path)})
    assert error.value.code == "SKILL_UNSAFE_PATH"
    assert not (manager._skills_dir / "unsafe-test").exists()


@pytest.mark.asyncio
@pytest.mark.usefixtures("allow_macos_pytest_temp_sources")
@pytest.mark.parametrize("entry", ["file", "directory", "upload", "trusted"])
@pytest.mark.parametrize(
    "frontmatter",
    [
        "description: demo",
        "name: ''\ndescription: demo",
        "name: '   '\ndescription: demo",
        "name: 42\ndescription: demo",
        "name: true\ndescription: demo",
        "name: [demo]\ndescription: demo",
        "name: {id: demo}\ndescription: demo",
        "name: null\ndescription: demo",
        "name: valid",
        "name: valid\ndescription: '   '",
        "name: valid\ndescription: 42",
        "name: valid\ndescription: false",
        "name: valid\ndescription: [demo]",
        "name: valid\ndescription: null",
        "name: ../escape\ndescription: demo",
        "[name, description]",
        "name: [unterminated\ndescription: demo",
    ],
)
async def test_import_entries_reject_invalid_metadata_without_writes(
    manager: SkillManager, tmp_path: Path, entry: str, frontmatter: str,
) -> None:
    source = tmp_path / "input-folder"
    source.mkdir()
    md = source / "SKILL.md"
    md.write_text(f"---\n{frontmatter}\n---\nBody", encoding="utf-8")
    before = {path.relative_to(manager.skills_dir): path.read_bytes()
              for path in manager.skills_dir.rglob("*") if path.is_file()}
    before_dirs = set(manager.skills_dir.rglob("*"))
    if entry == "upload":
        package = tmp_path / "input.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.write(md, "input-folder/SKILL.md")
        with pytest.raises(SkillRpcError) as error:
            await manager.handle_skills_import_upload({"path": str(package)})
        assert error.value.code == "SKILL_INVALID_METADATA"
    else:
        if entry == "trusted":
            result = manager._import_local_from_path(
                source, force=False, origin="https://allowed-host/input.zip", source_trusted=True,
            )
        else:
            result = await manager.handle_skills_import_local(
                {"path": str(md if entry == "file" else source)},
            )
        assert result["success"] is False
        assert result["code"] == "SKILL_INVALID_METADATA"
    assert set(manager.skills_dir.rglob("*")) == before_dirs
    assert {path.relative_to(manager.skills_dir): path.read_bytes()
            for path in manager.skills_dir.rglob("*") if path.is_file()} == before


@pytest.mark.asyncio
@pytest.mark.usefixtures("allow_macos_pytest_temp_sources")
@pytest.mark.parametrize("entry", ["file", "directory", "upload", "trusted"])
async def test_import_entries_accept_leading_blanks_and_normalize_metadata(
    manager: SkillManager, tmp_path: Path, entry: str,
) -> None:
    source = tmp_path / "different-folder-name"
    source.mkdir()
    md = source / "SKILL.md"
    md.write_text(
        "\n  \n---\nname: ' SKILL '\ndescription: >-\n  Demo skill\n  with multiple lines\n---\nBody",
        encoding="utf-8",
    )
    if entry == "upload":
        package = tmp_path / "input.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.write(md, "different-folder-name/SKILL.md")
        result = await manager.handle_skills_import_upload({"path": str(package)})
    elif entry == "trusted":
        result = manager._import_local_from_path(
            source, force=False, origin="https://allowed-host/input.zip", source_trusted=True,
        )
    else:
        result = await manager.handle_skills_import_local(
            {"path": str(md if entry == "file" else source)},
        )
    assert result["success"] is True
    assert result["skill"]["name"] == "SKILL"
    assert result["skill"]["description"] == "Demo skill with multiple lines"
    assert (manager.skills_dir / "SKILL" / "SKILL.md").read_text(encoding="utf-8") == md.read_text(encoding="utf-8")


@pytest.mark.parametrize("text", ["# Legacy skill", "---\ndescription: legacy\n---\nBody"])
def test_display_parser_keeps_filename_fallback(tmp_path: Path, text: str) -> None:
    md = tmp_path / "legacy-name.md"
    md.write_text(text, encoding="utf-8")
    assert SkillManager._parse_skill_md(md)["name"] == "legacy-name"


@pytest.mark.parametrize("member", ["../escape.md", "pkg/../../escape.md", "/absolute.md", "C:/escape.md", r"pkg\..\escape.md"])
def test_zip_rejects_unsafe_paths_before_extracting_any_member(
    tmp_path: Path, member: str,
) -> None:
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("pkg/SKILL.md", _skill_md("valid"))
        archive.writestr(member, "unsafe")
    target = tmp_path / "extract"
    target.mkdir()
    with zipfile.ZipFile(package) as archive:
        with pytest.raises(RuntimeError, match="非法路径"):
            SkillManager._safe_extract_zip_members_into(archive, target.resolve())
    assert list(target.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["remote", "hub", "clawhub", "skillnet"])
@pytest.mark.parametrize(
    ("frontmatter", "valid"),
    [
        ("name: remote-demo\ndescription: >-\n  Remote skill\n  description", True),
        ("description: demo", False),
        ("name: 42\ndescription: demo", False),
        ("name: remote-demo", False),
        ("name: remote-demo\ndescription: 42", False),
        ("name: remote-demo\ndescription: [demo]", False),
    ],
)
async def test_external_install_entries_share_metadata_rules(
    manager: SkillManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    entry: str, frontmatter: str, valid: bool,
) -> None:
    source = tmp_path / "downloaded-source"
    source.mkdir()
    md = source / "SKILL.md"
    md.write_text(f"\n---\n{frontmatter}\n---\nBody", encoding="utf-8")
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.write(md, "downloaded-source/SKILL.md")
    body = package.getvalue()
    url = "https://openjiuwen-market.obs.ap-southeast-1.myhuaweicloud.com/plugins/demo.zip"
    before = {path.relative_to(manager.skills_dir): path.read_bytes()
              for path in manager.skills_dir.rglob("*") if path.is_file()}
    before_dirs = set(manager.skills_dir.rglob("*"))
    monkeypatch.setattr(manager, "_get_mirror_skills_dirs", lambda: [])
    if entry == "remote":
        response = MagicMock()
        response.iter_content.return_value = [body]
        response.__enter__.return_value = response
        session = MagicMock()
        session.get.return_value = response
        session.__enter__.return_value = session
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.skill.skill_manager.requests.Session", lambda: session,
        )
        result = await manager.handle_skills_import_local({"path": url})
    elif entry == "hub":
        monkeypatch.setattr(manager, "_team_skills_hub_http_get_data", AsyncMock(return_value={
            "download_url": url, "version": "1.0.0",
        }))
        monkeypatch.setattr(manager, "_download_zip_and_verify", AsyncMock(return_value=body))
        try:
            result = await manager.handle_skills_team_skills_hub_install({"asset_id": "remote-demo"})
        except SkillRpcError as exc:
            assert not valid
            result = {"success": False, "code": exc.code}
    elif entry == "clawhub":
        response = MagicMock(content=body)
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(return_value=response)
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.skill.skill_manager.httpx.AsyncClient", lambda **kwargs: client,
        )
        monkeypatch.setattr(manager, "_get_clawhub_token", lambda: "test-token")
        result = await manager.handle_skills_clawhub_download({"slug": "remote-demo"})
    else:
        monkeypatch.setattr(manager, "_skillnet_download_sync", lambda *args: str(source))
        result = manager._skillnet_install_files_sync("https://github.com/example/skills/tree/main/demo", False)
    assert result.get("success", result.get("ok")) is valid, result
    if valid:
        assert (manager.skills_dir / "remote-demo" / "SKILL.md").is_file()
        meta = manager._parse_skill_md(manager.skills_dir / "remote-demo" / "SKILL.md")
        assert meta["description"] == "Remote skill description"
    else:
        assert result["code"] == "SKILL_INVALID_METADATA", result
        assert set(manager.skills_dir.rglob("*")) == before_dirs
        assert {path.relative_to(manager.skills_dir): path.read_bytes()
                for path in manager.skills_dir.rglob("*") if path.is_file()} == before
