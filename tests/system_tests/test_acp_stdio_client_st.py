# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""System tests for jiuwenswarm.acp (stdio client + CLI smoke entry).

Writes an inline fake ACP agent script to *tmp_path* per test (no Codex CLI / API keys).
Modes: echo | error | permission | fs_read
"""

from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent

import pytest

from jiuwenswarm.acp.cli import _get_spec, _run_once
from jiuwenswarm.acp.stdio_client import AcpStdioClient

pytestmark = pytest.mark.system

_FAKE_AGENT_SOURCE = dedent(
    '''
    import json
    import sys

    _SESSION_ID = "st-fake-session"
    _MODE = (sys.argv[1] if len(sys.argv) > 1 else "echo").strip().lower()
    _FS_TARGET = "st_file.txt"


    def _read_one():
        buf = ""
        dec = json.JSONDecoder()
        while True:
            chunk = sys.stdin.read(1)
            if not chunk:
                return None
            buf += chunk
            stripped = buf.lstrip()
            if not stripped:
                continue
            try:
                obj, idx = dec.raw_decode(stripped)
                consumed = len(buf) - len(stripped) + idx
                buf = buf[consumed:]
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                if len(buf) > 1024 * 1024:
                    return None


    def _write(obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\\n")
        sys.stdout.flush()


    def _wait_response(expected_id):
        while True:
            msg = _read_one()
            if msg is None:
                return None
            if str(msg.get("id")) == str(expected_id):
                return msg


    def _handle_prompt(req_id, text):
        if _MODE == "error":
            _write({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": "agent failed"}})
            return

        if _MODE == "permission":
            perm_id = "perm-st-1"
            _write({
                "jsonrpc": "2.0",
                "id": perm_id,
                "method": "session/request_permission",
                "params": {
                    "sessionId": _SESSION_ID,
                    "toolCall": {"toolCallId": "tc-1", "title": "run test"},
                    "options": [{"optionId": "allow-once", "kind": "allow"}],
                },
            })
            reply = _wait_response(perm_id)
            if reply is None or "error" in reply:
                _write({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": "permission handshake failed"},
                })
                return

        body_text = text
        if _MODE == "fs_read":
            fs_id = "fs-st-1"
            _write({
                "jsonrpc": "2.0",
                "id": fs_id,
                "method": "fs/read_text_file",
                "params": {"path": _FS_TARGET},
            })
            reply = _wait_response(fs_id)
            if reply is None or "error" in reply:
                _write({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": "fs read handshake failed"},
                })
                return
            result = reply.get("result")
            body_text = str(result.get("content") or "") if isinstance(result, dict) else ""

        if _MODE == "echo":
            body_text = f"echo:{text}"
        elif _MODE == "permission":
            body_text = f"after-perm:{text}"

        _write({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": _SESSION_ID,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": body_text},
                },
            },
        })
        _write({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})


    while True:
        msg = _read_one()
        if msg is None:
            break
        req_id = msg.get("id")
        method = str(msg.get("method") or "")

        if method == "initialize":
            _write({"jsonrpc": "2.0", "id": req_id, "result": {"protocolVersion": 1}})
        elif method == "session/new":
            _write({"jsonrpc": "2.0", "id": req_id, "result": {"sessionId": _SESSION_ID}})
        elif method == "session/prompt":
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            prompt = params.get("prompt")
            text = ""
            if isinstance(prompt, list) and prompt:
                first = prompt[0]
                if isinstance(first, dict):
                    text = str(first.get("text") or "")
            _handle_prompt(req_id, text)
        elif req_id is not None:
            _write({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            })
    '''
).strip()


@pytest.fixture
def fake_agent_script(tmp_path: Path) -> Path:
    script = tmp_path / "fake_acp_agent.py"
    script.write_text(_FAKE_AGENT_SOURCE, encoding="utf-8")
    return script


def _profile(script: Path, *modes: str) -> dict:
    return {"command": sys.executable, "args": [str(script), *modes]}


@pytest.fixture
def fake_acp_profile(fake_agent_script: Path) -> dict:
    return _profile(fake_agent_script, "echo")


@pytest.mark.asyncio
async def test_stdio_client_connect_and_chat_with_fake_agent(fake_acp_profile: dict) -> None:
    client = AcpStdioClient(fake_acp_profile["command"], fake_acp_profile["args"])
    try:
        await client.connect()
        assert client.session_id == "st-fake-session"
        out = await client.chat("hello st")
        assert out == "echo:hello st"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stdio_client_chat_raises_on_jsonrpc_error(fake_agent_script: Path) -> None:
    prof = _profile(fake_agent_script, "error")
    client = AcpStdioClient(prof["command"], prof["args"])
    try:
        await client.connect()
        with pytest.raises(RuntimeError, match=r"ACP JSON-RPC error:.*agent failed"):
            await client.chat("trigger error")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stdio_client_auto_approves_permission_and_completes_chat(
    fake_agent_script: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACP_AUTO_APPROVE_PERMISSIONS", "true")
    prof = _profile(fake_agent_script, "permission")
    client = AcpStdioClient(prof["command"], prof["args"])
    try:
        await client.connect()
        out = await client.chat("need perm")
        assert out == "after-perm:need perm"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stdio_client_fs_read_text_file_via_peer_request(
    fake_agent_script: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "st_file.txt"
    target.write_text("file-content-xyz\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    prof = _profile(fake_agent_script, "fs_read")
    client = AcpStdioClient(prof["command"], prof["args"], cwd=str(tmp_path))
    try:
        await client.connect()
        out = await client.chat("read file")
        assert out == "file-content-xyz"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cli_run_once_uses_config_profile(monkeypatch: pytest.MonkeyPatch, fake_acp_profile: dict) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.acp.cli.get_config",
        lambda: {"acp_agents": {"fake": fake_acp_profile}},
    )

    code = await _run_once("fake", "ping")

    assert code == 0


@pytest.mark.asyncio
async def test_cli_run_once_returns_1_on_chat_failure(
    monkeypatch: pytest.MonkeyPatch,
    fake_agent_script: Path,
) -> None:
    prof = _profile(fake_agent_script, "error")
    monkeypatch.setattr(
        "jiuwenswarm.acp.cli.get_config",
        lambda: {"acp_agents": {"fake": prof}},
    )

    assert await _run_once("fake", "trigger error") == 1


def test_get_spec_returns_none_when_profile_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.acp.cli.get_config",
        lambda: {"acp_agents": {}},
    )

    assert _get_spec("missing") is None
