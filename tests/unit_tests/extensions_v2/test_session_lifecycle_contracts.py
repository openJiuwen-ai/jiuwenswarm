# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Claude-shaped lifecycle data through installed handlers and the real bus."""

import asyncio
from dataclasses import replace
import json

import pytest

from jiuwenswarm.extensions_v2.event_types.session_attach import AttachInvocation
from jiuwenswarm.extensions_v2.event_types.session_surface import RenderViewport
from jiuwenswarm.runtime.session_lifecycle import SessionRunEndEvent, SessionRunEndReason
from jiuwenswarm.server.runtime.session.attach_notifications import ExtensionSessionAttachParticipant
from jiuwenswarm.server.runtime.session.end_notifications import ExtensionSessionEndParticipant

from .management.support import write_package
from .test_session_start import host, USER_CODE as START_CODE, _context, _entered, _install, _start
from .test_session_attach import attach_host
from .test_session_detach import detach_host, rows


@pytest.mark.asyncio
async def test_claude_camel_case_matcher_and_cwd_receipt_preserve_ops_identity(host):
    code = START_CODE.replace('self.start, {}', 'self.start, {"surface": "desktop", "isInteractive": True}')
    await _install(host, source=code)
    receipt, = await _start(host)
    assert receipt.cwd == "D:/project/src"
    row, = _entered(host)
    assert row["input"] == {"cwd": "D:/project/src", "surface": "desktop", "isInteractive": True}
    assert row["session"] == "s1" and row["package"] == "start.a"
    assert row["root"] == "D:/project" and row["model"] == "active-model"


@pytest.mark.asyncio
async def test_noninteractive_start_does_not_invent_a_render_surface(host):
    await _install(host)
    await host.manager.ensure_session_started(_context(), request_id="one-shot", channel_id="sdk",
                                              is_interactive=False)
    assert _entered(host)[0]["input"] == {
        "cwd": "D:/project/src", "surface": None, "isInteractive": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"cwd": None}, {"cwd": "relative/path"}, {"surface": "view-one"}, {"is_interactive": 1},
])
async def test_invalid_start_observations_do_not_consume_activation_attempt(host, changes):
    await _install(host)
    context = _context()
    if "cwd" in changes:
        context = replace(context, cwd=changes["cwd"])
    kwargs = {"request_id": "bad", "channel_id": "web", "is_interactive": True, "surface": "desktop"}
    kwargs.update({key: value for key, value in changes.items() if key != "cwd"})
    with pytest.raises(ValueError):
        await host.manager.ensure_session_started(context, **kwargs)
    assert not _entered(host)
    assert len(await _start(host)) == 1


@pytest.mark.asyncio
async def test_surface_kind_and_client_identity_are_different_across_channels(detach_host):
    base = detach_host.base
    participant = ExtensionSessionAttachParticipant(surfaces={"web": "desktop", "other-web": "mobile"})
    await participant.foreground_changed(base.event)
    other = replace(base.event, target=replace(base.event.target,
                    descriptor=replace(base.event.target.descriptor, channel_id="other-web")))
    await participant.foreground_changed(other)
    attached = [row for row in rows(detach_host) if row["phase"] == "attach"]
    assert [row["input"] for row in attached] == [
        {"surface": "desktop", "clientId": "web:view-one", "viewport": None},
        {"surface": "mobile", "clientId": "other-web:view-one", "viewport": None},
    ]
    assert attached[0]["association"]["attachment_id"] != attached[1]["association"]["attachment_id"]


@pytest.mark.asyncio
async def test_reported_viewport_reaches_user_without_becoming_pixel_measurements(attach_host):
    from jiuwenswarm.server.runtime.session.attach_notifications import _confirmed_context
    from .test_session_attach import rows as attach_rows

    p, event = attach_host.participant, attach_host.event
    context = await asyncio.to_thread(_confirmed_context, event)
    value = p.associations.commit(event, context, surface="desktop", viewport=RenderViewport(120, 40, True))

    async def validate():
        return await asyncio.to_thread(_confirmed_context, event) is not None

    state = AttachInvocation(value.event_input(), lambda: p.associations.is_current(value),
                             validate, value.session_id)
    await p._dispatch(attach_host.host.bus, attach_host.host.manager, state, p.associations.context(value))
    before, after = attach_rows(attach_host)
    assert before["input"]["viewport"] == {"columns": 120, "rows": 40, "isFullscreen": True}
    assert after["receipt"] == {"clientId": "web:view-one"}


END_OBSERVER = '''
from dataclasses import asdict
import json
import os
from pathlib import Path
class Observer:
    def register(self, registrar):
        registrar.register_event_handler("session.end", self.on_end, {}, "user")
    def save(self, row):
        with Path(os.environ["SESSION_DETACH_TEST_OUTPUT"]).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\\n")
    async def on_end(self, event, ops, next_handler):
        self.save({"phase": "end-before", "input": asdict(event.event_input), "sid": ops.session_context.session_id})
        receipt = await next_handler(event)
        self.save({"phase": "end-after", "receipt": asdict(receipt)})
        return receipt
def create_extension():
    return Observer()
'''


@pytest.mark.asyncio
async def test_run_end_detaches_each_old_client_before_end_and_preserves_other_session(detach_host):
    base = detach_host.base
    package = write_package(base.output.parent / "end-source", package_id="end.observer", module_source=END_OBSERVER)
    assert (await base.host.manager.install([package]))[0].status == "SUCCESS"
    await base.participant.foreground_changed(base.event)
    await base.participant.foreground_changed(replace(base.event, view_id="second-view"))
    other = replace(base.event, view_id="other-session", target=replace(base.event.target,
                    descriptor=replace(base.event.target.descriptor, session_id="attach-b")))
    await base.participant.foreground_changed(other)
    end = ExtensionSessionEndParticipant(release_attachments=base.participant.release_for_end)
    event = SessionRunEndEvent(base.event.target, 1, SessionRunEndReason.OTHER)
    await end.session_run_ended(event)
    observed = rows(detach_host)
    detached = [row for row in observed if row["phase"] == "before"]
    assert {row["input"]["clientId"] for row in detached} == {"web:view-one", "web:second-view"}
    assert all(row["input"]["reason"] == "end" and row["sid"] == "attach-a" for row in detached)
    assert all(row["association"]["cause"] == "end" for row in detached)
    assert [row["phase"] for row in observed][-2:] == ["end-before", "end-after"]
    assert observed[-2]["input"] == {"reason": "other", "sessionId": "attach-a", "resume": {"id": "attach-a"}}
    assert observed[-1]["receipt"] == {"sessionId": "attach-a"}
    assert base.participant.associations.get("web", "view-one") is None
    assert base.participant.associations.get("web", "second-view") is None
    assert base.participant.associations.get("web", "other-session").session_id == "attach-b"
    assert (base.sessions / "attach-a/metadata.json").is_file()
    await end.session_run_ended(event)
    assert rows(detach_host) == observed


@pytest.mark.asyncio
async def test_end_budget_releases_all_association_facts_even_if_cleanup_times_out(detach_host, monkeypatch):
    base = detach_host.base
    await base.participant.foreground_changed(base.event)
    await base.participant.foreground_changed(replace(base.event, view_id="second-view"))
    monkeypatch.setenv("SESSION_DETACH_TEST_ACTION", "block")
    end = ExtensionSessionEndParticipant(timeout_seconds=.03,
                                          release_attachments=base.participant.release_for_end)
    await end.session_run_ended(SessionRunEndEvent(base.event.target, 1, SessionRunEndReason.OTHER))
    assert base.participant.associations.get("web", "view-one") is None
    assert base.participant.associations.get("web", "second-view") is None
    pending = tuple(end._tasks | base.participant._tasks)
    if pending:
        await asyncio.wait(pending, timeout=1)
    await asyncio.sleep(0)
    assert not base.host.manager.registry.runtime._inflight
