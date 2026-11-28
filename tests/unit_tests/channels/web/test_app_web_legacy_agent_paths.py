# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JW-87: Web 文件 API 不依赖 profile 迁移兼容链也能服务 ``agent/...`` 请求。"""

from __future__ import annotations

import shutil
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest

from jiuwenswarm.channels.web.app_web import (
    _SpaStaticHandler,
    _canonicalize_legacy_path,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("agent/sessions/sess_demo", "profile/agent/sessions/sess_demo"),
        ("agent\\workspace\\todo\\s1", "profile/agent/workspace/todo/s1"),
        ("agent", "profile/agent"),
        # agent/.logs 改写到日志收敛正位置 logs/agent（与 _effective_logs_dir
        # 的无链落点一致）；跟随实际落点的行为单独覆盖在
        # test_canonicalize_legacy_log_path_follows_effective_logs_dir。
        ("./agent/.logs/ws-dev.log", "logs/agent/ws-dev.log"),
        # 其余迁移过的老根目录同样改写
        ("workspace/report.md", "profile/report.md"),
        ("application_plugins/plugin.json", "profile/application_plugins/plugin.json"),
        (".agent_teams/t1/team.json", "profile/.agent_teams/t1/team.json"),
        (".agent_history/h1", "profile/.agent_history/h1"),
        ("config/runtime_state/task.json", "profile/runtime_state/task.json"),
        ("logs/logs/core.log", "logs/core/core.log"),
        # 非老前缀、绝对路径、含 .. 的路径原样保留
        ("profile/agent/workspace", "profile/agent/workspace"),
        ("logs/agent/a.log", "logs/agent/a.log"),
        ("agent/../logs/agent/x.log", "agent/../logs/agent/x.log"),
        ("../agent/sessions", "../agent/sessions"),
        (r"D:\data\agent\sessions", r"D:\data\agent\sessions"),
        ("/var/log/agent", "/var/log/agent"),
    ],
)
def test_canonicalize_legacy_path(raw: str, expected: str) -> None:
    assert _canonicalize_legacy_path(raw) == expected


def test_canonicalize_legacy_log_path_follows_effective_logs_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """agent/.logs 改写跟随 _effective_logs_dir 的实际落点。

    兼容链建不出来、日志只留在 profile/agent/.logs 时，改写目标必须落到
    有数据的那一侧，否则文件 API 读不到日志（logs_root 同源，见
    test_legacy_agent_file_api_works_without_compat_link）。
    """
    # 数据根与进程默认（tests/conftest.py 的隔离 JIUWENSWARM_DATA_DIR）无关
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))

    # 链建不出来：数据只在 profile/agent/.logs，logs/agent 为空 → 用 .logs
    # 链建不出来：数据只在 profile/agent/.logs，logs/agent 为空 → 用 .logs
    dot_logs = tmp_path / "profile" / "agent" / ".logs"
    dot_logs.mkdir(parents=True)
    (dot_logs / "ws-dev.log").write_text("log", encoding="utf-8")
    assert (
        _canonicalize_legacy_path("agent/.logs/ws-dev.log", tmp_path)
        == "profile/agent/.logs/ws-dev.log"
    )

    # 数据只在 logs/agent → 用新桶
    shutil.rmtree(dot_logs)
    bucket = tmp_path / "logs" / "agent"
    bucket.mkdir(parents=True)
    (bucket / "ws-dev.log").write_text("log", encoding="utf-8")
    assert (
        _canonicalize_legacy_path("agent/.logs/ws-dev.log", tmp_path)
        == "logs/agent/ws-dev.log"
    )


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
    # 日志兼容链也建不出来时，setup_logger 直接写 profile/agent/.logs；
    # 日志吸收迁移的正位置是 logs/agent——两处都可能留有数据
    (agent_root / ".logs").mkdir(parents=True)
    (agent_root / ".logs" / "ws-dev.log").write_text("ws dev log\n", encoding="utf-8")
    (tmp_path / "logs" / "agent").mkdir(parents=True)
    (tmp_path / "logs" / "agent" / "ws-dev.log").write_text("ws dev log\n", encoding="utf-8")
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


def test_real_legacy_agent_dir_is_not_silently_rewritten(
    _web_server: int, tmp_path: Path
) -> None:
    """迁移未完成/新旧版本并存：<root>/agent 是真实目录时保持 develop 原行为。

    此时 profile/agent 未必有完整数据，改写过去会静默读到另一棵树；兼容链
    也不会遮蔽真实目录，因此按原路径解析（不在白名单根下 → 403，与 develop
    一致），而不是悄悄给出 profile 里的内容。
    """
    legacy_file = tmp_path / "agent" / "workspace" / "legacy.txt"
    legacy_file.parent.mkdir(parents=True)
    legacy_file.write_text("from legacy tree", encoding="utf-8")

    status, body = _get(
        _web_server,
        f"/file-api/list-files?dir={quote('agent/workspace')}",
    )
    assert status == 403
    assert "legacy.txt" not in body

    # 老根是真实目录时整棵老树都不改写：profile 树中的内容不能借老路径泄漏出来
    status, body = _get(
        _web_server,
        f"/file-api/file-content?path={quote('agent/workspace/agent-data.json')}",
    )
    assert status == 403
    assert '{"agents": []}' not in body


def test_dotdot_path_is_not_rewritten(_web_server: int) -> None:
    # 含 .. 的老前缀路径按原样解析：不能改写进另一棵树造成落点变化
    status, _ = _get(
        _web_server,
        f"/file-api/file-content?path={quote('agent/../logs/agent/no.log')}",
    )
    assert status == 404
