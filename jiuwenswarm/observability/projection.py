# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Central normalization of OTLP records for trajectory navigation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class TrajectoryScope:
    """Stable navigation hints projected from one immutable OTLP record."""

    team_id: str | None = None
    team_name: str | None = None


_ATTRIBUTE_ALIASES: dict[str, tuple[str, ...]] = {
    "team_id": (
        "openjiuwen.team.id",
        "agentteam.team.id",
    ),
    "team_name": (
        "openjiuwen.team.name",
        "agentteam.team.name",
        "openjiuwen.team.id",
        "agentteam.team.id",
    ),
}


def project_trajectory_scope(otlp: Mapping[str, Any]) -> TrajectoryScope:
    """Project Team identity hints without rewriting OTLP."""
    attributes: dict[str, Any] = {}
    resource_spans = otlp.get("resourceSpans")
    if not isinstance(resource_spans, list):
        return TrajectoryScope()
    for resource_span in resource_spans:
        if not isinstance(resource_span, Mapping):
            continue
        resource = resource_span.get("resource")
        if isinstance(resource, Mapping):
            attributes.update(_decode_attributes(resource.get("attributes")))
        scope_spans = resource_span.get("scopeSpans")
        if not isinstance(scope_spans, list):
            continue
        for scope_span in scope_spans:
            if not isinstance(scope_span, Mapping):
                continue
            spans = scope_span.get("spans")
            if not isinstance(spans, list):
                continue
            for span in spans:
                if not isinstance(span, Mapping):
                    continue
                attributes.update(_decode_attributes(span.get("attributes")))

    return TrajectoryScope(
        team_id=_as_text(attributes.get("openjiuwen.team.id")) or _as_text(attributes.get("agentteam.team.id")),
        team_name=_as_text(attributes.get("openjiuwen.team.name")) or _as_text(attributes.get("agentteam.team.name")),
    )


def scope_matches(
    scope: TrajectoryScope,
    *,
    team_id: str | None = None,
    member_id: str | None = None,
    execution_subject_id: str | None = None,
) -> bool:
    """Return whether a normalized record belongs to all requested scopes."""
    del member_id, execution_subject_id
    if team_id is not None and team_id not in {scope.team_id, scope.team_name}:
        return False
    return True


def _decode_attributes(raw_attributes: Any) -> dict[str, Any]:
    if not isinstance(raw_attributes, list):
        return {}
    decoded: dict[str, Any] = {}
    for item in raw_attributes:
        if not isinstance(item, Mapping):
            continue
        key = _as_text(item.get("key"))
        value = item.get("value")
        if key is not None and isinstance(value, Mapping):
            decoded[key] = _decode_otlp_value(value)
    return decoded


def _decode_otlp_value(value: Mapping[str, Any]) -> Any:
    for key in (
        "stringValue",
        "intValue",
        "doubleValue",
        "boolValue",
        "bytesValue",
    ):
        if key in value:
            return value[key]
    return None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


__all__ = [
    "TrajectoryScope",
    "project_trajectory_scope",
    "scope_matches",
]
