from __future__ import annotations

from pathlib import Path

import pytest
from openjiuwen.core.events import ExtensionEventBus

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

EXAMPLES = Path(__file__).resolve().parents[4] / "jiuwenswarm" / "extensions_v2" / "examples" / "token_weather"
SLOT = "conversation.input.below"
PACKAGE = "webui.token-weather"
INSTANCE = "token-weather.summary"


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


async def _runtime(tmp_path: Path, transport: FakeTransport, registry=None) -> tuple[WebUIRuntime, ExtensionManager]:
    bus = ExtensionEventBus()
    manager = ExtensionManager(tmp_path / "store")
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
    return runtime, manager


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
async def test_uninstall_clears_views_of_the_package(tmp_path: Path) -> None:
    transport = FakeTransport()
    runtime, manager = await _runtime(tmp_path, transport)
    await manager.install([EXAMPLES])
    target = RenderTarget("s1", SLOT, PACKAGE, INSTANCE)
    await runtime.render(UiRenderInput(target=target, root=RenderNode("Text", {"value": "hello"})))
    assert runtime.registry.get(target) is not None

    report = await manager.uninstall(PACKAGE)
    assert report.status == "SUCCESS"
    assert runtime.registry.get(target) is None
    assert [frame.target.key for frame in transport.clears] == [target.key]


@pytest.mark.asyncio
async def test_eviction_happens_after_the_new_frame_is_delivered(tmp_path: Path) -> None:
    transport = FakeTransport()
    registry = ActiveRenderRegistry(max_instances_per_session=1)
    runtime, _manager = await _runtime(tmp_path, transport, registry)
    first = RenderTarget("s1", SLOT, "pkg.a", "a")
    second = RenderTarget("s1", SLOT, "pkg.b", "b")

    await runtime.render(UiRenderInput(target=first, root=RenderNode("Text", {"value": "a"})))
    await runtime.render(UiRenderInput(target=second, root=RenderNode("Text", {"value": "b"})))

    assert runtime.registry.get(first) is None
    assert runtime.registry.get(second) is not None
    assert [frame.target.key for frame in transport.clears] == [first.key]
