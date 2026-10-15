"""JM-10: private messages, history and request status through the Web RPCs."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from itertools import count

import pytest
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.jiuwenmate.agent_manager import AgentManager
from jiuwenswarm.server.jiuwenmate.budget_repository import BudgetRepository
from jiuwenswarm.server.jiuwenmate.execution_models import ConversationRef
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
from jiuwenswarm.server.jiuwenmate.repository import PersistentAgentRepository, TemplateStore
from jiuwenswarm.server.jiuwenmate.service import PersistentAgentService
from jiuwenswarm.server.jiuwenmate.web_adapter import (
    LEGACY_CONVERSATION_ID,
    STREAM_EVENT,
    WEB_NAMESPACE,
    JiuwenMateWebAdapter,
    _coalesce,
)

from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.harness import persistent_agent as core
from openjiuwen.harness.schema.deep_agent_spec import TeamModelConfig

SECRET = "sk-test-secret-value"
NOW = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
CONVERSATION = "conv-1"


class FakeModels:
    def default_selection(self):
        return "deepseek#0"

    def resolve(self, selection, *, client_id):
        return TeamModelConfig(
            model_client_config=ModelClientConfig(
                client_id=client_id,
                client_provider="OpenAI",
                api_key=SECRET,
                api_base="https://example.invalid/v1",
                model_name="deepseek",
            ),
            model_request_config=ModelRequestConfig(model="deepseek"),
        )


class ReplyCore:
    """Stand-in for core: proposes a draft and a reply, waits until released, then adopts only the reply.

    ``modes`` maps a message text to ``raise`` (the entry fails) or ``ignored`` (final without outputs).
    ``streams`` maps a message text to the fragments of each model call streamed before waiting.
    """

    def __init__(self) -> None:
        self.modes: dict[str, str] = {}
        self.streams: dict[str, tuple[tuple[str, ...], ...]] = {}
        self.started: list[str] = []
        self._gates: dict[str, asyncio.Event] = {}
        self._stopped: set[str] = set()
        self._request_of: dict[str, str] = {}

    def gate(self, text: str) -> asyncio.Event:
        return self._gates.setdefault(text, asyncio.Event())

    def release(self, *texts: str) -> None:
        for text in texts:
            self.gate(text).set()

    async def abort(self, context, *, reason):
        request_id = context.execution.request_id
        self._stopped.add(request_id)
        for text, event in self._gates.items():
            if self._request_of.get(text) == request_id:
                event.set()

    async def execute(self, spec, context, inputs):
        execution, text = context.execution, inputs.text
        self._request_of[text] = execution.request_id
        self.started.append(text)
        seq = count(1)

        async def emit(event_type, payload, **visibility):
            event = core.AgentEvent.create(
                execution, event_type=event_type, local_seq=next(seq), occurred_at=NOW, payload=payload, **visibility
            )
            await context.ports.commit.record(execution, event)
            return event

        for call, fragments in enumerate(self.streams.get(text, ())):
            for fragment in fragments:
                yield await emit(core.EventType.REPLY_DELTA, {"text": fragment}, visibility="progress")
            usage = {"status": "unknown", "reason": "test"}
            payload = {"model_call_id": f"call-{call}", "config_version": "v1", "usage": usage}
            yield await emit(core.EventType.MODEL_COMPLETED, payload)
        for suffix, body in (("draft", f"草稿：{text}"), ("reply", f"回复：{text}")):
            yield await emit(
                core.EventType.OUTPUT_PROPOSED,
                {
                    "output_id": f"{execution.execution_id}-{suffix}",
                    "target_ref": inputs.reply_target,
                    "share_mode": "message",
                    "text": body,
                },
                visibility="candidate",
            )
        await self.gate(text).wait()
        if execution.request_id in self._stopped:
            yield await emit(core.EventType.EXECUTION_PAUSED, {"reason": "stopped"})
            return
        mode = self.modes.get(text)
        if mode == "raise":
            raise RuntimeError("model crashed")
        if mode == "ignored":
            payload = {"status": "ignored", "reason": "无需回复", "output_refs": ()}
        else:
            payload = {"status": "completed", "output_refs": (f"{execution.execution_id}-reply",)}
        yield await emit(core.EventType.EXECUTION_FINAL, payload)


class Host:
    def __init__(self, tmp_path) -> None:
        self.db = tmp_path / "agents.sqlite3"
        self.tmp_path = tmp_path
        self.core = ReplyCore()
        self.managers: list[AgentManager] = []
        self.pushed: list[dict] = []
        self.adapter = JiuwenMateWebAdapter(
            self._service, model_catalog=dict, manager_factory=self._manager, push=self._push
        )

    async def _push(self, message: dict) -> bool:
        self.pushed.append(message)
        return True

    def _service(self) -> PersistentAgentService:
        ticks = count()
        return PersistentAgentService(
            PersistentAgentRepository(self.db),
            TemplateStore(self.tmp_path / "templates"),
            models=FakeModels(),
            template_source=lambda _: None,
            clock=lambda: NOW + timedelta(seconds=next(ticks)),
            windows=WindowBackendStore(self.db),
        )

    def _manager(self, service: PersistentAgentService, *, on_event=None) -> AgentManager:
        manager = AgentManager(
            service,
            ExecutionRepository(self.db),
            WindowBackendStore(self.db),
            BudgetRepository(self.db),
            execute=self.core.execute,
            abort=self.core.abort,
            reservation_tokens=1000,
            on_event=on_event,
        )
        self.managers.append(manager)
        return manager

    async def call(self, method: ReqMethod, ws_id: str | None = "ws-1", **params):
        metadata = {"ws_id": ws_id} if ws_id else None
        request = AgentRequest(
            request_id="req-1", channel_id="web", req_method=method, params=params, metadata=metadata
        )
        response = await self.adapter.handle(request)
        assert SECRET not in json.dumps(response.payload, ensure_ascii=False)
        return response

    async def colleague(self) -> str:
        settings = {"name": "研发同事", "description": "负责整理研发进展。", "capabilities": {"model": None}}
        created = await self.call(ReqMethod.JIUWENMATE_COLLEAGUE_CREATE, settings=settings)
        return created.payload["colleague"]["agent_id"]

    async def send(
        self,
        agent_id: str,
        text: str,
        message_id: str | None = None,
        ws_id: str | None = "ws-1",
        conversation_id: str = CONVERSATION,
    ):
        return await self.call(
            ReqMethod.JIUWENMATE_CHAT_SEND,
            ws_id=ws_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            text=text,
            message_id=message_id or f"msg-{text}",
        )

    async def history(self, agent_id: str, conversation_id: str = CONVERSATION, ws_id: str | None = "ws-1"):
        return await self.call(
            ReqMethod.JIUWENMATE_CHAT_HISTORY, ws_id=ws_id, agent_id=agent_id, conversation_id=conversation_id
        )

    async def conversations(self, agent_id: str) -> list[dict]:
        listed = await self.call(ReqMethod.JIUWENMATE_CONVERSATION_LIST, agent_id=agent_id)
        assert listed.ok, listed.payload
        return listed.payload["conversations"]

    async def pushed_until_ended(self) -> list[dict]:
        for _ in range(400):
            if any(message["payload"]["kind"] == "ended" for message in self.pushed):
                return self.pushed
            await asyncio.sleep(0.005)
        raise AssertionError(f"stream did not end: {self.pushed}")

    async def request(self, request_id: str, ws_id: str | None = "ws-1") -> dict:
        response = await self.call(ReqMethod.JIUWENMATE_REQUEST_GET, ws_id=ws_id, request_id=request_id)
        return response.payload["request"]

    async def settled(self, request_id: str, ws_id: str | None = "ws-1") -> dict:
        for _ in range(400):
            view = await self.request(request_id, ws_id)
            if view["status"] == "ended" and not view["running"]:
                return view
            await asyncio.sleep(0.005)
        raise AssertionError(f"request did not finish: {view}")


@pytest.fixture
async def host(tmp_path):
    host = Host(tmp_path)
    yield host
    await host.adapter.aclose()


# JM-10-V-01 sending, history and status


async def test_sent_message_returns_its_request_before_the_reply(host):
    agent_id = await host.colleague()

    sent = await host.send(agent_id, "今天进展")

    request = sent.payload["request"]
    assert sent.ok
    assert request["text"] == "今天进展" and request["message_id"] == "msg-今天进展"
    assert request["status"] in ("queued", "dispatched") and request["replies"] == []
    host.core.release("今天进展")
    done = await host.settled(request["request_id"])
    assert done["outcome"] == "completed"
    assert done["replies"] == ["回复：今天进展"]
    assert len(done["deliveries"]) == 1
    assert done["deliveries"][0]["channel"] == "web"
    assert done["deliveries"][0]["status"] == "available"
    assert done["deliveries"][0]["output_id"].endswith("-reply")


async def test_history_lists_the_conversation_in_order(host):
    agent_id = await host.colleague()
    host.core.release("第一条", "第二条")
    first = (await host.send(agent_id, "第一条")).payload["request"]
    second = (await host.send(agent_id, "第二条")).payload["request"]
    await host.settled(second["request_id"])

    history = await host.history(agent_id)

    requests = history.payload["requests"]
    assert [item["request_id"] for item in requests] == [first["request_id"], second["request_id"]]
    assert [item["replies"] for item in requests] == [["回复：第一条"], ["回复：第二条"]]


async def test_history_before_the_first_message_is_empty_and_creates_nothing(host):
    agent_id = await host.colleague()

    history = await host.history(agent_id)

    assert history.ok and history.payload == {"requests": []}
    assert ExecutionRepository(host.db).find_session(WEB_NAMESPACE, agent_id, ConversationRef.web(CONVERSATION)) is None


async def test_resending_a_message_id_returns_the_same_request(host):
    agent_id = await host.colleague()
    host.core.release("今天进展")

    first = (await host.send(agent_id, "今天进展", "msg-1")).payload["request"]
    again = (await host.send(agent_id, "今天进展", "msg-1")).payload["request"]
    await host.settled(first["request_id"])

    assert again["request_id"] == first["request_id"]
    history = await host.history(agent_id)
    assert len(history.payload["requests"]) == 1 and host.core.started == ["今天进展"]


# JM-10-V-03 only adopted outputs are shown as replies


async def test_proposed_outputs_are_not_shown_before_the_final_event(host):
    agent_id = await host.colleague()
    request = (await host.send(agent_id, "今天进展")).payload["request"]
    for _ in range(400):
        if host.core.started:
            break
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.02)

    running = await host.request(request["request_id"])

    assert running["running"] and running["replies"] == [] and running["outcome"] is None
    host.core.release("今天进展")
    done = await host.settled(request["request_id"])
    assert "草稿：今天进展" not in done["replies"]


@pytest.mark.parametrize(("mode", "outcome"), [("ignored", "ignored"), ("raise", "failed")])
async def test_unsuccessful_requests_show_no_reply(host, mode, outcome):
    agent_id = await host.colleague()
    host.core.modes["今天进展"] = mode
    host.core.release("今天进展")

    request = (await host.send(agent_id, "今天进展")).payload["request"]
    done = await host.settled(request["request_id"])

    assert done["outcome"] == outcome and done["replies"] == []


# refusals keep their error codes


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"conversation_id": "c-1", "text": "你好", "message_id": "m-1"}, "BAD_REQUEST"),
        ({"agent_id": "{agent}", "conversation_id": "c-1", "message_id": "m-1"}, "BAD_REQUEST"),
        ({"agent_id": "{agent}", "conversation_id": "c-1", "text": "你好"}, "BAD_REQUEST"),
        ({"agent_id": "{agent}", "conversation_id": "c-1", "text": "你好", "message_id": "has space"}, "BAD_REQUEST"),
        ({"agent_id": "{agent}", "text": "你好", "message_id": "m-1"}, "BAD_REQUEST"),
        ({"agent_id": "{agent}", "conversation_id": "has space", "text": "你好", "message_id": "m-1"}, "BAD_REQUEST"),
        ({"agent_id": "mate_missing", "conversation_id": "c-1", "text": "你好", "message_id": "m-1"}, "NOT_FOUND"),
    ],
)
async def test_refused_messages_keep_their_error_code(host, params, code):
    agent_id = await host.colleague()
    params = {key: value.format(agent=agent_id) for key, value in params.items()}

    response = await host.call(ReqMethod.JIUWENMATE_CHAT_SEND, **params)

    assert not response.ok and response.payload["code"] == code
    assert host.core.started == []


async def test_unknown_request_is_not_found(host):
    response = await host.call(ReqMethod.JIUWENMATE_REQUEST_GET, request_id="req_missing")

    assert not response.ok and response.payload["code"] == "NOT_FOUND"


# runtime lifecycle


async def test_deleting_a_colleague_ends_its_running_request_first(host):
    agent_id = await host.colleague()
    request = (await host.send(agent_id, "今天进展")).payload["request"]
    for _ in range(400):
        if host.core.started:
            break
        await asyncio.sleep(0.005)

    deleted = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_DELETE, agent_id=agent_id)

    assert deleted.ok and deleted.payload == {"deleted": True}
    assert host.managers[0].scheduler.running == []
    missing = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_GET, agent_id=agent_id)
    assert missing.payload["code"] == "NOT_FOUND"
    assert request["status"] in ("queued", "dispatched")


async def test_runtime_starts_once_and_restarts_after_close(host):
    agent_id = await host.colleague()
    await asyncio.gather(*(host.history(agent_id) for _ in range(5)))
    assert len(host.managers) == 1

    await host.adapter.aclose()
    await host.history(agent_id)

    assert len(host.managers) == 2


# JM-10-V-05 streamed replies


async def test_running_request_shows_the_current_model_call_as_draft(host):
    agent_id = await host.colleague()
    host.core.streams["今天进展"] = (("先查一下",), ("**进展**", "如下"))
    request = (await host.send(agent_id, "今天进展")).payload["request"]

    for _ in range(400):
        view = await host.request(request["request_id"])
        if view["draft"] and view["draft"]["text"] == "**进展**如下":
            break
        await asyncio.sleep(0.005)

    assert view["draft"]["text"] == "**进展**如下" and view["replies"] == []
    host.core.release("今天进展")
    done = await host.settled(request["request_id"])
    assert done["draft"] is None and done["replies"] == ["回复：今天进展"]


async def test_fragments_are_pushed_in_order_to_the_sending_connection(host):
    agent_id = await host.colleague()
    host.core.streams["今天进展"] = (("先查", "一下"), ("**进展**", "如下"))
    host.core.release("今天进展")

    request = (await host.send(agent_id, "今天进展")).payload["request"]
    pushed = await host.pushed_until_ended()

    frames = [message["payload"] for message in pushed]
    assert {message["metadata"]["ws_id"] for message in pushed} == {"ws-1"}
    assert {message["channel_id"] for message in pushed} == {"web"}
    assert {frame["event_type"] for frame in frames} == {STREAM_EVENT}
    assert {frame["request_id"] for frame in frames} == {request["request_id"]}
    seqs = [frame["seq"] for frame in frames]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    segments, text = [], ""
    for frame in frames:
        if frame["kind"] == "delta":
            text += frame["text"]
        else:
            segments.append((frame["kind"], text))
            text = ""
    assert segments == [("segment", "先查一下"), ("segment", "**进展**如下"), ("ended", "")]


async def test_stream_goes_to_connections_that_asked_about_the_colleague(host):
    agent_id = await host.colleague()
    await host.history(agent_id, ws_id="ws-2")
    host.core.streams["今天进展"] = (("进展",),)
    host.core.release("今天进展")

    await host.send(agent_id, "今天进展", ws_id=None)
    pushed = await host.pushed_until_ended()

    assert {message["metadata"]["ws_id"] for message in pushed} == {"ws-2"}


async def test_without_a_web_connection_nothing_is_pushed(host):
    agent_id = await host.colleague()
    host.core.streams["今天进展"] = (("进展",),)
    host.core.release("今天进展")

    request = (await host.send(agent_id, "今天进展", ws_id=None)).payload["request"]
    await host.settled(request["request_id"], ws_id=None)
    await asyncio.sleep(0.05)

    assert host.pushed == []


def test_consecutive_fragments_of_one_execution_are_merged():
    def delta(execution_id, seq, text):
        return {"kind": "delta", "execution_id": execution_id, "seq": seq, "text": text}

    segment = {"kind": "segment", "execution_id": "exe_1", "seq": 3, "text": ""}

    merged = _coalesce([delta("exe_1", 1, "进"), delta("exe_1", 2, "展"), segment, delta("exe_1", 4, "如")])

    assert merged == [delta("exe_1", 2, "进展"), segment, delta("exe_1", 4, "如")]
    assert _coalesce([delta("exe_1", 1, "甲"), delta("exe_2", 1, "乙")]) == [
        delta("exe_1", 1, "甲"),
        delta("exe_2", 1, "乙"),
    ]


# JM-10-V-06 several conversations per colleague


async def test_each_conversation_keeps_its_own_history_and_is_listed_latest_first(host):
    agent_id = await host.colleague()
    host.core.release("第一个对话的问题", "第二个对话的问题")

    first = (await host.send(agent_id, "第一个对话的问题", conversation_id="conv-a")).payload
    await host.settled(first["request"]["request_id"])
    second = (await host.send(agent_id, "第二个对话的问题", conversation_id="conv-b")).payload
    await host.settled(second["request"]["request_id"])

    assert first["conversation"]["title"] == "第一个对话的问题"
    assert first["request"]["conversation_id"] == "conv-a"
    listed = await host.conversations(agent_id)
    assert [item["conversation_id"] for item in listed] == ["conv-b", "conv-a"]
    a = (await host.history(agent_id, "conv-a")).payload["requests"]
    b = (await host.history(agent_id, "conv-b")).payload["requests"]
    assert [item["text"] for item in a] == ["第一个对话的问题"]
    assert [item["text"] for item in b] == ["第二个对话的问题"]
    sessions = ExecutionRepository(host.db)
    session_a = sessions.find_session(WEB_NAMESPACE, agent_id, ConversationRef.web("conv-a"))
    session_b = sessions.find_session(WEB_NAMESPACE, agent_id, ConversationRef.web("conv-b"))
    assert session_a.session_id != session_b.session_id


async def test_a_new_conversation_is_listed_only_after_its_first_message(host):
    agent_id = await host.colleague()

    history = await host.history(agent_id, "conv-new")

    assert history.ok and history.payload == {"requests": []}
    assert await host.conversations(agent_id) == []


async def test_the_title_is_the_start_of_the_first_message_and_can_be_renamed(host):
    agent_id = await host.colleague()
    host.core.release("请帮我  整理\n本周研发进展并输出一份详细的周报", "继续")

    await host.send(agent_id, "请帮我  整理\n本周研发进展并输出一份详细的周报", message_id="msg-long")
    await host.send(agent_id, "继续")
    titled = await host.conversations(agent_id)
    renamed = await host.call(
        ReqMethod.JIUWENMATE_CONVERSATION_RENAME, agent_id=agent_id, conversation_id=CONVERSATION, title="  周报  "
    )
    empty = await host.call(
        ReqMethod.JIUWENMATE_CONVERSATION_RENAME, agent_id=agent_id, conversation_id=CONVERSATION, title=" "
    )

    assert [item["title"] for item in titled] == ["请帮我 整理 本周研发进展并输出一份详细"]
    assert renamed.ok and renamed.payload["conversation"]["title"] == "周报"
    assert not empty.ok and empty.payload["code"] == "BAD_REQUEST"
    assert [item["title"] for item in await host.conversations(agent_id)] == ["周报"]


async def test_a_deleted_conversation_is_hidden_and_refuses_new_messages(host):
    agent_id = await host.colleague()
    host.core.release("今天进展")
    request = (await host.send(agent_id, "今天进展")).payload["request"]
    await host.settled(request["request_id"])

    deleted = await host.call(ReqMethod.JIUWENMATE_CONVERSATION_DELETE, agent_id=agent_id, conversation_id=CONVERSATION)
    again = await host.call(ReqMethod.JIUWENMATE_CONVERSATION_DELETE, agent_id=agent_id, conversation_id=CONVERSATION)
    refused = await host.send(agent_id, "还在吗", message_id="msg-again")

    assert deleted.ok and deleted.payload == {"deleted": True}
    assert again.payload["code"] == "NOT_FOUND" and refused.payload["code"] == "NOT_FOUND"
    assert await host.conversations(agent_id) == []
    assert (await host.request(request["request_id"]))["replies"] == ["回复：今天进展"]


async def test_messages_from_the_single_conversation_era_are_listed_as_a_conversation(host):
    agent_id = await host.colleague()
    host.core.release("早先的消息")
    old = (await host.send(agent_id, "早先的消息", conversation_id=LEGACY_CONVERSATION_ID)).payload
    await host.settled(old["request"]["request_id"])
    with sqlite3.connect(host.db) as conn:
        conn.execute("DELETE FROM web_conversations")

    listed = await host.conversations(agent_id)

    assert [(item["conversation_id"], item["title"]) for item in listed] == [(LEGACY_CONVERSATION_ID, "早先的消息")]
    history = (await host.history(agent_id, LEGACY_CONVERSATION_ID)).payload["requests"]
    assert [item["text"] for item in history] == ["早先的消息"]


async def test_deleting_a_colleague_removes_its_conversation_list(host):
    agent_id = await host.colleague()
    host.core.release("今天进展")
    request = (await host.send(agent_id, "今天进展")).payload["request"]
    await host.settled(request["request_id"])

    await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_DELETE, agent_id=agent_id)

    with sqlite3.connect(host.db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM web_conversations").fetchone()[0] == 0


# JM-12-V-05 the page stops a running reply and cancels a queued message


async def test_the_page_stops_a_running_reply_and_cancels_a_queued_one(host):
    agent_id = await host.colleague()
    running = (await host.send(agent_id, "第一条")).payload["request"]["request_id"]
    queued = (await host.send(agent_id, "第二条")).payload["request"]["request_id"]
    for _ in range(400):
        if host.core.started:
            break
        await asyncio.sleep(0.005)

    cancelled = await host.call(ReqMethod.JIUWENMATE_REQUEST_ABORT, request_id=queued, reason="cancel")
    stopped = await host.call(ReqMethod.JIUWENMATE_REQUEST_ABORT, request_id=running, reason="stop")
    done = await host.settled(running)

    assert cancelled.ok and cancelled.payload["request"]["outcome"] == "cancelled"
    assert cancelled.payload["request"]["control"]["reason"] == "cancel"
    assert stopped.ok and stopped.payload["request"]["control"]["reason"] == "stop"
    assert done["outcome"] == "stopped" and done["control"]["status"] == "accepted"
    assert host.core.started == ["第一条"]
    history = (await host.history(agent_id)).payload["requests"]
    assert [item["control"]["reason"] for item in history] == ["stop", "cancel"]


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"request_id": "req_missing", "reason": "stop"}, "NOT_FOUND"),
        ({"request_id": "{request}", "reason": "pause"}, "BAD_REQUEST"),
        ({"reason": "stop"}, "BAD_REQUEST"),
    ],
)
async def test_refused_controls_keep_their_error_code(host, params, code):
    agent_id = await host.colleague()
    request_id = (await host.send(agent_id, "任务")).payload["request"]["request_id"]
    params = {key: value.format(request=request_id) for key, value in params.items()}

    response = await host.call(ReqMethod.JIUWENMATE_REQUEST_ABORT, **params)

    assert not response.ok and response.payload["code"] == code
    host.core.release("任务")
