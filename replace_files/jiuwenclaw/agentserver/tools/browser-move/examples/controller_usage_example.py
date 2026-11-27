#!/usr/bin/env python
# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Example usage of the playwright_runtime custom action registry.

Shows how to:
  1. Register your own action handlers.
  2. Call run_action directly (e.g. from tests or scripts).
  3. Use the MCP tools browser_custom_action and browser_list_custom_actions
     when the runtime MCP server is running.

Run from repo root:
  uv run examples/controller_usage_example.py
  uv run python src/playwright_runtime/controller.py   # minimal demo
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict

# Allow importing playwright_runtime when run from repo root
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


async def example_register_and_call() -> None:
    """Example: register a custom action and invoke it via the custom action API."""
    from playwright_runtime.controller import (
        list_actions,
        register_action,
        register_example_actions,
        run_action,
    )

    # Optional: register built-in examples (ping, echo)
    register_example_actions()

    # Register your own action
    async def my_uppercase(
        session_id: str = "",
        request_id: str = "",
        text: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        return {"uppercased": (text or "").upper(), "session_id": session_id}

    register_action("uppercase", my_uppercase)

    print("Registered actions:", list_actions())

    # Call built-in examples
    out = await run_action("ping", session_id="s1", request_id="r1")
    print("run_action('ping', ...):", json.dumps(out, indent=2))

    out = await run_action("echo", session_id="s1", text="hello controller")
    print("run_action('echo', text='hello controller'):", json.dumps(out, indent=2))

    # Call your custom action
    out = await run_action("uppercase", session_id="s1", text="hello")
    print("run_action('uppercase', text='hello'):", json.dumps(out, indent=2))

    # Unknown action
    out = await run_action("nonexistent")
    print("run_action('nonexistent'):", json.dumps(out, indent=2))


async def example_via_mcp_tools() -> None:
    """
    When the Playwright runtime MCP server is running, clients can call:

      - browser_list_custom_actions()  -> {"ok": true, "actions": ["ping", "echo", ...]}
      - browser_custom_action(action="ping", session_id="...", request_id="...")
      - browser_custom_action(action="echo", params={"text": "hello"})

    The server registers register_example_actions() in its lifespan, so
    "ping" and "echo" are available by default. Add your own in a startup
    hook or by calling register_action() before the server starts.
    """
    print(
        "To use from an MCP client: call browser_custom_action with action='ping' or 'echo', "
        "or register your own actions and call browser_list_custom_actions to see them."
    )


def main() -> None:
    asyncio.run(example_register_and_call())
    print()
    asyncio.run(example_via_mcp_tools())


if __name__ == "__main__":
    main()
