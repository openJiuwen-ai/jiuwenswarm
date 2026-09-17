"""MessageHandler unit tests."""

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema import Message
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler


_APPROVAL_SCHEMA = "openjiuwen.skill_evolution_approval.v1"
_APPROVAL_SOURCE = "skill_evolution_approval"
_INTERRUPT_APPROVAL_META = {
    "event_kind": "approval",
    "rail_kind": "regular",
    "approval_kind": "evolve",
    "approval_transport": "interrupt",
}


class _FakeAgentClient:
    sent_requests: list[object] = []
    sent_stream_requests: list[object] = []
    stream_payloads: list[dict[str, object]] = []
    response_payload: dict[str, object] = {
        "event_type": "chat.interrupt_result",
        "message": "当前没有可取消的团队任务",
        "success": False,
    }

    @staticmethod
    async def send_request(env: object) -> SimpleNamespace:
        _FakeAgentClient.sent_requests.append(env)
        return SimpleNamespace(
            request_id="interrupt-1",
            channel_id="feishu_enterprise",
            ok=True,
            payload=dict(_FakeAgentClient.response_payload),
            metadata=None,
        )

    @staticmethod
    async def send_request_stream(env: object) -> AsyncIterator[object]:
        _FakeAgentClient.sent_stream_requests.append(env)
        for index, payload in enumerate(_FakeAgentClient.stream_payloads):
            yield SimpleNamespace(
                request_id=getattr(env, "request_id", "") or f"stream-{index}",
                channel_id=getattr(env, "channel", "") or "web",
                payload=payload,
                is_complete=False,
            )


class _TestMessageHandler(MessageHandler):
    @classmethod
    def create(cls) -> "_TestMessageHandler":
        setattr(MessageHandler, "_instance", None)
        setattr(cls, "_instance", None)
        _FakeAgentClient.sent_requests = []
        _FakeAgentClient.sent_stream_requests = []
        _FakeAgentClient.stream_payloads = []
        return cls(_FakeAgentClient())

    def seed_pending_evolution_approval(
        self,
        session_id: str,
        request_id: str,
    ) -> None:
        coordinator = getattr(self, "_evolution_approval")
        coordinator.mark_pending(session_id, request_id)

    def seed_session_evolution_in_progress(self, session_id: str) -> None:
        coordinator = getattr(self, "_evolution_approval")
        coordinator.mark_session_in_progress(session_id)

    def seed_queued_supplement_input(
        self,
        session_id: str,
        payload: dict[str, object],
    ) -> None:
        coordinator = getattr(self, "_evolution_approval")
        coordinator.queue_supplement(
            session_id,
            str(payload.get("new_input") or ""),
            payload.get("attachments") if isinstance(payload.get("attachments"), list) else None,
        )

    async def handle_evolution_chunk(
        self,
        chunk: SimpleNamespace,
        session_id: str,
        request_metadata: dict[str, object] | None = None,
    ) -> None:
        handler = getattr(self, "_handle_evolution_chunk")
        await handler(chunk, session_id, request_metadata)

    async def handle_agent_server_push(self, wire: dict[str, object]) -> None:
        await self._handle_agent_server_push(wire)

    async def complete_evolution_approval_if_current(
        self,
        msg: Message,
        answered_request_id: str,
    ) -> None:
        completer = getattr(self, "_complete_evolution_approval_if_current")
        await completer(msg, answered_request_id)

    def pending_evolution_approval(self, session_id: str) -> str | None:
        coordinator = getattr(self, "_evolution_approval")
        return coordinator.pending_request_id(session_id)

    def deferred_evolution_approvals(self, session_id: str) -> list[str]:
        coordinator = getattr(self, "_evolution_approval")
        return coordinator.deferred_request_ids(session_id)

    def has_session_evolution_in_progress(self, session_id: str) -> bool:
        coordinator = getattr(self, "_evolution_approval")
        return coordinator.is_session_in_progress(session_id)

    def queued_supplement_input(self, session_id: str) -> dict[str, object] | None:
        coordinator = getattr(self, "_evolution_approval")
        return coordinator.queued_supplement(session_id)

    def pop_user_message_nowait(self):
        user_messages = getattr(self, "_user_messages")
        return user_messages.get_nowait()

    def user_message_queue_empty(self) -> bool:
        user_messages = getattr(self, "_user_messages")
        return user_messages.empty()

    def should_emit_processing_status_for_stream(self, msg: Message) -> bool:
        return self._should_emit_processing_status_for_stream(msg)

    async def cancel_agent_work_for_session(
        self,
        msg: Message,
        old_sid: str | None,
        *,
        publish_interrupt_result: bool = True,
        agent_notify: str = "await",
    ) -> None:
        await self._cancel_agent_work_for_session(
            msg,
            old_sid,
            publish_interrupt_result=publish_interrupt_result,
            agent_notify=agent_notify,  # type: ignore[arg-type]
        )

    def build_queued_chat_send_message(
        self,
        msg: Message,
        new_input: str,
        original_request: str = "",
    ) -> Message:
        return self._build_queued_chat_send_message(
            msg,
            new_input,
            original_request=original_request,
        )

    def remember_user_query_context(self, msg: Message) -> None:
        self._remember_user_query_context(msg)

    def get_session_last_user_query(self, session_id: str) -> str:
        return self._get_session_last_user_query(session_id)

    async def _trigger_before_chat_request_hook(self, msg: Message) -> None:
        return None

    async def prepare_agent_dispatch_message(self, msg: Message) -> Message:
        return await self._prepare_agent_dispatch_message(msg)


def _message(req_method: ReqMethod) -> Message:
    return Message(
        id="req-1",
        type="req",
        channel_id="web",
        session_id="sess-1",
        params={},
        timestamp=0,
        ok=True,
        req_method=req_method,
        is_stream=True,
    )


def _answer_message(params: dict[str, object]) -> Message:
    return Message(
        id="answer-1",
        type="req",
        channel_id="web",
        session_id="sess-1",
        params=params,
        timestamp=0,
        ok=True,
        req_method=ReqMethod.CHAT_ANSWER,
        is_stream=False,
    )


def _chat_send_message(params: dict[str, object]) -> Message:
    return Message(
        id="chat-send-1",
        type="req",
        channel_id="web",
        session_id="sess-1",
        params=params,
        timestamp=0,
        ok=True,
        req_method=ReqMethod.CHAT_SEND,
        is_stream=False,
    )


def _stream_chat_send_message(params: dict[str, object]) -> Message:
    msg = _chat_send_message(params)
    msg.is_stream = True
    return msg


def _evolution_question_chunk(
    request_id: str,
    *,
    include_approval_context: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        channel_id="web",
        request_id="stream-1",
        payload=_evolution_question_payload(
            request_id,
            include_approval_context=include_approval_context,
        ),
    )


def _evolution_question_payload(
    request_id: str,
    *,
    include_approval_context: bool = False,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "event_type": "chat.ask_user_question",
        "request_id": request_id,
        "questions": [{"header": "x"}],
    }
    if include_approval_context:
        payload.update(
            {
                "source": _APPROVAL_SOURCE,
                "approval_schema": _APPROVAL_SCHEMA,
            }
        )
    return payload


def _set_evolution_auto_save(
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.gateway.message_handler.message_handler.get_evolution_auto_save_enabled",
        lambda _config=None: enabled,
    )
    original_init = MessageHandler.__init__

    def _init_with_cached_auto_save(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._evolution_auto_save_enabled = enabled

    monkeypatch.setattr(MessageHandler, "__init__", _init_with_cached_auto_save)


async def _deliver_evolution_question(
    handler: _TestMessageHandler,
    path: str,
    request_id: str,
    metadata: dict[str, object] | None = None,
) -> None:
    payload = _evolution_question_payload(request_id)
    if path == "stream":
        _FakeAgentClient.stream_payloads = [payload]
        await handler.process_stream(
            SimpleNamespace(request_id="stream-1", channel="web", params={}),
            "sess-1",
            metadata,
            emit_processing_status=False,
        )
        return

    await handler.handle_agent_server_push(
        {
            "request_id": "stream-1",
            "channel_id": "web",
            "session_id": "sess-1",
            "is_complete": False,
            "payload": payload,
            "metadata": metadata or {},
        }
    )


def _interrupt_approval_meta() -> dict[str, str]:
    return dict(_INTERRUPT_APPROVAL_META)


def _approval_answer_params(
    request_id: str,
    selected_options: list[str],
    *,
    query: str | None = None,
    evolution_meta: dict[str, str] | None = None,
) -> dict[str, object]:
    params: dict[str, object] = {
        "request_id": request_id,
        "answers": [{"selected_options": selected_options}],
        "source": _APPROVAL_SOURCE,
        "approval_schema": _APPROVAL_SCHEMA,
    }
    if query is not None:
        params["query"] = query
    if evolution_meta is not None:
        params["evolution_meta"] = evolution_meta
    return params


def _is_finished_processing_status(msg: object) -> bool:
    payload = getattr(msg, "payload", None)
    return (
        isinstance(payload, dict)
        and payload.get("event_type") == "chat.processing_status"
        and payload.get("is_processing") is False
    )


def _has_finished_processing_status(outputs: list[object]) -> bool:
    return any(_is_finished_processing_status(msg) for msg in outputs)


async def _wait_for_pending_clear(
    handler: _TestMessageHandler,
    *,
    require_stream_request: bool = False,
) -> None:
    for _ in range(20):
        if (
            handler.pending_evolution_approval("sess-1") is None
            and (not require_stream_request or _FakeAgentClient.sent_stream_requests)
        ):
            return
        await asyncio.sleep(0.05)


def _assert_evolution_state_cleared(handler: _TestMessageHandler) -> None:
    assert handler.pending_evolution_approval("sess-1") is None
    assert handler.has_session_evolution_in_progress("sess-1") is False
    assert handler.queued_supplement_input("sess-1") is None


def _control_message() -> Message:
    return Message(
        id="control-1",
        type="req",
        channel_id="feishu_enterprise",
        session_id="sess-1",
        params={"mode": "team"},
        timestamp=0,
        ok=True,
        req_method=ReqMethod.CHAT_SEND,
        is_stream=False,
    )


@pytest.mark.parametrize("path", ["stream", "server_push"])
@pytest.mark.asyncio
async def test_regular_stream_chunks_do_not_read_evolution_auto_save_config(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    calls = 0

    def count_auto_save_reads() -> bool:
        nonlocal calls
        calls += 1
        return True

    monkeypatch.setattr(
        "jiuwenswarm.gateway.message_handler.message_handler.get_evolution_auto_save_enabled",
        count_auto_save_reads,
    )
    handler = _TestMessageHandler.create()

    payload = {"event_type": "chat.delta", "content": "chunk"}
    if path == "stream":
        published = await handler.publish_stream_chunk(
            SimpleNamespace(
                channel_id="web",
                request_id="stream-1",
                payload=payload,
                is_complete=False,
            ),
            session_id="sess-1",
        )
        assert published is True
    else:
        await handler.handle_agent_server_push(
            {
                "request_id": "stream-1",
                "channel_id": "web",
                "session_id": "sess-1",
                "is_complete": False,
                "payload": payload,
                "metadata": {},
            }
        )

    assert calls == 0


@pytest.mark.asyncio
async def test_interrupt_evolution_approval_does_not_read_auto_save_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def count_auto_save_reads() -> bool:
        nonlocal calls
        calls += 1
        return True

    monkeypatch.setattr(
        "jiuwenswarm.gateway.message_handler.message_handler.get_evolution_auto_save_enabled",
        count_auto_save_reads,
    )
    handler = _TestMessageHandler.create()
    chunk = _evolution_question_chunk("call_123", include_approval_context=True)
    chunk.payload["evolution_meta"] = _interrupt_approval_meta()

    should_publish = await handler.publish_stream_chunk(
        chunk,
        session_id="sess-1",
        request_metadata={"k": "v"},
    )

    assert calls == 0
    assert should_publish is True
    assert handler.pending_evolution_approval("sess-1") == "call_123"
    assert handler.user_message_queue_empty() is True
    out = await handler.consume_robot_messages(timeout=0)
    assert out is not None
    assert out.payload["request_id"] == "call_123"
    assert out.metadata == {"k": "v"}


def test_processing_status_is_only_emitted_for_chat_streams() -> None:
    handler = _TestMessageHandler.create()

    assert handler.should_emit_processing_status_for_stream(
        _message(ReqMethod.CHAT_SEND)
    ) is True
    assert handler.should_emit_processing_status_for_stream(
        _message(ReqMethod.HISTORY_GET)
    ) is False



@pytest.mark.asyncio
async def test_permission_resume_stream_keeps_processing_while_goal_stream_active() -> None:
    """command.goal (emit=False) still blocks processing_status=false on permission ack."""
    handler = _TestMessageHandler.create()
    _FakeAgentClient.stream_payloads = [
        {"event_type": "runtime.accepted", "request_id": "perm-1"},
    ]
    # Seed an in-flight Goal stream on the same session (emit=False).
    getattr(handler, "_stream_emits_processing_status")["goal-1"] = False
    getattr(handler, "_stream_methods")["goal-1"] = ReqMethod.COMMAND_GOAL.value
    getattr(handler, "_stream_sessions")["goal-1"] = "sess-goal"
    getattr(handler, "_stream_modes")["goal-1"] = "agent"
    getattr(handler, "_stream_channels")["goal-1"] = "web"

    await handler.process_stream(
        SimpleNamespace(
            request_id="perm-1",
            channel="web",
            method=ReqMethod.CHAT_SEND.value,
            params={
                "source": "permission_interrupt",
                "request_id": "call_perm_1",
                "answers": [{"selected_options": ["allow_once"]}],
            },
        ),
        "sess-goal",
        None,
        emit_processing_status=True,
    )

    payloads = await _drain_robot_payloads_for_permission_resume(handler)
    assert not any(
        p.get("event_type") == "chat.processing_status"
        and p.get("is_processing") is False
        for p in payloads
    )
    # Goal tracking must still be present after the short resume stream exits.
    assert "goal-1" in getattr(handler, "_stream_sessions")


@pytest.mark.asyncio
async def test_chat_send_clears_processing_while_history_get_stream_active() -> None:
    """history.get must not block processing_status=false (unlike command.goal)."""
    handler = _TestMessageHandler.create()
    _FakeAgentClient.stream_payloads = [
        {"event_type": "chat.final", "content": "done"},
    ]
    getattr(handler, "_stream_emits_processing_status")["hist-1"] = False
    getattr(handler, "_stream_methods")["hist-1"] = ReqMethod.HISTORY_GET.value
    getattr(handler, "_stream_sessions")["hist-1"] = "sess-chat"
    getattr(handler, "_stream_modes")["hist-1"] = "agent"
    getattr(handler, "_stream_channels")["hist-1"] = "web"

    await handler.process_stream(
        SimpleNamespace(
            request_id="chat-1",
            channel="web",
            method=ReqMethod.CHAT_SEND.value,
            params={"query": "hello"},
        ),
        "sess-chat",
        None,
        emit_processing_status=True,
    )

    payloads = await _drain_robot_payloads_for_permission_resume(handler)
    assert any(
        p.get("event_type") == "chat.processing_status"
        and p.get("is_processing") is False
        for p in payloads
    )
    assert "hist-1" in getattr(handler, "_stream_sessions")


async def _drain_robot_payloads_for_permission_resume(handler: _TestMessageHandler) -> list[dict]:
    payloads: list[dict] = []
    while True:
        out = await handler.consume_robot_messages(timeout=0)
        if out is None:
            break
        if isinstance(out.payload, dict):
            payloads.append(out.payload)
    return payloads


def test_queued_supplement_message_instructs_todo_continuation():
    handler = _TestMessageHandler.create()
    msg = _message(ReqMethod.CHAT_CANCEL)

    queued = handler.build_queued_chat_send_message(
        msg,
        "删除 todo 列表里的提出改善意见",
        original_request=r"Analyze C:\repo\src\ui\screen-layout.ts",
    )

    assert queued.params["supplement_input"] == "删除 todo 列表里的提出改善意见"
    assert queued.params["original_request"] == r"Analyze C:\repo\src\ui\screen-layout.ts"
    assert r"C:\repo\src\ui\screen-layout.ts" in queued.params["query"]
    assert "继续执行当前会话 todo 列表中仍未完成" in queued.params["query"]
    assert "不要因为补充请求本身处理完成就询问用户下一步" in queued.params["query"]
    assert "上一轮正在输出的任务结果可能只展示了一部分" in queued.params["query"]
    assert "不要仅因为 todo 状态已经变为 completed 就跳过" in queued.params["query"]


def test_chat_send_query_context_is_remembered_for_supplement():
    handler = _TestMessageHandler.create()
    msg = _message(ReqMethod.CHAT_SEND)
    msg.params = {
        "query": r"Read C:\repo\src\ui\screen-layout.ts and summarize it",
    }

    handler.remember_user_query_context(msg)

    assert (
        handler.get_session_last_user_query("sess-1")
        == r"Read C:\repo\src\ui\screen-layout.ts and summarize it"
    )


@pytest.mark.asyncio
async def test_resolved_approval_replays_deferred_approval_before_supplement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_evolution_auto_save(monkeypatch, False)
    handler = _TestMessageHandler.create()
    handler.seed_pending_evolution_approval("sess-1", "team_skill_evolve_old")
    handler.seed_session_evolution_in_progress("sess-1")
    handler.seed_queued_supplement_input("sess-1", {"new_input": "继续补充"})

    await handler.handle_evolution_chunk(
        _evolution_question_chunk("team_skill_evolve_new"),
        "sess-1",
        {"k": "v"},
    )
    await handler.complete_evolution_approval_if_current(
        _answer_message(_approval_answer_params("team_skill_evolve_old", ["接收"])),
        "team_skill_evolve_old",
    )

    out = await handler.consume_robot_messages(timeout=0)

    assert out is not None
    assert out.payload["event_type"] == "chat.ask_user_question"
    assert out.payload["request_id"] == "team_skill_evolve_new"
    assert out.metadata == {"k": "v"}
    assert handler.pending_evolution_approval("sess-1") == "team_skill_evolve_new"
    assert handler.queued_supplement_input("sess-1") == {"new_input": "继续补充"}
    assert _FakeAgentClient.sent_stream_requests == []


@pytest.mark.parametrize("path", ["stream", "server_push"])
@pytest.mark.asyncio
async def test_evolution_approval_paths_suppress_deferred_chunk(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    _set_evolution_auto_save(monkeypatch, False)
    handler = _TestMessageHandler.create()
    handler.seed_pending_evolution_approval("sess-1", "team_skill_evolve_old")

    await _deliver_evolution_question(handler, path, "team_skill_evolve_new", {"k": "v"})

    assert handler.pending_evolution_approval("sess-1") == "team_skill_evolve_old"
    assert handler.deferred_evolution_approvals("sess-1") == ["team_skill_evolve_new"]
    assert await handler.consume_robot_messages(timeout=0) is None
    assert handler.user_message_queue_empty() is True


@pytest.mark.parametrize("path", ["stream", "server_push"])
@pytest.mark.asyncio
async def test_evolution_approval_paths_suppress_auto_saved_chunk(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    _set_evolution_auto_save(monkeypatch, True)
    handler = _TestMessageHandler.create()

    await _deliver_evolution_question(handler, path, "team_skill_evolve_new", {"k": "v"})

    assert handler.pending_evolution_approval("sess-1") is None
    assert await handler.consume_robot_messages(timeout=0) is None
    auto_msg = handler.pop_user_message_nowait()
    assert auto_msg.params["request_id"] == "team_skill_evolve_new"
    assert auto_msg.params["answers"] == [{"selected_options": ["接收"]}]
    assert auto_msg.params["approval_schema"] == _APPROVAL_SCHEMA
    assert auto_msg.params["evolution_meta"]["rail_kind"] == "regular"
    assert auto_msg.metadata == {"k": "v"}


@pytest.mark.asyncio
async def test_auto_save_regular_approval_preserves_pending_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_evolution_auto_save(monkeypatch, True)
    handler = _TestMessageHandler.create()
    handler.seed_pending_evolution_approval("sess-1", "call_123")

    await handler.handle_evolution_chunk(
        _evolution_question_chunk("skill_evolve_new"),
        "sess-1",
        {"k": "v"},
    )

    assert handler.pending_evolution_approval("sess-1") == "call_123"
    auto_msg = handler.pop_user_message_nowait()
    assert auto_msg.params["request_id"] == "skill_evolve_new"
    assert auto_msg.params["source"] == _APPROVAL_SOURCE
    assert auto_msg.params["approval_schema"] == _APPROVAL_SCHEMA
    assert auto_msg.params["evolution_meta"]["rail_kind"] == "regular"
    assert "approval_transport" not in auto_msg.params["evolution_meta"]
    assert auto_msg.metadata == {"k": "v"}


@pytest.mark.asyncio
async def test_handle_evolution_chunk_tracks_regular_approval_without_request_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_evolution_auto_save(monkeypatch, False)
    handler = _TestMessageHandler.create()

    await handler.handle_evolution_chunk(
        _evolution_question_chunk("approval_123", include_approval_context=True),
        "sess-1",
        {"k": "v"},
    )

    assert handler.pending_evolution_approval("sess-1") == "approval_123"


@pytest.mark.asyncio
async def test_regular_auto_save_answer_resolved_clears_processing_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_evolution_auto_save(monkeypatch, True)
    handler = _TestMessageHandler.create()
    old_response_payload = dict(_FakeAgentClient.response_payload)
    _FakeAgentClient.response_payload = {"accepted": True, "resolved": True}
    await handler.handle_evolution_chunk(
        _evolution_question_chunk("team_skill_evolve_123"),
        "sess-1",
    )
    await handler.start_forwarding()
    try:
        outputs = [
            await handler.consume_robot_messages(timeout=2),
            await handler.consume_robot_messages(timeout=2),
        ]

        assert _has_finished_processing_status(outputs)
    finally:
        _FakeAgentClient.response_payload = old_response_payload
        await handler.stop_forwarding()


@pytest.mark.asyncio
async def test_interrupt_evolution_approval_chat_send_cleans_pending_and_releases_queued_supplement() -> None:
    handler = _TestMessageHandler.create()
    old_response_payload = dict(_FakeAgentClient.response_payload)
    _FakeAgentClient.response_payload = {"accepted": True}
    handler.seed_pending_evolution_approval("sess-1", "call_123")
    handler.seed_session_evolution_in_progress("sess-1")
    handler.seed_queued_supplement_input("sess-1", {"new_input": "继续补充"})
    await handler.start_forwarding()
    try:
        await handler.publish_user_messages(
            _chat_send_message(
                _approval_answer_params(
                    "call_123",
                    ["allow_once"],
                    query="",
                    evolution_meta=_interrupt_approval_meta(),
                )
            )
        )

        await _wait_for_pending_clear(handler, require_stream_request=True)

        _assert_evolution_state_cleared(handler)
        assert _FakeAgentClient.sent_requests == []
        sent_params = _FakeAgentClient.sent_stream_requests[0].params
        assert sent_params["request_id"] == "call_123"
        assert sent_params["source"] == _APPROVAL_SOURCE
        queued_params = _FakeAgentClient.sent_stream_requests[-1].params
        assert queued_params["supplement_input"] == "继续补充"
        assert queued_params["is_supplement"] is True
    finally:
        _FakeAgentClient.response_payload = old_response_payload
        await handler.stop_forwarding()


@pytest.mark.asyncio
async def test_interrupt_evolution_approval_chat_send_without_supplement_finishes_processing() -> None:
    handler = _TestMessageHandler.create()
    old_response_payload = dict(_FakeAgentClient.response_payload)
    _FakeAgentClient.response_payload = {"accepted": True}
    handler.seed_pending_evolution_approval("sess-1", "call_123")
    handler.seed_session_evolution_in_progress("sess-1")
    await handler.start_forwarding()
    try:
        await handler.publish_user_messages(
            _chat_send_message(
                _approval_answer_params(
                    "call_123",
                    ["allow_once"],
                    evolution_meta=_interrupt_approval_meta(),
                )
            )
        )

        await _wait_for_pending_clear(handler)
        outputs = [
            await handler.consume_robot_messages(timeout=2),
            await handler.consume_robot_messages(timeout=2),
        ]

        _assert_evolution_state_cleared(handler)
        assert _FakeAgentClient.sent_requests == []
        assert _FakeAgentClient.sent_stream_requests[0].params["request_id"] == "call_123"
        assert _has_finished_processing_status(outputs)
    finally:
        _FakeAgentClient.response_payload = old_response_payload
        await handler.stop_forwarding()


@pytest.mark.asyncio
async def test_interrupt_evolution_approval_chat_send_streams_resume_output() -> None:
    handler = _TestMessageHandler.create()
    _FakeAgentClient.stream_payloads = [
        {"event_type": "chat.delta", "content": "审批后继续展示"},
    ]
    handler.seed_pending_evolution_approval("sess-1", "call_123")
    handler.seed_session_evolution_in_progress("sess-1")
    await handler.start_forwarding()
    try:
        await handler.publish_user_messages(
            _chat_send_message(
                _approval_answer_params(
                    "call_123",
                    ["allow_once"],
                    evolution_meta=_interrupt_approval_meta(),
                )
            )
        )

        await _wait_for_pending_clear(handler, require_stream_request=True)
        outputs = [
            await handler.consume_robot_messages(timeout=2),
            await handler.consume_robot_messages(timeout=2),
            await handler.consume_robot_messages(timeout=2),
        ]

        _assert_evolution_state_cleared(handler)
        assert _FakeAgentClient.sent_requests == []
        assert _FakeAgentClient.sent_stream_requests[0].params["request_id"] == "call_123"
        assert any(
            getattr(msg, "payload", {}).get("content") == "审批后继续展示"
            for msg in outputs
            if msg is not None
        )
        assert _has_finished_processing_status(outputs)
    finally:
        await handler.stop_forwarding()


@pytest.mark.asyncio
async def test_stale_interrupt_evolution_approval_chat_send_keeps_current_processing() -> None:
    handler = _TestMessageHandler.create()
    handler.seed_pending_evolution_approval("sess-1", "call_new")
    handler.seed_session_evolution_in_progress("sess-1")
    await handler.start_forwarding()
    try:
        await handler.publish_user_messages(
            _chat_send_message(
                _approval_answer_params(
                    "call_old",
                    ["allow_once"],
                    evolution_meta=_interrupt_approval_meta(),
                )
            )
        )

        await asyncio.sleep(0.05)

        assert handler.pending_evolution_approval("sess-1") == "call_new"
        assert handler.has_session_evolution_in_progress("sess-1") is True
        assert _FakeAgentClient.sent_requests == []
        assert _FakeAgentClient.sent_stream_requests == []
        assert await handler.consume_robot_messages(timeout=0) is None
    finally:
        await handler.stop_forwarding()


@pytest.mark.asyncio
async def test_interrupt_evolution_approval_user_answer_is_dispatched_as_chat_send() -> None:
    handler = _TestMessageHandler.create()
    old_response_payload = dict(_FakeAgentClient.response_payload)
    _FakeAgentClient.response_payload = {"accepted": True}
    handler.seed_pending_evolution_approval("sess-1", "call_123")
    handler.seed_session_evolution_in_progress("sess-1")
    handler.seed_queued_supplement_input("sess-1", {"new_input": "继续补充"})
    await handler.start_forwarding()
    try:
        await handler.publish_user_messages(
            _answer_message(
                _approval_answer_params(
                    "call_123",
                    ["allow_once"],
                    evolution_meta=_interrupt_approval_meta(),
                )
            )
        )

        await _wait_for_pending_clear(handler, require_stream_request=True)

        assert handler.pending_evolution_approval("sess-1") is None
        assert _FakeAgentClient.sent_requests == []
        sent = _FakeAgentClient.sent_stream_requests[0]
        assert sent.method == ReqMethod.CHAT_SEND.value
        assert sent.is_stream is True
        assert sent.params["request_id"] == "call_123"
        assert sent.params["answers"] == [{"selected_options": ["allow_once"]}]
        queued_params = _FakeAgentClient.sent_stream_requests[-1].params
        assert queued_params["supplement_input"] == "继续补充"
    finally:
        _FakeAgentClient.response_payload = old_response_payload
        await handler.stop_forwarding()


@pytest.mark.asyncio
async def test_stream_interrupt_evolution_approval_chat_send_cleans_pending() -> None:
    handler = _TestMessageHandler.create()
    handler.seed_pending_evolution_approval("sess-1", "call_123")
    handler.seed_session_evolution_in_progress("sess-1")
    handler.seed_queued_supplement_input("sess-1", {"new_input": "继续补充"})
    await handler.start_forwarding()
    try:
        await handler.publish_user_messages(
            _stream_chat_send_message(
                _approval_answer_params(
                    "call_123",
                    ["allow_once"],
                    query="",
                    evolution_meta=_interrupt_approval_meta(),
                )
            )
        )

        await _wait_for_pending_clear(handler, require_stream_request=True)

        _assert_evolution_state_cleared(handler)
        assert _FakeAgentClient.sent_stream_requests[0].params["request_id"] == "call_123"
        assert _FakeAgentClient.sent_stream_requests[-1].params["supplement_input"] == "继续补充"
    finally:
        await handler.stop_forwarding()


@pytest.mark.asyncio
async def test_control_command_cancel_suppresses_interrupt_result() -> None:
    handler = _TestMessageHandler.create()

    await handler.cancel_agent_work_for_session(
        _control_message(),
        "sess-1",
        publish_interrupt_result=False,
    )

    assert len(_FakeAgentClient.sent_requests) == 1
    assert await handler.consume_robot_messages(timeout=0) is None


@pytest.mark.asyncio
async def test_default_cancel_publishes_interrupt_result() -> None:
    handler = _TestMessageHandler.create()

    await handler.cancel_agent_work_for_session(_control_message(), "sess-1")

    out = await handler.consume_robot_messages(timeout=0)
    assert out is not None
    assert out.payload == _FakeAgentClient.response_payload


@pytest.mark.asyncio
async def test_fire_and_forget_cancel_publishes_interrupt_result() -> None:
    handler = _TestMessageHandler.create()

    await handler.cancel_agent_work_for_session(
        _control_message(),
        "sess-1",
        agent_notify="fire_and_forget",
    )

    out = await handler.consume_robot_messages(timeout=0)
    assert out is not None
    assert out.payload == {
        "event_type": "chat.interrupt_result",
        "intent": "cancel",
        "success": True,
        "message": "任务已取消",
    }
    await asyncio.sleep(0)
    assert len(_FakeAgentClient.sent_requests) == 1


@pytest.mark.asyncio
async def test_fire_and_forget_cancel_forwards_cancelled_tools() -> None:
    """Background interrupt must still push chat.tool_result for cancelled tools."""
    handler = _TestMessageHandler.create()
    old_payload = dict(_FakeAgentClient.response_payload)
    _FakeAgentClient.response_payload = {
        "event_type": "chat.interrupt_result",
        "intent": "cancel",
        "success": True,
        "message": "任务已取消",
        "cancelled_tools": [
            {
                "tool_name": "task_tool",
                "tool_call_id": "call_spin",
                "result": "[Interrupted] Tool execution cancelled by user.",
                "status": "error",
            }
        ],
    }
    try:
        await handler.cancel_agent_work_for_session(
            _control_message(),
            "sess-1",
            agent_notify="fire_and_forget",
        )

        interrupt_out = await handler.consume_robot_messages(timeout=0)
        assert interrupt_out is not None
        assert interrupt_out.payload["event_type"] == "chat.interrupt_result"

        # Wait for fire-and-forget task to publish tool_result
        tool_out = None
        deadline = asyncio.get_running_loop().time() + 2.0
        while asyncio.get_running_loop().time() < deadline:
            tool_out = await handler.consume_robot_messages(timeout=0.05)
            if tool_out is not None:
                break
            await asyncio.sleep(0.01)

        assert tool_out is not None
        assert tool_out.event_type.value == "chat.tool_result"
        assert tool_out.payload["tool_result"]["tool_call_id"] == "call_spin"
        assert "[Interrupted]" in tool_out.payload["tool_result"]["result"]
    finally:
        _FakeAgentClient.response_payload = old_payload


@pytest.mark.asyncio
async def test_forward_loop_cancel_intent_uses_fire_and_forget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHAT_CANCEL intent=cancel 必须 fire_and_forget，避免堵 _forward_loop。"""
    handler = _TestMessageHandler.create()
    notify_modes: list[str] = []

    async def _spy_cancel(msg, old_sid, **kwargs):
        notify_modes.append(str(kwargs.get("agent_notify", "await")))

    monkeypatch.setattr(handler, "_cancel_agent_work_for_session", _spy_cancel)
    await handler.start_forwarding()
    try:
        cancel_msg = Message(
            id="cancel-fire-and-forget",
            type="req",
            channel_id="web",
            session_id="sess-1",
            params={"intent": "cancel", "session_id": "sess-1"},
            timestamp=0.0,
            ok=True,
            req_method=ReqMethod.CHAT_CANCEL,
            is_stream=False,
        )
        await handler.publish_user_messages(cancel_msg)
        deadline = asyncio.get_running_loop().time() + 2.0
        while not notify_modes and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert notify_modes == ["fire_and_forget"]
    finally:
        await handler.stop_forwarding()


@pytest.mark.asyncio
async def test_forward_loop_supplement_forwards_new_input_to_interrupt() -> None:
    """AgentServer needs the text marker to discard a superseded ask_user round."""
    handler = _TestMessageHandler.create()
    await handler.start_forwarding()
    try:
        supplement_msg = Message(
            id="supplement-ask-user",
            type="req",
            channel_id="tui",
            session_id="sess-ask-user",
            params={
                "intent": "supplement",
                "new_input": "再执行一次",
                "mode": "agent.plan",
            },
            timestamp=0.0,
            ok=True,
            req_method=ReqMethod.CHAT_CANCEL,
            is_stream=False,
        )
        await handler.publish_user_messages(supplement_msg)

        deadline = asyncio.get_running_loop().time() + 2.0
        while (
            not _FakeAgentClient.sent_requests
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.01)

        assert _FakeAgentClient.sent_requests
        interrupt_request = _FakeAgentClient.sent_requests[0]
        assert interrupt_request.params["intent"] == "supplement"
        assert interrupt_request.params["new_input"] == "再执行一次"

        deadline = asyncio.get_running_loop().time() + 2.0
        while (
            not _FakeAgentClient.sent_stream_requests
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.01)
        assert len(_FakeAgentClient.sent_stream_requests) == 1
        follow_up_request = _FakeAgentClient.sent_stream_requests[0]
        assert follow_up_request.params["supplement_input"] == "再执行一次"
    finally:
        await handler.stop_forwarding()
