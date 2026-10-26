# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Tool: forward a prompt to an external ACP agent (stdio) configured in config.yaml."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.acp.stdio_client import AcpStdioClient
from jiuwenswarm.common.config import get_config

logger = logging.getLogger(__name__)

_clients: dict[str, AcpStdioClient] = {}
_locks: dict[str, asyncio.Lock] = {}


def _profile_lock(name: str) -> asyncio.Lock:
    if name not in _locks:
        _locks[name] = asyncio.Lock()
    return _locks[name]


def _load_profile(agent: str) -> dict[str, Any]:
    cfg = get_config()
    agents = cfg.get("acp_agents")
    if not isinstance(agents, dict):
        raise ValueError("config has no acp_agents mapping")
    spec = agents.get(str(agent).strip())
    if not isinstance(spec, dict):
        raise ValueError(
            f"unknown acp_agents profile {agent!r}; define it under acp_agents in config.yaml"
        )
    return spec


async def _close_profile_unlocked(profile: str) -> None:
    client = _clients.pop(profile, None)
    if client is not None:
        try:
            await client.close()
        except Exception as exc:
            logger.warning("[acp_chat] close profile=%s failed: %s", profile, exc)


async def _get_or_create_client(profile: str, new_session: bool) -> AcpStdioClient:
    spec = _load_profile(profile)

    if new_session:
        await _close_profile_unlocked(profile)

    if profile in _clients:
        existing = _clients[profile]
        if existing.is_connected:
            return existing
        await _close_profile_unlocked(profile)

    command = str(spec.get("command") or "").strip()
    if not command:
        raise ValueError("acp_agents profile needs non-empty command")

    raw_args = spec.get("args")
    args: list[str]
    if raw_args is None:
        args = []
    elif isinstance(raw_args, list):
        args = [str(x) for x in raw_args]
    else:
        raise ValueError("acp_agents.args must be a list of strings")

    cwd = spec.get("cwd")
    cwd_s: str | None
    if cwd is None or (isinstance(cwd, str) and not cwd.strip()):
        cwd_s = None
    elif isinstance(cwd, str):
        cwd_s = cwd.strip()
    else:
        cwd_s = None

    env = spec.get("env")
    if env is not None and not isinstance(env, dict):
        raise ValueError("acp_agents.env must be a map of strings")

    client = AcpStdioClient(command, args, cwd=cwd_s, env=env if isinstance(env, dict) else None)
    await client.connect()
    _clients[profile] = client
    return client


@tool(
    name="acp_chat",
    description=(
        "Send a user message to an external ACP-compatible agent subprocess defined in "
        "config.yaml under acp_agents.<name> (command + args). Use for Codex CLI, Gemini CLI, "
        "or any other stdio ACP agent. Parameter agent is the profile key, message is the prompt. "
        "Set new_session=true to restart the subprocess and ACP session for that profile."
    ),
)
async def acp_chat(
    agent: str,
    message: str,
    new_session: bool = False,
) -> str:
    profile = (agent or "").strip()
    text = (message or "").strip()
    if not profile:
        return "[ERROR] agent (profile name) is required."
    if not text:
        return "[ERROR] message is empty."

    lock = _profile_lock(profile)
    async with lock:
        try:
            client = await _get_or_create_client(profile, new_session=new_session)
            out = await client.chat(text)
            return out or "(empty response from external ACP agent)"
        except Exception as exc:
            logger.exception("[acp_chat] profile=%s failed", profile)
            await _close_profile_unlocked(profile)
            return f"[ERROR] acp_chat failed: {exc}"
