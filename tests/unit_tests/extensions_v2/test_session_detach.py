# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Committed association release through real Registry, USER and CORE."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from openjiuwen.core.events import Event, SessionContext

from jiuwenswarm.extensions_v2.event_types.session_detach import SessionDetachInput
import jiuwenswarm.server.runtime.session.detach_notifications as notifications

from .management.support import write_package
from .test_session_attach import attach_host


USER_CODE = '''import asyncio
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
from openjiuwen.core.events import Event
from jiuwenswarm.extensions_v2.event_types.session_detach import SessionDetachOutput
class Extension:
    def register(self, registrar):
        registrar.register_event_handler("session.attach", self.on_attach, {}, "user")
        registrar.register_event_handler("session.detach", self.on_detach, {}, "user")
    def save(self,row):
        with Path(os.environ["SESSION_DETACH_TEST_OUTPUT"]).open("a",encoding="utf-8") as out:
            out.write(json.dumps(row)+"\\n")
    def resource(self,ident):
        return Path(os.environ["SESSION_DETACH_TEST_OUTPUT"]).parent / ("resource-"+ident)
    async def on_attach(self,event,ops,next_handler):
        receipt = await next_handler(event)
        self.resource(ops.session_context.metadata["session_attachment"]["attachment_id"]).touch()
        self.save({"phase":"attach","input":asdict(event.event_input),"association":dict(ops.session_context.metadata["session_attachment"])})
        return receipt
    async def on_detach(self,event,ops,next_handler):
        value = event.event_input
        self.resource(ops.session_context.metadata["session_attachment"]["attachment_id"]).unlink(missing_ok=True)
        self.save({"phase":"before","input":asdict(value),"association":dict(ops.session_context.metadata["session_attachment"]),"sid":ops.session_context.session_id,
                   "root":await ops.session.root(),"package":ops.package_metadata.package_id})
        action = os.environ.get("SESSION_DETACH_TEST_ACTION","normal")
        if action == "raise":
            raise RuntimeError("user cleanup failed")
        if action in ("block","ignore_cancel"):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if action != "ignore_cancel":
                    raise
                while not Path(str(os.environ["SESSION_DETACH_TEST_OUTPUT"])+".release").exists():
                    await asyncio.sleep(.002)
        if action == "short":
            return SessionDetachOutput(value.clientId)
        if action.startswith("input-"):
            event = Event(event.event_type,replace(value,**{action.removeprefix("input-"):"forged"}))
        receipt = await next_handler(event)
        if action == "double":
            assert await next_handler(event) is receipt
        self.save({"phase":"after","receipt":asdict(receipt)})
        if action == "output":
            return replace(receipt,clientId="forged")
        if action == "int-output":
            return replace(receipt,clientId=1)
        return receipt
def create_extension():
    return Extension()
'''


@pytest_asyncio.fixture
async def detach_host(attach_host, monkeypatch):
    output = attach_host.output.parent / "detach.jsonl"
    monkeypatch.setenv("SESSION_DETACH_TEST_OUTPUT", str(output))
    monkeypatch.setenv("SESSION_DETACH_TEST_ACTION", "normal")
    package = write_package(output.parent / "detach-source", package_id="detach.reader", module_source=USER_CODE)
    assert (await attach_host.host.manager.install([package]))[0].status == "SUCCESS"
    invocations = []
    original = notifications.DetachInvocation

    def capture(*args):
        state = original(*args)
        invocations.append(state)
        return state

    monkeypatch.setattr(notifications, "DetachInvocation", capture)
    value = SimpleNamespace(base=attach_host, output=output, invocations=invocations)
    yield value
    Path(str(output) + ".release").touch()


def rows(host):
    return [json.loads(line) for line in host.output.read_text(encoding="utf-8").splitlines()] if host.output.exists() else []


async def release(base, value):
    return await base.participant.detach(channel_id=value.channel_id, session_id=value.session_id,
                                         view_id=value.surface_id, attachment_id=value.attachment_id)


@pytest.mark.asyncio
async def test_two_views_only_one_released_and_no_usage_required(detach_host):
    base = detach_host.base
    await base.participant.foreground_changed(base.event)
    await base.participant.foreground_changed(replace(base.event, view_id="view-two"))
    first, second = [r["association"] for r in rows(detach_host) if r["phase"] == "attach"]
    old = base.participant.associations.get("web", "view-one")
    assert await release(base, old) is True
    assert await release(base, old) is False
    records = rows(detach_host)
    before = [r for r in records if r["phase"] == "before"]
    assert len(before) == 1 and before[0]["sid"] == "attach-a"
    assert before[0]["package"] == "detach.reader" and before[0]["root"] == base.root
    assert before[0]["input"]["reason"] == "detach" and before[0]["association"]["next_session_id"] is None
    assert not (detach_host.output.parent / ("resource-"+first["attachment_id"])).exists()
    assert (detach_host.output.parent / ("resource-"+second["attachment_id"])).exists()
    assert base.participant.associations.get("web", "view-one") is None
    assert base.participant.associations.get("web", "view-two") is not None
    assert (base.sessions / "attach-a/metadata.json").exists()
    assert detach_host.invocations[0].receipt.clientId == "web:view-one"


@pytest.mark.asyncio
async def test_switch_old_notification_before_new_attach_and_stale_request_cannot_remove_new(detach_host):
    base = detach_host.base
    await base.participant.foreground_changed(base.event)
    old = base.participant.associations.get("web", "view-one")
    other = replace(base.event, target=replace(base.event.target, descriptor=replace(base.event.target.descriptor, session_id="attach-b")))
    await base.participant.foreground_changed(other)
    assert [r["phase"] for r in rows(detach_host)] == ["attach", "before", "after", "attach"]
    before = rows(detach_host)[1]
    assert before["input"]["reason"] == "detach" and before["association"]["cause"] == "switch" and before["association"]["next_session_id"] == "attach-b"
    assert before["sid"] == "attach-a" and before["association"]["attachment_id"] == old.attachment_id
    with pytest.raises(LookupError):
        await release(base, old)
    current = base.participant.associations.get("web", "view-one")
    assert current.session_id == "attach-b"
    await base.participant.foreground_changed(other)
    await base.participant.foreground_changed(replace(other, visible=False))
    assert len(rows(detach_host)) == 4
    await base.participant.foreground_changed(base.event)
    with pytest.raises(LookupError):
        await release(base, old)
    assert base.participant.associations.get("web", "view-one").attachment_id != old.attachment_id


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["raise", "short", "output", "int-output", "double", "input-clientId", "input-surface", "input-reason"])
async def test_invalid_user_cannot_restore_or_forge_release(detach_host, monkeypatch, action):
    base = detach_host.base
    await base.participant.foreground_changed(base.event)
    old = base.participant.associations.get("web", "view-one")
    monkeypatch.setenv("SESSION_DETACH_TEST_ACTION", action)
    assert await release(base, old)
    assert base.participant.associations.get("web", "view-one") is None
    assert detach_host.invocations[0].receipt.clientId == "web:view-one"
    assert not base.host.manager.registry.runtime._inflight


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["block", "ignore_cancel"])
async def test_timeout_commits_release_and_no_late_core(detach_host, monkeypatch, action):
    base = detach_host.base
    await base.participant.foreground_changed(base.event)
    old = base.participant.associations.get("web", "view-one")
    monkeypatch.setenv("SESSION_DETACH_TEST_ACTION", action)
    base.participant._timeout = .03
    assert await release(base, old)
    assert base.participant.associations.get("web", "view-one") is None
    assert await release(base, old) is False
    Path(str(detach_host.output) + ".release").touch()
    if base.participant._tasks:
        await asyncio.wait(tuple(base.participant._tasks), timeout=1)
    await asyncio.sleep(0)
    assert detach_host.invocations[0].receipt is None
    assert not base.host.manager.registry.runtime._inflight


@pytest.mark.asyncio
async def test_parent_cancel_does_not_restore_association(detach_host, monkeypatch):
    base = detach_host.base
    await base.participant.foreground_changed(base.event)
    old = base.participant.associations.get("web", "view-one")
    monkeypatch.setenv("SESSION_DETACH_TEST_ACTION", "block")
    task = asyncio.create_task(release(base, old))
    for _ in range(300):
        if any(r["phase"] == "before" for r in rows(detach_host)):
            break
        await asyncio.sleep(.001)
    assert any(r["phase"] == "before" for r in rows(detach_host))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert base.participant.associations.get("web", "view-one") is None
    assert detach_host.invocations[0].receipt is None


@pytest.mark.asyncio
async def test_explicit_release_invalidates_older_slow_foreground(detach_host, monkeypatch):
    import threading
    import jiuwenswarm.server.runtime.session.attach_notifications as attach

    base = detach_host.base
    await base.participant.foreground_changed(base.event)
    old = base.participant.associations.get("web", "view-one")
    started, finish = threading.Event(), threading.Event()
    original = attach._confirmed_context

    def blocked(event):
        started.set()
        finish.wait(2)
        return original(event)

    monkeypatch.setattr(attach, "_confirmed_context", blocked)
    other = replace(base.event, target=replace(base.event.target, descriptor=replace(base.event.target.descriptor, session_id="attach-b")))
    pending = asyncio.create_task(base.participant.foreground_changed(other))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        assert await release(base, old)
    finally:
        finish.set()
        await pending
    assert base.participant.associations.get("web", "view-one") is None


@pytest.mark.asyncio
async def test_untracked_target_removes_only_old_main_binding(detach_host):
    from jiuwenswarm.runtime.session_lifecycle import SessionKind

    base = detach_host.base
    await base.participant.foreground_changed(base.event)
    await base.participant.foreground_changed(replace(base.event, target=replace(base.event.target, kind=SessionKind.TEAM)))
    assert base.participant.associations.get("web", "view-one") is None
    assert [r["phase"] for r in rows(detach_host)] == ["attach", "before", "after"]
    assert rows(detach_host)[1]["association"]["next_session_id"] is None


@pytest.mark.asyncio
async def test_event_requires_host_scope_and_uninstall_preserves_release(detach_host):
    base = detach_host.base
    await base.participant.foreground_changed(base.event)
    old = base.participant.associations.get("web", "view-one")
    facts = SessionDetachInput(old.surface, old.client_id, "detach")
    with pytest.raises(Exception):
        await base.host.bus.trigger(Event("session.detach", facts), SessionContext(old.session_id))
    assert base.participant.associations.get("web", "view-one") is old
    assert (await base.host.manager.uninstall("detach.reader")).status == "SUCCESS"
    assert await release(base, old)
    assert detach_host.invocations[-1].receipt.clientId == "web:view-one"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{"session_id":"../bad"}, {"view_id":"bad/view"}, {"attachment_id":"wrong"}, {"attachment_id":None}])
async def test_malformed_request_preserves_current_association(detach_host, bad):
    base = detach_host.base
    await base.participant.foreground_changed(base.event)
    old = base.participant.associations.get("web", "view-one")
    kwargs = {"channel_id":"web", "session_id":old.session_id, "view_id":old.surface_id, "attachment_id":old.attachment_id}
    kwargs.update(bad)
    with pytest.raises(ValueError):
        await base.participant.detach(**kwargs)
    assert base.participant.associations.get("web", "view-one") is old


@pytest.mark.asyncio
async def test_wire_adapter_returns_actual_release_and_clear_errors(detach_host, monkeypatch):
    import jiuwenswarm.server.agent_ws_server as server
    from dataclasses import asdict
    from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod

    base = detach_host.base
    await base.participant.foreground_changed(base.event)
    old = base.participant.associations.get("web", "view-one")
    sent = []

    async def capture(ws, payload):
        del ws
        sent.append(asdict(parse_agent_server_wire_unary(payload)))

    monkeypatch.setattr(server, "send_wire_payload", capture)
    host = SimpleNamespace(_session_association_participant=base.participant)
    params = {"session_id":old.session_id, "view_id":old.surface_id, "attachment_id":"0"*32}
    request = AgentRequest(request_id="detach-wire", req_method=ReqMethod.SESSION_DETACH, channel_id="web", params=params)
    await server.AgentWebSocketServer._handle_session_detach(host, None, request, asyncio.Lock())
    assert sent[-1]["ok"] is False and sent[-1]["payload"]["code"] == "STALE_ATTACHMENT"
    params["attachment_id"] = old.attachment_id
    await server.AgentWebSocketServer._handle_session_detach(host, None, request, asyncio.Lock())
    assert sent[-1]["ok"] and sent[-1]["payload"]["detached"] is True
    await server.AgentWebSocketServer._handle_session_detach(host, None, request, asyncio.Lock())
    assert sent[-1]["payload"]["detached"] is False
    request.params = {}
    await server.AgentWebSocketServer._handle_session_detach(host, None, request, asyncio.Lock())
    assert sent[-1]["payload"]["code"] == "BAD_REQUEST"
    await server.AgentWebSocketServer._handle_session_detach(SimpleNamespace(), None, request, asyncio.Lock())
    assert sent[-1]["payload"]["code"] == "NOT_READY"
