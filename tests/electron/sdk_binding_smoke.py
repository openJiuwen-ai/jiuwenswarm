# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Live SDK registry check, driven by the isolated Electron integration fixture."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request
from dataclasses import dataclass

from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.core.foundation.tool.mcp.base import mcp_model_tool_name
from openjiuwen.core.runner import Runner
from openjiuwen.harness.tools.browser_move.playwright_runtime.config import BrowserRunGuardrails
from openjiuwen.harness.tools.browser_move.playwright_runtime.runtime import BrowserAgentRuntime

from jiuwenswarm.agents.harness.common.electron_sideview import apply_session_sideview_target
from tests.electron.sdk_managed_smoke import verify_managed_fallback


@dataclass(frozen=True)
class Settings:
    mcp_cfg: McpServerConfig


async def main():
    request = json.load(sys.stdin)
    os.environ["BROWSER_DRIVER"] = "remote"
    os.environ["PLAYWRIGHT_MCP_TARGET_RESOLVER"] = request["env"]["PLAYWRIGHT_MCP_TARGET_RESOLVER"]
    os.environ["JIUWENSWARM_ELECTRON"] = "1"
    original = Settings(McpServerConfig(
        server_id="unbound", server_name="unbound", server_path="stdio://browser",
        client_type="stdio", params={"command": request["node"], "args": [request["wrapper"]],
                                     "env": request["env"], "cwd": request["cwd"]},
    ))
    identities = [
        ("sdk-single-one", "", "single-one"), ("sdk-single-two", "", "single-two"),
        ("sdk-swarm", "alice", "alice"), ("sdk-swarm", "bob", "bob"),
    ]
    configs = [apply_session_sideview_target(original, sid, member_id=member).mcp_cfg
               for sid, member, _ in identities]
    assert len({config.server_id for config in configs}) == len(identities)
    for config in configs:
        assert len(mcp_model_tool_name(config.server_name, "browser_run_code_unsafe")) <= 64
    runtimes = [BrowserAgentRuntime(
        provider="OpenAI", api_key="synthetic-not-used", api_base="https://example.invalid/v1",
        model_name="synthetic-not-used", mcp_cfg=config, guardrails=BrowserRunGuardrails(),
    ) for config in configs]
    assert len({runtime.service.lifecycle_identity for runtime in runtimes}) == len(identities)

    async def invoke(config, name, inputs):
        tool = await Runner.resource_mgr.get_mcp_tool(name=name, server_id=config.server_id)
        tool = tool[0] if isinstance(tool, list) else tool
        assert tool is not None
        result = await tool.invoke(inputs)
        assert getattr(result, "success", True), result
        return result

    try:
        await Runner.start()
        await asyncio.gather(*(runtime.ensure_runtime_ready() for runtime in runtimes))
        await asyncio.gather(*(invoke(config, "browser_navigate", {"url": f'{request["base"]}/sdk-{label}'})
                               for config, (_, _, label) in zip(configs, identities)))
        viewports = await asyncio.gather(*(invoke(config, "browser_run_code_unsafe", {"code": (
            "async page => ({viewport:await page.evaluate(() => ({url:location.href, width:innerWidth, height:innerHeight,"
            " visualWidth:visualViewport.width, visualHeight:visualViewport.height})),"
            " button:await page.getByRole('button', {name:'Submit'}).boundingBox()})"
        )}) for config in configs))
        print("SDK_VIEWPORTS " + json.dumps([str(result) for result in viewports]), flush=True)
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            request["base"] + "/browser-diagnostics", timeout=10,
        ) as response:
            print("SDK_NATIVE_VIEWS " + response.read().decode("utf-8"), flush=True)
        await asyncio.gather(*(invoke(config, "browser_run_code_unsafe", {"code": (
            "async page => { await page.getByRole('textbox', {name:'Query'}).fill("
            + json.dumps(label)
            + "); await page.getByRole('button', {name:'Submit'}).click({timeout:5000}); return await page.locator('h1').innerText(); }"
        )}) for config, (_, _, label) in zip(configs, identities)))
        for config, (_, _, label) in zip(configs, identities):
            result = await invoke(config, "browser_run_code_unsafe", {"code": (
                "async page => ({url:page.url(), text:await page.locator('h1').innerText()})"
            )})
            assert f"/sdk-{label}" in str(result), result
            assert sum(other_label in str(result) for _, _, other_label in identities) == 1, result
        states = await asyncio.gather(*(runtime.capture_browser_state() for runtime in runtimes))
        for state, (_, _, label) in zip(states, identities):
            assert state["ok"], state
            assert state["url"] == f'{request["base"]}/sdk-{label}', state
        await runtimes[0].shutdown()
        state = await runtimes[1].capture_browser_state()
        assert state["ok"] and state["url"] == f'{request["base"]}/sdk-single-two', state
        print("SDK_SINGLE_SESSION_ISOLATION_OK")
        await verify_managed_fallback(request, original, invoke, runtimes[1])
        print("SDK_BINDINGS_OK")
    finally:
        for runtime in runtimes:
            await runtime.shutdown()
        await Runner.stop()


if __name__ == "__main__":
    asyncio.run(main())
