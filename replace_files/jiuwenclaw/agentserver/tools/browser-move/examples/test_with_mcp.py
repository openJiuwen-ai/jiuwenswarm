#!/usr/bin/env python
# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""
Test script for Main Agent + MCP tools and browser sub-agent.

How to run the MCP servers (each in its own terminal; SSE = default, ports match below):

  # Browser (required for browser_cdp_sub)
  uv run -m src.super_agent.tool.mcp_servers.browser_use_mcp_server_cdp --host 127.0.0.1 --port 8930

  # Reasoning (port 8934)
  uv run -m src.super_agent.tool.mcp_servers.reasoning_mcp_server --transport sse --host 127.0.0.1 --port 8934

  # Searching (port 8936)
  uv run -m src.super_agent.tool.mcp_servers.searching_mcp_server --transport sse --host 127.0.0.1 --port 8936

  # Vision (port 8932)
  uv run -m src.super_agent.tool.mcp_servers.vision_mcp_server --transport sse --host 127.0.0.1 --port 8932

Then run this test: uv run -m src.super_agent.test.test_with_mcp
"""
from __future__ import annotations

import sys
import os
import time
import json
import asyncio
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv

# =============================================================================
# Force using repo openjiuwen (same as your current test.py)
# =============================================================================
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

# Load environment variables from .env (so you don't have to export them manually)
load_dotenv()

# -----------------------------
# openjiuwen imports
# -----------------------------
from openjiuwen.core.runner import Runner
from openjiuwen.core.foundation.tool import Tool, ToolCard, McpServerConfig
from openjiuwen.core.single_agent.agents.react_agent import ReActAgent, ReActAgentConfig
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.core.session.session import Session, BaseSession


# =============================================================================
# Minimal Session (same idea as your original file)
# =============================================================================
class _DummyBaseSession(BaseSession):
    def __init__(self, sid: str):
        self._sid = sid

    def config(self): return None
    def state(self): return None
    def tracer(self) -> Any: return None
    def stream_writer_manager(self): return None
    def callback_manager(self): return None
    def session_id(self) -> str: return self._sid
    def checkpointer(self): return None


class DemoSession(Session):
    def __init__(self, sid: Optional[str] = None):
        self._sid = sid or f"demo-{int(time.time() * 1000)}"
        self._exec_id = f"exec-{self._sid}"
        self._state: Dict[str, Any] = {}
        self._global_state: Dict[str, Any] = {}
        self._base = _DummyBaseSession(self._sid)

    def base(self) -> BaseSession: return self._base
    def session_id(self) -> str: return self._sid
    def exec_id(self) -> str: return self._exec_id

    def get(self, key: str, default=None): return self._state.get(key, default)
    def set(self, key: str, value: Any) -> None: self._state[key] = value
    def get_global(self, key: str, default=None): return self._global_state.get(key, default)
    def set_global(self, key: str, value: Any) -> None: self._global_state[key] = value


# =============================================================================
# A simple local Tool
# =============================================================================
class AddLocalTool(Tool):
    def __init__(self):
        super().__init__(
            card=ToolCard(
                id="tool.add_local",
                name="add_local",
                description="(Local) Add two numbers and return the sum.",
                input_params={
                    "type": "object",
                    "properties": {
                        "a": {"type": "number", "description": "first number"},
                        "b": {"type": "number", "description": "second number"},
                    },
                    "required": ["a", "b"],
                },
            )
        )

    async def invoke(self, a: float, b: float, **kwargs) -> str:
        return str(a + b)


# =============================================================================
# MCP Server Registry - Configure YOUR MCP servers here
# =============================================================================
# Only this server is exposed as a sub-agent; all other enabled servers are main-agent tools.
BROWSER_MCP_KEY = "browser_cdp"

# Add/remove servers based on what you have running
# Format: (server_name, default_port, description, system_prompt_snippet)
AVAILABLE_MCP_SERVERS = {
    "browser_cdp": {
        "server_name": "browser-use-cdp-server",
        "port": 8930,
        "description": "Browser automation using CDP. Input: {'query': string with browser task}.",
        "prompt": (
            "You are a browser automation sub-agent.\n"
            "You have access to browser MCP tools for web interaction.\n"
            "When given a browser task:\n"
            "1. Use the available MCP tools to complete it\n"
            "2. Return clear, concise results about what you observed\n"
        ),
        "enabled": True,  # Set to False to disable
    },
    "vision": {
        "server_name": "vision-mcp-server",
        "port": 8932,
        "description": "Vision/VQA for image analysis. Input: {'query': string with image path/url and question}.",
        "prompt": (
            "You are a vision analysis sub-agent.\n"
            "You can analyze images and answer questions about visual content.\n"
            "Provide detailed descriptions and insights.\n"
        ),
        "enabled": False,  # Enable if server is running
    },
    "reasoning": {
        "server_name": "reasoning-mcp-server",
        "port": 8934,
        "description": "Complex reasoning tasks. Input: {'query': string with problem}.",
        "prompt": (
            "You are a reasoning sub-agent.\n"
            "You can perform complex multi-step reasoning and problem solving.\n"
            "Break down problems and solve them systematically.\n"
        ),
        "enabled": True,
    },
    "searching": {
        "server_name": "searching-mcp-server",
        "port": 8936,
        "description": "Web search. Input: {'query': string with search query}.",
        "prompt": (
            "You are a web search sub-agent.\n"
            "You can search the web and retrieve relevant information.\n"
            "Provide comprehensive and accurate search results.\n"
        ),
        "enabled": True,
    },
}


# =============================================================================
# Sub-agent builder for MCP servers
# =============================================================================
def build_mcp_sub_agent(
    agent_id: str,
    agent_name: str,
    description: str,
    system_prompt: str,
    provider: str,
    api_key: str,
    api_base: str,
    model_name: str,
    mcp_cfg: McpServerConfig
) -> ReActAgent:
    """
    Build a sub-agent that uses MCP tools from a specific server.
    """
    card = AgentCard(
        id=agent_id,
        name=agent_name,
        description=description,
        input_params={},
    )

    return ReActAgent(card=card).configure(
        ReActAgentConfig()
        .configure_model_client(
            provider=provider,
            api_key=api_key,
            api_base=api_base,
            model_name=model_name,
        )
        .configure_max_iterations(8)
        .configure_prompt_template(
            [
                {
                    "role": "system",
                    "content": system_prompt,
                }
            ]
        )
    )


# =============================================================================
# MCP config helper (supports different McpServerConfig field names)
# =============================================================================
def make_mcp_config(
    server_id: str,
    server_name: str,
    host: str,
    port: int,
    transport: str = "sse",
) -> McpServerConfig:
    fields = getattr(McpServerConfig, "model_fields", None) or getattr(McpServerConfig, "__fields__", {})
    def has(k: str) -> bool:
        try:
            return k in fields
        except Exception:
            return False

    base_http = f"http://{host}:{port}"
    sse_url = f"{base_http}/sse"

    payload: Dict[str, Any] = {}
    if has("server_id"):
        payload["server_id"] = server_id
    elif has("id"):
        payload["id"] = server_id

    if has("name"):
        payload["name"] = server_name

    if has("transport"):
        payload["transport"] = transport

    # required in your env: server_path
    if has("server_path"):
        payload["server_path"] = sse_url

    # compat fields (if exist)
    if has("url"):
        payload["url"] = sse_url
    if has("base_url"):
        payload["base_url"] = base_http
    if has("host"):
        payload["host"] = host
    if has("port"):
        payload["port"] = port
    if has("server_name") and "server_name" not in payload:
        payload["server_name"] = server_name
    if has("client_type"):
        payload["client_type"] = transport  # "sse" for SSE endpoints

    # filter unknown fields for pydantic v1/v2
    try:
        payload = {k: v for k, v in payload.items() if has(k)}
    except Exception:
        pass

    return McpServerConfig(**payload)


async def register_mcp_server_to_resource_mgr(mcp_cfg: McpServerConfig, tag: str):
    """Register MCP server to ResourceManager.

    Returns the Result from add_mcp_server. Caller should check .is_ok() and only
    add to ability_manager when True (so failed servers are not offered as tools).
    """
    rm = Runner.resource_mgr
    if not hasattr(rm, "add_mcp_server"):
        raise RuntimeError("Runner.resource_mgr has no method add_mcp_server() in this version.")
    return await rm.add_mcp_server(mcp_cfg, tag=tag)


# =============================================================================
# Main Agent: registers local tool + MCP sub-agents
# =============================================================================
async def build_main_agent_with_mcp_subs(
    provider: str,
    api_key: str,
    api_base: str,
    model_name: str,
    mcp_host: str = "127.0.0.1",
) -> ReActAgent:
    """Build main agent with direct MCP tools (non-browser) and one browser sub-agent."""
    
    # Direct MCP tools on main agent (all enabled except browser)
    main_agent_mcp = [
        f"- {key}: {info['description']}"
        for key, info in AVAILABLE_MCP_SERVERS.items()
        if info["enabled"] and key != BROWSER_MCP_KEY
    ]
    main_mcp_desc = "\n".join(main_agent_mcp) if main_agent_mcp else "  (none enabled)"
    # Sub-agent: only browser
    sub_agent_desc = (
        "- browser_cdp_sub: Browser automation. For browser tasks, call this sub-agent."
        if AVAILABLE_MCP_SERVERS.get(BROWSER_MCP_KEY, {}).get("enabled")
        else "  (none)"
    )

    main_card = AgentCard(
        id="agent.main",
        name="main_agent",
        description="Main agent with local tools, MCP tools, and browser sub-agent.",
        input_params={},
    )

    agent = ReActAgent(card=main_card).configure(
        ReActAgentConfig()
        .configure_model_client(
            provider=provider,
            api_key=api_key,
            api_base=api_base,
            model_name=model_name,
        )
        .configure_max_iterations(10)
        .configure_prompt_template(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a main orchestrator agent with multiple capabilities:\n\n"
                        "Local tools:\n"
                        "- add_local: Add two numbers\n\n"
                        "MCP tools (use these directly):\n"
                        f"{main_mcp_desc}\n\n"
                        "Sub-agent (delegate browser tasks only):\n"
                        f"{sub_agent_desc}\n\n"
                        "For browser tasks, call the browser_cdp_sub sub-agent. For other tasks, use the MCP tools or add_local.\n"
                        "Then provide a final natural language answer to the user.\n"
                    ),
                }
            ]
        )
    )

    # Register local tool
    add_local = AddLocalTool()
    Runner.resource_mgr.add_tool(add_local, tag=agent.card.id)
    agent.ability_manager.add(add_local.card)

    # Register enabled MCP servers: browser as sub-agent, rest as main-agent tools
    for key, config in AVAILABLE_MCP_SERVERS.items():
        if not config["enabled"]:
            continue

        mcp_cfg = make_mcp_config(
            server_id=key,
            server_name=config["server_name"],
            host=mcp_host,
            port=config["port"],
        )

        if key == BROWSER_MCP_KEY:
            # Browser: sub-agent only
            print(f"  Registering sub-agent {key}_sub at port {config['port']} (browser)...")
            await register_mcp_server_to_resource_mgr(mcp_cfg, tag=f"agent.{key}_sub")
            sub_agent = build_mcp_sub_agent(
                agent_id=f"agent.{key}_sub",
                agent_name=f"{key}_sub",
                description=config["description"],
                system_prompt=config["prompt"],
                provider=provider,
                api_key=api_key,
                api_base=api_base,
                model_name=model_name,
                mcp_cfg=mcp_cfg,
            )
            sub_agent.ability_manager.add(mcp_cfg)
            Runner.resource_mgr.add_agent(sub_agent.card, lambda a=sub_agent: a, tag=agent.card.id)
            agent.ability_manager.add(sub_agent.card)
        else:
            # Non-browser: main agent MCP tools (only add to ability_manager if registration succeeded)
            print(f"  Registering MCP tools for main agent: {key} at port {config['port']}...")
            result = await register_mcp_server_to_resource_mgr(mcp_cfg, tag=agent.card.id)
            if result is not None and getattr(result, "is_ok", lambda: False)():
                agent.ability_manager.add(mcp_cfg)
            else:
                err = getattr(result, "value", result) if result is not None else "connection failed"
                print(f"  [WARN] Skipping {key} (MCP server not available): {err}")

    return agent


# =============================================================================
# Run (assumes MCP servers are started manually)
# =============================================================================
async def main():
    # Start the runner first (required for MCP resource manager)
    await Runner.start()
    
    # Get credentials from environment (OpenRouter or OpenAI)
    # When MODEL_PROVIDER=openrouter, use OpenRouter key/base; otherwise OpenAI.
    model_provider = (os.getenv("MODEL_PROVIDER") or "OpenAI").strip().lower()
    if model_provider == "openrouter":
        api_key = (os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
        api_base = (os.getenv("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1").strip()
        model_name = (os.getenv("MODEL_NAME") or "anthropic/claude-sonnet-4").strip()
        # openjiuwen Model only accepts OpenAI, SiliconFlow, DashScope; OpenRouter is OpenAI-compatible
        provider = "OpenAI"
    else:
        api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
        api_base = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip()
        model_name = (os.getenv("MODEL_NAME") or "gpt-4o-mini").strip()
        provider = "OpenAI"

    # MCP server host
    mcp_host = os.getenv("MCP_SERVER_HOST", "127.0.0.1")

    print(f"\n{'='*70}")
    print(f"Testing Main Agent + MCP Sub-Agents")
    print(f"{'='*70}")
    print(f"Backend: {model_provider} (client provider: {provider})")
    print(f"Model: {model_name}")
    print(f"MCP Host: {mcp_host}")
    if not api_key:
        raise RuntimeError(
            "No API key found. Set OPENROUTER_API_KEY (for OpenRouter) or OPENAI_API_KEY (for OpenAI) in .env"
        )
    print(f"\nEnabled MCP Servers:")
    for key, info in AVAILABLE_MCP_SERVERS.items():
        if not info["enabled"]:
            print(f"  ⏭️  {key:15} @ port {info['port']} - disabled")
        elif key == BROWSER_MCP_KEY:
            print(f"  ✅  {key:15} @ port {info['port']} - sub-agent (browser)")
        else:
            print(f"  ✅  {key:15} @ port {info['port']} - main agent tools")
    print(f"{'='*70}\n")

    # Build main agent (direct MCP tools + browser sub-agent)
    print("Building main agent (MCP tools + browser sub-agent)...")
    agent = await build_main_agent_with_mcp_subs(
        provider=provider,
        api_key=api_key,
        api_base=api_base,
        model_name=model_name,
        mcp_host=mcp_host,
    )
    
    print("\nDiscovering available tools and sub-agents...")
    tools = await agent.ability_manager.list_tool_info()
    print(f"✅ Main agent has {len(tools)} capabilities\n")
    
    # Show available capabilities
    print("Main agent capabilities:")
    for tool in tools:
        tool_name = getattr(tool, 'name', 'unknown')
        tool_desc = getattr(tool, 'description', '')[:100]
        print(f"  - {tool_name}: {tool_desc}")
    print()

    # Default: main agent uses an MCP tool directly (reasoning or search), not a local tool.
    # Override with EXAMPLE_QUERY env var (e.g. for browser sub-agent: "Open https://example.com and tell me the title").
    query = """
    Use the reasoning tool to solve: if 3x + 5 = 14, what is x? Reply with the value of x.
    Use that value of x to open https://www.roastful.com/top-roasters#top-50 and find the position of the roaster on the list.
    Reply with the name of the roaster that sits in that position.
    """
    print(f"Test Query: {query}\n")
    print("Running main agent (will call MCP tools or delegate to sub-agent as needed)...\n")
    
    result = await agent.invoke({"query": query}, session=None)

    print("\n" + "="*70)
    print("FINAL RESULT")
    print("="*70)
    print(result)
    print("="*70)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError as e:
        # Swallow the specific MCP/anyio shutdown bug so tests can continue
        if "Attempted to exit cancel scope in a different task than it was entered in" in str(e):
            print(f"Ignoring MCP shutdown error: {e}", file=sys.stderr)
        else:
            # Re-raise anything else so real errors still fail
            raise
