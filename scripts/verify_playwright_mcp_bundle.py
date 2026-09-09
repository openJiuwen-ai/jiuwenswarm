# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Verify Node and the packaged Playwright MCP CLI in a frozen build."""

from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
from pathlib import Path

from jiuwenswarm.common.playwright_mcp_runtime import (
    PLAYWRIGHT_MCP_VERSION,
    materialize_bundled_runtime,
    resolve_node_executable,
)


LOGGER = logging.getLogger(__name__)


def verify_playwright_mcp_bundle() -> None:
    if not getattr(sys, "frozen", False):
        raise RuntimeError(
            "Playwright MCP bundle verification must run inside the frozen executable"
        )
    with tempfile.TemporaryDirectory(prefix="jiuwenswarm-playwright-mcp-verify-") as temp:
        cli, manifest = materialize_bundled_runtime(
            user_workspace_dir=Path(temp),
        )
        node = resolve_node_executable()
        result = subprocess.run(
            [node, str(cli), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    help_output = (result.stdout + result.stderr).lower()
    if result.returncode != 0 or not any(
        marker in help_output for marker in ("playwright mcp", "playwright-mcp")
    ):
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise RuntimeError(f"Packaged Playwright MCP CLI --help failed: {detail}")
    if manifest.get("version") != PLAYWRIGHT_MCP_VERSION:
        raise RuntimeError(
            f"Unexpected Playwright MCP version: {manifest.get('version')!r}"
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    verify_playwright_mcp_bundle()
    LOGGER.info("Playwright MCP frozen bundle verification passed (%s)", PLAYWRIGHT_MCP_VERSION)
