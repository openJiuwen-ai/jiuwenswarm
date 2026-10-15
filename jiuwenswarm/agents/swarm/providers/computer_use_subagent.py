"""Config-sourced desktop subagent provider for all Swarm member profiles."""

from __future__ import annotations

from typing import Any

from openjiuwen.agent_teams.harness.manifest import (
    ConstructionInput,
    ElementKind,
    harness_element,
    param_field,
)

from jiuwenswarm.agents.harness.computer_use.agent import (
    build_computer_use_agent_config,
)

SWARM_COMPUTER_USE_AGENT = "swarm.computer_use_agent"


class ComputerUseAgentInput(ConstructionInput):
    max_iterations: int | None = param_field(
        default=None, description="Inner ReAct budget; None inherits the parent."
    )
    language: str = param_field(default="en", description="Subagent response language.")


@harness_element(
    kind=ElementKind.SUBAGENT,
    name=SWARM_COMPUTER_USE_AGENT,
    description="Desktop subagent with an independent persistent Python session and task-scoped desktop lease.",
    input_model=ComputerUseAgentInput,
)
def build_swarm_computer_use_agent(factory_kwargs: dict[str, Any], ctx: Any) -> Any:
    options = ComputerUseAgentInput.resolve(factory_kwargs, ctx)
    model = ctx.extras.get("_parent_model")
    if model is None:
        return None
    return build_computer_use_agent_config(
        model,
        config=ctx.config,
        workspace=getattr(ctx.workspace, "root_path", None) or "./",
        language=options.language,
        max_iterations=options.max_iterations,
    )
