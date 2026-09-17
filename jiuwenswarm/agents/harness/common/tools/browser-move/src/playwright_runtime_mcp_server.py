#!/usr/bin/env python
# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""MCP server wrapper for Playwright browser runtime.

Usage (from repo root):
  uv run python src/playwright_runtime_mcp_server.py
  uv run python src/playwright_runtime_mcp_server.py --transport sse --host 127.0.0.1 --port 8940
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

# Ensure repo and src packages are importable when running as a script.
_HERE = Path(__file__).resolve().parent
_SRC_ROOT = _HERE
_REPO_ROOT = _SRC_ROOT.parent
for _p in (str(_REPO_ROOT), str(_SRC_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

logger = logging.getLogger(__name__)

from fastmcp import Context, FastMCP
from playwright_runtime.openjiuwen_monkeypatch import apply_openjiuwen_monkeypatch

# Inject browser-move client subclasses before openjiuwen's ToolMgr is used,
# so retry/timeout/reconnect logic is active in MCP server mode as well.
apply_openjiuwen_monkeypatch()
import openjiuwen.core.runner.resources_manager.tool_manager as _tool_mgr_mod
from playwright_runtime.clients.stdio_client import BrowserMoveStdioClient
from playwright_runtime.clients.streamable_http_client import BrowserMoveStreamableHttpClient
from playwright_runtime.config import (
    BrowserRunGuardrails,
    build_playwright_mcp_config,
    resolve_model_settings,
)
from playwright_runtime.controllers.action import (
    bind_runtime,
    clear_runtime_runner,
    list_actions,
    register_example_actions,
    run_action,
)

from jiuwenswarm.agents.harness.common.tools.browser_timeout_policy import resolve_browser_task_timeout

if TYPE_CHECKING:
    from playwright_runtime.runtime import BrowserAgentRuntime

_tool_mgr_mod.StdioClient = BrowserMoveStdioClient
_tool_mgr_mod.StreamableHttpClient = BrowserMoveStreamableHttpClient

_runtime: Optional["BrowserAgentRuntime"] = None
_runtime_lock = asyncio.Lock()
GUARDRAIL_MAX_STEPS = 20
GUARDRAIL_MAX_FAILURES = 2
GUARDRAIL_RETRY_ONCE = True


def _build_runtime() -> "BrowserAgentRuntime":
    from playwright_runtime.runtime import BrowserAgentRuntime

    provider, api_key, api_base = resolve_model_settings()
    if not api_key:
        raise RuntimeError("Missing API key. Set OPENROUTER_API_KEY or OPENAI_API_KEY.")

    # Sync --headless flag in PLAYWRIGHT_MCP_ARGS from config.yaml.
    # @playwright/mcp@latest uses the --headless CLI flag, not an env var.
    try:
        from jiuwenswarm.common.config import get_config
        _cfg = get_config() or {}
        _browser_cfg = _cfg.get("browser", {}) if isinstance(_cfg, dict) else {}
        _headless_val = _browser_cfg.get("headless", True) if isinstance(_browser_cfg, dict) else True
        _headless = bool(_headless_val) if isinstance(_headless_val, bool) else True
        _mcp_args_raw = (os.getenv("PLAYWRIGHT_MCP_ARGS") or "-y @playwright/mcp@latest").strip()
        _mcp_args_list = _mcp_args_raw.split() if _mcp_args_raw else ["-y", "@playwright/mcp@latest"]
        _mcp_args_list = [a for a in _mcp_args_list if a != "--headless"]
        if _headless:
            _mcp_args_list.append("--headless")
        os.environ["PLAYWRIGHT_MCP_ARGS"] = " ".join(_mcp_args_list)
    except Exception:
        logger.warning("Failed to sync headless flag from config; using PLAYWRIGHT_MCP_ARGS as-is", exc_info=True)

    model_name = (os.getenv("MODEL_NAME") or "anthropic/claude-sonnet-4.5").strip()
    guardrails = BrowserRunGuardrails(
        max_steps=GUARDRAIL_MAX_STEPS,
        max_failures=GUARDRAIL_MAX_FAILURES,
        timeout_s=int(os.getenv("BROWSER_TIMEOUT_S", "180")),
        retry_once=GUARDRAIL_RETRY_ONCE,
    )
    return BrowserAgentRuntime(
        provider=provider,
        api_key=api_key,
        api_base=api_base,
        model_name=model_name,
        mcp_cfg=build_playwright_mcp_config(),
        guardrails=guardrails,
    )


async def _get_runtime() -> BrowserAgentRuntime:
    global _runtime
    if _runtime is not None:
        return _runtime

    async with _runtime_lock:
        if _runtime is None:
            _runtime = _build_runtime()
            await _runtime.ensure_started()
            bind_runtime(_runtime)
        return _runtime


async def _shutdown_runtime() -> None:
    global _runtime
    async with _runtime_lock:
        if _runtime is not None:
            await _runtime.shutdown()
            _runtime = None
        clear_runtime_runner()


def _resolve_session_id(explicit_session_id: str, ctx: Context | None = None) -> str:
    """Resolve logical browser session id, preferring explicit value over MCP context."""
    explicit = (explicit_session_id or "").strip()
    if explicit:
        return explicit
    if ctx is None:
        return ""
    try:
        return (ctx.session_id or "").strip()
    except Exception:
        return ""


def _resolve_request_id(explicit_request_id: str, ctx: Context | None = None) -> str:
    """Resolve request id, preferring explicit value over MCP context."""
    explicit = (explicit_request_id or "").strip()
    if explicit:
        return explicit
    if ctx is None:
        return ""
    try:
        return (ctx.request_id or "").strip()
    except Exception:
        return ""


@asynccontextmanager
async def _lifespan(_server: FastMCP):
    try:
        register_example_actions()
        yield {}
    finally:
        await _shutdown_runtime()


mcp = FastMCP(
    "playwright-runtime-mcp",
    instructions=(
        "Browser automation MCP server. "
        "Use browser_run_task for web tasks. "
        "Pass a stable session_id to reuse browser session state."
    ),
    lifespan=_lifespan,
)


@mcp.tool(
    name="browser_run_task",
    description=(
        "Execute a browser task using Playwright runtime. "
        "Prefer one comprehensive task per request instead of many tiny retries. "
        "Use a long timeout and do not pass timeout_s below the configured default; "
        "omit timeout_s to use the default long timeout. "
        "Returns JSON with ok/session_id/request_id/final/page/screenshot/error/attempt."
    ),
)
async def browser_run_task(
    task: str,
    session_id: str = "",
    request_id: str = "",
    timeout_s: int = 0,
    ctx: Context | None = None,
) -> dict[str, Any]:
    runtime = await _get_runtime()
    effective_timeout = None
    if timeout_s > 0:
        effective_timeout = resolve_browser_task_timeout(timeout_s, runtime._service.guardrails.timeout_s)
    effective_session_id = _resolve_session_id(session_id, ctx)
    effective_request_id = _resolve_request_id(request_id, ctx)
    return await runtime.run_browser_task(
        task=task,
        session_id=effective_session_id or None,
        request_id=effective_request_id or None,
        timeout_s=effective_timeout,
    )


@mcp.tool(
    name="browser_cancel_task",
    description="Cancel an in-flight browser run for a session/request.",
)
async def browser_cancel_task(
    session_id: str = "",
    request_id: str = "",
    ctx: Context | None = None,
) -> dict[str, Any]:
    runtime = await _get_runtime()
    effective_session_id = _resolve_session_id(session_id, ctx)
    if not effective_session_id:
        raise ValueError("session_id is required for cancellation")
    effective_request_id = _resolve_request_id(request_id, ctx)
    return await runtime.cancel_run(session_id=effective_session_id, request_id=effective_request_id or None)


@mcp.tool(
    name="browser_clear_cancel",
    description="Clear cancellation flag for a session/request.",
)
async def browser_clear_cancel(
    session_id: str = "",
    request_id: str = "",
    ctx: Context | None = None,
) -> dict[str, Any]:
    runtime = await _get_runtime()
    effective_session_id = _resolve_session_id(session_id, ctx)
    if not effective_session_id:
        raise ValueError("session_id is required to clear cancellation")
    effective_request_id = _resolve_request_id(request_id, ctx)
    return await runtime.clear_cancel(session_id=effective_session_id, request_id=effective_request_id or None)


@mcp.tool(
    name="browser_custom_action",
    description=(
        "Run a custom controller action by name. Use for actions the Playwright MCP does not provide. "
        "Optional: session_id, request_id, and action-specific args via params object "
        "(e.g. params={'text': 'hello'} for 'echo'). "
        "Built-in examples: ping, echo, browser_get_element_coordinates, browser_drag_and_drop. "
        "For browser_get_element_coordinates use at least element_source (element_target optional). "
        "For browser_drag_and_drop use element_source+element_target. "
        "Or use coord_source_x+coord_source_y+coord_target_x+coord_target_y. "
        "Aliases source/target and source_x/source_y/target_x/target_y are accepted. "
        "Register your own via playwright_runtime.controllers.action.register_action."
    ),
)
async def browser_custom_action(
    action: str,
    session_id: str = "",
    request_id: str = "",
    params: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    effective_session_id = _resolve_session_id(session_id, ctx)
    effective_request_id = _resolve_request_id(request_id, ctx)
    result = await run_action(
        action=action,
        session_id=effective_session_id,
        request_id=effective_request_id,
        **(params or {}),
    )
    if isinstance(result, dict) and str(result.get("error", "")).startswith("runtime_not_bound:"):
        runtime = await _get_runtime()
        bind_runtime(runtime)
        result = await run_action(
            action=action,
            session_id=effective_session_id,
            request_id=effective_request_id,
            **(params or {}),
        )
    return result


@mcp.tool(
    name="browser_list_custom_actions",
    description="List registered custom action names (for browser_custom_action).",
)
async def browser_list_custom_actions() -> dict[str, Any]:
    return {"ok": True, "actions": list_actions()}


@mcp.tool(
    name="browser_runtime_health",
    description="Return runtime readiness and selected model/provider config.",
)
async def browser_runtime_health() -> dict[str, Any]:
    provider, _api_key, api_base = resolve_model_settings()
    model_name = (os.getenv("MODEL_NAME") or "anthropic/claude-sonnet-4").strip()
    return {
        "ok": True,
        "provider": provider,
        "api_base": api_base,
        "model_name": model_name,
        "started": _runtime is not None,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Playwright runtime MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http", "http"],
        default=(os.getenv("PLAYWRIGHT_RUNTIME_MCP_TRANSPORT") or "stdio").strip().lower(),
        help="MCP transport mode. Use stdio for agent-launched server; sse/http for standalone server.",
    )
    parser.add_argument(
        "--host",
        default=(os.getenv("PLAYWRIGHT_RUNTIME_MCP_HOST") or "127.0.0.1").strip(),
        help="Host for sse/http transports.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int((os.getenv("PLAYWRIGHT_RUNTIME_MCP_PORT") or "8940").strip()),
        help="Port for sse/http transports.",
    )
    parser.add_argument(
        "--path",
        default=(os.getenv("PLAYWRIGHT_RUNTIME_MCP_PATH") or "").strip(),
        help="Optional custom endpoint path for sse/http transports.",
    )
    parser.add_argument(
        "--log-level",
        default=(os.getenv("PLAYWRIGHT_RUNTIME_MCP_LOG_LEVEL") or "INFO").strip(),
        help="Server log level.",
    )
    parser.add_argument(
        "--no-banner",
        action="store_true",
        help="Disable FastMCP startup banner.",
    )
    parser.add_argument(
        "--stateless-http",
        action="store_true",
        help=(
            "Enable stateless HTTP mode for http/streamable-http transports. "
            "Useful when clients intermittently lose transport session state."
        ),
    )
    return parser.parse_args()


def _apply_timeout_defaults(transport: str) -> None:
    """Set runtime timeout defaults based on transport when not explicitly provided."""
    os.environ.setdefault("BROWSER_TIMEOUT_S", "180")
    os.environ.setdefault("PLAYWRIGHT_TOOL_TIMEOUT_S", os.getenv("BROWSER_TIMEOUT_S", "180"))


def _resolve_stateless_http(args: argparse.Namespace) -> bool:
    """Resolve whether to run HTTP transports in stateless mode."""
    if args.stateless_http:
        return True

    env_raw = (os.getenv("PLAYWRIGHT_RUNTIME_MCP_STATELESS_HTTP") or "").strip().lower()
    if env_raw in {"1", "true", "yes", "on"}:
        return True
    if env_raw in {"0", "false", "no", "off"}:
        return False

    # Default to stateless for streamable/http to avoid stateful session crashes.
    return args.transport in {"streamable-http", "http"}


def main() -> None:
    args = parse_args()
    _apply_timeout_defaults(args.transport)
    kwargs: Dict[str, Any] = {
        "show_banner": not args.no_banner,
        "log_level": args.log_level,
    }
    if args.transport in {"sse", "streamable-http", "http"}:
        kwargs["host"] = args.host
        kwargs["port"] = args.port
        if args.path:
            kwargs["path"] = args.path
    if args.transport in {"streamable-http", "http"} and _resolve_stateless_http(args):
        kwargs["stateless_http"] = True
    mcp.run(transport=args.transport, **kwargs)


if __name__ == "__main__":
    main()
