# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Subagent callbacks persist lifecycle and usage without copying model inputs."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openjiuwen.agent_teams.context import reset_session_id, set_session_id
from openjiuwen.core.foundation.llm import SystemMessage, UserMessage
from openjiuwen.core.runner import Runner
from openjiuwen.core.runner.callback.events import LLMCallEvents
from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework

from jiuwenswarm.agents.harness import agent_observability as observability
from jiuwenswarm.server.runtime.session import history_artifacts as artifacts
from jiuwenswarm.server.runtime.session import session_history as history
from jiuwenswarm.server.runtime.session import session_metadata
from tests.unit_tests.test_context_snapshots import stored

INPUT_EVENTS = (LLMCallEvents.LLM_INVOKE_INPUT, LLMCallEvents.LLM_STREAM_INPUT)


@pytest.fixture
def forwarder(stored, monkeypatch):
    framework = AsyncCallbackFramework(enable_metrics=False, enable_logging=False)
    monkeypatch.setattr(Runner, "callback_framework", framework)
    monkeypatch.setattr(observability, "_PARENT_REQ_BY_SESSION", {})
    monkeypatch.setattr(observability, "_LAST_CALL_START_BY_SESSION", {})
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: stored.path.parent.parent)
    observability.set_parent_request_context(
        session_id=stored.sid, request_id="parent-request", channel_id="web", mode="agent",
    )
    observability.install_subagent_llm_history_forwarder()
    return SimpleNamespace(framework=framework, stored=stored, child=stored.sid + "_sub_code_test")


async def _trigger(forwarder, event, *, session_id=None, **kwargs):
    token = set_session_id(session_id or forwarder.child)
    try:
        await forwarder.framework.trigger(event, **kwargs)
    finally:
        reset_session_id(token)


async def _records(forwarder):
    assert await asyncio.to_thread(history.flush_pending_writes, timeout=5)
    return [json.loads(line) for line in forwarder.stored.path.read_bytes().splitlines()]


@pytest.mark.asyncio
@pytest.mark.parametrize("event", INPUT_EVENTS)
@pytest.mark.parametrize("failure", [OSError, PermissionError])
async def test_artifact_io_failure_keeps_start_usage_and_end_audit(forwarder, monkeypatch, event, failure):
    def unavailable(*_args, **_kwargs):
        raise failure("controlled artifact storage failure")

    monkeypatch.setattr(artifacts, "put_prompt", unavailable)
    messages = [SystemMessage(content="LARGE SYSTEM " * 10000), UserMessage(content="FIRST USER TASK")]
    await _trigger(forwarder, event, messages=messages, tools=[])
    await _trigger(forwarder, LLMCallEvents.LLM_OUTPUT, response=SimpleNamespace(
        content="SUBAGENT RESULT", model="test-model", usage_metadata={"input_tokens": 11, "output_tokens": 3},
    ))
    rows = await _records(forwarder)
    assert [row["event_type"] for row in rows] == [
        "chat.llm_call_start", "chat.usage_metadata", "chat.llm_call_end",
    ]
    start = rows[0]
    assert start["request_id"] == "parent-request" and start["channel_id"] == "web"
    assert start["subagent_type"] == "code" and start["sub_session_id"] == forwarder.child
    assert "prompt" not in start
    assert "payload_refs" not in start
    usage = rows[1]["metadata"]
    assert "prompt" not in usage["usage_metadata"]
    assert usage["usage_metadata"]["input_tokens"] == 11
    assert usage["total_latency_ms"] >= 0
    assert rows[2]["content"] == "SUBAGENT RESULT"
    assert len(forwarder.stored.path.read_bytes()) < 32 * 1024


@pytest.mark.asyncio
@pytest.mark.parametrize("event", INPUT_EVENTS)
async def test_model_input_and_tools_are_preserved_without_prompt_artifact(forwarder, monkeypatch, event):
    put_prompt = Mock()
    monkeypatch.setattr(artifacts, "put_prompt", put_prompt)
    system = "COMPLETE SYSTEM " * 10000
    messages = [SystemMessage(content=system), UserMessage(content="ORIGINAL TASK")]
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    await _trigger(
        forwarder, event,
        messages=messages,
        tools=tools,
    )
    rows = await _records(forwarder)
    assert len(rows) == 1 and rows[0]["event_type"] == "chat.llm_call_start"
    assert "prompt" not in rows[0] and "payload_refs" not in rows[0]
    assert messages[0].content == system and messages[1].content == "ORIGINAL TASK"
    assert tools == [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    put_prompt.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("event", INPUT_EVENTS)
async def test_image_probe_is_not_written_as_a_subagent_call(forwarder, monkeypatch, event):
    def unexpected(*_args, **_kwargs):
        pytest.fail("Image probes must not write prompt artifacts")

    monkeypatch.setattr(artifacts, "put_prompt", unexpected)
    await _trigger(forwarder, event, messages=[{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,probe"}},
        {"type": "text", "text": "probe"},
    ]}])
    assert await _records(forwarder) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", ["parent-only", "missing-parent_sub_code_test"])
async def test_calls_without_a_bound_subagent_parent_do_not_pollute_history(forwarder, monkeypatch, session_id):
    def unexpected(*_args, **_kwargs):
        pytest.fail("Unattributed calls must not write prompt artifacts")

    monkeypatch.setattr(artifacts, "put_prompt", unexpected)
    await _trigger(forwarder, LLMCallEvents.LLM_INVOKE_INPUT, session_id=session_id,
                   messages=[UserMessage(content="unrelated")])
    assert await _records(forwarder) == []
