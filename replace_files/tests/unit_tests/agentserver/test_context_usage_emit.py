# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Tests for context.usage emission (limit coercion and used-token semantics)."""

# pylint: disable=protected-access

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenclaw.agentserver.deep_agent.rails.stream_event_rail import (
    JiuClawStreamEventRail,
    _DEFAULT_CONTEXT_WINDOW_LIMIT_TOKENS,
    _resolve_context_window_limit_tokens,
)


def test_resolve_limit_accepts_numeric_forms() -> None:
    with patch("jiuwenclaw.agentserver.deep_agent.rails.stream_event_rail.get_config") as gc:
        gc.return_value = {"react": {"context_window_limit_tokens": 128000}}
        assert _resolve_context_window_limit_tokens() == 128000

    with patch("jiuwenclaw.agentserver.deep_agent.rails.stream_event_rail.get_config") as gc:
        gc.return_value = {"react": {"context_window_limit_tokens": "128000"}}
        assert _resolve_context_window_limit_tokens() == 128000

    with patch("jiuwenclaw.agentserver.deep_agent.rails.stream_event_rail.get_config") as gc:
        gc.return_value = {"react": {"context_window_limit_tokens": 128000.0}}
        assert _resolve_context_window_limit_tokens() == 128000


def test_resolve_limit_invalid_falls_back() -> None:
    with patch("jiuwenclaw.agentserver.deep_agent.rails.stream_event_rail.get_config") as gc:
        gc.return_value = {"react": {"context_window_limit_tokens": "nope"}}
        assert _resolve_context_window_limit_tokens() == _DEFAULT_CONTEXT_WINDOW_LIMIT_TOKENS

    with patch("jiuwenclaw.agentserver.deep_agent.rails.stream_event_rail.get_config") as gc:
        gc.return_value = {"react": {"context_window_limit_tokens": True}}
        assert _resolve_context_window_limit_tokens() == _DEFAULT_CONTEXT_WINDOW_LIMIT_TOKENS

    with patch("jiuwenclaw.agentserver.deep_agent.rails.stream_event_rail.get_config") as gc:
        gc.return_value = {"react": {"context_window_limit_tokens": 0}}
        assert _resolve_context_window_limit_tokens() == _DEFAULT_CONTEXT_WINDOW_LIMIT_TOKENS

    with patch("jiuwenclaw.agentserver.deep_agent.rails.stream_event_rail.get_config") as gc:
        gc.return_value = {"react": {"context_window_limit_tokens": -1}}
        assert _resolve_context_window_limit_tokens() == _DEFAULT_CONTEXT_WINDOW_LIMIT_TOKENS


def test_resolve_limit_missing_key_falls_back() -> None:
    with patch("jiuwenclaw.agentserver.deep_agent.rails.stream_event_rail.get_config") as gc:
        gc.return_value = {}
        assert _resolve_context_window_limit_tokens() == _DEFAULT_CONTEXT_WINDOW_LIMIT_TOKENS

    with patch("jiuwenclaw.agentserver.deep_agent.rails.stream_event_rail.get_config") as gc:
        gc.return_value = None
        assert _resolve_context_window_limit_tokens() == _DEFAULT_CONTEXT_WINDOW_LIMIT_TOKENS


@pytest.mark.asyncio
async def test_emit_context_usage_with_tokens() -> None:
    session = AsyncMock()
    stat = SimpleNamespace(single_messages_token=10000)
    context = MagicMock()
    context.statistic.return_value = stat
    ctx = SimpleNamespace(session=session, context=context)

    with patch("jiuwenclaw.agentserver.deep_agent.rails.stream_event_rail.get_config") as gc:
        gc.return_value = {"react": {"context_window_limit_tokens": 20000}}
        await JiuClawStreamEventRail._emit_context_usage(ctx)
    event = session.write_stream.await_args.args[0]
    assert event.type == "context.usage"
    assert event.payload["used_tokens"] == 10000
    assert event.payload["limit_tokens"] == 20000
    assert event.payload["usage_percent"] == 50.0


@pytest.mark.asyncio
async def test_emit_context_usage_no_session() -> None:
    ctx = SimpleNamespace(session=None, context=MagicMock())
    await JiuClawStreamEventRail._emit_context_usage(ctx)


@pytest.mark.asyncio
async def test_emit_context_usage_no_context() -> None:
    session = AsyncMock()
    ctx = SimpleNamespace(session=session, context=None)
    await JiuClawStreamEventRail._emit_context_usage(ctx)
    session.write_stream.assert_not_called()


@pytest.mark.asyncio
async def test_emit_context_usage_string_limit_coerced() -> None:
    session = AsyncMock()
    stat = SimpleNamespace(single_messages_token=64000)
    context = MagicMock()
    context.statistic.return_value = stat
    ctx = SimpleNamespace(session=session, context=context)

    with patch("jiuwenclaw.agentserver.deep_agent.rails.stream_event_rail.get_config") as gc:
        gc.return_value = {"react": {"context_window_limit_tokens": "128000"}}
        await JiuClawStreamEventRail._emit_context_usage(ctx)
    event = session.write_stream.await_args.args[0]
    assert event.payload["limit_tokens"] == 128000
    assert event.payload["usage_percent"] == 50.0


@pytest.mark.asyncio
async def test_emit_context_usage_no_stat_token_percent_none() -> None:
    session = AsyncMock()
    stat = SimpleNamespace(single_messages_token=None)
    context = MagicMock()
    context.statistic.return_value = stat
    ctx = SimpleNamespace(session=session, context=context)

    with patch("jiuwenclaw.agentserver.deep_agent.rails.stream_event_rail.get_config") as gc:
        gc.return_value = {"react": {"context_window_limit_tokens": 1000}}
        await JiuClawStreamEventRail._emit_context_usage(ctx)
    event = session.write_stream.await_args.args[0]
    assert event.payload["used_tokens"] is None
    assert event.payload["usage_percent"] is None
