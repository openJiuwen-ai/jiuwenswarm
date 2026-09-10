# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

# pylint: disable=protected-access

"""Regression tests for request context crossing the DeepAgent input boundary."""

from __future__ import annotations

import pytest
from openjiuwen.harness.deep_agent import DeepAgent

from jiuwenswarm.common.context_keys import (
    JIUWENSWARM_CHANNEL_CONTEXT_KEY,
    JIUWENSWARM_SKIP_A2UI_CONTEXT_KEY,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module


def _build_inputs(monkeypatch, request: AgentRequest) -> dict:
    monkeypatch.setattr(
        interface_module,
        "get_config",
        lambda: {"preferred_language": "zh"},
    )
    monkeypatch.setattr(
        interface_module,
        "get_memory_mode",
        lambda _config: "disabled",
    )
    inputs, _, _ = interface_module.JiuWenSwarm().build_inputs(request)
    return inputs


@pytest.mark.parametrize(
    "mode",
    ["agent", "code.normal", "code.plan", "code.team"],
)
def test_request_channel_survives_sdk_normalization_for_every_mode(
    monkeypatch,
    mode,
):
    """Channel propagation is an invocation invariant, not a mode concern."""
    inputs = _build_inputs(
        monkeypatch,
        AgentRequest(
            request_id=f"req-{mode}",
            channel_id="web",
            session_id="opaque-session-id",
            params={"query": "generate an A2UI form", "mode": mode},
        ),
    )

    normalized = DeepAgent._normalize_inputs(None, inputs)

    assert normalized.run_context is not None
    assert (
        normalized.run_context.extra[JIUWENSWARM_CHANNEL_CONTEXT_KEY]
        == "web"
    )


def test_request_context_preserves_existing_run_metadata(monkeypatch):
    """JiuwenSwarm metadata must merge without mutating caller-owned data."""
    original_run = {
        "kind": "heartbeat",
        "context": {
            "session_id": "heartbeat-session",
            "extra": {"existing": "kept"},
        },
    }
    inputs = _build_inputs(
        monkeypatch,
        AgentRequest(
            request_id="req-heartbeat",
            channel_id="web",
            session_id="opaque-session-id",
            params={
                "query": "heartbeat query",
                "mode": "code.normal",
                "run": original_run,
            },
            metadata={"skip_a2ui": True},
        ),
    )

    run = inputs["run"]
    assert run["kind"] == "heartbeat"
    assert run["context"]["session_id"] == "heartbeat-session"
    assert run["context"]["extra"] == {
        "existing": "kept",
        JIUWENSWARM_CHANNEL_CONTEXT_KEY: "web",
        JIUWENSWARM_SKIP_A2UI_CONTEXT_KEY: True,
    }
    assert original_run["context"]["extra"] == {"existing": "kept"}


def test_cron_context_keeps_channel_and_cron_payload(monkeypatch):
    """Cron conversion and request channel propagation must compose."""
    cron = {"job_id": "job-1"}
    inputs = _build_inputs(
        monkeypatch,
        AgentRequest(
            request_id="req-cron",
            channel_id="feishu",
            session_id="opaque-session-id",
            params={
                "query": "scheduled query",
                "mode": "code.normal",
                "cron": cron,
            },
        ),
    )

    assert inputs["run"]["kind"] == "cron"
    assert inputs["run"]["context"]["extra"] == {
        "cron": cron,
        JIUWENSWARM_CHANNEL_CONTEXT_KEY: "feishu",
    }


def test_missing_request_channel_remains_fail_closed(monkeypatch):
    """A session id must not silently turn missing request context into Web."""
    inputs = _build_inputs(
        monkeypatch,
        AgentRequest(
            request_id="req-missing-channel",
            channel_id="",
            session_id="sess_looks_like_web",
            params={"query": "generate an A2UI form", "mode": "code.normal"},
        ),
    )

    assert (
        inputs["run"]["context"]["extra"][JIUWENSWARM_CHANNEL_CONTEXT_KEY]
        == ""
    )
