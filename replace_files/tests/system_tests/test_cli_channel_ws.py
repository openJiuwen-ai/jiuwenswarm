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

pytestmark = [pytest.mark.integration, pytest.mark.system]

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pick_free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _start_process(cmd: list[str], *, env: dict[str, str], log_path: Path) -> subprocess.Popen:
    log_file = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    log_file.close()
    return proc


def _stop_process(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    if proc.poll() is not None:
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        return

    proc.terminate()
    try:
        proc.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


async def _wait_for_log(log_path: Path, needle: str, timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="ignore")
            if needle in text:
                return
        await asyncio.sleep(0.2)
    log_text = (
        log_path.read_text(encoding="utf-8", errors="ignore")
        if log_path.exists()
        else ""
    )
    raise AssertionError(
        f"Timed out waiting for log line: {needle}\nlog={log_text}"
    )


async def _wait_for_websocket_ready(url: str, timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    last_error: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            async with websockets.connect(url):
                return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            await asyncio.sleep(0.2)
    raise AssertionError(
        f"Timed out waiting for websocket: {url} last_error={last_error}"
    )


async def _recv_until_response(ws, req_id: str, timeout: float = 10.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        remaining = max(0.1, deadline - asyncio.get_running_loop().time())
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
        if frame.get("type") == "res" and frame.get("id") == req_id:
            return frame
    raise AssertionError(f"Timed out waiting for response id={req_id}")


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
        [sys.executable, "-m", "jiuwenclaw.app_agentserver", "--port", str(agent_port)],
        env=env,
        log_path=agent_log,
    )
    gateway_proc = None
    try:
        await _wait_for_log(agent_log, "ready:", timeout=60)

        gateway_proc = _start_process(
            [sys.executable, "-m", "jiuwenclaw.app_gateway", "--port", str(web_port)],
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
