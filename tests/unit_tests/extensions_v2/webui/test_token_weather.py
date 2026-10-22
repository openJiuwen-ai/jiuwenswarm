from __future__ import annotations

from pathlib import Path

import pytest
from openjiuwen.core.events import Event, ExtensionEventBus, SessionContext

from jiuwenswarm.extensions_v2.events.context_usage import (
    CONTEXT_USAGE_EVENT,
    ContextUsageUpdate,
    register_context_usage_events,
)
from jiuwenswarm.extensions_v2.management import ExtensionManager
from jiuwenswarm.extensions_v2.webui.constants import WebUIEvent
from jiuwenswarm.extensions_v2.webui.contributions import WebUICatalogService
from jiuwenswarm.extensions_v2.webui.models import RenderNode, RenderTarget, UiActionInput
from jiuwenswarm.extensions_v2.webui.runtime import WebUIRuntime

EXAMPLES = Path(__file__).resolve().parents[4] / "jiuwenswarm" / "extensions_v2" / "examples" / "token_weather"
SLOT_TURN = "message.assistant.meta"
SLOT_SUMMARY = "conversation.input.below"


class FakeTransport:
    def __init__(self) -> None:
        self.frames = []
        self.clears = []
        self.errors = []

    async def send_frame(self, frame) -> bool:
        self.frames.append(frame)
        return True

    async def send_clear(self, frame) -> bool:
        self.clears.append(frame)
        return True

    async def send_error(self, target, code, message="", *, revision=0) -> bool:
        self.errors.append((target, code, message, revision))
        return True


async def _runtime(tmp_path: Path, transport: FakeTransport) -> tuple[ExtensionEventBus, ExtensionManager, WebUIRuntime]:
    bus = ExtensionEventBus()
    manager = ExtensionManager(tmp_path / "store")
    runtime = WebUIRuntime(
        bus=bus,
        transport=transport,
        catalog=WebUICatalogService(manager.active_packages),
    )
    manager.set_inactive_callback(runtime.drop_package)

    def host_event_types(target: ExtensionEventBus) -> None:
        runtime.register_event_types(target)
        register_context_usage_events(target)

    await manager.init(bus, host_event_types=host_event_types, host_handlers=runtime.register_handlers)
    return bus, manager, runtime


def _update(
    request_id: str,
    input_tokens: int,
    output_tokens: int,
    percent: float,
) -> ContextUsageUpdate:
    return ContextUsageUpdate(
        session_id="s1",
        request_id=request_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        cache_tokens=input_tokens // 2,
        cache_hit_rate="50.0%",
        context_window_tokens=200_000,
        usage_percent=percent,
        model="test-model",
    )


def _nodes(node: RenderNode | None):
    if node is None:
        return
    yield node
    for child in node.children:
        yield from _nodes(child)


def _texts(node: RenderNode | None) -> list[str]:
    return [item.props.get("value", "") for item in _nodes(node) if item.component == "Text"]


@pytest.mark.asyncio
async def test_two_rounds_render_turn_and_summary(tmp_path: Path) -> None:
    transport = FakeTransport()
    bus, manager, _runtime_obj = await _runtime(tmp_path, transport)
    assert (await manager.install([EXAMPLES]))[0].status == "SUCCESS"

    await bus.trigger(Event(CONTEXT_USAGE_EVENT, _update("r1", 1_000, 200, 20.0)), SessionContext("s1"))
    await bus.trigger(Event(CONTEXT_USAGE_EVENT, _update("r2", 3_000, 400, 40.0)), SessionContext("s1"))

    turns = [frame for frame in transport.frames if frame.target.slot_id == SLOT_TURN]
    summaries = [frame for frame in transport.frames if frame.target.slot_id == SLOT_SUMMARY]
    assert len(turns) == 2
    assert len(summaries) == 2
    assert turns[-1].target.instance_key == "r2"
    assert "3.4K tok" in _texts(turns[-1].root)
    assert "2 轮 · 累计 4.6K tok · 缓存 50%" in _texts(summaries[-1].root)
    assert "上下文 40% · 窗口 200.0K tok" in _texts(summaries[-1].root)

    before = len(transport.frames)
    await bus.trigger(Event(CONTEXT_USAGE_EVENT, _update("r2", 3_000, 400, 40.0)), SessionContext("s1"))
    assert len(transport.frames) == before


@pytest.mark.asyncio
async def test_ready_restores_known_state_and_ignores_empty_session(tmp_path: Path) -> None:
    transport = FakeTransport()
    bus, manager, runtime = await _runtime(tmp_path, transport)
    assert (await manager.install([EXAMPLES]))[0].status == "SUCCESS"

    idle = await runtime.dispatch(
        UiActionInput(event_type=WebUIEvent.READY.value, target=RenderTarget("s1", SLOT_SUMMARY))
    )
    assert idle is not None and idle.accepted and idle.status == "idle"
    assert transport.frames == []

    await bus.trigger(Event(CONTEXT_USAGE_EVENT, _update("r1", 1_000, 200, 20.0)), SessionContext("s1"))
    transport.frames.clear()

    ready = await runtime.dispatch(
        UiActionInput(event_type=WebUIEvent.READY.value, target=RenderTarget("s1", SLOT_SUMMARY))
    )
    assert ready is not None and ready.accepted and ready.status == "rendered"
    assert [frame.target.slot_id for frame in transport.frames] == [SLOT_SUMMARY]

    transport.frames.clear()
    missing = await runtime.dispatch(
        UiActionInput(event_type=WebUIEvent.READY.value, target=RenderTarget("s1", SLOT_TURN, "", "missing"))
    )
    assert missing is not None and missing.accepted and missing.status == "idle"
    assert transport.frames == []

    turn = await runtime.dispatch(
        UiActionInput(event_type=WebUIEvent.READY.value, target=RenderTarget("s1", SLOT_TURN, "", "r1"))
    )
    assert turn is not None and turn.accepted and turn.status == "rendered"
    assert [(frame.target.slot_id, frame.target.instance_key) for frame in transport.frames] == [
        (SLOT_TURN, "r1")
    ]
