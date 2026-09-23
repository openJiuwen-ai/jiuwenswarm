# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Optional real managed Chrome checks alongside the live Electron fixture."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from urllib.parse import urlparse

from openjiuwen.harness.tools.browser_move.playwright_runtime.config import BrowserRunGuardrails, RuntimeSettings
from openjiuwen.harness.tools.browser_move.playwright_runtime.runtime import BrowserAgentRuntime

from jiuwenswarm.agents.swarm.browser_runtime import apply_swarm_browser_settings
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


async def verify_managed_fallback(request, original, invoke, electron_runtime):
    """Use synthetic local pages and new profiles; never touch a personal profile."""
    chrome = request.get("chrome")
    if not chrome:
        return
    settings = RuntimeSettings(
        provider="OpenAI", api_key="synthetic-not-used", api_base="https://example.invalid/v1",
        model_name="synthetic-not-used", mcp_cfg=original.mcp_cfg, guardrails=BrowserRunGuardrails(),
    )
    adapter = JiuWenSwarmDeepAdapter()
    electron_command = os.environ.get("PLAYWRIGHT_MCP_COMMAND")
    for headless in (True, False):
        config = {"browser": {"chrome_path": chrome, "headless": headless}}
        adapter._sync_browser_runtime_environment(config, runtime_enabled=True)
        bound = [apply_swarm_browser_settings(settings, config, f"external-{headless}", member_id=member)
                 for member in ("alice", "bob")]
        runtimes = [BrowserAgentRuntime(
            provider=item.provider, api_key=item.api_key, api_base=item.api_base, model_name=item.model_name,
            mcp_cfg=item.mcp_cfg, guardrails=item.guardrails, instance=item.instance,
        ) for item in bound]
        endpoints = []
        try:
            await asyncio.gather(*(runtime.ensure_runtime_ready() for runtime in runtimes))
            endpoints = [runtime.service.mcp_cfg.params["env"]["PLAYWRIGHT_MCP_CDP_ENDPOINT"] for runtime in runtimes]
            assert len(set(endpoints)) == 2, endpoints
            assert request["env"]["PLAYWRIGHT_MCP_CDP_ENDPOINT"] not in endpoints
            await asyncio.gather(*(invoke(item.mcp_cfg, "browser_navigate", {
                "url": f'{request["base"]}/external-{headless}-{index}',
            }) for index, item in enumerate(bound)))
            for index, item in enumerate(bound):
                result = await invoke(item.mcp_cfg, "browser_run_code_unsafe", {"code": (
                    "async page => {await page.getByRole('textbox', {name:'Query'}).fill("
                    + json.dumps(f"external-{headless}-{index}")
                    + "); await page.getByRole('button', {name:'Submit'}).click();"
                    " return {text:await page.locator('h1').innerText(), ua:await page.evaluate(() => navigator.userAgent)};}"
                )})
                assert f"external-{headless}-{index}" in str(result), result
                assert ("HeadlessChrome" in str(result)) is headless, result
            states = await asyncio.gather(*(runtime.capture_browser_state() for runtime in runtimes))
            for index, state in enumerate(states):
                assert state["ok"] and state["url"] == f'{request["base"]}/external-{headless}-{index}', state
            await runtimes[0].shutdown()
            assert (await runtimes[1].capture_browser_state())["ok"]
            state = await electron_runtime.capture_browser_state()
            assert state["ok"] and state["url"] == f'{request["base"]}/sdk-single-two', state
            assert os.environ["BROWSER_DRIVER"] == "remote"
            assert os.environ.get("PLAYWRIGHT_MCP_COMMAND") == electron_command
        finally:
            for runtime in runtimes:
                await runtime.shutdown()
        for endpoint in endpoints:
            port = urlparse(endpoint).port
            with socket.socket() as sock:
                sock.settimeout(1)
                assert sock.connect_ex(("127.0.0.1", port)) != 0, f"Chrome still listening: {endpoint}"
        print(f"SDK_SWARM_MANAGED_{'HEADLESS' if headless else 'HEADED'}_OK", flush=True)
