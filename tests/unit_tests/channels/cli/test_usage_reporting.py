# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression evidence for tool names and accounting on the CLI wire path."""

import argparse
import logging
from unittest.mock import AsyncMock, patch

import pytest

from jiuwenswarm.channels.cli import chat
from jiuwenswarm.channels.cli.gateway_client import GatewayClient
from jiuwenswarm.channels.cli.render import HumanRenderer


@pytest.fixture(autouse=True)
def capture_cli_logs(monkeypatch):
    """Let pytest capture logs despite the application's isolated log handlers."""
    monkeypatch.setattr(logging.getLogger("jiuwenswarm"), "propagate", True)


def test_nested_tool_call_displays_name_and_arguments(caplog):
    renderer = HumanRenderer(show_tools=True)
    with caplog.at_level(logging.INFO, logger="jiuwenswarm.channels.cli.render"):
        renderer.handle_tool_call({
            "tool_call": {"name": "bash", "arguments": {"command": "pwd"}},
        })
    assert '[tool] bash: {"command": "pwd"}' in caplog.messages


def test_usage_is_silent_until_reported(caplog):
    with caplog.at_level(logging.INFO, logger=chat.__name__):
        chat._emit_usage(HumanRenderer())
    assert not any(message.startswith("[usage]") for message in caplog.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize(("terminal", "expected_code"), [
    ({"type": "event", "event": "chat.final", "payload": {"content": "done"}}, 0),
    ({"type": "event", "event": "chat.error", "payload": {"error": "failed"}}, 1),
    (OSError("connection lost"), 4),
])
async def test_chat_reports_usage_once_on_success_and_failure(terminal, expected_code, caplog):
    client = AsyncMock(spec=GatewayClient)
    usage = {"metadata": {"usage_metadata": {
        "input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050,
        "cache_tokens": 200, "total_cost": 0.01,
    }}}
    client.recv.side_effect = [
        {"type": "event", "event": "chat.usage_metadata", "payload": usage},
        {"type": "event", "event": "chat.usage_metadata", "payload": usage},
        {"type": "event", "event": "context.usage",
         "payload": {"tokens_used": 100, "context_max": 8000}},
        {"type": "event", "event": "context.usage",
         "payload": {"tokens_used": 2000, "context_max": 8000}},
        terminal,
    ]
    args = argparse.Namespace(gateway_url="ws://127.0.0.1:19001/tui", json=False,
                              jsonl=False, show_reasoning=False, show_tools=False, timeout=2)
    request = {"params": {"mode": "code.normal", "session_id": "usage-test"}}
    with (
        patch.object(chat, "GatewayClient", return_value=client),
        patch.object(chat, "_build_request", return_value=request),
        caplog.at_level(logging.INFO, logger=chat.__name__),
    ):
        assert await chat._run_chat(args, "hello") == expected_code

    summaries = [message for message in caplog.messages if message.startswith("[usage]")]
    assert summaries == [
        "[usage] 2 model call(s)  ·  in 2,000  ·  out 100  ·  total 2,100 tokens"
        "  ·  cached 400  ·  context 2,000/8,000  ·  cost 0.0200"
    ]
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_reports_usage_when_cancelled(caplog):
    import asyncio

    client = AsyncMock(spec=GatewayClient)
    args = argparse.Namespace(gateway_url="ws://127.0.0.1:19001/tui", json=False,
                              jsonl=False, show_reasoning=False, show_tools=False, timeout=2)

    async def cancel_after_usage(_client, renderer, _request, **_kwargs):
        renderer.handle_usage({"metadata": {"usage_metadata": {"total_tokens": 12}}})
        raise asyncio.CancelledError

    with (
        patch.object(chat, "GatewayClient", return_value=client),
        patch.object(chat, "_build_request", return_value={"params": {"mode": "code.normal"}}),
        patch.object(chat, "_run_interactive_loop", side_effect=cancel_after_usage),
        caplog.at_level(logging.INFO, logger=chat.__name__),
        pytest.raises(asyncio.CancelledError),
    ):
        await chat._run_chat(args, "hello")

    summaries = [message for message in caplog.messages if message.startswith("[usage]")]
    assert summaries == ["[usage] 1 model call(s)  ·  in 0  ·  out 0  ·  total 12 tokens"]
    client.close.assert_awaited_once()
