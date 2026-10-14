"""Ordinary SubAgentConfig; no special factory in agent-core is required."""

from __future__ import annotations

from typing import Any

from openjiuwen.core.single_agent import AgentCard
from openjiuwen.harness.schema.config import SubAgentConfig

from .config import ComputerUseSettings
from .rail import ComputerUseRail

COMPUTER_USE_AGENT = "computer_use_agent"
SYSTEM_PROMPT = """You are a desktop computer-use subagent. Complete the delegated task
using the Computer Use Skill and its Python Computer → App → Element API.
Choose actions from actual accessibility observations and screenshots. Read explicitly
after actions when their effects matter. A dispatched receipt is not task completion.
Respect the specified application, document, regions and desktop mode. Keep desktop
control within this task. Do not bypass the SDK's routing, focus or input checks.
The Python environment is persistent during this invocation and fresh on each new invocation.
Observations use full presentation in this integration; no retained-history diff is enabled.
Application experience/learning and model delegation extensions are disabled here.
Do not retry actions whose effects are uncertain; observe and report unresolved effects.
Return a concise answer with what was accomplished, UI verification, output locations,
and any remaining work. Preserve useful partial results. Cleanup is managed by the host.
"""


def build_computer_use_agent_config(
    model: Any,
    *,
    config: dict[str, Any] | None = None,
    workspace: Any = "./",
    language: str = "en",
    max_iterations: int | None = None,
) -> SubAgentConfig:
    return SubAgentConfig(
        agent_card=AgentCard(
            id="jiuwenswarm.computer_use_agent",
            name=COMPUTER_USE_AGENT,
            description="Operate desktop applications through accessibility and screenshots; "
            "use for GUI tasks, editing documents, and interacting with app dialogs.",
        ),
        system_prompt=SYSTEM_PROMPT,
        model=model,
        workspace=workspace,
        language=language,
        rails=[ComputerUseRail(ComputerUseSettings.from_config(config))],
        parallel_tool_calls=False,
        max_iterations=max_iterations,
        factory_kwargs={
            "enable_sys_operation": False,
            "enable_skill_discovery": False,
            "enable_tool_resilience_rail": False,
            "auto_create_workspace": False,
        },
    )
