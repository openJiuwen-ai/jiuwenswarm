"""Result types and logging helpers for agent.reload_config hot-reload."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any

_MISSING = object()

AGENT_CONFIG_HOT_RELOAD_MARKER = "[agent_config] hot-reload"
AGENT_CONFIG_HOT_RELOAD_REPLAY_MARKER = "[agent_config] hot-reload replay"

_SENSITIVE_ERROR_PATTERN = re.compile(
    r"(?i)(api[_-]?key|secret|token|password)",
)
_MAX_ERROR_LOG_LEN = 120

MEMORY_ENV_KEYS = frozenset(
    {"EMBED_API_KEY", "EMBED_API_BASE", "EMBED_MODEL", "MEMORY_ENGINE"}
)

EXTERNAL_MEMORY_ENV_KEYS = frozenset({
    "MEMORY_EXTERNAL_PROVIDER",
    "MEMORY_USER_ID",
    "MEMORY_SCOPE_ID",
    "MEMORY_KV_PATH",
    "MEMORY_VECTOR_DIR",
    "MEMORY_DB_PATH",
    "MEM0_API_KEY",
    "MEM0_USER_ID",
    "MEM0_AGENT_ID",
    "OPENVIKING_ENDPOINT",
    "OPENVIKING_API_KEY",
    "OPENVIKING_ACCOUNT",
    "OPENVIKING_USER",
})

# Env keys that feed resolve_task_memory_config / TaskMemoryService fingerprint.
TASK_MEMORY_ENV_KEYS = frozenset({
    "TASK_MEMORY_LLM_MODEL",
    "TASK_MEMORY_EMBED_MODEL",
    "TASK_MEMORY_API_KEY",
    "TASK_MEMORY_API_BASE",
    "TASK_MEMORY_RETRIEVAL_ALGO",
    "TASK_MEMORY_SUMMARY_ALGO",
    "MODEL_NAME",
    "MODEL_PROVIDER",
    "API_KEY",
    "API_BASE",
    "EMBED_API_KEY",
    "EMBED_KEY",
    "EMBED_MODEL",
    "EMBEDDING_MODEL",
    "EMBED_API_BASE",
    "EMBED_BASE",
    "EMBED_BASE_URL",
})

SHARED_SKILLS_ENV_KEYS = frozenset(
    {
        "JIUWENSWARM_SHARED_SKILLS_DIRS",
        "ENABLED_SKILLS",
    }
)


def env_touches_external_memory(env_overrides: Any) -> bool:
    """Return True when reload env payload may affect external memory provider."""
    if not isinstance(env_overrides, dict):
        return False
    return bool({str(k) for k in env_overrides} & EXTERNAL_MEMORY_ENV_KEYS)


def env_touches_memory(env_overrides: Any) -> bool:
    """Return True when reload env payload may affect memory / embedding."""
    if not isinstance(env_overrides, dict):
        return False
    keys = {str(k) for k in env_overrides}
    return bool(keys & MEMORY_ENV_KEYS) or bool(keys & EXTERNAL_MEMORY_ENV_KEYS)


def env_touches_task_memory(env_overrides: Any) -> bool:
    """Return True when reload env payload may affect TaskMemoryService."""
    if not isinstance(env_overrides, dict):
        return False
    return bool({str(k) for k in env_overrides} & TASK_MEMORY_ENV_KEYS)


def env_touches_shared_skills_dirs(env_overrides: Any) -> bool:
    """Return True when reload env payload may change shared skill directories."""
    if not isinstance(env_overrides, dict):
        return False
    from jiuwenswarm.common.local_env_config import canonical_product_env_key

    keys = {canonical_product_env_key(k) for k in env_overrides}
    return bool(keys & SHARED_SKILLS_ENV_KEYS)


def embed_config_fingerprint(config: Any) -> tuple[Any, ...]:
    embed = config.get("embed") if isinstance(config, dict) else None
    if not isinstance(embed, dict):
        return ("", "", "")
    return (
        str(embed.get("embed_api_key") or ""),
        str(embed.get("embed_base_url") or ""),
        str(embed.get("embed_model") or ""),
    )


def memory_cache_fingerprint(config: Any) -> str:
    """Stable short hash for memory manager cache keys (engine + embed triple)."""
    from jiuwenswarm.agents.harness.common.memory.external_memory_config import get_memory_engine

    engine = get_memory_engine(config if isinstance(config, dict) else None)
    embed = embed_config_fingerprint(config)
    payload = f"{engine}|{embed[0]}|{embed[1]}|{embed[2]}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def reload_touches_memory(
    env_overrides: Any,
    config: Any,
    *,
    previous_config: Any = None,
) -> bool:
    """Return True when a reload may require rebuilding memory manager caches."""
    if env_touches_memory(env_overrides):
        return True
    if not isinstance(config, dict):
        return False
    new_fp = memory_cache_fingerprint(config)
    if isinstance(previous_config, dict):
        if memory_cache_fingerprint(previous_config) != new_fp:
            return True
        from jiuwenswarm.agents.harness.common.memory.external_memory_config import (
            external_memory_fingerprint,
        )

        prev_fp = external_memory_fingerprint(previous_config)
        new_fp = external_memory_fingerprint(config)
        return prev_fp != new_fp
    return False


@dataclass
class ReloadResult:
    """Per-session reload outcome."""

    applied: bool = False
    deferred: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.applied:
            payload["applied"] = True
        if self.deferred:
            payload["deferred"] = True
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass
class ReloadAggregateResult:
    """Aggregated reload stats for AgentManager / TenantAgentPool."""

    applied: int = 0
    deferred: int = 0
    failed: list[dict[str, str]] = field(default_factory=list)

    def merge(self, result: ReloadResult, *, session_key: str = "") -> None:
        if result.error:
            self.failed.append({"session": session_key, "error": result.error})
        elif result.deferred:
            self.deferred += 1
        elif result.applied:
            self.applied += 1

    def to_payload(self) -> dict[str, Any]:
        return {
            "reloaded": not self.failed or self.applied > 0 or self.deferred > 0,
            "applied": self.applied,
            "deferred": self.deferred,
            "failed": self.failed,
        }


def collect_config_path_keys(config: Any, *, prefix: str = "") -> list[str]:
    """Collect dot-separated paths for all keys in a config dict (values omitted)."""
    if not isinstance(config, dict):
        return []
    paths: list[str] = []
    for key, value in config.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        paths.append(path)
        if isinstance(value, dict):
            paths.extend(collect_config_path_keys(value, prefix=path))
    return paths


def _diff_config_paths(old: dict[str, Any], new: dict[str, Any], *, prefix: str = "") -> list[str]:
    changed: list[str] = []
    for key in set(old.keys()) | set(new.keys()):
        path = f"{prefix}.{key}" if prefix else str(key)
        old_val = old.get(key, _MISSING)
        new_val = new.get(key, _MISSING)
        if old_val is _MISSING or new_val is _MISSING:
            changed.append(path)
            continue
        if isinstance(old_val, dict) and isinstance(new_val, dict):
            changed.extend(_diff_config_paths(old_val, new_val, prefix=path))
        elif old_val != new_val:
            changed.append(path)
    return changed


def collect_changed_config_paths(previous: Any, current: Any) -> list[str]:
    """Return config paths that differ between previous and current snapshots."""
    if current is None or not isinstance(current, dict):
        return []
    if previous is None or not isinstance(previous, dict):
        return sorted(collect_config_path_keys(current))
    return sorted(_diff_config_paths(previous, current))


def collect_env_override_keys(env: Any) -> tuple[list[str], list[str]]:
    """Return (updated_keys, removed_keys) from an incremental env reload payload."""
    if not isinstance(env, dict) or not env:
        return [], []
    updated = sorted(str(k) for k, v in env.items() if v is not None)
    removed = sorted(str(k) for k, v in env.items() if v is None)
    return updated, removed


def redact_reload_error_message(error: str | None) -> str:
    """Return a short, redacted error string safe for hot-reload logs."""
    if not error:
        return ""
    text = str(error).strip()
    if _SENSITIVE_ERROR_PATTERN.search(text):
        return "[redacted]"
    if len(text) > _MAX_ERROR_LOG_LEN:
        return text[:_MAX_ERROR_LOG_LEN] + "..."
    return text


def format_reload_changed_keys(
    *,
    env: Any,
    config: Any,
    previous_config: Any = None,
    updated_param_keys: set[str] | list[str] | None = None,
) -> dict[str, Any]:
    """Build changed-key fields for logging (keys/paths only, never values)."""
    env_updated, env_removed = collect_env_override_keys(env)
    config_changed = collect_changed_config_paths(previous_config, config)
    fields: dict[str, Any] = {}
    if updated_param_keys:
        fields["updated_param_keys"] = sorted(updated_param_keys)
    if env_updated:
        fields["env_updated_keys"] = env_updated
    if env_removed:
        fields["env_removed_keys"] = env_removed
    if config_changed:
        fields["config_changed_paths"] = config_changed
    return fields


def log_agent_config_hot_reload(
    logger: logging.Logger,
    *,
    reload_trace_id: str | None,
    phase: str,
    source: str,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """Emit a unified hot-reload log line (never includes config/env values)."""
    parts: list[str] = [
        AGENT_CONFIG_HOT_RELOAD_MARKER,
        f"reload_trace_id={reload_trace_id or 'unknown'}",
        f"phase={phase}",
        f"source={source}",
    ]
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={value}")
    message = " ".join(parts)
    if phase == "failed":
        logger.log(level, message)
    else:
        logger.log(level, message)


def summarize_reload_payload(payload: Any) -> dict[str, Any]:
    """Extract applied/deferred/failed summary for completed-phase logs."""
    if not isinstance(payload, dict):
        return {"applied": 0, "deferred": 0, "failed_count": 0, "failed_sessions": []}

    failed = payload.get("failed")
    failed_list = failed if isinstance(failed, list) else []
    failed_sessions: list[str] = []
    for item in failed_list:
        if isinstance(item, dict):
            session = item.get("session")
            if session:
                failed_sessions.append(str(session))

    return {
        "ok": payload.get("reloaded", True),
        "applied": int(payload.get("applied") or 0),
        "deferred": int(payload.get("deferred") or 0),
        "failed_count": len(failed_list),
        "failed_sessions": failed_sessions or None,
    }


def log_agent_config_hot_reload_replay(
    logger: logging.Logger,
    *,
    reload_trace_id: str | None,
    session: str,
    agent_key: str,
    mode: str,
    config: Any,
    env: Any,
    source: str = "AgentManager",
) -> None:
    """Log config replay on new session creation (counts only, no values)."""
    env_updated, env_removed = collect_env_override_keys(env)
    env_key_count = len(env_updated) + len(env_removed)
    config_path_count = len(collect_config_path_keys(config)) if isinstance(config, dict) else 0
    parts = [
        AGENT_CONFIG_HOT_RELOAD_REPLAY_MARKER,
        f"reload_trace_id={reload_trace_id or 'unknown'}",
        "phase=replay",
        f"source={source}",
        f"session={session}",
        f"agent_key={agent_key}",
        f"mode={mode}",
        f"has_config={config is not None}",
        f"env_key_count={env_key_count}",
        f"config_path_count={config_path_count}",
    ]
    logger.info(" ".join(parts))


def log_reload_config_changes(
    logger: logging.Logger,
    *,
    env: Any,
    config: Any,
    previous_config: Any = None,
    reload_trace_id: str | None = None,
    source: str = "agent.reload_config",
    updated_param_keys: set[str] | list[str] | None = None,
    config_set_req_id: str | None = None,
) -> None:
    """Log modified config/env keys only (never values) for hot-reload tracing."""
    fields = format_reload_changed_keys(
        env=env,
        config=config,
        previous_config=previous_config,
        updated_param_keys=updated_param_keys,
    )
    if not fields:
        log_agent_config_hot_reload(
            logger,
            reload_trace_id=reload_trace_id,
            phase="changed_keys",
            source=source,
            note="no config/env keys in reload payload",
        )
        return

    if config_set_req_id:
        fields["config_set_req_id"] = config_set_req_id

    log_agent_config_hot_reload(
        logger,
        reload_trace_id=reload_trace_id,
        phase="changed_keys",
        source=source,
        **fields,
    )
