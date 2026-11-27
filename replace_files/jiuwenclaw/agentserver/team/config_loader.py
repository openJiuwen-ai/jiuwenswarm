# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Team configuration loader."""

from __future__ import annotations

import logging
from copy import deepcopy
from pathlib import Path
from typing import Any

from openjiuwen.agent_teams.paths import get_agent_teams_home

from jiuwenclaw.config import get_config
from jiuwenclaw.utils import get_agent_skills_dir

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ITERATIONS = 200
_DEFAULT_COMPLETION_TIMEOUT = 600.0
_DEFAULT_AGENT_WORKSPACE = {"stable_base": True}


def resolve_team_sqlite_db_path(config_base: dict[str, Any] | None = None) -> Path | None:
    """Resolve the team sqlite database path using openjiuwen semantics."""
    if config_base is None:
        config_base = get_config()

    team_raw = config_base.get("team", {})
    if not isinstance(team_raw, dict):
        return None

    storage_raw = team_raw.get("storage", {})
    if not isinstance(storage_raw, dict):
        return None

    storage_type = str(storage_raw.get("type", "")).strip().lower()
    if storage_type and storage_type != "sqlite":
        return None

    storage_params = storage_raw.get("params", {})
    if not isinstance(storage_params, dict):
        storage_params = {}

    conn_str = str(storage_params.get("connection_string", "")).strip()
    if not conn_str:
        return get_agent_teams_home() / "team.db"

    db_path = Path(conn_str).expanduser()
    if db_path.is_absolute():
        return db_path

    return get_agent_teams_home() / conn_str


def _build_default_model_dict(config_base: dict[str, Any]) -> dict[str, Any]:
    model_config = config_base.get("models", {}).get("default", {})
    model_client_config = model_config.get("model_client_config", {})
    model_request_config = dict(model_config.get("model_config_obj", {}))

    model_name = model_client_config.get("model_name", "")
    if model_name and "model" not in model_request_config:
        model_request_config["model"] = model_name

    logger.info(
        "[TeamConfigLoader] model config loaded: model_name=%s, provider=%s",
        model_name,
        model_client_config.get("client_provider", "unknown"),
    )
    return {
        "model_client_config": model_client_config,
        "model_request_config": model_request_config,
    }


def _resolve_storage_config(storage_raw: dict[str, Any]) -> dict[str, Any]:
    storage_dict = deepcopy(storage_raw)
    storage_params = storage_dict.get("params", {})
    if "connection_string" not in storage_params:
        return storage_dict

    db_path = resolve_team_sqlite_db_path({"team": {"storage": storage_dict}})
    if db_path is None:
        return storage_dict

    storage_params["connection_string"] = str(db_path)

    db_dir = db_path.parent
    if not db_dir.exists():
        db_dir.mkdir(parents=True, exist_ok=True)
        logger.info("[TeamConfigLoader] Created database directory: %s", db_dir)

    return storage_dict


def _build_agent_defaults() -> tuple[dict[str, Any], int, float]:
    return (
        deepcopy(_DEFAULT_AGENT_WORKSPACE),
        _DEFAULT_MAX_ITERATIONS,
        _DEFAULT_COMPLETION_TIMEOUT,
    )


def _build_agent_spec_dict(
    agent_config: dict[str, Any],
    *,
    default_model: dict[str, Any],
    default_workspace: dict[str, Any],
    max_iterations: int,
    completion_timeout: float,
) -> dict[str, Any]:
    merged = deepcopy(agent_config)
    merged.setdefault("model", deepcopy(default_model))
    merged.setdefault("workspace", deepcopy(default_workspace))
    merged.setdefault("max_iterations", max_iterations)
    merged.setdefault("completion_timeout", completion_timeout)
    return merged


def _build_agents_config(team_raw: dict[str, Any], config_base: dict[str, Any]) -> dict[str, Any]:
    default_model = _build_default_model_dict(config_base)
    default_workspace, max_iterations, completion_timeout = _build_agent_defaults()

    agents_raw = team_raw.get("agents", {})
    if not isinstance(agents_raw, dict) or not agents_raw:
        logger.warning("[TeamConfigLoader] agents config is empty, using default leader/teammate")
        agents_raw = {"leader": {}, "teammate": {}}

    agents: dict[str, Any] = {}
    for agent_key, raw_agent_config in agents_raw.items():
        agent_config = dict(raw_agent_config) if isinstance(raw_agent_config, dict) else {}
        # No longer auto-fill all skills
        # When skills not configured, member won't copy any skill to own directory
        # Global skills are copied to team shared directory, accessible via .team/{team_name}/skills
        agent_spec = _build_agent_spec_dict(
            agent_config,
            default_model=default_model,
            default_workspace=default_workspace,
            max_iterations=max_iterations,
            completion_timeout=completion_timeout,
        )
        agents[agent_key] = agent_spec

    if "leader" not in agents:
        agents["leader"] = _build_agent_spec_dict(
            {},
            default_model=default_model,
            default_workspace=default_workspace,
            max_iterations=max_iterations,
            completion_timeout=completion_timeout,
        )

    return agents


def _build_workspace_spec(team_raw: dict[str, Any]) -> dict[str, Any] | None:
    workspace_raw = team_raw.get("workspace")
    if not isinstance(workspace_raw, dict) or "enabled" not in workspace_raw:
        return None

    workspace_spec = deepcopy(workspace_raw)
    workspace_spec.setdefault("version_control", False)
    return workspace_spec


def _build_leader_spec(team_raw: dict[str, Any]) -> dict[str, Any]:
    leader_raw = team_raw.get("leader", {})
    return {
        "member_name": leader_raw.get("member_name", "team_leader"),
        "display_name": leader_raw.get("display_name", "Team Leader"),
        "persona": leader_raw.get("persona", "天才项目管理专家"),
    }


def _build_predefined_members(team_raw: dict[str, Any]) -> list[dict[str, Any]]:
    predefined_members_raw = team_raw.get("predefined_members", [])
    if not isinstance(predefined_members_raw, list):
        logger.warning("[TeamConfigLoader] predefined_members must be a list, ignored")
        return []

    predefined_members: list[dict[str, Any]] = []
    for item in predefined_members_raw:
        if not isinstance(item, dict):
            continue

        member_name = str(item.get("member_name", "")).strip()
        if not member_name:
            logger.warning("[TeamConfigLoader] skipped predefined member without member_name: %s", item)
            continue

        member_spec = deepcopy(item)
        member_spec["member_name"] = member_name
        member_spec.setdefault("display_name", member_name)
        member_spec["persona"] = member_spec.get("persona") or ""

        predefined_members.append(member_spec)

    return predefined_members


def load_team_spec_dict(session_id: str) -> dict[str, Any]:
    """Load team config and build a TeamAgentSpec-compatible dict."""
    config_base = get_config()
    team_raw = config_base.get("team", {})

    if not team_raw:
        logger.warning("[TeamConfigLoader] no team config found, using defaults")
        team_raw = {}

    agents = _build_agents_config(team_raw, config_base)
    spec_dict = deepcopy(team_raw)

    spec_dict["team_name"] = f"{team_raw.get('team_name', 'team')}_{session_id}"
    spec_dict["lifecycle"] = team_raw.get("lifecycle", "persistent")
    spec_dict["teammate_mode"] = team_raw.get("teammate_mode", "build_mode")
    spec_dict["spawn_mode"] = team_raw.get("spawn_mode", "inprocess")
    spec_dict["leader"] = _build_leader_spec(team_raw)
    spec_dict["agents"] = agents

    workspace_spec = _build_workspace_spec(team_raw)
    if workspace_spec is not None:
        spec_dict["workspace"] = workspace_spec

    predefined_members = _build_predefined_members(team_raw)
    if predefined_members:
        spec_dict["predefined_members"] = predefined_members
    elif "predefined_members" in spec_dict:
        spec_dict.pop("predefined_members", None)

    storage_raw = team_raw.get("storage", {})
    if storage_raw:
        spec_dict["storage"] = _resolve_storage_config(storage_raw)

    logger.info(
        "[TeamConfigLoader] team config loaded: team_name=%s, lifecycle=%s, agents=%s, predefined_members=%s",
        spec_dict["team_name"],
        spec_dict["lifecycle"],
        list(agents.keys()),
        [item["member_name"] for item in predefined_members],
    )
    return spec_dict
