# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""CLI smoke test for external ACP agents (stdio), sharing config with acp_chat tool."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from jiuwenswarm.acp.stdio_client import AcpStdioClient
from jiuwenswarm.common.config import get_config

logger = logging.getLogger(__name__)


def _get_spec(agent: str) -> dict | None:
    cfg = get_config()
    agents = cfg.get("acp_agents")
    if not isinstance(agents, dict):
        logger.error("[ERROR] config has no acp_agents.")
        return None
    spec = agents.get(agent.strip())
    if not isinstance(spec, dict):
        logger.error("[ERROR] unknown acp_agents profile %r.", agent)
        return None
    return spec


async def _run_once(agent: str, message: str) -> int:
    spec = _get_spec(agent)
    if spec is None:
        return 2
    command = str(spec.get("command") or "").strip()
    if not command:
        logger.error("[ERROR] profile needs command.")
        return 2
    raw_args = spec.get("args")
    args = [str(x) for x in raw_args] if isinstance(raw_args, list) else []
    cwd = spec.get("cwd")
    cwd_s: str | None = None
    if isinstance(cwd, str) and cwd.strip():
        cwd_s = cwd.strip()
    env = spec.get("env") if isinstance(spec.get("env"), dict) else None

    client = AcpStdioClient(command, args, cwd=cwd_s, env=env)
    try:
        await client.connect()
        out = await client.chat(message)
        logger.info("%s", out or "(empty response)")
        return 0
    except Exception as exc:
        logger.error("[ERROR] ACP session failed: %s", exc)
        return 1
    finally:
        _close_timeout = float(os.getenv("ACP_CLI_CLOSE_TIMEOUT_S", "30"))
        try:
            await asyncio.wait_for(asyncio.shield(client.close()), timeout=_close_timeout)
        except Exception as exc:
            logger.warning("Failed to close ACP client: %s", exc)


async def _main_async() -> int:
    p = argparse.ArgumentParser(description="Smoke-test an acp_agents profile (one-shot subprocess).")
    p.add_argument("agent", help="Profile key under acp_agents in config.yaml")
    p.add_argument("message", nargs="?", default="Hello", help="Prompt text")
    args = p.parse_args()
    return await _run_once(args.agent, args.message)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    if os.environ.get("JIUWENSWARM_SKIP_DOTENV", "").strip() != "1":
        try:
            from dotenv import load_dotenv
            from jiuwenswarm.common.utils import get_env_file

            load_dotenv(dotenv_path=get_env_file(), override=False)
        except Exception as exc:
            logger.debug("Skipping dotenv load: %s", exc)
    try:
        sys.exit(asyncio.run(_main_async()))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
