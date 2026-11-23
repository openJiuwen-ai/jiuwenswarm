"""Build the desktop subagent with ComputerUseAgent and the hosted REPL."""

from __future__ import annotations

from functools import wraps
import logging
from pathlib import Path
from uuid import uuid4

from openjiuwen.harness import DeepAgent
from openjiuwen.harness.rails.multimodal_context_summarizer_rail import MultimodalContextSummarizerRail
from openjiuwen.harness.schema.config import SubAgentConfig
from openjiuwen.harness.tools.cua.rails import CuaProgressRail
from openjiuwen.harness.workspace.workspace import Workspace

from jiuwenswarm.common import cua_config

from .cua_defaults import build_cua_agent_card
from .prompt.cua_task_prompt import build_cua_app_launch_prompt, build_cua_repl_prompt
from .rails.cua_extension_rail import CuaExtensionRail

_LOG = logging.getLogger(__name__)
COMPUTER_USE_FACTORY_NAME = "jiuwenswarm.computer_use"


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
            "cua_snapshot_keep_last_k": cua_snapshot_keep_last_k,
            "cua_pause_on_user_input": cua_pause_on_user_input,
        },
        parallel_tool_calls=False,
    )


def _create_cua_agent(parent, spec: SubAgentConfig, subsession_id: str):
    from computer_use_agent import AgentConfig, RoutingConfig, create_agent
    from computer_use_agent.rails import ComputerUseRail
    from openjiuwen.harness.rails.context_engineer.context_processor_rail import ContextProcessorRail
    from openjiuwen.harness.rails.model_anomaly_detection_rail import ModelAnomalyDetectionRail
    from openjiuwen.harness.rails.tool_call_resilience_rail import ToolCallResilienceRail

    inherited = parent.deep_config
    workspace = spec.workspace
    if workspace is None:
        root = getattr(inherited.workspace, "root_path", inherited.workspace) or "."
        workspace = Workspace(root_path=Path(root) / "sub_agents" / subsession_id, language=inherited.language)

    agent = create_agent(
        spec.model if spec.model is not None else inherited.model,
        card=spec.agent_card,
        config=AgentConfig(routing=RoutingConfig(enabled=False), check_desktop=False),
        tool_owner_id=uuid4().hex,
        system_prompt=spec.system_prompt,
        language=spec.language,
        workspace=workspace,
        context_engine_config=None,
        max_iterations=spec.max_iterations if spec.max_iterations is not None else inherited.max_iterations,
        prompt_mode=spec.prompt_mode or inherited.prompt_mode,
        backend=spec.backend if spec.backend is not None else inherited.backend,
        invocation_log_dir=spec.invocation_log_dir or inherited.invocation_log_dir,
        enable_read_image_multimodal=(
            spec.enable_read_image_multimodal if spec.enable_read_image_multimodal is not None
            else inherited.enable_read_image_multimodal
        ),
        kv_cache_affinity_config=inherited.kv_cache_affinity_config,
        auto_create_workspace=spec.factory_kwargs.get("auto_create_workspace", True),
    )

    # The published Agent installs its stand-alone toolkit before returning.
    # Replace that surface before initialization so only the hosted REPL is offered.
    agent.ability_manager.remove_ability([tool.card.name for tool in agent.toolkit.tools])
    agent.deep_config.tools = None
    agent.strip_rails_by_type((ComputerUseRail, ContextProcessorRail))
    agent.add_rail(ModelAnomalyDetectionRail())
    agent.deep_config.enable_tool_resilience_rail = True
    agent.add_rail(ToolCallResilienceRail())
    for rail in spec.rails or ():
        fork = getattr(rail, "fork_for_agent", None)
        agent.add_rail(fork() if callable(fork) else rail)
    agent.add_rail(CuaProgressRail(""))
    agent.add_rail(MultimodalContextSummarizerRail(spec.factory_kwargs["cua_snapshot_keep_last_k"]))
    agent.register_task_resource_cleanup(agent.close)
    return parent._bind_inherited_artifact_root(agent)


def _install_cua_subagent_factory() -> None:
    # The pinned DeepAgent resolves its built-in runtime factories internally.
    original = DeepAgent.create_subagent
    if getattr(original, "_jiuwenswarm_computer_use", False):
        return

    @wraps(original)
    def create_subagent(parent, subagent_type, subsession_id, browser_capabilities=None):
        if subagent_type == "cua_agent":
            spec = parent._find_subagent_spec(subagent_type)
            if isinstance(spec, SubAgentConfig) and spec.factory_name == COMPUTER_USE_FACTORY_NAME:
                return _create_cua_agent(parent, spec, subsession_id)
        return original(parent, subagent_type, subsession_id, browser_capabilities)

    create_subagent._jiuwenswarm_computer_use = True
    DeepAgent.create_subagent = create_subagent
