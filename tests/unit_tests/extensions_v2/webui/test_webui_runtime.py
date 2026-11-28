from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.events import Event, ExtensionEventBus, SessionContext
from openjiuwen.core.extension.data import StateAddress

from jiuwenswarm.extensions_v2.event_types.context_usage import register_context_usage_events
from jiuwenswarm.extensions_v2.management import ExtensionManager
from jiuwenswarm.extensions_v2.webui.contributions import WebUICatalogService
from jiuwenswarm.extensions_v2.webui.models import (
    RenderMode,
    RenderNode,
    RenderStatus,
    RenderTarget,
    UiActionInput,
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

from jiuwenswarm.extensions_v2.webui.models import RenderNode, UiRenderInput


PACKAGE_ID = "webui.state-refresh"


def counter(value):
    return RenderNode(
        "Button", {"value": str(value)}, events={"ui.click": {"id": "counter", "args": {}}},
    )


class Extension:
    def register(self, registrar):
        registrar.register_event_handler(
            "ui.render", self.render, {"target.package_id": PACKAGE_ID},
        )
        registrar.register_event_handler(
            "ui.click", self.click, {"target.package_id": PACKAGE_ID},
        )
        registrar.register_event_handler("test.event", self.update, {})

    async def render(self, event, ops, next_handler):
        request = event.event_input
        uses_prebuilt_tree = request.target.instance_key == "prebuilt"
        root = request.root
        if uses_prebuilt_tree and root is None:
            return await next_handler(event)
        if not uses_prebuilt_tree and root is not None:
            return await next_handler(event)
        ref = StateRef(PACKAGE_ID, request.target.instance_key)
        snapshot = await ops.state.get(ref)
        value = snapshot.value if snapshot.exists else 0
        if root is None:
            root = counter(value)
        else:
            props = dict(root.props)
            props["value"] = str(value)
            root = replace(root, props=props)
        request = replace(request, root=root)
        return await next_handler(Event(event.event_type, request))

    async def click(self, event, ops, next_handler):
        receipt = await next_handler(event)
        if not receipt.accepted:
            return receipt
        target = event.event_input.target
        if target.instance_key == "explicit-only":
            await ops.ui.render(UiRenderInput(target))
            await ops.ui.render(UiRenderInput(target))
        else:
            ref = StateRef(PACKAGE_ID, target.instance_key)
            snapshot = await ops.state.get(ref)
            value = (snapshot.value if snapshot.exists else 0) + 1
            await ops.state.set(ref, value)
            visible_value = f"manual {value}" if target.instance_key == "different-tree" else value
            await ops.ui.render(UiRenderInput(target, root=counter(visible_value)))
        return receipt

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


async def _mount_counter(
    tmp_path: Path, transport: FakeTransport, instance_key: str = "panel", *, root: RenderNode | None = None,
) -> tuple[WebUIRuntime, ExtensionManager, RenderTarget, SessionContext]:
    runtime, manager = await _runtime(tmp_path, transport)
    package = write_package(tmp_path / "source", package_id="webui.state-refresh", module_source=STATEFUL_EXTENSION)
    assert (await manager.install([package]))[0].status == "SUCCESS"
    target = RenderTarget("session-1", SLOT, "webui.state-refresh", instance_key)
    context = SessionContext(target.session_id)
    result = await runtime.render(UiRenderInput(target, root=root), context)
    assert result is not None and result.status is RenderStatus.DELIVERED, transport.errors
    assert [(frame.revision, frame.root.props["value"]) for frame in transport.frames] == [(1, "0")]
    return runtime, manager, target, context


def _counter_click(target: RenderTarget, revision: int) -> UiActionInput:
    return UiActionInput("ui.click", target, {
        "node_key": "0", "action_id": "counter", "args": {}, "data": {}, "revision": revision,
    })


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
@pytest.mark.parametrize("cancel", [False, True])
async def test_absent_application_clear_failure_restores_revision(tmp_path: Path, cancel: bool) -> None:
    transport = FakeTransport()
    runtime, _manager = await _runtime(tmp_path, transport)
    target = RenderTarget("", "application.sidebar.footer", PACKAGE, INSTANCE,
                          scope="application", connection_id="web-1")
    first = await runtime.render(UiRenderInput(target=target, mode=RenderMode.CLEAR))
    assert first is not None and first.revision == 1

    if cancel:
        async def cancel_clear(frame):
            raise asyncio.CancelledError()

        transport.send_clear = cancel_clear
        with pytest.raises(asyncio.CancelledError):
            await runtime.render(UiRenderInput(target=target, mode=RenderMode.CLEAR))
    else:
        transport.fail = True
        failed = await runtime.render(UiRenderInput(target=target, mode=RenderMode.CLEAR))
        assert failed is not None and failed.status is RenderStatus.DELIVERY_FAILED
        transport.fail = False

    result = await runtime.render(UiRenderInput(target=target, root=RenderNode("Text", {"value": "visible"})))
    assert result is not None and result.revision == 2
    assert transport.frames[-1].revision > transport.clears[-1].revision


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
@pytest.mark.parametrize(
    ("slot_id", "limit"),
    [
        ("conversation.header.title.after", 2),
        ("conversation.header.actions", 3),
    ],
)
async def test_header_slot_instance_limit_rejects_new_instances(
    tmp_path: Path,
    slot_id: str,
    limit: int,
) -> None:
    transport = FakeTransport()
    runtime, _manager = await _runtime(tmp_path, transport)
    targets = [
        RenderTarget("s1", slot_id, f"pkg.{index}", "main")
        for index in range(limit)
    ]

    for index, target in enumerate(targets):
        delivered = await runtime.render(
            UiRenderInput(target=target, root=RenderNode("Text", {"value": str(index)}))
        )
        assert delivered is not None and delivered.status is RenderStatus.DELIVERED

    blocked = RenderTarget("s1", slot_id, "pkg.overflow", "main")
    rejected = await runtime.render(
        UiRenderInput(target=blocked, root=RenderNode("Text", {"value": "blocked"}))
    )

    assert rejected is not None and rejected.status is RenderStatus.REJECTED
    assert runtime.registry.get(blocked) is None
    assert transport.errors == []

    updated = await runtime.render(
        UiRenderInput(target=targets[0], root=RenderNode("Text", {"value": "updated"}))
    )
    assert updated is not None and updated.status is RenderStatus.DELIVERED


@pytest.mark.asyncio
async def test_sidebar_footer_limits_new_instances_per_connection(tmp_path: Path) -> None:
    transport = FakeTransport()
    runtime, _manager = await _runtime(tmp_path, transport)
    slot_id = "application.sidebar.footer"
    root = RenderNode("Text", {"value": "footer"})
    accepted = [
        RenderTarget(
            "", slot_id, f"pkg.{index}", "footer",
            scope="application", connection_id="web-1",
        )
        for index in range(3)
    ]
    blocked = RenderTarget(
        "", slot_id, "pkg.overflow", "footer",
        scope="application", connection_id="web-1",
    )
    other = RenderTarget(
        "", slot_id, "pkg.other", "footer",
        scope="application", connection_id="web-2",
    )

    for index, target in enumerate(accepted):
        delivered = await runtime.render(
            UiRenderInput(target=target, root=RenderNode("Text", {"value": str(index)}))
        )
        assert delivered is not None and delivered.status is RenderStatus.DELIVERED

    rejected = await runtime.render(UiRenderInput(target=blocked, root=root))
    isolated = await runtime.render(UiRenderInput(target=other, root=root))
    updated = await runtime.render(
        UiRenderInput(target=accepted[0], root=RenderNode("Text", {"value": "updated"}))
    )

    assert rejected is not None and rejected.status is RenderStatus.REJECTED
    assert isolated is not None and isolated.status is RenderStatus.DELIVERED
    assert updated is not None and updated.status is RenderStatus.DELIVERED
    assert runtime.registry.get(blocked) is None
    assert transport.errors == []


@pytest.mark.asyncio
async def test_cancelled_header_render_releases_slot_capacity(tmp_path: Path) -> None:
    transport = DelayedTransport(blocked_instance="cancelled")
    runtime, _manager = await _runtime(tmp_path, transport)
    slot_id = "conversation.header.title.after"
    cancelled = RenderTarget("s1", slot_id, PACKAGE, "cancelled")
    root = RenderNode("Text", {"value": "title"})
    task = asyncio.create_task(runtime.render(UiRenderInput(target=cancelled, root=root)))
    try:
        await asyncio.wait_for(transport.entered.wait(), timeout=2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert runtime.registry.get(cancelled) is None
    targets = [RenderTarget("s1", slot_id, PACKAGE, key) for key in ("first", "second")]
    for target in targets:
        result = await runtime.render(UiRenderInput(target=target, root=root))
        assert result is not None and result.status is RenderStatus.DELIVERED

    assert [frame.target.key for frame in transport.frames] == [target.key for target in targets]


@pytest.mark.asyncio
async def test_header_cleanup_at_capacity_does_not_evict_other_instances(tmp_path: Path) -> None:
    transport = FakeTransport()
    registry = ActiveRenderRegistry(max_instances_per_session=2)
    runtime, _manager = await _runtime(tmp_path, transport, registry=registry)
    slot_id = "conversation.header.title.after"
    root = RenderNode("Text", {"value": "title"})
    targets = [RenderTarget("s1", slot_id, PACKAGE, key) for key in ("first", "second")]
    for target in targets:
        result = await runtime.render(UiRenderInput(target=target, root=root))
        assert result is not None and result.status is RenderStatus.DELIVERED

    missing = RenderTarget("s1", slot_id, PACKAGE, "missing")
    for _ in range(2):
        result = await runtime.render(UiRenderInput(target=missing, mode=RenderMode.CLEAR))
        assert result is not None and result.status is RenderStatus.DELIVERED

    assert [instance.target.key for instance in registry.snapshot()] == [target.key for target in targets]
    assert [frame.target.key for frame in transport.clears] == [missing.key, missing.key]


@pytest.mark.asyncio
async def test_state_set_rebuilds_the_delivered_dependent_target(tmp_path: Path) -> None:
    transport = FakeTransport()
    runtime, manager, target, context = await _mount_counter(tmp_path, transport)

    try:
        for value in (1, 2):
            await runtime.bus.trigger(Event("test.event", {"key": target.instance_key, "value": value}), context)
            await _wait_for_frames(transport, value + 1)
        assert [(frame.revision, frame.root.props["value"]) for frame in transport.frames] == [
            (1, "0"), (2, "1"), (3, "2"),
        ]
    finally:
        await runtime.shutdown()
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("explicit_first", "different_tree"),
    [(True, False), (False, False), (False, True)],
    ids=["explicit-first", "automatic-first", "automatic-first-with-different-tree"],
)
async def test_counter_click_preserves_state_and_delivers_expected_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_first: bool, different_tree: bool,
) -> None:
    transport = FakeTransport()
    instance_key = "different-tree" if different_tree else "panel"
    runtime, manager, target, context = await _mount_counter(tmp_path, transport, instance_key)
    explicit_completed = asyncio.Event()
    refresh_target = runtime._refresh_state_target
    render_with_route = runtime._render_with_route
    frames_before_click = len(transport.frames)

    async def wait_for_refresh() -> None:
        refresh = runtime._state_refresh_tasks.get(target.key)
        if refresh is not None:
            await asyncio.wait_for(refresh, timeout=2)

    async def refresh_after_explicit(target_key: str) -> None:
        await explicit_completed.wait()
        await refresh_target(target_key)

    async def render_after_automatic(
        request: UiRenderInput,
        session_context: SessionContext | None,
        *,
        connection_id: str,
        user_id: str,
        channel_id: str,
    ) -> object:
        if request.root is not None:
            await _wait_for_frames(transport, frames_before_click + 1)
            await wait_for_refresh()
        return await render_with_route(
            request, session_context, connection_id=connection_id, user_id=user_id, channel_id=channel_id,
        )

    if explicit_first:
        monkeypatch.setattr(runtime, "_refresh_state_target", refresh_after_explicit)
    else:
        monkeypatch.setattr(runtime, "_render_with_route", render_after_automatic)

    try:
        assert manager.state_store is not None
        address = StateAddress(target.package_id, target.instance_key, "session", target.session_id)
        for value in (1, 2):
            frames_before_click = len(transport.frames)
            revision = transport.frames[-1].revision
            explicit_completed.clear()
            result = await runtime.dispatch(_counter_click(target, revision))
            assert result is not None and result.accepted and result.status == "handled"
            explicit_completed.set()
            await wait_for_refresh()

            snapshot = await manager.state_store.get(address)
            assert (snapshot.exists, snapshot.value, snapshot.version) == (True, value, value)
            expected = [(revision + 1, str(value))]
            if different_tree:
                expected.append((revision + 2, f"manual {value}"))
            new_frames = transport.frames[frames_before_click:]
            assert [(frame.revision, frame.root.props["value"]) for frame in new_frames] == expected

        frames_after_clicks = len(transport.frames)
        revision = transport.frames[-1].revision
        await runtime.bus.trigger(Event("test.event", {"key": target.instance_key, "value": 3}), context)
        await _wait_for_frames(transport, frames_after_clicks + 1)
        await wait_for_refresh()
        assert len(transport.frames) == frames_after_clicks + 1
        assert (transport.frames[-1].revision, transport.frames[-1].root.props["value"]) == (revision + 1, "3")
        assert not transport.errors
    finally:
        explicit_completed.set()
        await runtime.shutdown()
        await manager.shutdown()


@pytest.mark.asyncio
async def test_state_refresh_supplies_the_prebuilt_tree_to_a_render_handler(tmp_path: Path) -> None:
    transport = FakeTransport()
    initial_tree = RenderNode("Button", {"value": "initial", "variant": "outlined"})
    runtime, manager, target, context = await _mount_counter(tmp_path, transport, "prebuilt", root=initial_tree)

    try:
        await runtime.bus.trigger(Event("test.event", {"key": target.instance_key, "value": 1}), context)
        await _wait_for_frames(transport, 2)
        assert [frame.revision for frame in transport.frames] == [1, 2]
        assert transport.frames[-1].root.props == {"value": "1", "variant": "outlined"}
        assert not transport.errors
    finally:
        await runtime.shutdown()
        await manager.shutdown()


@pytest.mark.asyncio
async def test_click_without_state_write_delivers_both_explicit_frames(tmp_path: Path) -> None:
    transport = FakeTransport()
    runtime, manager, target, _context = await _mount_counter(tmp_path, transport, "explicit-only")

    try:
        result = await runtime.dispatch(_counter_click(target, 1))
        assert result is not None and result.accepted and result.status == "handled"
        assert [(frame.revision, frame.root.props["value"]) for frame in transport.frames] == [
            (1, "0"), (2, "0"), (3, "0"),
        ]
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
            UiRenderInput(target=target),
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
        runtime.render(UiRenderInput(left), context)
    )

    try:
        await asyncio.wait_for(transport.entered.wait(), 2)
        right_output = await runtime.render(
            UiRenderInput(right),
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
