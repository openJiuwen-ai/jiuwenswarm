"""Thin boundary from JiuwenSwarm resolved DTOs to agent-core's compiler."""

from __future__ import annotations

from typing import Any

from agent_gateway.common.model_selection import ResolvedModel, ResolvedModelGroup, ResolvedSelection

from jiuwenswarm.common.model_errors import MODEL_RUNTIME_UNAVAILABLE, ModelSelectionError
<<<<<<< HEAD
from jiuwenswarm.common.model_selection import ResolvedModel, ResolvedModelGroup, ResolvedSelection
from jiuwenswarm.common.reasoning_injector import inject_reasoning_params


def _core_request_config(values: dict[str, Any]) -> dict[str, Any]:
    # Translate Swarm's UI hint before Core merges request layers. Core owns
    # provider wire formats; reasoning_level is not part of its request schema.
    return inject_reasoning_params(model_client_config={}, model_config_obj=values)


def _core_model_data(model: ResolvedModel) -> dict[str, Any]:
    data = model.model_dump()
    data["request_defaults"] = _core_request_config(model.request_defaults)
    return data
||||||| e487e9a30
from jiuwenswarm.common.model_selection import ResolvedModel, ResolvedModelGroup, ResolvedSelection
=======
>>>>>>> origin/develop


def compile_model_selection(resolved: ResolvedSelection) -> Any:
    try:
        from openjiuwen.core.foundation.llm.routing.compiler import (
            compile_model_selection as compile_core,
        )
        from openjiuwen.core.foundation.llm.routing.schema import ResolvedModel as CoreModel
        from openjiuwen.core.foundation.llm.routing.schema import ResolvedModelGroup as CoreGroup
        from openjiuwen.core.foundation.llm.routing.schema import ResolvedRoute as CoreRoute
    except ImportError as exc:
        raise ModelSelectionError(
            MODEL_RUNTIME_UNAVAILABLE,
            "agent-core model-selection compiler is unavailable",
        ) from exc
    if isinstance(resolved, ResolvedModel):
        core = CoreModel(**_core_model_data(resolved))
    else:
        routes = [
            CoreRoute(
                route_id=r.route_id,
                model=CoreModel(**_core_model_data(r.model)),
                enabled=r.enabled,
                request_overrides=_core_request_config(r.request_overrides),
                tpm=r.tpm,
                rpm=r.rpm,
                timeout=r.timeout,
            )
            for r in resolved.routes
        ]
        core = CoreGroup(
            model_group_id=resolved.model_group_id,
            routes=routes,
            request_config=_core_request_config(resolved.request_config),
            routing=resolved.routing,
        )
    return compile_core(core)


def build_model_from_selection(resolved: ResolvedSelection) -> Any:
    from openjiuwen.core.foundation.llm import Model
    # Single-Agent uses a manual pool; Team compiles the original routing directly.
    if isinstance(resolved, ResolvedModelGroup):
        resolved = resolved.model_copy(update={"routing": {}})
    compiled = compile_model_selection(resolved)
    return Model(
        model_client_config=compiled.model_client_config,
        model_config=compiled.model_request_config,
    )
