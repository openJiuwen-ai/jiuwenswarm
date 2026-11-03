# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team extension wiring: bootstrap registrations and the bus injection."""

from __future__ import annotations

from pathlib import Path

import pytest
from openjiuwen.core.events import Event, ExtensionEventBus, SessionContext
from openjiuwen.core.events.types import OPS_BUILD_EVENT_TYPE

from jiuwenswarm.extensions_v2 import assembly, bootstrap


class _NullWebUI:
    """WebUI stand-in: the bootstrap callbacks only pass it through."""

    def register_event_types(self, bus: ExtensionEventBus) -> None:
        del bus

    def register_handlers(self, bus: ExtensionEventBus) -> None:
        del bus


def _ready_bus() -> ExtensionEventBus:
    """Assemble the host bus the way the manager initializes it."""
    bus = ExtensionEventBus()
    webui = _NullWebUI()
    bootstrap.register_event_types(bus, webui)  # type: ignore[arg-type]
    bootstrap.register_handlers(bus, webui)  # type: ignore[arg-type]
    return bus


def test_bootstrap_registers_team_event_types() -> None:
    """The team events are on the bus; handlers can be registered for them.

    ``register_event_handler`` raises on an unregistered event type — both
    registrations going through is the assertion.
    """
    from openjiuwen.agent_teams.extensions.events import TEAM_BUILD_POLICY, TEAM_MONITOR_EVENT

    bus = _ready_bus()
    bus.register_event_handler(TEAM_MONITOR_EVENT, _noop_handler, {})
    bus.register_event_handler(TEAM_BUILD_POLICY, _noop_handler, {})


async def _noop_handler(event, ops, next_handler):  # pragma: no cover - registration smoke
    del event, ops, next_handler
    return None


@pytest.mark.asyncio
async def test_bootstrap_contributes_team_ops_namespace(tmp_path: Path, monkeypatch) -> None:
    """One ops.build retains team Ops and event-backed session queries."""

    async def _no_database():
        """Host stand-in without team storage: every team is not found."""
        return None

    # Keep the unit test off the real host config and database: wiring
    # only needs the namespace to exist and unknown teams to error.
    monkeypatch.setattr(bootstrap, "_team_db_factory", _no_database)

    from openjiuwen.agent_teams.extensions.models import TeamMonitorEventInput
    from openjiuwen.agent_teams.monitor.models import MonitorEvent, MonitorEventType

    bus = _ready_bus()
    builder = await bus.trigger(Event(OPS_BUILD_EVENT_TYPE, None))
    assert builder is not None
    ops = builder.build(None, SessionContext(session_id="session-1"))

    # The namespace exists; without team storage configured the resolver
    # reports the team as not found (an explicit error, never a crash).
    from openjiuwen.core.common.exception.errors import BaseError

    with pytest.raises(BaseError):
        await ops.team.tasks()

    # And the monitor event chain works end to end through the registered CORE.
    from openjiuwen.agent_teams.extensions.events import TEAM_MONITOR_EVENT
    from openjiuwen.agent_teams.extensions.models import TeamMonitorEventOutput

    monitor_input = TeamMonitorEventInput(
        team_name="research-team",
        member_name=None,
        monitor_event=MonitorEvent(
            event_type=MonitorEventType.TASK_CREATED,
            team_name="research-team",
            task_id="t1",
            timestamp=1,
        ),
    )
    output = await bus.trigger(
        Event(TEAM_MONITOR_EVENT, monitor_input),
        SessionContext(session_id="session-1"),
    )
    assert isinstance(output, TeamMonitorEventOutput)


@pytest.mark.asyncio
async def test_assembly_injects_the_bus_into_the_team_manager(tmp_path: Path, monkeypatch) -> None:
    """install_extensions hands the assembled bus to the team runtime manager."""
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path / "store")

    from openjiuwen.core.runner.runner import GLOBAL_RUNNER

    manager = GLOBAL_RUNNER._get_team_runtime_manager()
    previous = manager.extension_bus
    try:
        instance = await assembly.install_extensions()
        assert instance is not None
        assert manager.extension_bus is instance.bus
    finally:
        manager.set_extension_bus(previous)
        await assembly.shutdown_extensions()
