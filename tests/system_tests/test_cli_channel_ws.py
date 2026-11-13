# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""System test for CLI route on GatewayServer."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import websockets

from tests.system_tests.proc_harness import (
    _pick_free_port,
    _recv_until_response,
    _start_process,
    _stop_process,
    _wait_for_log,
    _wait_for_websocket_ready,
)

pytestmark = [pytest.mark.integration, pytest.mark.system]

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_cli_route_system_roundtrip(temp_home: Path, monkeypatch: pytest.MonkeyPatch):
    agent_port = _pick_free_port()
    web_port = _pick_free_port()
    gateway_port = _pick_free_port()

    env = os.environ.copy()
    env["HOME"] = str(temp_home)
    env["AGENT_SERVER_HOST"] = "127.0.0.1"
    env["AGENT_SERVER_PORT"] = str(agent_port)
    env["WEB_HOST"] = "127.0.0.1"
    env["WEB_PORT"] = str(web_port)
    env["GATEWAY_HOST"] = "127.0.0.1"
    env["GATEWAY_PORT"] = str(gateway_port)

    agent_log = temp_home / "agentserver.log"
    gateway_log = temp_home / "gateway.log"

    agent_proc = _start_process(
        [sys.executable, "-m", "jiuwenswarm.server.app_agentserver", "--port", str(agent_port)],
        env=env,
        log_path=agent_log,
    )
    gateway_proc = None
    try:
        await _wait_for_log(agent_log, "ready:", timeout=60)

        gateway_proc = _start_process(
            [sys.executable, "-m", "agent_gateway.app_gateway", "--port", str(web_port)],
            env=env,
            log_path=gateway_log,
        )
        await _wait_for_websocket_ready(
            f"ws://127.0.0.1:{gateway_port}/tui",
            timeout=60,
        )

        async with websockets.connect(f"ws://127.0.0.1:{gateway_port}/tui") as ws:
            req_chat = {
                "type": "req",
                "id": "req-chat",
                "method": "chat.send",
                "params": {
                    "session_id": "sess_test",
                    "content": "hello cli",
                    "mode": "agent.plan",
                },
            }
            await ws.send(json.dumps(req_chat, ensure_ascii=False))
            chat_res = await _recv_until_response(ws, "req-chat", timeout=10)
            assert chat_res["type"] == "res"
            assert chat_res["id"] == "req-chat"
            assert chat_res["ok"] is True
            assert chat_res["payload"]["accepted"] is True
            assert chat_res["payload"]["session_id"] == "sess_test"
    finally:
        _stop_process(gateway_proc)
        _stop_process(agent_proc)
