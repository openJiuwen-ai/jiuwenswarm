# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Production static-server upload contract used by the packaged desktop app."""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import zipfile
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from jiuwenswarm.channels.web.app_web import _SpaStaticHandler
from jiuwenswarm.server.runtime.skill import skill_manager as skill_manager_module
from jiuwenswarm.server.runtime.skill.skill_manager import (
    ERROR_SKILL_ALREADY_EXISTS,
    ERROR_SKILL_INVALID_PACKAGE,
    SkillManager,
    SkillRpcError,
)
from jiuwenswarm.server.runtime.skill.skills_multipart_http import (
    SKILL_UPLOAD_PREFIX,
    cleanup_stale_skill_uploads,
    discard_skill_upload,
)


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


def _post_upload(
    port: int, filename: str | None, content: bytes = b"",
    *, endpoint: str = "/file-api/skills/upload-temp",
):
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
            endpoint,
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
    assert uploaded.parent.name.startswith(SKILL_UPLOAD_PREFIX)
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
    # 导入成功后源 zip 及其专属目录被回收；未导入的另一份不受影响
    assert not uploaded.exists()
    assert not uploaded.parent.exists()
    assert second_upload.is_file()


@pytest.mark.asyncio
async def test_conflict_keeps_upload_until_overwrite(upload_server, manager):
    port, _ = upload_server
    package = _skill_zip(b"asset")

    status, payload = _post_upload(port, "dup.zip", package)
    assert status == 200, payload
    first = Path(payload["path"])
    await manager.handle_skills_import_upload({"path": str(first), "overwrite": False})
    assert not first.exists()

    status, payload = _post_upload(port, "dup.zip", package)
    assert status == 200, payload
    second = Path(payload["path"])
    with pytest.raises(SkillRpcError) as exc_info:
        await manager.handle_skills_import_upload({"path": str(second), "overwrite": False})
    assert exc_info.value.code == ERROR_SKILL_ALREADY_EXISTS
    # 同名冲突保留文件，等待覆盖确认后的二次导入
    assert second.is_file()

    result = await manager.handle_skills_import_upload(
        {"path": str(second), "overwrite": True}
    )
    assert result["success"] is True
    assert not second.exists()
    assert not second.parent.exists()


@pytest.mark.asyncio
async def test_invalid_package_import_failure_reclaims_upload(upload_server, manager):
    """非法包（非同名冲突）导入失败后，上传文件与专属目录都应被回收."""
    port, _ = upload_server
    status, payload = _post_upload(port, "broken.zip", b"not-a-zip")
    assert status == 200, payload
    uploaded = Path(payload["path"])
    assert uploaded.is_file()

    with pytest.raises(SkillRpcError) as exc_info:
        await manager.handle_skills_import_upload({"path": str(uploaded), "overwrite": False})
    assert exc_info.value.code == ERROR_SKILL_INVALID_PACKAGE
    assert not uploaded.exists()
    assert not uploaded.parent.exists()


@pytest.mark.asyncio
async def test_unexpected_import_error_reclaims_upload(upload_server, manager, monkeypatch):
    """解包阶段抛出非 SkillRpcError 时，兜底分支同样回收上传目录."""
    port, _ = upload_server
    status, payload = _post_upload(port, "pkg.zip", _skill_zip(b"asset"))
    assert status == 200, payload
    uploaded = Path(payload["path"])

    def _boom(*args, **kwargs):
        raise RuntimeError("extract failed")

    monkeypatch.setattr(manager, "_safe_extract_zip_to_dir", _boom)
    with pytest.raises(RuntimeError):
        await manager.handle_skills_import_upload({"path": str(uploaded), "overwrite": False})
    assert not uploaded.exists()
    assert not uploaded.parent.exists()


def test_discard_rejects_path_outside_upload_root(upload_server, tmp_path):
    outside = tmp_path / "outside.zip"
    outside.write_bytes(b"keep")
    assert discard_skill_upload(str(outside)) is False
    assert outside.is_file()


def test_stale_dangling_symlink_upload_dirs_are_cleaned(upload_server):
    _, upload_root = upload_server
    link = upload_root / f"{SKILL_UPLOAD_PREFIX}dangling"
    try:
        link.symlink_to(upload_root / "missing-target")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted on this platform")

    # 负数 TTL 让所有候选直接过期，无需设置 symlink 自身的 mtime。
    assert cleanup_stale_skill_uploads(max_age_seconds=-1) == 1
    assert not link.is_symlink()


def test_stale_upload_dirs_are_cleaned(upload_server):
    _, upload_root = upload_server
    fresh = upload_root / f"{SKILL_UPLOAD_PREFIX}fresh"
    stale = upload_root / f"{SKILL_UPLOAD_PREFIX}stale"
    legacy = upload_root / f"{SKILL_UPLOAD_PREFIX}legacy"
    for directory in (fresh, stale, legacy):
        directory.mkdir()
        (directory / "pkg.zip").write_bytes(b"zip")

    expired = time.time() - 2 * 24 * 60 * 60
    for directory in (stale, legacy):
        os.utime(directory, (expired, expired))
        os.utime(directory / "pkg.zip", (expired, expired))

    assert cleanup_stale_skill_uploads() == 2
    assert fresh.is_dir()
    assert not stale.exists()
    assert not legacy.exists()


def test_upload_without_file_returns_actionable_error(upload_server):
    port, upload_root = upload_server
    status, payload = _post_upload(port, None)
    assert status == 400
    assert payload["code"] == "SKILL_INVALID_PACKAGE"
    assert payload["detail_key"] == "skills.errors.uploadMissingFileField"
    assert "path" not in payload
    assert list(upload_root.iterdir()) == []


@pytest.mark.parametrize("filename", ["../技能.zip", r"..\技能.zip", "C:技能.zip"])
def test_upload_rejects_filename_paths(upload_server, filename):
    port, upload_root = upload_server
    status, payload = _post_upload(port, filename, b"uploaded content")
    assert status == 400
    assert payload["code"] == "SKILL_UNSAFE_PATH"
    assert payload["detail_key"] == "skills.errors.uploadUnsafePath"
    assert "path" not in payload
    assert list(upload_root.iterdir()) == []


@pytest.mark.parametrize(
    ("frontmatter", "member", "expected_code"),
    [
        ("name: http-demo\ndescription: >-\n  HTTP skill\n  description", None, None),
        ("description: demo", None, "SKILL_INVALID_METADATA"),
        ("name: 42\ndescription: demo", None, "SKILL_INVALID_METADATA"),
        ("name: http-demo\ndescription: 42", None, "SKILL_INVALID_METADATA"),
        ("name: http-demo\ndescription: demo", "pkg/evXool.md", "SKILL_UNSAFE_PATH"),
        ("name: http-demo\ndescription: demo", "../escape.md", "SKILL_UNSAFE_PATH"),
    ],
)
def test_http_import_validates_before_installing(
    upload_server, manager, monkeypatch, frontmatter, member, expected_code,
):
    port, _ = upload_server
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skills_multipart_http.SkillManager", lambda: manager,
    )
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("pkg/SKILL.md", f"\n---\n{frontmatter}\n---\nBody")
        if member is not None:
            archive.writestr(member, "unsafe")
    body = package.getvalue()
    if member == "pkg/evXool.md":
        body = body.replace(b"evXool.md", b"ev\x00ool.md")
    before = {path.relative_to(manager.skills_dir): path.read_bytes()
              for path in manager.skills_dir.rglob("*") if path.is_file()}
    before_dirs = set(manager.skills_dir.rglob("*"))
    status, payload = _post_upload(port, "skill.zip", body, endpoint="/file-api/skills/import")
    if expected_code is None:
        assert status == 200, payload
        assert payload["success"] is True
        assert payload["skill"]["name"] == "http-demo"
        assert payload["skill"]["description"] == "HTTP skill description"
        assert (manager.skills_dir / "http-demo" / "SKILL.md").is_file()
        state = json.loads((manager.skills_dir / "skills_state.json").read_text(encoding="utf-8"))
        assert any(item["name"] == "http-demo" for item in state["local_skills"])
    else:
        assert status == 400, payload
        assert payload["code"] == expected_code
        assert set(manager.skills_dir.rglob("*")) == before_dirs
        assert {path.relative_to(manager.skills_dir): path.read_bytes()
                for path in manager.skills_dir.rglob("*") if path.is_file()} == before
