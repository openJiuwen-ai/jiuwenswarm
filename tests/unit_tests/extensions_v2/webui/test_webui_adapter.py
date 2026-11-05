from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.events import Event, SessionContext

import jiuwenswarm.extensions_v2.assembly as assembly
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions_v2.event_types.context_usage import CONTEXT_USAGE_EVENT, ContextUsageUpdate
from jiuwenswarm.extensions_v2.webui.adapter import WebUIDispatchAdapter
from jiuwenswarm.extensions_v2.webui.constants import CODE_SESSION_MISMATCH, CODE_TARGET_INACTIVE
from jiuwenswarm.extensions_v2.webui.contributions import UiCatalog, UiContribution
from jiuwenswarm.extensions_v2.webui.models import UiActionOutput


class FakeServer:
    def __init__(self) -> None:
        self.messages = []

    async def send_push(self, message) -> bool:
        self.messages.append(message)
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize("changes,code", [
    ({"event": ["ui.click"]}, "SCHEMA_ERROR"),
    ({"payload": []}, "SCHEMA_ERROR"),
    ({"payload": None}, "SCHEMA_ERROR"),
    ({"target": []}, "SCHEMA_ERROR"),
    ({"target": {"slot_id": 1}}, "SCHEMA_ERROR"),
    ({"target": {"slot_id": "conversation.input.below", "package_id": False}}, "SCHEMA_ERROR"),
    ({"target": {"slot_id": "conversation.input.below", "unknown": "field"}}, "SCHEMA_ERROR"),
    ({"session_id": 1}, "SCHEMA_ERROR"),
    ({"session_id": "other"}, "SESSION_MISMATCH"),
    ({"target": {"session_id": "other", "slot_id": "conversation.input.below"}}, "SESSION_MISMATCH"),
    ({"target": {"slot_id": "conversation.input.below", "package_id": "spoofed"}}, "NO_CONTRIBUTION"),
])
async def test_invalid_adapter_requests_do_not_dispatch(monkeypatch, changes, code):
    catalog = UiCatalog(items=(UiContribution("pkg.demo", "web.slot.contribution", "main", "conversation.input.below"),))
    dispatch = AsyncMock(return_value=UiActionOutput(True, "handled"))
    runtime = SimpleNamespace(catalog=lambda: SimpleNamespace(current=lambda: catalog), dispatch=dispatch)
    monkeypatch.setattr(assembly, "get_webui_runtime", lambda: runtime)
    params = {
        "event": "ui.click", "session_id": "s1",
        "target": {"slot_id": "conversation.input.below", "package_id": "pkg.demo", "instance_key": "main"},
        "payload": {"node_key": "0", "revision": 1, "action_id": "refresh", "args": {}, "data": {}},
    }
    params.update(changes)
    response = await WebUIDispatchAdapter().handle(AgentRequest(
        request_id="invalid", session_id="s1", channel_id="web", req_method=ReqMethod.WEBUI_DISPATCH, params=params,
    ))
    assert response.payload["code"] == code
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_preserves_component_event_data_and_receipt(monkeypatch):
    catalog = UiCatalog(items=(UiContribution("pkg.demo", "web.slot.contribution", "main", "conversation.input.below"),))
    dispatch = AsyncMock(return_value=UiActionOutput(True, "updated"))
    runtime = SimpleNamespace(catalog=lambda: SimpleNamespace(current=lambda: catalog), dispatch=dispatch)
    monkeypatch.setattr(assembly, "get_webui_runtime", lambda: runtime)
    payload = {"node_key": "0.1", "revision": 3, "action_id": "edit", "args": {"id": 5},
               "data": {"value": "Beijing", "isComposing": False}}
    response = await WebUIDispatchAdapter().handle(AgentRequest(
        request_id="valid", session_id="s1", channel_id="web", user_id="user",
        req_method=ReqMethod.WEBUI_DISPATCH,
        params={"event": "ui.change", "target": {"slot_id": "conversation.input.below", "package_id": "pkg.demo", "instance_key": "main"},
                "payload": payload},
    ))
    assert response.ok and response.payload == {"event": "ui.change", "accepted": True, "status": "updated"}
    action = dispatch.call_args.args[0]
    assert action.target.session_id == "s1" and action.payload == payload
    assert dispatch.call_args.kwargs == {"user_id": "user", "channel_id": "web"}


@pytest.mark.asyncio
async def test_ready_dispatches_to_the_example_panel(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path)
    server = FakeServer()
    installed = await assembly.install_extensions(server)
    assert installed is not None
    try:
        example = Path(assembly.__file__).parent / "examples" / "token_weather"
        reports = await installed.manager.install([example])
        assert reports[0].status == "SUCCESS"
        await installed.bus.trigger(
            Event(
                CONTEXT_USAGE_EVENT,
                ContextUsageUpdate(
                    session_id="s1",
                    request_id="r1",
                    input_tokens=1000,
                    output_tokens=200,
                    total_tokens=1200,
                ),
            ),
            SessionContext(session_id="s1"),
        )
        server.messages.clear()

        response = await WebUIDispatchAdapter().handle(
            AgentRequest(
                request_id="r1",
                channel_id="web",
                session_id="s1",
                req_method=ReqMethod.WEBUI_DISPATCH,
                params={
                    "event": "ui.ready",
                    "session_id": "s1",
                    "target": {
                        "session_id": "s1",
                        "slot_id": "conversation.input.below",
                        "package_id": "",
                        "instance_key": "",
                    },
                    "payload": {},
                },
            )
        )
        assert response.ok
        assert response.payload["accepted"] is True
        assert response.payload["status"] == "rendered"
        assert server.messages[-1]["payload"]["event_type"] == "webui.render"
    finally:
        await assembly.shutdown_extensions()


@pytest.mark.asyncio
async def test_ready_without_a_contribution_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path)
    await assembly.install_extensions(FakeServer())
    try:
        response = await WebUIDispatchAdapter().handle(
            AgentRequest(
                request_id="r2",
                channel_id="web",
                session_id="s1",
                req_method=ReqMethod.WEBUI_DISPATCH,
                params={
                    "event": "ui.ready",
                    "session_id": "s1",
                    "target": {
                        "session_id": "s1",
                        "slot_id": "conversation.panel.right",
                        "package_id": "",
                        "instance_key": "",
                    },
                    "payload": {},
                },
            )
        )
        assert response.ok
        assert response.payload["accepted"] is False
        assert response.payload["code"] == "NO_CONTRIBUTION"
    finally:
        await assembly.shutdown_extensions()


@pytest.mark.asyncio
async def test_session_mismatch_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path)
    await assembly.install_extensions(FakeServer())
    try:
        response = await WebUIDispatchAdapter().handle(
            AgentRequest(
                request_id="r3",
                channel_id="web",
                session_id="s2",
                req_method=ReqMethod.WEBUI_DISPATCH,
                params={
                    "event": "ui.ready",
                    "session_id": "s1",
                    "target": {
                        "session_id": "s1",
                        "slot_id": "conversation.input.above",
                        "package_id": "",
                        "instance_key": "",
                    },
                    "payload": {},
                },
            )
        )
        assert response.ok is False
        assert response.payload["code"] == CODE_SESSION_MISMATCH
    finally:
        await assembly.shutdown_extensions()


@pytest.mark.asyncio
async def test_click_on_inactive_instance_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path)
    installed = await assembly.install_extensions(FakeServer())
    assert installed is not None
    try:
        example = Path(assembly.__file__).parent / "examples" / "token_weather"
        reports = await installed.manager.install([example])
        assert reports[0].status == "SUCCESS"
        response = await WebUIDispatchAdapter().handle(
            AgentRequest(
                request_id="r4",
                channel_id="web",
                session_id="s1",
                req_method=ReqMethod.WEBUI_DISPATCH,
                params={
                    "event": "ui.click",
                    "session_id": "s1",
                    "target": {
                        "session_id": "s1",
                        "slot_id": "conversation.input.below",
                        "package_id": "webui.token-weather",
                        "instance_key": "ghost",
                    },
                    "payload": {"action_id": "file.0"},
                },
            )
        )
        assert response.ok
        assert response.payload["accepted"] is False
        assert response.payload["code"] == CODE_TARGET_INACTIVE
    finally:
        await assembly.shutdown_extensions()
