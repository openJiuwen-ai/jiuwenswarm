# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Skill 上传资格门禁（``skill_upload_permission``）单元测试。"""

from __future__ import annotations

import json
import tempfile
import threading
import urllib.error
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.request import Request

import pytest

from jiuwenswarm.channels.web.skill_upload_permission import (
    SkillUploadPermissionError,
    check_skill_upload_permission,
    enforcement_enabled,
    reset_cache_for_test,
)


class _FakeResponse:
    def __init__(self, status: int, body: dict[str, Any]):
        self.status = status
        self._body = json.dumps(body).encode("utf-8")

    def read(self, amount: int = -1) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def _manager_response_ok(allowed: bool) -> _FakeResponse:
    return _FakeResponse(200, {"code": 200, "message": "success", "data": {"allowed": allowed, "via": "user"}})


@pytest.fixture(autouse=True)
def _reset_cache():
    reset_cache_for_test()
    yield
    reset_cache_for_test()


@pytest.fixture
def enforce(monkeypatch: pytest.MonkeyPatch, manager_api_url: str):
    monkeypatch.setenv("SKILL_UPLOAD_PERMISSION_ENFORCE", "1")
    monkeypatch.setenv("MANAGER_API_BASE_URL", manager_api_url)


@pytest.fixture
def manager_api_url() -> str:
    return "http://manager.test"


# ---------------------------------------------------------------------------
# 开关语义
# ---------------------------------------------------------------------------


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SKILL_UPLOAD_PERMISSION_ENFORCE", raising=False)
    assert not enforcement_enabled()
    # 未启用时即使缺 token 也放行（个人版语义不变）
    assert check_skill_upload_permission(authorization=None) is None


def test_enabled_requires_manager_base_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SKILL_UPLOAD_PERMISSION_ENFORCE", "true")
    monkeypatch.delenv("MANAGER_API_BASE_URL", raising=False)
    # 未配置基址：请求构造失败（invalid url），fail-closed 返回 503
    with pytest.raises(SkillUploadPermissionError) as exc:
        check_skill_upload_permission(authorization="Bearer token-1")
    assert exc.value.status == 503


# ---------------------------------------------------------------------------
# 令牌提取
# ---------------------------------------------------------------------------


def test_missing_token_rejected_401():
    with pytest.raises(SkillUploadPermissionError) as exc:
        check_skill_upload_permission(authorization=None)
    assert exc.value.status == 401
    assert exc.value.payload["code"] == "SKILL_UPLOAD_PERMISSION_REQUIRED"


def test_token_from_bearer_header_preferred(monkeypatch: pytest.MonkeyPatch, enforce):
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Request, timeout: float):
        captured["authorization"] = request.headers.get("Authorization")
        return _manager_response_ok(True)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    check_skill_upload_permission(authorization="Bearer header-token", access_cookie="cookie-token")
    assert captured["authorization"] == "Bearer header-token"


def test_token_fallback_to_cookie(monkeypatch: pytest.MonkeyPatch, enforce):
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Request, timeout: float):
        captured["authorization"] = request.headers.get("Authorization")
        return _manager_response_ok(True)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    check_skill_upload_permission(authorization="", access_cookie="cookie-token")
    assert captured["authorization"] == "Bearer cookie-token"


# ---------------------------------------------------------------------------
# 校验结果
# ---------------------------------------------------------------------------


def test_allowed_passes(monkeypatch: pytest.MonkeyPatch, enforce):
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: _manager_response_ok(True))
    assert check_skill_upload_permission(authorization="Bearer token-1") is None


def test_not_allowed_rejected_403(monkeypatch: pytest.MonkeyPatch, enforce):
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: _manager_response_ok(False))
    with pytest.raises(SkillUploadPermissionError) as exc:
        check_skill_upload_permission(authorization="Bearer token-1")
    assert exc.value.status == 403
    assert exc.value.payload["code"] == "SKILL_UPLOAD_PERMISSION_REQUIRED"


def test_manager_401_passthrough(monkeypatch: pytest.MonkeyPatch, enforce):
    def fake_urlopen(*args, **kwargs):
        raise urllib.error.HTTPError("url", 401, "unauthorized", hdrs=None, fp=None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(SkillUploadPermissionError) as exc:
        check_skill_upload_permission(authorization="Bearer token-1")
    assert exc.value.status == 401


def test_manager_down_fail_closed(monkeypatch: pytest.MonkeyPatch, enforce):
    def fake_urlopen(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(SkillUploadPermissionError) as exc:
        check_skill_upload_permission(authorization="Bearer token-1")
    assert exc.value.status == 503
    assert exc.value.payload["code"] == "SKILL_UPLOAD_PERMISSION_CHECK_UNAVAILABLE"


def test_malformed_body_fail_closed(monkeypatch: pytest.MonkeyPatch, enforce):
    class _Broken:
        status = 200

        def read(self, amount: int = -1) -> bytes:
            return b"not-json"

        def __enter__(self) -> "_Broken":
            return self

        def __exit__(self, *exc_info: object) -> bool:
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: _Broken())
    with pytest.raises(SkillUploadPermissionError) as exc:
        check_skill_upload_permission(authorization="Bearer token-1")
    assert exc.value.status == 503


def test_verdict_cached_per_token(monkeypatch: pytest.MonkeyPatch, enforce):
    calls: list[int] = []

    def fake_urlopen(*args, **kwargs):
        calls.append(1)
        return _manager_response_ok(False)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    for _ in range(3):
        with pytest.raises(SkillUploadPermissionError):
            check_skill_upload_permission(authorization="Bearer token-1")
    assert len(calls) == 1
    # 不同 token 不互串
    with pytest.raises(SkillUploadPermissionError):
        check_skill_upload_permission(authorization="Bearer token-2")
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# app_web 接线（真实 HTTP handler POST 流程）
# ---------------------------------------------------------------------------


@pytest.fixture
def upload_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    upload_root = tmp_path / "temp-upload-root"
    upload_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(upload_root))
    from jiuwenswarm.channels.web.app_web import _SpaStaticHandler

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


def test_upload_temp_gate_blocks_unauthorized(upload_server, monkeypatch: pytest.MonkeyPatch, enforce):
    def fake_urlopen(*args, **kwargs):
        return _manager_response_ok(False)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    port, upload_root = upload_server
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("POST", "/file-api/skills/upload-temp", body=b"", headers={})
    response = connection.getresponse()
    payload = json.loads(response.read())
    assert response.status == 403
    assert payload["code"] == "SKILL_UPLOAD_PERMISSION_REQUIRED"
    connection.close()
    # 未写盘
    assert list(upload_root.iterdir()) == []


def test_upload_temp_gate_blocks_missing_token(upload_server, monkeypatch: pytest.MonkeyPatch, enforce):
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: _manager_response_ok(True))
    port, _ = upload_server
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("POST", "/file-api/skills/upload-temp", body=b"", headers={})
    response = connection.getresponse()
    payload = json.loads(response.read())
    assert response.status == 401
    assert payload["code"] == "SKILL_UPLOAD_PERMISSION_REQUIRED"
    connection.close()


def test_upload_temp_gate_allows_authorized(upload_server, monkeypatch: pytest.MonkeyPatch, enforce):
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: _manager_response_ok(True))
    port, upload_root = upload_server
    boundary = "----testboundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="a.zip"\r\n'
        "Content-Type: application/zip\r\n\r\n"
        "not-a-real-zip-content\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(
        "POST",
        "/file-api/skills/upload-temp",
        body=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": "Bearer token-ok",
        },
    )
    response = connection.getresponse()
    payload = json.loads(response.read())
    # 资格放行后走正常落盘逻辑（非法 zip 会被后续校验拒绝，但不应是权限 40x）
    assert response.status in (200, 400)
    if response.status == 400:
        assert payload.get("code") != "SKILL_UPLOAD_PERMISSION_REQUIRED"
    connection.close()


def test_import_gate_blocks_unauthorized(upload_server, monkeypatch: pytest.MonkeyPatch, enforce):
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: _manager_response_ok(False))
    port, _ = upload_server
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("POST", "/file-api/skills/import", body=b"", headers={})
    response = connection.getresponse()
    payload = json.loads(response.read())
    assert response.status == 403
    assert payload["code"] == "SKILL_UPLOAD_PERMISSION_REQUIRED"
    connection.close()


def test_gate_disabled_by_env_leaves_endpoints_open(upload_server, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SKILL_UPLOAD_PERMISSION_ENFORCE", raising=False)
    monkeypatch.setenv("MANAGER_API_BASE_URL", "http://should-not-be-called.invalid")
    def _no_network(*args, **kwargs):
        raise AssertionError("network call must not happen when enforcement disabled")

    monkeypatch.setattr("urllib.request.urlopen", _no_network)
    port, _ = upload_server
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("POST", "/file-api/skills/upload-temp", body=b"", headers={})
    response = connection.getresponse()
    connection.close()
    assert response.status in (400, 200)
