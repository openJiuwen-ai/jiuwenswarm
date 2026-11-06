# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from shutil import copytree
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.events import Event
from openjiuwen.core.extension import ActivePackageRecord

from jiuwenswarm.common.e2a import E2AEnvelope, e2a_to_agent_request
from jiuwenswarm.common.schema.agent import AgentRef, AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions_v2.management import adapter as adapter_module
from jiuwenswarm.extensions_v2.management.adapter import PackageManagementAdapter
from jiuwenswarm.extensions_v2.management.registry import ExtensionManager
from jiuwenswarm.gateway.channel_manager.web import extension_package_handlers as proxy_module

from .support import METADATA_EXTENSION, archive_package, make_bus, write_package


def _request(method: ReqMethod, params: Any) -> AgentRequest:
    return AgentRequest(
        request_id="manage-1",
        channel_id="web",
        req_method=method,
        params=params,
        metadata={"trace": "trace-1"},
        agent_ref=AgentRef.default(),
    )


async def _manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ExtensionManager:
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await manager.init(make_bus())
    monkeypatch.setattr(adapter_module, "get_extension_manager", lambda: manager)
    return manager


async def test_request_install_query_reload_and_uninstall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    source = write_package(tmp_path / "source", module_source=METADATA_EXTENSION)
    adapter = PackageManagementAdapter()
    request = _request(ReqMethod.EXTENSIONS_V2_INSTALL, {"paths": [str(archive_package(source))]})

    installed = await adapter.handle(request)

    assert installed.ok
    assert installed.payload["reports"][0]["status"] == "SUCCESS"
    assert (installed.request_id, installed.metadata, installed.agent_ref) == (
        request.request_id, request.metadata, request.agent_ref,
    )
    state = await adapter.handle(_request(ReqMethod.EXTENSIONS_V2_QUERY, {}))
    assert state.payload["state"]["active"][0]["generation"] == 1
    assert state.payload["state"]["published"]["test.pkg"]
    json.dumps(state.payload)

    installed_package = installed.payload["installed_packages"][0]
    assert installed_package["version"] == "1.0.0"
    assert installed_package["generation"] == 1
    entry = Path(installed_package["install_path"]) / "extension.py"
    assert entry.parent == manager.install_dir / "test.pkg" / "1.0.0"
    entry.write_text(
        METADATA_EXTENSION.replace(
            "return (metadata.package_id, metadata.package_version, str(metadata.root))", 'return "new code"',
        ),
        encoding="utf-8",
    )
    reloaded = await adapter.handle(_request(ReqMethod.EXTENSIONS_V2_RELOAD, {"package_id": "test.pkg"}))
    assert reloaded.payload["reports"][0]["current_generation"] == 2
    assert await manager.registry.context.bus.trigger(Event("test.event", {})) == "new code"

    removed = await adapter.handle(_request(ReqMethod.EXTENSIONS_V2_UNINSTALL, {"package_id": "test.pkg"}))
    assert removed.payload["reports"][0]["status"] == "SUCCESS"
    assert (await manager.query()).active == ()
    assert not entry.exists()


async def test_failed_reload_returns_report_and_preserves_old_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    await manager.install([write_package(tmp_path / "source", module_source=METADATA_EXTENSION)])
    entry = manager.install_dir / "test.pkg" / "1.0.0" / "extension.py"
    entry.write_text('raise RuntimeError("bad replacement")\n', encoding="utf-8")

    response = await PackageManagementAdapter().handle(
        _request(ReqMethod.EXTENSIONS_V2_RELOAD, {"package_id": "test.pkg"})
    )

    report = response.payload["reports"][0]
    assert response.ok and report["status"] == "FAILED"
    assert report["error"]["code"] == "EXTENSION_LOAD_FAILED"
    assert (await manager.query()).active[0].generation == 1
    assert (await manager.registry.context.bus.trigger(Event("test.event", {})))[0] == "test.pkg"
    json.dumps(response.payload)


@pytest.mark.parametrize(
    ("method", "params"),
    [
        (ReqMethod.EXTENSIONS_V2_INSTALL, {}),
        (ReqMethod.EXTENSIONS_V2_INSTALL, {"paths": []}),
        (ReqMethod.EXTENSIONS_V2_INSTALL, {"paths": "source"}),
        (ReqMethod.EXTENSIONS_V2_INSTALL, {"paths": ["relative/path"]}),
        (ReqMethod.EXTENSIONS_V2_INSTALL, {"paths": [False]}),
        (ReqMethod.EXTENSIONS_V2_QUERY, {"package_id": ""}),
        (ReqMethod.EXTENSIONS_V2_QUERY, {"package_id": "bad name"}),
        (ReqMethod.EXTENSIONS_V2_UNINSTALL, {"package_id": "pkg..name"}),
        (ReqMethod.EXTENSIONS_V2_UNINSTALL, {"package_id": "../pkg"}),
        (ReqMethod.EXTENSIONS_V2_RELOAD, {"package_id": "x" * 129}),
        (ReqMethod.EXTENSIONS_V2_QUERY, {"extra": "ignored"}),
        (ReqMethod.EXTENSIONS_V2_QUERY, []),
        (ReqMethod.EXTENSIONS_V2_UNINSTALL, {}),
        (ReqMethod.EXTENSIONS_V2_RELOAD, {"package_id": 42}),
        (ReqMethod.WEBUI_DISPATCH, {}),
    ],
)
async def test_invalid_request_is_rejected_before_resolving_runtime(
    method: ReqMethod, params: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_runtime_lookup() -> None:
        raise AssertionError("invalid request reached the runtime")

    monkeypatch.setattr(adapter_module, "get_extension_manager", unexpected_runtime_lookup)
    request = _request(method, params)
    response = await PackageManagementAdapter().handle(request)

    assert not response.ok and response.payload["code"] == "INVALID_PARAMS"
    assert response.agent_ref == request.agent_ref


@pytest.mark.parametrize("uninitialized", [False, True])
async def test_unavailable_manager_is_not_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uninitialized: bool,
) -> None:
    manager = None
    if uninitialized:
        manager = ExtensionManager(
            tmp_path,
            report_path=tmp_path.parent / (tmp_path.name + "-report.yaml"),
        )
    monkeypatch.setattr(adapter_module, "get_extension_manager", lambda: manager)
    response = await PackageManagementAdapter().handle(_request(ReqMethod.EXTENSIONS_V2_QUERY, {}))
    assert not response.ok
    assert response.payload["code"] == "EXTENSION_MANAGER_NOT_INITIALIZED"
    assert manager is None or not manager.initialized


async def test_gateway_proxies_preserve_method_and_request_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    handlers: dict[str, Any] = {}
    channel = SimpleNamespace(register_method=lambda method, handler: handlers.setdefault(method, handler))
    proxy = AsyncMock()
    client = object()
    monkeypatch.setattr(proxy_module, "proxy_unary_request", proxy)
    proxy_module.register_extension_package_handlers(channel, lambda: client)

    gateway_only = {ReqMethod.EXTENSIONS_V2_IMPORT.value}
    assert set(handlers) == PackageManagementAdapter.methods | gateway_only
    for method, handler in handlers.items():
        if method in gateway_only:
            continue
        params = {"package_id": "test.pkg"}
        await handler("socket", "req-1", params, "session-1", user_id="user-1")
        call = proxy.await_args.kwargs
        assert call["req_method"].value == method
        assert call["params"] is params
        assert call["agent_client"] is client
        assert call["drop_params"] == ("session_id",)
        assert call["preserve_error_payload"] is True
        assert (call["req_id"], call["session_id"], call["user_id"]) == ("req-1", "session-1", "user-1")


@pytest.mark.parametrize("suffix", [".zip", ".ZIP"])
async def test_install_returns_actual_store_path_without_extra_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    source = write_package(tmp_path / "source with spaces" / "扩展包")
    source = archive_package(source, suffix=suffix)
    query = AsyncMock(wraps=manager.query)
    monkeypatch.setattr(manager, "query", query)

    response = await PackageManagementAdapter().handle(
        _request(ReqMethod.EXTENSIONS_V2_INSTALL, {"paths": [str(source)]})
    )

    assert response.ok and response.payload["reports"][0]["status"] == "SUCCESS"
    installed = response.payload["installed_packages"]
    assert installed == [{
        "package_id": "test.pkg", "version": "1.0.0", "generation": 1,
        "install_path": str((manager.install_dir / "test.pkg" / "1.0.0").resolve()),
    }]
    assert Path(installed[0]["install_path"]).is_absolute()
    assert (Path(installed[0]["install_path"]) / "manifest.json").is_file()
    assert installed[0]["install_path"] != str(source)
    query.assert_not_awaited()
    json.dumps(response.payload)


async def test_install_paths_only_describe_successful_reports_from_this_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr("jiuwenswarm.extensions_v2.assembly.get_webui_runtime",
                        lambda: SimpleNamespace(notify_packages_changed=notify))
    await manager.install([write_package(tmp_path / "old", package_id="existing.pkg")])
    good = write_package(tmp_path / "good", package_id="new.pkg")
    bad = write_package(tmp_path / "bad", package_id="broken.pkg", module_source='raise RuntimeError("broken")')

    response = await PackageManagementAdapter().handle(
        _request(ReqMethod.EXTENSIONS_V2_INSTALL, {"paths": [str(archive_package(good)), str(archive_package(bad))]})
    )

    assert response.ok
    assert {report["package_id"]: report["status"] for report in response.payload["reports"]} == {
        "new.pkg": "SUCCESS", "broken.pkg": "FAILED",
    }
    assert [item["package_id"] for item in response.payload["installed_packages"]] == ["new.pkg"]
    assert response.payload["ui_notified"] is True
    notify.assert_awaited_once_with(["new.pkg"])


@pytest.mark.parametrize("notification", [None, False, OSError("connection closed")])
async def test_install_notification_failure_preserves_successful_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, notification: object,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    notify = AsyncMock(return_value=False, side_effect=notification if isinstance(notification, OSError) else None)
    runtime = None if notification is None else SimpleNamespace(notify_packages_changed=notify)
    monkeypatch.setattr("jiuwenswarm.extensions_v2.assembly.get_webui_runtime", lambda: runtime)
    source = archive_package(write_package(tmp_path / "source"))

    response = await PackageManagementAdapter().handle(_request(ReqMethod.EXTENSIONS_V2_INSTALL, {"paths": [str(source)]}))

    assert response.ok and response.payload["reports"][0]["status"] == "SUCCESS"
    assert response.payload["ui_notified"] is False
    assert (await manager.query("test.pkg")).active[0].package_id == "test.pkg"


async def test_failed_install_does_not_notify_ready_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _manager(tmp_path, monkeypatch)
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr("jiuwenswarm.extensions_v2.assembly.get_webui_runtime",
                        lambda: SimpleNamespace(notify_packages_changed=notify))
    source = archive_package(write_package(tmp_path / "source", module_source='raise RuntimeError("broken")'))

    response = await PackageManagementAdapter().handle(_request(ReqMethod.EXTENSIONS_V2_INSTALL, {"paths": [str(source)]}))

    assert response.ok and response.payload["reports"][0]["status"] == "FAILED"
    assert response.payload["ui_notified"] is False
    notify.assert_not_awaited()


async def test_web_install_notifies_mounted_slot_and_renders_on_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.extensions_v2.webui.adapter import WebUIDispatchAdapter
    from jiuwenswarm.extensions_v2.webui.contributions import WebUICatalogService
    from jiuwenswarm.extensions_v2.webui.runtime import WebUIRuntime
    from jiuwenswarm.extensions_v2.webui.transport import RenderTransport
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel
    from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler

    source = write_package(tmp_path / "source", module_source='''\
from jiuwenswarm.extensions_v2.webui.models import RenderNode, RenderTarget, UiRenderInput

class Extension:
    def register(self, registrar):
        registrar.register_event_handler("ui.ready", self.ready, {"target.slot_id": "conversation.input.below"})

    async def ready(self, event, ops, next_handler):
        receipt = await next_handler(event)
        target = RenderTarget(ops.session_context.session_id, "conversation.input.below", "test.pkg", "panel")
        await ops.ui.render(UiRenderInput(target, root=RenderNode("Text", {"value": "Installed without a page reload"})))
        return receipt

def create_extension():
    return Extension()
''')
    manifest = source / "manifest.json"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["ui"] = {
        "apiVersion": 1,
        "items": [
            {
                "type": "web.slot.contribution",
                "id": "panel",
                "slot": "conversation.input.below",
                "renderer": {"kind": "webui-tree"},
            }
        ],
    }
    manifest.write_text(json.dumps(document, indent=2), encoding="utf-8")
    manager = ExtensionManager(
        tmp_path / "installed",
        report_path=tmp_path / "report.yaml",
        store_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
    )
    bus = make_bus()
    push = SimpleNamespace(send_push=AsyncMock(return_value=True))
    runtime = WebUIRuntime(bus=bus, transport=RenderTransport(push=push), catalog=WebUICatalogService(manager.active_packages))
    await manager.init(bus, host_event_types=runtime.register_event_types, host_handlers=runtime.register_handlers)
    monkeypatch.setattr(adapter_module, "get_extension_manager", lambda: manager)
    monkeypatch.setattr("jiuwenswarm.extensions_v2.assembly.get_webui_runtime", lambda: runtime)
    ready = AgentRequest(
        request_id="ready-before-install", channel_id="web", session_id="session-1", req_method=ReqMethod.WEBUI_DISPATCH,
        metadata={"ws_id": "ws-1"}, params={"event": "ui.ready", "payload": {},
            "target": {"slot_id": "conversation.input.below", "package_id": "", "instance_key": ""}},
    )
    try:
        assert (await WebUIDispatchAdapter().handle(ready)).payload["code"] == "NO_CONTRIBUTION"
        push.send_push.assert_not_awaited()
        handlers: dict[str, Any] = {}
        async def send_request(envelope: E2AEnvelope) -> AgentResponse:
            return await PackageManagementAdapter().handle(e2a_to_agent_request(envelope))
        channel = SimpleNamespace(channel_id="web", send_response=AsyncMock(),
                                  register_method=lambda method, handler: handlers.setdefault(method, handler))
        proxy_module.register_extension_package_handlers(channel, lambda: SimpleNamespace(send_request=send_request))

        await handlers[ReqMethod.EXTENSIONS_V2_INSTALL.value](
            "socket", "web-install", {"paths": [str(archive_package(source))]}, "session-1", user_id="web-user",
        )

        assert channel.send_response.await_args.kwargs["payload"]["ui_notified"] is True
        push.send_push.assert_awaited_once()
        notification = push.send_push.await_args.args[0]
        assert notification["metadata"] == {"ws_id": "ws-1"}
        assert notification["payload"] == {"event_type": "extensions.v2.installed",
                                           "package_ids": ["test.pkg"], "session_id": "session-1"}
        web_channel = object.__new__(WebChannel)
        web_channel._session_busy = {}
        web_channel._clients_by_key = {}
        web_channel._ws_by_id = {"ws-1": object()}
        web_channel._a4p_rebound_ws = {}
        web_channel._broadcast_to = AsyncMock()
        chunk = SimpleNamespace(request_id=notification["request_id"], channel_id="web",
                                payload=notification["payload"], metadata=notification["metadata"])
        message = MessageHandler._chunk_to_message(chunk, session_id="session-1")
        await web_channel.send(message)
        web_channel._broadcast_to.assert_awaited_once()
        wire_frame = web_channel._broadcast_to.await_args.args[0]
        assert wire_frame["event"] == "extensions.v2.installed"
        assert wire_frame["payload"]["package_ids"] == ["test.pkg"]
        # The mounted frontend slot responds to the notification on the same connection.
        ready.params["target"]["package_id"] = wire_frame["payload"]["package_ids"][0]
        assert (await WebUIDispatchAdapter().handle(ready)).payload["accepted"] is True
        frame = push.send_push.await_args.args[0]["payload"]
        assert frame["event_type"] == "webui.render"
        assert frame["root"]["props"]["value"] == "Installed without a page reload"
    finally:
        await manager.shutdown()


async def test_install_does_not_guess_a_path_for_a_different_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    active_packages = manager.active_packages

    def different_generation() -> tuple[ActivePackageRecord, ...]:
        return tuple(replace(record, generation=record.generation + 1) for record in active_packages())

    monkeypatch.setattr(manager, "active_packages", different_generation)
    response = await PackageManagementAdapter().handle(
        _request(ReqMethod.EXTENSIONS_V2_INSTALL, {"paths": [str(archive_package(write_package(tmp_path / "source")))]})
    )

    assert response.ok and response.payload["reports"][0]["status"] == "SUCCESS"
    assert response.payload["installed_packages"] == []


@pytest.mark.parametrize("session_id", [None, "web-session"])
async def test_web_command_parameters_cross_the_real_gateway_proxy_and_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session_id: str | None,
) -> None:
    await _manager(tmp_path, monkeypatch)
    adapter = PackageManagementAdapter()
    envelopes: list[E2AEnvelope] = []
    handlers: dict[str, Any] = {}

    async def send_request(envelope: E2AEnvelope) -> AgentResponse:
        envelopes.append(envelope)
        return await adapter.handle(e2a_to_agent_request(envelope))

    client = SimpleNamespace(send_request=send_request)
    channel = SimpleNamespace(
        channel_id="web", send_response=AsyncMock(),
        register_method=lambda method, handler: handlers.setdefault(method, handler),
    )
    proxy_module.register_extension_package_handlers(channel, lambda: client)
    source = write_package(tmp_path / "source")
    for method, args in (
        (ReqMethod.EXTENSIONS_V2_INSTALL, {"paths": [str(archive_package(source))]}),
        (ReqMethod.EXTENSIONS_V2_QUERY, {"package_id": "test.pkg"}),
        (ReqMethod.EXTENSIONS_V2_RELOAD, {"package_id": "test.pkg"}),
        (ReqMethod.EXTENSIONS_V2_UNINSTALL, {"package_id": "test.pkg"}),
    ):
        params = {**args} if session_id is None else {"session_id": session_id, **args}
        await handlers[method.value]("socket", method.value, params, session_id, user_id="web-user")
        response = channel.send_response.await_args.kwargs
        assert response["ok"] is True
        if method == ReqMethod.EXTENSIONS_V2_QUERY:
            assert response["payload"]["state"]["active"][0]["root"]
        else:
            assert response["payload"]["reports"][0]["status"] == "SUCCESS"
        assert envelopes[-1].params == args
        assert envelopes[-1].session_id == session_id
        assert envelopes[-1].user_id == "web-user"
        if session_id is None:
            assert "session_id" not in params
        else:
            assert params["session_id"] == session_id
    assert len(envelopes) == 4


async def test_gateway_preserves_unknown_outcome_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(manager, "install", AsyncMock(side_effect=RuntimeError("lost report")))
    adapter = PackageManagementAdapter()
    handlers: dict[str, Any] = {}

    async def send_request(envelope: E2AEnvelope) -> AgentResponse:
        return await adapter.handle(e2a_to_agent_request(envelope))

    channel = SimpleNamespace(
        channel_id="web", send_response=AsyncMock(),
        register_method=lambda method, handler: handlers.setdefault(method, handler),
    )
    proxy_module.register_extension_package_handlers(channel, lambda: SimpleNamespace(send_request=send_request))
    source = archive_package(write_package(tmp_path / "source"))
    await handlers[ReqMethod.EXTENSIONS_V2_INSTALL.value](
        "socket", "install-unknown", {"session_id": "session", "paths": [str(source)]}, "session",
    )

    response = channel.send_response.await_args.kwargs
    assert response["ok"] is False
    assert response["payload"]["outcome"] == "unknown"
    assert response["payload"]["state"]["active"] == []
    manager.install.assert_awaited_once()


@pytest.mark.parametrize("source_kind", ["directory", "zip_directory", "text_file", "missing_zip"])
async def test_external_install_rejects_non_zip_files_before_runtime_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_kind: str,
) -> None:
    source = tmp_path / ("package.zip" if source_kind in {"zip_directory", "missing_zip"} else "package")
    if source_kind in {"directory", "zip_directory"}:
        source.mkdir()
    elif source_kind == "text_file":
        source.write_text("not a zip", encoding="utf-8")

    def unexpected_runtime_lookup() -> None:
        raise AssertionError("invalid install source reached the runtime")

    monkeypatch.setattr(adapter_module, "get_extension_manager", unexpected_runtime_lookup)
    response = await PackageManagementAdapter().handle(
        _request(ReqMethod.EXTENSIONS_V2_INSTALL, {"paths": [str(source)]})
    )
    assert not response.ok and response.payload["code"] == "INVALID_PARAMS"
    assert "zip" in response.payload["error"]


async def test_zip_module_error_is_reported_after_source_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _manager(tmp_path, monkeypatch)
    source = write_package(tmp_path / "source", module_source="import definitely_missing_extension_module")
    response = await PackageManagementAdapter().handle(
        _request(ReqMethod.EXTENSIONS_V2_INSTALL, {"paths": [str(archive_package(source))]})
    )
    assert response.ok
    report = response.payload["reports"][0]
    assert report["status"] == "FAILED"
    assert report["error"]["code"] == "EXTENSION_LOAD_FAILED"
    assert "definitely_missing_extension_module" in report["error"]["message"]


async def test_query_includes_ids_for_installed_but_inactive_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    source = write_package(tmp_path / "source", package_id="inactive.pkg")
    installed = manager.install_dir / "inactive.pkg" / "1.0.0"
    copytree(source, installed)
    response = await PackageManagementAdapter().handle(
        _request(ReqMethod.EXTENSIONS_V2_QUERY, {"package_id": "inactive.pkg"})
    )
    assert response.ok and response.payload["state"]["active"] == []
    assert response.payload["state"]["installed_packages"] == [{
        "package_id": "inactive.pkg", "version": "1.0.0", "install_path": str(installed),
    }]


async def test_file_preview_serves_both_domains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _manager(tmp_path, monkeypatch)
    examples = tmp_path / "examples"
    package = examples / "gateway-demo"
    package.mkdir(parents=True)
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "package_type": "extension",
                "schemaVersion": 1,
                "domain": "gateway",
                "package": {"id": "gw.pkg", "version": "1.0.0"},
                "extensions": [{"id": "main", "runtime": "backend", "module": "extension.py"}],
            }
        ),
        encoding="utf-8",
    )
    (package / "extension.py").write_text("def create_extension():\n    return None\n", encoding="utf-8")
    monkeypatch.setattr(adapter_module, "resolve_examples_dir", lambda: examples)
    adapter = PackageManagementAdapter()

    tree = await adapter.handle(_request(ReqMethod.EXTENSIONS_V2_FILE_LIST, {"package_id": "gw.pkg"}))

    assert tree.ok
    assert "manifest.json" in [node["path"] for node in tree.payload["tree"]]

    content = await adapter.handle(
        _request(ReqMethod.EXTENSIONS_V2_FILE_READ, {"package_id": "gw.pkg", "path": "manifest.json"})
    )
    assert content.ok
    assert content.payload["path"] == "manifest.json"


async def test_file_preview_resolves_an_installed_package_outside_examples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    examples = tmp_path / "examples"
    examples.mkdir()
    installed = manager.install_dir / "gw.uploaded" / "1.0.0"
    installed.mkdir(parents=True)
    (installed / "manifest.json").write_text(
        json.dumps(
            {
                "package_type": "extension",
                "schemaVersion": 1,
                "domain": "gateway",
                "package": {"id": "gw.uploaded", "version": "1.0.0"},
                "extensions": [{"id": "main", "runtime": "backend", "module": "extension.py"}],
            }
        ),
        encoding="utf-8",
    )
    (installed / "extension.py").write_text("def create_extension():\n    return None\n", encoding="utf-8")
    monkeypatch.setattr(adapter_module, "resolve_examples_dir", lambda: examples)
    monkeypatch.setattr(adapter_module, "resolve_install_dir", lambda: manager.install_dir)

    response = await PackageManagementAdapter().handle(
        _request(ReqMethod.EXTENSIONS_V2_FILE_LIST, {"package_id": "gw.uploaded"})
    )

    assert response.ok
    assert {node["path"] for node in response.payload["tree"]} == {"manifest.json", "extension.py"}
