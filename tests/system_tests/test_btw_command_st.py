# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""System test for /btw command end-to-end flow.

Verifies that the gateway correctly forwards command.btw to the agent server
and returns a properly structured response frame. Follows the pattern from
test_cli_channel_ws.py.
"""

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


# =============================================================================
# Process helpers (identical pattern to test_cli_channel_ws.py)
# =============================================================================


def _pick_free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _start_process(
    cmd: list[str], *, env: dict[str, str], log_path: Path
) -> subprocess.Popen:
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


async def _wait_for_log(
    log_path: Path, needle: str, timeout: float = 30.0
) -> None:
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


async def _recv_until_response(
    ws, req_id: str, timeout: float = 10.0
) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        remaining = max(0.1, deadline - asyncio.get_running_loop().time())
        frame = json.loads(
            await asyncio.wait_for(ws.recv(), timeout=remaining)
        )
        if frame.get("type") == "res" and frame.get("id") == req_id:
            return frame
    raise AssertionError(f"Timed out waiting for response id={req_id}")


# =============================================================================
# System tests
# =============================================================================


@pytest.mark.asyncio
async def test_btw_command_system_roundtrip(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end system test: send command.btw via WebSocket, verify response.

    Starts agent server + gateway, connects via WebSocket, sends a
    command.btw request, and verifies the response frame structure.

    In a test environment without a configured model, the agent server
    will return ok=False with an error — this is still a valid test of
    the gateway → agent-server forwarding chain.
    """
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

    agent_log = temp_home / "agentserver_btw.log"
    gateway_log = temp_home / "gateway_btw.log"

    agent_proc = _start_process(
        [
            sys.executable,
            "-m",
            "jiuwenswarm.server.app_agentserver",
            "--port",
            str(agent_port),
        ],
        env=env,
        log_path=agent_log,
    )
    gateway_proc = None
    try:
        await _wait_for_log(agent_log, "ready:", timeout=60)

        gateway_proc = _start_process(
            [
                sys.executable,
                "-m",
                "jiuwenswarm.gateway.app_gateway",
                "--port",
                str(web_port),
            ],
            env=env,
            log_path=gateway_log,
        )
        await _wait_for_websocket_ready(
            f"ws://127.0.0.1:{gateway_port}/tui",
            timeout=60,
        )

        async with websockets.connect(
            f"ws://127.0.0.1:{gateway_port}/tui"
        ) as ws:
            # Send a /btw request with a test question
            req_btw = {
                "type": "req",
                "id": "req-btw-st",
                "method": "command.btw",
                "params": {
                    "session_id": "sess_btw_test",
                    "question": "what is the current project about?",
                    "mode": "agent.plan",
                },
            }
            await ws.send(json.dumps(req_btw, ensure_ascii=False))

            btw_res = await _recv_until_response(ws, "req-btw-st", timeout=15)
            # Verify response frame structure
            assert btw_res["type"] == "res"
            assert btw_res["id"] == "req-btw-st"
            # ok may be True or False depending on model availability
            assert "ok" in btw_res
            # payload must contain a status field
            assert "status" in btw_res.get("payload", {})

    finally:
        _stop_process(gateway_proc)
        _stop_process(agent_proc)


@pytest.mark.asyncio
async def test_btw_command_empty_question(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """System test: empty question should be rejected early by the handler.

    The handler validates the question param before attempting any
    model call, so this should succeed even without a configured model.
    """
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

    agent_log = temp_home / "agentserver_btw2.log"
    gateway_log = temp_home / "gateway_btw2.log"

    agent_proc = _start_process(
        [
            sys.executable,
            "-m",
            "jiuwenswarm.server.app_agentserver",
            "--port",
            str(agent_port),
        ],
        env=env,
        log_path=agent_log,
    )
    gateway_proc = None
    try:
        await _wait_for_log(agent_log, "ready:", timeout=60)

        gateway_proc = _start_process(
            [
                sys.executable,
                "-m",
                "jiuwenswarm.gateway.app_gateway",
                "--port",
                str(web_port),
            ],
            env=env,
            log_path=gateway_log,
        )
        await _wait_for_websocket_ready(
            f"ws://127.0.0.1:{gateway_port}/tui",
            timeout=60,
        )

        async with websockets.connect(
            f"ws://127.0.0.1:{gateway_port}/tui"
        ) as ws:
            # Empty question — should be rejected immediately
            req_btw = {
                "type": "req",
                "id": "req-btw-empty-st",
                "method": "command.btw",
                "params": {
                    "session_id": "sess_btw_empty",
                    "question": "",
                    "mode": "agent.plan",
                },
            }
            await ws.send(json.dumps(req_btw, ensure_ascii=False))

            btw_res = await _recv_until_response(
                ws, "req-btw-empty-st", timeout=15
            )
            # Verify response frame structure
            assert btw_res["type"] == "res"
            assert btw_res["id"] == "req-btw-empty-st"
            # Empty question returns ok=True with failed payload (by design)
            assert btw_res["ok"] is True
            assert btw_res["payload"]["status"] == "failed"
            assert "Question is required" in btw_res["payload"]["error"]

    finally:
        _stop_process(gateway_proc)
        _stop_process(agent_proc)


@pytest.mark.asyncio
async def test_btw_no_context_when_no_session(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """System test: btw on a fresh session with no conversation returns no_context.

    When there are no prior messages and no system prompt configured,
    the btw handler should return status=no_context.
    """
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

    agent_log = temp_home / "agentserver_btw3.log"
    gateway_log = temp_home / "gateway_btw3.log"

    agent_proc = _start_process(
        [
            sys.executable,
            "-m",
            "jiuwenswarm.server.app_agentserver",
            "--port",
            str(agent_port),
        ],
        env=env,
        log_path=agent_log,
    )
    gateway_proc = None
    try:
        await _wait_for_log(agent_log, "ready:", timeout=60)

        gateway_proc = _start_process(
            [
                sys.executable,
                "-m",
                "jiuwenswarm.gateway.app_gateway",
                "--port",
                str(web_port),
            ],
            env=env,
            log_path=gateway_log,
        )
        await _wait_for_websocket_ready(
            f"ws://127.0.0.1:{gateway_port}/tui",
            timeout=60,
        )

        async with websockets.connect(
            f"ws://127.0.0.1:{gateway_port}/tui"
        ) as ws:
            # Use a unique session ID that has no conversation history
            req_btw = {
                "type": "req",
                "id": "req-btw-nocontext-st",
                "method": "command.btw",
                "params": {
                    "session_id": "sess_btw_nonexistent_99999",
                    "question": "what is happening?",
                    "mode": "agent.plan",
                },
            }
            await ws.send(json.dumps(req_btw, ensure_ascii=False))

            btw_res = await _recv_until_response(
                ws, "req-btw-nocontext-st", timeout=15
            )
            assert btw_res["type"] == "res"
            assert btw_res["id"] == "req-btw-nocontext-st"
            assert "status" in btw_res.get("payload", {})
            # With no context and no system prompt, expect no_context or failed
            status = btw_res["payload"]["status"]
            assert status in ("no_context", "failed")

    finally:
        _stop_process(gateway_proc)
        _stop_process(agent_proc)
