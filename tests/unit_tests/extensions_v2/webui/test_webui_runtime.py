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

SLOT = "conversation.input.below"
PACKAGE = "webui.token-weather-glass"
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
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
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
