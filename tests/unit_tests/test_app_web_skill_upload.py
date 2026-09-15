# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Production static-server upload contract used by the packaged desktop app."""

from __future__ import annotations

import io
import json
import tempfile
import threading
import zipfile
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from jiuwenswarm.channels.web.app_web import _SpaStaticHandler
from jiuwenswarm.server.runtime.skill import skill_manager as skill_manager_module
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


@pytest.fixture
def upload_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    upload_root = tmp_path / "临时 文件"
    upload_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(upload_root))

    class Handler(_SpaStaticHandler):
        project_root = tmp_path
        workspace_root = tmp_path / "agent"
        agent_teams_root = tmp_path / "agent-teams"
        logs_root = tmp_path / "logs"
        auto_harness_root = tmp_path / "auto-harness"
        api_target = ""
        ws_target = ""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server.server_port, upload_root
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


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
    monkeypatch.setattr(skill_manager_module, "get_agent_skills_dir", lambda: skills_dir)
    monkeypatch.setattr(
        skill_manager_module, "get_builtin_skills_dir", lambda: tmp_path / "builtin_missing"
    )
    monkeypatch.setattr(skill_manager_module, "_get_agent_root_dir", lambda: tmp_path)
    monkeypatch.setattr(
        skill_manager_module, "_get_marketplace_dir", lambda: skills_dir / "_marketplace"
    )
    monkeypatch.setattr(skill_manager_module, "_get_state_file", lambda: state_file)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skilldev.state_utils.get_state_file",
        lambda: state_file,
    )
    return SkillManager()


def _post_upload(port: int, filename: str | None, content: bytes = b""):
    boundary = "----PackagedSkillUploadBoundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="overwrite"\r\n\r\n'
        "false\r\n"
    ).encode()
    if filename is not None:
        quoted_filename = filename.replace("\\", "\\\\").replace('"', '\\"')
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{quoted_filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8") + content + b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(
            "POST",
            "/file-api/skills/upload-temp",
            body=body,
            headers={"Content-Type": f'multipart/form-data; boundary="{boundary}"'},
        )
        response = connection.getresponse()
        assert response.headers["Content-Type"].startswith("application/json")
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def _skill_zip(asset: bytes) -> bytes:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr(
            "desktop-upload/SKILL.md",
            "---\nname: desktop-upload\ndescription: 桌面技能上传测试\n---\n# Upload\n",
        )
        package.writestr("desktop-upload/assets/数据.bin", asset)
    return archive.getvalue()


@pytest.mark.asyncio
async def test_uploaded_zip_remains_available_and_can_be_imported(upload_server, manager):
    port, upload_root = upload_server
    asset = b"\x00\xff\r\n----PackagedSkillUploadBoundary-inside-file\r\n"
    package = _skill_zip(asset)
    other_package = _skill_zip(b"second upload")

    status, payload = _post_upload(port, "中文技能.zip", package)
    assert status == 200, payload
    uploaded = Path(payload["path"])
    assert uploaded.is_absolute()
    assert uploaded.resolve().is_relative_to(upload_root.resolve())
    assert uploaded.name == "中文技能.zip"
    assert uploaded.read_bytes() == package

    status, second_payload = _post_upload(port, "中文技能.zip", other_package)
    assert status == 200, second_payload
    second_upload = Path(second_payload["path"])
    assert second_upload != uploaded
    assert second_upload.resolve().is_relative_to(upload_root.resolve())
    assert second_upload.read_bytes() == other_package
    assert uploaded.read_bytes() == package

    result = await manager.handle_skills_import_upload(
        {"path": str(uploaded), "overwrite": False}
    )
    assert result["success"] is True
    assert result["skill"]["name"] == "desktop-upload"
    workspace = Path(result["skill"]["workspace_path"])
    assert (workspace / "SKILL.md").is_file()
    assert (workspace / "assets" / "数据.bin").read_bytes() == asset


def test_upload_without_file_returns_actionable_error(upload_server):
    port, upload_root = upload_server
    status, payload = _post_upload(port, None)
    assert status == 400
    assert payload["code"] == "SKILL_INVALID_PACKAGE"
    assert "path" not in payload
    assert list(upload_root.iterdir()) == []


@pytest.mark.parametrize("filename", ["../技能.zip", r"..\技能.zip", "C:技能.zip"])
def test_upload_rejects_filename_paths(upload_server, filename):
    port, upload_root = upload_server
    status, payload = _post_upload(port, filename, b"uploaded content")
    assert status == 400
    assert payload["code"] == "SKILL_UNSAFE_PATH"
    assert "path" not in payload
    assert list(upload_root.iterdir()) == []
