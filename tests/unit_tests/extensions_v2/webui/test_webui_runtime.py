from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.events import Event, ExtensionEventBus, SessionContext

from jiuwenswarm.extensions_v2.event_types.context_usage import register_context_usage_events
from jiuwenswarm.extensions_v2.management import ExtensionManager
from jiuwenswarm.extensions_v2.webui.contributions import WebUICatalogService
from jiuwenswarm.extensions_v2.webui.models import (
    RenderMode,
    RenderNode,
    RenderStatus,
    RenderTarget,
    UiRenderInput,
)
from jiuwenswarm.extensions_v2.webui.registry import ActiveRenderRegistry
from jiuwenswarm.extensions_v2.webui.runtime import WebUIRuntime
from tests.unit_tests.extensions_v2.management.support import write_package

SLOT = "conversation.input.below"
PACKAGE = "webui.token-weather-glass"
INSTANCE = "token-weather.summary"

STATEFUL_EXTENSION = '''\
from dataclasses import replace

from openjiuwen.core.events import Event
from openjiuwen.core.extension.data import StateRef

from jiuwenswarm.extensions_v2.webui.models import RenderNode


PACKAGE_ID = "webui.state-refresh"


class Extension:
    def register(self, registrar):
        registrar.register_event_handler(
            "ui.render", self.render, {"target.package_id": PACKAGE_ID},
        )
        registrar.register_event_handler("test.event", self.update, {})

    async def render(self, event, ops, next_handler):
        ref = StateRef(PACKAGE_ID, event.event_input.target.instance_key)
        snapshot = await ops.state.get(ref)
        value = snapshot.value if snapshot.exists else 0
        request = replace(event.event_input, root=RenderNode("Text", {"value": str(value)}))
        return await next_handler(Event(event.event_type, request))

    async def update(self, event, ops, next_handler):
        await ops.state.set(
            StateRef(PACKAGE_ID, event.event_input["key"]),
            event.event_input["value"],
        )
        return await next_handler(event)


def create_extension():
    return Extension()
'''


class FakeTransport:
    def __init__(self) -> None:
        self.frames = []
        self.clears = []
        self.errors = []
        self.fail = False

    async def send_frame(self, frame) -> bool:
        if self.fail:
            return False
        self.frames.append(frame)
        return True

    async def send_clear(self, frame) -> bool:
        if self.fail:
            return False
        self.clears.append(frame)
        return True

    async def send_error(self, target, code, message="", *, revision=0) -> bool:
        self.errors.append((target, code, message, revision))
        return True


class DelayedTransport(FakeTransport):
    def __init__(self, blocked_instance: str) -> None:
        super().__init__()
        self.blocked_instance = blocked_instance
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def send_frame(self, frame) -> bool:
        if frame.target.instance_key == self.blocked_instance and not self.entered.is_set():
            self.entered.set()
            await self.release.wait()
        return await super().send_frame(frame)


async def _runtime(tmp_path: Path, transport: FakeTransport, registry=None) -> tuple[WebUIRuntime, ExtensionManager]:
    bus = ExtensionEventBus()

    async def test_core(event, ops, next_handler):
        del ops, next_handler
        return event.event_input

    bus.register_event_type("test.event", lambda event_input, pattern: not pattern, test_core)
    manager = ExtensionManager(
        tmp_path / "store",
        report_path=tmp_path / "report.yaml",
        state_dir=tmp_path / "state",
    )
    runtime = WebUIRuntime(
        bus=bus,
        transport=transport,
        registry=registry,
        catalog=WebUICatalogService(manager.active_packages),
    )
    manager.set_inactive_callback(runtime.drop_package)
    def host_event_types(bus: ExtensionEventBus) -> None:
        runtime.register_event_types(bus)
        register_context_usage_events(bus)

    await manager.init(
        bus,
        host_event_types=host_event_types,
        host_handlers=runtime.register_handlers,
    )
    assert manager.state_store is not None
    assert manager.state_dependencies is not None
    runtime.bind_state(manager.state_store, manager.state_dependencies)
    return runtime, manager


async def _wait_for_frames(transport: FakeTransport, count: int) -> None:
    for _ in range(100):
        if len(transport.frames) >= count:
            return
        await asyncio.sleep(0.01)
    assert len(transport.frames) >= count


@pytest.mark.asyncio
async def test_delivery_failure_restores_revision(tmp_path: Path) -> None:
    transport = FakeTransport()
    runtime, _manager = await _runtime(tmp_path, transport)
    target = RenderTarget("s1", SLOT, PACKAGE, INSTANCE)
    root = RenderNode("Text", {"value": "hello"})

    transport.fail = True
    failed = await runtime.render(UiRenderInput(target=target, root=root))
    assert failed is not None and failed.status is RenderStatus.DELIVERY_FAILED
    assert runtime.registry.get(target) is None

    transport.fail = False
    delivered = await runtime.render(UiRenderInput(target=target, root=root))
    assert delivered is not None and delivered.revision == 1


@pytest.mark.asyncio
async def test_clear_removes_instance_and_next_frame_starts_at_one(tmp_path: Path) -> None:
    transport = FakeTransport()
    runtime, _manager = await _runtime(tmp_path, transport)
    target = RenderTarget("s1", SLOT, PACKAGE, INSTANCE)

    await runtime.render(UiRenderInput(target=target, root=RenderNode("Text", {"value": "one"})))
    cleared = await runtime.render(UiRenderInput(target=target, mode=RenderMode.CLEAR))
    assert cleared is not None and cleared.status is RenderStatus.DELIVERED
    assert runtime.registry.get(target) is None

    again = await runtime.render(UiRenderInput(target=target, root=RenderNode("Text", {"value": "two"})))
    assert again is not None and again.revision == 1


@pytest.mark.asyncio
async def test_oversized_frame_is_rejected_without_commit(tmp_path: Path) -> None:
    transport = FakeTransport()
    runtime, _manager = await _runtime(tmp_path, transport)
    target = RenderTarget("s1", SLOT, PACKAGE, INSTANCE)
    children = tuple(RenderNode("Text", {"value": "x" * 4096}) for _ in range(100))
    root = RenderNode("Box", {"direction": "column"}, children=children)

    output = await runtime.render(UiRenderInput(target=target, root=root))
    assert output is not None and output.status is RenderStatus.REJECTED
    assert runtime.registry.get(target) is None
    assert transport.errors


@pytest.mark.asyncio
async def test_package_cleanup_retains_failed_clear_for_retry(tmp_path: Path) -> None:
    transport = FakeTransport()
    runtime, _manager = await _runtime(tmp_path, transport)
    target = RenderTarget("s1", SLOT, PACKAGE, INSTANCE)
    await runtime.render(UiRenderInput(target=target, root=RenderNode("Text", {"value": "hello"})))
    transport.fail = True
    with pytest.raises(RuntimeError, match="Could not clear package view"):
        await runtime.drop_package(PACKAGE)
    assert runtime.registry.contains(target)
    transport.fail = False
    await runtime.drop_package(PACKAGE)
    assert not runtime.registry.contains(target)


@pytest.mark.asyncio
async def test_package_cleanup_forwards_the_retired_generation(tmp_path: Path) -> None:
    transport = FakeTransport()
    runtime, _manager = await _runtime(tmp_path, transport)
    runtime.resources.drop = AsyncMock()
    await runtime.drop_package(PACKAGE, generation=3)
    runtime.resources.drop.assert_awaited_once_with(package_id=PACKAGE, generation=3)


@pytest.mark.asyncio
async def test_reenabled_application_view_resumes_above_package_clear_revision(tmp_path: Path) -> None:
    transport = FakeTransport()
    runtime, _manager = await _runtime(tmp_path, transport)
    target = RenderTarget(
        "",
        "application.sidebar.footer",
        PACKAGE,
        INSTANCE,
        scope="application",
        connection_id="web-1",
    )

    first = await runtime.render(UiRenderInput(target=target, root=RenderNode("Text", {"value": "before"})))
    await runtime.drop_package(PACKAGE)
    clear = transport.clears[-1]

    resumed = await runtime.render(UiRenderInput(target=target, root=RenderNode("Text", {"value": "after"})))

    assert first is not None and first.revision == 1
    assert clear.revision == first.revision + 1
    assert resumed is not None and resumed.revision > clear.revision


@pytest.mark.asyncio
async def test_eviction_happens_after_the_new_frame_is_delivered(tmp_path: Path) -> None:
    transport = FakeTransport()
    registry = ActiveRenderRegistry(max_instances_per_session=1)
    runtime, _manager = await _runtime(tmp_path, transport, registry)
    first = RenderTarget("s1", SLOT, "pkg.a", "a")
    second = RenderTarget("s1", SLOT, "pkg.b", "b")

    await runtime.render(UiRenderInput(target=first, root=RenderNode("Text", {"value": "a"})))
    delivered = await runtime.render(UiRenderInput(target=second, root=RenderNode("Text", {"value": "b"})))

    assert delivered is not None
    assert delivered.revision == transport.frames[-1].revision == 1
    assert runtime.registry.get(first) is None
    assert runtime.registry.get(second) is not None
    assert [frame.target.key for frame in transport.clears] == [first.key]


@pytest.mark.asyncio
async def test_state_set_rebuilds_the_delivered_dependent_target(tmp_path: Path) -> None:
    transport = FakeTransport()
    runtime, manager = await _runtime(tmp_path, transport)
    package = write_package(
        tmp_path / "source",
        package_id="webui.state-refresh",
        module_source=STATEFUL_EXTENSION,
    )
    assert (await manager.install([package]))[0].status == "SUCCESS"
    target = RenderTarget("session-1", SLOT, "webui.state-refresh", "panel")

    try:
        delivered = await runtime.render(
            UiRenderInput(target=target, root=RenderNode("Text", {"value": "placeholder"})),
            SessionContext("session-1"),
        )
        assert delivered is not None and delivered.status is RenderStatus.DELIVERED
        assert transport.frames[-1].root.props["value"] == "0"

        await runtime.bus.trigger(
            Event("test.event", {"key": "panel", "value": 7}),
            SessionContext("session-1"),
        )
        await _wait_for_frames(transport, 2)

        assert transport.frames[-1].target.key == target.key
        assert transport.frames[-1].root.props["value"] == "7"
        assert transport.frames[-1].revision == 2
    finally:
        await runtime.shutdown()
        await manager.shutdown()


@pytest.mark.asyncio
async def test_cleared_target_no_longer_refreshes_after_state_changes(tmp_path: Path) -> None:
    transport = FakeTransport()
    runtime, manager = await _runtime(tmp_path, transport)
    package = write_package(
        tmp_path / "source",
        package_id="webui.state-refresh",
        module_source=STATEFUL_EXTENSION,
    )
    assert (await manager.install([package]))[0].status == "SUCCESS"
    target = RenderTarget("session-1", SLOT, "webui.state-refresh", "panel")

    try:
        await runtime.render(
            UiRenderInput(target=target, root=RenderNode("Text", {"value": "placeholder"})),
            SessionContext("session-1"),
        )
        await runtime.render(UiRenderInput(target=target, mode=RenderMode.CLEAR), SessionContext("session-1"))
        frame_count = len(transport.frames)

        await runtime.bus.trigger(
            Event("test.event", {"key": "panel", "value": 9}),
            SessionContext("session-1"),
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert len(transport.frames) == frame_count
        assert runtime.registry.get(target) is None
    finally:
        await runtime.shutdown()
        await manager.shutdown()


@pytest.mark.asyncio
async def test_concurrent_render_delivery_keeps_state_dependencies_per_target(tmp_path: Path) -> None:
    transport = DelayedTransport("left")
    runtime, manager = await _runtime(tmp_path, transport)
    package = write_package(
        tmp_path / "source",
        package_id="webui.state-refresh",
        module_source=STATEFUL_EXTENSION,
    )
    assert (await manager.install([package]))[0].status == "SUCCESS"
    left = RenderTarget("session-1", SLOT, "webui.state-refresh", "left")
    right = RenderTarget("session-1", SLOT, "webui.state-refresh", "right")
    context = SessionContext("session-1")
    left_render = asyncio.create_task(
        runtime.render(UiRenderInput(left, root=RenderNode("Text", {"value": "left"})), context)
    )

    try:
        await asyncio.wait_for(transport.entered.wait(), 2)
        right_output = await runtime.render(
            UiRenderInput(right, root=RenderNode("Text", {"value": "right"})),
            context,
        )
        transport.release.set()
        left_output = await left_render
        assert right_output is not None and right_output.status is RenderStatus.DELIVERED
        assert left_output is not None and left_output.status is RenderStatus.DELIVERED

        transport.frames.clear()
        await runtime.bus.trigger(
            Event("test.event", {"key": "left", "value": 11}),
            context,
        )
        await _wait_for_frames(transport, 1)
        assert transport.frames[-1].target.key == left.key
        assert transport.frames[-1].root.props["value"] == "11"

        transport.frames.clear()
        await runtime.bus.trigger(
            Event("test.event", {"key": "right", "value": 22}),
            context,
        )
        await _wait_for_frames(transport, 1)
        assert transport.frames[-1].target.key == right.key
        assert transport.frames[-1].root.props["value"] == "22"
    finally:
        transport.release.set()
        await asyncio.gather(left_render, return_exceptions=True)
        await runtime.shutdown()
        await manager.shutdown()
