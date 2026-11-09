# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Load both resource routes from opaque-origin iframes in headless Chromium.

Install Playwright Chromium, or set CHROMIUM_EXECUTABLE_PATH explicitly. HTTP
routes and file readers are real; the E2A transport is replaced by the adapter.
"""

from __future__ import annotations

import asyncio
import os
import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from openjiuwen.core.events import PackageMetadata, SessionContext
from playwright.async_api import async_playwright

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.extensions_v2 import assembly
from jiuwenswarm.extensions_v2.webui.adapter import WebUIDispatchAdapter
from jiuwenswarm.extensions_v2.webui.contributions import WebUICatalogService
from jiuwenswarm.extensions_v2.webui.resources import WebUIResourceService
from jiuwenswarm.gateway.channel_manager.web import webui_resource_http
from jiuwenswarm.gateway.extensions_v2.models import GatewayRequestOutput
from jiuwenswarm.gateway.extensions_v2.webui.bridge import BrowserBridge
from jiuwenswarm.gateway.extensions_v2.webui.http import attach_browser_routes

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PAGE = """<!doctype html><body><script>
for (const [name, read] of Object.entries({
  parent: () => parent.document.body,
  cookie: () => document.cookie,
  storage: () => localStorage.length,
})) {
  try { read(); document.body.dataset[name] = 'accessible'; }
  catch (error) { document.body.dataset[name] = error.name; }
}
fetch('./data.json').then(response => response.json())
  .then(data => document.body.dataset.data = data.value)
  .catch(error => document.body.dataset.error = String(error));
</script><script type="module" src="./module.js"></script></body>"""
FILES = {
    "page.html": PAGE,
    "data.json": '{"value":"loaded"}',
    "module.js": "import {value} from './value.js'; document.body.dataset.module = value;",
    "value.js": "export const value = 'loaded';",
}


async def test_sandboxed_resource_modules_fetch_and_isolation(tmp_path, monkeypatch):
    record = SimpleNamespace(package_id="test.files", version="1.0.0", root=tmp_path, generation=1)
    resources = WebUIResourceService(WebUICatalogService(lambda: (record,)))
    ops = SimpleNamespace(package_metadata=PackageMetadata(record.package_id, record.version, tmp_path),
                          session_context=SessionContext("s1", metadata={"user_id": "u1"}))
    directory = await resources.create_directory(ops)
    async with directory:
        for path, content in FILES.items():
            (directory.path / path).write_text(content, encoding="utf-8")
    agent_url = await resources.asset_url(ops, "page.html", resource_id=directory.resource_id)
    assets = tmp_path / "assets"
    assets.mkdir()
    for path, content in FILES.items():
        (assets / path).write_text(content, encoding="utf-8")

    monkeypatch.setattr(assembly, "get_webui_runtime", lambda: SimpleNamespace(resources=resources))
    adapter = WebUIDispatchAdapter()

    async def fetch(**kwargs):
        response = await adapter.handle(AgentRequest(
            "read", channel_id=kwargs["channel_id"], req_method=kwargs["req_method"], params=kwargs["params"],
            user_id=kwargs["user_id"], session_id=kwargs["session_id"],
        ))
        return response.ok, response.payload

    monkeypatch.setattr(webui_resource_http, "fetch_agent_unary", fetch)
    package = SimpleNamespace(package_id="test.gateway-files", root=tmp_path, client={"requestBindings": []})
    # The published activation is the one record of what runs and its generation (Gateway S_01 invariant 13).
    activation = SimpleNamespace(package_id=package.package_id, package=package, generation="generation-one",
                                 application=True)
    runtime = SimpleNamespace(
        config=SimpleNamespace(install_dir=tmp_path, data_dir=tmp_path / "gateway-data"),
        active_packages=lambda: (package,), current_activations=lambda: (activation,),
        current_activation=lambda package_id: activation if package_id == package.package_id else None,
        bus=SimpleNamespace(trigger=AsyncMock(return_value=GatewayRequestOutput(route="forward"))),
    )
    runtime.browser = BrowserBridge(runtime)
    gateway_url = f"/extension-assets/{package.package_id}/generation-one/page.html"
    ready = asyncio.Event()

    @asynccontextmanager
    async def lifespan(app):
        ready.set()
        yield

    app = FastAPI(lifespan=lifespan)
    webui_resource_http.attach_webui_resource_routes(app, SimpleNamespace(agent_client=object()))
    attach_browser_routes(app, lambda: runtime)

    @app.get("/")
    async def index():
        sandbox = "allow-scripts allow-forms allow-popups allow-modals allow-downloads"
        return HTMLResponse("".join(f'<iframe sandbox="{sandbox}" src="{url}"></iframe>'
                                    for url in (agent_url, gateway_url)))

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await asyncio.wait_for(ready.wait(), 5)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True, executable_path=os.environ.get("CHROMIUM_EXECUTABLE_PATH"),
            )
            try:
                page = await browser.new_page()
                await page.goto(origin)
                assert len(page.frames) == 3
                for frame in page.frames[1:]:
                    await frame.wait_for_function(
                        "document.body.dataset.module === 'loaded' && document.body.dataset.data === 'loaded'",
                        timeout=10000,
                    )
                    assert await frame.evaluate("({...document.body.dataset})") == {
                        "module": "loaded", "data": "loaded", "parent": "SecurityError",
                        "cookie": "SecurityError", "storage": "SecurityError",
                    }
                await resources.release(ops, directory.resource_id)
                activation.generation = "generation-two"  # a reload: the old address is no longer served
                for url in (agent_url, gateway_url):
                    response = await page.request.get(origin + url)
                    assert response.status == 404
                    assert response.headers["access-control-allow-origin"] == "*"
            finally:
                await browser.close()
    finally:
        server.should_exit = True
        await serving
        sock.close()
        await resources.shutdown()
