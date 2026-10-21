# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Resolve the model a diagnosis reuses from the diagnosed session.

A single-Agent session uses its persisted session selection; a Team session
uses the Team binding selection first (the Leader's model) and falls back to
the session selection. When nothing can be resolved the host answers
``MODEL_UNAVAILABLE`` instead of starting the diagnosis.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.common.model_selection import ResolvedModel, ResolvedModelGroup
from jiuwenswarm.common.reasoning_injector import build_reasoning_model_request_kwargs
from jiuwenswarm.diagnosis.models import DiagnosisHostError, ModelInfo

logger = logging.getLogger(__name__)


def _sanitize_model_request_defaults(model: ResolvedModel) -> ResolvedModel:
    """Map internal request hints (``reasoning_level``) onto core config.

    ``ModelSelectionResolver`` copies ``model_config_obj`` verbatim into
    ``request_defaults``; the core compiler forwards unknown request keys to
    the vendor SDK, which rejects ``reasoning_level``. The chat path builds
    models through ``build_model_from_entry``, which cleans the same keys via
    ``reasoning_injector`` first; diagnosis applies the identical mapping here
    (same treatment as the jiuwenswarm-new attribution host) so it behaves
    like the chat path without touching the shared resolver.
    """
    cleaned = build_reasoning_model_request_kwargs(
        model_client_config={
            "model_name": model.model_name,
            "client_provider": model.provider,
            "api_base": model.api_base,
            "api_key": model.api_key,
        },
        model_config_obj=model.request_defaults,
        model_name=model.model_name,
    )
    return model.model_copy(update={"request_defaults": cleaned})


def _sanitize_extra_request_config(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Clean route overrides / group request config with the same mapping."""
    return build_reasoning_model_request_kwargs(
        model_client_config={},
        model_config_obj=dict(value or {}),
        model_name="",
    )


def _sanitize_group(group: ResolvedModelGroup) -> ResolvedModelGroup:
    routes = [
        route.model_copy(
            update={
                "model": _sanitize_model_request_defaults(route.model),
                "request_overrides": _sanitize_extra_request_config(route.request_overrides),
            },
        )
        for route in group.routes
    ]
    updates: dict[str, Any] = {"routes": routes}
    if group.request_config:
        updates["request_config"] = _sanitize_extra_request_config(group.request_config)
    return group.model_copy(update=updates)


def _sanitize_resolved_selection(resolved: Any) -> Any:
    """Strip internal request hints from a resolved selection before compiling."""
    if isinstance(resolved, ResolvedModel):
        return _sanitize_model_request_defaults(resolved)
    if isinstance(resolved, ResolvedModelGroup):
        return _sanitize_group(resolved)
    return resolved


@dataclass(slots=True)
class ResolvedDiagnosisModel:
    """A resolved model selection that can be compiled on demand."""

    info: ModelInfo
    resolved: Any

    def build(self) -> Any:
        """Compile the selection into a model client object.

        Returns:
            An ``openjiuwen`` ``Model``; the caller owns its lifecycle.
        """
        from jiuwenswarm.server.runtime.model_compiler_adapter import build_model_from_selection

        return build_model_from_selection(self.resolved)


def _team_selection(team_name: str) -> Any:
    from jiuwenswarm.common.model_selection import ModelSelection
    from jiuwenswarm.server.runtime.team_binding_store import get_team_binding_store

    raw = get_team_binding_store().get_team_selection(team_name)
    return ModelSelection.model_validate(raw) if isinstance(raw, dict) else None


def _model_info(resolved: Any) -> ModelInfo:
    group_id = getattr(resolved, "model_group_id", None)
    if group_id:
        routes = getattr(resolved, "routes", None) or []
        first_name = getattr(getattr(routes[0], "model", None), "model_name", "") if routes else ""
        return ModelInfo(type="model_group", id=str(group_id), display_name=first_name or str(group_id))
    return ModelInfo(
        type="model",
        id=str(getattr(resolved, "model_id", "")),
        display_name=str(getattr(resolved, "model_name", "") or "") or None,
    )


def resolve_diagnosis_model(
    session_id: str,
    *,
    team_name: str | None = None,
) -> ResolvedDiagnosisModel:
    """Resolve the model selection a diagnosis of ``session_id`` reuses.

    Args:
        session_id: Diagnosed session.
        team_name: Team name for Team sessions, whose binding selection wins.

    Returns:
        The resolved selection and its credential-free description.

    Raises:
        DiagnosisHostError: ``MODEL_UNAVAILABLE`` when no model can be resolved.
    """
    try:
        from jiuwenswarm.server.runtime.model_routing_registry import ModelSelectionResolver
        from jiuwenswarm.server.runtime.session.model_selection_store import (
            get_session_model_selection,
        )

        selection = None
        if team_name:
            selection = _team_selection(team_name)
        if selection is None:
            selection = get_session_model_selection(session_id)
        resolved = ModelSelectionResolver().resolve(selection)
        resolved = _sanitize_resolved_selection(resolved)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[diagnosis] model resolution failed: session_id=%s error=%s", session_id, exc)
        raise DiagnosisHostError(
            "MODEL_UNAVAILABLE",
            "no model is available for this session",
            503,
        ) from exc
    return ResolvedDiagnosisModel(info=_model_info(resolved), resolved=resolved)


__all__ = ["ResolvedDiagnosisModel", "resolve_diagnosis_model"]
