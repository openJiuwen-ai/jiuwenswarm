# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Own mock identity and login UI entirely inside one extension activation."""

from __future__ import annotations

import json
import asyncio
import logging
from dataclasses import replace
from collections.abc import Mapping
from pathlib import Path

from jiuwenswarm.gateway.extensions_v2.models import GatewayRequestOutput
from jiuwenswarm.gateway.extensions_v2.webui.models import (
    RenderMode, RenderStatus, RenderTarget, UiActionOutput, UiRenderInput,
)
from jiuwenswarm.gateway.extensions_v2.webui.ops import with_client_ops

from .views import login_view, user_view, render_node
from .identity import MockIdentity

PACKAGE_ID = "webui.login-demo"
ASSETS = ("background.svg", "promotion.png", "avatar.svg")
logger = logging.getLogger("jiuwenswarm.gateway.examples.auth_login_demo")


class LoginExtension:
    """Manage persistent mock accounts and sessions without host auth rules."""

    def __init__(self) -> None:
        settings = json.loads((Path(__file__).parents[1] / "settings.json").read_text(encoding="utf-8"))
        self.language = settings["language"]
        self.layout = settings["layout"]
        if self.language not in {"zh", "en"} or self.layout not in {"split", "center"}:
            raise ValueError("unsupported example language or layout")
        self._identity = MockIdentity(Path(__file__).parents[1] / "data" / "accounts.sqlite3")
        self._views: dict[str, str] = {}
        self._modes: dict[str, str] = {}

    def register(self, registrar) -> None:
        registrar.register_event_handler("gateway.request", self.on_request)
        registrar.register_event_handler("gateway.disconnect", self.on_disconnect)
        for event in ("ui.ready", "ui.click", "ui.select"):
            registrar.register_event_handler(event, self.on_ui, {"target.package_id": PACKAGE_ID})

    async def on_disconnect(self, event, ops, next_handler):
        self._views.pop(event.event_input.connection_id, None)
        self._modes.pop(event.event_input.connection_id, None)
        return await next_handler(event)

    def _token(self, headers: Mapping) -> str:
        if not isinstance(headers, Mapping):
            return ""
        value = next((value for key, value in headers.items() if isinstance(key, str) and key.lower() == "authorization"), "")
        return value[7:] if isinstance(value, str) and value.startswith("Bearer ") else ""

    async def _render(self, ops, slot: str, instance: str, tree: dict | None) -> None:
        output = await ops.ui.render(UiRenderInput(
            target=RenderTarget(session_id="", slot_id=slot, instance_key=instance),
            root=render_node(tree) if tree is not None else None,
            mode=RenderMode.REPLACE if tree is not None else RenderMode.CLEAR,
        ))
        if output is None or output.status is not RenderStatus.DELIVERED:
            raise RuntimeError("example view was not delivered")

    async def _login(self, ops, *, message: str = "") -> None:
        connection = ops.session_context.metadata["connection_id"]
        assets = {name: await ops.ui.asset_url(name) for name in ASSETS}
        await self._render(ops, "application.entry", "login", login_view(
            self.language, self.layout, assets,
            mode=self._modes.get(connection, "login"), message=message,
        ))
        self._views[connection] = "login"

    async def _user(self, ops, username: str, *, refresh: bool = False) -> None:
        connection = ops.session_context.metadata["connection_id"]
        view = f"user:{username}"
        if self._views.get(connection) != view:
            await self._render(ops, "application.entry", "login", None)
        if refresh or self._views.get(connection) != view:
            await self._render(ops, "application.sidebar.footer", "user", user_view(
                self.language, await ops.ui.asset_url("avatar.svg"), username,
            ))
        self._views[connection] = view

    async def on_request(self, event, ops, next_handler):
        request = event.event_input
        if request.transport == "websocket" and request.method == "webui.dispatch":
            target = request.params.get("target")
            event_name = request.params.get("event")
            if (isinstance(target, Mapping) and target.get("scope") == "application"
                    and target.get("slot_id") == "application.entry"
                    and ((event_name == "ui.ready" and target.get("package_id", "") in {"", PACKAGE_ID})
                         or (event_name == "ui.click" and target.get("package_id") == PACKAGE_ID))):
                return replace(await next_handler(event), handled=True)
        if request.transport == "http" and request.path.startswith("/extension-assets/"):
            return replace(await next_handler(event), handled=True)
        if request.transport == "websocket" and request.method == "ping":
            return replace(await next_handler(event), handled=True)
        valid = await asyncio.to_thread(self._identity.verify, self._token(request.headers))
        logger.info(
            "[LoginExample] request checked transport=%s method=%s context_present=%s credential_present=%s accepted=%s",
            request.transport, request.method, bool(request.request_context),
            bool(self._token(request.headers)), bool(valid),
        )
        if request.transport == "http":
            if valid:
                return replace(await next_handler(event), handled=True)
        elif valid:
            await self._user(ops, valid)
            return replace(await next_handler(event), handled=True)
        elif self._views.get(request.connection_id) != "login":
            await self._login(ops)
        return GatewayRequestOutput(handled=True, route="deny", error={
            "code": "AUTH_REQUIRED", "message": "login required", "status": 401,
        })

    async def on_ui(self, event, ops, next_handler):
        request = event.event_input
        slot = request.target.slot_id
        if request.event_type == "ui.ready":
            if slot == "application.sidebar.footer":
                username = await asyncio.to_thread(self._identity.verify, self._token(request.request_context.get("headers", {})))
                if username:
                    await self._user(ops, username, refresh=True)
            return UiActionOutput(accepted=True, status="ready")
        action = request.payload.get("action_id")
        data = request.payload.get("data", {})
        if slot == "application.entry" and action == "login.mode":
            mode = request.payload["args"].get("mode")
            if mode not in {"login", "register"}:
                return UiActionOutput(accepted=False, status="invalid_action")
            self._modes[ops.session_context.metadata["connection_id"]] = mode
            await self._login(ops)
            return UiActionOutput(accepted=True, status="rendered")
        if slot == "application.entry" and action in {"login.register", "login.confirm"}:
            if action == "login.register":
                status, token = await asyncio.to_thread(self._identity.register, data.get("username"), data.get("password"))
                message = status
            else:
                token = await asyncio.to_thread(self._identity.login, data.get("username"), data.get("password"))
                status, message = "invalid_credentials", "invalid"
            if token is None:
                await self._login(ops, message=message)
                return UiActionOutput(accepted=True, status=status)
            client_ops = with_client_ops(ops)
            await client_ops.client.local_storage.set("login", {"accessToken": token, "username": data["username"]})
            self._modes[ops.session_context.metadata["connection_id"]] = "login"
            await client_ops.client.page.reload()
            return UiActionOutput(accepted=True, status="logged_in")
        if slot == "application.sidebar.footer" and action == "user.menu" and data.get("key") == "logout":
            token = self._token(request.request_context.get("headers", {}))
            if not await asyncio.to_thread(self._identity.logout, token):
                return UiActionOutput(accepted=False, status="invalid_credentials")
            self._modes[ops.session_context.metadata["connection_id"]] = "login"
            client_ops = with_client_ops(ops)
            await client_ops.client.local_storage.delete("login")
            await client_ops.client.page.reload()
            return UiActionOutput(accepted=True, status="logged_out")
        return await next_handler(event)


def create_extension() -> LoginExtension:
    """Create an independent mock identity state for one package activation."""
    return LoginExtension()
