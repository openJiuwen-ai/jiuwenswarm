"""S1 host acceptance: Web private messages run through core's real execution entries.

Only the model is local; the colleague service, AgentManager, every host port and core's
session state record (PA-10) and window assembly (PA-06) are the real ones over one SQLite file.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path

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
from openjiuwen.core.foundation.llm.schema.message import ToolMessage, UsageMetadata, UserMessage
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.harness.persistent_agent import EventType
from openjiuwen.harness.schema.deep_agent_spec import (
    ModelSpec,
    TeamModelConfig,
    _TOOL_PROVIDER_REGISTRY,
)

from jiuwenswarm.server.jiuwenmate import service as service_module

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


class LocalModel:
    """Answers each call with its ordinal in two fragments and keeps the messages it was sent."""

    model_client_config = None
    model_config = None

    def __init__(self) -> None:
        self.calls: list[list] = []
        self.hold: asyncio.Event | None = None

    async def invoke(self, messages, **kwargs):
        raise AssertionError("core streams the native agent")

    async def stream(self, messages, **kwargs):
        self.calls.append(list(messages))
        yield AssistantMessageChunk(content=f"第{len(self.calls)}次")
        if self.hold is not None:
            await self.hold.wait()
        yield AssistantMessageChunk(
            content="回复",
            finish_reason="stop",
            usage_metadata=UsageMetadata(input_tokens=3, output_tokens=4),
        )


class Server:
    def __init__(self, tmp_path, *, manager_factory=None) -> None:
        self.tmp_path = tmp_path
        self.db = tmp_path / "agents.sqlite3"
        self.pushed: list[dict] = []
        if manager_factory is None:

            def manager_factory(service, on_event):
                return AgentManager.create_default(service, tmp_path, on_event=on_event)

        self.adapter = JiuwenMateWebAdapter(
            self._service,
            model_catalog=dict,
            manager_factory=manager_factory,
            push=self._push,
        )

    async def _push(self, message: dict) -> bool:
        self.pushed.append(message["payload"])
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

    async def call(self, method: ReqMethod, **params) -> dict:
        request = AgentRequest(
            request_id="req-1", channel_id="web", req_method=method, params=params, metadata={"ws_id": "ws-1"}
        )
        response = await self.adapter.handle(request)
        assert response.ok, response.payload
        return response.payload

    async def send(self, agent_id: str, text: str) -> str:
        sent = await self.call(
            ReqMethod.JIUWENMATE_CHAT_SEND,
            agent_id=agent_id,
            conversation_id="conv-1",
            text=text,
            message_id=f"msg-{text}",
        )
        return sent["request"]["request_id"]

    async def ask(self, agent_id: str, text: str) -> dict:
        return await self.ended(await self.send(agent_id, text))

    async def ended(self, request_id: str) -> dict:
        for _ in range(500):
            view = (await self.call(ReqMethod.JIUWENMATE_REQUEST_GET, request_id=request_id))["request"]
            if view["status"] == "ended" and not view["running"]:
                return view
            await asyncio.sleep(0.01)
        raise AssertionError(f"request did not end: {view}")


@pytest.fixture
def model(monkeypatch):
    local = LocalModel()
    monkeypatch.setattr(ModelSpec, "build", lambda self: local)
    return local


def user_texts(messages) -> list[str]:
    return [message.content for message in messages if isinstance(message, UserMessage)]


async def test_default_colleague_answers_and_remembers_the_conversation(tmp_path, model):
    server = Server(tmp_path)
    agent_id = (await server.call(ReqMethod.JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT))["colleague"]["agent_id"]

    first = await server.ask(agent_id, "你好")
    second = await server.ask(agent_id, "再说一次")
    await server.adapter.aclose()

    assert (first["outcome"], first["replies"]) == ("completed", ["第1次回复"])
    assert (second["outcome"], second["replies"]) == ("completed", ["第2次回复"])
    assert "你好" in user_texts(model.calls[1]) and "第1次回复" in [m.content for m in model.calls[1]]


async def test_the_conversation_window_survives_a_server_restart(tmp_path, model):
    first = Server(tmp_path)
    agent_id = (await first.call(ReqMethod.JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT))["colleague"]["agent_id"]
    await first.ask(agent_id, "记住数字七")
    await first.adapter.aclose()

    second = Server(tmp_path)
    answered = await second.ask(agent_id, "刚才的数字是多少")
    history = (await second.call(ReqMethod.JIUWENMATE_CHAT_HISTORY, agent_id=agent_id, conversation_id="conv-1"))[
        "requests"
    ]
    await second.adapter.aclose()

    assert answered["replies"] == ["第2次回复"]
    assert "记住数字七" in user_texts(model.calls[1])
    assert [item["text"] for item in history] == ["记住数字七", "刚才的数字是多少"]


async def test_reply_fragments_are_pushed_while_the_model_streams(tmp_path, model):
    server = Server(tmp_path)
    agent_id = (await server.call(ReqMethod.JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT))["colleague"]["agent_id"]

    answered = await server.ask(agent_id, "你好")
    for _ in range(100):
        if any(frame["kind"] == "ended" for frame in server.pushed):
            break
        await asyncio.sleep(0.01)
    await server.adapter.aclose()

    streamed = "".join(frame["text"] for frame in server.pushed if frame["kind"] == "delta")
    assert streamed == answered["replies"][0] == "第1次回复"
    assert [frame["kind"] for frame in server.pushed][-2:] == ["segment", "ended"]


@pytest.mark.parametrize(("reason", "outcome"), [("stop", "stopped"), ("cancel", "cancelled")])
async def test_a_running_reply_is_interrupted_and_the_conversation_continues(tmp_path, model, reason, outcome):
    server = Server(tmp_path)
    agent_id = (await server.call(ReqMethod.JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT))["colleague"]["agent_id"]
    model.hold = asyncio.Event()

    request_id = await server.send(agent_id, "写一篇长文")
    for _ in range(500):
        if any(frame["kind"] == "delta" for frame in server.pushed):
            break
        await asyncio.sleep(0.01)
    controlled = (await server.call(ReqMethod.JIUWENMATE_REQUEST_ABORT, request_id=request_id, reason=reason))[
        "request"
    ]
    interrupted = await server.ended(request_id)
    model.hold = None
    answered = await server.ask(agent_id, "换个问题")
    await server.adapter.aclose()

    assert controlled["control"]["reason"] == reason
    assert (interrupted["outcome"], interrupted["control"]["status"]) == (outcome, "accepted")
    assert answered["outcome"] == "completed"


class ReadingModel(LocalModel):
    """Asks for one file of the office directory, then answers with what the tool returned."""

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path

    async def stream(self, messages, **kwargs):
        self.calls.append(list(messages))
        results = [message.content for message in messages if isinstance(message, ToolMessage)]
        usage = UsageMetadata(input_tokens=3, output_tokens=4)
        if not results:
            arguments = json.dumps({"file_path": self.path})
            call = ToolCall(id="call-1", type="function", name="read_file", arguments=arguments)
            yield AssistantMessageChunk(content="", tool_calls=[call], finish_reason="tool_calls", usage_metadata=usage)
            return
        yield AssistantMessageChunk(content=results[-1], finish_reason="stop", usage_metadata=usage)


@pytest.mark.parametrize(
    ("path", "found"),
    [("资料/周报.md", True), ("../other/secret.md", False)],
)
async def test_a_colleague_reads_only_its_office_directory(tmp_path, monkeypatch, path, found):
    reader = ReadingModel(path)
    monkeypatch.setattr(ModelSpec, "build", lambda self: reader)
    server = Server(tmp_path)
    colleague = (await server.call(ReqMethod.JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT))["colleague"]
    office = Path(colleague["workspace_dir"])
    (office / "资料").mkdir()
    (office / "资料" / "周报.md").write_text("本周完成飞书接入。", encoding="utf-8")
    (office.parent / "other").mkdir()
    (office.parent / "other" / "secret.md").write_text("别人的文件", encoding="utf-8")

    answered = await server.ask(colleague["agent_id"], "看一下周报")
    await server.adapter.aclose()

    reply = answered["replies"][-1]
    assert answered["outcome"] == "completed"
    assert ("本周完成飞书接入。" in reply) is found
    assert "别人的文件" not in reply and str(tmp_path) not in reply


_CONTROLLED_WRITE_PROVIDER = "test.jiuwenmate.controlled_write"
_CONTROLLED_WRITE_NAME = "controlled_write"


class ControlledWriteTool(Tool):
    """Test-only side effect that records whether its intent was already in SQLite."""

    def __init__(self, *, db_path: str, output_path: str) -> None:
        super().__init__(
            card=ToolCard(
                id="controlled-write-test",
                name=_CONTROLLED_WRITE_NAME,
                description="Writes a test marker after checking the durable intent log.",
                input_params={
                    "type": "object",
                    "properties": {"content": {"type": "string"}},
                    "required": ["content"],
                },
            )
        )
        self.db_path = Path(db_path)
        self.output_path = Path(output_path)

    async def invoke(self, inputs, **kwargs):
        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT event_json FROM execution_events WHERE event_type = ?",
                (EventType.TOOL_INTENT.value,),
            ).fetchall()
        intent_was_persisted = any(
            json.loads(row[0])["payload"]["tool_name"] == _CONTROLLED_WRITE_NAME
            for row in rows
        )
        result = {
            "intent_was_persisted": intent_was_persisted,
            "content": inputs["content"],
        }
        self.output_path.write_text(json.dumps(result), encoding="utf-8")
        return result

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


class FailingToolIntentRepository(ExecutionRepository):
    """Real SQLite repository that injects a failure only for the critical intent write."""

    def record_events(self, events, *, now):
        if any(event.event_type == EventType.TOOL_INTENT for event in events):
            raise OSError("injected tool.intent persistence failure")
        super().record_events(events, now=now)


class ControlledWriteModel(LocalModel):
    """Requests one controlled write, then returns a deterministic answer."""

    async def stream(self, messages, **kwargs):
        self.calls.append(list(messages))
        usage = UsageMetadata(input_tokens=3, output_tokens=4)
        if not any(isinstance(message, ToolMessage) for message in messages):
            arguments = json.dumps({"content": "controlled side effect"})
            call = ToolCall(
                id="controlled-write-call",
                type="function",
                name=_CONTROLLED_WRITE_NAME,
                arguments=arguments,
            )
            yield AssistantMessageChunk(
                content="",
                tool_calls=[call],
                finish_reason="tool_calls",
                usage_metadata=usage,
            )
            return
        yield AssistantMessageChunk(
            content="write complete", finish_reason="stop", usage_metadata=usage
        )


def install_controlled_write_tool(
    monkeypatch, db_path: Path, output_path: Path
) -> None:
    """Inject a test-only tool into the published Spec without opening production write capabilities."""

    monkeypatch.setitem(
        _TOOL_PROVIDER_REGISTRY,
        _CONTROLLED_WRITE_PROVIDER,
        lambda params, _context: ControlledWriteTool(**params),
    )
    publish_spec = service_module.publish_spec

    def publish_spec_with_test_tool(agent_id, config_version, spec_fields):
        fields = dict(spec_fields)
        fields["tools"] = [
            {
                "type": _CONTROLLED_WRITE_PROVIDER,
                "params": {"db_path": str(db_path), "output_path": str(output_path)},
            }
        ]
        return publish_spec(agent_id, config_version, fields)

    monkeypatch.setattr(service_module, "publish_spec", publish_spec_with_test_tool)


def read_execution_events_after_restart(db_path: Path, request_id: str):
    """Reopen the database and read the Execution log through the host repository API."""

    repository = ExecutionRepository(db_path)
    request = repository.get_request("local", request_id)
    executions = repository.executions("local", request_id)
    assert len(executions) == 1
    return repository.read_execution_log(
        "local", request.record.session_id, request_id, executions[0].execution_id
    )


async def test_write_intent_is_persisted_before_tool_side_effect_and_survives_restart(
    tmp_path, monkeypatch
):
    output_path = tmp_path / "controlled-write.json"
    install_controlled_write_tool(monkeypatch, tmp_path / "agents.sqlite3", output_path)
    model = ControlledWriteModel()
    monkeypatch.setattr(ModelSpec, "build", lambda self: model)
    server = Server(tmp_path)
    colleague = (await server.call(ReqMethod.JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT))[
        "colleague"
    ]

    request_id = await server.send(colleague["agent_id"], "write-marker")
    answered = await server.ended(request_id)
    await server.adapter.aclose()

    assert answered["outcome"] == "completed"
    assert json.loads(output_path.read_text(encoding="utf-8")) == {
        "intent_was_persisted": True,
        "content": "controlled side effect",
    }
    events = read_execution_events_after_restart(server.db, request_id)
    intents = [event for event in events if event.event_type == EventType.TOOL_INTENT]
    assert len(intents) == 1
    assert intents[0].payload.tool_name == _CONTROLLED_WRITE_NAME


async def test_failed_tool_intent_persistence_prevents_tool_side_effect(
    tmp_path, monkeypatch
):
    output_path = tmp_path / "controlled-write.json"
    install_controlled_write_tool(monkeypatch, tmp_path / "agents.sqlite3", output_path)
    model = ControlledWriteModel()
    monkeypatch.setattr(ModelSpec, "build", lambda self: model)
    db_path = tmp_path / "agents.sqlite3"

    def manager_factory(service, on_event):
        return AgentManager(
            service,
            FailingToolIntentRepository(db_path),
            WindowBackendStore(db_path),
            BudgetRepository(db_path),
            on_event=on_event,
        )

    server = Server(tmp_path, manager_factory=manager_factory)
    colleague = (await server.call(ReqMethod.JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT))[
        "colleague"
    ]

    request_id = await server.send(colleague["agent_id"], "write-marker")
    answered = await server.ended(request_id)
    await server.adapter.aclose()

    assert answered["outcome"] == "failed"
    assert not output_path.exists()
    events = read_execution_events_after_restart(server.db, request_id)
    assert not any(event.event_type == EventType.TOOL_INTENT for event in events)
