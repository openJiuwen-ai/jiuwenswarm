"""Build the desktop subagent with ComputerUseAgent and the hosted REPL."""

from __future__ import annotations

from functools import wraps
import logging
from uuid import uuid4

from openjiuwen.harness.rails.multimodal_context_summarizer_rail import MultimodalContextSummarizerRail
from openjiuwen.harness.schema.config import SubAgentConfig
from openjiuwen.harness.subagents import cua_agent as core_cua_agent
from openjiuwen.harness.tools.cua.rails import CuaProgressRail

from jiuwenswarm.common import cua_config

from .cua_defaults import build_cua_agent_card
from .prompt.cua_task_prompt import build_cua_app_launch_prompt, build_cua_repl_prompt
from .rails.cua_extension_rail import CuaExtensionRail

_LOG = logging.getLogger(__name__)
COMPUTER_USE_FACTORY_NAME = core_cua_agent.CUA_AGENT_FACTORY_NAME
_FACTORY_HOOK_MARKER = "_jiuwenswarm_computer_use"


def build_cua_agent_config(
    model,
    *,
    card=None,
    workspace=None,
    sys_operation=None,
    language="cn",
    max_iterations=100,
    cua_snapshot_keep_last_k=3,
    cua_pause_on_user_input=False,
    rails=None,
) -> SubAgentConfig | None:
    """Register the desktop subagent only when its REPL backend is available."""
    if not cua_config.is_cua_repl_available():
        return None

    prompt = f"{build_cua_app_launch_prompt(language)}\n\n{build_cua_repl_prompt(language)}"
    try:
        from computer_use_mcp.instructions import DESCRIPTION
    except ImportError as error:
        _LOG.warning("Computer Use SDK instructions are unavailable: %s", error)
    else:
        prompt += f"\n\n{DESCRIPTION}"

    _install_cua_subagent_factory()
    return SubAgentConfig(
        agent_card=card if card is not None else build_cua_agent_card(language),
        system_prompt=prompt,
        model=model,
        workspace=workspace,
        sys_operation=sys_operation,
        language=language,
        max_iterations=max_iterations,
        rails=[
            *(rails or []),
            CuaExtensionRail(pause_on_user_input=cua_pause_on_user_input),
        ],
        factory_name=COMPUTER_USE_FACTORY_NAME,
        factory_kwargs={
            "jiuwenswarm_hosted_repl": True,
            "cua_snapshot_keep_last_k": cua_snapshot_keep_last_k,
            "cua_pause_on_user_input": cua_pause_on_user_input,
        },
        parallel_tool_calls=False,
    )


def _create_cua_agent(
    model,
    *,
    rails=None,
    cua_snapshot_keep_last_k=3,
    auto_create_workspace=True,
    **agent_options,
):
    from computer_use_agent import AgentConfig, RoutingConfig, create_agent
    from computer_use_agent.rails import ComputerUseRail
    from openjiuwen.harness.rails.context_engineer.context_processor_rail import ContextProcessorRail
    from openjiuwen.harness.rails.model_anomaly_detection_rail import ModelAnomalyDetectionRail
    from openjiuwen.harness.rails.tool_call_resilience_rail import ToolCallResilienceRail

    agent = create_agent(
        model,
        config=AgentConfig(routing=RoutingConfig(enabled=False), check_desktop=False),
        tool_owner_id=uuid4().hex,
        context_engine_config=None,
        auto_create_workspace=auto_create_workspace,
        **agent_options,
    )

    # The published Agent installs its stand-alone toolkit before returning.
    # Replace that surface before initialization so only the hosted REPL is offered.
    agent.ability_manager.remove_ability([tool.card.name for tool in agent.toolkit.tools])
    agent.deep_config.tools = None
    agent.strip_rails_by_type((ComputerUseRail, ContextProcessorRail))
    agent.add_rail(ModelAnomalyDetectionRail())
    agent.deep_config.enable_tool_resilience_rail = True
    agent.add_rail(ToolCallResilienceRail())
    for rail in rails or ():
        agent.add_rail(rail)
    agent.add_rail(CuaProgressRail(""))
    agent.add_rail(MultimodalContextSummarizerRail(cua_snapshot_keep_last_k))
    agent.register_task_resource_cleanup(agent.close)
    return agent


def _install_cua_subagent_factory() -> None:
    # Keep DeepAgent's dispatch, artifact binding and accounting hooks in place.
    original = core_cua_agent.create_cua_agent
    if getattr(original, _FACTORY_HOOK_MARKER, False):
        return

    @wraps(original)
    def create_cua_agent(*args, jiuwenswarm_hosted_repl=False, **kwargs):
        if jiuwenswarm_hosted_repl:
            # The hosted extension rail already holds the pause preference.
            kwargs.pop("cua_pause_on_user_input", None)
            return _create_cua_agent(*args, **kwargs)
        return original(*args, **kwargs)

    setattr(create_cua_agent, _FACTORY_HOOK_MARKER, True)
    core_cua_agent.create_cua_agent = create_cua_agent
