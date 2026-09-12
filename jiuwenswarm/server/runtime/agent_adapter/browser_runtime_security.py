# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fail-closed security profile for the current local Playwright MCP runtime."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.tool import McpServerConfig

GUARD_PROVIDER = "playwright_init_page"
GUARD_VERSION = "browser-public-network-guard-v1"
GUARD_INIT_PAGE_PATH = Path(__file__).with_name("browser_runtime_network_guard.js")
GUARD_SHA256 = "9b8a2c536dc4f7fe3dc2d91147d9184b843d60b07951a27da338b18304f23cdd"

_OFFICIAL_PACKAGE = re.compile(r"^@playwright/mcp(?:@[0-9A-Za-z][0-9A-Za-z._-]*)?$")
_OFFICIAL_CAPS_ARG = "--caps=pdf,vision,devtools,config,network,storage,testing"
_SUPPORTED_PARAMS = frozenset({"command", "args", "cwd", "env", "timeout_s"})
_SUPPORTED_ENV = frozenset(
    {"PLAYWRIGHT_BROWSERS_PATH", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}
)
_REMOTE_OR_CUSTOM_ENV = frozenset(
    {
        "PLAYWRIGHT_CDP_URL",
        "PLAYWRIGHT_MCP_CDP_ENDPOINT",
        "PLAYWRIGHT_MCP_CONFIG",
        "PLAYWRIGHT_MCP_EXTENSION",
        "PLAYWRIGHT_MCP_INIT_PAGE",
    }
)


@dataclass(frozen=True, slots=True)
class BrowserRuntimeSecurityProfile:
    """Runtime-owned browser guard installation result."""

    network_guard_enforced: bool = False
    guard_provider: str = ""
    guard_version: str = ""
    guard_digest: str = ""
    failure_reason: str = ""
    egress_guard_enforced: bool = False
    egress_guard_provider: str = ""
    egress_guard_failure_reason: str = ""


def apply_browser_runtime_security_profile(
    mcp_cfg: McpServerConfig,
) -> tuple[McpServerConfig, BrowserRuntimeSecurityProfile]:
    """Inject the owned guard only into an exact local official stdio config."""
    raw_params = getattr(mcp_cfg, "params", {})
    if not isinstance(raw_params, Mapping):
        return mcp_cfg, _profile(
            enforced=False,
            digest=_guard_digest(),
            failure_reason="unsupported_browser_config",
        )
    params = dict(raw_params)
    reason = _unsupported_runtime_reason(mcp_cfg, params)
    digest = _guard_digest()
    if not reason and digest != f"sha256:{GUARD_SHA256}":
        reason = "guard_digest_mismatch" if digest else "guard_init_page_missing"
    if reason:
        return _clone_mcp_config(mcp_cfg, params), _profile(
            enforced=False,
            digest=digest,
            failure_reason=reason,
        )

    guarded_args = [str(arg) for arg in params["args"]]
    guarded_args.extend(["--init-page", str(GUARD_INIT_PAGE_PATH)])
    guarded_args.append("--block-service-workers")
    params["args"] = guarded_args
    if not _injected_flags_are_exact(guarded_args):
        return _clone_mcp_config(mcp_cfg, dict(getattr(mcp_cfg, "params", {}) or {})), (
            _profile(
                enforced=False,
                digest=digest,
                failure_reason="guard_flag_injection_failed",
            )
        )
    return _clone_mcp_config(mcp_cfg, params), _profile(
        enforced=True,
        digest=digest,
        failure_reason="",
    )


def _unsupported_runtime_reason(
    mcp_cfg: McpServerConfig,
    params: dict[str, Any],
) -> str:
    if (
        mcp_cfg.server_id != "playwright_official_stdio"
        or mcp_cfg.server_name != "playwright-official"
        or mcp_cfg.server_path != "stdio://playwright"
    ):
        return "unsupported_browser_runtime"
    if mcp_cfg.client_type != "stdio" or bool(
        getattr(mcp_cfg, "auth_headers", {})
    ):
        return "unsupported_browser_runtime"
    if bool(getattr(mcp_cfg, "auth_query_params", {})):
        return "unsupported_browser_runtime"
    if set(params) - _SUPPORTED_PARAMS or params.get("command") != "npx":
        return "unsupported_browser_config"

    raw_env = params.get("env", {})
    raw_args = params.get("args")
    if not isinstance(raw_env, Mapping) or not isinstance(raw_args, (list, tuple)):
        return "unsupported_browser_config"
    env_map = dict(raw_env)
    if set(env_map) - _SUPPORTED_ENV:
        return "unsupported_browser_environment"
    if any(str(os.getenv(key) or "").strip() for key in _REMOTE_OR_CUSTOM_ENV):
        return "remote_or_custom_browser_configured"
    if (os.getenv("BROWSER_DRIVER") or "").strip():
        return "unsupported_browser_driver"
    if str(os.getenv("BROWSER_MANAGED_ARGS") or "").strip():
        return "unsafe_browser_flags"

    args = [str(arg) for arg in raw_args]
    packages = [arg for arg in args if _OFFICIAL_PACKAGE.fullmatch(arg)]
    permitted = {"-y", "--yes", "--headless", _OFFICIAL_CAPS_ARG, *packages}
    if len(packages) != 1 or any(arg not in permitted for arg in args):
        return "unsupported_browser_args"
    if (
        args.count("-y") + args.count("--yes") != 1
        or args.count("--headless") > 1
        or args.count(_OFFICIAL_CAPS_ARG) > 1
    ):
        return "unsupported_browser_args"
    return ""


def _injected_flags_are_exact(args: list[str]) -> bool:
    return (
        args.count("--init-page") == 1
        and args.count(str(GUARD_INIT_PAGE_PATH)) == 1
        and args.count("--block-service-workers") == 1
    )


def _clone_mcp_config(
    mcp_cfg: McpServerConfig,
    params: dict[str, Any],
) -> McpServerConfig:
    return McpServerConfig(
        server_id=mcp_cfg.server_id,
        server_name=mcp_cfg.server_name,
        server_path=mcp_cfg.server_path,
        client_type=mcp_cfg.client_type,
        params=params,
        auth_headers=dict(getattr(mcp_cfg, "auth_headers", {}) or {}),
        auth_query_params=dict(getattr(mcp_cfg, "auth_query_params", {}) or {}),
    )


def _guard_digest() -> str:
    try:
        content = GUARD_INIT_PAGE_PATH.read_bytes()
    except OSError:
        return ""
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _profile(
    *,
    enforced: bool,
    digest: str,
    failure_reason: str,
) -> BrowserRuntimeSecurityProfile:
    return BrowserRuntimeSecurityProfile(
        network_guard_enforced=enforced,
        guard_provider=GUARD_PROVIDER,
        guard_version=GUARD_VERSION,
        guard_digest=digest,
        failure_reason=failure_reason,
        egress_guard_enforced=False,
        egress_guard_failure_reason="egress_guard_unverified",
    )
