# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consolidated regression tests (merged from 4 small test modules)."""

from __future__ import annotations

import pytest
from e2a.agent_models import AgentResponse, AgentResponseChunk
from agent_gateway.cron.scheduler import CronSchedulerService
from agent_gateway.cron.store import CronJobStore
from agent_gateway.message_handler.message_handler import MessageHandler
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from lark_oapi.api.im.v1 import P2ImChatMemberBotAddedV1
from agent_gateway.channel_manager.im_platforms.feishu.feishu_connect import FeishuChannel
from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import InboundDisposition
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from e2a.models import E2AEnvelope
from jiuwenswarm.extensions.runtime_management_extension import pod_http_client as pod_http
from jiuwenswarm.extensions.runtime_management_extension.pod_http_client import (
    HttpSseAgentServerClient,
)
from e2a.gateway_normalize import e2a_from_agent_fields
from e2a.message import ReqMethod
from jiuwenswarm.common.schema import Message


# --- from test_cron_scheduler_web_session.py ---

class _MessageHandlerStreamTestApi(MessageHandler):
    @classmethod
    def is_terminal_stream_chunk(cls, chunk) -> bool:
        return cls._is_terminal_stream_chunk(chunk)

    @classmethod
    def chunk_to_message(cls, chunk, *, session_id, metadata=None):
        return cls._chunk_to_message(
            chunk,
            session_id=session_id,
            metadata=metadata,
        )

class _TestableScheduler(CronSchedulerService):
    """Subclass that exposes protected members as public methods.

    G.CLS.11 forbids accessing protected members from outside the class
    hierarchy. By subclassing, we can access them legitimately and then
    expose thin public wrappers for test assertions — no source changes needed.
    """

    async def handle_event(self, ev):
        return await self._handle_event(ev)

    @property
    def runs(self):
        return self._runs

    @property
    def run_tasks(self):
        """Expose _run_tasks for test assertions (G.CLS.11: access via subclass property)."""
        return self._run_tasks

    @property
    def events(self):
        """Expose the queued event heap for test assertions."""
        return self._events

    async def on_wake(self, job, run_id):
        return await self._on_wake(job, run_id)

class FakeAgentClient:
    """Stub AgentServerClient that never calls a real agent."""

    def __init__(self) -> None:
        self.unary_requests = []
        self.stream_requests = []
        self.created_session_id = "cron_agentserver_allocated"

    async def send_request(self, envelope, *a, **kw):
        self.unary_requests.append(envelope)
        if envelope.method == "session.create":
            return AgentResponse(
                request_id=envelope.request_id or "",
                channel_id=envelope.channel or "",
                ok=True,
                payload={"session_id": self.created_session_id},
            )
        if envelope.method == "project.lifecycle":
            # scheduler.project_execution_allowed 的准入查询：
            # 默认按"项目存在且未归档"放行，归档拦截场景由专项测试覆盖。
            return AgentResponse(
                request_id=envelope.request_id or "",
                channel_id=envelope.channel or "",
                ok=True,
                payload={"exists": True, "execution_blocked": False},
            )
        return AgentResponse(
            request_id=envelope.request_id or "",
            channel_id=envelope.channel or "",
            ok=True,
            payload={"content": {"output": "done", "result_type": "answer"}},
        )

    async def send_request_stream(self, envelope):
        self.stream_requests.append(envelope)
        payloads = [
            {
                "event_type": "workflow.updated",
                "workflow": {
                    "id": "wf-1",
                    "status": "completed",
                    "summary": "team workflow done",
                },
            },
            {"event_type": "chat.final", "content": "team result"},
            {"is_complete": True},
        ]
        for payload in payloads:
            yield AgentResponseChunk(
                request_id=envelope.request_id or "",
                channel_id=envelope.channel or "",
                payload=payload,
                is_complete=bool(payload.get("is_complete")),
            )

class FakeMessageHandler:
    """Stub MessageHandler that records published messages."""

    def __init__(self):
        self.published = []

    async def publish_robot_messages(self, msg):
        self.published.append(msg)

    async def register_background_stream(self, envelope, session_id):
        return None

    async def finish_background_stream(self, envelope, session_id):
        return None

    def background_stream_has_other_work(self, request_id, session_id):
        return False

    async def publish_stream_chunk(self, chunk, *, session_id, request_metadata=None):
        if _MessageHandlerStreamTestApi.is_terminal_stream_chunk(chunk):
            return False
        out = _MessageHandlerStreamTestApi.chunk_to_message(
            chunk,
            session_id=session_id,
            metadata=request_metadata,
        )
        await self.publish_robot_messages(out)
        return True

async def _create_one_job(store, name="job", targets="tui", user_id="", mode=None):
    """Convenience: create a single cron job via the store."""
    return await store.create_job(
        name=name,
        cron_expr="0 0 9 * * ? *",
        timezone="Asia/Shanghai",
        description="reminder",
        targets=targets,
        user_id=user_id,
        mode=mode,
    )

def _make_scheduler(store, handler=None, agent_client=None, now_fn=None):
    """Build a _TestableScheduler with fake deps for testing."""
    return _TestableScheduler(
        store=store,
        agent_client=agent_client or FakeAgentClient(),
        message_handler=handler or FakeMessageHandler(),
        **({"now_fn": now_fn} if now_fn is not None else {}),
    )

@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["agent", "team"])
async def test_web_cron_stream_uses_one_web_session_without_result_broadcast(tmp_path, mode):
    store = CronJobStore(path=tmp_path / "jobs.json")
    job = await _create_one_job(store, targets="web", user_id="owner", mode=mode)
    job.group_id = "group"
    job.bot_id = "bot"
    handler = FakeMessageHandler()
    client = FakeAgentClient()
    client.created_session_id = f"web_allocated_{mode}"
    scheduler = _make_scheduler(store, handler=handler, agent_client=client)
    run_id = f"{job.id}:1234"
    await scheduler.on_wake(job, run_id)
    await scheduler.run_tasks[run_id]
    created = next(env for env in client.unary_requests if env.method == "session.create")
    assert created.channel == "web"
    assert created.params["cron_id"] == job.id
    assert created.channel_context["routing"]["group_id"] == "group"
    assert created.channel_context["routing"]["bot_id"] == "bot"
    assert len(client.stream_requests) == 1
    execution = client.stream_requests[0]
    assert execution.channel == "web"
    assert execution.user_id == "owner"
    assert execution.channel_context["routing"]["group_id"] == "group"
    assert execution.channel_context["routing"]["bot_id"] == "bot"
    assert execution.session_id == scheduler.runs[run_id].exec_session_id
    assert execution.channel_context["cron"]["job_id"] == job.id
    final = [msg for msg in handler.published if (msg.payload or {}).get("event_type") == "chat.final"]
    assert len(final) == 1
    assert final[0].session_id == execution.session_id
    assert final[0].metadata["routing"]["group_id"] == "group"
    for _, _, event in list(scheduler.events):
        if event.kind == "push_update":
            await scheduler.handle_event(event)
    assert len([msg for msg in handler.published if (msg.payload or {}).get("event_type") == "chat.final"]) == 1
    notices = [msg.payload for msg in handler.published if (msg.payload or {}).get("event_type") == "cron.run.updated"]
    assert notices == []

# --- from test_feishu_lifecycle_delivery.py ---

def channel(callback):
    result = object.__new__(FeishuChannel)
    result.config = SimpleNamespace(app_id="app", agent_id="agent")
    result._main_loop = asyncio.get_running_loop()
    result._persistent_group_fanout_callback = callback
    return result

def joined():
    return P2ImChatMemberBotAddedV1({
        "schema": "2.0",
        "header": {"event_id": "join-event", "create_time": str(int(time.time() * 1000)),
                   "event_type": "im.chat.member.bot.added_v1"},
        "event": {"chat_id": "silent-chat", "name": "Silent group"},
    })

@pytest.mark.asyncio
async def test_silent_joins_are_acknowledged_without_registering_groups_or_requests(tmp_path, monkeypatch):
    from jiuwenswarm.server import agent_ws_server as module
    from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
    from agent_gateway.message_handler.message_handler import MessageHandler
    from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import FeishuBinding
    from tests.unit_tests.gateway.test_persistent_group_fanout import _Host, NS

    host = _Host(tmp_path)
    agent = host.colleague("Silent group tester")
    binding = FeishuBinding(binding_id="binding", platform="feishu", app_id="app", namespace_id=NS, agent_id=agent)
    fanout = host.fanout([binding])
    server = object.__new__(module.AgentWebSocketServer)
    server._ensure_persistent_group_fanout = AsyncMock(return_value=fanout)
    wire = AsyncMock()
    monkeypatch.setattr(module, "send_wire_payload", wire)
    handler = object.__new__(MessageHandler)

    async def send(envelope):
        await server.dispatch_parsed_request(object(), e2a_to_agent_request(envelope), asyncio.Lock())
        return SimpleNamespace(ok=True, payload={"disposition": "handled"})

    handler.agent_client = SimpleNamespace(send_request=send)
    c = channel(handler.handle_persistent_feishu_event)
    c.config.agent_id = agent
    try:
        for index in (1, 2):
            data = P2ImChatMemberBotAddedV1({
                "schema": "2.0", "header": {"event_id": f"join-{index}",
                    "create_time": str(int(time.time() * 1000)), "event_type": "im.chat.member.bot.added_v1"},
                "event": {"chat_id": f"silent-{index}", "name": f"Silent {index}"},
            })
            await c._on_persistent_change(data, "joined")
        rows = host.groups.directory(NS, agent)
        assert rows == ()
        assert host.groups.active_accesses(NS, agent) == ()
        assert host.row_counts() == (0, 0, 0)
        assert wire.await_count == 2
    finally:
        await host.close()

# --- from test_pod_http_client_stream_cancel.py ---

def _legacy_chunk(request_id: str, event_type: str, **extra: Any) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "channel_id": "web",
        "payload": {"event_type": event_type, **extra},
        "is_complete": False,
        "metadata": {},
        "sequence": 0,
    }

def _envelope(request_id: str, *, input_mode: str | None = None) -> E2AEnvelope:
    params: dict[str, Any] = {"session_id": "sess-1", "content": "x"}
    if input_mode:
        params["input_mode"] = input_mode
        params["expected_execution_id"] = "exec-1"
    return E2AEnvelope(
        method="chat.send",
        session_id="sess-1",
        request_id=request_id,
        channel="web",
        user_id="u1",
        params=params,
        is_stream=True,
    )

class _FakeResponse:
    def __init__(
        self,
        frames: list[dict[str, Any]],
        *,
        gate: asyncio.Event | None = None,
        entered: asyncio.Event | None = None,
    ) -> None:
        self.status_code = 200
        self._frames = frames
        self._gate = gate
        self._entered = entered

    async def aiter_lines(self) -> AsyncIterator[str]:
        import json

        if self._entered is not None:
            self._entered.set()
        if self._gate is not None:
            await self._gate.wait()
        for frame in self._frames:
            yield f"data: {json.dumps(frame, ensure_ascii=False)}"
            yield ""

    async def aread(self) -> bytes:
        return b""

@pytest.mark.asyncio
async def test_steer_stream_does_not_supersede_parent_input_received(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同 session 开 steer 短流时，父流随后到达的 chat.input_received 不得被丢弃。"""
    parent_rid = "req_parent_1"
    steer_rid = "req_steer_2"
    parent_gate = asyncio.Event()
    parent_entered = asyncio.Event()
    parent_frames = [
        _legacy_chunk(parent_rid, "chat.output_phase", output_phase_id="p1"),
        _legacy_chunk(
            parent_rid,
            "chat.input_received",
            input_request_id=steer_rid,
            content="2",
            timestamp=1_793_671_642_014,
        ),
        _legacy_chunk(parent_rid, "chat.reasoning", content="..."),
    ]
    steer_frames = [
        _legacy_chunk(steer_rid, "runtime.accepted", input_boundary="stream"),
        {
            "request_id": steer_rid,
            "channel_id": "web",
            "payload": None,
            "is_complete": True,
            "metadata": {},
            "sequence": 1,
        },
    ]

    client = HttpSseAgentServerClient(timeout_s=30.0)
    client._base_url = "http://agentserver.test"
    client._api_root = "http://agentserver.test/api/v1"
    client._server_ready = True
    client._running = True

    streams: dict[str, _FakeResponse] = {
        parent_rid: _FakeResponse(
            parent_frames, gate=parent_gate, entered=parent_entered
        ),
        steer_rid: _FakeResponse(steer_frames),
    }

    @asynccontextmanager
    async def fake_stream(verb: str, url: str, **kwargs: Any):
        body = kwargs.get("json") or {}
        rid = str(body.get("request_id") or "")
        yield streams[rid]

    fake_http = AsyncMock()
    fake_http.stream = fake_stream
    client._http = fake_http
    client._owns_http = False

    async def _assemble(envelope: E2AEnvelope, api_root: str):
        return SimpleNamespace(
            verb="POST",
            url=f"{api_root}/chat/completions",
            headers={},
            json_body={"request_id": envelope.request_id},
            query=None,
            used_rpc_fallback=False,
        )

    monkeypatch.setattr(pod_http, "_assemble_with_expert_packages", _assemble)
    monkeypatch.setattr(
        client,
        "_request_headers",
        lambda headers=None: dict(headers or {}),
    )

    parent_events: list[str] = []

    async def read_parent() -> None:
        async for chunk in client.send_request_stream(_envelope(parent_rid)):
            payload = chunk.payload if isinstance(chunk.payload, dict) else {}
            parent_events.append(str(payload.get("event_type") or ""))

    parent_task = asyncio.create_task(read_parent())
    await asyncio.wait_for(parent_entered.wait(), timeout=2)

    steer_events: list[str] = []
    async for chunk in client.send_request_stream(
        _envelope(steer_rid, input_mode="steer")
    ):
        payload = chunk.payload if isinstance(chunk.payload, dict) else {}
        if payload.get("event_type"):
            steer_events.append(str(payload["event_type"]))

    assert steer_events == ["runtime.accepted"]
    assert not client._should_drop_stream_chunk(
        envelope_rid=parent_rid, chunk_rid=parent_rid
    )

    parent_gate.set()
    await parent_task
    assert parent_events == [
        "chat.output_phase",
        "chat.input_received",
        "chat.reasoning",
    ]

@pytest.mark.asyncio
async def test_finished_stream_still_drops_only_its_residual_chunks() -> None:
    client = HttpSseAgentServerClient(timeout_s=30.0)
    rid = "req_done_1"
    peer = "req_peer_2"

    await client._mark_stream_cancelled(rid)

    assert client._should_drop_stream_chunk(envelope_rid=rid, chunk_rid=rid)
    assert not client._should_drop_stream_chunk(envelope_rid=peer, chunk_rid=peer)

# --- from test_swarmflow_reply_forward.py ---

class _FakeAgentClient:
    """Captures the forwarded env (proves the reply reached the AgentServer)."""

    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send_request(self, env: object) -> SimpleNamespace:
        self.sent.append(env)
        return SimpleNamespace(
            request_id="r1",
            channel_id="tui",
            ok=True,
            payload={"ok": True},
            metadata=None,
        )

    @staticmethod
    async def send_request_stream(env: object):  # pragma: no cover
        if False:
            yield env

class _TestMessageHandler(MessageHandler):
    @classmethod
    def create(cls) -> "_TestMessageHandler":
        setattr(MessageHandler, "_instance", None)
        setattr(cls, "_instance", None)
        return cls(_FakeAgentClient())

    async def cancel_stream_tasks_for_channel(self, msg: Message) -> int:
        return await getattr(self, "_cancel_stream_tasks_for_channel")(msg)

def _swarmflow_reply_message(*, session_id: str = "sess-1") -> Message:
    return Message(
        id="r-sw",
        type="req",
        channel_id="tui",
        session_id=session_id,
        params={
            "session_id": session_id,
            "run_id": "run-1",
            "correlation_id": "review:host:0",
            "answer": "ok",
        },
        timestamp=0.0,
        ok=True,
        req_method=ReqMethod.CHAT_SWARMFLOW_REPLY,
        is_stream=False,
    )

@pytest.mark.asyncio
async def test_swarmflow_reply_does_not_cancel_existing_stream() -> None:
    """The cancel gate is CHAT_SEND-only; a swarmflow reply leaves streams alive.

    ``_cancel_stream_tasks_for_channel`` is a generic by-channel tool; the real
    gate is ``_should_cancel_existing_stream_before_chat_send`` (called inside
    ``_forward_loop`` before cancelling). Pinning the gate here proves a
    swarmflow reply never triggers cancellation of an in-flight leader stream.
    """
    assert MessageHandler._should_cancel_existing_stream_before_chat_send(
        _swarmflow_reply_message()
    ) is False
    # Contrast: a plain chat.send on the same channel WOULD cancel.
    chat_send = Message(
        id="r-cs", type="req", channel_id="tui", session_id="sess-1",
        params={"mode": "agent.plan", "query": "hi"}, timestamp=0.0, ok=True,
        req_method=ReqMethod.CHAT_SEND, is_stream=True,
    )
    assert MessageHandler._should_cancel_existing_stream_before_chat_send(chat_send) is True

@pytest.mark.asyncio
async def test_swarmflow_reply_forwarded_to_agent_server_non_stream() -> None:
    """_process_non_stream_request forwards the reply env to the AgentServer client."""
    handler = _TestMessageHandler.create()
    msg = _swarmflow_reply_message()
    env = e2a_from_agent_fields(
        request_id=msg.id,
        channel_id=msg.channel_id,
        session_id=msg.session_id,
        req_method=ReqMethod.CHAT_SWARMFLOW_REPLY,
        params=msg.params,
        is_stream=False,
        timestamp=0.0,
    )
    await handler._process_non_stream_request(msg, env)  # pylint: disable=protected-access

    client = handler.agent_client  # type: ignore[attr-defined]
    assert len(client.sent) == 1
    forwarded = client.sent[0]
    # The forwarded envelope preserves the swarmflow reply method + params.
    assert forwarded.method == ReqMethod.CHAT_SWARMFLOW_REPLY.value
    assert forwarded.params["correlation_id"] == "review:host:0"
    assert forwarded.params["answer"] == "ok"

