#!/usr/bin/env python
# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""CLI entrypoint for Playwright browser runtime."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SRC_ROOT = _HERE.parent
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from jiuwenclaw.jiuwen_core_patch import apply_openai_model_client_patch
from playwright_runtime import REPO_ROOT  # noqa: F401

# Apply the same OpenAI client compatibility patch used by the main services
# so browser runtime can parse non-standard string responses as well.
apply_openai_model_client_patch()

# Inject browser-move client subclasses before openjiuwen's ToolMgr is used,
# so that our retry/timeout/reconnect logic is active without modifying openjiuwen.
import openjiuwen.core.runner.resources_manager.tool_manager as _tool_mgr_mod
from playwright_runtime.clients.stdio_client import BrowserMoveStdioClient
from playwright_runtime.clients.streamable_http_client import BrowserMoveStreamableHttpClient

_tool_mgr_mod.StdioClient = BrowserMoveStdioClient
_tool_mgr_mod.StreamableHttpClient = BrowserMoveStreamableHttpClient

from openjiuwen.core.runner import Runner
from playwright_runtime.config import (
    BrowserRunGuardrails,
    build_playwright_mcp_config,
    resolve_model_name,
    resolve_model_settings,
)
from playwright_runtime.runtime import BrowserAgentRuntime

GUARDRAIL_MAX_STEPS = 20
GUARDRAIL_MAX_FAILURES = 2
GUARDRAIL_RETRY_ONCE = True


async def main() -> None:
    provider, api_key, api_base = resolve_model_settings()
    model_name = resolve_model_name()
    if not api_key:
        raise RuntimeError("Missing API key. Set API_KEY (or OPENROUTER_API_KEY / OPENAI_API_KEY / DASHSCOPE_API_KEY).")

    guardrails = BrowserRunGuardrails(
        max_steps=GUARDRAIL_MAX_STEPS,
        max_failures=GUARDRAIL_MAX_FAILURES,
        timeout_s=int(os.getenv("BROWSER_TIMEOUT_S", "180")),
        retry_once=GUARDRAIL_RETRY_ONCE,
    )
    mcp_cfg = build_playwright_mcp_config()

    runtime = BrowserAgentRuntime(
        provider=provider,
        api_key=api_key,
        api_base=api_base,
        model_name=model_name,
        mcp_cfg=mcp_cfg,
        guardrails=guardrails,
    )

    initial_query = (os.getenv("AGENT_QUERY") or "").strip()
    session_id = (os.getenv("AGENT_SESSION_ID") or "").strip() or "demo-browser-session"

    try:
        await runtime.ensure_started()
        mcp_tools = await Runner.resource_mgr.get_mcp_tool_infos(server_id=mcp_cfg.server_id) or []

        print("=" * 72)
        print("Playwright Browser Runtime")
        print("=" * 72)
        print(f"Model provider: {provider}")
        print(f"Model: {model_name}")
        print(f"MCP command: {mcp_cfg.params.get('command')}")
        print(f"MCP args: {mcp_cfg.params.get('args')}")
        print(f"Discovered browser tools: {len(mcp_tools)}")
        for item in mcp_tools:
            print(f"  - {getattr(item, 'name', 'unknown')}")
        print("=" * 72)
        print(f"Session: {session_id}")
        print("Continuous mode: enter a task and press Enter.")
        print("Type 'exit' or 'quit' to stop.\n")

        query = initial_query
        while True:
            if not query:
                query = input("query> ").strip()
            if not query:
                continue
            if query.lower() in {"exit", "quit"}:
                break

            answer = await runtime.handle_request(query=query, session_id=session_id)
            print("Result:")
            print(json.dumps(answer, ensure_ascii=False, indent=2))
            print("")
            query = ""
    except Exception as exc:
        print("Runtime error:")
        print(str(exc))
        traceback.print_exc()
        raise
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    finally:
        await runtime.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
