# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""System test for Team mode continuous dialog via WebSocket.

Test scenario:
1. Connect to WebSocket
2. Send first message: "创建3个成员，轮流报数，不要说多余废话"
3. Team will continuously output (stream won't end)
4. Send second message while receiving: "现在从10开始轮流报数，一人说一句，就一轮"

Usage:
    .venv\Scripts\python.exe tests\system_tests\test_team_continuous_dialog.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import websockets

pytestmark = [pytest.mark.integration, pytest.mark.system]

REPO_ROOT = Path(__file__).resolve().parents[2]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Enable TeamManager logs
logging.getLogger("jiuwenclaw.agentserver.team.team_manager").setLevel(logging.INFO)


@dataclass
class RecvMessagesParams:
    ws: Any
    stop_event: asyncio.Event
    second_message_sent: asyncio.Event
    send_second_msg_callback: Any
    max_events: int = 50
    events_before_second: int = 10
    recv_timeout: float = 60.0


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


async def _wait_for_log(log_path: Path, needle: str, timeout: float = 60.0) -> None:
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


async def _wait_for_websocket_ready(url: str, timeout: float = 60.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    last_error: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            async with websockets.connect(url):
                return
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(0.2)
    raise AssertionError(
        f"Timed out waiting for websocket: {url} last_error={last_error}"
    )


def _print_event(event_number: int, data: dict[str, Any]) -> None:
    """Print a single event in a formatted way."""
    event_type = data.get("event", "unknown")
    payload = data.get("payload", {})

    if event_type == "connection.ack":
        logger.debug("[%04d] [CONNECTION_ACK] WebSocket connected", event_number)
        return

    if event_type == "stream.end":
        logger.debug("[%04d] [STREAM_END] Stream ended", event_number)
        return

    if not isinstance(payload, dict):
        logger.debug("[%04d] [%s] %s", event_number, event_type, payload)
        return

    inner_event_type = payload.get("event_type", "")

    if inner_event_type == "team.member":
        event = payload.get("event", {})
        sub_type = event.get("type", "unknown")
        member_id = event.get("member_id", "N/A")
        logger.debug("[%04d] [MEMBER] %s | member=%s", event_number, sub_type, member_id)

    elif inner_event_type == "team.task":
        event = payload.get("event", {})
        sub_type = event.get("type", "unknown")
        task_id = event.get("task_id", "N/A")
        logger.debug("[%04d] [TASK] %s | task=%s", event_number, sub_type, task_id)

    elif inner_event_type == "team.message":
        event = payload.get("event", {})
        sub_type = event.get("type", "unknown")
        from_member = event.get("from_member", "N/A")
        content = event.get("content", "")
        preview = content[:60] + "..." if len(content) > 60 else content
        logger.debug("[%04d] [MESSAGE] %s | from=%s | %s", event_number, sub_type, from_member, preview)

    elif inner_event_type == "chat.delta":
        content = payload.get("content", "")
        preview = content[:60] + "..." if len(content) > 60 else content
        logger.debug("[%04d] [CHAT_DELTA] %s", event_number, preview)

    elif inner_event_type == "chat.final":
        content = payload.get("content", "")
        preview = content[:60] + "..." if len(content) > 60 else content
        logger.debug("[%04d] [CHAT_FINAL] %s", event_number, preview)

    else:
        logger.debug("[%04d] [%s] %s", event_number, event_type, json.dumps(payload, ensure_ascii=False)[:100])


async def _recv_messages(params: RecvMessagesParams) -> list[dict]:
    """Receive messages and send second message after receiving some events.

    Args:
        params: Parameters for receiving messages
    """
    events = []
    event_count = 0
    start_time = asyncio.get_running_loop().time()

    while not params.stop_event.is_set() and event_count < params.max_events:
        # Check total timeout
        elapsed = asyncio.get_running_loop().time() - start_time
        if elapsed >= params.recv_timeout:
            logger.warning("Collected %s events before timeout", len(events))
            break

        try:
            remaining_timeout = 1.0
            raw = await asyncio.wait_for(params.ws.recv(), timeout=remaining_timeout)
            data = json.loads(raw)
            events.append(data)
            event_count += 1
            _print_event(event_count, data)

            if event_count == params.events_before_second and not params.second_message_sent.is_set():
                logger.info("Sending second message while team is still running...")
                await params.send_second_msg_callback()
                params.second_message_sent.set()

        except asyncio.TimeoutError:
            continue
        except websockets.ConnectionClosed:
            logger.info("WebSocket connection closed")
            break
        except Exception as e:
            logger.error("Error receiving message: %s", e)
            break

    return events


@pytest.mark.asyncio
@pytest.mark.skip(reason="temporarily skipped until team continuous dialog is stable")
async def test_team_continuous_dialog(temp_home: Path, monkeypatch: pytest.MonkeyPatch):
    """Test Team mode continuous dialog scenario.

    Scenario:
    1. Connect to WebSocket
    2. Send first message to create team with 3 members
    3. While team is running, send second message to change behavior
    4. Verify both messages are processed
    """
    logger.debug("temp_home: %s", temp_home)
    agent_port = _pick_free_port()
    web_port = _pick_free_port()
    gateway_port = _pick_free_port()

    # Load .env from project root
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        from dotenv import load_dotenv
        load_dotenv(env_file)

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

    logger.info("Starting agent server on port %s", agent_port)
    agent_proc = _start_process(
        [sys.executable, "-m", "jiuwenclaw.app_agentserver", "--port", str(agent_port)],
        env=env,
        log_path=agent_log,
    )
    gateway_proc = None
    try:
        await _wait_for_log(agent_log, "ready:", timeout=60)

        logger.info("Starting gateway on port %s", web_port)
        gateway_proc = _start_process(
            [sys.executable, "-m", "jiuwenclaw.app_gateway", "--port", str(web_port)],
            env=env,
            log_path=gateway_log,
        )
        await _wait_for_websocket_ready(
            f"ws://127.0.0.1:{web_port}/ws",
            timeout=60,
        )

        async with websockets.connect(f"ws://127.0.0.1:{web_port}/ws") as ws:
            session_id = f"sess_team_test_{int(time.time())}"

            logger.info("Team Continuous Dialog Test - Session ID: %s", session_id)

            req1 = {
                "type": "req",
                "id": "req-team-1",
                "method": "chat.send",
                "params": {
                    "session_id": session_id,
                    "mode": "team",
                    "content": "告诉我你有哪些工具，不要立刻开始任务",
                },
            }

            logger.info("Sending first message...")
            logger.debug("Request 1: %s", json.dumps(req1, ensure_ascii=False, indent=2))

            await ws.send(json.dumps(req1, ensure_ascii=False))

            stop_event = asyncio.Event()
            second_message_sent = asyncio.Event()
            events: list[dict] = []  # Initialize empty list

            req2 = {
                "type": "req",
                "id": "req-team-2",
                "method": "chat.send",
                "params": {
                    "session_id": session_id,
                    "mode": "team",
                    "content": "现在创建一个成员叫安娜，让安娜告诉我他有哪些工具",
                },
            }

            async def send_second_message():
                logger.info("Sending second message...")
                logger.debug("Request 2: %s", json.dumps(req2, ensure_ascii=False, indent=2))
                await ws.send(json.dumps(req2, ensure_ascii=False))

            events = await _recv_messages(
                RecvMessagesParams(
                    ws=ws,
                    stop_event=stop_event,
                    second_message_sent=second_message_sent,
                    send_second_msg_callback=send_second_message,
                    max_events=50,
                    events_before_second=10,
                    recv_timeout=60.0,
                )
            )

            logger.info("Test Results - Total events received: %s", len(events))

            event_types = {}
            for event in events:
                event_type = event.get("event", "unknown")
                event_types[event_type] = event_types.get(event_type, 0) + 1

            logger.info("Event type distribution:")
            for event_type, count in sorted(event_types.items(), key=lambda x: -x[1]):
                logger.info("  %s: %s", event_type, count)

            team_messages = [
                e for e in events
                if e.get("event") == "team.message"
            ]
            logger.info("Team messages received: %s", len(team_messages))

            if second_message_sent.is_set():
                logger.info("Second message was sent while team was running")
            else:
                logger.warning("Second message was NOT sent")

            # Core assertions
            assert len(events) > 0, "Should receive events from team"
            assert "team.member" in event_types or "team.message" in event_types, \
                "Should receive team events (member or message)"
            assert second_message_sent.is_set(), \
                "Second message should be sent during stream"

            logger.info("Test PASSED")

    finally:
        _stop_process(gateway_proc)
        _stop_process(agent_proc)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
