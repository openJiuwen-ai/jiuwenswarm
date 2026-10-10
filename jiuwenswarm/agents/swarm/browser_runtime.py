# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Swarm-only external Chrome fallback inside an Electron deployment."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from openjiuwen.harness.tools.browser_move.playwright_runtime.config import (
    BrowserInstanceConfig,
    PLAYWRIGHT_MCP_CAPABILITY_NAMES,
    RuntimeSettings,
)

from jiuwenswarm.agents.harness.common.browser_config import resolve_chrome_path
from jiuwenswarm.agents.harness.common.electron_sideview import (
    apply_session_sideview_target,
    electron_browser_selected,
)
from jiuwenswarm.common.playwright_mcp_runtime import resolve_playwright_mcp_launch
from jiuwenswarm.common.utils import get_user_workspace_dir


def apply_swarm_browser_settings(
    settings: RuntimeSettings,
    config: dict[str, Any],
    session_id: str,
    *,
    member_id: str = "",
) -> RuntimeSettings:
    """Keep backend selection local to this member, never change BROWSER_DRIVER.

    The adapter synchronizes the shared browser.headless setting before team
    construction. SDK managed instances consume it via BROWSER_MANAGED_ARGS;
    Electron's remote instances do not launch Chrome or consume those flags.
    """
    chrome_path = resolve_chrome_path(config)
    if not chrome_path or not electron_browser_selected():
        return apply_session_sideview_target(
            settings, session_id, member_id=member_id, label=member_id,
        )

    # Do not inherit Electron's target wrapper as the managed Chrome MCP server.
    # The normal resolver still supports the packaged, offline MCP/Node runtime.
    launch = resolve_playwright_mcp_launch(environ={})
    identity = json.dumps([session_id.strip(), member_id.strip()], ensure_ascii=False)
    key = "swarm_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    workspace = get_user_workspace_dir()
    instance = BrowserInstanceConfig(
        key=key,
        driver_mode="managed",
        browser_binary=chrome_path,
        profile_name=key,
        user_data_dir=str(workspace / ".browser-profiles" / key),
    )
    params = dict(settings.mcp_cfg.params or {})
    env = {}
    for name, value in (params.get("env") or {}).items():
        if name.startswith(("PLAYWRIGHT_MCP_", "PLAYWRIGHT_CDP_")):
            continue
        if name == "ELECTRON_RUN_AS_NODE":
            continue
        env[name] = value
    params.update(
        command=launch.command,
        args=[*launch.args, "--caps=" + ",".join(PLAYWRIGHT_MCP_CAPABILITY_NAMES)],
        cwd=str(workspace),
        env=env,
    )
    mcp_cfg = settings.mcp_cfg.model_copy(update={
        "params": params,
        "server_id": f"playwright_managed_{key}",
        "server_name": "playwright-official",
    })
    return replace(settings, instance=instance, mcp_cfg=mcp_cfg)
