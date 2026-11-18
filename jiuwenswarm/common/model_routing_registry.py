"""Resolve stable business selections into Foundation compiler input DTOs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from agent_gateway.common.model_selection import (
    ModelSelection,
    ResolvedModel,
    ResolvedModelGroup,
    ResolvedRoute,
    ResolvedSelection,
)

from jiuwenswarm.common.model_catalog import ModelCatalog
from jiuwenswarm.common.model_config_validation import is_placeholder_model_entry
from jiuwenswarm.common.reasoning_injector import build_reasoning_model_request_kwargs
from jiuwenswarm.common.model_errors import (
    MODEL_GROUP_INVALID,
    MODEL_SELECTION_DISABLED,
    MODEL_SELECTION_FORBIDDEN,
    MODEL_SELECTION_NOT_FOUND,
    ModelSelectionError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelExecutionContext:
    session_selection: ModelSelection | None = None
    spec_selection: ModelSelection | None = None
    legacy_model_name: str | None = None
    can_access: Callable[[str, str], bool] | None = None


class ModelSelectionResolver:
    def __init__(self, catalog: ModelCatalog | None = None) -> None:
        self.catalog = catalog or ModelCatalog()

    def _placeholder_model_id(self, model_id: str) -> bool:
        """Whether ``model_id`` names a ``.env`` template placeholder entry."""
        try:
            entry = self.catalog.get_model(model_id)["entry"]
        except ModelSelectionError:
            return False
        model_client_config = entry.get("model_client_config") or {}
        return is_placeholder_model_entry(model_client_config)

    def _usable_public_models(self) -> list[dict]:
        """Configured models without template placeholders (or all as fallback)."""
        models = self.catalog.list_public_models()
        usable = [m for m in models if not self._placeholder_model_id(m["model_id"])]
        return usable or models

    def _default_selection(self) -> ModelSelection:
        models = self._usable_public_models()
        explicit = [m for m in models if m.get("is_explicit_default")]
        defaults = [m for m in models if m["is_default"]]
        if explicit:
            chosen = explicit[0]
        elif defaults:
            chosen = defaults[0]
        else:
            chosen = models[0]
        return ModelSelection(type="model", id=chosen["model_id"])

    def restore(self, selection: ModelSelection) -> ModelSelection:
        """Restore a saved reference, falling back if deleted or a placeholder."""
        if self.catalog.has_selection(selection) and not self._placeholder_model_id(selection.id):
            return selection
        logger.warning(
            "saved model selection %s:%s was deleted or is a template placeholder; "
            "falling back to the current default",
            selection.type,
            selection.id,
        )
        return self.choose(None, ModelExecutionContext())

    def _reject_placeholder_selection(self, selection: ModelSelection) -> None:
        """Refuse an explicit selection that names a template placeholder."""
        if selection.type == "model" and self._placeholder_model_id(selection.id):
            raise ModelSelectionError(
                MODEL_SELECTION_DISABLED,
                f"model selection {selection.id!r} is a template placeholder",
            )

    def _legacy_model(self, name: str) -> ModelSelection | None:
        """Resolve an unambiguous legacy name, ignoring placeholders."""
        matches = []
        for model in self.catalog.list_public_models():
            if model["model_name"] != name and model["alias"] != name:
                continue
            if self._placeholder_model_id(model["model_id"]):
                continue
            matches.append(model)
        if len(matches) != 1:
            return None
        return ModelSelection(type="model", id=matches[0]["model_id"])

    def _default_group_selection(self) -> ModelSelection | None:
        """The enabled default model group, if one is configured."""
        for group in self.catalog.snapshot["groups"]:
            if isinstance(group, dict) and group.get("enabled", True) and group.get("is_default"):
                return ModelSelection(type="model_group", id=group["model_group_id"])
        return None

    def choose(self, explicit: ModelSelection | None, context: ModelExecutionContext) -> ModelSelection:
        if explicit is not None:
            self._reject_placeholder_selection(explicit)
            return explicit
        if context.session_selection is not None:
            return context.session_selection
        if context.spec_selection is not None:
            return context.spec_selection
        group = self._default_group_selection()
        if group is not None:
            return group
        if context.legacy_model_name:
            matched = self._legacy_model(context.legacy_model_name)
            if matched is not None:
                return matched
        models = self._usable_public_models()
        if not models:
            raise ModelSelectionError(MODEL_GROUP_INVALID, "no model is configured")
        return self._default_selection()

    def _model(
        self,
        model_id: str,
        context: ModelExecutionContext,
        *,
        allow_placeholder: bool = False,
    ) -> ResolvedModel:
        hit = self.catalog.get_model(model_id)
        if context.can_access and not context.can_access("model", model_id):
            raise ModelSelectionError(MODEL_SELECTION_FORBIDDEN, f"model {model_id!r} is forbidden")
        entry, source = hit["entry"], hit["source"]
        mcc, mco = entry.get("model_client_config") or {}, entry.get("model_config_obj") or {}
        model_detail = entry.get("model_detail") or {}
        if not mcc.get("model_name"):
            raise ModelSelectionError(MODEL_SELECTION_DISABLED, f"model {model_id!r} is disabled")
        if is_placeholder_model_entry(mcc) and not allow_placeholder:
            raise ModelSelectionError(
                MODEL_SELECTION_DISABLED,
                f"model {model_id!r} is a template placeholder",
            )
        excluded_client_options = {"model_name", "client_provider", "api_base", "api_key"}
        options = {key: value for key, value in mcc.items() if key not in excluded_client_options}
        defaults = build_reasoning_model_request_kwargs(
            model_client_config=mcc,
            model_config_obj=mco,
            model_name=mcc["model_name"],
        )
        defaults.pop("model", None)
        return ResolvedModel(
            model_id=model_id,
            source=source,
            model_name=mcc.get("model_name", ""),
            provider=mcc.get("client_provider", ""),
            api_base=mcc.get("api_base", ""),
            api_key=mcc.get("api_key", ""),
            endpoint_profile=entry.get("endpoint_profile") or mcc.get("endpoint_profile") or None,
            fallback_tag=model_detail.get("fallback_tag") or None,
            model_description=model_detail.get("model_description") or None,
            client_options=options,
            request_defaults=defaults,
        )

    def resolve(
        self,
        selection: ModelSelection | None,
        context: ModelExecutionContext | None = None,
    ) -> ResolvedSelection:
        context = context or ModelExecutionContext()
        selected = self.choose(selection, context)
        if selected.type == "model":
            return self._model(selected.id, context)
        if context.can_access and not context.can_access("model_group", selected.id):
            raise ModelSelectionError(MODEL_SELECTION_FORBIDDEN, f"model group {selected.id!r} is forbidden")
        group = self.catalog.get_group(selected.id)
        if not group.get("enabled", True):
            raise ModelSelectionError(MODEL_SELECTION_DISABLED, f"model group {selected.id!r} is disabled")
        group_routes = group.get("routes") or []
        if selected.route_id is not None:
            selected_routes = [route for route in group_routes if route.get("route_id") == selected.route_id]
            if not selected_routes:
                raise ModelSelectionError(
                    MODEL_SELECTION_NOT_FOUND,
                    f"unknown route_id {selected.route_id!r} in model group {selected.id!r}",
                )
            if not selected_routes[0].get("enabled", True):
                raise ModelSelectionError(
                    MODEL_SELECTION_DISABLED,
                    f"route {selected.route_id!r} in model group {selected.id!r} is disabled",
                )
            group_routes = selected_routes
        if not any(route.get("enabled", True) for route in group_routes):
            raise ModelSelectionError(MODEL_GROUP_INVALID, f"model group {selected.id!r} has no enabled routes")
        routes = [
            ResolvedRoute(
                route_id=route["route_id"],
                model=self._model(
                    route["model_id"],
                    context,
                    allow_placeholder=not route.get("enabled", True),
                ),
                enabled=route.get("enabled", True),
                request_overrides=route.get("request_overrides") or {},
                tpm=route.get("tpm"),
                rpm=route.get("rpm"),
                timeout=route.get("timeout"),
            )
            for route in group_routes
        ]
        if not routes:
            raise ModelSelectionError(MODEL_GROUP_INVALID, f"model group {selected.id!r} has no routes")
        return ResolvedModelGroup(
            model_group_id=selected.id,
            routes=routes,
            request_config=group.get("request_config") or {},
            routing=group.get("routing") or {},
        )
