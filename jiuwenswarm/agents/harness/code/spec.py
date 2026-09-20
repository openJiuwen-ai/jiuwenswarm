# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Convert the single-agent code config into a declarative DeepAgentSpec.

The code adapter still owns product runtime concerns (session leases, Goal,
interrupt recovery, request-scoped tools and extension mounting).  This module
owns the conversion boundary: every initialization/reload converts the current
``config.yaml`` snapshot into a ``DeepAgentSpec``. The existing code capability
builders are then invoked through registered providers during
``spec.build(context)``.

The providers are the standard ``DeepAgentSpec`` extension point for
runtime-bound capability objects. Capability selection is frozen in the Spec
parameters during conversion; providers only materialize that selection.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from openjiuwen.core.foundation.llm import Model
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.harness.schema.build_context import BuildContext
from openjiuwen.harness.schema.deep_agent_spec import (
    BuiltinToolSpec,
    DeepAgentSpec,
    ModelSpec,
    ProgressiveToolSpec,
    RailSpec,
    SubAgentSpec,
    SysOperationSpec,
    WorkspaceSpec,
    register_rail_provider,
    register_subagent_provider,
    register_tool_provider,
)

from jiuwenswarm.common.config import (
    get_progressive_tool_enabled,
    get_skill_evolution_enabled,
    is_subagent_runtime_enabled,
)
from jiuwenswarm.common.tool_ownership import register_tool

CODE_RAIL_BUNDLE = "jiuwenswarm.code.rail_bundle"
CODE_TOOL_BUNDLE = "jiuwenswarm.code.tool_bundle"
CODE_SUBAGENT_BUNDLE = "jiuwenswarm.code.subagent_bundle"


@dataclass
class CodeBuildArtifacts:
    """Live objects materialized by the code capability providers."""

    rails: list[Any] = field(default_factory=list)
    tools: list[Any] = field(default_factory=list)
    subagents: list[Any] = field(default_factory=list)


@dataclass
class CodeBuildContext(BuildContext):
    """Non-serializable runtime values used while materializing a code spec."""

    adapter: Any = None
    config_base: dict[str, Any] = field(default_factory=dict)
    react_config: dict[str, Any] = field(default_factory=dict)
    tool_owner_id: str = ""
    session_id: str = ""
    channel_id: str | None = None
    artifacts: CodeBuildArtifacts = field(default_factory=CodeBuildArtifacts)


def _require_code_context(context: BuildContext | None) -> CodeBuildContext:
    if not isinstance(context, CodeBuildContext) or context.adapter is None:
        raise TypeError("code capability providers require CodeBuildContext")
    return context


def _build_code_rail_bundle(
    params: dict[str, Any],
    context: BuildContext | None,
) -> list[Any]:
    """Resolve all configured code rails through the adapter's canonical builders."""
    ctx = _require_code_context(context)
    config_snapshot = deepcopy(ctx.config_base)
    modes = config_snapshot.setdefault("modes", {})
    if not isinstance(modes, dict):
        modes = {}
        config_snapshot["modes"] = modes
    mode_config = modes.setdefault("code", {})
    if not isinstance(mode_config, dict):
        mode_config = {}
        modes["code"] = mode_config
    mode_config["rails"] = list(params.get("configured_rails") or [])
    with ctx.adapter._code_spec_config_scope(  # pylint: disable=protected-access
        config_snapshot
    ):
        rails = ctx.adapter._build_agent_rails(  # pylint: disable=protected-access
            dict(ctx.react_config),
            config_snapshot,
            mode="code",
        )
    ctx.artifacts.rails = list(rails or [])
    return ctx.artifacts.rails


def _build_code_tool_bundle(
    params: dict[str, Any],
    context: BuildContext | None,
) -> list[Any]:
    """Resolve configured code tools and return their registered cards."""
    ctx = _require_code_context(context)
    adapter = ctx.adapter
    configured = list(params.get("configured_tools") or [])
    agent_id = str(ctx.tool_owner_id or adapter._tool_owner_id())  # pylint: disable=protected-access
    tools: list[Any] = []
    with adapter._code_spec_config_scope(  # pylint: disable=protected-access
        ctx.config_base
    ):
        for tool_name in configured:
            result = adapter._get_tool_build_func(  # pylint: disable=protected-access
                tool_name,
                agent_id,
            )
            if result is None:
                continue
            if isinstance(result, list):
                tools.extend(item for item in result if item is not None)
            else:
                tools.append(result)
    ctx.artifacts.tools = tools
    # DeepAgentSpec currently has no tool_owner_id field. Register configured
    # tool instances with the existing session-qualified owner and let the
    # standard spec carry their cards. Rail-owned tools are registered later,
    # after materialization updates the AbilityManager owner.
    for tool in tools:
        register_tool(tool, agent_id)
    return [tool.card for tool in tools if getattr(tool, "card", None) is not None]


def _build_code_subagent_bundle(
    factory_kwargs: dict[str, Any],
    context: BuildContext | None,
) -> list[Any]:
    """Resolve the configured code sub-agents using the spec-resolved parent model."""
    ctx = _require_code_context(context)
    parent_model = ctx.extras.get("_parent_model")
    if parent_model is None:
        raise RuntimeError("code sub-agent provider did not receive the parent model")
    react_snapshot = dict(ctx.react_config)
    react_snapshot["subagents"] = deepcopy(factory_kwargs.get("subagents") or {})
    react_snapshot["max_iterations"] = int(
        factory_kwargs.get(
            "max_iterations",
            react_snapshot.get("max_iterations", 15),
        )
    )
    config_snapshot = deepcopy(ctx.config_base)
    config_snapshot["react"] = react_snapshot
    with ctx.adapter._code_spec_config_scope(  # pylint: disable=protected-access
        config_snapshot
    ):
        subagents, _ = ctx.adapter._build_configured_subagents(  # pylint: disable=protected-access
            parent_model,
            react_snapshot,
            config_snapshot,
        )
    ctx.artifacts.subagents = list(subagents or [])
    return ctx.artifacts.subagents


_PROVIDERS_REGISTERED = False


def register_code_spec_providers() -> None:
    """Register the single-agent code bundle providers once per process."""
    global _PROVIDERS_REGISTERED
    if _PROVIDERS_REGISTERED:
        return
    register_rail_provider(CODE_RAIL_BUNDLE, _build_code_rail_bundle)
    register_tool_provider(CODE_TOOL_BUNDLE, _build_code_tool_bundle)
    register_subagent_provider(CODE_SUBAGENT_BUNDLE, _build_code_subagent_bundle)
    _PROVIDERS_REGISTERED = True


def _model_spec(model: Model) -> ModelSpec:
    """Copy the adapter-selected default model into the serializable spec shape."""
    return ModelSpec(
        model_client_config=model.model_client_config,
        model_request_config=model.model_config,
    )


def _sys_operation_spec(
    sys_operation: Any, sys_operation_card: Any
) -> SysOperationSpec:
    """Describe the already-resolved product SysOperation without replacing it."""
    if sys_operation is None or sys_operation_card is None:
        raise RuntimeError("code DeepAgent requires a resolved sys_operation")
    return SysOperationSpec(
        id=str(sys_operation.id),
        mode=sys_operation_card.mode,
        work_config=sys_operation_card.work_config,
        gateway_config=sys_operation_card.gateway_config,
    )


def convert_code_config_to_deep_agent_spec(
    *,
    adapter: Any,
    config_base: dict[str, Any],
    react_config: dict[str, Any],
    model: Model,
    card: AgentCard,
    system_prompt: str,
    workspace_root: str,
    project_dir: str | None,
    sys_operation: Any,
    sys_operation_card: Any,
    language: str,
    context_engine_config: Any,
    kv_cache_affinity_config: Any,
    enable_read_image_multimodal: bool | None,
    completion_timeout: float | None,
    session_id: str = "",
    channel_id: str | None = None,
) -> tuple[DeepAgentSpec, CodeBuildContext]:
    """Convert one resolved ``config.yaml`` snapshot into a standard spec."""
    register_code_spec_providers()
    config_snapshot = deepcopy(config_base)
    react_snapshot = deepcopy(react_config)
    config_snapshot["react"] = react_snapshot
    mode_config = config_snapshot.get("modes", {}).get("code", {})
    mode_config = mode_config if isinstance(mode_config, dict) else {}
    configured_rails = list(mode_config.get("rails") or [])
    configured_tools = list(mode_config.get("tools") or [])
    configured_subagents = react_snapshot.get("subagents")
    configured_subagents = (
        deepcopy(configured_subagents) if isinstance(configured_subagents, dict) else {}
    )
    context = CodeBuildContext(
        adapter=adapter,
        config_base=config_snapshot,
        react_config=react_snapshot,
        tool_owner_id=adapter._tool_owner_id(),  # pylint: disable=protected-access
        session_id=session_id,
        channel_id=channel_id,
        language=language,
        project_dir=project_dir,
    )
    spec = DeepAgentSpec(
        model=_model_spec(model),
        card=card,
        system_prompt=system_prompt,
        rails=[
            RailSpec(
                type=CODE_RAIL_BUNDLE,
                params={"configured_rails": configured_rails},
            )
        ],
        tools=[
            BuiltinToolSpec(
                type=CODE_TOOL_BUNDLE,
                params={"configured_tools": configured_tools},
            )
        ],
        subagents=[
            SubAgentSpec(
                agent_card=AgentCard(
                    id=f"{card.id}.code-subagents",
                    name="code-subagents",
                ),
                system_prompt="",
                factory_name=CODE_SUBAGENT_BUNDLE,
                factory_kwargs={
                    "subagents": configured_subagents,
                    "max_iterations": int(react_snapshot.get("max_iterations", 15)),
                },
            )
        ],
        enable_task_loop=(
            bool(react_snapshot.get("enable_task_loop", True))
            or get_skill_evolution_enabled(config_snapshot)
        ),
        enable_subagent_runtime=is_subagent_runtime_enabled(config_snapshot),
        max_iterations=int(react_snapshot.get("max_iterations", 15)),
        workspace=WorkspaceSpec(root_path=workspace_root or "./", language=language),
        sys_operation=_sys_operation_spec(sys_operation, sys_operation_card),
        language=language,
        enable_read_image_multimodal=enable_read_image_multimodal,
        # Preserve create_deep_agent()'s historical default for code mode.
        restrict_to_sandbox=True,
        context_engine_config=context_engine_config,
        kv_cache_affinity_config=kv_cache_affinity_config,
        auto_create_workspace=False,
        completion_timeout=completion_timeout,
        progressive_tool=ProgressiveToolSpec(
            enabled=get_progressive_tool_enabled(config_snapshot)
        ),
    )
    return spec, context


__all__ = [
    "CODE_RAIL_BUNDLE",
    "CODE_SUBAGENT_BUNDLE",
    "CODE_TOOL_BUNDLE",
    "CodeBuildArtifacts",
    "CodeBuildContext",
    "convert_code_config_to_deep_agent_spec",
    "register_code_spec_providers",
]
