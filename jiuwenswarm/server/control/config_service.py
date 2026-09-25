# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Config query Control Service.

Reads ``config.yaml`` and process env directly so Front never imports
``jiuwenswarm.common.config`` (that module pulls OpenJiuwen KV-cache types).
Write/validate RPCs stay on the Runtime path.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.utils import get_config_file
from jiuwenswarm.common.version import __version__
from jiuwenswarm.server.control.a2ui_config import get_a2ui_config
from jiuwenswarm.server.control.responses import build_error_response
from jiuwenswarm.symphony.config import (
    resolve_symphony_enabled,
    resolve_symphony_evolution_enabled,
)

logger = logging.getLogger(__name__)

_CONFIG_SET_ENV_MAP = {
    "model_provider": "MODEL_PROVIDER",
    "model": "MODEL_NAME",
    "api_base": "API_BASE",
    "api_key": "API_KEY",
    "endpoint_profile": "ENDPOINT_PROFILE",
    "video_api_base": "VIDEO_API_BASE",
    "video_api_key": "VIDEO_API_KEY",
    "video_model": "VIDEO_MODEL_NAME",
    "video_provider": "VIDEO_PROVIDER",
    "video_endpoint_profile": "VIDEO_ENDPOINT_PROFILE",
    "video_vendor_key": "VIDEO_VENDOR_KEY",
    "video_plan": "VIDEO_PLAN",
    "video_enabled": "VIDEO_ENABLED",
    "video_gen_api_base": "VIDEO_GEN_API_BASE",
    "video_gen_api_key": "VIDEO_GEN_API_KEY",
    "video_gen_model": "VIDEO_GEN_MODEL_NAME",
    "video_gen_provider": "VIDEO_GEN_PROVIDER",
    "video_gen_protocol": "VIDEO_GEN_PROTOCOL",
    "video_gen_enabled": "VIDEO_GEN_ENABLED",
    "visual_gen_api_base": "VISUAL_GEN_API_BASE",
    "visual_gen_api_key": "VISUAL_GEN_API_KEY",
    "visual_gen_model": "VISUAL_GEN_MODEL_NAME",
    "visual_gen_provider": "VISUAL_GEN_PROVIDER",
    "visual_gen_protocol": "VISUAL_GEN_PROTOCOL",
    "visual_gen_enabled": "VISUAL_GEN_ENABLED",
    "audio_api_base": "AUDIO_API_BASE",
    "audio_api_key": "AUDIO_API_KEY",
    "audio_model": "AUDIO_MODEL_NAME",
    "audio_provider": "AUDIO_PROVIDER",
    "audio_endpoint_profile": "AUDIO_ENDPOINT_PROFILE",
    "audio_vendor_key": "AUDIO_VENDOR_KEY",
    "audio_plan": "AUDIO_PLAN",
    "audio_enabled": "AUDIO_ENABLED",
    "vision_api_base": "VISION_API_BASE",
    "vision_api_key": "VISION_API_KEY",
    "vision_model": "VISION_MODEL_NAME",
    "vision_provider": "VISION_PROVIDER",
    "vision_endpoint_profile": "VISION_ENDPOINT_PROFILE",
    "vision_vendor_key": "VISION_VENDOR_KEY",
    "vision_plan": "VISION_PLAN",
    "vision_enabled": "VISION_ENABLED",
    "email_address": "EMAIL_ADDRESS",
    "email_token": "EMAIL_TOKEN",
    "embed_api_key": "EMBED_API_KEY",
    "embed_api_base": "EMBED_API_BASE",
    "embed_model": "EMBED_MODEL",
    "jina_api_key": "JINA_API_KEY",
    "bocha_api_key": "BOCHA_API_KEY",
    "serper_api_key": "SERPER_API_KEY",
    "perplexity_api_key": "PERPLEXITY_API_KEY",
    "github_token": "GITHUB_TOKEN",
    "teamskills_market_url": "TEAM_SKILLS_HUB_BASE_URL",
    "teamskills_user_token": "TEAM_SKILLS_HUB_USER_TOKEN",
    "teamskills_system_token": "TEAM_SKILLS_HUB_SYSTEM_TOKEN",
    "teamskills_allowed_download_hosts": "TEAM_SKILLS_HUB_ALLOWED_DOWNLOAD_HOSTS",
    "free_search_ddg_enabled": "FREE_SEARCH_DDG_ENABLED",
    "free_search_bing_enabled": "FREE_SEARCH_BING_ENABLED",
    "free_search_proxy_url": "FREE_SEARCH_PROXY_URL",
    "skills": "SKILLS",
    "max_iterations": "MAX_ITERATIONS",
    "completion_timeout": "COMPLETION_TIMEOUT",
    "team_name": "TEAM_NAME",
    "lifecycle": "LIFECYCLE",
    "teammate_mode": "TEAMATE_MODE",
    "spawn_mode": "SPAWN_MODE",
    "member_name": "MEMBER_NAME",
    "display_name": "DISPLAY_NAME",
    "persona": "PERSONA",
    "agent_key": "AGENT_KEY",
    "role_type": "ROLE_TYPE",
    "prompt_hint": "PROMPT_HINT",
}

_SWARMFLOW_ENABLED_PATH = ("modes", "team", "jiuwen_team", "enable_swarmflow")
_SWARMFLOW_BUDGET_PATH = ("modes", "team", "jiuwen_team", "swarmflow_budget")
_EXTERNAL_CLI_AGENTS_PATH = ("modes", "team", "jiuwen_team", "external_cli_agents")
_KV_CACHE_CONFIG_KEY = "kv_cache_affinity_config"
_KV_CACHE_ENABLED_KEY = "enable_kv_cache_affinity"


def _read_raw_config() -> dict[str, Any]:
    path = Path(get_config_file())
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[config_service] failed to read config.yaml: %s", exc)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _nested(raw: dict[str, Any], path: tuple[str, ...], default: Any = None) -> Any:
    current: Any = raw
    for segment in path:
        if not isinstance(current, dict):
            return default
        current = current.get(segment)
    return default if current is None else current


def _bool_text(value: Any, default: bool = False) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "true" if default else "false"
    return "true" if str(value).strip().lower() in {"true", "1", "yes", "on"} else "false"


def _affinity_enabled(raw: dict[str, Any]) -> bool:
    canonical = raw.get(_KV_CACHE_CONFIG_KEY)
    if isinstance(canonical, dict) and _KV_CACHE_ENABLED_KEY in canonical:
        return bool(canonical.get(_KV_CACHE_ENABLED_KEY, False))
    react = raw.get("react") if isinstance(raw.get("react"), dict) else {}
    legacy = react.get(_KV_CACHE_CONFIG_KEY)
    if isinstance(legacy, dict):
        return bool(legacy.get(_KV_CACHE_ENABLED_KEY, False))
    return False


def _flatten_symphony(raw: dict[str, Any]) -> dict[str, str]:
    symphony = raw.get("symphony") if isinstance(raw.get("symphony"), dict) else {}
    evolution = (
        symphony.get("evolution") if isinstance(symphony.get("evolution"), dict) else {}
    )
    flow = evolution.get("flow") if isinstance(evolution.get("flow"), dict) else {}
    flow_enabled = flow.get("enabled")
    if flow_enabled is None:
        flow_enabled = evolution.get("enabled")
    retrieval = (
        symphony.get("skill_retrieval")
        if isinstance(symphony.get("skill_retrieval"), dict)
        else {}
    )
    index = retrieval.get("index") if isinstance(retrieval.get("index"), dict) else {}
    discovery = (
        retrieval.get("discovery") if isinstance(retrieval.get("discovery"), dict) else {}
    )
    return {
        "symphony_enabled": _bool_text(
            resolve_symphony_enabled(symphony.get("enabled"))
        ),
        "symphony_evolution_enabled": _bool_text(
            resolve_symphony_evolution_enabled(flow_enabled)
        ),
        "skill_retrieval_enabled": _bool_text(retrieval.get("enabled"), False),
        "skill_retrieval_index_enabled": _bool_text(index.get("enabled"), False),
        "skill_retrieval_index_recommendation_shown": _bool_text(
            index.get("recommendation_shown"), False
        ),
        "skill_retrieval_max_results": str(discovery.get("max_results", 10)),
        "skill_retrieval_max_output_chars": str(discovery.get("max_output_chars", 12000)),
        "skill_retrieval_max_list_entries": str(discovery.get("max_list_entries", 40)),
        "skill_retrieval_incremental_notice_max_chars": str(
            discovery.get("incremental_notice_max_chars", 4000)
        ),
    }


def _flatten_external_cli(raw: dict[str, Any]) -> dict[str, str]:
    agents = _nested(raw, _EXTERNAL_CLI_AGENTS_PATH, [])
    configured: dict[str, dict[str, str]] = {}
    if isinstance(agents, list):
        for item in agents:
            if isinstance(item, str):
                cli_agent, cli_path = item.strip(), ""
            elif isinstance(item, dict):
                cli_agent = str(item.get("cli_agent") or "").strip()
                cli_path = str(item.get("cli_path") or item.get("codex_bin") or "").strip()
            else:
                continue
            if cli_agent:
                configured[cli_agent] = {"cli_path": cli_path}
    return {
        "external_cli_agent_claude_enabled": "true" if "claude" in configured else "false",
        "external_cli_agent_claude_use_builtin": (
            "true" if "claude" in configured and not configured["claude"].get("cli_path") else "false"
        ),
        "external_cli_agent_claude_cli_path": configured.get("claude", {}).get("cli_path", ""),
        "external_cli_agent_codex_enabled": "true" if "codex" in configured else "false",
        "external_cli_agent_codex_use_builtin": (
            "true" if "codex" in configured and not configured["codex"].get("cli_path") else "false"
        ),
        "external_cli_agent_codex_cli_path": configured.get("codex", {}).get("cli_path", ""),
    }


def _flatten_modes_team(raw: dict[str, Any]) -> dict[str, str]:
    modes = raw.get("modes")
    teams_raw = modes.get("team") if isinstance(modes, dict) else {}
    if not isinstance(teams_raw, dict):
        teams_raw = {}
    flat: dict[str, str] = {}
    agent_specs: dict[str, dict[str, Any]] = {}
    panel_cfg = raw.get("web_config_panel")
    if isinstance(panel_cfg, dict):
        registry = panel_cfg.get("agent_team_agents")
        if isinstance(registry, dict):
            for agent_key, spec in registry.items():
                if isinstance(agent_key, str) and isinstance(spec, dict):
                    agent_specs[agent_key] = spec

    def add_agent(agent_key: str, spec: Any) -> str:
        if agent_key and isinstance(spec, dict) and agent_key not in agent_specs:
            agent_specs[agent_key] = spec
        return agent_key

    def model_name_from_spec(spec: dict[str, Any]) -> str:
        model_cfg = spec.get("model")
        if not isinstance(model_cfg, dict):
            return ""
        if model_cfg.get("model") is not None:
            return str(model_cfg.get("model") or "")
        request_cfg = model_cfg.get("model_request_config")
        if isinstance(request_cfg, dict) and request_cfg.get("model") is not None:
            return str(request_cfg.get("model") or "")
        client_cfg = model_cfg.get("model_client_config")
        if isinstance(client_cfg, dict) and client_cfg.get("model_name") is not None:
            return str(client_cfg.get("model_name") or "")
        return ""

    for team_idx, (team_name, team_spec) in enumerate(teams_raw.items()):
        if team_idx >= 10 or not isinstance(team_spec, dict):
            continue
        prefix = f"team_{team_idx}_"
        flat[f"{prefix}name"] = str(team_spec.get("team_name") or team_name or "")
        flat[f"{prefix}lifecycle"] = str(team_spec.get("lifecycle") or "")
        flat[f"{prefix}teammate_mode"] = str(team_spec.get("teammate_mode") or "")
        flat[f"{prefix}spawn_mode"] = str(team_spec.get("spawn_mode") or "")
        flat[f"{prefix}enable_permissions"] = (
            "true" if bool(team_spec.get("enable_permissions", False)) else "false"
        )
        external_cli_agents = team_spec.get("external_cli_agents")
        flat[f"{prefix}external_cli_agents"] = (
            json.dumps(external_cli_agents, ensure_ascii=False)
            if isinstance(external_cli_agents, list)
            else ""
        )
        agents = team_spec.get("agents") if isinstance(team_spec.get("agents"), dict) else {}
        leader = team_spec.get("leader")
        if isinstance(leader, dict):
            for key in ("member_name", "display_name", "persona"):
                flat[f"{prefix}leader_{key}"] = str(leader.get(key) or "")
        leader_key = str(leader.get("agent_key") or "") if isinstance(leader, dict) else ""
        if not leader_key:
            leader_key = f"{team_name}_leader"
        flat[f"{prefix}leader_agent_key"] = add_agent(leader_key, agents.get("leader"))
        teammate_spec = agents.get("teammate")
        if isinstance(teammate_spec, dict):
            teammate = team_spec.get("teammate")
            teammate_key = str(teammate.get("agent_key") or "") if isinstance(teammate, dict) else ""
            if not teammate_key:
                teammate_key = f"{team_name}_teammate"
            flat[f"{prefix}teammate_agent_key"] = add_agent(teammate_key, teammate_spec)
        else:
            flat[f"{prefix}teammate_agent_key"] = ""
        members_out: list[dict[str, str]] = []
        members = team_spec.get("predefined_members")
        if isinstance(members, list):
            for member in members:
                if not isinstance(member, dict):
                    continue
                member_name = str(member.get("member_name") or "")
                agent_key = str(member.get("agent_key") or "")
                if not agent_key:
                    agent_key = f"{team_name}_{member_name}" if member_name else ""
                if agent_key:
                    add_agent(agent_key, agents.get(member_name))
                members_out.append(
                    {
                        "member_name": member_name,
                        "display_name": str(member.get("display_name") or ""),
                        "persona": str(member.get("persona") or ""),
                        "prompt_hint": str(member.get("prompt_hint") or ""),
                        "agent_key": agent_key,
                    }
                )
        flat[f"{prefix}predefined_members"] = json.dumps(members_out, ensure_ascii=False)
    for agent_idx, (agent_key, spec) in enumerate(agent_specs.items()):
        if agent_idx >= 10:
            break
        flat[f"agent_name_{agent_idx}"] = agent_key
        flat[f"agent_model_{agent_idx}"] = model_name_from_spec(spec)
        skills = spec.get("skills")
        flat[f"agent_skills_{agent_idx}"] = (
            ",".join(str(item) for item in skills) if isinstance(skills, list) else ""
        )
        flat[f"agent_max_iterations_{agent_idx}"] = str(spec.get("max_iterations") or 200)
        flat[f"agent_completion_timeout_{agent_idx}"] = str(spec.get("completion_timeout") or 600)
    return flat


def _resolve_env_vars(value: Any) -> Any:
    """Expand ``${VAR:-default}`` without importing ``common.config``."""
    if isinstance(value, str):
        pattern = r"\$\{([^:}]+)(?::-([^}]*))?\}"

        def replace_env(match: re.Match[str]) -> str:
            current = os.getenv(match.group(1))
            default = match.group(2)
            if default is not None:
                if current is None or current == "":
                    return default
                return current
            return current if current is not None else ""

        return re.sub(pattern, replace_env, value)
    if isinstance(value, dict):
        return {key: _resolve_env_vars(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_env_vars(item) for item in value]
    return value


def _resolve_browser_path(browser: dict[str, Any]) -> str:
    resolved = _resolve_env_vars(browser)
    if not isinstance(resolved, dict):
        return ""
    chrome_path = resolved.get("chrome_path", "")
    if isinstance(chrome_path, str):
        return chrome_path.strip()
    if not isinstance(chrome_path, dict):
        return ""
    platform_map = {
        "win32": "windows",
        "cygwin": "windows",
        "darwin": "macos",
        "linux": "linux",
        "linux2": "linux",
    }
    for key in (platform_map.get(sys.platform, "default"), "default"):
        value = chrome_path.get(key, "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def get_panel() -> dict[str, Any]:
    """Return the Web/TUI config panel payload without Gateway handlers."""
    payload: dict[str, Any] = {
        param_key: (os.getenv(env_key) or "")
        for param_key, env_key in _CONFIG_SET_ENV_MAP.items()
    }
    payload["app_version"] = __version__
    runtime_platform = (
        (os.getenv("JIUWENSWARM_RUNTIME_PLATFORM") or "").strip().lower() or "default"
    )
    payload["runtime_platform"] = runtime_platform
    payload["external_cli_agents_supported"] = "false" if runtime_platform == "harmony" else "true"
    try:
        raw = _read_raw_config()
        setup_guide_cfg = raw.get("setup_guide") or {}
        payload["setup_guide_enabled"] = _bool_text(
            setup_guide_cfg.get("enabled", True), True
        )
        react_cfg = raw.get("react") or {}
        ctx_cfg = react_cfg.get("context_engine_config") or {}
        payload["context_engine_enabled"] = _bool_text(ctx_cfg.get("enabled"), False)
        payload["kv_cache_affinity_enabled"] = (
            "true" if _affinity_enabled(raw) else "false"
        )
        perm_cfg = raw.get("permissions") or {}
        payload["permissions_enabled"] = _bool_text(perm_cfg.get("enabled"), False)
        evolution_cfg = (raw.get("react") or {}).get("evolution") or {}
        payload["skill_evolution"] = _bool_text(
            evolution_cfg.get("skill_evolution"), False
        )
        memory_cfg = (raw.get("memory") or {}).get("forbidden_memory_definition") or {}
        payload["memory_forbidden_enabled"] = _bool_text(memory_cfg.get("enabled"), False)
        payload["memory_forbidden_description"] = memory_cfg.get("description") or {}
        try:
            payload["a2ui_enabled"] = (
                "true" if get_a2ui_config(raw).enabled else "false"
            )
        except Exception:  # noqa: BLE001
            payload["a2ui_enabled"] = "false"
        trajectory_cfg = raw.get("trajectory_ui") or {}
        payload["trajectory_ui_enabled"] = _bool_text(trajectory_cfg.get("enabled"), False)
        payload["swarmflow_enabled"] = _bool_text(
            _nested(raw, _SWARMFLOW_ENABLED_PATH, False)
        )
        budget = _nested(raw, _SWARMFLOW_BUDGET_PATH)
        if budget is not None:
            payload["swarmflow_budget"] = str(budget)
        payload.update(_flatten_external_cli(raw))
        payload.update(_flatten_symphony(raw))
        if not payload.get("free_search_ddg_enabled"):
            payload["free_search_ddg_enabled"] = "false"
        if not payload.get("free_search_bing_enabled"):
            payload["free_search_bing_enabled"] = "false"
        payload.update(_flatten_modes_team(raw))
        proactive_cfg = raw.get("proactive_recommendation") or {}
        payload["proactive_recommendation_enabled"] = _bool_text(
            proactive_cfg.get("enabled"), False
        )
        payload["proactive_recommendation_max_recommend_per_day"] = str(
            proactive_cfg.get("max_recommend_per_day", 10)
        )
        payload["proactive_recommendation_max_rounds_per_tick"] = str(
            proactive_cfg.get("max_rounds_per_tick", 20)
        )
        models_cfg = raw.get("models") or {}
        payload["enable_free_models"] = _bool_text(
            models_cfg.get("enable_free_models", True), True
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[config_service] panel yaml merge failed: %s", exc)
        payload.setdefault("context_engine_enabled", "false")
        payload.setdefault("kv_cache_affinity_enabled", "false")
        payload.setdefault("permissions_enabled", "false")
        payload.setdefault("setup_guide_enabled", "true")
        payload.setdefault("skill_evolution", "false")
        payload.setdefault("memory_forbidden_enabled", "false")
        payload.setdefault("memory_forbidden_description", "")
        payload.setdefault("a2ui_enabled", "false")
        payload.setdefault("trajectory_ui_enabled", "false")
        payload.setdefault("swarmflow_enabled", "false")
        payload.setdefault("free_search_ddg_enabled", "false")
        payload.setdefault("free_search_bing_enabled", "false")
        payload.setdefault("proactive_recommendation_enabled", "false")
        payload.setdefault("proactive_recommendation_max_recommend_per_day", "10")
        payload.setdefault("proactive_recommendation_max_rounds_per_tick", "20")
        payload.setdefault("enable_free_models", "true")
    return payload


async def handle_config_request(request: AgentRequest) -> AgentResponse:
    method = request.req_method
    if method == ReqMethod.CONFIG_GET:
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=get_panel(),
            metadata=request.metadata,
        )
    raw = _read_raw_config()
    if method == ReqMethod.LOCALE_GET_CONF:
        lang = str(raw.get("preferred_language") or "zh").strip().lower()
        if lang not in {"zh", "en"}:
            lang = "zh"
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"preferred_language": lang},
            metadata=request.metadata,
        )
    if method == ReqMethod.PATH_GET:
        browser = raw.get("browser") or {}
        if not isinstance(browser, dict):
            browser = {}
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={
                "chrome_path": _resolve_browser_path(browser),
                "headless": browser.get("headless")
                if isinstance(browser.get("headless"), bool)
                else True,
            },
            metadata=request.metadata,
        )
    if method == ReqMethod.MEMORY_FORBIDDEN_GET:
        payload = ((raw.get("memory") or {}).get("forbidden_memory_definition", {}))
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload if isinstance(payload, dict) else {},
            metadata=request.metadata,
        )
    return build_error_response(
        request, "unsupported control config method", code="BAD_REQUEST"
    )
