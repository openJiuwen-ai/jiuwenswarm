#!/usr/bin/env python3
"""Command-line wrapper for the Workswarm REA base setup helpers."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from jiuwenswarm.common.rea_setup import (
    REA_MCP_NAME,
    install_rea_base,
    mark_rea_mcp_state,
    rea_mcp_record,
    reset_rea_setup,
    scan_rea_setup,
)


async def _verify_mcp(workspace: Path | None) -> int:
    from jiuwenswarm.common.mcp_config import fetch_mcp_tools_via_temp_connection

    record = rea_mcp_record(workspace)
    if not record:
        return 0
    tools = await fetch_mcp_tools_via_temp_connection({"name": REA_MCP_NAME, **record})
    if tools:
        mark_rea_mcp_state("connected", workspace, tool_count=len(tools))
    return len(tools)


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect or install the base REA runtime")
    parser.add_argument("action", choices=("status", "install", "reset"))
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.action == "install":
        def progress(
            stage: str,
            percent: float,
            message: str,
            details: dict | None = None,
        ) -> None:
            if not args.json:
                suffix = ""
                if details:
                    suffix = f" {details}"
                print(f"[{percent:5.1f}%] {stage}: {message}{suffix}", file=sys.stderr)

        installed = install_rea_base(args.workspace, progress_callback=progress)
        tool_count = asyncio.run(_verify_mcp(args.workspace))
        payload = scan_rea_setup(args.workspace)
        payload["verified_tool_count"] = tool_count
        payload["enhanced_install_results"] = installed.get("enhanced_install_results", [])
    elif args.action == "reset":
        payload = reset_rea_setup(args.workspace)
    else:
        payload = scan_rea_setup(args.workspace)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        state = "READY" if payload.get("ready") else "NOT READY"
        print(f"REA {payload.get('rea_version')} · {state}")
        for item in payload.get("base", []):
            marker = "OK" if item.get("ok") else "MISSING"
            print(f"[{marker}] {item.get('label')}: {item.get('detail')}")
    return 0 if args.action != "install" or payload.get("ready") else 1


if __name__ == "__main__":
    raise SystemExit(main())
