# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace capabilities through real Ops assembly and extension loading."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from openjiuwen.core.events import (
    OPS_BUILD_EVENT_TYPE,
    Event,
    EventHandlerOpsBuilder,
    ExtensionEventBus,
    SessionContext,
)

from jiuwenswarm.extensions_v2 import assembly
from jiuwenswarm.extensions_v2.handler_ops.session_ops import register_session_ops, session_cwd, session_root
from .management.support import write_package


WORKSPACE_EXTENSION = """\
import asyncio


class Extension:
    def register(self, registrar):
        registrar.register_event_handler("test.workspace", self.handle, {})

    async def handle(self, event, ops, next_handler):
        cwd = await ops.session.cwd()
        await asyncio.sleep(0)
        return {
            "session_id": ops.session_context.session_id if ops.session_context else None,
            "cwd": cwd,
            "root": await ops.session.root(),
            "ui_available": callable(ops.ui.render),
            "package_id": ops.package_metadata.package_id,
        }


def create_extension():
    return Extension()
"""


async def _workspace_core(event, ops, next_handler):
    return None


@pytest.fixture
async def workspace_host(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path / "store")
    instance = await assembly.install_extensions()
    assert instance is not None
    instance.bus.register_event_type("test.workspace", lambda value, pattern: True, _workspace_core)
    package = write_package(
        tmp_path / "source",
        package_id="workspace.reader",
        module_source=WORKSPACE_EXTENSION,
    )
    try:
        reports = await instance.manager.install([package])
        assert len(reports) == 1
        assert reports[0].status == "SUCCESS"
        yield instance
    finally:
        await assembly.shutdown_extensions()


@pytest.mark.asyncio
async def test_production_assembly_exposes_distinct_workspace_paths(workspace_host, tmp_path: Path) -> None:
    project = tmp_path / "project"
    cwd = project / "src"
    cwd.mkdir(parents=True)
    context = SessionContext("s1", str(cwd), {"project_root": str(project)})

    result = await workspace_host.bus.trigger(Event("test.workspace", {"cwd": str(tmp_path)}), context)

    assert result == {
        "session_id": "s1",
        "cwd": str(cwd),
        "root": str(project),
        "ui_available": True,
        "package_id": "workspace.reader",
    }


@pytest.mark.asyncio
async def test_projectless_session_does_not_invent_project_root(workspace_host, tmp_path: Path) -> None:
    cwd = tmp_path / "task" / "work"
    cwd.mkdir(parents=True)

    result = await workspace_host.bus.trigger(
        Event("test.workspace", {"project_root": str(tmp_path)}),
        SessionContext("projectless", str(cwd)),
    )

    assert result["cwd"] == str(cwd)
    assert result["root"] is None


@pytest.mark.asyncio
async def test_missing_workspace_facts_do_not_fall_back_to_process_directory(workspace_host) -> None:
    result = await workspace_host.bus.trigger(Event("test.workspace", {}), SessionContext("cold-session"))

    assert result["cwd"] is None
    assert result["root"] is None


@pytest.mark.asyncio
async def test_concurrent_invocations_keep_their_own_session_and_paths(workspace_host, tmp_path: Path) -> None:
    contexts = [
        SessionContext(
            f"s{index}",
            str(tmp_path / f"project{index}" / "src"),
            {"project_root": str(tmp_path / f"project{index}")},
        )
        for index in range(2)
    ]

    results = await asyncio.gather(*(
        workspace_host.bus.trigger(Event("test.workspace", {}), context)
        for context in contexts
    ))

    assert [(result["session_id"], result["cwd"], result["root"]) for result in results] == [
        (context.session_id, context.cwd, context.metadata["project_root"])
        for context in contexts
    ]
    assert not (tmp_path / "project0").exists()
    assert not (tmp_path / "project1").exists()


@pytest.mark.asyncio
async def test_nested_event_does_not_replace_outer_workspace(workspace_host, tmp_path: Path) -> None:
    outer = SessionContext("outer", str(tmp_path / "outer"), {"project_root": str(tmp_path / "outer-root")})
    inner = SessionContext("inner", str(tmp_path / "inner"), {"project_root": str(tmp_path / "inner-root")})

    async def core(event, ops, next_handler):
        before = await ops.session.cwd(), await ops.session.root()
        nested = await workspace_host.bus.trigger(Event("test.workspace", {}), inner)
        after = await ops.session.cwd(), await ops.session.root()
        return before, nested, after

    workspace_host.bus.register_event_type("test.workspace.nested", lambda value, pattern: True, core)
    before, nested, after = await workspace_host.bus.trigger(Event("test.workspace.nested", None), outer)

    assert before == after == (outer.cwd, outer.metadata["project_root"])
    assert (nested["cwd"], nested["root"]) == (inner.cwd, inner.metadata["project_root"])


@pytest.mark.asyncio
async def test_uninstall_removes_user_handler_without_removing_platform_ops(workspace_host, tmp_path: Path) -> None:
    report = await workspace_host.manager.uninstall("workspace.reader")
    assert report.status == "SUCCESS"
    result = await workspace_host.bus.trigger(Event("test.workspace", {}), SessionContext("s1"))
    assert result is None

    async def core(event, ops, next_handler):
        return await ops.session.cwd(), await ops.session.root()

    workspace_host.bus.register_event_type("test.workspace.host", lambda value, pattern: True, core)
    context = SessionContext("s1", str(tmp_path), {"project_root": str(tmp_path)})
    assert await workspace_host.bus.trigger(Event("test.workspace.host", None), context) == (str(tmp_path), str(tmp_path))


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", [session_cwd, session_root])
async def test_workspace_queries_require_a_session(reader) -> None:
    ops = EventHandlerOpsBuilder().build()
    with pytest.raises(RuntimeError, match="requires SessionContext"):
        await reader(ops)


@pytest.mark.asyncio
@pytest.mark.parametrize("value,error", [("relative/path", ValueError), ("", ValueError), (42, TypeError)])
@pytest.mark.parametrize("reader", [session_cwd, session_root])
async def test_workspace_queries_reject_invalid_bound_paths(reader, value, error) -> None:
    context = SessionContext("s1", value, {"project_root": value})
    ops = EventHandlerOpsBuilder().build(session_context=context)
    with pytest.raises(error):
        await reader(ops)


@pytest.mark.asyncio
async def test_file_preview_uses_trusted_user_and_channel_from_session_context():
    from unittest.mock import AsyncMock

    bus = ExtensionEventBus()
    service = AsyncMock()
    service.read_file.return_value = {"status": "ready", "file": None}
    register_session_ops(bus, file_service=service)

    async def core(event, ops, next_handler):
        return await ops.session.read_file(path=event.event_input["path"])

    bus.register_event_type("test.files", lambda value, pattern: True, core)
    await bus.trigger(Event(OPS_BUILD_EVENT_TYPE, None))
    context = SessionContext("s1", metadata={"user_id": "trusted-user", "channel_id": "web"})

    result = await bus.trigger(Event("test.files", {"path": "pic.png", "user_id": "forged-user"}), context)

    assert result == {"status": "ready", "file": None}
    service.read_file.assert_awaited_once_with("s1", "pic.png", "web", user_id="trusted-user")
