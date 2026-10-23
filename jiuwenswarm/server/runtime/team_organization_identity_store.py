# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persist the AgentGroup identity of dynamically invited organization Teams."""

import json
import os
import tempfile
from pathlib import Path

from openjiuwen.agent_teams.paths import team_home

from jiuwenswarm.server.runtime.team_binding_store import validate_team_name


def _identity_path(team_id: str) -> Path:
    return team_home(validate_team_name(team_id)) / "team-workspace" / ".team-meta" / "organization-identity.json"


def save_organization_team_identity(
    team_id: str, *, agent_group_name: str, display_name: str
) -> None:
    path = _identity_path(team_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"team_id": team_id, "agent_group_name": agent_group_name, "display_name": display_name}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_organization_team_identity(team_id: str) -> dict[str, str]:
    try:
        payload = json.loads(_identity_path(team_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("team_id") != team_id:
        return {}
    identity: dict[str, str] = {}
    for key in ("agent_group_name", "display_name"):
        value = payload.get(key)
        if not isinstance(value, str):
            continue
        value = value.strip()
        if value:
            identity[key] = value
    return identity
