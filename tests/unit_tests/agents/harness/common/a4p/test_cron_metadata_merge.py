# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cron job_id must survive into authorization execution context metadata."""

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.agents.harness.common.a4p.execution_context import (
    AUTHORIZATION_EXECUTION_CONTEXTS,
    build_authorization_execution_context,
)


def test_merge_cron_from_params_when_metadata_missing() -> None:
    request = SimpleNamespace(
        request_id="cron-abc123:1700000000",
        session_id="__cron___s1",
        channel_id="__cron__",
        metadata={},
        params={"cron": {"job_id": "abc123", "run_id": "abc123:1700000000"}, "cron_id": "abc123"},
        agent_ref=None,
        user_id="u1",
    )
    ctx = build_authorization_execution_context(request, agent_id="agent", mode="agent")
    assert ctx.metadata["cron"]["job_id"] == "abc123"


def test_merge_cron_from_request_id() -> None:
    request = SimpleNamespace(
        request_id="cron-deadbeef:1794723943",
        session_id="__cron___s2",
        channel_id="__cron__",
        metadata={"targets": "web"},
        params={"content": "date"},
        agent_ref=None,
        user_id="u1",
    )
    ctx = build_authorization_execution_context(request, agent_id="agent", mode="agent")
    assert ctx.metadata["cron"]["job_id"] == "deadbeef"


def test_get_by_request_id() -> None:
    AUTHORIZATION_EXECUTION_CONTEXTS.clear()
    request = SimpleNamespace(
        request_id="cron-jid1:1",
        session_id="__cron___lookup",
        channel_id="__cron__",
        metadata={"cron": {"job_id": "jid1"}},
        params={},
        agent_ref=None,
        user_id="u1",
    )
    ctx = build_authorization_execution_context(request, agent_id="agent", mode="agent")
    AUTHORIZATION_EXECUTION_CONTEXTS.activate(ctx)
    try:
        found = AUTHORIZATION_EXECUTION_CONTEXTS.get_by_request_id("cron-jid1:1")
        assert found is not None
        assert found.session_id == "__cron___lookup"
        assert found.metadata["cron"]["job_id"] == "jid1"
    finally:
        AUTHORIZATION_EXECUTION_CONTEXTS.release(ctx.session_id, ctx.request_id)
