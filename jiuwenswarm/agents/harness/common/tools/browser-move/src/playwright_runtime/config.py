#!/usr/bin/env python
# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Runtime configuration helpers."""

from __future__ import annotations

import json
import logging
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

from openjiuwen.core.foundation.tool import McpServerConfig
from playwright_runtime import REPO_ROOT

logger = logging.getLogger(__name__)


@dataclass
class BrowserRunGuardrails:
    max_steps: int = 20
    max_failures: int = 2
    timeout_s: int = 180
    retry_once: bool = True


def parse_args(value: str) -> List[str]:
    value = (value or "").strip()
    if not value:
        return []
    if value.startswith("["):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except json.JSONDecodeError as e:
            logger.debug("parse_args: JSON decode failed, falling back to shlex: %s", e)
    return shlex.split(value)


def _first_non_empty_env(*keys: str) -> str:
    for key in keys:
        value = (os.getenv(key) or "").strip()
        if value:
            return value
    return ""


def _normalize_provider(provider: str) -> str:
    raw = (provider or "").strip()
    lowered = raw.lower()
    if lowered in {"openai", "openrouter", "siliconflow", "dashscope"}:
        return lowered
    if lowered in {"alibaba", "aliyun"}:
        return "dashscope"
    if lowered in {"silicon-flow", "silicon_flow"}:
        return "siliconflow"
    return raw


def _is_truthy_env(value: str) -> bool:
    lowered = (value or "").strip().lower()
    return lowered in {"1", "true", "yes", "on"}


def _infer_provider_from_api_base(api_base: str) -> str:
    base = (api_base or "").strip().lower()
    if not base:
        return ""
    if "openrouter.ai" in base:
        return "openrouter"
    if "siliconflow.cn" in base or "siliconflow" in base:
        return "siliconflow"
    if "dashscope.aliyuncs.com" in base or "dashscope" in base:
        return "dashscope"
    return "openai"


def resolve_playwright_mcp_cwd() -> str:
    """Resolve MCP working directory with relocatable defaults."""
    process_cwd = Path.cwd().expanduser()
    if (process_cwd / "src" / "playwright_runtime").exists():
        return str(process_cwd.resolve())
    return str(Path(REPO_ROOT).expanduser().resolve())


def build_playwright_mcp_config() -> McpServerConfig:
    command = os.getenv("PLAYWRIGHT_MCP_COMMAND", "npx").strip() or "npx"
    args = parse_args(os.getenv("PLAYWRIGHT_MCP_ARGS", "-y @playwright/mcp@latest"))
    cwd = resolve_playwright_mcp_cwd()
    driver_mode = (os.getenv("BROWSER_DRIVER") or "").strip().lower()
    extension_mode = driver_mode == "extension" or _is_truthy_env(os.getenv("PLAYWRIGHT_MCP_EXTENSION") or "")
    timeout_raw = (
        os.getenv("PLAYWRIGHT_MCP_TIMEOUT_S")
        or os.getenv("BROWSER_TIMEOUT_S")
        or "180"
    )

    env_map: Dict[str, str] = {}
    for key in (
        "PLAYWRIGHT_BROWSERS_PATH",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
    ):
        value = os.getenv(key)
        if value:
            env_map[key] = value

    extra_env_json = (os.getenv("PLAYWRIGHT_MCP_ENV_JSON") or "").strip()
    if extra_env_json:
        try:
            extra = json.loads(extra_env_json)
            if isinstance(extra, dict):
                for k, v in extra.items():
                    env_map[str(k)] = str(v)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid PLAYWRIGHT_MCP_ENV_JSON: {exc}") from exc

    if extension_mode:
        env_map["PLAYWRIGHT_MCP_EXTENSION"] = "true"
        extension_token = _first_non_empty_env("PLAYWRIGHT_MCP_EXTENSION_TOKEN")
        if extension_token:
            env_map["PLAYWRIGHT_MCP_EXTENSION_TOKEN"] = extension_token
        if "--extension" not in args:
            args.append("--extension")
    else:
        # CDP support for official Playwright MCP server.
        cdp_endpoint = _first_non_empty_env("PLAYWRIGHT_MCP_CDP_ENDPOINT", "PLAYWRIGHT_CDP_URL")
        cdp_headers = _first_non_empty_env("PLAYWRIGHT_MCP_CDP_HEADERS", "PLAYWRIGHT_CDP_HEADERS")
        cdp_timeout = _first_non_empty_env("PLAYWRIGHT_MCP_CDP_TIMEOUT", "PLAYWRIGHT_CDP_TIMEOUT_MS")
        browser_name = _first_non_empty_env("PLAYWRIGHT_MCP_BROWSER")
        device_name = _first_non_empty_env("PLAYWRIGHT_MCP_DEVICE")

        if cdp_endpoint:
            if device_name:
                raise ValueError("PLAYWRIGHT_MCP_DEVICE is not supported with CDP endpoint mode.")
            env_map["PLAYWRIGHT_MCP_CDP_ENDPOINT"] = cdp_endpoint
            if not browser_name:
                # CDP mode is Chromium-only.
                env_map["PLAYWRIGHT_MCP_BROWSER"] = "chrome"
        if cdp_headers:
            env_map["PLAYWRIGHT_MCP_CDP_HEADERS"] = cdp_headers
        if cdp_timeout:
            env_map["PLAYWRIGHT_MCP_CDP_TIMEOUT"] = cdp_timeout

    params: Dict[str, Any] = {
        "command": command,
        "args": args,
        "cwd": cwd,
    }
    try:
        timeout_s = int(timeout_raw)
        if timeout_s > 0:
            params["timeout_s"] = timeout_s
    except (TypeError, ValueError) as e:
        logger.warning(
            "Invalid PLAYWRIGHT_MCP_TIMEOUT_S/BROWSER_TIMEOUT_S value %r, "
            "using MCP server default: %s",
            timeout_raw, e,
        )
    if env_map:
        params["env"] = env_map

    return McpServerConfig(
        server_id="playwright_official_stdio",
        server_name="playwright-official",
        server_path="stdio://playwright",
        client_type="stdio",
        params=params,
    )


def resolve_model_settings() -> Tuple[str, str, str]:
    provider_mode = _normalize_provider(
        _first_non_empty_env("MODEL_PROVIDER", "MODEL_CLIENT_PROVIDER")
    )
    if provider_mode and provider_mode not in {"openai", "openrouter", "siliconflow", "dashscope"}:
        raise ValueError(
            f"Unsupported MODEL_PROVIDER '{provider_mode}'. "
            "Supported: openai, openrouter, siliconflow, dashscope."
        )

    explicit_api_key = _first_non_empty_env("API_KEY", "MODEL_API_KEY")
    explicit_api_base = _first_non_empty_env("API_BASE", "MODEL_API_BASE")

    if provider_mode:
        provider = provider_mode
    else:
        base_hint = explicit_api_base or _first_non_empty_env(
            "OPENROUTER_BASE_URL",
            "OPENROUTER_API_BASE",
            "SILICONFLOW_BASE_URL",
            "SILICONFLOW_API_BASE",
            "DASHSCOPE_BASE_URL",
            "DASHSCOPE_API_BASE",
            "OPENAI_BASE_URL",
            "OPENAI_API_BASE",
        )
        provider = _infer_provider_from_api_base(base_hint)
        if not provider:
            has_openrouter_key = bool((os.getenv("OPENROUTER_API_KEY") or "").strip())
            has_siliconflow_key = bool((os.getenv("SILICONFLOW_API_KEY") or "").strip())
            has_dashscope_key = bool((os.getenv("DASHSCOPE_API_KEY") or "").strip())
            if has_openrouter_key:
                provider = "openrouter"
            elif has_siliconflow_key:
                provider = "siliconflow"
            elif has_dashscope_key:
                provider = "dashscope"
            else:
                provider = "openai"

    if provider == "openrouter":
        api_key = _first_non_empty_env(
            "API_KEY",
            "MODEL_API_KEY",
            "OPENROUTER_API_KEY",
            "OPENAI_API_KEY",
        )
        api_base = _first_non_empty_env(
            "API_BASE",
            "MODEL_API_BASE",
            "OPENROUTER_BASE_URL",
            "OPENROUTER_API_BASE",
        ) or "https://openrouter.ai/api/v1"
    elif provider == "siliconflow":
        api_key = _first_non_empty_env(
            "API_KEY",
            "MODEL_API_KEY",
            "SILICONFLOW_API_KEY",
            "OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
        )
        api_base = _first_non_empty_env(
            "API_BASE",
            "MODEL_API_BASE",
            "SILICONFLOW_BASE_URL",
            "SILICONFLOW_API_BASE",
        ) or "https://api.siliconflow.cn/v1"
    elif provider == "dashscope":
        api_key = _first_non_empty_env(
            "API_KEY",
            "MODEL_API_KEY",
            "DASHSCOPE_API_KEY",
            "OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
        )
        api_base = _first_non_empty_env(
            "API_BASE",
            "MODEL_API_BASE",
            "DASHSCOPE_BASE_URL",
            "DASHSCOPE_API_BASE",
        ) or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    else:
        api_key = _first_non_empty_env(
            "API_KEY",
            "MODEL_API_KEY",
            "OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
        )
        api_base = _first_non_empty_env(
            "API_BASE",
            "MODEL_API_BASE",
            "OPENAI_BASE_URL",
            "OPENAI_API_BASE",
        ) or "https://api.openai.com/v1"
    return provider, api_key, api_base