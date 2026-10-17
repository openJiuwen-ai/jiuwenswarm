# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Resolve the model a diagnosis reuses from the diagnosed session.

A single-Agent session uses its persisted session selection; a Team session
uses the Team binding selection first (the Leader's model) and falls back to
the session selection. When nothing can be resolved the host answers
``MODEL_UNAVAILABLE`` instead of starting the diagnosis.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.diagnosis.models import DiagnosisHostError, ModelInfo

logger = logging.getLogger(__name__)


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
    except Exception as exc:  # noqa: BLE001
        logger.warning("[diagnosis] model resolution failed: session_id=%s error=%s", session_id, exc)
        raise DiagnosisHostError(
            "MODEL_UNAVAILABLE",
            "no model is available for this session",
            503,
        ) from exc
    return ResolvedDiagnosisModel(info=_model_info(resolved), resolved=resolved)


__all__ = ["ResolvedDiagnosisModel", "resolve_diagnosis_model"]
