from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from openjiuwen.core.events import OPS_BUILD_EVENT_TYPE, Event, ExtensionEventBus, HandlerLevel
from openjiuwen.core.sys_operation.cwd import get_cwd, init_cwd, set_cwd

from jiuwenswarm.extensions_v2 import assembly
from jiuwenswarm.extensions_v2.event_types.context_usage import (
    CONTEXT_USAGE_EVENT,
    ContextUsageOutput,
    ContextUsageUpdate,
    notify_context_usage,
    register_context_usage_events,
)
from jiuwenswarm.extensions_v2.handler_ops.session_ops import register_session_ops


@pytest.mark.asyncio
async def test_core_handler_accepts_usage_update() -> None:
    bus = ExtensionEventBus()
    register_context_usage_events(bus)
    await bus.trigger(Event(OPS_BUILD_EVENT_TYPE, None))

    output = await bus.trigger(
        Event(
            CONTEXT_USAGE_EVENT,
            ContextUsageUpdate(session_id="s1", request_id="r1", input_tokens=10, total_tokens=10),
        )
    )

    assert output == ContextUsageOutput(accepted=True, request_id="r1")


@pytest.mark.asyncio
async def test_core_handler_rejects_unknown_payload() -> None:
    bus = ExtensionEventBus()
    register_context_usage_events(bus)
    await bus.trigger(Event(OPS_BUILD_EVENT_TYPE, None))

    with pytest.raises(TypeError):
        await bus.trigger(Event(CONTEXT_USAGE_EVENT, object()))


@pytest.mark.asyncio
async def test_usage_notification_binds_workspace_before_scheduling(tmp_path: Path, monkeypatch) -> None:
    bus = ExtensionEventBus()
    register_context_usage_events(bus)
    register_session_ops(bus)
    observed = []

    async def observe(event, ops, next_handler):
        observed.append((ops.session_context.session_id, await ops.session.cwd(), await ops.session.root()))
        return await next_handler(event)

    bus.register_event_handler(CONTEXT_USAGE_EVENT, observe, {}, HandlerLevel.USER)
    await bus.trigger(Event(OPS_BUILD_EVENT_TYPE, None))
    monkeypatch.setattr(assembly, "get_extension_bus", lambda: bus)
    project = tmp_path / "project"
    cwd = project / "src"
    cwd.mkdir(parents=True)

    async def publish():
        init_cwd(str(project), project_root=str(project))
        set_cwd(str(cwd))
        task = notify_context_usage(
            ContextUsageUpdate("s1", "r1", total_tokens=1),
            cwd=get_cwd(),
            project_root=str(project),
        )
        assert task is not None
        set_cwd(str(tmp_path))
        await task

    await asyncio.create_task(publish())

    assert observed == [("s1", str(cwd), str(project))]


@pytest.mark.asyncio
async def test_existing_usage_publishers_keep_missing_workspace_facts(monkeypatch) -> None:
    bus = ExtensionEventBus()
    register_context_usage_events(bus)
    register_session_ops(bus)
    observed = []

    async def observe(event, ops, next_handler):
        observed.append((await ops.session.cwd(), await ops.session.root()))
        return await next_handler(event)

    bus.register_event_handler(CONTEXT_USAGE_EVENT, observe, {}, HandlerLevel.USER)
    await bus.trigger(Event(OPS_BUILD_EVENT_TYPE, None))
    monkeypatch.setattr(assembly, "get_extension_bus", lambda: bus)
    task = notify_context_usage(ContextUsageUpdate("s1", "r1", total_tokens=1))
    assert task is not None
    await task

    assert observed == [(None, None)]
