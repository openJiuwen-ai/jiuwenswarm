# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JW-87: Web 文件 API 不依赖 profile 迁移兼容链也能服务 ``agent/...`` 请求。"""

from __future__ import annotations

import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest

from jiuwenswarm.channels.web.app_web import (
    _SpaStaticHandler,
    _canonicalize_legacy_agent_path,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("agent/sessions/sess_demo", "profile/agent/sessions/sess_demo"),
        ("agent\\workspace\\todo\\s1", "profile/agent/workspace/todo/s1"),
        ("agent", "profile/agent"),
        ("./agent/.logs/ws-dev.log", "profile/agent/.logs/ws-dev.log"),
        # 非 agent 前缀、绝对路径原样保留
        ("profile/agent/workspace", "profile/agent/workspace"),
        ("workspace/report.md", "workspace/report.md"),
        (r"D:\data\agent\sessions", r"D:\data\agent\sessions"),
        ("/var/log/agent", "/var/log/agent"),
    ],
)
def test_canonicalize_legacy_agent_path(raw: str, expected: str) -> None:
    assert _canonicalize_legacy_agent_path(raw) == expected


@pytest.fixture()
def _web_server(tmp_path: Path):
    """没有 <root>/agent 兼容链，只有 profile/agent 真实目录。"""
    agent_root = (tmp_path / "profile" / "agent").resolve()
    (agent_root / "sessions" / "sess_demo").mkdir(parents=True)
    (agent_root / "sessions" / "sess_demo" / "history.json").write_text("[]", encoding="utf-8")
    (agent_root / "workspace" / "todo" / "sess_demo").mkdir(parents=True)
    (agent_root / "workspace" / "todo" / "sess_demo" / "todo.json").write_text(
        "[]", encoding="utf-8"
    )
    (agent_root / "workspace" / "agent-data.json").write_text(
        '{"agents": []}', encoding="utf-8"
    )
    # 日志兼容链也建不出来时，setup_logger 直接写 profile/agent/.logs
    (agent_root / ".logs").mkdir(parents=True)
    (agent_root / ".logs" / "ws-dev.log").write_text("ws dev log\n", encoding="utf-8")
    # 沙箱之外的敏感文件：路径穿越不应读到
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")

    class Handler(_SpaStaticHandler):
        project_root = tmp_path
        workspace_root = agent_root
        agent_teams_root = tmp_path / "agent-teams"
        logs_root = tmp_path / "logs"
        auto_harness_root = tmp_path / "auto-harness"
        organizations_root = tmp_path / ".organizations"
        projectless_tasks_root = tmp_path / "tasks"
        api_target = ""
        ws_target = ""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _get(port: int, path: str) -> tuple[int, str]:
    connection = HTTPConnection("127.0.0.1", port)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8", "replace")
    finally:
        connection.close()


def test_legacy_agent_file_api_works_without_compat_link(_web_server: int) -> None:
    status, body = _get(
        _web_server, f"/file-api/list-files?dir={quote('agent/sessions/sess_demo')}"
    )
    assert status == 200
    assert "history.json" in body

    status, body = _get(
        _web_server,
        f"/file-api/list-files?dir={quote('agent/workspace/todo/sess_demo')}",
    )
    assert status == 200
    assert "todo.json" in body

    status, body = _get(
        _web_server,
        f"/file-api/file-content?path={quote('agent/workspace/agent-data.json')}",
    )
    assert status == 200
    assert '{"agents": []}' in body

    status, body = _get(
        _web_server,
        f"/file-api/file-content?path={quote('agent/.logs/ws-dev.log')}&tail_bytes=65536",
    )
    assert status == 200
    assert "ws dev log" in body


def test_legacy_agent_path_cannot_escape_with_dotdot(_web_server: int) -> None:
    status, _ = _get(
        _web_server,
        f"/file-api/file-content?path={quote('agent/../../secret.txt')}",
    )
    assert status == 403
