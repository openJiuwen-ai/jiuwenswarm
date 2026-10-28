# Gateway 侧副本，源文件 jiuwenswarm/common/config_panel/external_cli_read.py；改动请同步两侧。
# 本副本仅保留 gateway 实际引用的符号；同步母仓时请勿整体覆盖。
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""External CLI settings reads shared by Front and Runtime config handlers.

Keep these projections independent of config writers and Agent dependencies so
Front can describe installed providers while the Runtime is still warming.
"""

from __future__ import annotations



EXTERNAL_CLI_AGENT_KINDS = ("claude", "codex")


def external_cli_agent_kinds() -> tuple[str, ...]:
    """Return Claude, Codex and every installed custom agent."""
    from gateway.common.custom_agent_runtime import custom_agent_names

    return (*EXTERNAL_CLI_AGENT_KINDS, *custom_agent_names())


def _uploaded_cli_path(cli_agent: str) -> str:
    """Return the CLI executable uploaded with a custom agent, if any."""
    if cli_agent in EXTERNAL_CLI_AGENT_KINDS:
        return ""
    from gateway.common.custom_agent_runtime import get_custom_agent

    agent = get_custom_agent(cli_agent)
    return agent.cli_path if agent is not None else ""
