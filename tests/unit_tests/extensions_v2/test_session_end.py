# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Terminal cleanup through installed USER code, real EventBus and shared Ops."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from openjiuwen.core.events import Event, SessionContext

import jiuwenswarm.extensions_v2.assembly as assembly
import jiuwenswarm.server.runtime.session.end_notifications as notifications
from jiuwenswarm.extensions_v2.event_types.session_end import SESSION_END_EVENT, SessionEndInput
from jiuwenswarm.runtime.session_lifecycle import RuntimeParticipantRegistry, SessionDescriptor, SessionKind, SessionLifecycleTarget
from jiuwenswarm.server.runtime.session.end_notifications import ExtensionSessionEndParticipant

from .management.support import write_package


USER_CODE = '''import asyncio
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
from openjiuwen.core.events import Event
from jiuwenswarm.extensions_v2.event_types.session_end import SessionEndOutput

class Extension:
    def register(self, registrar):
        registrar.register_event_handler("session.end", self.on_end, {}, "user")

    async def on_end(self, event, ops, next_handler):
        mode = os.environ.get("SESSION_END_TEST_ACTION", "normal")
        output = Path(os.environ["SESSION_END_TEST_OUTPUT"])
        row = {"input": asdict(event.event_input), "session": ops.session_context.session_id,
               "package": ops.package_metadata.package_id, "cwd": await ops.session.cwd(),
               "root": await ops.session.root()}
        with output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"phase": "before", **row}) + "\\n")
        if mode == "raise":
            raise RuntimeError("user cleanup failed")
        if mode in {"block", "ignore_cancel"}:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if mode == "block":
                    raise
                while not Path(str(output) + ".release").exists():
                    await asyncio.sleep(0.005)
        if mode == "short":
            return SessionEndOutput(True, row["session"], row["input"]["end_id"])
        if mode.startswith("input-"):
            field = mode.removeprefix("input-")
            event = Event(event.event_type, replace(event.event_input, **{field: "forged"}))
        result = await next_handler(event)
        if mode == "double":
            again = await next_handler(event)
            assert again is result
        with output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"phase": "after", "receipt": asdict(result), **row}) + "\\n")
        if mode == "output":
            return replace(result, accepted=False)
        if mode == "int-output":
            return replace(result, accepted=1)
        return result

def create_extension():
    return Extension()
'''


@pytest.fixture
async def end_host(tmp_path, monkeypatch):
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path / "store")
    output = tmp_path / "observations.jsonl"
    monkeypatch.setenv("SESSION_END_TEST_OUTPUT", str(output))
    monkeypatch.setenv("SESSION_END_TEST_ACTION", "normal")
    host = await assembly.install_extensions()
    assert host is not None
    package = write_package(tmp_path / "source", package_id="end.reader", module_source=USER_CODE)
    assert (await host.manager.install([package]))[0].status == "SUCCESS"
    participant = ExtensionSessionEndParticipant(timeout_seconds=1.0)
    invocations = []
    make_invocation = notifications.EndInvocation

    def capture_invocation(value):
        state = make_invocation(value)
        invocations.append(state)
        return state

    monkeypatch.setattr(notifications, "EndInvocation", capture_invocation)
    target = SessionLifecycleTarget(
        SessionDescriptor(session_id="end-a", channel_id="web", mode="agent.work.normal", work_mode="work",
                          project_dir=str(tmp_path)), SessionKind.AGENT,
    )
    state = SimpleNamespace(host=host, participant=participant, target=target, output=output, invocations=invocations)
    yield state
    Path(str(output) + ".release").touch()
    if participant._tasks:
        await asyncio.wait(tuple(participant._tasks), timeout=1)
    assert not participant._tasks
    await assembly.shutdown_extensions()


def observed(host):
    return [json.loads(line) for line in host.output.read_text(encoding="utf-8").splitlines()] if host.output.exists() else []


@pytest.mark.asyncio
async def test_installed_user_gets_target_ops_and_actual_core_receipt(end_host):
    await end_host.participant.release_resources(end_host.target)
    rows = observed(end_host)
    assert [r["phase"] for r in rows] == ["before", "after"]
    assert rows[0]["session"] == rows[0]["input"]["session_id"] == "end-a"
    assert rows[0]["package"] == "end.reader" and rows[0]["cwd"] is None
    assert rows[0]["root"] == end_host.target.descriptor.project_dir
    assert rows[1]["receipt"] == {"accepted": True, "session_id": "end-a", "end_id": rows[0]["input"]["end_id"]}
    assert len(rows[0]["input"]["end_id"]) == 32 and rows[0]["input"]["reason"] == "session_deleted"


@pytest.mark.asyncio
async def test_concurrent_repeated_and_failed_delete_do_not_replay_cleanup(end_host):
    await asyncio.gather(*(end_host.participant.release_resources(end_host.target) for _ in range(3)))
    await end_host.participant.delete_failed(end_host.target, destructive_started=True)
    await end_host.participant.release_resources(end_host.target)
    assert len(observed(end_host)) == 2
    await end_host.participant.delete_committed(end_host.target)
    assert not end_host.participant._attempted


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["raise", "short", "output", "int-output", "double", "input-session_id", "input-end_id", "input-reason", "input-channel_id", "input-mode"])
async def test_user_failures_or_forgery_cannot_replace_target_or_core(end_host, monkeypatch, action):
    monkeypatch.setenv("SESSION_END_TEST_ACTION", action)
    await end_host.participant.release_resources(end_host.target)
    assert "end-a" in end_host.participant._attempted
    # No exception escapes to veto Runtime cleanup. CORE still executes for
    # invalid user results/inputs; valid after-hooks see its original target.
    for row in observed(end_host):
        if row["phase"] == "after":
            assert row["receipt"]["accepted"] is True and row["receipt"]["session_id"] == "end-a"
    assert end_host.invocations[0].receipt.accepted is True
    assert end_host.invocations[0].receipt.session_id == "end-a"
    assert not end_host.host.manager.registry.runtime._inflight


@pytest.mark.asyncio
async def test_timeout_continues_and_releases_package_busy_state(end_host, monkeypatch):
    monkeypatch.setenv("SESSION_END_TEST_ACTION", "block")
    end_host.participant._timeout = 0.03
    await asyncio.wait_for(end_host.participant.release_resources(end_host.target), timeout=1)
    await asyncio.sleep(0)
    assert [r["phase"] for r in observed(end_host)] == ["before"]
    assert not end_host.host.manager.registry.runtime._inflight


@pytest.mark.asyncio
async def test_ignored_cancellation_has_no_late_valid_core_receipt(end_host, monkeypatch):
    monkeypatch.setenv("SESSION_END_TEST_ACTION", "ignore_cancel")
    end_host.participant._timeout = 0.03
    await asyncio.wait_for(end_host.participant.release_resources(end_host.target), timeout=1)
    assert end_host.participant._tasks
    assert end_host.host.manager.registry.runtime._inflight
    Path(str(end_host.output) + ".release").touch()
    await asyncio.wait(tuple(end_host.participant._tasks), timeout=1)
    await asyncio.sleep(0)
    assert [r["phase"] for r in observed(end_host)] == ["before"]
    assert not end_host.host.manager.registry.runtime._inflight


@pytest.mark.asyncio
async def test_original_request_cancellation_propagates_without_marking_success(end_host, monkeypatch):
    monkeypatch.setenv("SESSION_END_TEST_ACTION", "block")
    task = asyncio.create_task(end_host.participant.release_resources(end_host.target))
    for _ in range(100):
        if observed(end_host):
            break
        await asyncio.sleep(0.001)
    assert observed(end_host)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert not end_host.host.manager.registry.runtime._inflight


@pytest.mark.asyncio
async def test_cross_session_and_team_scope(end_host):
    from dataclasses import replace
    other = replace(end_host.target, descriptor=replace(end_host.target.descriptor, session_id="end-b", project_dir="relative"))
    team = replace(end_host.target, kind=SessionKind.TEAM)
    await end_host.participant.release_resources(team)
    await asyncio.gather(end_host.participant.release_resources(end_host.target), end_host.participant.release_resources(other))
    before = [r for r in observed(end_host) if r["phase"] == "before"]
    assert {r["session"] for r in before} == {"end-a", "end-b"}
    assert len({r["input"]["end_id"] for r in before}) == 2
    assert next(r for r in before if r["session"] == "end-b")["root"] is None


@pytest.mark.asyncio
async def test_disabled_assembly_and_uninstalled_user_preserve_platform_boundary(end_host, monkeypatch):
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    await end_host.participant.release_resources(end_host.target)
    assert not observed(end_host) and not end_host.participant._attempted
    monkeypatch.setattr(assembly, "_ASSEMBLY", end_host.host)
    assert (await end_host.host.manager.uninstall("end.reader")).status == "SUCCESS"
    await end_host.participant.release_resources(end_host.target)
    assert not observed(end_host)


@pytest.mark.asyncio
async def test_users_cannot_directly_forge_terminal_host_event(end_host):
    event = Event(SESSION_END_EVENT, SessionEndInput("end-a", "invented", "session_deleted", "web", "agent"))
    with pytest.raises(Exception):
        await end_host.host.bus.trigger(event, SessionContext("end-a"))
    assert not observed(end_host)


def test_host_participants_survive_dynamic_replacement():
    host, dynamic = object(), object()
    registry = RuntimeParticipantRegistry(host_delete=(host,))
    registry.replace_session_participants(activity=(dynamic,), delete=(dynamic,))
    assert registry.snapshot_delete() == (host, dynamic)
    registry.replace_session_participants(activity=(), delete=())
    assert registry.snapshot_delete() == (host,)
