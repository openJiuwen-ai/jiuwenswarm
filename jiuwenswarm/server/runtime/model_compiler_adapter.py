"""Thin boundary from JiuwenSwarm resolved DTOs to agent-core's compiler."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.common.model_errors import MODEL_RUNTIME_UNAVAILABLE, ModelSelectionError
from jiuwenswarm.common.model_selection import ResolvedModel, ResolvedModelGroup, ResolvedSelection


def compile_model_selection(resolved: ResolvedSelection) -> tuple[Any, Any]:
    try:
        from openjiuwen.core.foundation.llm.routing.compiler import compile_model_selection as compile_core
        from openjiuwen.core.foundation.llm.routing.schema import ResolvedModel as CoreModel
        from openjiuwen.core.foundation.llm.routing.schema import ResolvedModelGroup as CoreGroup
        from openjiuwen.core.foundation.llm.routing.schema import ResolvedRoute as CoreRoute
    except (ImportError, ModuleNotFoundError) as exc:
        raise ModelSelectionError(MODEL_RUNTIME_UNAVAILABLE, "agent-core model-selection compiler is unavailable") from exc
    if isinstance(resolved, ResolvedModel):
        core = CoreModel(**resolved.model_dump())
    else:
        routes = [CoreRoute(route_id=r.route_id, model=CoreModel(**r.model.model_dump()),
            request_overrides=r.request_overrides, tags=r.tags, tpm=r.tpm, rpm=r.rpm, timeout=r.timeout)
            for r in resolved.routes]
        core = CoreGroup(model_group_id=resolved.model_group_id, routes=routes,
            request_config=resolved.request_config, routing=resolved.routing)
    return compile_core(core)


def build_model_from_selection(resolved: ResolvedSelection) -> Any:
    from openjiuwen.core.foundation.llm import Model
    client, request = compile_model_selection(resolved)
    return Model(model_client_config=client, model_config=request)

