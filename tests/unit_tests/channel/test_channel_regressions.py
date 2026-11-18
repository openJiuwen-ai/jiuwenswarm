# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consolidated regression tests (merged from 3 small test modules)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
import pytest
import json
from agent_gateway.channel_manager.web.trajectory_http import TrajectoryHttpService as _GatewayTrajectoryHttpService
from jiuwenswarm.observability.config import TrajectoryStoreSettings
from jiuwenswarm.server.trajectory.host import TrajectoryHost
import asyncio
from jiuwenswarm.common.schema.message import Message
from agent_gateway.channel_manager.base import RobotMessageRouter
from agent_gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
from agent_gateway.routing.keys import RoutingKey


# --- from test_input_change_on_blur.py ---

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]  # noqa: PTH200

_FRONTEND_ROOT = _REPOSITORY_ROOT / "jiuwenswarm" / "channels" / "web" / "frontend"

_CACHE_DIR = _FRONTEND_ROOT / "node_modules" / ".cache" / "input-change-on-blur"

_TEST_FILE = _FRONTEND_ROOT / "tests" / "inputChangeOnBlur.test.mjs"

def _require_tool(name: str) -> str:
    """Return the tool path, skipping the test when the tool is not installed."""
    executable = shutil.which(name)
    if executable is None:
        pytest.skip(f"Required tool is unavailable: {name}")
    return executable

def test_input_change_on_blur_clamps_to_min_max_range() -> None:
    node = _require_tool("node")
    npx = _require_tool("npx")

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    compile_result = subprocess.run(
        [
            npx,
            "esbuild",
            "src/components/ui/Input/Input.tsx",
            "--bundle",
            "--packages=external",
            "--platform=node",
            "--format=esm",
            f"--outfile={_CACHE_DIR / 'Input.mjs'}",
            "--loader:.css=empty",
        ],
        cwd=_FRONTEND_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert compile_result.returncode == 0, (
        f"esbuild failed:\nstdout: {compile_result.stdout}\nstderr: {compile_result.stderr}"
    )

    test_result = subprocess.run(
        [node, "--test", str(_TEST_FILE.relative_to(_FRONTEND_ROOT))],
        cwd=_FRONTEND_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert test_result.returncode == 0, (
        f"node --test failed:\n{test_result.stdout}\n{test_result.stderr}"
    )

# --- from test_trajectory_usage_http.py ---

async def _usage_metadata_loader(
    _session_id: str,
    _user_id: str | None = None,
) -> dict[str, str]:
    return {
        "mode": "agent.work.normal",
        "team_name": "",
    }

def TrajectoryHttpService(settings=None, *, reader=None, metadata_loader=None, fetch=None):
    """Exercise the gateway usage route through the in-process trajectory host."""
    if fetch is None:
        fetch = TrajectoryHost(settings=settings, reader=reader).call
    return _GatewayTrajectoryHttpService(metadata_loader=metadata_loader, fetch=fetch)

class _UsageReader:
    async def get_session_request_usage(self, session_id: str):
        return ([{
            "trace_id": "1" * 32,
            "inference_id": "inference-1",
            "subject_id": "main",
            "start_time_unix_nano": "10",
            "usage": {"input": 2, "total": 3},
            "cumulative_usage": {"input": 5, "total": 8},
        }], "epoch-1")

@pytest.mark.asyncio
async def test_http_exposes_session_scoped_request_usage(tmp_path: Path) -> None:
    settings = TrajectoryStoreSettings(
        enabled=True,
        database_path=tmp_path,
        retention_days=7,
        queue_size=16,
        batch_size=8,
        flush_interval_ms=20,
    )
    service = TrajectoryHttpService(
        settings,
        reader=_UsageReader(),
        metadata_loader=_usage_metadata_loader,
    )

    response = await service.get_session_usage("session-1")
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["scope"] == "session"
    assert payload["items"][0]["cumulative_usage"] == {"input": 5, "total": 8}

# --- from test_web_channel_jiuwenmate_stream.py ---

STREAM = {
    "event_type": "jiuwenmate.chat.stream",
    "kind": "delta",
    "agent_id": "mate_dev",
    "request_id": "req_1",
    "execution_id": "exe_1",
    "seq": 3,
    "text": "**进展**",
}

class _FakeClient:
    def __init__(self):
        self.frames = []
        self.remote_address = ("127.0.0.1", 12345)

    async def send(self, data):
        self.frames.append(json.loads(data))

@pytest.mark.asyncio
async def test_stream_push_without_session_reaches_only_its_connection():
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    mine, other = _FakeClient(), _FakeClient()
    for client in (mine, other):
        key = RoutingKey(channel_id="web", app_id="default", user_id="local", session_id="s-1", agent_ref=None)
        await channel.register_ws(client, key)
    try:
        msg = Message(
            id="exe_1:3",
            type="event",
            channel_id="web",
            session_id=None,
            params={},
            timestamp=1.0,
            ok=True,
            payload=dict(STREAM),
            metadata={"ws_id": getattr(mine, "_jiuwen_ws_id", "")},
        )

        await channel.send(msg)
        for _ in range(20):
            if mine.frames:
                break
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.01)

        assert mine.frames == [{"type": "event", "event": "jiuwenmate.chat.stream", "payload": STREAM}]
        assert other.frames == []
    finally:
        await channel.unregister_ws(mine)
        await channel.unregister_ws(other)

