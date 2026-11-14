# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""LLM lifecycle records must not duplicate the model input window."""

import json
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
from jiuwenswarm.server.runtime.session import session_history as history


@pytest.mark.asyncio
async def test_repeated_llm_calls_persist_status_without_copying_context():
    session_id = "incremental_llm_history"

    async def write_stream(event):
        history.append_history_record(
            session_id=session_id,
            request_id="request_1",
            channel_id="web",
            role="assistant",
            content="",
            timestamp=1.0,
            event_type="chat." + event.type,
            extra=event.payload,
        )

    context = "PRIVATE_CONTEXT_BODY" * 16384
    ctx = SimpleNamespace(
        session=SimpleNamespace(write_stream=write_stream),
        inputs=SimpleNamespace(messages=[{"role": "user", "content": context}]),
    )
    rail = JiuSwarmStreamEventRail()
    for _ in range(20):
        await rail._emit_llm_call_signal(ctx, "llm_call_start")
        await rail._emit_llm_call_signal(ctx, "llm_call_end")
    history.flush_history_writes()
    records = history.load_history_records(session_id)

    assert [row["event_type"] for row in records] == ["chat.llm_call_start", "chat.llm_call_end"] * 20
    assert all("prompt" not in row for row in records)
    assert history.get_read_history_path(session_id).stat().st_size < 16384
    assert ctx.inputs.messages[0]["content"] == context


def test_start_event_without_prompt_is_saved():
    history.append_history_record(
        session_id="status_only_start",
        request_id="request_1",
        channel_id="web",
        role="assistant",
        content="",
        timestamp=1.0,
        event_type="chat.llm_call_start",
        extra={"member_name": "worker", "role": "teammate"},
    )
    history.flush_history_writes()
    records = history.load_history_records("status_only_start")
    assert len(records) == 1
    assert records[0]["member_name"] == "worker"
    assert records[0]["event_type"] == "chat.llm_call_start"


@pytest.mark.asyncio
async def test_subagent_history_keeps_usage_without_prompt(monkeypatch):
    from openjiuwen.agent_teams import context
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.runner.callback.events import LLMCallEvents
    from jiuwenswarm.agents.harness import agent_observability as observability

    callbacks = {}

    def register(event, callback, *, namespace):
        callbacks[event] = callback

    monkeypatch.setattr(Runner, "callback_framework", SimpleNamespace(register_sync=register))
    monkeypatch.setattr(context, "get_session_id", lambda: "parent_sub_worker_123")
    monkeypatch.setattr(observability, "_PARENT_REQ_BY_SESSION", {"parent": ("request_1", "web", "agent")})
    monkeypatch.setattr(observability, "_LAST_CALL_START_BY_SESSION", {})
    observability.install_subagent_llm_history_forwarder()
    await callbacks[LLMCallEvents.LLM_INVOKE_INPUT](
        messages=[{"role": "user", "content": "PRIVATE_SUBAGENT_INPUT" * 1000}],
    )
    await callbacks[LLMCallEvents.LLM_OUTPUT](
        response=SimpleNamespace(content="answer", usage_metadata={"input_tokens": 100, "output_tokens": 20}),
    )
    history.flush_history_writes()
    records = history.load_history_records("parent")
    assert [row["event_type"] for row in records] == [
        "chat.llm_call_start", "chat.usage_metadata", "chat.llm_call_end",
    ]
    usage = records[1]["metadata"]["usage_metadata"]
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 20
    assert "prompt" not in usage
    assert "PRIVATE_SUBAGENT_INPUT" not in json.dumps(records)
    assert records[0]["subagent_type"] == "worker"
