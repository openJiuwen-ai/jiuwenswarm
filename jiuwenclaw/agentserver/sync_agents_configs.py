# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Helpers for sync_agents_configs protocol validation and materialization."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from typing import Any

from jiuwenclaw.agentserver.memory.external_memory_config import (
    _VALID_ENGINES as _VALID_MEMORY_ENGINES,
)
from jiuwenclaw.agentserver.tenant_catalog_registry import TenantAgentSpec
from jiuwenclaw.local_env_config import EnvNsIdError, normalize_env_ns_id

logger = logging.getLogger(__name__)

SYNC_ENV_SCHEMA: frozenset[str] = frozenset(
    {
        "API_KEY",
        "API_BASE",
        "MEMORY_ENGINE",
        "EVOLUTION_ENABLED",
        "EMBED_API_KEY",
        "EMBED_API_BASE",
        "EMBED_MODEL",
        "MODEL_NAME",
        "MODEL_PROVIDER",
        "MODEL_CONTEXT_WINDOW",
        "TOOL_CALLING_GUARD_ENABLED",
        "TOOL_CALLING_GUARD_DISABLE",
        "TOOL_CALLING_GUARD_STRIP_REASON",
        "ENABLED_SKILLS",
        "DISABLED_SKILLS",
        "JIUWENCLAW_DISABLED_SKILLS",
        "JIUWENCLAW_SHARED_SKILLS_DIRS",
        "BOCHA_API_KEY",
        "JINA_API_KEY",
        "PERPLEXITY_API_KEY",
        "SERPER_API_KEY",
        "default_headers",
        "VISION_API_KEY",
        "VISION_API_BASE",
        "VISION_PROVIDER",
        "VISION_MODEL_NAME",
        "VISION_DEFAULT_HEADERS",
        "IMAGE_GEN_API_KEY",
        "IMAGE_GEN_API_BASE",
        "IMAGE_GEN_PROVIDER",
        "IMAGE_GEN_MODEL_NAME",
        "IMAGE_GEN_DEFAULT_HEADERS",
        "AUDIO_API_KEY",
        "AUDIO_API_BASE",
        "AUDIO_PROVIDER",
        "AUDIO_MODEL_NAME",
        "VIDEO_API_KEY",
        "VIDEO_API_BASE",
        "VIDEO_PROVIDER",
        "VIDEO_MODEL_NAME",
        "PETAL_SEARCH_URL",
        "PETAL_SEARCH_HEADERS",
        "TAVILY_API_KEY",
        "LLM_API_KEY",
        "WEB_SEARCH_API_KEY",
        "EMAIL_TOKEN",
        "GITHUB_TOKEN",
        "ACR_ACCESS_KEY",
        "ACR_ACCESS_SECRET",
        "ACR_BASE_URL",
        "FREE_SEARCH_DDG_ENABLED",
        "FREE_SEARCH_BING_ENABLED",
    }
)


def materialize_sync_env(env_dict: dict[str, Any]) -> dict[str, str]:
    """Build active-tip map: keep empty strings, omit nulls (deletes)."""
    if not isinstance(env_dict, dict):
        raise ValueError("agent env must be an object")
    return {str(k): str(v) for k, v in env_dict.items() if v is not None}


def _env_bool(value: Any) -> bool | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return None


def _ensure_memory_dict(result: dict[str, Any]) -> dict[str, Any]:
    memory = result.get("memory")
    if not isinstance(memory, dict):
        memory = {}
        result["memory"] = memory
    return memory


def _ensure_react_evolution_dict(result: dict[str, Any]) -> dict[str, Any]:
    """Prefer config.react.evolution; migrate bare config.evolution into react when needed."""
    react = result.get("react")
    if not isinstance(react, dict):
        react = {}
        result["react"] = react
    evolution = react.get("evolution")
    if not isinstance(evolution, dict):
        top = result.pop("evolution", None)
        evolution = top if isinstance(top, dict) else {}
        react["evolution"] = evolution
    else:
        top = result.pop("evolution", None)
        if isinstance(top, dict) and top:
            logger.warning(
                "sync_agents_configs: discarding top-level evolution=%r; "
                "react.evolution takes precedence",
                top,
            )
    return evolution


def synthesize_config(
    config: Any,
    env: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Materialize memory/evolution into config; protocol authority is env.

    Writes ``config.memory.engine`` and ``config.react.evolution`` from
    ``MEMORY_ENGINE`` / ``EVOLUTION_ENABLED``. Mis-sent config blocks are
    overlaid from env (warn). When env values are blank/absent, falls back to
    existing config then defaults (``builtin`` / ``enabled=false``).
    """
    if not isinstance(config, dict):
        raise ValueError("agent config must be an object")
    result = copy.deepcopy(config)

    inbound_engine: str | None = None
    mem_in = config.get("memory")
    if isinstance(mem_in, dict) and mem_in.get("engine") is not None:
        inbound_engine = str(mem_in.get("engine")).strip().lower()

    inbound_evo: bool | None = None
    react_in = config.get("react")
    if isinstance(react_in, dict) and isinstance(react_in.get("evolution"), dict):
        if "enabled" in react_in["evolution"]:
            inbound_evo = bool(react_in["evolution"].get("enabled"))
    elif isinstance(config.get("evolution"), dict) and "enabled" in config["evolution"]:
        inbound_evo = bool(config["evolution"].get("enabled"))

    memory = _ensure_memory_dict(result)
    evolution = _ensure_react_evolution_dict(result)

    engine = (
        inbound_engine
        if inbound_engine in _VALID_MEMORY_ENGINES
        else "builtin"
    )
    evo_enabled = bool(inbound_evo) if inbound_evo is not None else False

    if isinstance(env, dict):
        env_engine_raw = env.get("MEMORY_ENGINE")
        if env_engine_raw is not None:
            env_engine_text = str(env_engine_raw).strip().lower()
            if env_engine_text in _VALID_MEMORY_ENGINES:
                if inbound_engine is not None and inbound_engine != env_engine_text:
                    logger.warning(
                        "sync_agents_configs: MEMORY_ENGINE env=%r overlays "
                        "mis-sent config engine=%r",
                        env_engine_text,
                        inbound_engine,
                    )
                engine = env_engine_text
            elif env_engine_text:
                logger.warning(
                    "sync_agents_configs: invalid MEMORY_ENGINE env=%r; "
                    "keeping engine=%r",
                    env_engine_text,
                    engine,
                )

        env_evo = _env_bool(env.get("EVOLUTION_ENABLED"))
        if env_evo is not None:
            if inbound_evo is not None and inbound_evo != env_evo:
                logger.warning(
                    "sync_agents_configs: EVOLUTION_ENABLED env=%r overlays "
                    "mis-sent config enabled=%r",
                    env.get("EVOLUTION_ENABLED"),
                    inbound_evo,
                )
            evo_enabled = env_evo

    memory["engine"] = engine
    evolution["enabled"] = evo_enabled
    return result


def compute_content_hash(
    *,
    config: dict[str, Any],
    env: dict[str, Any],
    runtime: dict[str, Any],
) -> str:
    """Stable SHA-256 of config + env + runtime JSON."""
    payload = json.dumps(
        {"config": config, "env": env, "runtime": runtime},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_env_schema(env: Any, *, agent_id: str) -> None:
    if not isinstance(env, dict):
        raise ValueError(f"agent {agent_id!r}: env must be an object")
    missing = sorted(SYNC_ENV_SCHEMA - {str(k) for k in env})
    if missing:
        raise ValueError(
            f"agent {agent_id!r}: env missing required keys: {', '.join(missing)}"
        )
    extra = sorted(str(k) for k in env if str(k) not in SYNC_ENV_SCHEMA)
    if extra:
        logger.debug(
            "sync_agents_configs: agent %r env has extra keys (accepted): %s",
            agent_id,
            ", ".join(extra),
        )


def _validate_shared_env(shared_env: Any) -> None:
    if not isinstance(shared_env, dict):
        raise ValueError("shared_env must be an object when provided")
    # Track A contract: MVP log/validate presence only — never mutate spawn env in sync.
    from jiuwenclaw.local_env_config import SPAWN_ENV_KEYS

    unknown = sorted(str(k) for k in shared_env if str(k) not in SPAWN_ENV_KEYS)
    if unknown:
        logger.info(
            "sync_agents_configs: shared_env keys outside SPAWN table (ignored): %s",
            ", ".join(unknown),
        )
    logger.info(
        "sync_agents_configs: shared_env received (%d keys); spawn mutation skipped",
        len(shared_env),
    )


def _validate_top_level_models(models_raw: Any) -> list[dict[str, Any]]:
    """Validate and normalize the top-level models array.

    Returns the validated list, or raises ValueError on protocol errors.
    """
    if not isinstance(models_raw, list):
        raise ValueError("models must be an array when provided")
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, entry in enumerate(models_raw):
        if not isinstance(entry, dict):
            raise ValueError(f"models[{i}] must be an object")
        mcc = entry.get("model_client_config")
        if not isinstance(mcc, dict):
            raise ValueError(f"models[{i}].model_client_config is required and must be an object")
        model_name = str(mcc.get("model_name") or "").strip()
        if not model_name:
            raise ValueError(f"models[{i}].model_client_config.model_name is required")
        alias = str(entry.get("alias") or "").strip()
        key = alias if alias else model_name
        if key in seen:
            raise ValueError(
                f"models[{i}] duplicate {'alias' if alias else 'model_name'}: {key!r}"
            )
        seen.add(key)
        validated.append(entry)
    return validated


def validate_sync_payload(params: Any) -> dict[str, Any]:
    """Validate sync_agents_configs params; raise ValueError on protocol errors."""
    if not isinstance(params, dict):
        raise ValueError("sync_agents_configs params must be an object")

    revision = params.get("revision")
    if not isinstance(revision, str) or not revision.strip():
        raise ValueError("revision is required and must be a non-empty string")
    revision = revision.strip()

    service_id_raw = params.get("service_id")
    if not isinstance(service_id_raw, str) or not str(service_id_raw).strip():
        raise ValueError("service_id is required and must be a non-empty string")
    try:
        service_id = normalize_env_ns_id(str(service_id_raw).strip(), default="")
    except EnvNsIdError as exc:
        raise ValueError(f"invalid service_id: {exc}") from exc
    if not service_id:
        raise ValueError("service_id is required and must be a non-empty string")

    agents = params.get("agents")
    if not isinstance(agents, list):
        raise ValueError("agents is required and must be an array")

    shared_env = params.get("shared_env")
    if shared_env is not None:
        _validate_shared_env(shared_env)

    models_raw = params.get("models")
    models_validated: list[dict[str, Any]] | None = None
    if models_raw is not None:
        models_validated = _validate_top_level_models(models_raw)
        logger.info(
            "sync_agents_configs: top-level models received (%d entries)",
            len(models_validated),
        )

    seen_ids: set[str] = set()
    normalized_agents: list[dict[str, Any]] = []
    for index, entry in enumerate(agents):
        if not isinstance(entry, dict):
            raise ValueError(f"agents[{index}] must be an object")
        agent_id_raw = entry.get("agent_id")
        if not isinstance(agent_id_raw, str) or not str(agent_id_raw).strip():
            raise ValueError(f"agents[{index}].agent_id is required")
        try:
            agent_id = normalize_env_ns_id(str(agent_id_raw).strip(), default="")
        except EnvNsIdError as exc:
            raise ValueError(f"agents[{index}].agent_id invalid: {exc}") from exc
        if not agent_id:
            raise ValueError(f"agents[{index}].agent_id is required")
        if agent_id in seen_ids:
            raise ValueError(f"duplicate agent_id in sync payload: {agent_id!r}")
        seen_ids.add(agent_id)

        config = entry.get("config")
        env = entry.get("env")
        runtime = entry.get("runtime")
        if config is None:
            raise ValueError(f"agent {agent_id!r}: config is required")
        if env is None:
            raise ValueError(f"agent {agent_id!r}: env is required")
        if runtime is None:
            raise ValueError(f"agent {agent_id!r}: runtime is required")
        if not isinstance(runtime, dict):
            raise ValueError(f"agent {agent_id!r}: runtime must be an object")

        _validate_env_schema(env, agent_id=agent_id)
        normalized_agents.append(
            {
                "agent_id": agent_id,
                "config": config,
                "env": env,
                "runtime": runtime,
            }
        )

    return {
        "revision": revision,
        "service_id": service_id,
        "agents": normalized_agents,
        "shared_env": shared_env,
        "models": models_validated,
    }


def build_agent_spec(
    *,
    service_id: str,
    agent_id: str,
    config: dict[str, Any],
    env: dict[str, Any],
    runtime: dict[str, Any],
    revision: str,
) -> TenantAgentSpec:
    synthesized = synthesize_config(config, env)
    content_hash = compute_content_hash(
        config=synthesized,
        env=env,
        runtime=runtime,
    )
    return TenantAgentSpec(
        service_id=service_id,
        agent_id=agent_id,
        config=synthesized,
        env=env,
        runtime=runtime,
        revision=revision,
        content_hash=content_hash,
    )


@dataclass
class AgentSyncResultItem:
    """Per-agent sync_agents_configs result fields (G.FNM.03)."""

    agent_id: str
    action: str
    ok: bool
    error: str | None = None
    warmup: dict[str, Any] | None = None
    reload: dict[str, Any] | None = None


def build_agent_result(item: AgentSyncResultItem) -> dict[str, Any]:
    return asdict(item)
