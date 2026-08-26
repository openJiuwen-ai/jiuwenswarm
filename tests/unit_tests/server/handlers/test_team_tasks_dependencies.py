# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for team.tasks.dependencies RPC handler.

Tests handle_team_tasks_dependencies (team.py) — the handler that returns
task dependency edges for the task panorama (relay-claw finalize best-effort RPC).
Mirrors the handler's lazy-import structure; mocks at source modules.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.server.handlers import team as team_handlers


def _make_ctx(
    params: dict[str, Any] | None = None,
    session_id: str = "s1",
    channel_id: str = "web",
    request_id: str = "req-1",
) -> tuple[SimpleNamespace, list]:
    """Build a fake ctx with request + sink that captures send_wire payloads."""
    sent: list = []
    ctx = SimpleNamespace(
        request=SimpleNamespace(
            params=params or {},
            session_id=session_id,
            channel_id=channel_id,
            request_id=request_id,
        ),
        sink=SimpleNamespace(
            send_wire=AsyncMock(side_effect=lambda wire: sent.append(wire)),
        ),
    )
    return ctx, sent


def _patch_common_mocks(monkeypatch: pytest.MonkeyPatch, db_return: Any) -> None:
    """Patch the lazy-imported deps + the encode (passthrough) + DB query."""
    fake_tm = SimpleNamespace(get_active_team_name=lambda sid: "t1")
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda channel: fake_tm,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda sid, sessions_root=None: {},
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.resolve_session_runtime_team_name",
        lambda metadata: "",
    )
    monkeypatch.setattr(team_handlers, "_sessions_dir_for_request", lambda request: "/tmp")
    # encode passthrough: send the resp directly (skip wire serialization for easy assertion)
    monkeypatch.setattr(team_handlers, "encode_agent_response_for_wire", lambda resp, **kw: resp)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.handlers.team_monitor_handler."
        "TeamMonitorHandler.get_team_dependencies_from_db",
        AsyncMock(return_value=db_return),
    )


def _make_edge(task_id: str, depends_on: str, resolved: bool = False) -> dict:
    return {
        "task_id": task_id,
        "depends_on_task_id": depends_on,
        "resolved": resolved,
    }


@pytest.mark.asyncio
async def test_dependencies_rpc_returns_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    """Handler returns edges from get_team_dependencies_from_db."""
    ctx, sent = _make_ctx(params={"session_id": "s1", "team_name": "t1"})
    test_edges = [_make_edge("t2", "t1", False), _make_edge("t3", "t1", True)]
    _patch_common_mocks(monkeypatch, test_edges)

    await team_handlers.handle_team_tasks_dependencies(ctx)

    assert len(sent) == 1
    resp = sent[0]
    assert resp.ok is True
    edges = resp.payload["edges"]
    assert len(edges) == 2
    assert edges[0] == {"task_id": "t2", "depends_on_task_id": "t1", "resolved": False}
    assert edges[1] == {"task_id": "t3", "depends_on_task_id": "t1", "resolved": True}


@pytest.mark.asyncio
async def test_dependencies_rpc_empty_when_no_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    """Handler returns empty edges list when DB has none."""
    ctx, sent = _make_ctx(params={"session_id": "s1", "team_name": "t1"})
    _patch_common_mocks(monkeypatch, [])

    await team_handlers.handle_team_tasks_dependencies(ctx)

    assert len(sent) == 1
    resp = sent[0]
    assert resp.ok is True
    assert resp.payload["edges"] == []


@pytest.mark.asyncio
async def test_dependencies_rpc_empty_when_no_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Handler returns empty edges when session_id is missing (guard skips DB query)."""
    ctx, sent = _make_ctx(params={}, session_id="")

    mock_db = AsyncMock(side_effect=AssertionError("should not be called without session_id"))
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.handlers.team_monitor_handler."
        "TeamMonitorHandler.get_team_dependencies_from_db",
        mock_db,
    )
    monkeypatch.setattr(team_handlers, "encode_agent_response_for_wire", lambda resp, **kw: resp)

    await team_handlers.handle_team_tasks_dependencies(ctx)

    assert len(sent) == 1
    resp = sent[0]
    assert resp.ok is True
    assert resp.payload["edges"] == []
    mock_db.assert_not_called()


@pytest.mark.asyncio
async def test_dependencies_rpc_db_failure_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Handler returns empty edges (not error) when DB query fails (best-effort)."""
    ctx, sent = _make_ctx(params={"session_id": "s1", "team_name": "t1"})
    _patch_common_mocks(monkeypatch, None)  # simulates DB failure (returns None)

    await team_handlers.handle_team_tasks_dependencies(ctx)

    assert len(sent) == 1
    resp = sent[0]
    assert resp.ok is True
    assert resp.payload["edges"] == []
