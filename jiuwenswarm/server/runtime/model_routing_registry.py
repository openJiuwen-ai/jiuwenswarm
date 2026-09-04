"""Resolve stable business selections into Foundation compiler input DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from jiuwenswarm.common.model_catalog import ModelCatalog
from jiuwenswarm.common.model_errors import (
    MODEL_GROUP_INVALID, MODEL_SELECTION_DISABLED, MODEL_SELECTION_FORBIDDEN, ModelSelectionError,
)
from jiuwenswarm.common.model_selection import ModelSelection, ResolvedModel, ResolvedModelGroup, ResolvedRoute, ResolvedSelection


@dataclass(frozen=True)
class ModelExecutionContext:
    session_selection: ModelSelection | None = None
    spec_selection: ModelSelection | None = None
    legacy_model_name: str | None = None
    can_access: Callable[[str, str], bool] | None = None


class ModelSelectionResolver:
    def __init__(self, catalog: ModelCatalog | None = None) -> None:
        self.catalog = catalog or ModelCatalog()

    def choose(self, explicit: ModelSelection | None, context: ModelExecutionContext) -> ModelSelection:
        if explicit is not None:
            return explicit
        if context.session_selection is not None:
            return context.session_selection
        if context.spec_selection is not None:
            return context.spec_selection
        groups = [g for g in self.catalog.snapshot["groups"] if g.get("enabled", True) and g.get("is_default")]
        if groups:
            return ModelSelection(type="model_group", id=groups[0]["model_group_id"])
        if context.legacy_model_name:
            matches = [m for m in self.catalog.list_public_models() if m["model_name"] == context.legacy_model_name or m["alias"] == context.legacy_model_name]
            if len(matches) == 1:
                return ModelSelection(type="model", id=matches[0]["model_id"])
        models = [m for m in self.catalog.list_public_models() if m["is_default"]]
        if not models:
            models = self.catalog.list_public_models()
        if not models:
            raise ModelSelectionError(MODEL_GROUP_INVALID, "no model is configured")
        return ModelSelection(type="model", id=models[0]["model_id"])

    def _model(self, model_id: str, context: ModelExecutionContext) -> ResolvedModel:
        hit = self.catalog.get_model(model_id)
        if context.can_access and not context.can_access("model", model_id):
            raise ModelSelectionError(MODEL_SELECTION_FORBIDDEN, f"model {model_id!r} is forbidden")
        entry, source = hit["entry"], hit["source"]
        mcc, mco = entry.get("model_client_config") or {}, entry.get("model_config_obj") or {}
        if not mcc.get("model_name"):
            raise ModelSelectionError(MODEL_SELECTION_DISABLED, f"model {model_id!r} is disabled")
        options = {k: v for k, v in mcc.items() if k not in {"model_name", "client_provider", "api_base", "api_key"}}
        defaults = {k: v for k, v in mco.items() if k not in {"context_window", "_source"}}
        return ResolvedModel(model_id=model_id, source=source, model_name=mcc.get("model_name", ""),
            provider=mcc.get("client_provider", ""), api_base=mcc.get("api_base", ""), api_key=mcc.get("api_key", ""),
            client_options=options, request_defaults=defaults)

    def resolve(self, selection: ModelSelection | None, context: ModelExecutionContext | None = None) -> ResolvedSelection:
        context = context or ModelExecutionContext()
        selected = self.choose(selection, context)
        if selected.type == "model":
            return self._model(selected.id, context)
        if context.can_access and not context.can_access("model_group", selected.id):
            raise ModelSelectionError(MODEL_SELECTION_FORBIDDEN, f"model group {selected.id!r} is forbidden")
        group = self.catalog.get_group(selected.id)
        if not group.get("enabled", True):
            raise ModelSelectionError(MODEL_SELECTION_DISABLED, f"model group {selected.id!r} is disabled")
        routes = [ResolvedRoute(route_id=r["route_id"], model=self._model(r["model_id"], context),
            request_overrides=r.get("request_overrides") or {}, tags=r.get("tags") or [],
            tpm=r.get("tpm"), rpm=r.get("rpm"), timeout=r.get("timeout")) for r in group.get("routes") or []]
        if not routes:
            raise ModelSelectionError(MODEL_GROUP_INVALID, f"model group {selected.id!r} has no routes")
        return ResolvedModelGroup(model_group_id=selected.id, routes=routes,
            request_config=group.get("request_config") or {}, routing=group.get("routing") or {})

