# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Heartbeat turns retain their execution identity in restored session history."""

from __future__ import annotations

from typing import Any, AsyncIterator

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    HOST_USER_ORIGIN_EXTERNAL,
    HOST_USER_ORIGIN_INTERNAL,
    root_decision_context_from_extra,
)
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.agent_adapter.interface import (
    JiuWenSwarm,
    _history_user_extra,
    _with_heartbeat_history_metadata,
)


def _heartbeat_params() -> dict:
    automation = {
        "kind": "heartbeat",
        "job_id": "hb-1",
        "run_id": "run-1",
        "triggered_at": 1000.0,
        "trigger": "scheduler",
        "source": "agent_tool",
    }
    return {
        "automation": automation,
    }


@pytest.mark.parametrize("location", ["params", "metadata", "nested", "both", "none"])
def test_web_heartbeat_is_history_not_user_authority(monkeypatch, location):
    marker = _heartbeat_params()
    params = {"query": "inspect notes", "mode": "agent"}
    metadata = {}
    if location in {"params", "both"}:
        params.update(marker)
    if location in {"metadata", "both"}:
        metadata.update(marker)
        # A later metadata merge must not erase an inbound authority downgrade.
        params["metadata"] = {"automation": {"kind": "other"}}
    if location == "nested":
        params["metadata"] = marker
    request = AgentRequest(
        request_id="heartbeat-origin", session_id="origin-session", channel_id="web",
        req_method=ReqMethod.CHAT_SEND, params=params, metadata=metadata,
    )
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _cfg: "off")
    inputs, _, turn = JiuWenSwarm()._build_inputs(request)
    adapter = JiuWenSwarmDeepAdapter()
    adapter.mark_as_session_scoped(request.session_id)
    inputs = adapter._with_root_context(request, inputs)
    context = root_decision_context_from_extra(inputs["run"]["context"]["extra"])
    external = location == "none"
    assert turn.origin_kind == (
        HOST_USER_ORIGIN_EXTERNAL if external else HOST_USER_ORIGIN_INTERNAL
    )
    assert adapter._is_host_permission_update_input(request) is external
    assert len(context.trusted_turns) == int(external)
    assert interface_module._should_record_user_history(params) is True


def test_heartbeat_user_history_retains_automation_context() -> None:
    params = _heartbeat_params()

    extra = _history_user_extra(params)

    assert extra == {"metadata": {"automation": params["automation"]}}


def test_heartbeat_user_history_preserves_latest_develop_skills() -> None:
    params = {
        **_heartbeat_params(),
        "skills": ["  skill-a  ", "", "skill-b"],
    }

    extra = _history_user_extra(params)

    assert extra == {
        "skills": ["skill-a", "skill-b"],
        "metadata": {"automation": params["automation"]},
    }


def test_heartbeat_assistant_history_uses_web_compatible_marker_shape() -> None:
    params = _heartbeat_params()

    extra = _with_heartbeat_history_metadata(
        {"source": "agent-output", "automation": {"legacy": True}},
        params,
    )

    assert extra == {
        "source": "agent-output",
        "metadata": {"automation": params["automation"]},
    }


@pytest.mark.asyncio
async def test_heartbeat_stream_persists_both_turns_in_original_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Adapter:
        async def process_message_stream_impl(
            self, request: AgentRequest, _inputs: dict[str, Any]
        ) -> AsyncIterator[AgentResponseChunk]:
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"event_type": "chat.final", "content": "follow-up done"},
                is_complete=False,
            )

        async def handle_heartbeat(self, _request: AgentRequest) -> None:
            return None

    facade = JiuWenSwarm()
    records: list[dict[str, Any]] = []
    monkeypatch.setattr(facade, "_adapter", _Adapter())
    monkeypatch.setattr(facade, "_sdk_name", "harness")
    monkeypatch.setattr(
        interface_module,
        "append_history_record",
        lambda **kwargs: records.append(kwargs),
    )
    monkeypatch.setattr(
        interface_module,
        "get_config",
        lambda: {"preferred_language": "zh"},
    )
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _cfg: "off")
    monkeypatch.setattr(interface_module, "build_user_prompt", lambda q, **_kw: q)

    params = {**_heartbeat_params(), "query": "continue in this session", "mode": "agent"}
    request = AgentRequest(
        request_id="run-1",
        channel_id="web",
        session_id="original-session",
        params=params,
        metadata={"automation": params["automation"]},
        is_stream=True,
    )

    async for _ in facade.process_message_stream(request):
        pass

    user_records = [record for record in records if record["role"] == "user"]
    assistant_records = [
        record
        for record in records
        if record["role"] == "assistant" and record.get("event_type") == "chat.final"
    ]
    assert len(user_records) == 1
    assert len(assistant_records) == 1
    assert user_records[0]["session_id"] == "original-session"
    assert assistant_records[0]["session_id"] == "original-session"
    assert user_records[0]["extra"] == {
        "metadata": {"automation": params["automation"]}
    }
    assert assistant_records[0]["extra"]["metadata"] == {
        "automation": params["automation"]
    }
