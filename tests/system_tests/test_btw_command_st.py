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
BTW_MODEL_RESPONSE_TIMEOUT = 120.0
# Gateway reserves five seconds to return a timeout response before the
# client's own deadline. Keep the system-test wait aligned with that contract.
_BTW_CLIENT_TIMEOUT_MS = 30_000
_BTW_RESPONSE_TIMEOUT_SECONDS = 30.0
_INHERITED_CONFIG_ENV_KEYS = (
    "AGENT_SERVER_URL",
    "API_BASE",
    "API_KEY",
    "JIUWENSWARM_CONFIG_DIR",
    "JIUWENSWARM_DATA_DIR",
    "MODEL_ALIAS",
    "MODEL_NAME",
    "MODEL_PROVIDER",
)


# =============================================================================
# Process helpers (shared from tests/system_tests/proc_harness.py)
# =============================================================================


def _isolated_service_env(temp_home: Path) -> dict[str, str]:
    """Build a subprocess environment without inherited user/model config."""
    env = os.environ.copy()
    for key in _INHERITED_CONFIG_ENV_KEYS:
        env.pop(key, None)
    env["HOME"] = str(temp_home)
    env["JIUWENSWARM_HOME"] = str(temp_home)
    # Preserve the per-test ports when service startup loads runtime dotenv.
    env["JIUWENSWARM_CLI_PORTS"] = "1"
    return env


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

    env = _isolated_service_env(temp_home)
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
                "agent_gateway.app_gateway",
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
                "timeout_ms": _BTW_CLIENT_TIMEOUT_MS,
                "params": {
                    "session_id": "sess_btw_test",
                    "question": "what is the current project about?",
                    "mode": "agent.plan",
                },
            }
            await ws.send(json.dumps(req_btw, ensure_ascii=False))

            btw_res = await _recv_until_response(
                ws,
                "req-btw-st",
                timeout=BTW_MODEL_RESPONSE_TIMEOUT,
            )
            # Verify response frame structure
            assert btw_res["type"] == "res"
            assert btw_res["id"] == "req-btw-st"
            # ok may be True or False depending on model availability.
            assert "ok" in btw_res
            payload = btw_res.get("payload", {})
            assert isinstance(payload, dict)
            assert "status" in payload

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

    env = _isolated_service_env(temp_home)
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
                "agent_gateway.app_gateway",
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
                "timeout_ms": _BTW_CLIENT_TIMEOUT_MS,
                "params": {
                    "session_id": "sess_btw_empty",
                    "question": "",
                    "mode": "agent.plan",
                },
            }
            await ws.send(json.dumps(req_btw, ensure_ascii=False))

            btw_res = await _recv_until_response(
                ws,
                "req-btw-empty-st",
                timeout=_BTW_RESPONSE_TIMEOUT_SECONDS,
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
    """System test: btw on a session with no conversation history.

    ``no_context`` is only returned when both recent messages and the agent
    system prompt are empty. A freshly created session adapter usually still
    has a system prompt (project context / prompt builder), so the handler
    proceeds to the model path and returns ``ok`` or ``failed``.
    """
    agent_port = _pick_free_port()
    web_port = _pick_free_port()
    gateway_port = _pick_free_port()

    env = _isolated_service_env(temp_home)
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
                "agent_gateway.app_gateway",
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
                "timeout_ms": _BTW_CLIENT_TIMEOUT_MS,
                "params": {
                    "session_id": "sess_btw_nonexistent_99999",
                    "question": "what is happening?",
                    "mode": "agent.plan",
                },
            }
            await ws.send(json.dumps(req_btw, ensure_ascii=False))

            btw_res = await _recv_until_response(
                ws,
                "req-btw-nocontext-st",
                timeout=BTW_MODEL_RESPONSE_TIMEOUT,
            )
            assert btw_res["type"] == "res"
            assert btw_res["id"] == "req-btw-nocontext-st"
            payload = btw_res.get("payload", {})
            assert isinstance(payload, dict)
            assert "status" in payload
            # Empty history alone is not enough for no_context once the agent
            # has a system prompt; accept model path outcomes too.
            status = payload["status"]
            assert status in ("ok", "no_context", "failed")
            if status == "ok":
                assert isinstance(payload.get("answer"), str)
                assert payload["answer"].strip()
            elif status == "failed":
                assert "error" in payload

    finally:
        _stop_process(gateway_proc)
        _stop_process(agent_proc)
