"""Exercise the packaged counter through the real AgentServer assembly."""

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openjiuwen.core.events import Event, SessionContext

from jiuwenswarm.extensions_v2 import assembly
from jiuwenswarm.extensions_v2.event_types.context_usage import CONTEXT_USAGE_EVENT, ContextUsageUpdate
from jiuwenswarm.extensions_v2.webui.models import RenderTarget, UiActionInput

PACKAGE = "sample.counter"
SLOT = "conversation.input.below"
SOURCE = Path(__file__).resolve().parents[4] / "jiuwenswarm/extensions_v2/examples/state_counter"


class Push:
    def __init__(self):
        self.messages = []
        self.fail = False

    async def send_push(self, message):
        if self.fail:
            return False
        self.messages.append(message)
        return True

    def frames(self, session="s1"):
        return [message["payload"] for message in self.messages
                if message.get("session_id") == session and message["payload"].get("root") is not None]


def _text(frame):
    return frame["root"]["children"][0]["props"]["value"]


async def _ready(host, session="s1"):
    return await host.webui.dispatch(
        UiActionInput("ui.ready", RenderTarget(session, SLOT)), connection_id="counter-test-ws",
    )


async def _click(host, frame, session="s1"):
    return await host.webui.dispatch(UiActionInput(
        "ui.click", RenderTarget(session, SLOT, PACKAGE, "counter.panel"),
        {"action_id": "increment", "args": {}, "data": {}, "node_key": "0.1", "revision": frame["revision"]},
    ), connection_id="counter-test-ws")


@pytest.fixture
async def counter(tmp_path, monkeypatch):
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path / "installed")
    monkeypatch.setattr(assembly, "resolve_install_report_path", lambda: tmp_path / "report.yaml")
    monkeypatch.setattr(assembly, "resolve_store_dir", lambda: tmp_path / "store")
    monkeypatch.setattr(assembly, "resolve_state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path)
    archive = tmp_path / "state_counter.zip"
    with ZipFile(archive, "w", ZIP_DEFLATED) as output:
        for name in ("manifest.yaml", "extension.py", "README.md"):
            output.write(SOURCE / name, name)
    push = Push()
    host = await assembly.install_extensions(push)
    assert host is not None
    try:
        reports = await host.manager.install([archive])
        assert reports[0].status == "SUCCESS"
        yield host, push, tmp_path
    finally:
        await assembly.shutdown_extensions()


async def test_zip_counter_click_and_completed_round_refresh_the_frame(counter):
    host, push, _ = counter
    assert (await _ready(host)).accepted
    assert _text(push.frames()[-1]) == "Count: 0 (state version 1)"
    button = push.frames()[-1]["root"]["children"][1]
    assert button["props"]["value"] == "+1"
    assert (await _click(host, push.frames()[-1])).accepted
    assert _text(push.frames()[-1]) == "Count: 1 (state version 2)"
    update = ContextUsageUpdate(session_id="s1", request_id="completed-1")
    await host.bus.trigger(Event(CONTEXT_USAGE_EVENT, update), SessionContext("s1"))
    assert _text(push.frames()[-1]) == "Count: 2 (state version 3)"
    before = len(push.frames())
    await host.bus.trigger(Event(CONTEXT_USAGE_EVENT, update), SessionContext("s1"))
    assert len(push.frames()) == before
    rows = (await host.manager.query()).packages
    assert rows[0].domain == "agentserver" and rows[0].successful


async def test_counter_reload_preserves_state_and_uninstall_removes_it(counter):
    host, push, tmp_path = counter
    await _ready(host)
    await _click(host, push.frames()[-1])
    assert (await host.manager.reload(PACKAGE)).status == "SUCCESS"
    await _ready(host)
    assert _text(push.frames()[-1]) == "Count: 1 (state version 2)"
    await _ready(host, "s2")
    assert _text(push.frames("s2")[-1]) == "Count: 0 (state version 1)"
    assert (await host.manager.uninstall(PACKAGE)).status == "SUCCESS"
    assert not (tmp_path / "state" / PACKAGE).exists()
    assert not (tmp_path / "installed" / PACKAGE).exists()
    assert not (await host.manager.query()).packages


async def test_completed_round_before_ready_only_saves_state(counter):
    host, push, _ = counter
    update = ContextUsageUpdate(session_id="s1", request_id="before-ready")
    await host.bus.trigger(Event(CONTEXT_USAGE_EVENT, update), SessionContext("s1"))
    assert not push.frames()
    await _ready(host)
    assert _text(push.frames()[-1]) == "Count: 1 (state version 1)"


async def test_failed_render_keeps_saved_value_and_ready_recovers(counter):
    host, push, tmp_path = counter
    await _ready(host)
    frame = push.frames()[-1]
    push.fail = True
    output = await _click(host, frame)
    assert not output.accepted and output.status == "render_failed"
    payload = json.loads((tmp_path / "state" / PACKAGE / "sessions/s1.json").read_text())
    assert payload["count"]["value"] == 1
    assert _text(push.frames()[-1]) == "Count: 0 (state version 1)"
    push.fail = False
    await _ready(host)
    assert _text(push.frames()[-1]) == "Count: 1 (state version 2)"
