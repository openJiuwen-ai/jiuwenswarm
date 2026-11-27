#!/usr/bin/env python
# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Test harness for playwright_runtime package.

Usage (from repo root):
  uv run python src/test_playwright_runtime.py
  uv run python src/test_playwright_runtime.py --live
  uv run python src/test_playwright_runtime.py --live --query "Go to example.com and return title"
  uv run python src/test_playwright_runtime.py --live --via-controller --controller-action browser_get_element_coordinates --controller-params-json "{\"url\":\"https://example.com\",\"element_source\":\"Learn more\",\"element_target\":\"Example Domain\"}"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict

from playwright_runtime.config import (
    BrowserRunGuardrails,
    build_playwright_mcp_config,
    resolve_model_settings,
)

GUARDRAIL_MAX_STEPS = 20
GUARDRAIL_MAX_FAILURES = 2
GUARDRAIL_RETRY_ONCE = True


def _check(condition: bool, msg: str) -> None:
    if not condition:
        raise AssertionError(msg)


def _autoload_env() -> Dict[str, Any]:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return {"loaded": False, "path": str(env_path), "reason": "missing"}

    try:
        from dotenv import load_dotenv

        loaded = load_dotenv(dotenv_path=env_path, override=True)
        return {"loaded": bool(loaded), "path": str(env_path), "reason": "ok"}
    except Exception as exc:
        return {"loaded": False, "path": str(env_path), "reason": f"error: {exc}"}


def run_smoke_checks() -> Dict[str, Any]:
    mcp_cfg = build_playwright_mcp_config()
    provider, api_key, api_base = resolve_model_settings()

    _check(mcp_cfg.client_type == "stdio", "Expected stdio MCP transport")
    _check(bool(mcp_cfg.server_id), "server_id is empty")
    _check(bool(mcp_cfg.server_name), "server_name is empty")
    _check(bool(mcp_cfg.params.get("command")), "MCP command is missing")
    _check(isinstance(mcp_cfg.params.get("args", []), list), "MCP args must be a list")
    _check(provider in ("openai", "openrouter"), "Unexpected provider")
    _check(bool(api_base), "api_base is empty")

    return {
        "ok": True,
        "mode": "smoke",
        "provider": provider,
        "has_api_key": bool(api_key),
        "mcp_server_id": mcp_cfg.server_id,
        "mcp_server_name": mcp_cfg.server_name,
        "mcp_command": mcp_cfg.params.get("command"),
        "mcp_args": mcp_cfg.params.get("args"),
    }


async def run_live_check(query: str, session_id: str, cancel_after_s: float = 0.0) -> Dict[str, Any]:
    try:
        from playwright_runtime.runtime import BrowserAgentRuntime
    except ModuleNotFoundError as exc:
        missing = getattr(exc, "name", None) or str(exc)
        raise RuntimeError(
            f"Live mode missing dependency: {missing}. "
            "Install project runtime dependencies before using --live."
        ) from exc

    provider, api_key, api_base = resolve_model_settings()
    if not api_key:
        raise RuntimeError("Missing API key for live run. Set OPENROUTER_API_KEY or OPENAI_API_KEY.")

    model_name = (os.getenv("MODEL_NAME") or "anthropic/claude-sonnet-4").strip()
    guardrails = BrowserRunGuardrails(
        max_steps=GUARDRAIL_MAX_STEPS,
        max_failures=GUARDRAIL_MAX_FAILURES,
        timeout_s=int(os.getenv("BROWSER_TIMEOUT_S", "180")),
        retry_once=GUARDRAIL_RETRY_ONCE,
    )
    runtime = BrowserAgentRuntime(
        provider=provider,
        api_key=api_key,
        api_base=api_base,
        model_name=model_name,
        mcp_cfg=build_playwright_mcp_config(),
        guardrails=guardrails,
    )

    try:
        await runtime.ensure_started()
        if cancel_after_s > 0:
            request_task = asyncio.create_task(runtime.handle_request(query=query, session_id=session_id))
            await asyncio.sleep(cancel_after_s)
            await runtime.cancel_run(session_id=session_id)
            result = await request_task
        else:
            result = await runtime.handle_request(query=query, session_id=session_id)
        return {
            "ok": bool(result.get("ok", False)),
            "mode": "live",
            "session_id": result.get("session_id"),
            "request_id": result.get("request_id"),
            "final": result.get("final"),
            "error": result.get("error"),
        }
    finally:
        await runtime.shutdown()


def _parse_json_object(raw: str) -> Dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("controller params JSON must be an object")
    return value


def _extract_first_url(text: str) -> str:
    match = re.search(r"https?://[^\s\"'<>]+", str(text or ""))
    if not match:
        return ""
    return match.group(0).rstrip(").,;!?")


async def run_live_controller_check(
    query: str,
    session_id: str,
    controller_action: str,
    controller_params_json: str = "",
) -> Dict[str, Any]:
    try:
        from playwright_runtime.runtime import BrowserAgentRuntime
    except ModuleNotFoundError as exc:
        missing = getattr(exc, "name", None) or str(exc)
        raise RuntimeError(
            f"Live mode missing dependency: {missing}. "
            "Install project runtime dependencies before using --live."
        ) from exc

    provider, api_key, api_base = resolve_model_settings()
    if not api_key:
        raise RuntimeError("Missing API key for live run. Set OPENROUTER_API_KEY or OPENAI_API_KEY.")

    model_name = (os.getenv("MODEL_NAME") or "anthropic/claude-sonnet-4").strip()
    guardrails = BrowserRunGuardrails(
        max_steps=GUARDRAIL_MAX_STEPS,
        max_failures=GUARDRAIL_MAX_FAILURES,
        timeout_s=int(os.getenv("BROWSER_TIMEOUT_S", "180")),
        retry_once=GUARDRAIL_RETRY_ONCE,
    )
    runtime = BrowserAgentRuntime(
        provider=provider,
        api_key=api_key,
        api_base=api_base,
        model_name=model_name,
        mcp_cfg=build_playwright_mcp_config(),
        guardrails=guardrails,
    )

    try:
        await runtime.ensure_started()
        action_name = (controller_action or "").strip() or "browser_task"
        params = _parse_json_object(controller_params_json)
        if not params and action_name in ("browser_task", "run_browser_task"):
            params = {"task": query}
        if action_name in ("browser_get_element_coordinates", "browser_drag_and_drop"):
            url_value = str(params.get("url") or "").strip()
            if not url_value:
                inferred_url = _extract_first_url(query)
                if inferred_url:
                    params["url"] = inferred_url

        result = await runtime.run_custom_action(
            action=action_name,
            params=params,
            session_id=session_id,
        )
        return {
            "ok": bool(result.get("ok", False)),
            "mode": "live_controller",
            "action": action_name,
            "session_id": result.get("session_id"),
            "request_id": result.get("request_id"),
            "final": result.get("final"),
            "error": result.get("error"),
            "result": result,
        }
    finally:
        await runtime.shutdown()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Playwright runtime test harness")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run full live browser/runtime check (requires API key and Playwright MCP runtime).",
    )
    parser.add_argument(
        "--query",
        default="Go to https://example.com and return the page title.",
        help="Query used for --live mode.",
    )
    parser.add_argument(
        "--session-id",
        default="test-browser-session",
        help="Session id used for --live mode.",
    )
    parser.add_argument(
        "--cancel-after-s",
        type=float,
        default=0.0,
        help="For --live mode, send a cancel signal after N seconds (session-level cancel).",
    )
    parser.add_argument(
        "--via-controller",
        action="store_true",
        help="For --live mode, bypass main-agent tool choice and run through controller directly.",
    )
    parser.add_argument(
        "--controller-action",
        default="browser_task",
        help="Action name used with --via-controller (default: browser_task).",
    )
    parser.add_argument(
        "--controller-params-json",
        default="",
        help="Optional JSON object string for --via-controller action params.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        env_info = _autoload_env()
        smoke_result = run_smoke_checks()
        smoke_result["dotenv"] = env_info
        print(json.dumps(smoke_result, ensure_ascii=False, indent=2))

        if not args.live:
            return 0

        if args.via_controller:
            if args.cancel_after_s > 0:
                raise RuntimeError("--cancel-after-s is not supported with --via-controller mode.")
            live_result = asyncio.run(
                run_live_controller_check(
                    query=args.query,
                    session_id=args.session_id,
                    controller_action=args.controller_action,
                    controller_params_json=args.controller_params_json,
                )
            )
        else:
            live_result = asyncio.run(
                run_live_check(query=args.query, session_id=args.session_id, cancel_after_s=args.cancel_after_s)
            )
        print(json.dumps(live_result, ensure_ascii=False, indent=2))
        return 0 if live_result.get("ok") else 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
