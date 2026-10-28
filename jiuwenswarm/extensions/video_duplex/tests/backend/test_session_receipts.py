"""Session-wide voice receipts use transport identity, never model-written IDs."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.events import RuntimeEvent
from jiuwenswarm.extensions.video_duplex.backend import session_receipts as receipts
from jiuwenswarm.extensions.video_duplex.backend.video_search import present_core_agent_result
from jiuwenswarm.extensions.video_duplex.backend.core_receipts import receipt_payload


@pytest.fixture(autouse=True)
def clear_receipts():
    receipts._subscriptions.clear()
    yield
    receipts._subscriptions.clear()


def request(sid="chat", owner="alice", **params):
    return AgentRequest(request_id="request-1", session_id=sid, user_id=owner,
                        channel_id="web", req_method=ReqMethod.CHAT_SEND, params=params)


def activate(sid="chat", token="token"):
    receipts._subscriptions[sid] = receipts.ReceiptSubscription("alice", token, time.monotonic() + 180)


@pytest.mark.parametrize("legacy", [False, True])
def test_summary_is_taken_from_current_answer_without_matching_task_id(legacy):
    tag = ":previous-task" if legacy else ""
    output = "Video is still running.\n" + f"[[JIUWEN_BRIEF_BEGIN{tag}]]Still generating; no file yet.[[JIUWEN_BRIEF_END{tag}]]"
    result = present_core_agent_result(output, nonce="current-task", question="video")
    assert result["display_result"] == "Video is still running."
    assert result["realtime_brief"]["source"] == "core_agent"
    assert result["realtime_brief"]["summary"] == "Still generating; no file yet."


@pytest.mark.parametrize("suffix,source", [("Still generating; no file yet.", "core_agent"),
    ("First line\nSecond line", "derived")])
def test_final_reply_with_missing_summary_end_marker_uses_the_same_summary_validation(suffix, source):
    result = receipt_payload({"answer": "Video is still running.\n[[JIUWEN_BRIEF_BEGIN]]\n" + suffix})
    assert result["display_result"] == "Video is still running."
    assert result["realtime_brief"]["source"] == source
    assert "JIUWEN_BRIEF" not in result["realtime_brief"]["summary"]


@pytest.mark.asyncio
async def test_subscription_validates_owner_and_old_stop_cannot_stop_new_run(monkeypatch):
    monkeypatch.setattr("jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
                        lambda *args, **kwargs: {"user_id": "alice"})
    await receipts.configure(request(action="start", token="old"))
    await receipts.configure(request(action="start", token="new"))
    await receipts.configure(request(action="stop", token="old"))
    assert receipts.subscription("chat").token == "new"
    with pytest.raises(ValueError, match="replaced"):
        await receipts.configure(request(action="renew", token="old"))
    with pytest.raises(ValueError, match="unavailable"):
        await receipts.configure(request(owner="bob", action="start", token="attack"))
    await receipts.configure(request(action="stop", token="new"))
    assert receipts.subscription("chat") is None


@pytest.mark.asyncio
async def test_new_conversation_replaces_previous_voice_session(monkeypatch):
    monkeypatch.setattr("jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
                        lambda *args, **kwargs: {"user_id": "alice"})
    await receipts.configure(request(action="start", token="one"))
    await receipts.configure(request(sid="other", action="start", token="two"))
    assert receipts.subscription("chat") is None
    assert receipts.subscription("other").token == "two"


@pytest.mark.parametrize("mode", ["agent.work.normal", "agent.code.normal", "team.design"])
def test_runtime_policy_survives_native_answer_without_overwriting_answer(mode):
    activate()
    req = request(mode=mode, answers=[{"question": "Which?", "custom_input": "blue"}])
    receipts.prepare_request(req)
    assert req.params["answers"][0]["custom_input"] == "blue"
    builder = Mock()
    receipts.apply_summary_policy(builder, "chat")
    section = builder.add_section.call_args.args[0]
    assert "[[JIUWEN_BRIEF_BEGIN]]" in str(section.content)
    assert "pending, partial, blocked and failed" in str(section.content)
    receipts._subscriptions.clear()
    builder.reset_mock()
    receipts.apply_summary_policy(builder, "chat")
    builder.remove_section.assert_called_once_with("video_duplex.voice_receipt")
    builder.add_section.assert_not_called()


@pytest.mark.parametrize("mode", ["agent.work.normal", "agent.code.normal", "team.design"])
def test_active_session_uses_model_policy_without_rewriting_user_or_tool_input(mode):
    activate()
    for channel in ("web", "video_tool"):
        req = request(mode=mode, query="Keep the scene blue", content="Keep the scene blue")
        req.channel_id = channel
        receipts.prepare_request(req)
        receipts.prepare_request(req)
        assert req.params["query"] == req.params["content"] == "Keep the scene blue"
        assert req.params["duplex_original_question"] == "Keep the scene blue"


def test_standalone_tool_keeps_protocol_once_and_ordinary_chat_stays_unchanged():
    req = request(query="Render the scene")
    receipts.prepare_request(req)
    assert req.params == {"query": "Render the scene"}
    req.channel_id = "video_tool"
    receipts.prepare_request(req)
    receipts.prepare_request(req)
    assert req.params["query"].count("Voice receipt protocol") == 1
    assert req.params["query"] == req.params["content"]


@pytest.mark.parametrize("mode", ["agent.work.normal", "agent.code.normal", "team.design"])
def test_stored_and_native_results_share_summary_and_missing_summary_handling(mode):
    answer = "Video is still running.\n[[JIUWEN_BRIEF_BEGIN]]No final file yet.[[JIUWEN_BRIEF_END]]"
    native = receipt_payload({"answer": answer}, question=mode)
    stored = receipt_payload({"answer": native["display_result"], **native}, question=mode)
    assert native == stored
    assert native["realtime_brief"]["summary"] == "No final file yet."
    unmarked = receipt_payload({"answer": "Rendering remains blocked."}, question=mode)
    assert unmarked["realtime_brief"]["summary"] == "Rendering remains blocked."
    assert unmarked["realtime_brief"]["source"] == "derived"
    assert receipt_payload({}) == {"result": "", "display_result": "", "realtime_brief": None}


def test_task_projection_repairs_legacy_results_before_frontend_delivery():
    from jiuwenswarm.extensions.video_duplex.backend.task_adapter import VideoSearchManager

    task = dict(id="task", session="chat", instruction="Generate video", request={},
                result={"answer": "Generation is blocked."}, progress=[], status="completed",
                revision=1, sequence=1, error="", changes=[], successor_id=None, parent_id=None)
    public = VideoSearchManager.public(task)
    assert public["result"] == public["display_result"] == "Generation is blocked."
    assert public["realtime_brief"]["summary"] == "Generation is blocked."
    assert public["realtime_brief"]["source"] == "derived"


@pytest.mark.asyncio
async def test_text_final_is_delivered_once_as_receipt_without_changing_chat(monkeypatch):
    activate()
    push = AsyncMock(return_value=True)
    monkeypatch.setattr("jiuwenswarm.runtime.host_services.send_runtime_push", push)
    req = request(query="Write code")
    receipts.prepare_request(req)
    assert req.params["query"] == "Write code"
    assert req.params["duplex_original_question"] == "Write code"
    event = RuntimeEvent(request_id=req.request_id, session_id="chat", channel_id="web",
        payload={"event_type": "chat.final", "content": "Code is ready.\n[[JIUWEN_BRIEF_BEGIN]]Code tested.[[JIUWEN_BRIEF_END]]"})
    original = dict(event.payload)
    await receipts.publish_result(req, event)
    await receipts.publish_result(req, event)
    assert event.payload == original
    assert push.await_count == 1
    payload = push.call_args.args[0]["payload"]
    assert payload["event_type"] == "video.core.result"
    assert payload["subscription_token"] == "token"
    assert payload["realtime_brief"]["summary"] == "Code tested."
    assert payload["display_result"] == "Code is ready."


@pytest.mark.asyncio
async def test_delegated_result_keeps_existing_delivery_and_page_observer_does_not_duplicate(monkeypatch):
    activate()
    push = AsyncMock(return_value=True)
    monkeypatch.setattr("jiuwenswarm.runtime.host_services.send_runtime_push", push)
    req = request(managed_task_binding={"task_id": "task"})
    receipts.prepare_request(req)
    event = RuntimeEvent(request_id="task-request", session_id="chat", channel_id="video_tool",
                         payload={"event_type": "chat.final", "content": "Done"})
    observer = request(attach_only=True)
    await receipts.publish_result(observer, event)
    await receipts.publish_result(req, event)
    receipts.finish_request(req)
    await receipts.publish_result(observer, event)
    push.assert_not_awaited()


@pytest.mark.asyncio
async def test_design_text_stream_becomes_observer_after_its_reply_and_does_not_repeat_voice_tasks(monkeypatch):
    activate()
    push = AsyncMock(return_value=True)
    monkeypatch.setattr("jiuwenswarm.runtime.host_services.send_runtime_push", push)
    text = request(mode="team.design", query="First question")
    first = RuntimeEvent(request_id=text.request_id, session_id="chat", channel_id="web",
                         payload={"event_type": "chat.final", "content": "First answer"})
    await receipts.publish_result(text, first)
    managed = request(mode="team.design", managed_task_binding={"task_id": "task"})
    managed.request_id = "voice-request"
    receipts.prepare_request(managed)
    voice = RuntimeEvent(request_id=text.request_id, session_id="chat", channel_id="web",
                         payload={"event_type": "chat.final", "content": "Voice task answer"})
    await receipts.publish_result(text, voice)
    await receipts.publish_result(managed, voice)
    receipts.finish_request(managed)
    await receipts.publish_result(text, voice)
    assert push.await_count == 1
    # An independent subsequent text request may legitimately have the same answer.
    next_text = request(mode="team.design", query="Another question")
    next_text.request_id = "next-text"
    voice.request_id = next_text.request_id
    await receipts.publish_result(next_text, voice)
    assert push.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,role,sid", [("chat.delta", "leader", "chat"),
    ("chat.final", "member", "chat"), ("chat.final", "leader", "other")])
async def test_progress_workers_and_other_sessions_are_not_announced(monkeypatch, kind, role, sid):
    activate()
    push = AsyncMock()
    monkeypatch.setattr("jiuwenswarm.runtime.host_services.send_runtime_push", push)
    event = RuntimeEvent(request_id="r", session_id=sid, channel_id="web",
                         payload={"event_type": kind, "content": "text", "role": role})
    await receipts.publish_result(request(sid=sid), event)
    push.assert_not_awaited()


def test_lost_client_lease_removes_model_policy():
    activate()
    receipts._subscriptions["chat"].expires = time.monotonic() - 1
    assert receipts.subscription("chat") is None



def test_web_channel_preserves_structured_core_receipt():
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel

    payload = {"event_type": "video.core.result", "subscription_token": "current",
               "session_id": "chat", "realtime_brief": {"summary": "Still running"}}
    message = SimpleNamespace(payload=payload, session_id="chat", id="request", metadata={}, ok=True)
    result = WebChannel._build_event_payload(message, "video.core.result")
    assert result["subscription_token"] == "current"
    assert result["realtime_brief"]["summary"] == "Still running"



@pytest.mark.asyncio
async def test_internal_background_and_unary_core_answers_are_forwarded(monkeypatch):
    activate()
    push = AsyncMock(return_value=True)
    monkeypatch.setattr("jiuwenswarm.runtime.host_services.send_runtime_push", push)
    req = request(owner=None, query="Background result")
    event = RuntimeEvent(request_id="background-final", session_id="chat", channel_id="web",
                         payload={"content": "Ready"}, is_complete=True)
    await receipts.publish_result(req, event)
    push.assert_awaited_once()
