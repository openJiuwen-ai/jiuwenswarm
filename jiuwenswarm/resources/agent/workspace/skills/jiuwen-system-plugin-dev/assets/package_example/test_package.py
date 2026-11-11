"""Exercise the real loader and WebUI chain in a temporary host."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from openjiuwen.core.events import ExtensionEventBus

from jiuwenswarm.extensions_v2.management import ExtensionManager
from jiuwenswarm.extensions_v2.webui.constants import FRAME_CLEAR, FRAME_RENDER, WebUIEvent
from jiuwenswarm.extensions_v2.webui.contributions import WebUICatalogService
from jiuwenswarm.extensions_v2.webui.models import RenderTarget, UiActionInput, UiActionOutput
from jiuwenswarm.extensions_v2.webui.runtime import WebUIRuntime
from jiuwenswarm.extensions_v2.webui.transport import RenderTransport

PACKAGE_ID = "example.greeting-panel"
SLOT_ID = "conversation.input.above"
SOURCE = Path(__file__).resolve().parent / "greeting_package"
GREETING = "你好，系统插件已生效"


class RecordingPush:
    """Only transport is replaced; loader, Handler, Ops and rendering stay real."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.available = True
        self.cancel = False

    async def send_push(self, message: dict[str, Any]) -> bool:
        if self.cancel:
            raise asyncio.CancelledError
        if not self.available:
            return False
        self.messages.append(message)
        return True


@dataclass
class Host:
    manager: ExtensionManager
    runtime: WebUIRuntime
    push: RecordingPush


@pytest_asyncio.fixture
async def host(tmp_path: Path) -> AsyncIterator[Host]:
    # 只写 pytest 临时目录，不读取或修改用户的 ~/.jiuwenswarm 安装区。
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


def load_script(name: str) -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts" / f"{name}.py"
    module_name = f"package_example_{name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def package_zip(tmp_path: Path) -> Path:
    # 加载和渲染测试使用正式打包脚本的产物，与最终交付布局保持一致。
    return load_script("build_package").build_package(SOURCE, tmp_path / "greeting.zip")


async def install(host: Host, tmp_path: Path) -> None:
    reports = await host.manager.install([package_zip(tmp_path)])
    assert len(reports) == 1 and reports[0].status == "SUCCESS", reports
    assert any(item.package_id == PACKAGE_ID for item in host.manager.active_packages())
    assert host.runtime.catalog().current().allows_slot(SLOT_ID)


async def ready(host: Host, session_id: str = "session-1", slot: str = SLOT_ID) -> UiActionOutput | None:
    # 复用现有的上行事件入口；此触发仅在测试中，不新增生产业务入口。
    return await host.runtime.dispatch(
        UiActionInput(WebUIEvent.READY.value, RenderTarget(session_id, slot)),
        user_id="test-user",
    )


def rendered(host: Host) -> list[dict[str, Any]]:
    return [message for message in host.push.messages if message["payload"]["event_type"] == FRAME_RENDER]


@pytest.mark.asyncio
async def test_real_zip_load_identity_and_render_value(host: Host, tmp_path: Path) -> None:
    await install(host, tmp_path)
    receipt = await ready(host)
    assert receipt is not None and receipt.accepted
    frames = rendered(host)
    assert len(frames) == 1
    payload = frames[0]["payload"]
    assert payload["root"]["component"] == "Text"
    assert payload["root"]["props"]["value"] == GREETING
    assert payload["target"]["package_id"] == PACKAGE_ID
    assert payload["target"]["session_id"] == "session-1"


@pytest.mark.asyncio
async def test_unmatched_slot_keeps_default_result(host: Host, tmp_path: Path) -> None:
    await install(host, tmp_path)
    receipt = await ready(host, slot="conversation.input.below")
    assert receipt is not None and receipt.accepted
    assert rendered(host) == []


@pytest.mark.asyncio
async def test_repeated_ready_updates_one_instance(host: Host, tmp_path: Path) -> None:
    await install(host, tmp_path)
    await ready(host)
    await ready(host)
    frames = rendered(host)
    assert [frame["payload"]["revision"] for frame in frames] == [1, 2]
    assert len(host.runtime.registry.snapshot()) == 1


@pytest.mark.asyncio
async def test_sessions_have_separate_targets(host: Host, tmp_path: Path) -> None:
    await install(host, tmp_path)
    await ready(host, "session-1")
    await ready(host, "session-2")
    assert [frame["session_id"] for frame in rendered(host)] == ["session-1", "session-2"]
    assert len(host.runtime.registry.snapshot()) == 2


@pytest.mark.asyncio
async def test_delivery_failure_is_not_reported_as_success(host: Host, tmp_path: Path) -> None:
    await install(host, tmp_path)
    host.push.available = False
    receipt = await ready(host)
    assert receipt is not None and not receipt.accepted
    assert receipt.status == "render_failed"
    assert rendered(host) == []
    assert not host.runtime.registry.snapshot()


@pytest.mark.asyncio
async def test_cancelled_delivery_remains_cancelled(host: Host, tmp_path: Path) -> None:
    await install(host, tmp_path)
    host.push.cancel = True
    try:
        with pytest.raises(asyncio.CancelledError):
            await ready(host)
    finally:
        host.push.cancel = False


@pytest.mark.asyncio
async def test_reload_uses_modified_installed_copy(host: Host, tmp_path: Path) -> None:
    await install(host, tmp_path)
    await ready(host)
    record = next(record for record in host.manager.active_packages() if record.package_id == PACKAGE_ID)
    entry = record.root / "greeting" / "extension.py"
    content = entry.read_text(encoding="utf-8")
    entry.write_text(content.replace(GREETING, "问候语已更新"), encoding="utf-8")
    report = await host.manager.reload(PACKAGE_ID)
    assert report.status == "SUCCESS", report
    await ready(host)
    assert rendered(host)[-1]["payload"]["root"]["props"]["value"] == "问候语已更新"


@pytest.mark.asyncio
async def test_uninstall_removes_handler_and_rendered_view(host: Host, tmp_path: Path) -> None:
    await install(host, tmp_path)
    await ready(host)
    before = len(rendered(host))
    report = await host.manager.uninstall(PACKAGE_ID)
    assert report.status == "SUCCESS", report
    assert any(message["payload"]["event_type"] == FRAME_CLEAR for message in host.push.messages)
    assert not host.runtime.registry.snapshot()
    assert not host.runtime.catalog().current().items
    receipt = await ready(host)
    assert receipt is not None and receipt.accepted
    assert len(rendered(host)) == before


@pytest.mark.asyncio
async def test_final_zip_client_install_reaches_real_loader_and_business_result(
    host: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.channels.cli import extension as client
    from jiuwenswarm.common.extension_cli import LocalControl
    from jiuwenswarm.extensions_v2.management import adapter as adapter_module
    from jiuwenswarm.extensions_v2.management.local_cli import handle

    monkeypatch.setattr(adapter_module, "get_extension_manager", lambda: host.manager)
    monkeypatch.setattr("jiuwenswarm.extensions_v2.assembly.get_webui_runtime", lambda: host.runtime)
    monkeypatch.delenv("AGENT_SERVER_PORT", raising=False)
    monkeypatch.delenv("AGENT_PORT", raising=False)
    archive = package_zip(tmp_path)
    service = LocalControl("agent_server", handle)
    await service.start()
    try:
        args = client.parser().parse_args(["install", str(archive), "--json"])
        result, code = await client.execute(args)
    finally:
        await service.close()
    assert code == 0 and result["status"] == "SUCCESS", result
    assert result["package_id"] == PACKAGE_ID and result["version"] == "1.0.0"
    assert result["install_path"] != str(SOURCE)
    assert result["zip_sha256"] == client.inspect_zip(str(archive))["zip_sha256"]
    receipt = await ready(host)
    assert receipt is not None and receipt.accepted
    assert rendered(host)[-1]["payload"]["root"]["props"]["value"] == GREETING
