# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Regression: raw-file served from Gateway Web HTTP (migrated from app_web)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web.web_http_app import create_web_http_app
from jiuwenswarm.gateway.channel_manager.web.file_http import FileHttpRoots
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig


def test_raw_file_serves_persisted_session_image(tmp_path: Path):
    agent_root = tmp_path / "agent"
    image_path = agent_root / "sessions" / "session-1" / "uploads" / "image.png"
    image_path.parent.mkdir(parents=True)
    image_bytes = b"\x89PNG\r\n\x1a\nimage-data"
    image_path.write_bytes(image_bytes)

    roots = FileHttpRoots(
        project_root=tmp_path,
        workspace_root=agent_root,
        agent_teams_root=tmp_path / "agent-teams",
        logs_root=tmp_path / "logs",
        auto_harness_root=tmp_path / "auto-harness",
    )
    for d in (roots.agent_teams_root, roots.logs_root, roots.auto_harness_root):
        d.mkdir(parents=True, exist_ok=True)

    channel = WebChannel(WebChannelConfig(host="127.0.0.1", port=0), RobotMessageRouter())
    app = create_web_http_app(channel)
    app.state.file_http_roots = roots
    client = TestClient(app)

    rel = str(image_path.relative_to(tmp_path)).replace("\\", "/")
    response = client.get("/file-api/raw-file", params={"path": rel})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.content == image_bytes
