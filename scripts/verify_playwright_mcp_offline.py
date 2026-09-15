#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Offline Playwright MCP smoke test using managed Chrome over CDP.

The test materializes the committed archive, initializes an MCP stdio session,
discovers tools, attaches to a temporary headless Chrome profile, and navigates
to a local-only page. npm is never invoked.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from jiuwenswarm.common.playwright_mcp_runtime import (
    materialize_bundled_runtime,
    resolve_node_executable,
)


LOGGER = logging.getLogger(__name__)

EXPECTED_TOOLS = frozenset(
    {
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_evaluate",
    }
)
ALL_CAPABILITIES = "pdf,vision,devtools,config,network,storage,testing"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, message_format: str, *args: object) -> None:
        del message_format, args


def _resolve_chrome(explicit: str | None) -> Path:
    candidates: list[str | None] = [explicit]
    if os.name == "nt":
        candidates.extend(
            [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            ]
        )
    elif sys.platform == "darwin":
        candidates.extend(
            [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            ]
        )
    candidates.extend(
        shutil.which(name)
        for name in ("google-chrome", "google-chrome-stable", "chromium", "microsoft-edge")
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise RuntimeError("Chrome or Edge was not found; pass --chrome with an executable path")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_cdp(endpoint: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 20
    version_url = f"{endpoint}/json/version"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Chrome exited before CDP was ready ({process.returncode})")
        try:
            with urllib.request.urlopen(version_url, timeout=1) as response:  # noqa: S310 - loopback only
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"Chrome CDP endpoint did not become ready: {endpoint}")


def _result_text(result: object) -> str:
    content = getattr(result, "content", ())
    return "\n".join(
        str(getattr(item, "text"))
        for item in content
        if getattr(item, "text", None) is not None
    )


async def _exercise_mcp(
    *,
    node: str,
    cli: Path,
    cwd: Path,
    cdp_endpoint: str,
    local_url: str,
) -> int:
    child_env = os.environ.copy()
    child_env.update(
        {
            "PLAYWRIGHT_MCP_CDP_ENDPOINT": cdp_endpoint,
            "PLAYWRIGHT_MCP_BROWSER": "chrome",
            "npm_config_offline": "true",
            "npm_config_registry": "http://127.0.0.1:9",
        }
    )
    params = StdioServerParameters(
        command=node,
        args=[str(cli), f"--caps={ALL_CAPABILITIES}", "--headless"],
        env=child_env,
        cwd=cwd,
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()
            tool_names = {tool.name for tool in listed.tools}
            missing = EXPECTED_TOOLS - tool_names
            if missing:
                raise RuntimeError(f"MCP tool discovery is missing: {sorted(missing)}")
            navigate = await session.call_tool(
                "browser_navigate",
                {"url": local_url},
                read_timeout_seconds=timedelta(seconds=30),
            )
            if getattr(navigate, "isError", False):
                raise RuntimeError(f"browser_navigate failed: {_result_text(navigate)}")
            snapshot = await session.call_tool(
                "browser_snapshot",
                {},
                read_timeout_seconds=timedelta(seconds=30),
            )
            snapshot_text = _result_text(snapshot)
            if getattr(snapshot, "isError", False) or "Offline Playwright MCP" not in snapshot_text:
                raise RuntimeError(f"local page was not observed: {snapshot_text[-2000:]}")
            return len(tool_names)


def verify(*, node_path: str | None = None, chrome_path: str | None = None) -> int:
    chrome = _resolve_chrome(chrome_path)
    with tempfile.TemporaryDirectory(prefix="jiuwenswarm-mcp-offline-") as temp:
        root = Path(temp)
        site = root / "site"
        site.mkdir()
        (site / "index.html").write_text(
            "<!doctype html><title>Offline Playwright MCP</title>"
            "<h1>Offline Playwright MCP</h1>",
            encoding="utf-8",
        )
        handler = partial(_QuietHandler, directory=str(site))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        cdp_port = _free_port()
        cdp_endpoint = f"http://127.0.0.1:{cdp_port}"
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        chrome_process = subprocess.Popen(
            [
                str(chrome),
                "--headless=new",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-sync",
                "--metrics-recording-only",
                f"--remote-debugging-port={cdp_port}",
                f"--user-data-dir={root / 'chrome-profile'}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        try:
            _wait_for_cdp(cdp_endpoint, chrome_process)
            cli, manifest = materialize_bundled_runtime(user_workspace_dir=root / "workspace")
            node = resolve_node_executable(
                which=(lambda _: node_path) if node_path else shutil.which,
            )
            tool_count = asyncio.run(
                asyncio.wait_for(
                    _exercise_mcp(
                        node=node,
                        cli=cli,
                        cwd=root,
                        cdp_endpoint=cdp_endpoint,
                        local_url=f"http://127.0.0.1:{server.server_port}/index.html",
                    ),
                    timeout=60,
                )
            )
        finally:
            chrome_process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                chrome_process.wait(timeout=10)
            if chrome_process.poll() is None:
                chrome_process.kill()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
    LOGGER.info(
        "Offline Playwright MCP verification passed: version=%s, tools=%s, browser=%s",
        manifest["version"],
        tool_count,
        chrome.name,
    )
    return tool_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", help="Absolute Node.js 20+ executable")
    parser.add_argument("--chrome", help="Absolute Chrome/Edge executable")
    args = parser.parse_args()
    verify(node_path=args.node, chrome_path=args.chrome)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
