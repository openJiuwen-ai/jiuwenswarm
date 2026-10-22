from __future__ import annotations

from pathlib import Path

import pytest
from openjiuwen.core.events import Event, SessionContext

import jiuwenswarm.extensions_v2.assembly as assembly
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions_v2.event_types.context_usage import CONTEXT_USAGE_EVENT, ContextUsageUpdate
from jiuwenswarm.extensions_v2.webui.adapter import WebUIDispatchAdapter
from jiuwenswarm.extensions_v2.webui.constants import CODE_SESSION_MISMATCH, CODE_TARGET_INACTIVE


class FakeServer:
    def __init__(self) -> None:
        self.messages = []

    async def send_push(self, message) -> bool:
        self.messages.append(message)
        return True


@pytest.mark.asyncio
async def test_ready_dispatches_to_the_example_panel(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path)
    server = FakeServer()
    installed = await assembly.install_extensions(server)
    assert installed is not None
    try:
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
async def test_press_on_inactive_instance_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path)
    await assembly.install_extensions(FakeServer())
    try:
        response = await WebUIDispatchAdapter().handle(
            AgentRequest(
                request_id="r4",
                channel_id="web",
                session_id="s1",
                req_method=ReqMethod.WEBUI_DISPATCH,
                params={
                    "event": "ui.press",
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
