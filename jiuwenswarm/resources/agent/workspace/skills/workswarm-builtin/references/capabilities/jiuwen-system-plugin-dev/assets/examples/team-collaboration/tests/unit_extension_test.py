# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests: extension wiring, rendering, event refresh, degradation, lifetime."""

from __future__ import annotations

import asyncio
import gc
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openjiuwen.core.common.exception.codes import StatusCode  # noqa: E402
from openjiuwen.core.common.exception.errors import build_error  # noqa: E402
from openjiuwen.core.events import Event  # noqa: E402

from jiuwenswarm.extensions_v2.webui.models import (  # noqa: E402
    RenderStatus,
    RenderTarget,
    UiActionInput,
    UiRenderOutput,
)

from team_collab_viz import extension as ext_mod  # noqa: E402

PACKAGE_ID = ext_mod.PACKAGE_ID
SLOT = ext_mod.SLOT

TEAM_INFO = {
    "team_name": "t1",
    "display_name": "团队一",
    "leader_member_name": "leader",
    "desc": None,
    "created": 1000,
}
MEMBERS = [
    {"member_name": "leader", "display_name": "队长", "desc": None, "role": "leader", "status": "ready",
     "execution_status": "idle", "mode": "build_mode", "cli_agent": None},
    {"member_name": "dev", "display_name": "开发", "desc": "后端", "role": "teammate", "status": "busy",
     "execution_status": "running", "mode": "build_mode", "cli_agent": None},
]
TASKS = [
    {"task_id": "a", "title": "契约", "content": "内容", "status": "completed", "assignee": "leader",
     "updated_at": 10},
    {"task_id": "b", "title": "后端", "content": "内容", "status": "in_progress", "assignee": "dev",
     "updated_at": 20},
]
DEPS = {"a": [], "b": [{"task_id": "b", "depends_on_task_id": "a", "resolved": True}]}


class _FakeTeam:
    def __init__(self) -> None:
        self.fail = False
        self.calls = {"info": 0, "members": 0, "tasks": 0, "deps": 0}

    def _maybe_fail(self) -> None:
        if self.fail:
            raise RuntimeError("ops unavailable")

    async def info(self, **kwargs):
        self.calls["info"] += 1
        self._maybe_fail()
        return dict(TEAM_INFO)

    async def members(self, **kwargs):
        self.calls["members"] += 1
        self._maybe_fail()
        return [dict(member) for member in MEMBERS]

    async def tasks(self, **kwargs):
        self.calls["tasks"] += 1
        self._maybe_fail()
        return [dict(task) for task in TASKS]

    async def task_dependencies(self, task_id, **kwargs):
        self.calls["deps"] += 1
        self._maybe_fail()
        return [dict(row) for row in DEPS.get(task_id, [])]


class _FakeState:
    def __init__(self) -> None:
        self.stored: dict[str, object] = {}

    async def get(self, ref):
        value = self.stored.get(ref.key)
        return SimpleNamespace(exists=value is not None, value=value, version=1)

    async def set(self, ref, value, expected_version=None):
        self.stored[ref.key] = value
        return SimpleNamespace(is_set=True, changed=True, version=2, reason=None)


class _FakeUi:
    def __init__(self) -> None:
        self.renders: list = []
        self.reject = False

    async def render(self, payload):
        self.renders.append(payload)
        if self.reject:
            return UiRenderOutput(RenderStatus.REJECTED, 0)
        return UiRenderOutput(RenderStatus.DELIVERED, len(self.renders))


class _FakeOps:
    def __init__(self, session_id: str = "s1") -> None:
        self.session_context = SimpleNamespace(session_id=session_id)
        self.package_metadata = SimpleNamespace(package_id=PACKAGE_ID, package_version="1.0.0", root="/tmp")
        self.team = _FakeTeam()
        self.state = _FakeState()
        self.ui = _FakeUi()


class _Registrar:
    def __init__(self) -> None:
        self.registered: list[tuple[str, object, object, object]] = []

    def register_event_handler(self, event_type, handler, match_pattern, level="user"):
        self.registered.append((event_type, handler, match_pattern, level))
        return f"handler-{len(self.registered)}"


class _NextHandler:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, event):
        self.calls += 1
        return "core-output"


def _ready_event(instance_key: str = "", package_id: str = PACKAGE_ID, session_id: str = "s1") -> Event:
    target = RenderTarget(session_id, SLOT, package_id, instance_key)
    return Event("ui.ready", UiActionInput("ui.ready", target))


def _monitor_event(team_name: str = "t1", event_type: str = "task_completed", session_id: str = "s1") -> Event:
    monitor = SimpleNamespace(event_type=event_type, team_name=team_name, timestamp=1234, member_name="dev")
    payload = SimpleNamespace(team_name=team_name, member_name="dev", monitor_event=monitor)
    return Event("team.monitor_event", payload)


@pytest.fixture
def handlers():
    instance = ext_mod.TeamVizHandlers()
    try:
        yield instance
    finally:
        instance.close("test")


def _listening(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def test_registers_two_handlers_and_listens(handlers):
    registrar = _Registrar()
    handlers.mark_registered()
    for event_type, handler, pattern in handlers.handlers():
        registrar.register_event_handler(event_type, handler, pattern, "user")
    assert [entry[0] for entry in registrar.registered] == ["ui.ready", "team.monitor_event"]
    assert registrar.registered[0][2] == {"target.slot_id": SLOT}
    assert registrar.registered[1][2] == {}
    assert handlers.port > 0
    assert _listening(handlers.port)


def test_ui_ready_renders_both_tabs_with_sid(handlers):
    async def scenario():
        ops = _FakeOps()
        nxt = _NextHandler()
        output = await handlers.on_ui_ready(_ready_event(), ops, nxt)
        return output, ops, nxt

    output, ops, nxt = asyncio.run(scenario())
    assert output.accepted is True
    assert output.status == "rendered"
    assert nxt.calls == 0
    assert len(ops.ui.renders) == 2
    rendered = {render.target.instance_key: render.root for render in ops.ui.renders}
    assert set(rendered) == {"tcv.team-info", "tcv.task-graph"}
    for instance_key, root in rendered.items():
        assert root.component == "Iframe"
        assert root.props["title"] == ext_mod.TABS[instance_key]
        assert f"127.0.0.1:{handlers.port}/web/" in root.props["src"]
        assert "?sid=s1" in root.props["src"]


def test_ui_ready_single_instance_key(handlers):
    async def scenario():
        ops = _FakeOps()
        return await handlers.on_ui_ready(_ready_event("tcv.task-graph"), ops, _NextHandler()), ops

    output, ops = asyncio.run(scenario())
    assert output.status == "rendered"
    assert len(ops.ui.renders) == 1
    assert ops.ui.renders[0].target.instance_key == "tcv.task-graph"


def test_ui_ready_ignores_foreign_package(handlers):
    async def scenario():
        ops = _FakeOps()
        nxt = _NextHandler()
        output = await handlers.on_ui_ready(_ready_event(package_id="other.pkg"), ops, nxt)
        return output, ops, nxt

    output, ops, nxt = asyncio.run(scenario())
    assert output == "core-output"
    assert nxt.calls == 1
    assert ops.ui.renders == []


def test_render_rejection_is_reported(handlers):
    async def scenario():
        ops = _FakeOps()
        ops.ui.reject = True
        return await handlers.on_ui_ready(_ready_event(), ops, _NextHandler())

    output = asyncio.run(scenario())
    assert output.accepted is False
    assert output.status == "render_failed"


def test_monitor_event_refreshes_snapshot_and_chains(handlers):
    async def scenario():
        ops = _FakeOps()
        nxt = _NextHandler()
        first = await handlers.on_team_monitor_event(_monitor_event("task_created"), ops, nxt)
        second = await handlers.on_team_monitor_event(_monitor_event("task_updated"), ops, nxt)
        return first, second, ops, nxt

    first, second, ops, nxt = asyncio.run(scenario())
    assert first == "core-output"
    assert second == "core-output"
    assert nxt.calls == 2
    snapshot, revision = handlers._context.snapshots.get("s1")
    assert revision == 2
    assert snapshot["team"]["display_name"] == "团队一"
    assert snapshot["stale"] is False
    assert len(snapshot["members"]) == 2
    assert len(snapshot["tasks"]) == 2
    assert snapshot["graph"]["edges"] == [{"from": "a", "to": "b", "resolved": True}]
    assert snapshot["availability"]["session_map_source"] == "event"
    assert ops.team.calls["deps"] == 4  # one per task per event


def test_degrade_keeps_last_successful_payload(handlers):
    async def scenario():
        ops = _FakeOps()
        nxt = _NextHandler()
        cache = handlers._context.snapshots
        await handlers.on_team_monitor_event(_monitor_event("task_created"), ops, nxt)
        good, good_revision = cache.view("s1")
        ops.team.fail = True
        await handlers.on_team_monitor_event(_monitor_event("task_updated"), ops, nxt)
        degraded, degraded_revision = cache.view("s1")
        # D5: repeating the failure must not bump the revision again.
        await handlers.on_team_monitor_event(_monitor_event("task_updated"), ops, nxt)
        repeated, repeated_revision = cache.view("s1")
        ops.team.fail = False
        await handlers.on_team_monitor_event(_monitor_event("task_completed"), ops, nxt)
        recovered, recovered_revision = cache.view("s1")
        return (good, good_revision, degraded, degraded_revision, repeated, repeated_revision, recovered, recovered_revision)

    good, good_rev, degraded, degraded_rev, repeated, repeated_rev, recovered, recovered_rev = asyncio.run(scenario())
    # D1: every data field survives the failure untouched.
    for field in ("team", "members", "tasks", "graph", "limits", "scope"):
        assert degraded[field] == good[field], field
    # D5: first false->true bumps once, the repeat keeps it stable.
    assert degraded_rev == good_rev + 1
    assert repeated_rev == degraded_rev
    assert repeated["stale"] is True
    # D3/D4: data time frozen, serve time moves.
    assert degraded["generated_at"] == good["generated_at"]
    assert degraded["served_at"] >= good["served_at"]
    # D6: reason always present.
    assert degraded["stale_reason"] == "ops_error"
    # D5: true->false bumps once more and clears the flag.
    assert recovered_rev == degraded_rev + 1
    assert recovered["stale"] is False
    assert recovered["stale_reason"] is None
    assert recovered["team"]["display_name"] == good["team"]["display_name"]


def test_cold_start_failure_is_empty_not_503(handlers):
    async def scenario():
        ops = _FakeOps()
        ops.team.fail = True
        nxt = _NextHandler()
        # learn the mapping first, then fail the read
        handlers._sessions.learn("s1", "t1")
        await handlers.on_team_monitor_event(_monitor_event("task_created"), ops, nxt)
        return handlers._context.snapshots.get("s1")

    snapshot, revision = asyncio.run(scenario())
    assert revision == 1
    assert snapshot["team"] is None
    assert snapshot["stale"] is True
    assert snapshot["stale_reason"] == "cold_start"


def test_ready_restores_default_team_without_saved_mapping(handlers) -> None:
    session_id = "web_history_session"
    team_name = f"jiuwen_team_{session_id}"
    ops = _FakeOps(session_id)
    ops.team.info = AsyncMock(return_value={**TEAM_INFO, "team_name": team_name})
    ops.team.tasks = AsyncMock(return_value=TASKS)

    output = asyncio.run(handlers.on_ui_ready(_ready_event(session_id=session_id), ops, _NextHandler()))

    assert output.accepted
    assert len(ops.ui.renders) == 2
    snapshot, _ = handlers._context.snapshots.get(session_id)
    assert snapshot["stale"] is False
    assert snapshot["team"]["team_name"] == team_name
    assert snapshot["availability"]["session_map_source"] == "default_name"
    assert len(snapshot["members"]) == len(MEMBERS)
    assert len(snapshot["tasks"]) == len(TASKS)
    assert snapshot["graph"]["edges"] == [{"from": "a", "to": "b", "resolved": True}]
    ops.team.tasks.assert_awaited_once_with(team_name=team_name, session_id=session_id)


def test_no_mapping_yields_history_state(handlers) -> None:
    session_id = "unknown-session"
    team_name = f"jiuwen_team_{session_id}"
    ops = _FakeOps(session_id)
    ops.team.info = AsyncMock(
        side_effect=build_error(StatusCode.AGENT_TEAM_STATE_INVALID, reason=f"team '{team_name}' not found"),
    )

    asyncio.run(handlers._refresh(ops, session_id))

    ops.team.info.assert_awaited_once_with(team_name=team_name)
    snapshot, revision = handlers._context.snapshots.get(session_id)
    assert revision == 1
    assert snapshot["scope"] == {"kind": "session", "key": session_id}
    assert snapshot["team"] is None
    assert snapshot["stale_reason"] == "no_session_map"


def test_default_team_read_failure_degrades_without_reporting_missing_team(handlers) -> None:
    ops = _FakeOps()
    ops.team.fail = True

    asyncio.run(handlers._refresh(ops, "s1"))

    snapshot, _ = handlers._context.snapshots.get("s1")
    assert snapshot["team"] is None
    assert snapshot["stale_reason"] == "cold_start"


def test_monitor_event_never_raises(handlers):
    class _BrokenOps(_FakeOps):
        def __init__(self) -> None:
            super().__init__()
            self.session_context = None

    async def scenario():
        nxt = _NextHandler()
        return await handlers.on_team_monitor_event(_monitor_event(), _BrokenOps(), nxt), nxt

    output, nxt = asyncio.run(scenario())
    assert output == "core-output"
    assert nxt.calls == 1


def test_handler_anchor_keeps_port_until_collected():
    instance = ext_mod.TeamVizHandlers()
    port = instance.port
    assert _listening(port)
    del instance
    gc.collect()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _listening(port):
        time.sleep(0.05)
    assert not _listening(port), "listener must be released after the handler is collected"


def test_extension_register_hands_over_bound_methods():
    extension = ext_mod.create_extension()
    registrar = _Registrar()
    extension.register(registrar)
    assert len(registrar.registered) == 2
    try:
        handlers = extension._handlers
        assert _listening(handlers.port)
    finally:
        extension._handlers.close("test")
