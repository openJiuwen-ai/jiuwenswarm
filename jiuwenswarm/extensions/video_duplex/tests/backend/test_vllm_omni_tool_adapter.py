"""Behavioral contracts for managed duplex calls and backend result admission."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.runtime.tool_calls import ToolCall, ToolCallCoordinator, ToolEvent, ToolExecutionContext, ToolResult
from jiuwenswarm.extensions.video_duplex.backend.tool_adapters.vllm_omni import VllmOmniConfig, VllmOmniToolAdapter


def adapter():
    sent = []

    async def send(event):
        sent.append(event)

    instance = VllmOmniToolAdapter(send, generation="generation")
    instance.adopt({"id": "backend", "capabilities": {
        "tool_mode": "declared", "max_tools": 2,
        "function_call_output_continuation": "client_response_create",
    }}, tool_count=2)
    instance.ready = True
    return instance, sent


def call():
    return ToolCall("backend", "generation", "call-1", "jiuwen_delegate", {"task": "calculate"})


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.parametrize("mode,continuation", [
    ("none", "none"), ("delegate_only", "next_observation"), ("declared", "unknown"),
])
def test_rejects_unsupported_capabilities(mode, continuation):
    instance, _ = adapter()
    with pytest.raises(ValueError):
        instance.adopt({"id": "backend", "capabilities": {
            "tool_mode": mode, "function_call_output_continuation": continuation, "max_tools": 2,
        }}, tool_count=2)


def test_config_forces_explicit_handshake_and_no_credential_url():
    config = VllmOmniConfig("ws://localhost:1234/v1/realtime?autostart=1", "model")
    assert "autostart=0" in config.endpoint()
    with pytest.raises(ValueError):
        VllmOmniConfig("ws://user:secret@localhost", "model").endpoint()


def test_backend_identity_is_namespaced_across_reconnections():
    original = call()
    reconnected = ToolCall("other-backend", "next-generation", original.call_id, original.name, original.arguments)
    assert original.command_id != reconnected.command_id
    assert len(original.command_id) < 200


@pytest.mark.parametrize("arguments", ['[]', 'null', '{"value":NaN}', '{broken'])
def test_invalid_arguments_never_become_calls(arguments):
    instance, _ = adapter()
    with pytest.raises(ValueError):
        instance.normalize({"type": "response.function_call_arguments.done", "call_id": "a",
                            "name": "jiuwen_delegate", "arguments": arguments})


async def test_accepted_result_still_continues_after_original_response_finishes():
    instance, sent = adapter()
    instance.normalize({"type": "response.created", "response": {"id": "r1"}})
    task = asyncio.create_task(instance.submit_result(call(), ToolResult(True, "42")))
    await until(lambda: sent)
    assert not task.done()
    instance.normalize({"type": "conversation.item.created", "created": True,
                        "item": {"id": sent[0]["item"]["id"], "type": "function_call_output", "call_id": "call-1"}})
    assert await task is True
    await instance.continue_after_result(call())
    assert not any(event["type"] == "response.create" for event in sent)
    instance.normalize({"type": "response.done", "response": {"id": "r1"}})
    await instance.flush_continuations()
    await instance.flush_continuations()
    assert [event["type"] for event in sent] == ["conversation.item.create", "response.create"]


async def test_rejected_result_does_not_continue():
    instance, sent = adapter()
    task = asyncio.create_task(instance.submit_result(call(), ToolResult(True, "42")))
    await until(lambda: sent)
    instance.normalize({"type": "error", "error": {"code": "invalid_function_call_output",
                        "event_id": sent[0]["event_id"]}})
    assert await task is False
    await instance.flush_continuations()
    assert len(sent) == 1


@pytest.mark.parametrize("kind", ["conversation.item.added", "conversation.item.created", "conversation.item.done"])
async def test_ga_committed_output_projection_acknowledges_exact_submission(kind):
    instance, sent = adapter()
    task = asyncio.create_task(instance.submit_result(call(), ToolResult(True, "42")))
    await until(lambda: sent)
    item = sent[0]["item"]
    instance.normalize({"type": kind, "item": {**item, "id": "other-output"}})
    assert not task.done()
    instance.normalize({"type": kind, "created": False, "item": item})
    assert not task.done()
    instance.normalize({"type": kind, "item": item})
    assert await task is True
    await instance.continue_after_result(call())
    assert [event["type"] for event in sent] == ["conversation.item.create", "response.create"]


async def test_automatic_backend_never_receives_response_create():
    instance, sent = adapter()
    instance.capabilities["function_call_output_continuation"] = "automatic"
    await instance.continue_after_result(call())
    assert sent == []


async def test_continuation_waits_for_authoritative_playback_drain_after_response_done():
    instance, sent = adapter()
    instance.normalize({"type": "response.created", "response": {"id": "r"}})
    instance.normalize({"type": "response.output_audio.delta", "response_id": "r", "delta": "AA=="})
    instance.normalize({"type": "response.done", "response": {"id": "r"}})
    instance.accepted.add(call().call_id)
    await instance.continue_after_result(call())
    assert sent == []
    instance.normalize({"type": "playback.acknowledged", "event": {
        "item_id": "item_r", "playback": {"sent_ms": 100, "committed_ms": 50},
    }})
    await instance.flush_continuations()
    assert sent == []
    instance.normalize({"type": "playback.acknowledged", "event": {
        "item_id": "item_r", "playback": {"sent_ms": 100, "committed_ms": 100},
    }})
    await instance.flush_continuations()
    assert sent == [{"type": "response.create"}]


async def test_settled_discards_pending_output_and_continuation():
    instance, sent = adapter()
    task = asyncio.create_task(instance.submit_result(call(), ToolResult(True, "42")))
    await until(lambda: sent)
    instance.normalize({"type": "duplex.function_call.settled", "event": {"call_id": "call-1"}})
    assert await task is False
    await instance.continue_after_result(call())
    assert len(sent) == 1


async def test_duplicate_completion_events_execute_once_and_preserve_terminal_tombstone():
    instance, sent = adapter()
    execution = SimpleNamespace(wait=AsyncMock(return_value=ToolResult(True, "done")), cancel=AsyncMock())
    executor = SimpleNamespace(start=AsyncMock(return_value=execution))
    coordinator = ToolCallCoordinator(instance, executor, ToolExecutionContext("owner", "scope"))
    event = {"type": "response.function_call_arguments.done", "call_id": "call-1",
             "name": "jiuwen_delegate", "arguments": '{"task":"calculate"}'}
    try:
        await coordinator.handle(instance.normalize(event)[0])
        await coordinator.handle(instance.normalize({"type": "response.output_item.done", "item": {
            "type": "function_call", "call_id": "call-1", "name": "jiuwen_delegate",
            "arguments": '{"task":"calculate"}',
        }})[0])
        await until(lambda: sent)
        instance.normalize({"type": "conversation.item.created", "created": True,
                            "item": {"id": sent[0]["item"]["id"], "type": "function_call_output", "call_id": "call-1"}})
        await until(lambda: coordinator.entries["call-1"].task.done())
        await coordinator.handle(instance.normalize(event)[0])
        assert executor.start.await_count == 1
        assert any(item["type"] == "response.create" for item in sent)
    finally:
        await coordinator.close()


def test_standard_arguments_done_without_name_waits_for_complete_output_item():
    instance, _ = adapter()
    assert instance.normalize({"type": "response.function_call_arguments.done", "call_id": "standard",
                               "arguments": '{"task":"work"}', "item_id": "item"}) == ()
    events = instance.normalize({"type": "response.output_item.done", "item": {
        "type": "function_call", "id": "item", "call_id": "standard", "name": "jiuwen_delegate",
        "arguments": '{"task":"work"}',
    }})
    assert len(events) == 1
    assert events[0].call.name == "jiuwen_delegate"
    assert events[0].call.arguments == {"task": "work"}


async def test_timeout_cancels_execution_without_publishing_error_text():
    instance, sent = adapter()
    execution = SimpleNamespace(wait=AsyncMock(side_effect=lambda: None), cancel=AsyncMock())

    async def wait():
        await asyncio.Future()

    execution.wait = wait
    coordinator = ToolCallCoordinator(instance, SimpleNamespace(start=AsyncMock(return_value=execution)),
                                      ToolExecutionContext("owner", "scope"), timeout_s=0.02)
    await coordinator.handle(ToolEvent("call", call().call_id, call()))
    await until(lambda: coordinator.entries["call-1"].task.done())
    assert sent == [{"type": "function_call.cancel", "call_id": "call-1", "reason": "failed"}]
    assert execution.cancel.await_count == 1
    await coordinator.close()


async def test_second_inflight_call_is_refused_without_starting_execution():
    instance, sent = adapter()

    async def wait():
        await asyncio.Future()

    execution = SimpleNamespace(wait=wait, cancel=AsyncMock())
    executor = SimpleNamespace(start=AsyncMock(return_value=execution))
    coordinator = ToolCallCoordinator(instance, executor, ToolExecutionContext("owner", "scope"))
    first = call()
    second = ToolCall(first.backend_session_id, first.generation, "call-2", first.name, first.arguments)
    try:
        await coordinator.handle(ToolEvent("call", first.call_id, first))
        await until(lambda: executor.start.await_count == 1)
        await coordinator.handle(ToolEvent("call", second.call_id, second))
        assert executor.start.await_count == 1
        assert sent[-1] == {"type": "function_call.cancel", "call_id": "call-2", "reason": "failed"}
    finally:
        await coordinator.close()


async def test_settlement_then_close_waits_for_native_cleanup_without_second_cancel():
    instance, _ = adapter()
    cleaning = asyncio.Event()
    release = asyncio.Event()

    async def wait():
        await asyncio.Future()

    async def cancel(reason):
        cleaning.set()
        await release.wait()

    execution = SimpleNamespace(wait=wait, cancel=AsyncMock(side_effect=cancel))
    executor = SimpleNamespace(start=AsyncMock(return_value=execution))
    coordinator = ToolCallCoordinator(instance, executor, ToolExecutionContext("owner", "scope"))
    await coordinator.handle(ToolEvent("call", call().call_id, call()))
    await until(lambda: executor.start.await_count == 1)
    await coordinator.handle(ToolEvent("settled", call().call_id))
    await cleaning.wait()
    closing = asyncio.create_task(coordinator.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await closing
    execution.cancel.assert_awaited_once()


async def test_unsolicited_settlement_tombstones_are_bounded():
    instance, _ = adapter()
    coordinator = ToolCallCoordinator(instance, SimpleNamespace(start=AsyncMock()),
                                      ToolExecutionContext("owner", "scope"), max_calls=1)
    await coordinator.handle(ToolEvent("settled", "first"))
    await coordinator.handle(ToolEvent("settled", "first"))
    with pytest.raises(ValueError, match="settlement budget"):
        await coordinator.handle(ToolEvent("settled", "second"))
    await coordinator.close()


async def test_queue_manager_receives_namespaced_id_and_cannot_answer_approvals():
    from jiuwenswarm.extensions.video_duplex.backend.duplex_task_executor import JiuwenTaskExecutor

    manager = SimpleNamespace(client=None, manage_queue_call=AsyncMock(return_value={"state": "complete"}))
    incoming = ToolCall("backend", "generation", "same-id", "jiuwen_queue_manage", {"instruction": "list tasks"})
    execution = await JiuwenTaskExecutor(manager).start(incoming, ToolExecutionContext("owner", "scope"))
    assert (await execution.wait()).success
    args, kwargs = manager.manage_queue_call.call_args
    assert args[0:2] == ("owner", "scope")
    assert args[2].call_id == incoming.command_id
    assert kwargs["allow_answers"] is False


async def test_incomplete_queue_management_does_not_publish_diagnostics_as_success():
    from jiuwenswarm.extensions.video_duplex.backend.duplex_task_executor import JiuwenTaskExecutor

    manager = SimpleNamespace(client=None, manage_queue_call=AsyncMock(return_value={
        "state": "incomplete", "error": "private diagnostic", "receipts": [{"state": "committed"}],
    }))
    incoming = ToolCall("backend", "generation", "queue", "jiuwen_queue_manage", {"instruction": "list tasks"})
    execution = await JiuwenTaskExecutor(manager).start(incoming, ToolExecutionContext("owner", "scope"))
    result = await execution.wait()
    assert result == ToolResult(False)
    await execution.cancel("closed")


@pytest.mark.parametrize("name,arguments", [
    ("jiuwen_delegate", {"task": "work"}), ("jiuwen_queue_manage", {"instruction": "list tasks"}),
])
async def test_archived_conversation_cannot_start_a_tool(monkeypatch, name, arguments):
    from jiuwenswarm.extensions.video_duplex.backend.duplex_task_executor import JiuwenTaskExecutor

    monkeypatch.setattr(
        "jiuwenswarm.extensions.video_duplex.backend.task_adapter.task_agent_query",
        AsyncMock(return_value={"user_id": "owner", "archived": True}),
    )
    manager = SimpleNamespace(client=object(), submit_task=AsyncMock(), manage_queue_call=AsyncMock())
    incoming = ToolCall("backend", "generation", "archived", name, arguments)
    execution = await JiuwenTaskExecutor(manager).start(incoming, ToolExecutionContext("owner", "task-duplex:saved"))
    with pytest.raises(ValueError, match="unavailable"):
        await execution.wait()
    manager.submit_task.assert_not_called()
    manager.manage_queue_call.assert_not_called()


async def test_unregistered_tool_is_rejected_before_any_business_execution():
    from jiuwenswarm.extensions.video_duplex.backend.duplex_task_executor import JiuwenTaskExecutor

    with pytest.raises(ValueError, match="not registered"):
        await JiuwenTaskExecutor(SimpleNamespace()).start(
            ToolCall("backend", "g", "a", "jiuwen_task_answer", {}), ToolExecutionContext("owner", "scope"),
        )


async def test_delegate_captures_trusted_frame_and_preserves_login_credential_binding():
    from jiuwenswarm.extensions.video_duplex.backend.duplex_task_executor import JiuwenTaskExecutor

    frame = ["data:image/jpeg;base64,AA=="]
    completed = {"status": "completed", "result": {"answer": "17"}}
    manager = SimpleNamespace(
        client=None, submit_task=Mock(return_value={"id": "job"}),
        service=SimpleNamespace(get=Mock(return_value=completed)), public=lambda task: task,
    )
    lookup = AsyncMock(return_value="trusted-credential-ref")
    executor = JiuwenTaskExecutor(manager, frame_lookup=lambda _call: frame[0], credential_resolver=lookup)
    execution = await executor.start(call(), ToolExecutionContext("owner", "scope"))
    frame[0] = "data:image/jpeg;base64,AQ=="
    assert (await execution.wait()).success
    assert manager.submit_task.call_args.kwargs["frame_data_url"] == "data:image/jpeg;base64,AA=="
    assert manager.submit_task.call_args.kwargs["credential_ref"] == "trusted-credential-ref"
    lookup.assert_awaited_once_with({"mode": "agent", "work_mode": "work"})


def test_turn_backend_receives_images_as_replaceable_conversation_items():
    instance, _ = adapter()
    instance.capabilities["supports_image_input"] = True
    media = {"type": "input_audio_buffer.append", "audio": "AA==", "video_frames": ["data:image/jpeg;base64,AA=="]}
    first = instance.media_commands(media)
    second = instance.media_commands(media)
    assert first[0]["item"]["content"][0]["type"] == "input_image"
    assert "video_frames" not in first[-1]
    assert second[0] == {"type": "conversation.item.delete", "item_id": first[0]["item"]["id"]}


@pytest.mark.parametrize("frame", ["http://localhost/private", "data:text/html;base64,AA==",
                                  "data:image/jpeg;base64,broken!", "data:image/jpeg;base64,"])
def test_camera_carrier_never_allows_remote_url_fetches(frame):
    instance, _ = adapter()
    instance.capabilities["supports_image_input"] = True
    with pytest.raises(ValueError):
        instance.media_commands({"type": "input_audio_buffer.append", "audio": "", "video_frames": [frame]})


async def test_settlement_before_completion_projection_does_not_execute():
    instance, _ = adapter()
    instance.normalize({"type": "duplex.function_call.settled", "event": {"call_id": "late"}})
    assert instance.normalize({"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": "late", "name": "jiuwen_delegate", "arguments": '{}',
    }}) == ()
