# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Run-end cleanup through installed USER code, real EventBus and shared Ops."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.events import Event, SessionContext

import jiuwenswarm.extensions_v2.assembly as assembly
import jiuwenswarm.server.runtime.session.end_notifications as notifications
from jiuwenswarm.extensions_v2.event_types.session_end import SESSION_END_EVENT, SessionEndInput, SessionResume
from jiuwenswarm.runtime.session_lifecycle import (
    RuntimeParticipantRegistry, SessionDescriptor, SessionKind, SessionLifecycleTarget, SessionRunEndEvent, SessionRunEndReason,
)
from jiuwenswarm.server.runtime.session.end_notifications import ExtensionSessionEndParticipant
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.session import RuntimeSessionState, SessionWorkKind

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
        stopped = os.environ.get("SESSION_END_TEST_STOPPED")
        row["stopped"] = Path(stopped).exists() if stopped else None
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
            return SessionEndOutput(row["session"])
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
            return replace(result, session_id="forged")
        if mode == "int-output":
            return replace(result, session_id=1)
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
    event = SessionRunEndEvent(target, 1, SessionRunEndReason.OTHER)
    state = SimpleNamespace(host=host, participant=participant, target=target, event=event, output=output, invocations=invocations)
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
    await end_host.participant.session_run_ended(end_host.event)
    rows = observed(end_host)
    assert [r["phase"] for r in rows] == ["before", "after"]
    assert rows[0]["session"] == rows[0]["input"]["session_id"] == "end-a"
    assert rows[0]["package"] == "end.reader" and rows[0]["cwd"] is None
    assert rows[0]["root"] == end_host.target.descriptor.project_dir
    assert rows[1]["receipt"] == {"session_id": "end-a"}
    assert rows[0]["input"] == {"reason": "other", "session_id": "end-a", "resume": {"id": "end-a"}}


@pytest.mark.asyncio
async def test_same_run_deduplicates_and_resumed_generation_ends_again(end_host):
    from dataclasses import replace

    await asyncio.gather(*(end_host.participant.session_run_ended(end_host.event) for _ in range(3)))
    await end_host.participant.session_run_ended(end_host.event)
    assert len(observed(end_host)) == 2
    await end_host.participant.session_run_ended(replace(end_host.event, generation=2))
    await end_host.participant.session_run_ended(end_host.event)
    assert len(observed(end_host)) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["raise", "short", "output", "int-output", "double", "input-session_id", "input-reason", "input-resume"])
async def test_user_failures_or_forgery_cannot_replace_target_or_core(end_host, monkeypatch, action):
    monkeypatch.setenv("SESSION_END_TEST_ACTION", action)
    await end_host.participant.session_run_ended(end_host.event)
    assert "end-a" in end_host.participant._attempted
    # No exception escapes to veto Runtime cleanup. CORE still executes for
    # invalid user results/inputs; valid after-hooks see its original target.
    for row in observed(end_host):
        if row["phase"] == "after":
            assert row["receipt"] == {"session_id": "end-a"}
    assert end_host.invocations[0].receipt.session_id == "end-a"
    assert not end_host.host.manager.registry.runtime._inflight


@pytest.mark.asyncio
async def test_timeout_continues_and_releases_package_busy_state(end_host, monkeypatch):
    monkeypatch.setenv("SESSION_END_TEST_ACTION", "block")
    end_host.participant._timeout = 0.03
    await asyncio.wait_for(end_host.participant.session_run_ended(end_host.event), timeout=1)
    await asyncio.sleep(0)
    assert [r["phase"] for r in observed(end_host)] == ["before"]
    assert not end_host.host.manager.registry.runtime._inflight


@pytest.mark.asyncio
async def test_ignored_cancellation_has_no_late_valid_core_receipt(end_host, monkeypatch):
    monkeypatch.setenv("SESSION_END_TEST_ACTION", "ignore_cancel")
    end_host.participant._timeout = 0.03
    await asyncio.wait_for(end_host.participant.session_run_ended(end_host.event), timeout=1)
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
    task = asyncio.create_task(end_host.participant.session_run_ended(end_host.event))
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
    await end_host.participant.session_run_ended(replace(end_host.event, target=team))
    await asyncio.gather(
        end_host.participant.session_run_ended(end_host.event),
        end_host.participant.session_run_ended(replace(end_host.event, target=other)),
    )
    before = [r for r in observed(end_host) if r["phase"] == "before"]
    assert {r["session"] for r in before} == {"end-a", "end-b"}
    assert {r["input"]["resume"]["id"] for r in before} == {"end-a", "end-b"}
    assert next(r for r in before if r["session"] == "end-b")["root"] is None


@pytest.mark.asyncio
async def test_disabled_assembly_and_uninstalled_user_preserve_platform_boundary(end_host, monkeypatch):
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    await end_host.participant.session_run_ended(end_host.event)
    assert not observed(end_host) and not end_host.participant._attempted
    monkeypatch.setattr(assembly, "_ASSEMBLY", end_host.host)
    assert (await end_host.host.manager.uninstall("end.reader")).status == "SUCCESS"
    await end_host.participant.session_run_ended(end_host.event)
    assert not observed(end_host)


@pytest.mark.asyncio
async def test_users_cannot_directly_forge_terminal_host_event(end_host):
    event = Event(SESSION_END_EVENT, SessionEndInput("other", "end-a", SessionResume("end-a")))
    with pytest.raises(Exception):
        await end_host.host.bus.trigger(event, SessionContext("end-a"))
    assert not observed(end_host)


def test_host_participants_survive_dynamic_replacement():
    host, dynamic = object(), object()
    registry = RuntimeParticipantRegistry(host_run_end=(host,))
    registry.replace_session_participants(activity=(dynamic,), delete=(dynamic,))
    assert registry.snapshot_run_end() == (host,)
    assert registry.snapshot_delete() == (dynamic,)
    registry.replace_session_participants(activity=(), delete=())
    assert registry.snapshot_run_end() == (host,)
    assert registry.snapshot_delete() == ()


def run_host(end_host):
    manager = SimpleNamespace(
        cleanup_session_runtime=AsyncMock(return_value=True),
        cancel_all_inflight_work=AsyncMock(), cleanup=AsyncMock(),
    )
    registry = RuntimeParticipantRegistry(host_run_end=(end_host.participant,))
    registry.remember_target(end_host.target)
    runtime = AgentRuntime(
        agent_manager=manager, initializer=AsyncMock(),
        participant_registry=registry,
        plan_controller=SimpleNamespace(reset_session=lambda session_id: None),
    )
    return runtime, manager


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", list(SessionRunEndReason))
async def test_runtime_run_end_preserves_history_and_reports_host_reason(end_host, tmp_path, reason):
    runtime, manager = run_host(end_host)
    history = tmp_path / "history.jsonl"
    history.write_text('{"content":"keep this transcript"}\n', encoding="utf-8")
    original = history.read_bytes()
    await runtime.start()
    await runtime._register_session(session_id="end-a", channel_id="web")
    try:
        assert await runtime.cleanup_session(channel_id="web", session_id="end-a", end_reason=reason)
        rows = observed(end_host)
        assert rows[0]["input"] == {"reason": reason.value, "session_id": "end-a", "resume": {"id": "end-a"}}
        assert rows[1]["receipt"] == {"session_id": "end-a"}
        assert history.read_bytes() == original
        await runtime.close()
        assert len(observed(end_host)) == 2
        manager.cleanup.assert_awaited_once()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_resumed_id_gets_new_end_and_offline_id_has_no_fake_end(end_host):
    runtime, manager = run_host(end_host)
    await runtime.start()
    try:
        await runtime.cleanup_session(channel_id="web", session_id="offline-history")
        assert not observed(end_host)
        for generation in (1, 2):
            await runtime._register_session(session_id="end-a", channel_id="web")
            assert runtime._session_coordinator.snapshot_session("end-a").generation == generation
            await asyncio.gather(*(
                runtime.cleanup_session(channel_id="web", session_id="end-a") for _ in range(3)
            ))
            assert len(observed(end_host)) == generation * 2
        await runtime.close()
        assert len(observed(end_host)) == 4
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_turn_completion_does_not_end_run_and_close_waits_for_execution(end_host, tmp_path, monkeypatch):
    runtime, manager = run_host(end_host)
    await runtime.start()
    await runtime._register_session(session_id="end-a", channel_id="web")
    started = asyncio.Event()
    stopped = tmp_path / "execution-stopped"
    monkeypatch.setenv("SESSION_END_TEST_STOPPED", str(stopped))

    async def complete():
        return "one turn completed"

    async def active():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.touch()

    coordinator = runtime._session_coordinator
    assert await coordinator.run_unary("end-a", "turn-1", SessionWorkKind.CHAT_UNARY, complete) == "one turn completed"
    assert not observed(end_host)
    task = asyncio.create_task(coordinator.run_unary("end-a", "turn-2", SessionWorkKind.CHAT_UNARY, active))
    await started.wait()
    try:
        await runtime.close()
        assert stopped.exists()
        assert all(row["stopped"] for row in observed(end_host))
        assert observed(end_host)[1]["receipt"] == {"session_id": "end-a"}
    finally:
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_new_generation_waits_until_previous_platform_cleanup_finishes(end_host):
    runtime, manager = run_host(end_host)
    await runtime.start()
    await runtime._register_session(session_id="end-a", channel_id="web")
    cleaning, release = asyncio.Event(), asyncio.Event()

    async def cleanup(**kwargs):
        cleaning.set()
        await release.wait()
        return True

    manager.cleanup_session_runtime.side_effect = cleanup
    task = asyncio.create_task(runtime.cleanup_session(channel_id="web", session_id="end-a"))
    await cleaning.wait()
    resumed = asyncio.create_task(runtime._register_session(session_id="end-a", channel_id="web"))
    try:
        await asyncio.sleep(0)
        assert not resumed.done()
        assert runtime._session_coordinator.snapshot_session("end-a").state is RuntimeSessionState.CLOSED
        release.set()
        await asyncio.gather(task, resumed)
        assert runtime._session_coordinator.snapshot_session("end-a").generation == 2
        await runtime.close()
        assert len(observed(end_host)) == 4
    finally:
        release.set()
        await asyncio.gather(task, resumed, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_worker_shutdown_keeps_user_handlers_until_all_run_ends(end_host):
    from dataclasses import replace
    from jiuwenswarm.server.worker.lifecycle import WorkerRuntime

    runtime, manager = run_host(end_host)
    await runtime.start()
    for sid in ("end-a", "end-b"):
        await runtime._register_session(session_id=sid, channel_id="web")
        runtime._participant_registry.remember_target(
            replace(end_host.target, descriptor=replace(end_host.target.descriptor, session_id=sid))
        )
    worker = WorkerRuntime(server=SimpleNamespace(stop=runtime.close), stop_event=asyncio.Event())
    await worker.shutdown()
    assert assembly.get_extension_bus() is None
    assert runtime.closed
    rows = [row for row in observed(end_host) if row["phase"] == "after"]
    assert {row["receipt"]["session_id"] for row in rows} == {"end-a", "end-b"}
    assert all(row["input"]["resume"]["id"] == row["session"] for row in rows)


@pytest.mark.asyncio
async def test_runtime_close_starts_session_notifications_concurrently(end_host, monkeypatch):
    from dataclasses import replace

    monkeypatch.setenv("SESSION_END_TEST_ACTION", "block")
    end_host.participant._timeout = 0.5
    runtime, manager = run_host(end_host)
    await runtime.start()
    for sid in ("end-a", "end-b", "end-c"):
        await runtime._register_session(session_id=sid, channel_id="web")
        runtime._participant_registry.remember_target(
            replace(end_host.target, descriptor=replace(end_host.target.descriptor, session_id=sid))
        )
    close = asyncio.create_task(runtime.close())
    try:
        for _ in range(200):
            if len(observed(end_host)) == 3:
                break
            await asyncio.sleep(0.001)
        assert len(observed(end_host)) == 3
        assert all(invocation.active for invocation in end_host.invocations)
        await close
        assert runtime.closed
        await asyncio.sleep(0)
        assert not end_host.host.manager.registry.runtime._inflight
    finally:
        await close


@pytest.mark.asyncio
async def test_cancelled_run_end_does_not_poison_later_platform_cleanup(end_host, monkeypatch):
    monkeypatch.setenv("SESSION_END_TEST_ACTION", "block")
    runtime, manager = run_host(end_host)
    await runtime.start()
    await runtime._register_session(session_id="end-a", channel_id="web")
    task = asyncio.create_task(runtime.cleanup_session(channel_id="web", session_id="end-a"))
    for _ in range(100):
        if observed(end_host):
            break
        await asyncio.sleep(0.001)
    assert observed(end_host)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    try:
        assert await asyncio.wait_for(runtime.cleanup_session(channel_id="web", session_id="end-a"), timeout=1)
        assert len(observed(end_host)) == 1
        assert len(end_host.invocations) == 1
        manager.cleanup_session_runtime.assert_awaited_once()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_invalid_host_reason_is_rejected_before_run_or_resource_change(end_host):
    runtime, manager = run_host(end_host)
    await runtime.start()
    await runtime._register_session(session_id="end-a", channel_id="web")
    try:
        with pytest.raises(ValueError):
            await runtime.cleanup_session(channel_id="web", session_id="end-a", end_reason="session_deleted")
        with pytest.raises(ValueError):
            await runtime.close(end_reason="made-up")
        assert runtime._session_coordinator.snapshot_session("end-a").state is RuntimeSessionState.READY
        manager.cleanup_session_runtime.assert_not_awaited()
        manager.cleanup.assert_not_awaited()
        assert not observed(end_host)
    finally:
        await runtime.close()
