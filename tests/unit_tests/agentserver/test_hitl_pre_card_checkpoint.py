# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HITL cards must persist the interrupted turn before reaching the client."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)

_UNSET = object()


def _make_adapter(
    *,
    commit_error: Exception | None = None,
    context: object = _UNSET,
) -> tuple[JiuWenSwarmDeepAdapter, SimpleNamespace]:
    if context is _UNSET:
        context = SimpleNamespace(get_messages=lambda: [object()])
    session = SimpleNamespace(commit=AsyncMock())
    if commit_error is not None:
        session.commit.side_effect = commit_error
    context_engine = SimpleNamespace(
        save_contexts=AsyncMock(),
        get_context=MagicMock(return_value=context),
    )
    instance = SimpleNamespace(
        _interaction_session=session,
        react_agent=SimpleNamespace(context_engine=context_engine),
    )
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = instance
    adapter.mark_session_awaiting_tool_permission = MagicMock()
    return adapter, session


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", ["sess_hitl_1", "cron_job_1"])
async def test_hitl_checkpoint_committed_for_all_sessions(session_id: str) -> None:
    adapter, session = _make_adapter()

    await adapter._commit_hitl_pause_before_card(session_id, "req-1")

    adapter._instance.react_agent.context_engine.save_contexts.assert_awaited_once_with(session)
    session.commit.assert_awaited_once()
    adapter.mark_session_awaiting_tool_permission.assert_called_once_with(session_id)


@pytest.mark.asyncio
async def test_hitl_checkpoint_commit_failure_does_not_raise() -> None:
    adapter, session = _make_adapter(commit_error=RuntimeError("disk full"))

    await adapter._commit_hitl_pause_before_card("sess_hitl_2", "req-2")

    session.commit.assert_awaited_once()
    adapter.mark_session_awaiting_tool_permission.assert_called_once_with("sess_hitl_2")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "context",
    [None, SimpleNamespace(get_messages=list)],
    ids=["missing", "empty"],
)
async def test_hitl_checkpoint_skips_unpersistable_context(context: object) -> None:
    adapter, session = _make_adapter(context=context)

    await adapter._commit_hitl_pause_before_card("sess_hitl_2", "req-3")

    adapter._instance.react_agent.context_engine.save_contexts.assert_not_awaited()
    session.commit.assert_not_awaited()
    adapter.mark_session_awaiting_tool_permission.assert_called_once_with("sess_hitl_2")
