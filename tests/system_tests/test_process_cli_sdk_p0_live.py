# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Opt-in acceptance through a real SDK child, Runtime, and configured model."""

import os
import shutil
import sys
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_sdk_p0_real_model(tmp_path: Path) -> None:
    if os.getenv("JIUWENSWARM_REAL_MODEL_TEST") != "1":
        pytest.skip("set JIUWENSWARM_REAL_MODEL_TEST=1")
    source = Path.home() / ".jiuwenswarm" / "config"
    if not (source / "config.yaml").is_file() or not (source / ".env").is_file():
        pytest.skip("local model configuration is unavailable")
    config = tmp_path / "config"
    config.mkdir()
    for name in ("config.yaml", ".env"):
        shutil.copyfile(source / name, config / name)
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdks" / "python" / "src"))
    from jiuwenswarm_sdk import Client

    client = Client(
        [sys.executable, "-m", "jiuwenswarm.channels.process_cli.main"],
        cwd=str(root), env={"JIUWENSWARM_DATA_DIR": str(tmp_path)},
    )
    schema = {
        "type": "object", "required": ["answer"],
        "properties": {"answer": {"type": "integer"}},
        "additionalProperties": False,
    }
    structured = await client.run({
        "input": "What is 2 + 3? Answer using the requested JSON schema.",
        "mode": "agent.work.normal", "output_schema": schema,
        "max_turns": 2, "timeout_seconds": 120,
    }, deadline_seconds=180)
    assert structured["status"] == "completed", structured
    assert structured["output_json"] == {"answer": 5}, structured
    assert 1 <= structured["usage"]["model_calls"] <= 2, structured

    seen = []

    async def lookup(event):
        seen.append(event["payload"])
        assert event["payload"]["name"] == "host_lookup"
        return {"value": "HOST_TOOL_RESULT_731"}

    tool_run = await client.run({
        "input": "Call host_lookup with key=sample, then report its returned value.",
        "mode": "agent.work.normal",
        "agent": {"name": "live_host_tool", "instructions": "Use host_lookup for the user's request.",
                  "tools": ["host_lookup"]},
        "host_tools": [{"name": "host_lookup", "description": "Look up a value in the host application.",
                        "input_schema": {"type": "object", "required": ["key"],
                                         "properties": {"key": {"type": "string"}}}}],
        "max_turns": 4, "timeout_seconds": 120,
    }, on_tool_call=lookup, deadline_seconds=180)
    assert tool_run["status"] == "completed", tool_run
    assert seen and seen[0]["arguments"]["key"] == "sample", (seen, tool_run)
    assert "HOST_TOOL_RESULT_731" in tool_run["output"], tool_run

    callbacks_before_limit = len(seen)
    one_turn = await client.run({
        "input": "Call host_lookup with key=sample, then report its returned value.",
        "mode": "agent.work.normal",
        "agent": {"name": "live_one_turn_host_tool",
                  "instructions": "Use host_lookup for this request.",
                  "tools": ["host_lookup"]},
        "host_tools": [{"name": "host_lookup", "description": "Look up a value in the host application.",
                        "input_schema": {"type": "object", "required": ["key"],
                                         "properties": {"key": {"type": "string"}}}}],
        "max_turns": 1, "timeout_seconds": 120,
    }, on_tool_call=lookup, deadline_seconds=180)
    assert len(seen) == callbacks_before_limit + 1, (seen, one_turn)
    assert one_turn["error"]["code"] == "TURN_LIMIT_EXCEEDED", one_turn
    assert one_turn["usage"]["model_calls"] == 1, one_turn

    budget = await client.run({
        "input": "Say hello briefly.", "mode": "agent.work.normal",
        "max_budget_usd": 0.000000001, "timeout_seconds": 120,
    }, deadline_seconds=180)
    assert budget["error"]["code"] in {
        "BUDGET_EXCEEDED", "BUDGET_METER_UNAVAILABLE"
    }, budget

@pytest.mark.asyncio
async def test_unattended_permission_real_model(tmp_path: Path) -> None:
    if os.getenv("JIUWENSWARM_REAL_MODEL_TEST") != "1":
        pytest.skip("set JIUWENSWARM_REAL_MODEL_TEST=1")
    source = Path.home() / ".jiuwenswarm" / "config"
    if not (source / "config.yaml").is_file() or not (source / ".env").is_file():
        pytest.skip("local model configuration is unavailable")
    config = tmp_path / "config"
    config.mkdir()
    for name in ("config.yaml", ".env"):
        shutil.copyfile(source / name, config / name)
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdks" / "python" / "src"))
    from jiuwenswarm_sdk import Client

    client = Client(
        [sys.executable, "-m", "jiuwenswarm.channels.process_cli.main"],
        cwd=str(root), env={"JIUWENSWARM_DATA_DIR": str(tmp_path)},
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "permission_must_not_write.txt"
    denial_events = []
    permission_cards = []

    async def record_denial(event):
        denial_events.append(event["event_type"])
        if event["event_type"] == "interaction.requested":
            permission_cards.append(event["payload"]["interaction"])

    denial = await client.run({
        "input": f"Use write_file to create {target} with content blocked. If denied, say denied.",
        "mode": "agent.code.normal",
        "workspace": {"cwd": str(workspace), "project_dir": str(workspace)},
        "permissions": {"tools": {"write_file": "ask"}},
        "agent": {"name": "live_unattended_permission",
                  "instructions": "Try write_file once. If denied, report that and stop.",
                  "tools": ["write_file"], "max_iterations": 4},
        "timeout_seconds": 120,
    }, on_event=record_denial, deadline_seconds=180)
    assert not target.exists(), denial
    assert denial["status"] == "completed", denial
    assert "chat.ask_user_question" in denial_events, (sorted(set(denial_events)), denial)
    assert permission_cards and permission_cards[0]["source"] == "permission_interrupt", (
        permission_cards, denial
    )
