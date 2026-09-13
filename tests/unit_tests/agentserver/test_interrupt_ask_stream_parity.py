# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tool-approval prompt delivery must not depend on model narration.

Regression net for the "narration + permission interrupt" incident: when an
LLM response carries assistant text together with a permission-gated tool call
(``content_len>0, tool_call_count=1``), the resulting ``__interaction__`` chunk
must still be converted into a ``chat.ask_user_question`` event and relayed to
the client, exactly as in the bare-interrupt (``content_len=0``) case.

Two layers are pinned here:

1. Stream parity through the real parsing/relay code — the session-scoped
   DeepAdapter loop (``process_message_stream_impl``) and the facade relay
   (``JiuWenSwarm.process_message_stream``) both surface the ask event whether
   or not narration chunks streamed first.
2. The durable side of the same prompt — ``pending_interrupt_ask_payload_from_state``
   rebuilds the identical ask payload from the engine's parked
   ``ToolInterruptionState``, and ``_republish_pending_interrupt_ask`` pushes
   it to a freshly attached client (the fix for the prompt being lost when the
   client's WebSocket was between connections at publish time).
"""

from __future__ import annotations

import collections
import contextlib
import types
from typing import Any, AsyncIterator

import pytest

from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.session.interaction.interaction import InteractionOutput
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.core.single_agent.interrupt.response import (
    InterruptRequest,
    ToolCallInterruptRequest,
)
from openjiuwen.core.foundation.llm import AssistantMessage
from openjiuwen.core.single_agent.interrupt.state import (
    ToolInterruptEntry,
    ToolInterruptionState,
)

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    pending_interrupt_ask_payload_from_state,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    bind_root_permission_request,
    reset_root_permission_request,
)
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)

_TOOL_CALL_ID = "chatcmpl-tool-9059a0fa6c88fb16"
_PERMISSION_MESSAGE = "**工具 `clouddoc_batch_edit`** 需要授权才能执行"


def _tool_call() -> ToolCall:
    return ToolCall(
        id=_TOOL_CALL_ID,
        type="function",
        name="clouddoc_batch_edit",
        arguments='{"doc_id": "doc-1", "edits": [{"old_string": "a", "new_string": "b"}]}',
    )


def _interaction_chunk() -> OutputSchema:
    """The ``__interaction__`` chunk exactly as the engine writes it on interrupt."""
    request = ToolCallInterruptRequest.from_tool_call(
        request=InterruptRequest(message=_PERMISSION_MESSAGE),
        tool_call=_tool_call(),
    )
    return OutputSchema(
        type="__interaction__",
        index=0,
        payload=InteractionOutput(id=_TOOL_CALL_ID, value=request),
    )


def _llm_output_chunk(text: str) -> OutputSchema:
    return OutputSchema(
        type="llm_output",
        index=0,
        payload={"content": text, "result_type": "answer"},
    )


class _FakeInteractionStream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = list(chunks)

    def __aiter__(self) -> "_FakeInteractionStream":
        return self

    async def __anext__(self) -> Any:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def close(self, *, abort_active_round: bool = True) -> None:
        return None


class _FakeInstance:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks

    async def attach_output(self) -> _FakeInteractionStream:
        return _FakeInteractionStream(self._chunks)

    async def send_input(self, _req: Any) -> None:
        return None


async def _async_none(*_args: Any, **_kwargs: Any) -> None:
    return None


@contextlib.asynccontextmanager
async def _passthrough_async_cm(*args, **kwargs):
    yield


@contextlib.contextmanager
def _bind_legacy_permission_request(request):
    token = bind_root_permission_request(
        root_session_id=str(request.session_id or ""),
        request_id=str(request.request_id or ""),
        enabled=False,
    )
    try:
        yield
    finally:
        reset_root_permission_request(token)


def _make_loop_adapter(chunks: list[Any]) -> JiuWenSwarmDeepAdapter:
    """A session-scoped DeepAdapter whose runner stream is scripted.

    Only the state the ``process_message_stream_impl`` chat path actually
    touches is provided; everything unrelated to chunk parsing/relay is a
    no-op stub so the loop under test is the real one.
    """
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True
    # Upstream wraps the stream in a permission admission / binding pair. The
    # admission reads the Smart-permission lifecycle off a fully built adapter,
    # which this bare one is not, so it passes straight through. The binding
    # has to be real, though: converting an interrupt into an ask now asks for
    # the request's root-permission binding and raises when there is none, so
    # an unbound request would drop the very event this case is about. Bound
    # as an ordinary (non-Smart) request, the shape a legacy permission ask has.
    adapter._permission_request_admission = _passthrough_async_cm
    adapter._bind_permission_request_context = _bind_legacy_permission_request
    adapter.validate_auto_permission_workspace_request = lambda request: None
    adapter._enable_auto_permission = False
    adapter._permission_dispatch = types.SimpleNamespace(finalize=lambda inputs: None)
    adapter._instance = _FakeInstance(chunks)
    adapter._parent_session_id = None
    adapter._stream_event_rail = None
    adapter._stream_content_run_kind = None
    adapter._stream_round_kind_latch = None
    adapter._stream_round_output_ended = False
    adapter._config_cache = {}
    adapter._model_request_config = None
    adapter._project_dir = None
    adapter._workspace_dir = None
    adapter._runtime_prompt_rail = None
    adapter._heartbeat_service = None
    adapter._active_session_ids = collections.Counter()
    adapter._session_agent_tasks = {}
    adapter._vision_model_config = None
    adapter._skill_evolution_rail = None
    adapter._requested_model_name = lambda request: "gpt-4"
    adapter._has_valid_model_config = lambda model: True
    adapter._resolve_model_for_request = lambda request: "gpt-4"
    adapter._apply_model_to_react_agent = lambda *a, **k: None
    adapter._structured_goal_op_from_request = lambda request: None
    adapter._handle_slash_command = _async_none
    adapter._update_runtime_config = _async_none
    adapter._prepare_multimodal_image_inputs = lambda request, inputs: inputs
    adapter._native_image_input_enabled = lambda *a, **k: False
    adapter._build_image_tool_fallback_notice = lambda *a, **k: None
    adapter._prepare_react_image_tool_prompt = lambda request, inputs, **k: inputs
    adapter._resolve_eternal_conversation_enabled = lambda params: False
    adapter._is_eternal_interaction_resume = lambda params: False
    adapter._should_inject_into_existing_interaction = lambda params: False
    adapter._ensure_chat_extensions = _async_none
    adapter._bind_runtime_cron_context = lambda **k: []
    adapter._reset_runtime_cron_context = lambda tokens: None
    adapter._runtime_cron_tool_context = types.SimpleNamespace(
        remember_current_binding=lambda: None,
    )
    adapter._mark_session_active = lambda sid: None
    adapter._register_session_agent_task = lambda sid: None
    adapter._unregister_session_agent_task = lambda *a, **k: None
    adapter._current_interaction_run_kind = lambda: None
    adapter._should_demote_goal_intermediate_final = lambda: False
    adapter._goal_intermediate_final_repeats_streamed_text = lambda text: False
    adapter._goal_record_is_active = lambda: False
    adapter._record_goal_completed_history_if_needed = lambda **k: None
    adapter._record_goal_set_history_if_needed = lambda *a, **k: None
    adapter._flush_pending_goal_objective_history = lambda *a, **k: None
    adapter._note_round_visible_text = lambda text: None
    adapter._resolve_model_name = lambda: "gpt-4"
    adapter._resolve_runtime_language = lambda: "zh"
    adapter._resolve_prompt_channel = lambda sid: "web"
    adapter._write_runtime_state = lambda **k: None
    adapter._get_goal_manager = lambda: None
    return adapter


async def _run_loop(chunks: list[Any]) -> list[str]:
    adapter = _make_loop_adapter(chunks)
    request = AgentRequest(
        request_id="req-ask-parity",
        channel_id="web",
        session_id="ask-parity-sess",
        params={"query": "批量替换", "mode": "agent"},
    )
    inputs = {"query": "批量替换", "conversation_id": "ask-parity-sess"}
    events: list[str] = []
    async for chunk in adapter.process_message_stream_impl(request, inputs):
        payload = chunk.payload
        if isinstance(payload, dict):
            events.append(str(payload.get("event_type")))
    return events


@pytest.mark.asyncio
async def test_adapter_loop_surfaces_ask_after_narration() -> None:
    """content_len>0: narration deltas must not swallow the interrupt ask."""
    events = await _run_loop(
        [
            _llm_output_chunk("好的，我将一次性批量替换这11处内容。"),
            _interaction_chunk(),
        ]
    )
    assert "chat.delta" in events
    assert "chat.ask_user_question" in events
    # The prompt follows the narration; the frontend renders text first, then
    # arms the approval bar.
    assert events.index("chat.ask_user_question") > events.index("chat.delta")


@pytest.mark.asyncio
async def test_adapter_loop_surfaces_ask_without_narration() -> None:
    """content_len=0: the bare interrupt keeps working (baseline)."""
    events = await _run_loop([_interaction_chunk()])
    assert "chat.ask_user_question" in events


class _ScriptedFacadeAdapter:
    """Replays parsed payloads as the facade's inner adapter output."""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._payloads = payloads

    async def create_instance(self, config: dict[str, Any] | None = None) -> None:
        return None

    async def reload_agent_config(self, *_a: Any, **_k: Any) -> None:
        return None

    async def process_message_impl(self, *_a: Any, **_k: Any) -> Any:
        return None

    async def process_message_stream_impl(
        self, request: AgentRequest, _inputs: dict[str, Any]
    ) -> AsyncIterator[AgentResponseChunk]:
        for payload in self._payloads:
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload=dict(payload),
                is_complete=False,
            )

    async def reconcile_session_mcp(self, *_a: Any, **_k: Any) -> None:
        return None


_ASK_PAYLOAD = {
    "event_type": "chat.ask_user_question",
    "request_id": _TOOL_CALL_ID,
    "questions": [
        {
            "question": _PERMISSION_MESSAGE,
            "options": [{"label": "本次允许"}, {"label": "拒绝"}],
            "multi_select": False,
        }
    ],
    "source": "permission_interrupt",
}


async def _run_facade(
    monkeypatch: pytest.MonkeyPatch, payloads: list[dict[str, Any]]
) -> list[str]:
    facade = JiuWenSwarm()
    monkeypatch.setattr(facade, "_adapter", _ScriptedFacadeAdapter(payloads))
    monkeypatch.setattr(facade, "_sdk_name", "harness")
    monkeypatch.setattr(interface_module, "append_history_record", lambda **_k: None)
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _cfg: "off")
    monkeypatch.setattr(interface_module, "build_user_prompt", lambda q, **_k: q)
    request = AgentRequest(
        request_id="req-ask-facade",
        channel_id="web",
        session_id="ask_facade_sess",
        params={"query": "批量替换", "mode": "agent"},
    )
    events: list[str] = []
    async for chunk in facade.process_message_stream(request):
        payload = chunk.payload
        if isinstance(payload, dict):
            events.append(str(payload.get("event_type")))
    return events


@pytest.mark.asyncio
async def test_facade_relays_ask_after_narration(monkeypatch: pytest.MonkeyPatch) -> None:
    events = await _run_facade(
        monkeypatch,
        [{"event_type": "chat.delta", "content": "先说明一下要做的替换。"}, dict(_ASK_PAYLOAD)],
    )
    assert "chat.ask_user_question" in events


@pytest.mark.asyncio
async def test_facade_relays_bare_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    events = await _run_facade(monkeypatch, [dict(_ASK_PAYLOAD)])
    assert "chat.ask_user_question" in events


# ── durable side: rebuild + republish ────────────────────────────────────────


def _parked_state() -> ToolInterruptionState:
    tool_call = _tool_call()
    return ToolInterruptionState(
        ai_message=AssistantMessage(content="", tool_calls=[tool_call]),
        iteration=2,
        original_query="批量替换",
        interrupted_tools={
            _TOOL_CALL_ID: ToolInterruptEntry(
                tool_call=tool_call,
                interrupt_requests={
                    _TOOL_CALL_ID: InterruptRequest(message=_PERMISSION_MESSAGE)
                },
            )
        },
    )


def test_pending_ask_rebuilt_from_parked_state() -> None:
    payload = pending_interrupt_ask_payload_from_state(_parked_state())
    assert payload is not None
    assert payload["event_type"] == "chat.ask_user_question"
    assert payload["request_id"] == _TOOL_CALL_ID
    assert payload["source"] == "permission_interrupt"
    assert payload["questions"], "rebuilt prompt must carry answerable questions"


def test_pending_ask_rebuild_returns_none_without_parked_tools() -> None:
    assert pending_interrupt_ask_payload_from_state(None) is None
    empty = ToolInterruptionState(
        ai_message=AssistantMessage(content="", tool_calls=[]),
        iteration=1,
        original_query="",
        interrupted_tools={},
    )
    assert pending_interrupt_ask_payload_from_state(empty) is None


class _FakeLoopSession:
    def __init__(self, session_id: str, state: Any) -> None:
        self._session_id = session_id
        self._state = state

    def get_session_id(self) -> str:
        return self._session_id

    def get_state(self, _key: str) -> Any:
        return self._state


def _make_ws_server_for_republish(state: Any, session_id: str):
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    server = object.__new__(AgentWebSocketServer)
    deep_agent = types.SimpleNamespace(
        _loop_session=_FakeLoopSession(session_id, state)
    )
    session_adapter = types.SimpleNamespace(_instance=deep_agent)

    class _RootAdapter:
        _is_session_scoped_adapter = False

        @staticmethod
        def apply_sandbox_runtime_patch() -> None:  # marker for _resolve_adapter
            return None

        @staticmethod
        def _get_cached_session_adapter(_sid: str):
            return session_adapter

    agent = types.SimpleNamespace(_adapter=_RootAdapter())
    server._agent_manager = types.SimpleNamespace(
        get_agent_for_session_nowait=lambda channel_id, session_id: agent,
        get_agent_nowait=lambda channel_id: agent,
    )
    pushed: list[dict[str, Any]] = []

    async def _send_push(msg: dict[str, Any]) -> bool:
        pushed.append(msg)
        return True

    server.send_push = _send_push
    return server, pushed


@pytest.mark.asyncio
async def test_session_switch_republishes_parked_ask() -> None:
    server, pushed = _make_ws_server_for_republish(_parked_state(), "sess-republish")
    assert await server._republish_pending_interrupt_ask("web", "sess-republish") is True
    assert len(pushed) == 1
    msg = pushed[0]
    assert msg["session_id"] == "sess-republish"
    payload = msg["payload"]
    assert payload["event_type"] == "chat.ask_user_question"
    assert payload["request_id"] == _TOOL_CALL_ID
    assert payload["session_id"] == "sess-republish"


@pytest.mark.asyncio
async def test_session_switch_republish_noop_when_answered() -> None:
    """State cleared on resume ⇒ nothing owed ⇒ no duplicate prompt."""
    server, pushed = _make_ws_server_for_republish(None, "sess-republish")
    assert await server._republish_pending_interrupt_ask("web", "sess-republish") is False
    assert pushed == []


@pytest.mark.asyncio
async def test_session_switch_republish_noop_for_other_session() -> None:
    server, pushed = _make_ws_server_for_republish(_parked_state(), "sess-a")
    assert await server._republish_pending_interrupt_ask("web", "sess-b") is False
    assert pushed == []


# ── full republish chain: server push wire → gateway relay → robot_messages ──


class _NullAgentClient:
    async def send_request(self, env: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("unexpected send_request")

    async def send_request_stream(self, env: Any):  # pragma: no cover - unused
        raise AssertionError("unexpected send_request_stream")
        yield  # noqa: W0101


@pytest.mark.asyncio
async def test_republished_ask_reaches_web_channel_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The republish must survive the whole gateway leg, not just send_push.

    Server side: ``_republish_pending_interrupt_ask`` encodes its message with
    the real ``build_server_push_wire``. Gateway side: that exact wire dict is
    fed to the real ``MessageHandler._handle_agent_server_push``, and the test
    asserts a ``chat.ask_user_question`` Message routed to the parked session
    lands in ``robot_messages`` — the web channel's input queue.
    """
    from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
    from jiuwenswarm.server.gateway_push.wire import build_server_push_wire

    session_id = "sess-republish-relay"
    server, pushed = _make_ws_server_for_republish(_parked_state(), session_id)

    wires: list[dict[str, Any]] = []
    original_send_push = server.send_push

    async def _send_push_and_encode(msg: dict[str, Any]) -> bool:
        wires.append(build_server_push_wire(msg))
        return await original_send_push(msg)

    server.send_push = _send_push_and_encode
    assert await server._republish_pending_interrupt_ask("web", session_id) is True
    assert len(wires) == 1

    saved_instance = MessageHandler._instance
    MessageHandler._instance = None
    try:
        handler = MessageHandler(_NullAgentClient())
        published: list[Any] = []

        async def _capture(msg: Any) -> None:
            published.append(msg)

        monkeypatch.setattr(handler, "publish_robot_messages", _capture)
        await handler._handle_agent_server_push(wires[0])
    finally:
        MessageHandler._instance = saved_instance

    assert len(published) == 1, "gateway must relay the pushed ask to robot_messages"
    out = published[0]
    assert out.session_id == session_id
    assert isinstance(out.payload, dict)
    assert out.payload.get("event_type") == "chat.ask_user_question"
    assert out.payload.get("request_id") == _TOOL_CALL_ID
    # The frontend routes the prompt by payload.session_id; it must survive
    # the wire round-trip so a freshly attached client can arm the prompt.
    assert out.payload.get("session_id") == session_id
    assert out.payload.get("questions"), "relayed prompt must stay answerable"
