# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team configuration loader."""

from __future__ import annotations

import hashlib
import json
import logging
from copy import deepcopy
from pathlib import Path
from typing import Any

from openjiuwen.agent_teams.paths import get_agent_teams_home

from jiuwenswarm.common.agentos_runtime import is_agentos_runtime
from jiuwenswarm.common.auth.login_credentials import bare_model_name
from jiuwenswarm.common.config import get_config, get_default_models
from jiuwenswarm.common.reasoning_injector import build_reasoning_model_request_kwargs
from jiuwenswarm.server.runtime.opencode_zen import get_zen_free_model_entries

from jiuwenswarm.common.team_template_config import (
    TeamTemplateNotFoundError,  # noqa: F401
    _resolve_legacy_team_template,
    _select_modes_team,
    get_team_template_snapshot,  # noqa: F401
    list_team_template_summaries,  # noqa: F401
)

logger = logging.getLogger(__name__)


def _prepare_external_cli_agents(spec_dict: dict[str, Any]) -> None:
    """Finish the external CLI agent entries before the team spec is built.

    Each catalog of built-in models is reconciled with what its CLI last
    reported (the configuration keeps the policy, the CLI owns the facts).
    Enabled custom agents (user-uploaded harness providers, each extracted
    into its own ``site-packages``) become resolvable once that directory is
    on ``sys.path``, and get the ``openjiuwen-team-mcp`` stdio server for their
    team tools, whose location depends on whether this is a frozen build.
    """
    from jiuwenswarm.common.custom_agent_runtime import (
        activate_custom_agent_paths,
        custom_agent_names,
        team_mcp_server_command,
    )
    from jiuwenswarm.common.external_cli_catalog import apply_reported_models

    entries = spec_dict.get("external_cli_agents")
    if not isinstance(entries, list):
        return
    apply_reported_models(entries)
    custom_names = set(custom_agent_names())
    if not custom_names:
        return
    activate_custom_agent_paths()
    for entry in entries:
        if isinstance(entry, dict) and entry.get("cli_agent") in custom_names:
            entry.setdefault("mcp_server_command", team_mcp_server_command())

_DEFAULT_MAX_ITERATIONS = 200
_DEFAULT_COMPLETION_TIMEOUT = 600.0
# The leader coordinates the whole team for the lifetime of a long-horizon
# task, so its inner ReAct loop must never be cut short. ``None`` means
# unbounded. ``completion_timeout`` is left alone: on the team path it only sets
# the slow-round log threshold and never aborts a round.
_LEADER_UNBOUNDED_BUDGET = {"max_iterations": None}
_DEFAULT_AGENT_WORKSPACE = {"stable_base": True}
_DEFAULT_TEAM_WORKSPACE = {"enabled": True}
_DEFAULT_TRANSPORT = {"type": "inprocess"}


def _select_first_modes_team(config_base: dict[str, Any]) -> dict[str, Any]:
    _, team_raw = _select_modes_team(config_base)
    return team_raw


def _resolve_team_raw_for_storage(config_base: dict[str, Any]) -> dict[str, Any]:
    selected = _select_first_modes_team(config_base)
    if selected:
        return selected

    _, legacy_team = _resolve_legacy_team_template(config_base)
    if legacy_team:
        return legacy_team

    return {}


def resolve_team_sqlite_db_path(config_base: dict[str, Any] | None = None) -> Path | None:
    """Resolve the team sqlite database path using openjiuwen semantics."""
    if config_base is None:
        config_base = get_config()

    team_raw = _resolve_team_raw_for_storage(config_base)
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


def _entry_model_name(entry: dict[str, Any] | None) -> str:
    if not isinstance(entry, dict):
        return ""
    mcc = entry.get("model_client_config") or {}
    if not isinstance(mcc, dict):
        return ""
    return str(mcc.get("model_name") or "").strip()


def _select_default_model_config(
    configured_entries: list[dict[str, Any]],
    *,
    requested_model_name: str | None = None,
    login_model_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requested = (requested_model_name or "").strip()
    agentos = is_agentos_runtime()
    if requested:
        # Port agent_os b8273f093's selector over develop's normalized entries.
        # Keep their positions: models.list and the single-agent cache use the
        # same defaults+agentos global origin_index, including placeholders.
        if agentos and "#" in requested:
            bare_name, _, index_part = requested.rpartition("#")
            if bare_name and index_part.isdigit():
                origin_index = int(index_part)
                if 0 <= origin_index < len(configured_entries):
                    entry = configured_entries[origin_index]
                    if _entry_model_name(entry) == bare_name:
                        return entry
        # When the caller (chat page) provides a requested model name, prefer
        # the entry whose ``model_client_config.model_name`` matches it so
        # team members without an explicit ``modes.team.agents.*.model`` fall
        # back to the page-selected model instead of the first list item.
        for item in configured_entries:
            if _entry_model_name(item) == requested or (agentos and item.get("alias") == requested):
                return item

        # Login-granted models are request-scoped and never written into
        # ``models.defaults``. Match the forwarded entry by name so a
        # page-selected login model still drives the team's fallback, without
        # overriding a same-named user-configured model above.
        # The login entry is built from the bare name (``#<index>`` stripped), so compare bare names.
        if login_model_entry is not None and _entry_model_name(login_model_entry) == bare_model_name(requested):
            return login_model_entry

        # The selected model may be a Zen free model that is appended to
        # ``models.list`` at runtime but never written into ``models.defaults``
        # (see ``opencode_zen``). Match it from the in-memory Zen cache so a
        # page-selected free model still drives the whole team's fallback model.
        for item in get_zen_free_model_entries():
            if _entry_model_name(item) == requested:
                return item

        if agentos:
            logger.warning("[TeamConfigLoader] requested model %r not found; using configured fallback", requested)

    if agentos:
        # agent_os 5cc86e59b: an unresolved placeholder must not shadow a usable
        # backup. Skip it only for fallback, without shifting origin_index.
        return next((entry for entry in configured_entries if _entry_model_name(entry)), {})
    if configured_entries:
        return configured_entries[0]

    return {}


def _resolve_default_model_config(
    config_base: dict[str, Any],
    *,
    requested_model_name: str | None = None,
    login_model_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve the selected model from normalized executable entries."""
    return _select_default_model_config(
        get_default_models(config_base),
        requested_model_name=requested_model_name,
        login_model_entry=login_model_entry,
    )


def _sanitize_team_member_model(model: dict[str, Any]) -> dict[str, Any]:
    """Map internal ``reasoning_level`` onto provider-neutral request kwargs.

    Team members construct ``ModelRequestConfig`` from this dict. Leaving the
    UI hint in ``model_request_config`` lets core ``extra=allow`` forward it to
    ``AsyncCompletions.create()`` as ``reasoning_level``.
    """
    if not isinstance(model, dict):
        return model
    model_client_config = dict(model.get("model_client_config") or {})
    # Keep UI hints that only exist on ``model_config_obj``, then let an
    # already-built ``model_request_config`` override overlapping keys.
    model_config_obj = dict(model.get("model_config_obj") or {})
    model_config_obj.update(dict(model.get("model_request_config") or {}))
    sanitized = dict(model)
    sanitized.pop("model_config_obj", None)
    sanitized["model_client_config"] = model_client_config
    # Same fill rule as the previous ``_build_default_model_dict``: keep an
    # already-declared request ``model``; only fall back to
    # ``model_client_config.model_name`` when it is missing. Injector rewrites
    # ``model_request_config["model"]`` from this name.
    declared_model = str(
        model_config_obj.get("model") or model_config_obj.get("model_name") or ""
    ).strip()
    sanitized["model_request_config"] = build_reasoning_model_request_kwargs(
        model_client_config=model_client_config,
        model_config_obj=model_config_obj,
        model_name=declared_model or str(model_client_config.get("model_name") or ""),
    )
    return sanitized


def _model_entry_fingerprint(entry: dict[str, Any]) -> str:
    """Return a fingerprint of all request-affecting model configuration."""
    model_client_config = deepcopy(entry.get("model_client_config") or {})
    model_name = str(model_client_config.get("model_name") or "").strip()
    provider = str(model_client_config.get("client_provider") or "").strip().casefold()
    api_base = str(model_client_config.get("api_base") or "").strip().rstrip("/")
    model_client_config["model_name"] = model_name
    model_client_config["client_provider"] = provider
    model_client_config["api_base"] = api_base
    payload = {
        "model_client_config": model_client_config,
        "model_config_obj": entry.get("model_config_obj") or {},
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _merge_effective_model_entries(
    configured_entries: list[dict[str, Any]],
    selected_entry: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Merge executable entries without collapsing distinct endpoints."""
    candidates = list(configured_entries)
    if selected_entry:
        candidates.append(selected_entry)
    merged: list[dict[str, Any]] = []
    fingerprints: set[str] = set()
    for entry in candidates:
        model_client_config = entry.get("model_client_config") or {}
        if not isinstance(model_client_config, dict) or not model_client_config.get("model_name"):
            continue
        fingerprint = _model_entry_fingerprint(entry)
        if fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        merged.append(deepcopy(entry))
    return merged


def get_effective_team_model_entries(
    config_base: dict[str, Any],
    *,
    requested_model_name: str | None = None,
    login_model_entry: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return configured models plus the effective page-selected model."""
    configured_entries = get_default_models(config_base)
    selected_entry = _select_default_model_config(
        configured_entries,
        requested_model_name=requested_model_name,
        login_model_entry=login_model_entry,
    )
    return _merge_effective_model_entries(
        configured_entries,
        selected_entry,
    )


def _build_default_model_dict(
    config_base: dict[str, Any],
    *,
    requested_model_name: str | None = None,
    login_model_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    model_config = _resolve_default_model_config(
        config_base,
        requested_model_name=requested_model_name,
        login_model_entry=login_model_entry,
    )
    model_client_config = dict(model_config.get("model_client_config", {}))
    model_name = model_client_config.get("model_name", "")

    logger.info(
        "[TeamConfigLoader] model config loaded: model_name=%s, provider=%s",
        model_name,
        model_client_config.get("client_provider", "unknown"),
    )
    return _sanitize_team_member_model(
        {
            "model_client_config": model_client_config,
            "model_config_obj": dict(model_config.get("model_config_obj", {})),
        }
    )


def _resolve_storage_config(storage_raw: dict[str, Any]) -> dict[str, Any]:
    storage_dict = deepcopy(storage_raw)
    storage_params = storage_dict.get("params", {})
    if "connection_string" not in storage_params:
        return storage_dict

    db_path = resolve_team_sqlite_db_path({"storage": storage_dict})
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
    member_model = merged.get("model")
    if isinstance(member_model, dict):
        merged["model"] = _sanitize_team_member_model(member_model)
    return merged


def _build_agents_config(
    team_raw: dict[str, Any],
    config_base: dict[str, Any],
    *,
    requested_model_name: str | None = None,
    login_model_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    default_model = _build_default_model_dict(
        config_base,
        requested_model_name=requested_model_name,
        login_model_entry=login_model_entry,
    )
    default_workspace, max_iterations, completion_timeout = _build_agent_defaults()

    agents_raw = team_raw.get("agents", {})
    if not isinstance(agents_raw, dict) or not agents_raw:
        logger.warning("[TeamConfigLoader] agents config is empty, using default leader/teammate")
        agents_raw = {"leader": {}, "teammate": {}}

    top_agents = config_base.get("agents", {})
    if not isinstance(top_agents, dict):
        top_agents = {}

    agents: dict[str, Any] = {}
    for agent_key, raw_agent_config in agents_raw.items():
        if isinstance(raw_agent_config, str) and raw_agent_config.startswith("$"):
            ref_name = raw_agent_config[1:]
            if ref_name in top_agents:
                agent_config = deepcopy(top_agents[ref_name])
                logger.debug(
                    "[TeamConfigLoader] resolved agent reference $%s -> agents.%s",
                    ref_name,
                    ref_name,
                )
            else:
                logger.warning(
                    "[TeamConfigLoader] agent reference '$%s' not found in top-level agents, using defaults",
                    ref_name,
                )
                agent_config = {}
        else:
            agent_config = dict(raw_agent_config) if isinstance(raw_agent_config, dict) else {}
        # No longer auto-fill all skills from global into each member by default.
        # On spawn, each member workspace exposes only its configured skill links.
        # Team-shared skills are maintained in the team workspace skill view.
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

    # Applied after the merge on purpose: a cap written by an older config
    # template must not survive and truncate a long-running leader.
    agents["leader"].update(_LEADER_UNBOUNDED_BUDGET)

    if set(agents.keys()) == {"leader"}:
        logger.info(
            "[TeamConfigLoader] agents config contains only leader; "
            "adding default teammate template"
        )
        agents["teammate"] = _build_agent_spec_dict(
            {},
            default_model=default_model,
            default_workspace=default_workspace,
            max_iterations=max_iterations,
            completion_timeout=completion_timeout,
        )

    return agents


def _build_workspace_spec(team_raw: dict[str, Any]) -> dict[str, Any] | None:
    workspace_raw = team_raw.get("workspace")
    if not isinstance(workspace_raw, dict):
        workspace_spec = deepcopy(_DEFAULT_TEAM_WORKSPACE)
        workspace_spec.setdefault("version_control", False)
        return workspace_spec

    workspace_spec = deepcopy(workspace_raw)
    workspace_spec.setdefault("enabled", True)
    workspace_spec.setdefault("version_control", False)
    return workspace_spec


def _build_transport_spec(team_raw: dict[str, Any]) -> dict[str, Any]:
    transport_raw = team_raw.get("transport")
    if not isinstance(transport_raw, dict):
        return deepcopy(_DEFAULT_TRANSPORT)

    transport_spec = deepcopy(transport_raw)
    transport_spec.setdefault("type", "inprocess")
    return transport_spec


def _build_leader_spec(team_raw: dict[str, Any]) -> dict[str, Any]:
    leader_raw = team_raw.get("leader", {})
    leader_name = (
        str(leader_raw.get("name", "")).strip()
        or str(leader_raw.get("display_name", "")).strip()
        or "TeamLeader"
    )
    return {
        "member_name": leader_raw.get("member_name", "team_leader"),
        "display_name": leader_raw.get("display_name", "Team Leader"),
        "name": leader_name,
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

        identity_name = item.get("name") or item.get("display_name")
        if not identity_name or not str(identity_name).strip():
            logger.warning(
                "[TeamConfigLoader] skipped predefined member without name/display_name: %s",
                item,
            )
            continue

        member_spec = deepcopy(item)
        member_spec["member_name"] = member_name
        member_spec["display_name"] = str(identity_name).strip()
        member_spec["persona"] = member_spec.get("persona") or ""
        # openjiuwen TeamMemberSpec 现按 role_type 判别联合类型，缺省补 teammate
        role_type = str(member_spec.get("role_type") or "").strip()
        member_spec["role_type"] = role_type or "teammate"

        predefined_members.append(member_spec)

    return predefined_members


def _resolve_enable_permissions(config_base: dict[str, Any]) -> bool:
    """Use the global permission switch for every Team runtime."""
    return bool((config_base.get("permissions") or {}).get("enabled", False))


def _apply_swarmflow_budget(spec_dict: dict[str, Any], raw_value: Any) -> None:
    """Validate and apply ``swarmflow_budget`` to *spec_dict* in-place.

    Only positive integers are accepted. Invalid / non-positive values are
    logged and silently dropped (the key is removed from *spec_dict*).
    """
    if raw_value is None:
        return
    try:
        budget_value = int(raw_value)
    except (ValueError, TypeError):
        logger.warning(
            "[TeamConfigLoader] invalid swarmflow_budget %r, ignored",
            raw_value,
        )
        spec_dict.pop("swarmflow_budget", None)
        return
    if budget_value <= 0:
        logger.warning(
            "[TeamConfigLoader] swarmflow_budget must be positive, got %r, ignored",
            raw_value,
        )
        spec_dict.pop("swarmflow_budget", None)
        return
    spec_dict["swarmflow_budget"] = budget_value


def load_team_spec_dict(
    config_base: dict[str, Any] | None = None,
    *,
    requested_model_name: str | None = None,
    login_model_entry: dict[str, Any] | None = None,
    template_id: str | None = None,
    template_snapshot: dict[str, Any] | None = None,
    strict_template: bool = False,
) -> dict[str, Any]:
    """Load team config and build a TeamAgentSpec-compatible dict.

    When ``requested_model_name`` is provided (e.g. from the chat page model
    selector), team members without an explicit ``modes.team.agents.*.model``
    fall back to the matching entry in ``models.defaults`` instead of the
    first list item. ``login_model_entry`` fills that gap when the selected
    name is a request-scoped login model (not in ``models.defaults``).
    """
    if config_base is None:
        config_base = get_config()
    if isinstance(template_snapshot, dict) and template_snapshot:
        team_raw = deepcopy(template_snapshot)
        resolved_template_id = (
            str(template_id or "").strip()
            or str(team_raw.get("team_name") or "").strip()
            or str(team_raw.get("name") or "").strip()
            or "team"
        )
    else:
        resolved_template_id, team_raw = _select_modes_team(
            config_base,
            template_id=template_id,
            strict_template=strict_template,
        )

    if not team_raw:
        logger.warning("[TeamConfigLoader] no modes.team config found, using defaults")
        team_raw = {}

    agents = _build_agents_config(
        team_raw,
        config_base,
        requested_model_name=requested_model_name,
        login_model_entry=login_model_entry,
    )
    spec_dict = deepcopy(team_raw)
    spec_dict.pop("enable_team_plan", None)
    _prepare_external_cli_agents(spec_dict)

    spec_dict["team_name"] = str(team_raw.get("team_name") or resolved_template_id or "team").strip() or "team"
    spec_dict["lifecycle"] = team_raw.get("lifecycle", "persistent")
    spec_dict["teammate_mode"] = team_raw.get("teammate_mode", "build_mode")
    spec_dict["spawn_mode"] = team_raw.get("spawn_mode", "inprocess")
    spec_dict["dispatch_mode"] = team_raw.get("dispatch_mode", "autonomous")
    # Who materializes the team (F_122): the leader via build_team, or the
    # framework on start. team_display_name / team_desc only matter under
    # "framework" and pass through from the raw section unchanged.
    spec_dict["bootstrap_mode"] = team_raw.get("bootstrap_mode", "leader")
    spec_dict["enable_task_verification"] = team_raw.get("enable_task_verification", False)
    spec_dict["enable_member_messaging"] = team_raw.get("enable_member_messaging", False)
    spec_dict["enable_hitt"] = team_raw.get("enable_hitt", True)
    spec_dict["enable_permissions"] = _resolve_enable_permissions(config_base)
    # Experimental switch drives both frontend UI and backend org tools.
    spec_dict["enable_organization"] = bool(
        (config_base.get("experimental") or {}).get("team_organization_ui_enabled", False)
    )
    _apply_swarmflow_budget(spec_dict, team_raw.get("swarmflow_budget"))
    # Team-level key forcing swarmflow workers into progressive exposure; the
    # spec field is named progressive_tool on the engine side. Leader /
    # teammate members (and, through the teammate base spec, workers) follow
    # the top-level progressive_tool_enabled switch in config_specs instead.
    spec_dict["progressive_tool"] = bool(spec_dict.pop("swarmflow_progressive_tool", False))
    spec_dict["leader"] = _build_leader_spec(team_raw)
    spec_dict["agents"] = agents
    spec_dict["language"] = str(config_base.get("preferred_language", "zh")).strip().lower()

    workspace_spec = _build_workspace_spec(team_raw)
    if workspace_spec is not None:
        spec_dict["workspace"] = workspace_spec

    spec_dict["transport"] = _build_transport_spec(team_raw)

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
