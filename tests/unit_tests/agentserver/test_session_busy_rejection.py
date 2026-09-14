# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for session-busy explicit rejection instead of silent empty accept."""

from __future__ import annotations

from jiuwenswarm.common.e2a.gateway_normalize import e2a_response_from_agent_chunk
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def test_session_busy_chunk_shape() -> None:
    """The busy chunk is a terminal chat.error with a retryable marker."""
    chunk = JiuWenSwarmDeepAdapter._session_busy_chunk("req-1", "web")

    assert chunk.is_complete is True
    assert chunk.request_id == "req-1"
    assert chunk.channel_id == "web"
    assert chunk.payload["event_type"] == "chat.error"
    assert chunk.payload["code"] == "SESSION_BUSY"
    assert "session is busy" in chunk.payload["error"]
    assert chunk.payload["retryable"] is True


def test_session_busy_chunk_normalizes_to_e2a_error() -> None:
    """gateway_normalize must map the busy chunk to e2a.error/failed (never succeeded)."""
    chunk = JiuWenSwarmDeepAdapter._session_busy_chunk("req-1", "web")

    e2a = e2a_response_from_agent_chunk(chunk, response_id="req-1", sequence=0)

    assert e2a.response_kind == "e2a.error"
    assert e2a.status == "failed"
    assert e2a.is_final is True
    assert e2a.body["details"]["code"] == "SESSION_BUSY"
    assert "session is busy" in e2a.body["message"]
