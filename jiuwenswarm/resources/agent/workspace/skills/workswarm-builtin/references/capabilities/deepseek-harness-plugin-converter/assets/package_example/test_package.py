"""Load the candidate ZIP through the real extension manager and WebUI chain."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from openjiuwen.core.events import ExtensionEventBus

from jiuwenswarm.extensions_v2.management import ExtensionManager
from jiuwenswarm.extensions_v2.webui.constants import FRAME_RENDER, WebUIEvent
from jiuwenswarm.extensions_v2.webui.contributions import WebUICatalogService
from jiuwenswarm.extensions_v2.webui.models import RenderTarget, UiActionInput
from jiuwenswarm.extensions_v2.webui.runtime import WebUIRuntime
from jiuwenswarm.extensions_v2.webui.transport import RenderTransport

PACKAGE_ID = "example.converted-panel"
SLOT_ID = "conversation.input.above"
MESSAGE = "转换后的系统插件已生效"


class RecordingPush:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send_push(self, message: dict[str, Any]) -> bool:
        self.messages.append(message)
        return True


@dataclass
class Host:
    manager: ExtensionManager
    runtime: WebUIRuntime
    push: RecordingPush


@pytest_asyncio.fixture(name="host")
async def host_fixture(tmp_path: Path) -> AsyncIterator[Host]:
    bus = ExtensionEventBus()
    manager = ExtensionManager(tmp_path / "install", report_path=tmp_path / "report.yaml")
    push = RecordingPush()
    runtime = WebUIRuntime(
        bus=bus,
        transport=RenderTransport(push=push),
        catalog=WebUICatalogService(manager.active_packages),
    )
    manager.set_inactive_callback(runtime.drop_package)
    await manager.init(bus, host_event_types=runtime.register_event_types, host_handlers=runtime.register_handlers)
    try:
        yield Host(manager, runtime, push)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_candidate_zip_loads_and_renders_expected_result(host: Host) -> None:
    archive = Path(os.environ["JIUWEN_SYSTEM_PLUGIN_ZIP"])
    reports = await host.manager.install([archive])
    assert len(reports) == 1 and reports[0].status == "SUCCESS", reports
    assert any(item.package_id == PACKAGE_ID for item in host.manager.active_packages())

    receipt = await host.runtime.dispatch(
        UiActionInput(WebUIEvent.READY.value, RenderTarget("session-1", SLOT_ID)),
        user_id="test-user",
    )
    assert receipt is not None and receipt.accepted
    frames = [
        item for item in host.push.messages
        if item["payload"]["event_type"] == FRAME_RENDER
    ]
    assert len(frames) == 1
    assert frames[0]["payload"]["root"]["props"]["value"] == MESSAGE
