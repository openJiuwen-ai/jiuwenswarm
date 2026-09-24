# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Interactive control commands use read-only Runtime queries and local state."""

from __future__ import annotations

import argparse
import asyncio
import json
from io import StringIO

import pytest

from jiuwenswarm.channels.process_cli import app, control_commands, repl
from jiuwenswarm.channels.process_cli.commands import parse_slash_command
from jiuwenswarm.channels.process_cli.ui import ProcessCliUI


def _args(**overrides) -> argparse.Namespace:
    values = {
        "mode": "agent.code.normal",
        "work_mode": "code",
        "session": None,
        "cwd": None,
        "project_dir": None,
        "trusted_dir": [],
        "timeout": None,
        "output": "human",
        "show_reasoning": False,
        "show_tools": False,
        "prompt": "hello",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _state(**overrides) -> repl._ReplState:
    values = {
        "session_id": "process_cli_current",
        "cwd": "D:/workspace",
        "model_name": "default-model",
        "display_mode": "agent.code",
    }
    values.update(overrides)
    return repl._ReplState(**values)


async def _command(value: str, args, state, ui) -> None:
    parsed = parse_slash_command(value)
    assert parsed is not None
    assert not await repl._handle_slash_command(args, parsed, ui, state)


@pytest.mark.asyncio
async def test_sessions_status_and_permissions_query_current_runtime_state(
    monkeypatch,
) -> None:
    calls = []

    async def fake_query(operation, *, cwd, params=None, timeout=30.0):
        calls.append((operation, cwd, params, timeout))
        return {
            "session.list": {
                "total": 1,
                "offset": 0,
                "sessions": [
                    {
                        "session_id": "process_cli_current",
                        "title": "demo",
                        "mode": "agent.code.normal",
                    }
                ],
            },
            "session.get": {
                "session": {
                    "session_id": "process_cli_current",
                    "mode": "agent.code.normal",
                    "model": "applied-model",
                    "message_count": 2,
                    "project_dir": "D:/project",
                }
            },
            "permission.get": {
                "scope": "session",
                "effective": {
                    "enabled": True,
                    "tools": [{"name": "shell", "level": "ask"}],
                    "rules": [],
                },
            },
        }[operation]

    monkeypatch.setattr(repl, "query_runtime", fake_query)
    output = StringIO()
    ui = ProcessCliUI(output, columns=100)
    state = _state()
    args = _args()

    await _command("/sessions", args, state, ui)
    await _command("/status", args, state, ui)
    await _command("/permissions", args, state, ui)

    assert [call[0] for call in calls] == [
        "session.list",
        "session.get",
        "permission.get",
    ]
    assert calls[0][2] == {"limit": 20, "offset": 0}
    assert calls[1][2] == {"session_id": "process_cli_current"}
    assert calls[2][2] == {"session_id": "process_cli_current"}
    text = output.getvalue()
    assert "* process_cli_current · demo" in text
    assert "会话模型：applied-model" in text
    assert "下一轮模型：default-model" in text
    assert "shell: ask" in text


@pytest.mark.asyncio
async def test_model_selection_flows_to_next_chat_request(monkeypatch) -> None:
    async def fake_query(operation, *, cwd, params=None, timeout=30.0):
        assert operation == "model.resolve"
        assert params == {"requested": "chosen"}
        return {"selection_key": "chosen#2", "display_name": "Chosen"}

    monkeypatch.setattr(repl, "query_runtime", fake_query)
    output = StringIO()
    ui = ProcessCliUI(output)
    state = _state()
    args = _args()

    await _command("/model chosen", args, state, ui)

    assert state.model_selection == "chosen#2"
    assert state.model_name == "Chosen"
    worker = repl._worker_command(
        args,
        prompt_file="prompt.txt",
        session_id=state.session_id,
        session_result_file="session.txt",
        operation="chat",
    )
    assert worker[worker.index("--_model-selection") + 1] == "chosen#2"
    request = app._build_request(args, session_id=state.session_id, request_id="r1")
    assert request.params["model_name"] == "chosen#2"


@pytest.mark.asyncio
async def test_plan_toggle_preserves_work_mode_and_rejects_team() -> None:
    output = StringIO()
    ui = ProcessCliUI(output)
    args = _args()
    state = _state()

    await _command("/plan on", args, state, ui)
    assert args.mode == "agent.code.plan"
    assert state.display_mode == "agent.code.plan"
    await _command("/plan off", args, state, ui)
    assert args.mode == "agent.code.normal"
    assert state.display_mode == "agent.code"

    args.mode = "team.code.normal"
    state.display_mode = "team.code"
    await _command("/plan", args, state, ui)
    assert args.mode == "team.code.normal"
    assert "仅支持单 Agent" in output.getvalue()


@pytest.mark.asyncio
async def test_query_worker_uses_public_read_only_protocol(monkeypatch) -> None:
    observed = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self, data):
            observed["request"] = json.loads(data)
            return (
                json.dumps(
                    {
                        "operation": "session.list",
                        "status": "completed",
                        "data": {"sessions": [], "total": 0},
                    }
                ).encode(),
                b"",
            )

    async def fake_create_subprocess_exec(*command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await control_commands.query_runtime(
        "session.list", cwd="D:/workspace", params={"limit": 10}
    )

    assert result["total"] == 0
    assert observed["command"][-2:] == ("--query-json", "-")
    assert observed["request"]["params"] == {"limit": 10}
    assert observed["request"]["workspace"] == {"cwd": "D:/workspace"}


@pytest.mark.asyncio
async def test_invalid_control_arguments_never_start_query(monkeypatch) -> None:
    async def fail_query(*_args, **_kwargs):
        pytest.fail("invalid control input must stay local")

    monkeypatch.setattr(repl, "query_runtime", fail_query)
    output = StringIO()
    ui = ProcessCliUI(output)
    args = _args()
    state = _state()

    await _command("/sessions 0", args, state, ui)
    await _command("/status extra", args, state, ui)
    await _command("/permissions extra", args, state, ui)

    assert "limit 必须在 1 到 200" in output.getvalue()
    assert "用法：/status" in output.getvalue()
    assert "用法：/permissions" in output.getvalue()
