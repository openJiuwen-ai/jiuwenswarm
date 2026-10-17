"""Read operator approval overrides without exposing permission storage layers."""

from typing import Any


def operator_approval_overrides(session_id: str | None = None) -> list[Any]:
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        load_global_permissions,
        load_session_permissions,
        load_user_permissions,
    )

    entries: list[Any] = []
    for layer in (
        load_global_permissions(),
        load_user_permissions(),
        load_session_permissions(session_id),
    ):
        raw = layer.get("approval_overrides") if isinstance(layer, dict) else None
        if isinstance(raw, list):
            entries.extend(raw)
    return entries
