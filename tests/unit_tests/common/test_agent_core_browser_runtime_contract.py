# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cross-repository contracts for the agent-core browser runtime."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.harness.tools.browser_move.playwright_runtime import browser_tools
from openjiuwen.harness.tools.browser_move.playwright_runtime import (
    service as service_module,
)
from openjiuwen.harness.tools.browser_move.playwright_runtime.config import (
    BrowserRunGuardrails,
)
from openjiuwen.harness.tools.browser_move.playwright_runtime.service import (
    BrowserService,
)


@pytest.mark.asyncio
async def test_direct_mcp_command_does_not_require_npx_and_reaches_registration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The bundled Node launch contract must survive agent-core upgrades."""
    executable = Path(sys.executable).resolve()
    monkeypatch.setenv("BROWSER_DRIVER", "remote")
    monkeypatch.setenv(
        "BROWSER_PROFILE_STORE_PATH",
        str(tmp_path / "browser-profiles.json"),
    )

    original_which = service_module.shutil.which

    def which_without_npx(command: str, *args: Any, **kwargs: Any) -> str | None:
        if command == "npx":
            return None
        return original_which(command, *args, **kwargs)

    monkeypatch.setattr(service_module.shutil, "which", which_without_npx)
    monkeypatch.setattr(
        browser_tools,
        "ensure_browser_runtime_client_patch",
        lambda: None,
    )

    registered: dict[str, Any] = {}

    async def start_runner() -> None:
        return None

    async def add_mcp_server(config: McpServerConfig, *, tag: str) -> None:
        registered["config"] = config
        registered["params"] = dict(config.params)
        registered["tag"] = tag
        return None

    monkeypatch.setattr(
        service_module,
        "Runner",
        SimpleNamespace(
            start=start_runner,
            resource_mgr=SimpleNamespace(add_mcp_server=add_mcp_server),
        ),
    )
    monkeypatch.setattr(
        service_module.BROWSER_SERVICE_REGISTRY,
        "activate_binding",
        lambda *_: False,
    )

    mcp_cfg = McpServerConfig(
        server_id="playwright_official_stdio",
        server_name="playwright-official",
        server_path="stdio://playwright",
        client_type="stdio",
        params={
            "command": str(executable),
            "args": ["node_modules/@playwright/mcp/cli.js"],
            "cwd": str(tmp_path),
            "env": {},
            "timeout_s": 30,
        },
    )
    browser_service = BrowserService(
        provider="test",
        api_key="test",
        api_base="https://example.invalid",
        model_name="test",
        mcp_cfg=mcp_cfg,
        guardrails=BrowserRunGuardrails(),
    )
    browser_service._task_binding_ref_count = 1
    monkeypatch.setattr(browser_service, "_acquire_registry_lease", lambda: None)

    async def no_managed_driver() -> bool:
        return False

    monkeypatch.setattr(
        browser_service,
        "_ensure_managed_driver_started",
        no_managed_driver,
    )
    monkeypatch.setattr(browser_service, "_ensure_screenshots_dir", lambda: None)

    await browser_service.ensure_runtime_ready()

    assert registered["config"] is mcp_cfg
    assert registered["tag"] == "browser.service"
    assert registered["params"]["command"] == str(executable)
