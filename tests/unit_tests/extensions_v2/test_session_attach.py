# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Committed view facts through installed USER packages and real EventBus."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from openjiuwen.core.events import Event, SessionContext

import jiuwenswarm.extensions_v2.assembly as assembly
import jiuwenswarm.server.runtime.session.attach_notifications as notifications
from jiuwenswarm.extensions_v2.event_types.session_attach import SESSION_ATTACH_EVENT, SessionAttachInput
from jiuwenswarm.runtime.session_lifecycle import (
    RuntimeParticipantRegistry, SessionDescriptor, SessionForegroundEvent, SessionKind, SessionLifecycleTarget,
)

from .management.support import write_package


USER_CODE = '''import asyncio
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
from openjiuwen.core.events import Event
from jiuwenswarm.extensions_v2.event_types.session_attach import SessionAttachOutput
class Extension:
    def register(self, registrar):
        registrar.register_event_handler("session.attach", self.on_attach, {}, "user")
    async def on_attach(self, event, ops, next_handler):
        action = os.environ.get("SESSION_ATTACH_TEST_ACTION", "normal")
        sid = ops.session_context.session_id
        output = Path(os.environ["SESSION_ATTACH_TEST_OUTPUT"])
        row = {"input":asdict(event.event_input),"association":dict(ops.session_context.metadata["session_attachment"]),"sid":sid,"package":ops.package_metadata.package_id,
               "cwd":await ops.session.cwd(),"root":await ops.session.root(),"model":await ops.session.model()}
        with output.open("a",encoding="utf-8") as out:
            out.write(json.dumps({"phase":"before",**row}) + "\\n")
        if action == "raise":
            raise RuntimeError("user init failed")
        if action == "block" or action == "ignore_cancel" or (action == "stale" and sid == "attach-a"):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if action != "ignore_cancel":
                    raise
                while not Path(str(output)+".release").exists():
                    await asyncio.sleep(.005)
        if action == "physical-delete":
            path = Path(os.environ["SESSION_ATTACH_TEST_SESSIONS"]) / sid
            for child in path.iterdir():
                child.unlink()
            path.rmdir()
        if action == "short":
            return SessionAttachOutput(event.event_input.clientId)
        if action.startswith("input-"):
            event = Event(event.event_type,replace(event.event_input,**{action.removeprefix("input-"):"forged"}))
        receipt = await next_handler(event)
        if action == "double":
            assert await next_handler(event) is receipt
        with output.open("a",encoding="utf-8") as out:
            out.write(json.dumps({"phase":"after","receipt":asdict(receipt),**row}) + "\\n")
        if action == "output":
            return replace(receipt,clientId="forged")
        if action == "int-output":
            return replace(receipt,clientId=1)
        return receipt
def create_extension():
    return Extension()
'''


@pytest.fixture
async def attach_host(tmp_path, monkeypatch):
    from jiuwenswarm.common import utils
    from jiuwenswarm.server.runtime.session import session_history, session_metadata, model_selection_store

    sessions = tmp_path / "data/agent/sessions"
    sessions.mkdir(parents=True)
    for sid in ("attach-a", "attach-b"):
        path = sessions / sid
        path.mkdir()
        (path / "metadata.json").write_text(json.dumps({"mode":"agent.work.normal","channel_id":"web",
                                                      "project_dir":str(tmp_path),"model":"demo-model"}),encoding="utf-8")
    monkeypatch.setattr(utils,"get_agent_sessions_dir",lambda:sessions)
    monkeypatch.setattr(session_metadata,"get_agent_sessions_dir",lambda:sessions)
    monkeypatch.setattr(session_history,"get_agent_sessions_dir",lambda:sessions)
    monkeypatch.setattr(model_selection_store,"get_agent_sessions_dir",lambda:sessions)
    monkeypatch.setattr(assembly,"_ASSEMBLY",None)
    monkeypatch.setattr(assembly,"resolve_install_dir",lambda:tmp_path/"store")
    output = tmp_path / "observed.jsonl"
    monkeypatch.setenv("SESSION_ATTACH_TEST_OUTPUT",str(output))
    monkeypatch.setenv("SESSION_ATTACH_TEST_SESSIONS",str(sessions))
    monkeypatch.setenv("SESSION_ATTACH_TEST_ACTION","normal")
    host = await assembly.install_extensions()
    assert host is not None
    package = write_package(tmp_path/"source",package_id="attach.reader",module_source=USER_CODE)
    assert (await host.manager.install([package]))[0].status == "SUCCESS"
    participant = notifications.ExtensionSessionAttachParticipant(timeout_seconds=1)
    invocations = []
    make_invocation = notifications.AttachInvocation

    def capture(*args):
        state = make_invocation(*args)
        invocations.append(state)
        return state

    monkeypatch.setattr(notifications,"AttachInvocation",capture)
    target = SessionLifecycleTarget(SessionDescriptor(session_id="attach-a",channel_id="web",mode="agent.work.normal",work_mode="work"),SessionKind.AGENT)
    event = SessionForegroundEvent(target,"view-one",True)
    value = SimpleNamespace(host=host,participant=participant,event=event,output=output,sessions=sessions,root=str(tmp_path),invocations=invocations)
    yield value
    Path(str(output)+".release").touch()
    for task in tuple(participant._tasks):
        task.cancel()
    if participant._tasks:
        await asyncio.wait(tuple(participant._tasks),timeout=1)
        await asyncio.sleep(0)
    assert not participant._tasks
    await assembly.shutdown_extensions()
    session_metadata._METADATA_CACHE.clear()


def rows(host):
    return [json.loads(line) for line in host.output.read_text(encoding="utf-8").splitlines()] if host.output.exists() else []


@pytest.mark.asyncio
async def test_real_user_reads_ops_and_current_association_before_usage(attach_host):
    await attach_host.participant.foreground_changed(attach_host.event)
    before, after = rows(attach_host)
    assert before["sid"] == "attach-a"
    assert before["package"] == "attach.reader" and before["cwd"] is None
    assert before["root"] == attach_host.root and before["model"] == "demo-model"
    assert before["association"]["previous_session_id"] is None
    assert after["receipt"] == {"clientId":"web:view-one"}
    assert attach_host.participant.associations.get("web","view-one") .event_input() == attach_host.invocations[0].original


@pytest.mark.asyncio
async def test_replay_two_views_switch_back_and_visibility(attach_host):
    p, event = attach_host.participant, attach_host.event
    await p.foreground_changed(replace(event,visible=False))
    assert not rows(attach_host)
    await p.foreground_changed(event)
    await asyncio.gather(p.foreground_changed(event),p.foreground_changed(event))
    await p.foreground_changed(replace(event,view_id="view-two"))
    other = replace(event,target=replace(event.target,descriptor=replace(event.target.descriptor,session_id="attach-b")))
    await p.foreground_changed(other)
    await p.foreground_changed(event)
    before = [r for r in rows(attach_host) if r["phase"] == "before"]
    assert len(before) == 4 and len({r["association"]["attachment_id"] for r in before}) == 4
    assert [r["association"]["previous_session_id"] for r in before] == [None,None,"attach-a","attach-b"]
    assert p.associations.get("web","view-two").session_id == "attach-a"


@pytest.mark.asyncio
@pytest.mark.parametrize("action",["raise","short","output","int-output","double","input-clientId","input-surface","input-viewport"])
async def test_invalid_user_input_or_result_cannot_replace_core(attach_host,monkeypatch,action):
    monkeypatch.setenv("SESSION_ATTACH_TEST_ACTION",action)
    await attach_host.participant.foreground_changed(attach_host.event)
    state = attach_host.invocations[0]
    assert state.receipt.clientId == "web:view-one"
    assert not attach_host.host.manager.registry.runtime._inflight


@pytest.mark.asyncio
async def test_timeout_leaves_committed_association_and_no_replay(attach_host,monkeypatch):
    monkeypatch.setenv("SESSION_ATTACH_TEST_ACTION","block")
    attach_host.participant._timeout = .03
    await attach_host.participant.foreground_changed(attach_host.event)
    await asyncio.sleep(0)
    assert attach_host.invocations[0].receipt is None
    assert attach_host.participant.associations.get("web","view-one") is not None
    await attach_host.participant.foreground_changed(attach_host.event)
    assert len(rows(attach_host)) == 1
    assert not attach_host.host.manager.registry.runtime._inflight


@pytest.mark.asyncio
async def test_ignored_cancel_cannot_acknowledge_after_budget(attach_host,monkeypatch):
    monkeypatch.setenv("SESSION_ATTACH_TEST_ACTION","ignore_cancel")
    attach_host.participant._timeout = .03
    await attach_host.participant.foreground_changed(attach_host.event)
    assert attach_host.host.manager.registry.runtime._inflight
    Path(str(attach_host.output)+".release").touch()
    await asyncio.wait(tuple(attach_host.participant._tasks),timeout=1)
    await asyncio.sleep(0)
    assert attach_host.invocations[0].receipt is None and len(rows(attach_host)) == 1
    assert not attach_host.host.manager.registry.runtime._inflight


@pytest.mark.asyncio
async def test_replaced_association_cannot_receive_a_late_ack(attach_host,monkeypatch):
    monkeypatch.setenv("SESSION_ATTACH_TEST_ACTION","stale")
    first = asyncio.create_task(attach_host.participant.foreground_changed(attach_host.event))
    for _ in range(300):
        if rows(attach_host):
            break
        await asyncio.sleep(.001)
    assert rows(attach_host)
    other = replace(attach_host.event,target=replace(attach_host.event.target,descriptor=replace(attach_host.event.target.descriptor,session_id="attach-b")))
    await attach_host.participant.foreground_changed(other)
    for task in tuple(attach_host.participant._tasks):
        task.cancel()
    await first
    assert attach_host.invocations[0].receipt is None
    assert attach_host.invocations[1].receipt.clientId == "web:view-one"
    assert attach_host.participant.associations.get("web","view-one").session_id == "attach-b"


@pytest.mark.asyncio
async def test_target_removed_during_user_handler_is_not_acknowledged(attach_host,monkeypatch):
    monkeypatch.setenv("SESSION_ATTACH_TEST_ACTION","physical-delete")
    await attach_host.participant.foreground_changed(attach_host.event)
    assert attach_host.invocations[0].receipt is None
    assert [r["phase"] for r in rows(attach_host)] == ["before"]


@pytest.mark.asyncio
async def test_missing_archived_team_and_invalid_view_do_not_notify(attach_host):
    event, p = attach_host.event, attach_host.participant
    missing = replace(event,target=replace(event.target,descriptor=replace(event.target.descriptor,session_id="missing")))
    await p.foreground_changed(missing)
    archived = attach_host.sessions.parent / "sessions_archived/attach-a"
    archived.mkdir(parents=True)
    await p.foreground_changed(event)
    archived.rmdir()
    await p.foreground_changed(replace(event,target=replace(event.target,kind=SessionKind.TEAM)))
    with pytest.raises(ValueError):
        await p.foreground_changed(replace(event,view_id="invalid\nview"))
    assert not rows(attach_host) and p.associations.get("web","view-one") is None


@pytest.mark.asyncio
async def test_cancelled_original_call_propagates_and_preserves_fact(attach_host,monkeypatch):
    monkeypatch.setenv("SESSION_ATTACH_TEST_ACTION","block")
    task = asyncio.create_task(attach_host.participant.foreground_changed(attach_host.event))
    for _ in range(300):
        if rows(attach_host):
            break
        await asyncio.sleep(.001)
    assert rows(attach_host)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert attach_host.participant.associations.get("web","view-one") is not None
    assert attach_host.invocations[0].receipt is None


@pytest.mark.asyncio
async def test_users_cannot_invent_host_attach_invocation(attach_host):
    value = SessionAttachInput("desktop","web:view-one")
    with pytest.raises(Exception):
        await attach_host.host.bus.trigger(Event(SESSION_ATTACH_EVENT,value),SessionContext("attach-a"))
    assert not rows(attach_host)


@pytest.mark.asyncio
async def test_uninstall_keeps_binding_and_disabled_host_has_no_retroactive_replay(attach_host,monkeypatch):
    monkeypatch.setattr(assembly,"_ASSEMBLY",None)
    await attach_host.participant.foreground_changed(attach_host.event)
    monkeypatch.setattr(assembly,"_ASSEMBLY",attach_host.host)
    await attach_host.participant.foreground_changed(attach_host.event)
    assert not rows(attach_host)
    assert (await attach_host.host.manager.uninstall("attach.reader")).status == "SUCCESS"
    await attach_host.participant.foreground_changed(replace(attach_host.event,view_id="another-view"))
    assert not rows(attach_host)


def test_fixed_foreground_observers_do_not_change_other_activity_hooks():
    host, dynamic = object(), object()
    registry = RuntimeParticipantRegistry()
    registry.register_host_foreground(host)
    registry.register_host_foreground(host)
    assert registry.snapshot_foreground() == (host,) and registry.snapshot_activity() == ()
    registry.replace_session_participants(activity=(dynamic,),delete=())
    assert registry.snapshot_foreground() == (host,dynamic) and registry.snapshot_activity() == (dynamic,)
    registry.replace_session_participants(activity=(),delete=())
    assert registry.snapshot_foreground() == (host,)
