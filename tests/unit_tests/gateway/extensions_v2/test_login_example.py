"""Install the actual example ZIP and exercise its browser protocol."""

from __future__ import annotations

import importlib.util
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import httpx
import pytest
from fastapi import FastAPI

from jiuwenswarm.gateway.extensions_v2.config import GatewayExtensionConfig
from jiuwenswarm.gateway.extensions_v2.runtime import GatewayRuntime
from jiuwenswarm.gateway.extensions_v2.models import GatewayRequestOutput
from jiuwenswarm.gateway.extensions_v2.webui.http import attach_browser_routes
from jiuwenswarm.gateway.extensions_v2.webui.models import RenderNode, RenderTarget, UiRenderInput, node_to_payload
from jiuwenswarm.gateway.extensions_v2.webui.bridge import tree_nodes

SOURCE = Path(__file__).resolve().parents[4] / "jiuwenswarm/extensions_v2/examples/auth_login_demo"
PACKAGE = "webui.login-demo"


def _views_module():
    spec = importlib.util.spec_from_file_location("login_avatar_views", SOURCE / "auth_login_demo/views.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name,initial", [
    ("test1", "T"), (" 张三 ", "张"), ("𠮷田", "𠮷"), ("e\u0301lodie", "É"), ("👩‍💻coder", "👩‍💻"),
])
def test_plugin_owns_unicode_avatar_initials(name, initial):
    props = _views_module().account_avatar(name, "Current user")
    assert props["children"] == initial
    assert props["alt"] == "Current user"
    assert "name" not in props


def test_plugin_avatar_color_is_stable_and_its_view_declares_native_styles():
    views = _views_module()
    avatar = views.account_avatar("test1", "Current user")
    assert avatar["style"] == {"backgroundColor": "#89D7D8", "color": "#FFFFFF", "fontSize": 14}
    assert views.account_avatar(" test1 ", "Current user")["style"] == avatar["style"]
    assert views.account_avatar("e\u0301lodie", "Current user")["style"] == views.account_avatar("élodie", "Current user")["style"]
    assert views.account_avatar("test1", "Current user", "/avatar.svg")["src"] == "/avatar.svg"
    with pytest.raises(ValueError, match="must not be empty"):
        views.account_avatar("  ", "Current user")
    tree = views.user_view("en", "test1")
    menu_avatar = tree["props"]["items"][0]["label"][0]["children"][0]["props"]
    assert menu_avatar["children"] == tree["children"][0]["props"]["children"] == "T"
    assert menu_avatar["style"]["backgroundColor"] == avatar["style"]["backgroundColor"]
    assert menu_avatar["size"] == 20 and menu_avatar["style"]["fontSize"] == 10


class BrowserChannel:
    channel_id = "web"

    def __init__(self):
        self.events = []
        self.responses = []
        self.sockets = {}

    async def ensure_ws_id(self, ws):
        if not getattr(ws, "_jiuwen_ws_id", None):
            ws._jiuwen_ws_id = "browser-one"
        self.sockets[ws._jiuwen_ws_id] = ws
        return ws._jiuwen_ws_id

    def ws_by_id(self, connection_id):
        return self.sockets.get(connection_id)

    def connection_user_id(self, ws):
        return None

    async def send_event(self, ws, event, payload):
        self.events.append({"event": event, "payload": json.loads(json.dumps(payload))})

    async def send_response(self, ws, request_id, *, ok, payload=None, error=None, code=None):
        self.responses.append({"id": request_id, "ok": ok, "payload": payload, "code": code})


def _zip(tmp_path):
    spec = importlib.util.spec_from_file_location("example_packer", SOURCE / "pack.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_package(tmp_path / "example.zip")


async def _installed(tmp_path):
    channel = BrowserChannel()
    runtime = GatewayRuntime(channel=channel, config=GatewayExtensionConfig(
        install_dir=tmp_path / "installed",
    ))
    await runtime.start()
    report = (await runtime.packages.install([_zip(tmp_path)]))[0]
    assert report.status == "SUCCESS", report
    return runtime, channel, SimpleNamespace(closed=False)


def _action(runtime, action_id):
    for record in runtime.registry.snapshot():
        if record.root is None:
            continue
        for key, node in tree_nodes(record.root).items():
            for event, action in (node.events or {}).items():
                if action["id"] == action_id:
                    return {"event": event, "target": record.target.to_payload(), "payload": {"node_key": key,
                            "revision": record.revision, "action_id": action_id,
                            "args": dict(action.get("args", {})), "data": {}}}
    raise AssertionError(f"Action not declared: {action_id}")


async def _click(runtime, ws, action_id, data=None, token=""):
    params = _action(runtime, action_id)
    params["payload"]["data"] = data or {}
    context = {"headers": {"Authorization": "Bearer " + token}} if token else {}
    await runtime.intercept(ws, action_id, "webui.dispatch", params, request_context=context)


async def _register(runtime, channel, ws, username):
    await _click(runtime, ws, "login.mode")
    await _click(runtime, ws, "login.register", {"username": username, "password": "Example123!"})
    assert channel.responses[-1]["payload"]["status"] == "logged_in"
    return _saved_token(channel, username)


async def _login(runtime, channel, ws, username):
    await _click(runtime, ws, "login.confirm", {"username": username, "password": "Example123!"})
    return _saved_token(channel, username)


def _saved_token(channel, username):
    operation = next(item["payload"] for item in reversed(channel.events) if item["event"] == "client.operation" and item["payload"]["method"] == "local_storage.set")
    assert operation["method"] == "local_storage.set"
    assert operation["package_id"] == PACKAGE
    assert operation["params"]["value"]["username"] == username
    return operation["params"]["value"]["accessToken"]


async def test_zip_registration_login_logout_and_multiple_persistent_accounts(tmp_path):
    runtime, channel, ws = await _installed(tmp_path)
    try:
        visited, _ = await runtime.intercept(ws, "config", "config.get", {})
        assert visited and channel.responses[-1]["code"] == "AUTH_REQUIRED"
        assert channel.events[0]["event"] == "client.declarations"
        declaration = channel.events[0]["payload"]["packages"][0]
        assert len(declaration["requestBindings"]) == 2
        assert any(item["event"] == "webui.render" for item in channel.events)
        for username in ("alpha", "bravo"):
            token = await _register(runtime, channel, ws, username)
            context = {"headers": {"Authorization": "Bearer " + token}}
            visited, params = await runtime.intercept(ws, "chat", "chat.send", {"message": "hello"}, request_context=context)
            assert not visited and params == {"message": "hello"}
            assert "request_context" not in params
            record = next(item for item in runtime.registry.snapshot() if item.target.slot_id == "application.sidebar.footer")
            footer = {"root": node_to_payload(record.root)}
            avatar = footer["root"]["children"][0]["props"]
            assert "name" not in avatar
            assert avatar["children"] == username[0].upper()
            assert avatar["alt"] == "当前用户"
            assert avatar["style"]["color"] == "#FFFFFF"
            assert avatar["style"]["fontSize"] == 14
            assert footer["root"]["props"]["items"] == [
                {"key": "account", "disabled": True, "label": [
                    {"component": "Box", "props": {"direction": "row", "align": "center", "gap": "sm"},
                     "children": [{"component": "Avatar", "props": {**avatar, "size": 20,
                                  "style": {**avatar["style"], "fontSize": 10}}},
                                  {"component": "Text", "props": {"value": username}}]},
                ]},
                {"type": "divider"}, {"key": "logout", "label": "退出登录"},
            ]
            assert footer["root"]["props"]["styles"] == {"root": {"width": 200}}
            assert footer["root"]["props"]["autoFocus"] is False
            assert footer["root"]["props"]["destroyOnHidden"] is True
            await _click(runtime, ws, "user.menu", {"key": "account"}, token)
            assert channel.responses[-1]["payload"]["status"] == "invalid_action"
            await _click(runtime, ws, "user.menu", {"key": "logout"}, token)
            assert channel.responses[-1]["payload"]["status"] == "logged_out"
            operations = [item["payload"] for item in channel.events if item["event"] == "client.operation"]
            assert [item["method"] for item in operations[-2:]] == ["local_storage.delete", "page.reload"]
            visited, _ = await runtime.intercept(ws, "old", "config.get", {}, request_context=context)
            assert visited and channel.responses[-1]["code"] == "AUTH_REQUIRED"
        await runtime.packages.reload(PACKAGE)
        await runtime.intercept(ws, "reload", "config.get", {})
        await _login(runtime, channel, ws, "alpha")
        with ZipFile(_zip(tmp_path)) as archive:
            assert "manifest.json" in archive.namelist()
            assert not any("accounts.sqlite3" in name or "__pycache__" in name for name in archive.namelist())
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("language,title,username_label,logout_label", [
    ("zh", "欢迎使用WorkSwarm", "用户名", "退出登录"),
    ("en", "Welcome to WorkSwarm", "Username", "Sign out"),
])
async def test_installed_example_localizes_form_and_menu(tmp_path, language, title, username_label, logout_label):
    runtime, channel, ws = await _installed(tmp_path)
    try:
        package = runtime.active_packages()[0]
        (package.root / "settings.json").write_text(json.dumps({"language": language, "layout": "center"}), encoding="utf-8")
        await runtime.packages.reload(PACKAGE)
        await runtime.intercept(ws, "localized-config", "config.get", {})
        entry = next(item for item in runtime.registry.snapshot() if item.target.slot_id == "application.entry")
        nodes = tree_nodes(entry.root).values()
        assert any(node.component == "Text" and node.props.get("value") == title for node in nodes)
        fields = [node for node in tree_nodes(entry.root).values() if node.component == "Form.Item"]
        assert fields[0].props["label"] == username_label
        assert all(rule.get("message") for field in fields for rule in field.props["rules"])
        token = await _register(runtime, channel, ws, "localized-user")
        await runtime.intercept(ws, "localized-user", "config.get", {},
            request_context={"headers": {"Authorization": "Bearer " + token}})
        footer = next(item for item in runtime.registry.snapshot() if item.target.slot_id == "application.sidebar.footer")
        assert footer.root.props["items"][-1]["label"] == logout_label
    finally:
        await runtime.shutdown()


async def test_stale_or_forged_click_never_queues_storage(tmp_path):
    runtime, channel, ws = await _installed(tmp_path)
    try:
        await runtime.intercept(ws, "config", "config.get", {})
        params = _action(runtime, "login.confirm")
        params["payload"]["data"] = {"username": "alpha", "password": "Example123!"}
        params["payload"]["action_id"] = "login.register"
        await runtime.intercept(ws, "forged", "webui.dispatch", params)
        assert channel.responses[-1]["payload"]["status"] == "invalid_action"
        params = _action(runtime, "login.confirm")
        params["payload"]["revision"] += 1
        await runtime.intercept(ws, "stale", "webui.dispatch", params)
        assert channel.responses[-1]["payload"]["status"] == "inactive"
        assert not any(item["event"] == "client.operation" for item in channel.events)
    finally:
        await runtime.shutdown()


async def test_http_headers_and_asset_boundaries(tmp_path):
    runtime, channel, ws = await _installed(tmp_path)
    try:
        await runtime.intercept(ws, "config", "config.get", {})
        token = await _register(runtime, channel, ws, "alpha")
        app = FastAPI()
        attach_browser_routes(app, lambda: runtime)
        @app.get("/business")
        async def business():
            return {"ok": True}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/business")).status_code == 401
            assert (await client.get("/business", headers={"Authorization": "Bearer " + token})).status_code == 200
            generation = runtime.browser.generations[PACKAGE]
            asset_url = f"/extension-assets/{PACKAGE}/{generation}/background.svg"
            for name in ("background.svg", "promotion.png", "avatar.svg"):
                expected = (SOURCE / "assets" / name).read_bytes()
                for method in ("GET", "HEAD"):
                    response = await client.request(method, f"/extension-assets/{PACKAGE}/{generation}/{name}")
                    assert response.status_code == 200
                    assert response.headers["content-type"].startswith("image/")
                    assert response.headers["content-length"] == str(len(expected))
                    assert response.headers["cache-control"] == "no-store"
                    assert response.headers["x-content-type-options"] == "nosniff"
                    assert response.content == (expected if method == "GET" else b"")
            assert (await client.get(f"/extension-assets/{PACKAGE}/{generation}/missing.svg")).status_code == 404
            with pytest.raises(ValueError):
                runtime.browser.asset_file(PACKAGE, generation, "../settings.json")
            await runtime.packages.reload(PACKAGE)
            current_generation = runtime.browser.generations[PACKAGE]
            assert current_generation != generation
            assert (await client.get(asset_url)).status_code == 404
            current_url = f"/extension-assets/{PACKAGE}/{current_generation}/avatar.svg"
            assert (await client.get(current_url)).status_code == 200
            runtime.unpublish(PACKAGE)
            assert (await client.get(current_url)).status_code == 404
    finally:
        await runtime.shutdown()


async def test_registration_errors_do_not_sign_in_or_queue_storage(tmp_path):
    runtime, channel, ws = await _installed(tmp_path)
    try:
        await runtime.intercept(ws, "config", "config.get", {})
        token = await _register(runtime, channel, ws, "alpha")
        await runtime.intercept(ws, "signed-in", "config.get", {}, request_context={
            "headers": {"Authorization": "Bearer " + token},
        })
        await _click(runtime, ws, "user.menu", {"key": "logout"}, token)
        await runtime.intercept(ws, "after-reload", "config.get", {})
        await _click(runtime, ws, "login.mode")
        operations = [item for item in channel.events if item["event"] == "client.operation"]
        for data, status in [
            ({"username": "bad", "password": "Example123!"}, "invalid_fields"),
            ({"username": "alpha", "password": "Different123!"}, "user_exists"),
        ]:
            await _click(runtime, ws, "login.register", data)
            assert channel.responses[-1]["payload"]["status"] == status
            assert [item for item in channel.events if item["event"] == "client.operation"] == operations
        visited, _ = await runtime.intercept(ws, "no-session", "config.get", {})
        assert visited and channel.responses[-1]["code"] == "AUTH_REQUIRED"
    finally:
        await runtime.shutdown()


async def test_installed_example_restarts_without_deployment_grants(tmp_path):
    runtime, channel, ws = await _installed(tmp_path)
    config = runtime.config
    try:
        await runtime.intercept(ws, "config", "config.get", {})
        token = await _register(runtime, channel, ws, "alpha")
    finally:
        await runtime.shutdown()
    channel = BrowserChannel()
    runtime = GatewayRuntime(channel=channel, config=config)
    await runtime.start()
    try:
        assert [package.package_id for package in runtime.active_packages()] == [PACKAGE]
        ws = SimpleNamespace(closed=False)
        visited, params = await runtime.intercept(ws, "restored", "chat.send", {"message": "hello"}, request_context={
            "headers": {"Authorization": "Bearer " + token},
        })
        assert not visited and params == {"message": "hello"}
        await _click(runtime, ws, "user.menu", {"key": "logout"}, token)
        assert channel.responses[-1]["payload"]["status"] == "logged_out"
        operations = [item["payload"]["method"] for item in channel.events if item["event"] == "client.operation"]
        assert operations == ["local_storage.delete", "page.reload"]
    finally:
        await runtime.shutdown()


async def test_reload_does_not_reuse_revisions_for_old_clicks(tmp_path):
    runtime, channel, ws = await _installed(tmp_path)
    try:
        await runtime.intercept(ws, "first", "config.get", {})
        stale = _action(runtime, "login.mode")
        await runtime.packages.reload(PACKAGE)
        await runtime.intercept(ws, "second", "config.get", {})
        assert _action(runtime, "login.mode")["payload"]["revision"] > stale["payload"]["revision"]
        await runtime.intercept(ws, "stale", "webui.dispatch", stale)
        assert channel.responses[-1]["payload"]["status"] == "inactive"
    finally:
        await runtime.shutdown()


async def test_delayed_operation_cannot_cross_package_reload(tmp_path):
    runtime, channel, ws = await _installed(tmp_path)
    reached = asyncio.Event()
    release = asyncio.Event()
    package = runtime.active_packages()[0]
    original = next(item for item in package.handlers if item.event_type == "gateway.request")
    async def delayed(event, ops, next_handler):
        if event.event_input.method != "delayed":
            return await original.handler(event, ops, next_handler)
        reached.set()
        await release.wait()
        await ops.client.local_storage_set("late", {"value": "old activation"})
        return GatewayRequestOutput(handled=True, route="consume", response={})
    handlers = tuple(replace(item, handler=delayed) if item is original else item for item in package.handlers)
    runtime.publish(replace(package, handlers=handlers))
    pending = asyncio.create_task(runtime.intercept(ws, "delayed", "delayed", {}))
    try:
        await asyncio.wait_for(reached.wait(), timeout=2)
        await runtime.packages.reload(PACKAGE)
        release.set()
        await pending
        assert not any(item["event"] == "client.operation" for item in channel.events)
    finally:
        release.set()
        await pending
        await runtime.shutdown()


async def test_delayed_client_render_cannot_overwrite_the_reloaded_package_tree(tmp_path):
    runtime, channel, ws = await _installed(tmp_path)
    reached = asyncio.Event()
    release = asyncio.Event()
    outputs = []
    package = runtime.active_packages()[0]
    original = next(item for item in package.handlers if item.event_type == "gateway.request")

    async def delayed(event, ops, next_handler):
        if event.event_input.method != "delayed-render":
            return await original.handler(event, ops, next_handler)
        reached.set()
        await release.wait()
        outputs.append(await ops.ui.render(UiRenderInput(
            target=RenderTarget("", "application.entry", instance_key="login"),
            root=RenderNode("Text", {"value": "inactive activation"}),
        )))
        return GatewayRequestOutput(handled=True, route="consume", response={})

    handlers = tuple(replace(item, handler=delayed) if item is original else item for item in package.handlers)
    runtime.publish(replace(package, handlers=handlers))
    pending = asyncio.create_task(runtime.intercept(ws, "delayed", "delayed-render", {}))
    try:
        await asyncio.wait_for(reached.wait(), timeout=2)
        await runtime.packages.reload(PACKAGE)
        await runtime.intercept(ws, "current", "config.get", {})
        current = runtime.registry.snapshot()[0]
        before = node_to_payload(current.root)
        frames = len(channel.events)
        release.set()
        await pending
        assert outputs[0].status == "rejected"
        assert len(channel.events) == frames
        assert node_to_payload(runtime.registry.get(current.target).root) == before
    finally:
        release.set()
        await pending
        await runtime.shutdown()
