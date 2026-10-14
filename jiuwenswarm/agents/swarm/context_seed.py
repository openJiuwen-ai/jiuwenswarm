# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Validate and snapshot the serializable part of a Swarm build context."""

from __future__ import annotations

import math
from typing import Any


_TEXT_FIELDS = (
    "session_id",
    "request_id",
    "user_id",
    "channel_id",
    "channel",
    "mode",
    "project_dir",
    "team_id",
    "team_ws_root",
    "task_workspace_root",
    "team_outputs_dir",
    "team_skill_visibility_path",
    "global_skills_dir",
)
_IDENTITY_FIELDS = frozenset({"session_id", "team_id"})


def _copy_json_value(value: Any, *, field: str) -> Any:
    """Copy metadata without sharing containers or admitting runtime handles.

    A JSON round trip would silently change integer mapping keys into strings
    and accept non-finite floats. Check these cases explicitly so a bad seed
    fails at the boundary, rather than later during worker startup.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [_copy_json_value(item, field=field) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError(f"swarm seed {field} must use string object keys")
        return {key: _copy_json_value(item, field=field) for key, item in value.items()}
    raise ValueError(f"swarm seed {field} must contain only JSON values")


def snapshot_swarm_seed(seed: dict[str, Any]) -> dict[str, Any]:
    """Return an independent, validated seed while ignoring unknown fields.

    Older snapshots can omit fields and newer writers can add fields. Neither
    requires a schema version change; the receiving context supplies defaults.
    Known fields, however, must retain their types. In particular, the string
    ``"false"`` must not turn worktree isolation off through ``bool(value)``.
    Runtime config and other local handles are never restored from a seed.
    """
    if not isinstance(seed, dict):
        raise ValueError("swarm seed must be an object")
    result: dict[str, Any] = {}
    for key in _TEXT_FIELDS:
        if key not in seed:
            continue
        value = seed[key]
        if value is None and key not in _IDENTITY_FIELDS:
            result[key] = None
        elif isinstance(value, str):
            result[key] = value
        else:
            raise ValueError(f"swarm seed {key} must be a string")

    if "disable_teammate_worktree" in seed:
        value = seed["disable_teammate_worktree"]
        if not isinstance(value, bool):
            raise ValueError("swarm seed disable_teammate_worktree must be a boolean")
        result["disable_teammate_worktree"] = value

    if "trusted_dirs" in seed:
        value = seed["trusted_dirs"]
        if value is None:
            result["trusted_dirs"] = None
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            result["trusted_dirs"] = list(value)
        else:
            raise ValueError("swarm seed trusted_dirs must be a list of strings")

    if "request_metadata" in seed:
        value = seed["request_metadata"]
        if value is not None and not isinstance(value, dict):
            raise ValueError("swarm seed request_metadata must be an object")
        try:
            result["request_metadata"] = _copy_json_value(
                value, field="request_metadata"
            )
        except RecursionError as exc:
            raise ValueError(
                "swarm seed request_metadata is cyclic or too deeply nested"
            ) from exc
    return result
