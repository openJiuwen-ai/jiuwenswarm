# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import argparse
import asyncio
import io
from collections.abc import AsyncIterator
from typing import Any

import pytest

from jiuwenswarm.channels.process_cli import app
from jiuwenswarm.channels.process_cli.live_input import (
    LIVE_INPUT_PROMPT,
    LiveInputReceipt,
    LiveSessionInputController,
    PipeLineReader,
    build_steer_request,
    execution_id_from_event,
    observe_steer_stream,
    supports_live_session_input,
)
from jiuwenswarm.channels.process_cli.live_layout import LiveTurnLayout
from jiuwenswarm.channels.process_cli.render import EventRenderer
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.events import RuntimeEvent


class Tty:
    def isatty(self) -> bool:
        return True


class QueueLineReader:
    prompt_safe = False

    def __init__(self) -> None:
        self.values: asyncio.Queue[str | None] = asyncio.Queue()
        self.prompts: list[str] = []
        self.prompted = asyncio.Event()
        self.cancelled = 0

    async def __call__(self, prompt: str) -> str | None:
        self.prompts.append(prompt)
        self.prompted.set()
        try:
            return await self.values.get()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


def root_request() -> AgentRequest:
    return AgentRequest(
        request_id="root-request",
        channel_id="process_cli",
        session_id="runtime-session",
        req_method=ReqMethod.CHAT_SEND,
        is_stream=True,
        timestamp=1.0,
        params={
            "query": "initial",
            "content": "initial",
            "mode": "agent.code.normal",
            "work_mode": "code",
            "cwd": "owned-cwd",
            "project_dir": "owned-project",
            "trusted_dirs": ["owned-trusted"],
            "supports_user_interaction": True,
            "untrusted": "must-not-copy",
        },
    )


def event(event_type: str, **payload: Any) -> RuntimeEvent:
    return RuntimeEvent(
        request_id="root-request",
        channel_id="process_cli",
        session_id="runtime-session",
        payload={"event_type": event_type, **payload},
    )


@pytest.mark.parametrize(
    ("mode", "enabled"),
    [
        ("code.normal", True),
        ("agent.code.normal", True),
        ("agent.work.normal", True),
        ("agent.code.plan", False),
        ("team.work.normal", False),
    ],
)
def test_live_input_only_supports_human_tty_single_agent_normal(
    mode: str,
    enabled: bool,
) -> None:
    args = argparse.Namespace(output="human", _operation="chat", mode=mode)
    assert supports_live_session_input(args, stdin=Tty(), stdout=Tty()) is enabled
    args.output = "json"
    assert not supports_live_session_input(args, stdin=Tty(), stdout=Tty())


def test_build_steer_request_copies_only_owned_bindings() -> None:
    request = build_steer_request(
        root_request(),
        text="change the title",
        execution_id="execution-1",
    )

    assert request.request_id.startswith("steer-")
    assert request.req_method is ReqMethod.CHAT_SEND
    assert request.channel_id == "process_cli"
    assert request.session_id == "runtime-session"
    assert request.is_stream is True
    assert request.params == {
        "query": "change the title",
        "content": "change the title",
        "input_mode": "steer",
        "expected_execution_id": "execution-1",
        "mode": "agent.code.normal",
        "work_mode": "code",
        "cwd": "owned-cwd",
        "project_dir": "owned-project",
        "trusted_dirs": ["owned-trusted"],
        "supports_user_interaction": True,
    }


def test_execution_id_requires_non_empty_runtime_payload() -> None:
    assert execution_id_from_event(
        event("chat.delta", execution_id=" execution-1 ")
    ) == ("execution-1")
    assert execution_id_from_event(event("chat.delta", execution_id="")) is None
    assert execution_id_from_event(event("chat.delta")) is None


@pytest.mark.asyncio
async def test_pipe_line_reader_accepts_parent_forwarded_input() -> None:
    reader = PipeLineReader(io.BytesIO("启动前补充\n".encode()))

    assert await reader("ignored prompt") == "启动前补充"
    assert await reader("ignored prompt") is None
    await reader.close()


def test_human_layout_orders_request_processing_output_and_input() -> None:
    stderr = io.StringIO()
    renderer = EventRenderer(
        "human",
        stdout=io.StringIO(),
        stderr=stderr,
    )

    renderer.live_input_ready("写一段短文\n主题关于宇宙")
    layout = stderr.getvalue() + LIVE_INPUT_PROMPT

    assert "  写一段短文\n  主题关于宇宙" in layout
    assert layout.index("请求") < layout.index("处理")
    assert layout.index("处理") < layout.index("模型输出")
    assert layout.index("模型输出") < layout.index("输入栏")


def test_parent_live_turn_keeps_inputs_output_and_editor_separate() -> None:
    layout = LiveTurnLayout("写一段关于宇宙的短文")
    layout.add_supplement("主题改成花园")
    layout.apply_receipt("accepted")
    layout.append_output("花园里开满了花。")

    rendered = "".join(text for _style, text in layout.message())

    assert (
        rendered.index("写一段关于宇宙的短文")
        < rendered.index("✓ 主题改成花园")
        < rendered.index("• JiuwenSwarm")
        < rendered.index("花园里开满了花。")
        < rendered.rindex("› ")
    )


def test_renderer_separates_consecutive_model_generations() -> None:
    stdout = io.StringIO()
    renderer = EventRenderer("human", stdout=stdout, stderr=io.StringIO())

    renderer.render(event("chat.delta", delta="第一轮末尾。"))
    renderer.render(
        event(
            "chat.delta",
            delta="第二轮开头。",
            steering_generation_start=True,
        )
    )

    assert stdout.getvalue() == "第一轮末尾。\n\n第二轮开头。"


async def stream(*events: RuntimeEvent) -> AsyncIterator[RuntimeEvent]:
    for item in events:
        yield item


@pytest.mark.asyncio
async def test_steer_receipts_distinguish_accept_reject_and_unknown() -> None:
    accepted = await observe_steer_stream(stream(event("runtime.accepted")))
    idle = await observe_steer_stream(
        stream(event("runtime.accepted", input_delivery="chat"))
    )
    unknown = await observe_steer_stream(stream(event("chat.delta", delta="ignored")))

    assert accepted == LiveInputReceipt("accepted", "补充输入已受理")
    assert idle.status == "rejected"
    assert unknown.status == "unknown"


class BlockingClient:
    def __init__(self) -> None:
        self.requests: list[AgentRequest] = []
        self.delivery_started = asyncio.Event()
        self.release_delivery = asyncio.Event()

    async def _events(self, request: AgentRequest) -> AsyncIterator[RuntimeEvent]:
        self.requests.append(request)
        self.delivery_started.set()
        await self.release_delivery.wait()
        yield event("runtime.accepted")

    def stream(self, request: AgentRequest) -> AsyncIterator[RuntimeEvent]:
        return self._events(request)


class ImmediateClient:
    def __init__(self) -> None:
        self.requests: list[AgentRequest] = []

    async def _events(self, request: AgentRequest) -> AsyncIterator[RuntimeEvent]:
        self.requests.append(request)
        yield event("runtime.accepted")

    def stream(self, request: AgentRequest) -> AsyncIterator[RuntimeEvent]:
        return self._events(request)


@pytest.mark.asyncio
async def test_controller_accepts_input_immediately_then_binds_before_delivery() -> (
    None
):
    client = BlockingClient()
    reader = QueueLineReader()
    receipts: list[LiveInputReceipt] = []
    ready: list[bool] = []
    controller = LiveSessionInputController(
        client=client,  # type: ignore[arg-type]
        root_request=root_request(),
        read_line=reader,
        on_ready=lambda: ready.append(True),
        on_receipt=receipts.append,
        delivery_timeout=1,
    )
    controller.start()
    await asyncio.wait_for(reader.prompted.wait(), timeout=1)
    assert ready == [True]
    assert reader.prompts == [LIVE_INPUT_PROMPT]

    reader.values.put_nowait("change the title")
    await asyncio.sleep(0)
    assert client.requests == []

    rendered: list[str] = []
    await controller.render_root_event(lambda: rendered.append("processing"))
    assert rendered == ["processing"]

    controller.observe(event("chat.delta", execution_id="execution-1"))
    await asyncio.wait_for(client.delivery_started.wait(), timeout=1)
    await controller.render_root_event(lambda: rendered.append("root delta"))
    assert rendered == ["processing", "root delta"]
    assert receipts == []

    client.release_delivery.set()
    for _ in range(20):
        if receipts:
            break
        await asyncio.sleep(0)
    assert receipts == [LiveInputReceipt("accepted", "补充输入已受理")]
    assert client.requests[0].params["expected_execution_id"] == "execution-1"
    await controller.close()


@pytest.mark.asyncio
async def test_controller_delivers_multiple_lines_serially_with_unique_requests() -> (
    None
):
    client = ImmediateClient()
    reader = QueueLineReader()
    receipts: list[LiveInputReceipt] = []
    controller = LiveSessionInputController(
        client=client,  # type: ignore[arg-type]
        root_request=root_request(),
        read_line=reader,
        on_ready=lambda: None,
        on_receipt=receipts.append,
        delivery_timeout=1,
    )
    controller.start()
    reader.values.put_nowait("first steer")
    reader.values.put_nowait("second steer")
    controller.observe(event("chat.delta", execution_id="execution-1"))

    for _ in range(50):
        if len(receipts) == 2:
            break
        await asyncio.sleep(0)

    assert [request.params["query"] for request in client.requests] == [
        "first steer",
        "second steer",
    ]
    assert len({request.request_id for request in client.requests}) == 2
    assert [receipt.status for receipt in receipts] == ["accepted", "accepted"]
    await controller.close()


@pytest.mark.asyncio
async def test_pause_cancels_only_prompt_and_resume_waits_for_runtime_event() -> None:
    client = BlockingClient()
    reader = QueueLineReader()
    controller = LiveSessionInputController(
        client=client,  # type: ignore[arg-type]
        root_request=root_request(),
        read_line=reader,
        on_ready=lambda: None,
        on_receipt=lambda _receipt: None,
        delivery_timeout=1,
    )
    controller.start()
    await asyncio.wait_for(reader.prompted.wait(), timeout=1)

    await controller.pause()
    assert reader.cancelled == 1
    controller.resume_after_event()
    await asyncio.sleep(0)
    assert len(reader.prompts) == 1

    reader.prompted.clear()
    controller.observe(event("chat.delta", execution_id="execution-1"))
    await asyncio.wait_for(reader.prompted.wait(), timeout=1)
    assert len(reader.prompts) == 2
    await controller.close()


class AppClient:
    latest: AppClient | None = None

    def __init__(self) -> None:
        type(self).latest = self
        self.calls: list[str] = []

    async def start(self) -> None:
        self.calls.append("start")

    async def create_or_resume_session(
        self,
        *,
        channel_id: str,
        session_id: str | None,
    ) -> str:
        self.calls.append("session")
        return session_id or "runtime-session"

    async def _events(self, request: AgentRequest) -> AsyncIterator[RuntimeEvent]:
        self.calls.append("stream")
        yield RuntimeEvent(
            request_id=request.request_id,
            channel_id=request.channel_id,
            session_id=request.session_id,
            payload={
                "event_type": "chat.delta",
                "delta": "hello",
                "execution_id": "execution-1",
            },
        )
        final = event("chat.final", content="hello", execution_id="execution-1")
        final.is_complete = True
        yield final

    def stream(self, request: AgentRequest) -> AsyncIterator[RuntimeEvent]:
        return self._events(request)

    async def cleanup_session(self, *, channel_id: str, session_id: str) -> bool:
        self.calls.append("cleanup")
        return True

    async def close(self) -> None:
        self.calls.append("close")


class AppController:
    latest: AppController | None = None

    def __init__(self, **kwargs: Any) -> None:
        type(self).latest = self
        self.root_request = kwargs["root_request"]
        self.observed: list[str] = []
        self.started = False
        self.paused = 0
        self.closed = False

    def start(self) -> None:
        self.started = True

    def observe(self, current: RuntimeEvent) -> None:
        self.observed.append(current.event_type)

    async def render_root_event(self, render) -> None:
        render()

    async def pause(self) -> None:
        self.paused += 1

    def resume_after_event(self) -> None:
        return

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_app_installs_live_input_on_the_same_runtime_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    AppClient.latest = None
    AppController.latest = None
    monkeypatch.setattr(app, "InProcessRuntimeClient", AppClient)
    monkeypatch.setattr(app, "LiveSessionInputController", AppController)
    monkeypatch.setattr(app, "TtyLineReader", lambda: object())
    monkeypatch.setattr(app, "PipeLineReader", lambda: object())
    monkeypatch.setattr(
        app, "supports_live_session_input", lambda *args, **kwargs: True
    )
    args = argparse.Namespace(
        cwd=str(tmp_path),
        project_dir=str(tmp_path),
        trusted_dir=[],
        mode="code.normal",
        work_mode="code",
        prompt="initial",
        output="human",
        session=None,
        timeout=None,
        show_reasoning=False,
        show_tools=False,
        _interactive_worker=True,
        _forwarded_live_input=True,
        _operation="chat",
        _session_result_file=None,
        _worker_result_file=None,
    )

    result = await app.run(args, stdout=io.StringIO(), stderr=io.StringIO())

    assert result == 0
    assert AppClient.latest is not None
    assert AppClient.latest.calls == ["start", "session", "stream", "cleanup", "close"]
    assert AppController.latest is not None
    assert AppController.latest.root_request.session_id == "runtime-session"
    assert AppController.latest.started
    assert AppController.latest.observed == ["chat.delta", "chat.final"]
    assert AppController.latest.paused == 1
    assert AppController.latest.closed
