# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Deletion failures remain visible from provider results through E2A completion."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openjiuwen.core.single_agent.rail.base import ToolCallInputs
from openjiuwen.harness.tools.base_tool import ToolOutput
from openjiuwen.extensions.sys_operation.sandbox.providers import jiuwenbox as box

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
from jiuwenswarm.common.e2a.gateway_normalize import e2a_response_from_agent_chunk
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


class StreamSession:
    def __init__(self):
        self.chunks = []

    async def write_stream(self, chunk):
        self.chunks.append(chunk)


async def emit_result(result, exception=None):
    rail = JiuSwarmStreamEventRail()
    session = StreamSession()
    tool = SimpleNamespace(id="call-1", name="write_file", arguments={})
    ctx = SimpleNamespace(
        session=session, extra={}, exception=exception,
        inputs=ToolCallInputs(
            tool_call=tool, tool_name=tool.name, tool_args={}, tool_result=result,
        ),
    )
    await rail.after_tool_call(ctx)
    # Exercise the default harness parser, not the generic utility fallback.
    return [JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk) for chunk in session.chunks]


@pytest.mark.asyncio
async def test_provider_file_failure_survives_tool_output_and_stream(monkeypatch):
    # pylint: disable=protected-access
    key = "http://localhost:8321|scope"
    monkeypatch.setattr(
        box._JiuwenBoxProviderMixin, "_pending_deletions", {(key, "old"): (True, None)},
    )
    monkeypatch.setattr(box._JiuwenBoxProviderMixin, "_delete_tasks", {})
    monkeypatch.setattr(box._JiuwenBoxProviderMixin, "_bulk_deletions", 0)
    endpoint = SimpleNamespace(base_url="http://localhost:8321", isolation_key="scope")
    provider = box.JiuwenBoxFSProvider(endpoint)
    client = Mock()
    provider._client = client
    result = await provider.write_file("/tmp/test.txt", "text")
    assert result.code != 0
    client.upload_bytes.assert_not_called()
    events = await emit_result(ToolOutput(success=False, error=result.message))
    assert events[0]["event_type"] == "chat.tool_result"
    assert events[0]["success"] is False
    assert events[0]["is_error"] is True
    assert events[1]["event_type"] == "chat.error"
    assert events[1]["code"] == "SANDBOX_DELETION_FAILED"
    assert "old" in events[1]["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [
    ToolOutput(success=False, error="file does not exist"),
    ToolOutput(success=True, data="[SANDBOX_DELETION_FAILED] quoted file content"),
    {"success": False, "data": "[SANDBOX_DELETION_FAILED]", "error": "ordinary error"},
    "assistant says [SANDBOX_DELETION_FAILED]",
])
async def test_ordinary_errors_and_content_do_not_escalate(result):
    assert not any(event["event_type"] == "chat.error" for event in await emit_result(result))


@pytest.mark.asyncio
async def test_exception_path_reports_deletion_marker():
    events = await emit_result(None, RuntimeError("[SANDBOX_DELETION_PENDING] cleanup in progress"))
    assert events[-1]["code"] == "SANDBOX_DELETION_PENDING"


@pytest.mark.asyncio
@pytest.mark.parametrize("as_dict", [False, True])
@pytest.mark.parametrize("code, final_code", [
    ("SANDBOX_DELETION_PENDING", "SANDBOX_DELETION_PENDING"),
    ("SANDBOX_DELETION_FAILED", "SANDBOX_DELETION_FAILED"),
    ("SANDBOX_DELETION_PENDING", "SANDBOX_DELETION_FAILED"),
    (None, None),
])
async def test_request_deduplicates_deletion_errors_and_finishes_with_wire_status(
    monkeypatch, as_dict, code, final_code,
):
    result = ToolOutput(
        success=False, error=f"[{code}] cleanup blocked" if code else "file does not exist",
    )
    tool_events = await emit_result(result)

    class FakeAdapter:
        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs):
            events = tool_events * 2 + [
                {"event_type": "chat.final", "content": "Operation failed"},
            ]
            for index, event in enumerate(events):
                payload = dict(event)
                if index >= len(tool_events) and payload.get("event_type") == "chat.error":
                    payload.update(code=final_code, error=f"[{final_code}] cleanup blocked")
                yield dict(payload) if as_dict else AgentResponseChunk(
                    request_id="req", channel_id="desktop",
                    payload=dict(payload), is_complete=False,
                )
            if code and not as_dict and tool_events:
                yield AgentResponseChunk(
                    request_id="req", channel_id="desktop",
                    payload={"is_complete": True}, is_complete=True,
                )

    monkeypatch.setattr(
        interface_module.JiuWenSwarm, "_ensure_adapter", lambda *_a, **_k: FakeAdapter(),
    )
    monkeypatch.setattr(interface_module, "get_config", lambda: {"memory": {"mode": "disabled"}})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface_module, "append_history_record", lambda **_record: None)
    monkeypatch.setattr(
        interface_module, "_schedule_symphony_session_feedback", lambda *_args: None,
    )
    request = AgentRequest(
        request_id="req", channel_id="desktop", session_id="deletion-session",
        params={"query": "write file", "mode": "agent"}, is_stream=True,
    )
    swarm = interface_module.JiuWenSwarm()
    for _ in range(2):  # A previous request must not suppress the next request's error.
        chunks = [chunk async for chunk in swarm.process_message_stream(request)]
        assert sum(chunk.is_complete for chunk in chunks) == 1
        assert sum(chunk.payload.get("event_type") == "chat.tool_result" for chunk in chunks) == 2
        errors = [chunk for chunk in chunks if chunk.payload.get("event_type") == "chat.error"]
        assert len(errors) == (2 if code else 0)  # One notification plus the terminal frame.
        wire = e2a_response_from_agent_chunk(chunks[-1], response_id="req", sequence=len(chunks))
        assert wire.status == ("failed" if code else "succeeded")
        if code:
            assert chunks[-1].payload["code"] == final_code
    tool_events.clear()
    recovered = [chunk async for chunk in swarm.process_message_stream(request)]
    wire = e2a_response_from_agent_chunk(recovered[-1], response_id="req", sequence=len(recovered))
    assert wire.status == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("source, previous_code, code", [
    ("core", None, "SANDBOX_DELETION_PENDING"),
    ("core", None, "SANDBOX_DELETION_FAILED"),
    ("wrapped", None, "SANDBOX_DELETION_PENDING"),
    ("wrapped", None, "SANDBOX_DELETION_FAILED"),
    ("core", "SANDBOX_DELETION_PENDING", "SANDBOX_DELETION_FAILED"),
    ("core", "SANDBOX_DELETION_FAILED", "SANDBOX_DELETION_PENDING"),
    ("marker", None, "SANDBOX_DELETION_FAILED"),
    ("code", None, "SANDBOX_DELETION_PENDING"),
    ("ordinary", None, None),
])
async def test_producer_exception_sets_terminal_status_and_deduplicates_both_ingresses(
    monkeypatch, source, previous_code, code,
):
    # pylint: disable=protected-access
    previous_events = await emit_result(ToolOutput(
        success=False, error=f"[{previous_code}] cleanup blocked",
    )) if previous_code else []
    error = RuntimeError("ordinary producer failure")
    if source in {"core", "wrapped"}:
        # Raise the real Core deletion barrier; its framework code differs
        # from the deletion marker carried by message/details.
        provider = box._JiuwenBoxProviderMixin
        key = "http://localhost:8321|scope"
        monkeypatch.setattr(provider, "_pending_deletions", {(key, "old"): (True, None)})
        monkeypatch.setattr(provider, "_delete_tasks", {})
        monkeypatch.setattr(provider, "_bulk_deletions", int(code == "SANDBOX_DELETION_PENDING"))
        with pytest.raises(RuntimeError) as caught:
            provider.ensure_not_deleting(key)
        error = caught.value
        assert error.code != code
        if source == "wrapped":
            wrapper = RuntimeError("adapter failed")
            wrapper.__cause__ = error
            error = wrapper
    elif source == "marker":
        error = RuntimeError(f"[{code}] cleanup blocked")
    elif source == "code":
        error.code = code

    class RaisingAdapter:
        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs):
            for event in previous_events:
                yield AgentResponseChunk(
                    request_id="req", channel_id="desktop", payload=event, is_complete=False,
                )
            raise error

    monkeypatch.setattr(
        interface_module.JiuWenSwarm, "_ensure_adapter", lambda *_a, **_k: RaisingAdapter(),
    )
    monkeypatch.setattr(interface_module, "get_config", lambda: {"memory": {"mode": "disabled"}})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface_module, "append_history_record", lambda **_record: None)
    monkeypatch.setattr(interface_module, "_schedule_symphony_session_feedback", lambda *_args: None)
    request = AgentRequest(
        request_id="req", channel_id="desktop", session_id="producer-deletion-session",
        params={"query": "write file", "mode": "agent"}, is_stream=True,
    )
    chunks = [chunk async for chunk in interface_module.JiuWenSwarm().process_message_stream(request)]

    assert sum(chunk.is_complete for chunk in chunks) == 1
    notifications = [
        chunk for chunk in chunks
        if not chunk.is_complete and chunk.payload.get("event_type") == "chat.error"
    ]
    assert len(notifications) == 1
    wire = e2a_response_from_agent_chunk(chunks[-1], response_id="req", sequence=len(chunks))
    assert wire.status == ("failed" if code else "succeeded")
    if code:
        expected = "SANDBOX_DELETION_FAILED" if "SANDBOX_DELETION_FAILED" in (previous_code, code) else code
        assert chunks[-1].payload["code"] == expected
        assert chunks[-1].payload["event_type"] == "chat.error"
    if source == "wrapped":
        assert code in chunks[-1].payload["message"]
