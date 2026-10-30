"""Application slots are addressed by the web socket, not by a chat id."""

from __future__ import annotations

from pathlib import Path

import pytest
from openjiuwen.core.events import SessionContext

import jiuwenswarm.extensions_v2.assembly as assembly
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions_v2.webui.adapter import WebUIDispatchAdapter
from jiuwenswarm.extensions_v2.webui import constants
from jiuwenswarm.extensions_v2.webui.constants import (
    CODE_CHANNEL_UNSUPPORTED,
    CODE_CONNECTION_REQUIRED,
    CODE_SCHEMA_ERROR,
    SlotScope,
    SlotSpec,
)
from jiuwenswarm.extensions_v2.webui.models import RenderNode, RenderStatus, RenderTarget, UiRenderInput
from jiuwenswarm.extensions_v2.webui.registry import ActiveRenderRegistry
from jiuwenswarm.extensions_v2.webui.runtime import WebUIRuntime
from jiuwenswarm.extensions_v2.webui.schema import validate_target
from jiuwenswarm.extensions_v2.webui.transport import RenderTransport
from jiuwenswarm.gateway.channel_manager.web import webui_handlers

_EXTENSION = """
from jiuwenswarm.extensions_v2.webui.constants import WebUIEvent
from jiuwenswarm.extensions_v2.webui.models import (
    RenderNode,
    RenderTarget,
    UiActionOutput,
    UiRenderInput,
)


class AppHome:
    def register(self, registrar):
        registrar.register_event_handler(
            WebUIEvent.READY.value,
            self.on_ready,
            {"target.slot_id": "application.test"},
            "user",
        )

    async def on_ready(self, event, ops, next_handler):
        del next_handler
        request = event.event_input
        target = RenderTarget(
            session_id="",
            slot_id=request.target.slot_id,
            package_id="demo.app-home",
            instance_key="home",
        )
        await ops.ui.render(
            UiRenderInput(target=target, root=RenderNode("Text", {"value": "home"}))
        )
        return UiActionOutput(accepted=True, status="rendered")


def create_extension():
    return AppHome()
"""

_MANIFEST = """
schemaVersion: 1
package:
  id: demo.app-home
  version: 1.0.0
  displayName: App Home
extensions:
  - id: app-home
    runtime: backend
    module: demo_app/extension.py
ui:
  apiVersion: 1
  items:
    - type: web.slot.contribution
      id: home
      slot: application.test
      order: 1
      panel:
        title: Home
      renderer:
        kind: webui-tree
"""


class _Push:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_push(self, message: dict) -> bool:
        self.messages.append(message)
        return True


def _write_package(root: Path) -> None:
    package = root / "demo_app"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "extension.py").write_text(_EXTENSION, encoding="utf-8")
    (root / "manifest.yaml").write_text(_MANIFEST, encoding="utf-8")


_TEST_SLOT = "application.test"


@pytest.fixture(autouse=True)
def _allow_test_application_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """生产白名单不含应用级槽位；测试临时登记一行。"""

    specs = dict(constants.SLOT_SPECS)
    specs[_TEST_SLOT] = SlotSpec("list", SlotScope.APPLICATION)
    monkeypatch.setattr(constants, "SLOT_SPECS", specs)


def _request(channel_id: str, ws_id: str, slot_id: str = _TEST_SLOT) -> AgentRequest:
    metadata = {"ws_id": ws_id} if ws_id else None
    return AgentRequest(
        request_id="ready-1",
        channel_id=channel_id,
        session_id="web_abc",
        req_method=ReqMethod.WEBUI_DISPATCH,
        metadata=metadata,
        params={
            "event": "ui.ready",
            "target": {
                "scope": "application",
                "slot_id": slot_id,
                "package_id": "",
                "instance_key": "",
            },
            "payload": {},
        },
    )


def test_session_slot_still_requires_a_chat_id() -> None:
    report = validate_target(RenderTarget("", "conversation.panel.right"))
    assert report.ok is False
    assert report.code == CODE_SCHEMA_ERROR


def test_application_slot_rejects_a_chat_id_and_a_missing_connection() -> None:
    missing = validate_target(RenderTarget("", "application.test", scope="application"))
    assert missing.ok is False
    assert missing.code == CODE_CONNECTION_REQUIRED
    with_chat = validate_target(
        RenderTarget("web_abc", "application.test", scope="application", connection_id="sock-1")
    )
    assert with_chat.ok is False
    assert "session_id" in with_chat.message


def test_application_frame_omits_the_chat_id_and_names_the_socket() -> None:
    target = RenderTarget(
        "",
        "application.test",
        "demo.app-home",
        "home",
        scope="application",
        connection_id="sock-1",
    )
    assert "session_id" not in target.to_payload()
    assert target.to_payload()["scope"] == "application"
    assert target.key != RenderTarget(
        "",
        "application.test",
        "demo.app-home",
        "home",
        scope="application",
        connection_id="sock-2",
    ).key


@pytest.mark.asyncio
async def test_capacity_is_per_connection_for_application_slots() -> None:
    registry = ActiveRenderRegistry(max_instances_per_session=1)
    first = RenderTarget("", "application.test", "pkg.a", "home", scope="application", connection_id="sock-1")
    other = RenderTarget("", "application.test", "pkg.b", "home", scope="application", connection_id="sock-2")
    second = RenderTarget("", "application.test", "pkg.b", "home", scope="application", connection_id="sock-1")

    async with registry.claim(first):
        pass
    async with registry.claim(other) as claim:
        assert claim.evicted == ()
    async with registry.claim(second) as claim:
        assert [item.target.package_id for item in claim.evicted] == ["pkg.a"]


@pytest.mark.asyncio
async def test_render_uses_the_caller_connection_and_rejects_a_mismatch(tmp_path: Path) -> None:
    from jiuwenswarm.extensions_v2.event_types.context_usage import register_context_usage_events
    from jiuwenswarm.extensions_v2.management import ExtensionManager
    from jiuwenswarm.extensions_v2.webui.contributions import WebUICatalogService
    from openjiuwen.core.events import ExtensionEventBus

    class FakeTransport:
        def __init__(self) -> None:
            self.errors: list[tuple] = []

        async def send_frame(self, frame) -> bool:
            return True

        async def send_clear(self, frame) -> bool:
            return True

        async def send_error(self, target, code, message="", *, revision=0) -> bool:
            self.errors.append((target.connection_id, code))
            return True

    bus = ExtensionEventBus()
    manager = ExtensionManager(tmp_path / "store")
    transport = FakeTransport()
    runtime = WebUIRuntime(
        bus=bus,
        transport=transport,
        catalog=WebUICatalogService(manager.active_packages),
    )

    def host_event_types(event_bus: ExtensionEventBus) -> None:
        runtime.register_event_types(event_bus)
        register_context_usage_events(event_bus)

    await manager.init(bus, host_event_types=host_event_types, host_handlers=runtime.register_handlers)
    target = RenderTarget("", "application.test", "pkg", "home", connection_id="other")
    output = await runtime.render(
        UiRenderInput(target=target, root=RenderNode("Text", {"value": "x"})),
        SessionContext(session_id="sock-1", metadata={"ws_id": "sock-1"}),
    )
    assert output is not None and output.status is RenderStatus.REJECTED
    assert transport.errors == [("sock-1", CODE_SCHEMA_ERROR)]


@pytest.mark.asyncio
async def test_dispatch_renders_each_socket_and_refuses_other_channels(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path / "installed")
    server = _Push()
    installed = await assembly.install_extensions(server)
    assert installed is not None
    try:
        package = tmp_path / "src"
        _write_package(package)
        reports = await installed.manager.install([package])
        assert reports[0].status == "SUCCESS"

        missing = await WebUIDispatchAdapter().handle(_request("web", ""))
        assert missing.ok is False
        assert missing.payload["code"] == CODE_CONNECTION_REQUIRED

        foreign = await WebUIDispatchAdapter().handle(_request("feishu", "sock-feishu"))
        assert foreign.ok is False
        assert foreign.payload["code"] == CODE_CHANNEL_UNSUPPORTED
        assert server.messages == []

        for socket_id in ("sock-1", "sock-2"):
            response = await WebUIDispatchAdapter().handle(_request("web", socket_id))
            assert response.ok is True
            assert response.payload["status"] == "rendered"
        assert [message["metadata"]["ws_id"] for message in server.messages] == ["sock-1", "sock-2"]
        assert "session_id" not in server.messages[0]
        assert server.messages[0]["channel_id"] == "web"
        assert server.messages[0]["payload"]["target"]["scope"] == "application"
        assert "session_id" not in server.messages[0]["payload"]["target"]
        assert installed.webui.registry.get(
            RenderTarget("", "application.test", "demo.app-home", "home", connection_id="sock-1")
        ) is not None
        assert installed.webui.registry.get(
            RenderTarget("", "application.test", "demo.app-home", "home", connection_id="sock-2")
        ) is not None
    finally:
        await assembly.shutdown_extensions()


@pytest.mark.asyncio
async def test_transport_addresses_an_application_frame_by_socket() -> None:
    from jiuwenswarm.extensions_v2.webui.models import RenderFrame, RenderMode

    push = _Push()
    transport = RenderTransport(push=push)
    target = RenderTarget(
        "",
        "application.test",
        "demo.app-home",
        "home",
        scope="application",
        connection_id="sock-1",
    )
    sent = await transport.send_frame(
        RenderFrame(
            target=target,
            revision=1,
            mode=RenderMode.REPLACE,
            root=RenderNode("Text", {"value": "home"}),
        )
    )
    assert sent is True
    message = push.messages[0]
    assert message["channel_id"] == "web"
    assert message["metadata"] == {"ws_id": "sock-1"}
    assert "session_id" not in message


@pytest.mark.asyncio
async def test_gateway_forwards_the_socket_id(monkeypatch) -> None:
    captured: dict = {}

    async def fake_proxy(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(webui_handlers, "proxy_unary_request", fake_proxy)

    class _Channel:
        channel_id = "web"

        def register_method(self, name, handler) -> None:
            del name
            self.handler = handler

    channel = _Channel()
    webui_handlers.register_webui_handlers(channel, lambda: object())
    socket = type("Socket", (), {"_jiuwen_ws_id": "sock-1"})()
    await channel.handler(socket, "req-1", {"event": "ui.ready", "target": {}}, None)
    assert captured["metadata"] == {"ws_id": "sock-1"}
    assert captured["session_id"] is None


@pytest.mark.asyncio
async def test_workbench_example_entry_renders_avatar_and_clears_on_close(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path / "installed")
    server = _Push()
    installed = await assembly.install_extensions(server)
    assert installed is not None
    try:
        example_dir = Path(assembly.__file__).resolve().parent / "examples" / "webui_workbench_v2"
        reports = await installed.manager.install([example_dir])
        assert reports[0].status == "SUCCESS"

        ready_req = AgentRequest(
            request_id="entry-ready",
            channel_id="web",
            req_method=ReqMethod.WEBUI_DISPATCH,
            metadata={"ws_id": "sock-entry"},
            params={
                "event": "ui.ready",
                "target": {
                    "scope": "application",
                    "slot_id": "application.entry",
                    "package_id": "",
                    "instance_key": "",
                },
                "payload": {"reason": "first_visit"},
            },
        )
        ready_resp = await WebUIDispatchAdapter().handle(ready_req)
        assert ready_resp.ok is True
        assert ready_resp.payload["status"] == "rendered"
        assert len(server.messages) == 1
        frame = server.messages[0]["payload"]
        assert frame["event_type"] == "webui.render"
        assert frame["target"] == {
            "scope": "application",
            "slot_id": "application.entry",
            "package_id": "webui.workbench-v2",
            "instance_key": "workbench",
        }
        avatar_node = frame["root"]["children"][0]["children"][0]
        assert avatar_node["component"] == "Avatar"
        assert avatar_node["props"]["url"] == "/logo.svg"

        close_req = AgentRequest(
            request_id="entry-close",
            channel_id="web",
            req_method=ReqMethod.WEBUI_DISPATCH,
            metadata={"ws_id": "sock-entry"},
            params={
                "event": "ui.click",
                "target": {
                    "scope": "application",
                    "slot_id": "application.entry",
                    "package_id": "webui.workbench-v2",
                    "instance_key": "workbench",
                },
                "payload": {
                    "revision": 1,
                    "node_key": "0.0.2",
                    "action_id": "workbench.entry.close",
                    "args": {},
                    "data": {},
                },
            },
        )
        close_resp = await WebUIDispatchAdapter().handle(close_req)
        assert close_resp.ok is True
        assert close_resp.payload["status"] == "cleared"
        assert len(server.messages) == 2
        clear_frame = server.messages[1]["payload"]
        assert clear_frame["event_type"] == "webui.render.clear"
        assert clear_frame["reason"] == "requested"
    finally:
        await assembly.shutdown_extensions()
