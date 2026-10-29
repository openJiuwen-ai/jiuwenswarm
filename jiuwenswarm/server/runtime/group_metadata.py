# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Group identity and persisted metadata, independent of the optional Team SDK."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from agent_gateway.common.protocol_ids import validate_session_id
from jiuwenswarm.common.utils import get_agent_sessions_dir


GROUP_METHODS = frozenset(
    {
        "group.create",
        "group.delete",
        "group.list",
        "group.get",
        "group.update",
        "group.member.add",
        "group.history.get",
        "group.execution.list",
        "group.execution.get",
        "group.file.rename",
        "group.file.delete",
        "group.history.clear",
    }
)


class GroupError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def group_path(group_id: str) -> Path:
    return get_agent_sessions_dir() / validate_session_id(group_id) / "group.json"


def normalize_group_member(member: dict) -> dict:
    """Read legacy roster fields without changing the stored or requested object."""
    if not isinstance(member, dict):
        raise GroupError("BAD_REQUEST", "invalid group member fields")
    result = copy.deepcopy(member)
    result["kind"] = {"expert": "agent_template", "expert_team": "agent_group"}.get(
        result.get("kind"), result.get("kind")
    )
    if "definition_id" in result:
        result.setdefault("id", result.pop("definition_id"))
    return result


def read_group(group_id: str, *, path: Path | None = None) -> dict | None:
    path = path if path is not None else group_path(group_id)
    if not path.is_file():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GroupError("GROUP_CORRUPT", "invalid persisted group") from exc
    required = {
        "group_id",
        "session_id",
        "team_name",
        "name",
        "description",
        "members",
        "created_at",
        "updated_at",
        "status",
        "user_id",
        "channel_id",
        "compiled",
        "receipts",
        "mode",
    }
    if (
        not isinstance(state, dict)
        or state.get("group_id") != group_id
        or not required <= state.keys()
    ):
        raise GroupError("GROUP_CORRUPT", "invalid persisted group")
    state.setdefault("configs", {})
    state.setdefault("avatar", "")
    state["members"] = [normalize_group_member(member) for member in state["members"]]
    state["receipts"] = {
        key: normalize_group_member(member) for key, member in state["receipts"].items()
    }
    retired_tools = {"swarm.group_adjust_task", "swarm.group_submit_task"}
    for member in state["compiled"].values():
        template = member.get("template")
        if template is not None:
            template["tools"] = [
                tool for tool in template.get("tools", []) if tool.get("type") not in retired_tools
            ]
    return state
