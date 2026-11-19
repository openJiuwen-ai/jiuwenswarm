"""JM-10-V-02: a private message outlives the page that sent it, and survives an AgentServer restart.

The page holds no connection to the Execution: it sends, leaves, and later reads the
Request again. Core's execution entries are replaced by a stand-in that replies when
released; everything else is the real host stack over one SQLite file.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from itertools import count

import pytest
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.jiuwenmate.agent_manager import AgentManager
from jiuwenswarm.server.jiuwenmate.budget_repository import BudgetRepository
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
from jiuwenswarm.server.jiuwenmate.repository import PersistentAgentRepository, TemplateStore
from jiuwenswarm.server.jiuwenmate.service import PersistentAgentService
from jiuwenswarm.server.jiuwenmate.web_adapter import JiuwenMateWebAdapter

from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.harness import persistent_agent as core
from openjiuwen.harness.schema.deep_agent_spec import TeamModelConfig

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)


class FakeModels:
    def default_selection(self):
        return "deepseek#0"

    def resolve(self, selection, *, client_id):
        return TeamModelConfig(
            model_client_config=ModelClientConfig(
                client_id=client_id,
                client_provider="OpenAI",
                api_key="sk-test",
                api_base="https://example.invalid/v1",
                model_name="deepseek",
            ),
            model_request_config=ModelRequestConfig(model="deepseek"),
        )


class ReplyCore:
    """Replies to a message once it is released; a stop pauses the Execution instead."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.released: dict[str, asyncio.Event] = {}
        self.stopped: set[str] = set()
        self._wake: dict[str, asyncio.Event] = {}

    def release(self, text: str) -> None:
        self.released.setdefault(text, asyncio.Event()).set()

    async def abort(self, context, *, reason):
        self.stopped.add(context.execution.execution_id)
        self._wake.setdefault(context.execution.execution_id, asyncio.Event()).set()

    async def execute(self, spec, context, inputs):
        execution, text = context.execution, inputs.text
        self.started.append(text)
        wake = self._wake.setdefault(execution.execution_id, asyncio.Event())
        released = self.released.setdefault(text, asyncio.Event())
        waiters = [asyncio.ensure_future(wake.wait()), asyncio.ensure_future(released.wait())]
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        for waiter in waiters:
            waiter.cancel()
        if execution.execution_id in self.stopped:
            events = [(core.EventType.EXECUTION_PAUSED, {"reason": "stopped"}, {})]
        else:
            output_id = f"{execution.execution_id}-reply"
            events = [
                (
                    core.EventType.OUTPUT_PROPOSED,
                    {
                        "output_id": output_id,
                        "target_ref": inputs.reply_target,
                        "share_mode": "message",
                        "text": f"回复：{text}",
                    },
                    {"visibility": "candidate"},
                ),
                (core.EventType.EXECUTION_FINAL, {"status": "completed", "output_refs": (output_id,)}, {}),
            ]
        for seq, (event_type, payload, extra) in enumerate(events, start=1):
            event = core.AgentEvent.create(
                execution, event_type=event_type, local_seq=seq, occurred_at=NOW, payload=payload, **extra
            )
            await context.ports.commit.record(execution, event)
            yield event


class Server:
    """One AgentServer process: its own adapter over the shared data directory."""

    def __init__(self, tmp_path, core_fake: ReplyCore) -> None:
        self.db = tmp_path / "agents.sqlite3"
        self.tmp_path = tmp_path
        self.core = core_fake
        self.adapter = JiuwenMateWebAdapter(self._service, model_catalog=dict, manager_factory=self._manager)

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
        return AgentManager(
            service,
            ExecutionRepository(self.db),
            WindowBackendStore(self.db),
            BudgetRepository(self.db),
            execute=self.core.execute,
            abort=self.core.abort,
            reservation_tokens=1000,
            on_event=on_event,
        )

    async def call(self, method: ReqMethod, **params) -> dict:
        request = AgentRequest(request_id="req-1", channel_id="web", req_method=method, params=params)
        response = await self.adapter.handle(request)
        assert response.ok, response.payload
        return response.payload

    async def colleague(self) -> str:
        settings = {"name": "研发同事", "description": "负责整理研发进展。", "capabilities": {"model": None}}
        return (await self.call(ReqMethod.JIUWENMATE_COLLEAGUE_CREATE, settings=settings))["colleague"]["agent_id"]

    async def send(self, agent_id: str, text: str) -> str:
        sent = await self.call(
            ReqMethod.JIUWENMATE_CHAT_SEND,
            agent_id=agent_id,
            conversation_id="conv-1",
            text=text,
            message_id=f"msg-{text}",
        )
        return sent["request"]["request_id"]

    async def history(self, agent_id: str) -> dict[str, dict]:
        payload = await self.call(ReqMethod.JIUWENMATE_CHAT_HISTORY, agent_id=agent_id, conversation_id="conv-1")
        return {item["text"]: item for item in payload["requests"]}

    async def until(self, request_id: str, predicate) -> dict:
        for _ in range(400):
            view = (await self.call(ReqMethod.JIUWENMATE_REQUEST_GET, request_id=request_id))["request"]
            if predicate(view):
                return view
            await asyncio.sleep(0.005)
        raise AssertionError(f"request never reached the expected state: {view}")


@pytest.fixture
def core_fake():
    return ReplyCore()


async def test_reply_is_found_after_the_page_comes_back(tmp_path, core_fake):
    server = Server(tmp_path, core_fake)
    agent_id = await server.colleague()
    request_id = await server.send(agent_id, "今天进展")
    await server.until(request_id, lambda view: view["running"])

    # The page has gone; the Execution keeps running and finishes on its own.
    core_fake.release("今天进展")
    reconnected = await server.until(request_id, lambda view: view["status"] == "ended")

    assert reconnected["outcome"] == "completed" and reconnected["replies"] == ["回复：今天进展"]
    assert (await server.history(agent_id))["今天进展"]["replies"] == ["回复：今天进展"]
    await server.adapter.aclose()


async def test_queued_message_runs_after_the_server_restarts(tmp_path, core_fake):
    first = Server(tmp_path, core_fake)
    agent_id = await first.colleague()
    running_id = await first.send(agent_id, "第一条")
    await first.until(running_id, lambda view: view["running"])
    queued_id = await first.send(agent_id, "第二条")

    await first.adapter.aclose()

    second = Server(tmp_path, core_fake)
    history = await second.history(agent_id)
    stopped, left = history["第一条"], history["第二条"]
    assert stopped["status"] == "ended" and stopped["outcome"] == "stopped" and stopped["replies"] == []
    assert left["request_id"] == queued_id and left["status"] != "ended"
    core_fake.release("第二条")
    done = await second.until(queued_id, lambda view: view["status"] == "ended")

    assert done["outcome"] == "completed" and done["replies"] == ["回复：第二条"]
    await second.adapter.aclose()
