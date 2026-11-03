# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""External CLI settings reads shared by Front and Runtime config handlers.

Keep these projections independent of config writers and Agent dependencies so
Front can describe installed providers while the Runtime is still warming.
"""

from __future__ import annotations

import json
from typing import Any


EXTERNAL_CLI_AGENT_KINDS = ("claude", "codex")
EXTERNAL_CLI_AGENT_KEY_SUFFIXES = ("enabled", "use_builtin", "cli_path", "path_histories")
EXTERNAL_CLI_CUSTOM_AGENTS_KEY = "external_cli_custom_agents"


def external_cli_agent_key(cli_agent: str, suffix: str) -> str:
    return f"external_cli_agent_{cli_agent}_{suffix}"


def external_cli_agent_kinds() -> tuple[str, ...]:
    """Return Claude, Codex and every installed custom agent."""
    from jiuwenswarm.common.custom_agent_runtime import custom_agent_names

    return (*EXTERNAL_CLI_AGENT_KINDS, *custom_agent_names())


def custom_agents_config_value() -> str:
    """Describe the current installed-agent registry as a config-panel string."""
    from jiuwenswarm.common.custom_agent_runtime import list_custom_agents

    return json.dumps(
        [
            {
                "name": agent.name,
                "version": agent.version,
                "summary": agent.summary,
                "has_cli": bool(agent.cli_path),
            }
            for agent in list_custom_agents()
        ],
        ensure_ascii=False,
    )


def _uploaded_cli_path(cli_agent: str) -> str:
    """Return the CLI executable uploaded with a custom agent, if any."""
    if cli_agent in EXTERNAL_CLI_AGENT_KINDS:
        return ""
    from jiuwenswarm.common.custom_agent_runtime import get_custom_agent

    agent = get_custom_agent(cli_agent)
    return agent.cli_path if agent is not None else ""


def flatten_external_cli_agents_for_config_panel(raw: dict[str, Any]) -> dict[str, str]:
    """Project default-team selections without loading or detecting providers."""
    agents: Any = raw
    for segment in ("modes", "team", "jiuwen_team", "external_cli_agents"):
        agents = agents.get(segment) if isinstance(agents, dict) else None
    histories: Any = raw
    for segment in ("modes", "team", "jiuwen_team", "external_cli_path_histories"):
        histories = histories.get(segment) if isinstance(histories, dict) else None
    configured: dict[str, dict[str, str]] = {}
    if isinstance(agents, list):
        for item in agents:
            if isinstance(item, str):
                cli_agent = item.strip()
                cli_path = ""
            elif isinstance(item, dict):
                cli_agent = str(item.get("cli_agent") or "").strip()
                cli_path = str(item.get("cli_path") or item.get("codex_bin") or "").strip()
            else:
                continue
            if cli_agent:
                configured[cli_agent] = {"cli_path": cli_path}
    flat: dict[str, str] = {}
    for cli_agent in external_cli_agent_kinds():
        entry = configured.get(cli_agent)
        cli_path = entry.get("cli_path", "") if entry is not None else ""
        # The uploaded executable is the custom agent's built-in CLI choice.
        builtin = entry is not None and cli_path in {"", _uploaded_cli_path(cli_agent)}
        history = histories.get(cli_agent) if isinstance(histories, dict) else None
        # Keep the first occurrence so a hand-edited config cannot show
        # duplicate dropdown options.
        history_paths: list[str] = []
        if isinstance(history, list):
            for path in history:
                if isinstance(path, str) and path.strip() and path not in history_paths:
                    history_paths.append(path)
        flat[external_cli_agent_key(cli_agent, "enabled")] = "true" if entry is not None else "false"
        flat[external_cli_agent_key(cli_agent, "use_builtin")] = "true" if builtin else "false"
        flat[external_cli_agent_key(cli_agent, "cli_path")] = "" if builtin else cli_path
        flat[external_cli_agent_key(cli_agent, "path_histories")] = json.dumps(history_paths, ensure_ascii=False)
    return flat
