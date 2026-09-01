# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""WebChannel RPC handlers and shared constants (used by app gateway; single source with app.py)."""

from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import inspect
import json
import logging
import math
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import time
import base64
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import psutil as _psutil
    _HAS_PSUTIL = True
except ImportError:
    _psutil = None  # type: ignore[assignment]
    _HAS_PSUTIL = False
from openjiuwen.core.common.logging import LogManager
from openjiuwen.core.foundation.llm import Model, ProviderType
from openjiuwen.core.foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.llm.utils.provider_utils import is_openai_account_provider
from openjiuwen.extensions.external_provider.openai_auth.openai_account_auth import (
    OpenAIAccountAuthError,
    OpenAIAccountAuthManager,
    OpenAIAccountDeviceCode,
)
from openjiuwen.extensions.external_provider.openai_auth.openai_account_models import (
    OpenAIAccountModelCatalog,
    OpenAIAccountModelListError,
)

from jiuwenswarm.common.config import (
    DEFAULT_SWARMFLOW_ENABLED,
    EXTERNAL_CLI_AGENTS_CONFIG_PATH,
    SWARMFLOW_ENABLED_CONFIG_PATH,
    get_config,
    get_config_raw,
    get_default_models,
    replace_teams_in_config,
    update_default_models_in_config,
    update_health_check_in_config,
    update_channel_in_config,
    replace_channel_subsection_with_cleanup,
    update_browser_in_config,
    update_preferred_language_in_config,
    update_context_engine_enabled_in_config,
    update_default_model_provider_in_config,
    update_kv_cache_affinity_enabled_in_config,
    validate_persisted_kv_cache_affinity,
    update_kv_cache_release_enabled_in_config,
    update_skill_retrieval_in_config,
    update_symphony_in_config,
    update_permissions_enabled_in_config,
    update_setup_guide_enabled_in_config,
    update_enable_free_models_in_config,
    update_memory_forbidden_enabled_in_config,
    update_memory_forbidden_description_in_config,
    update_external_cli_agents_in_config,
    update_swarmflow_enabled_in_config,
    update_a2ui_in_config,
    update_updater_in_config,
    update_proactive_recommendation_in_config,
    update_skill_evolution_enabled_in_config,
)
from jiuwenswarm.common.kv_cache_affinity_config import (
    ASCEND_AFFINITY_PROVIDER,
    KVC_CONFIG_KEYS,
    default_model_provider_from_entries,
    is_affinity_enabled,
    normalize_affinity_request,
    parse_bool as parse_kvc_bool,
    set_default_model_provider_in_entries,
)
from jiuwenswarm.server.runtime.a2ui.integration import (
    get_a2ui_config_payload,
    get_default_a2ui_config_payload,
    validate_a2ui_config_update,
)
from jiuwenswarm.common.reasoning_config import (
    effective_endpoint_profile,
    validate_reasoning_level_for_model,
)
from jiuwenswarm.common.reasoning_injector import (
    build_reasoning_model_request_kwargs,
    core_has_context_window_field,
)
from jiuwenswarm.common.context_window import resolve_context_window_tokens
from jiuwenswarm.common.updater import DEFAULT_SOURCE_CONFIG, UpdaterService
from jiuwenswarm.common.utils import (
    get_env_file,
    get_root_dir,
)
from jiuwenswarm.dotenv_early import load_dotenv_runtime
from jiuwenswarm.common.work_mode import (
    DEFAULT_PROJECT_ID_CODE,
    DEFAULT_PROJECT_ID_WORK,
    DEFAULT_TUI_WORK_MODE,
    DEFAULT_WEB_WORK_MODE,
    SUPPORTED_WORK_MODES,
    is_default_project_id,
)
from jiuwenswarm.common.version import __version__

for _jiuwen_log in LogManager.get_all_loggers().values():
    _jiuwen_log.set_level(logging.INFO)

logger = logging.getLogger(__name__)


_WEB_CONFIG_RELOAD_CHANNEL_ID = "web"
_MODEL_RELOAD_ENV_KEYS = {
    "MODEL_PROVIDER",
    "MODEL_NAME",
    "API_BASE",
    "API_KEY",
}
_MULTIMODAL_RELOAD_ENV_KEYS = {
    "VIDEO_PROVIDER",
    "VIDEO_MODEL_NAME",
    "VIDEO_API_BASE",
    "VIDEO_API_KEY",
    "VIDEO_ENDPOINT_PROFILE",
    "AUDIO_PROVIDER",
    "AUDIO_MODEL_NAME",
    "AUDIO_API_BASE",
    "AUDIO_API_KEY",
    "AUDIO_ENDPOINT_PROFILE",
    "VISION_PROVIDER",
    "VISION_MODEL_NAME",
    "VISION_API_BASE",
    "VISION_API_KEY",
    "VISION_ENDPOINT_PROFILE",
    "VISION_ENABLED",
    "AUDIO_ENABLED",
    "VIDEO_ENABLED",
    "VIDEO_GEN_PROVIDER",
    "VIDEO_GEN_MODEL_NAME",
    "VIDEO_GEN_API_BASE",
    "VIDEO_GEN_API_KEY",
}


@dataclass(frozen=True)
class _ConfigChangeSet:
    env_updates: dict[str, str]
    yaml_updated: list[str]
    force: bool = False

    @property
    def changed(self) -> bool:
        return self.force or bool(self.env_updates or self.yaml_updated)

    @property
    def updated_keys(self) -> set[str]:
        return set(self.env_updates.keys()) | set(self.yaml_updated)

    @property
    def reload_scopes(self) -> set[str]:
        scopes: set[str] = set()
        if _MODEL_RELOAD_ENV_KEYS & set(self.env_updates):
            scopes.add("model")
        if _MULTIMODAL_RELOAD_ENV_KEYS & set(self.env_updates):
            scopes.add("multimodal")
        for key in self.yaml_updated:
            key_text = str(key)
            if key_text in {"models.defaults"} or key_text.startswith("models."):
                scopes.add("model")
            elif key_text in {"modes.team", "agents", "team"}:
                scopes.add("team")
            elif key_text.startswith("permissions"):
                scopes.add("permissions")
            elif key_text.startswith("proactive_recommendation"):
                scopes.add("proactive")
            elif key_text.startswith("symphony") or key_text.startswith("skill_retrieval"):
                scopes.add("agent_runtime")
            elif key_text.startswith("a2ui_") or key_text == "setup_guide_enabled":
                scopes.add("web_ui")
            else:
                scopes.add("agent_runtime")
        if self.force and not scopes:
            scopes.add("agent_runtime")
        return scopes

    @property
    def reload_options(self) -> dict[str, Any]:
        return {
            "target_channel_id": _WEB_CONFIG_RELOAD_CHANNEL_ID,
            "reload_scopes": sorted(self.reload_scopes),
        }


@dataclass(frozen=True)
class _ConfigApplyResult:
    env_updates: dict[str, str]
    yaml_updated: list[str]
    codex_dependency_install: dict[str, Any] | None = None
    external_cli_dependency_installs: dict[str, dict[str, Any]] | None = None


_CODEX_DEPENDENCY_INSTALL_LOCK = threading.Lock()
_CODEX_DEPENDENCY_INSTALL_STATUS: dict[str, Any] = {
    "status": "idle",
    "phase": "idle",
    "error": "",
    "last_log": "",
    "log_tail": [],
    "started_at": 0.0,
    "finished_at": 0.0,
    "updated_at": 0.0,
    "downloaded_bytes": 0,
    "total_bytes": 0,
    "bytes_per_second": 0.0,
    "eta_seconds": 0.0,
    "artifact_index": 0,
    "artifact_count": 0,
    "current_package": "",
    "current_version": "",
    "download_attempt": 0,
    "download_max_attempts": 0,
    "switching_source": False,
}
_CODEX_DEPENDENCY_INSTALL_LOG_TAIL_LIMIT = 8
_OPTIONAL_DEPENDENCY_INSTALL_TIMEOUT_SECONDS = 60 * 60
_CLAUDE_DEPENDENCY_INSTALL_LOCK = threading.Lock()
_CLAUDE_DEPENDENCY_INSTALL_STATUS: dict[str, Any] = dict(_CODEX_DEPENDENCY_INSTALL_STATUS)
_EXTERNAL_CLI_DEPENDENCY_INSTALL_LOCKS = {
    "claude": _CLAUDE_DEPENDENCY_INSTALL_LOCK,
    "codex": _CODEX_DEPENDENCY_INSTALL_LOCK,
}
_EXTERNAL_CLI_DEPENDENCY_INSTALL_STATUSES = {
    "claude": _CLAUDE_DEPENDENCY_INSTALL_STATUS,
    "codex": _CODEX_DEPENDENCY_INSTALL_STATUS,
}


_PROJECT_ROOT = get_root_dir()
_ENV_FILE = get_env_file()
load_dotenv_runtime(dotenv_path=_ENV_FILE, override=True)


_ENV_VAR_PLACEHOLDER_RE = re.compile(r"^\$\{([^:}]+)(?::-([^}]*))?\}$")
_OPENAI_ACCOUNT_LOGIN_MAX_TTL_SECONDS = 5 * 60
_OPENAI_ACCOUNT_LOGIN_JOBS: dict[str, "_OpenAIAccountLoginJob"] = {}
_OPENAI_ACCOUNT_LOGIN_JOBS_LOCK = threading.RLock()
_OPENAI_ACCOUNT_AUTH_OPERATION_LOCK = threading.RLock()
_OPENAI_ACCOUNT_LOCAL_ERRORS = (OSError, TypeError, ValueError)


@dataclass
class _OpenAIAccountLoginJob:
    device_code: OpenAIAccountDeviceCode
    created_at: float
    expires_at: float


def _cleanup_openai_account_login_jobs(now: float | None = None) -> None:
    current = time.time() if now is None else now
    with _OPENAI_ACCOUNT_LOGIN_JOBS_LOCK:
        expired = [
            login_id
            for login_id, job in _OPENAI_ACCOUNT_LOGIN_JOBS.items()
            if job.expires_at <= current
        ]
        for login_id in expired:
            _OPENAI_ACCOUNT_LOGIN_JOBS.pop(login_id, None)


def _latest_openai_account_login_job(
        now: float | None = None,
) -> tuple[str, "_OpenAIAccountLoginJob"] | None:
    current = time.time() if now is None else now
    with _OPENAI_ACCOUNT_LOGIN_JOBS_LOCK:
        _cleanup_openai_account_login_jobs(current)
        if not _OPENAI_ACCOUNT_LOGIN_JOBS:
            return None
        return max(_OPENAI_ACCOUNT_LOGIN_JOBS.items(), key=lambda item: item[1].created_at)


def _get_openai_account_login_job(
        login_id: str,
        now: float | None = None,
) -> "_OpenAIAccountLoginJob" | None:
    current = time.time() if now is None else now
    with _OPENAI_ACCOUNT_LOGIN_JOBS_LOCK:
        _cleanup_openai_account_login_jobs(current)
        return _OPENAI_ACCOUNT_LOGIN_JOBS.get(login_id)


def _store_openai_account_login_job(
        login_id: str,
        job: "_OpenAIAccountLoginJob",
        now: float | None = None,
) -> None:
    current = time.time() if now is None else now
    with _OPENAI_ACCOUNT_LOGIN_JOBS_LOCK:
        _cleanup_openai_account_login_jobs(current)
        _OPENAI_ACCOUNT_LOGIN_JOBS[login_id] = job


def _remove_openai_account_login_job(login_id: str) -> None:
    with _OPENAI_ACCOUNT_LOGIN_JOBS_LOCK:
        _OPENAI_ACCOUNT_LOGIN_JOBS.pop(login_id, None)


def _clear_openai_account_login_jobs() -> None:
    with _OPENAI_ACCOUNT_LOGIN_JOBS_LOCK:
        _OPENAI_ACCOUNT_LOGIN_JOBS.clear()


def _openai_account_auth_status_payload(
        manager: OpenAIAccountAuthManager | None = None,
) -> dict[str, Any]:
    with _OPENAI_ACCOUNT_AUTH_OPERATION_LOCK:
        auth_manager = manager or OpenAIAccountAuthManager()
        status = auth_manager.status()
        return {
            "authenticated": status.authenticated,
            "auth_path": str(status.auth_path),
            "has_refresh_token": status.has_refresh_token,
            "expires_at": status.expires_at,
            "needs_refresh": status.needs_refresh,
            "error": status.error,
            "base_url": auth_manager.base_url,
        }


def _openai_account_login_payload(
        login_id: str,
        job: "_OpenAIAccountLoginJob",
        manager: OpenAIAccountAuthManager | None = None,
        now: float | None = None,
) -> dict[str, Any]:
    current = time.time() if now is None else now
    device_code = job.device_code
    return {
        "status": "pending",
        "login_id": login_id,
        "user_code": device_code.user_code,
        "verification_uri": device_code.verification_uri,
        "interval": device_code.interval,
        "expires_in": max(0, int(job.expires_at - current)),
        "expires_at": job.expires_at,
        "auth": _openai_account_auth_status_payload(manager),
    }


def _openai_account_pending_login_payload(
        manager: OpenAIAccountAuthManager | None = None,
) -> dict[str, Any]:
    with _OPENAI_ACCOUNT_AUTH_OPERATION_LOCK:
        current = time.time()
        latest_job = _latest_openai_account_login_job(current)
        if latest_job is None:
            return {
                "status": "none",
                "auth": _openai_account_auth_status_payload(manager),
            }
        login_id, job = latest_job
        return _openai_account_login_payload(login_id, job, manager, current)


def _openai_account_start_login_payload() -> dict[str, Any]:
    with _OPENAI_ACCOUNT_AUTH_OPERATION_LOCK:
        manager = OpenAIAccountAuthManager()
        now = time.time()
        latest_job = _latest_openai_account_login_job(now)
        if latest_job is not None:
            login_id, job = latest_job
            return _openai_account_login_payload(login_id, job, manager, now)

        device_code = manager.start_device_login()
        now = time.time()
        raw_expires_in = device_code.expires_in or _OPENAI_ACCOUNT_LOGIN_MAX_TTL_SECONDS
        expires_in = min(int(raw_expires_in), _OPENAI_ACCOUNT_LOGIN_MAX_TTL_SECONDS)
        expires_at = now + expires_in
        login_id = uuid.uuid4().hex
        job = _OpenAIAccountLoginJob(
            device_code=device_code,
            created_at=now,
            expires_at=expires_at,
        )
        _store_openai_account_login_job(login_id, job, now)
        return _openai_account_login_payload(login_id, job, manager, now)


def _openai_account_poll_login_payload(login_id: str) -> dict[str, Any]:
    with _OPENAI_ACCOUNT_AUTH_OPERATION_LOCK:
        now = time.time()
        job = _get_openai_account_login_job(login_id, now)
        if job is None:
            return {"status": "expired", "authenticated": False}

        manager = OpenAIAccountAuthManager()
        try:
            tokens = manager.poll_device_login(job.device_code)
        except OpenAIAccountAuthError as exc:
            if exc.relogin_required:
                _remove_openai_account_login_job(login_id)
            raise
        if tokens is None:
            return {
                "status": "pending",
                "authenticated": False,
                "expires_at": job.expires_at,
            }

        _remove_openai_account_login_job(login_id)
        return {
            "status": "authenticated",
            "authenticated": True,
            "auth": _openai_account_auth_status_payload(manager),
        }


def _openai_account_logout_payload() -> dict[str, Any]:
    with _OPENAI_ACCOUNT_AUTH_OPERATION_LOCK:
        manager = OpenAIAccountAuthManager()
        _clear_openai_account_login_jobs()
        logged_out = manager.logout()
        return {
            "logged_out": logged_out,
            "auth": _openai_account_auth_status_payload(manager),
        }


def _openai_account_models_payload() -> dict[str, Any]:
    with _OPENAI_ACCOUNT_AUTH_OPERATION_LOCK:
        manager = OpenAIAccountAuthManager()
        catalog = OpenAIAccountModelCatalog(base_url=manager.base_url)
        models = catalog.list_model_ids(auth_manager=manager)
        return {
            "models": models,
            "base_url": manager.base_url,
            "auth": _openai_account_auth_status_payload(manager),
        }


def _openai_account_auth_error_payload(exc: OpenAIAccountAuthError) -> dict[str, Any]:
    return {
        "status": "error",
        "error": str(exc),
        "code": exc.code,
        "status_code": exc.status_code,
        "relogin_required": exc.relogin_required,
    }


def _is_env_var_placeholder(value: Any) -> bool:
    return isinstance(value, str) and bool(_ENV_VAR_PLACEHOLDER_RE.match(value.strip()))


def _values_match(parsed_val: Any, resolved_val: Any) -> bool:
    """Compare a frontend-sent value against the resolved value of a model entry.

    Numeric and stringified env-var output (e.g. ``${TEMP:-0.95}`` resolves to ``"0.95"``)
    are normalized so that ``0.95 == "0.95"`` is treated as "unchanged".
    """
    if isinstance(parsed_val, bool) or isinstance(resolved_val, bool):
        return bool(parsed_val) == bool(resolved_val)
    if parsed_val is None and resolved_val is None:
        return True
    try:
        return float(parsed_val) == float(resolved_val)
    except (TypeError, ValueError):
        pass
    return str(parsed_val if parsed_val is not None else "") == str(
        resolved_val if resolved_val is not None else ""
    )


def _read_proc_meminfo() -> tuple[float, float, float]:
    """Fallback for platforms without psutil: read /proc/meminfo.

    Returns (rss_mb, total_mb, available_mb).  Returns (0, 0, 0) on failure.
    """
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1])  # kB
        total_mb = info.get("MemTotal", 0) / 1024
        available_mb = info.get("MemAvailable", info.get("MemFree", 0)) / 1024
        # RSS from /proc/self/status
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss_mb = int(line.split()[1]) / 1024  # kB → MB
                    return rss_mb, total_mb, available_mb
        return 0.0, total_mb, available_mb
    except OSError:
        return 0.0, 0.0, 0.0


def _serialize_reasoning_level(value: Any) -> Any:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString
    # Always emit a quoted YAML string so the same field never round-trips
    # as a mix of plain scalars and quoted scalars.
    return DoubleQuotedScalarString(text)


def _reasoning_level_display(value: Any) -> str:
    """Normalize a stored reasoning_level to its canonical string level.

    Legacy YAML entries hold bare ``on``/``off`` scalars which YAML 1.1
    loaders parse into booleans; map them back so the frontend and the
    replace_all change detection never see raw booleans.
    """
    if value is True:
        return "on"
    if value is False:
        return "off"
    return str(value or "").strip()


def _merge_models_for_replace_all(
        parsed: list[dict[str, Any]],
        raw_defaults: list[dict[str, Any]],
        resolved_defaults: list[dict[str, Any]],
        crypto: Any,
) -> list[dict[str, Any]]:
    """Merge the frontend draft with the persisted YAML so that env-var placeholders
    (``${VAR:-default}``) survive when the user edits unrelated fields.

    For each frontend entry that carries an ``origin_index`` pointing at a still-existing
    persisted entry, we deep-copy the raw entry (preserving placeholders, custom_headers,
    etc.) and only overwrite the fields whose value differs from the resolved snapshot
    the frontend was originally shown. New entries (no ``origin_index``) fall back to
    encrypting/storing the frontend payload verbatim.
    """
    import copy as _copy

    out: list[dict[str, Any]] = []
    for item in parsed:
        origin_idx = item.get("origin_index")
        raw_entry = None
        resolved_entry = None
        if isinstance(origin_idx, int) and 0 <= origin_idx < len(raw_defaults):
            raw_entry = raw_defaults[origin_idx]
            if 0 <= origin_idx < len(resolved_defaults):
                resolved_entry = resolved_defaults[origin_idx]

        if raw_entry is not None and isinstance(raw_entry, dict):
            new_entry = _copy.deepcopy(raw_entry)
            new_mcc = new_entry.setdefault("model_client_config", {})
            new_mco = new_entry.setdefault("model_config_obj", {})
            resolved_mcc = (resolved_entry or {}).get("model_client_config", {}) or {}
            resolved_mco = (resolved_entry or {}).get("model_config_obj", {}) or {}

            if not _values_match(item["model_name"], resolved_mcc.get("model_name")):
                new_mcc["model_name"] = item["model_name"]
            if not _values_match(item["api_base"], resolved_mcc.get("api_base")):
                new_mcc["api_base"] = item["api_base"]
            # client_provider: 当 YAML 仍是 ${MODEL_PROVIDER} 占位符时，其解析值会与前端
            # 选择（如 OpenAI）一致而被误判为"未改"，导致首次配置后占位符残留。只要原值是
            # 占位符就用前端值固化它。
            if item["model_provider"] and (
                _is_env_var_placeholder(new_mcc.get("client_provider"))
                or not _values_match(item["model_provider"], resolved_mcc.get("client_provider"))
            ):
                new_mcc["client_provider"] = item["model_provider"]
            if not _values_match(item["temperature"], resolved_mco.get("temperature")):
                new_mco["temperature"] = item["temperature"]
            reasoning_level = str(item.get("reasoning_level") or "").strip()
            # 不能用 _values_match：legacy YAML 1.1 会把裸 on/off 读成布尔，
            # 其布尔分支使 bool("")==bool(False) 成立，「清空档位」会被误判为
            # 未修改而让旧值残留。按规范化后的字符串比较。
            if reasoning_level != _reasoning_level_display(resolved_mco.get("reasoning_level")):
                if reasoning_level:
                    new_mco["reasoning_level"] = _serialize_reasoning_level(reasoning_level)
                else:
                    new_mco.pop("reasoning_level", None)
            if not _values_match(item["timeout"], resolved_mcc.get("timeout")):
                new_mcc["timeout"] = item["timeout"]
            if not _values_match(item["alias"], (resolved_entry or {}).get("alias")):
                new_entry["alias"] = item["alias"]
            # vendor_key + plan: persist the exact provider selection identity
            # into model_client_config (or clear it).
            if item.get("vendor_key"):
                new_mcc["vendor_key"] = item["vendor_key"]
            else:
                new_mcc.pop("vendor_key", None)
            if item.get("plan"):
                new_mcc["plan"] = item["plan"]
            else:
                new_mcc.pop("plan", None)
            # endpoint_profile: OpenAI 协议端点方言(deepseek/openrouter/dashscope/...)。
            # 前端透传则落库；不传则清掉(避免残留旧方言)。Anthropic 协议时此字段被 core 忽略。
            if item.get("endpoint_profile"):
                new_mcc["endpoint_profile"] = item["endpoint_profile"]
            else:
                new_mcc.pop("endpoint_profile", None)
            new_entry["is_default"] = item["is_default"]
            # api_key: resolved holds the decrypted plaintext shown to the frontend.
            # Unchanged → keep raw (placeholder or ciphertext); changed → encrypt new value.
            if not _values_match(item["api_key"], resolved_mcc.get("api_key")):
                new_mcc["api_key"] = (
                    crypto.encrypt(item["api_key"]) if (item["api_key"] and crypto) else item["api_key"]
                )
        else:
            # New entry — frontend payload is the source of truth.
            new_entry = {
                "model_client_config": {
                    "api_base": item["api_base"],
                    "api_key": (
                        crypto.encrypt(item["api_key"]) if (item["api_key"] and crypto) else item["api_key"]
                    ),
                    "model_name": item["model_name"],
                    "client_provider": item["model_provider"],
                    "timeout": item["timeout"],
                    "verify_ssl": item["verify_ssl"],
                    # vendor_key + plan identify the exact registry preset so
                    # the UI can restore the provider selection after reload.
                    **({"vendor_key": item["vendor_key"]} if item.get("vendor_key") else {}),
                    **({"plan": item["plan"]} if item.get("plan") else {}),
                    # endpoint_profile: OpenAI 协议端点方言(透传；Anthropic 时 core 忽略)。
                    **({"endpoint_profile": item["endpoint_profile"]} if item.get("endpoint_profile") else {}),
                },
                "model_config_obj": {
                    "temperature": item["temperature"],
                    **({"reasoning_level": _serialize_reasoning_level(item.get("reasoning_level"))}
                       if item.get("reasoning_level") else {}),
                },
                "is_default": item["is_default"],
                "alias": item["alias"],
            }

        out.append(new_entry)
    return out


# 仅满足 Channel 构造所需，不入队、不路由；仅用 channel_manager + message_handler 做入站/出站
class _DummyBus:
    async def publish_user_messages(self, msg):  # noqa: ANN001, ARG002
        pass

    async def route_incoming_message(self, msg):  # noqa: ANN001, ARG002
        pass

    async def route_user_message(self, msg):
        pass


# 仅转发到 Agent 的 Web method
_FORWARD_REQ_METHODS = frozenset({
    "initialize",
    "session.switch",
    "acp.tool_response",
    "team.delete",
    "command.goal",
    "command.btw",
    "command.compact",
    "chat.send",
    "chat.interrupt",
    "chat.resume",
    "chat.user_answer",
    "history.get",
    # "tts.synthesize",
    "skills.marketplace.list",
    "skills.list",
    "skills.installed",
    "skills.get",
    "skills.versions.list",
    "skills.files.list",
    "skills.files.get",
    "skills.rebuild",
    "skills.toggle",
    "skills.install",
    "skills.import_local",
    "skills.import_upload",
    "skills.create_from_knowledge",
    "skills.marketplace.add",
    "skills.marketplace.remove",
    "skills.marketplace.toggle",
    "skills.uninstall",
    "skills.online_search.search",
    "skills.skillnet.search",
    "skills.skillnet.install",
    "skills.skillnet.install_status",
    "skills.skillnet.evaluate",
    "skills.clawhub.get_token",
    "skills.clawhub.set_token",
    "skills.clawhub.search",
    "skills.clawhub.download",
    "skills.teamskillshub.info",
    "skills.teamskillshub.init",
    "skills.teamskillshub.validate",
    "skills.teamskillshub.pack",
    "skills.teamskillshub.search",
    "skills.swarmskillshub.recommend",
    "skills.teamskillshub.install",
    "skills.teamskillshub.publish",
    "skills.teamskillshub.delete",
    "skills.swarmskillshub.detail",
    "skills.retrieval.status",
    "skills.retrieval.index_build",
    "skills.retrieval.index_cancel",
    "skills.retrieval.search",
    "skills.retrieval.tree",
    "skills.evolution.status",
    "skills.evolution.get",
    "skills.evolution.save",
    "skills.graph.build",
    "skills.graph.status",
    "skills.graph.get",
    "skills.graph.cancel",
    "personal_context.runtime.status",
    "personal_context.runtime.start_collection",
    "personal_context.runtime.stop_collection",
    "personal_context.runtime.start_agent_use",
    "personal_context.runtime.stop_agent_use",
    "personal_context.runtime.get_config",
    "personal_context.runtime.patch_config",
    "personal_context.runtime.select_model",
    "personal_context.fetch.list_services",
    "personal_context.fetch.create_service",
    "personal_context.fetch.delete_service",
    "personal_context.fetch.patch_service",
    "personal_context.fetch.start_service",
    "personal_context.fetch.stop_service",
    "personal_context.fetch.run_all",
    "personal_context.fetch.run_one",
    "personal_context.fetch.get_run_status",
    "personal_context.fetch.get_authorization_status",
    "personal_context.fetch.authorize_provider",
    "personal_context.context.stream_graph",
    "personal_context.context.stream_tree",
    "personal_context.context.search_pages",
    "personal_context.context.get_node",
    "personal_context.context.get_source",
    "plugins.list",
    "plugins.install",
    "plugins.uninstall",
    "plugins.enable",
    "plugins.disable",
    "plugins.reload",
    "agent_groups.list",
    "agent_groups.show",
    "agent_groups.file.list",
    "agent_groups.file.read",
    "agent_groups.create",
    "agent_groups.import_local",
    "agent_groups.install",
    "agent_groups.uninstall",
    "agent_templates.list",
    "agent_templates.show",
    "agent_templates.file.list",
    "agent_templates.file.read",
    "agent_templates.create",
    "agent_templates.import_local",
    "agent_templates.install",
    "agent_templates.uninstall",
    "plugin_packages.list",
    "plugin_packages.show",
    "plugin_packages.create",
    "plugin_packages.import_local",
    "plugin_packages.install",
    "plugin_packages.uninstall",
    "mcp.list",
    "mcp.show",
    "mcp.connect",
    "mcp.wait_auth",
    "mcp.disconnect",
    "mcp.register_custom",
    "mcp.delete_custom",
    "mcp.save_credentials",
    "extensions.list",
    "extensions.import",
    "extensions.delete",
    "extensions.toggle",
    "team.templates.list",
    "team.bindings.list",
    "team.binding.create",
    "team.binding.generate",
    "team.session.bind",
    "team.snapshot",
    "team.history.get",
    "team.mq.publish",
    # Agent configuration
    "agents.list",
    "agents.get",
    "agents.create",
    "agents.update",
    "agents.delete",
    "agents.enable",
    "agents.disable",
    "agents.tools_list",
    # Schedule task management
    "schedule.check_config",
    "schedule.update_config",
    "schedule.create",
    "schedule.run",
    "schedule.list",
    "schedule.status",
    "schedule.logs",
    "schedule.cancel",
    "schedule.delete",
    "issue.watch_once",
    "issue.state.list",
    "issue.matrix",
    "issue.delete",
})

_FORWARD_NO_LOCAL_HANDLER_METHODS = frozenset({
    "initialize",
    "session.switch",
    "acp.tool_response",
    "team.templates.list",
    "team.bindings.list",
    "team.binding.create",
    "team.binding.generate",
    "team.session.bind",
    "team.delete",
    "command.goal",
    "command.btw",
    "command.compact",
    "team.snapshot",
    "team.history.get",
    "team.mq.publish",
    "skills.marketplace.list",
    "skills.list",
    "skills.installed",
    "skills.get",
    "skills.versions.list",
    "skills.files.list",
    "skills.files.get",
    "skills.rebuild",
    "skills.toggle",
    "skills.install",
    "skills.import_local",
    "skills.import_upload",
    "skills.create_from_knowledge",
    "skills.marketplace.add",
    "skills.marketplace.remove",
    "skills.marketplace.toggle",
    "skills.uninstall",
    "skills.online_search.search",
    "skills.skillnet.search",
    "skills.skillnet.install",
    "skills.skillnet.install_status",
    "skills.skillnet.evaluate",
    "skills.clawhub.get_token",
    "skills.clawhub.set_token",
    "skills.clawhub.search",
    "skills.clawhub.download",
    "skills.teamskillshub.info",
    "skills.teamskillshub.init",
    "skills.teamskillshub.validate",
    "skills.teamskillshub.pack",
    "skills.teamskillshub.search",
    "skills.swarmskillshub.recommend",
    "skills.teamskillshub.install",
    "skills.teamskillshub.publish",
    "skills.teamskillshub.delete",
    "skills.swarmskillshub.detail",
    "skills.retrieval.status",
    "skills.retrieval.index_build",
    "skills.retrieval.index_cancel",
    "skills.retrieval.search",
    "skills.retrieval.tree",
    "skills.evolution.status",
    "skills.evolution.get",
    "skills.evolution.save",
    "skills.graph.build",
    "skills.graph.status",
    "skills.graph.get",
    "skills.graph.cancel",
    "personal_context.runtime.status",
    "personal_context.runtime.start_collection",
    "personal_context.runtime.stop_collection",
    "personal_context.runtime.start_agent_use",
    "personal_context.runtime.stop_agent_use",
    "personal_context.runtime.get_config",
    "personal_context.runtime.patch_config",
    "personal_context.runtime.select_model",
    "personal_context.fetch.list_services",
    "personal_context.fetch.create_service",
    "personal_context.fetch.delete_service",
    "personal_context.fetch.patch_service",
    "personal_context.fetch.start_service",
    "personal_context.fetch.stop_service",
    "personal_context.fetch.run_all",
    "personal_context.fetch.run_one",
    "personal_context.fetch.get_run_status",
    "personal_context.fetch.get_authorization_status",
    "personal_context.fetch.authorize_provider",
    "personal_context.context.stream_graph",
    "personal_context.context.stream_tree",
    "personal_context.context.search_pages",
    "personal_context.context.get_node",
    "personal_context.context.get_source",
    "plugins.list",
    "plugins.install",
    "plugins.uninstall",
    "plugins.enable",
    "plugins.disable",
    "plugins.reload",
    "agent_groups.list",
    "agent_groups.show",
    "agent_groups.file.list",
    "agent_groups.file.read",
    "agent_groups.create",
    "agent_groups.import_local",
    "agent_groups.install",
    "agent_groups.uninstall",
    "agent_templates.list",
    "agent_templates.show",
    "agent_templates.file.list",
    "agent_templates.file.read",
    "agent_templates.create",
    "agent_templates.import_local",
    "agent_templates.install",
    "agent_templates.uninstall",
    "plugin_packages.list",
    "plugin_packages.show",
    "plugin_packages.create",
    "plugin_packages.import_local",
    "plugin_packages.install",
    "plugin_packages.uninstall",
    "mcp.list",
    "mcp.show",
    "mcp.connect",
    "mcp.wait_auth",
    "mcp.disconnect",
    "mcp.register_custom",
    "mcp.delete_custom",
    "mcp.save_credentials",
    "extensions.list",
    "extensions.import",
    "extensions.delete",
    "extensions.toggle",
    # Agent configuration
    "agents.list",
    "agents.get",
    "agents.create",
    "agents.update",
    "agents.delete",
    "agents.enable",
    "agents.disable",
    "agents.tools_list",
    "external_cli.detect",
    "external_cli.codex_install_status",
})

# 配置信息：config.get 返回、config.set 可修改的键（前端 param 名 -> 环境变量名）
# default 模型 + video/audio/vision/video_gen 多模型
_CONFIG_SET_ENV_MAP = {
    # default 模型（主对话）
    "model_provider": "MODEL_PROVIDER",
    "model": "MODEL_NAME",
    "api_base": "API_BASE",
    "api_key": "API_KEY",
    "endpoint_profile": "ENDPOINT_PROFILE",
    # video vision 模型（视频理解）
    "video_api_base": "VIDEO_API_BASE",
    "video_api_key": "VIDEO_API_KEY",
    "video_model": "VIDEO_MODEL_NAME",
    "video_provider": "VIDEO_PROVIDER",
    "video_endpoint_profile": "VIDEO_ENDPOINT_PROFILE",
    "video_vendor_key": "VIDEO_VENDOR_KEY",
    "video_plan": "VIDEO_PLAN",
    "video_enabled": "VIDEO_ENABLED",
    # audio 模型
    "audio_api_base": "AUDIO_API_BASE",
    "audio_api_key": "AUDIO_API_KEY",
    "audio_model": "AUDIO_MODEL_NAME",
    "audio_provider": "AUDIO_PROVIDER",
    "audio_endpoint_profile": "AUDIO_ENDPOINT_PROFILE",
    "audio_vendor_key": "AUDIO_VENDOR_KEY",
    "audio_plan": "AUDIO_PLAN",
    "audio_enabled": "AUDIO_ENABLED",
    # vision 模型
    "vision_api_base": "VISION_API_BASE",
    "vision_api_key": "VISION_API_KEY",
    "vision_model": "VISION_MODEL_NAME",
    "vision_provider": "VISION_PROVIDER",
    "vision_endpoint_profile": "VISION_ENDPOINT_PROFILE",
    "vision_vendor_key": "VISION_VENDOR_KEY",
    "vision_plan": "VISION_PLAN",
    "vision_enabled": "VISION_ENABLED",
    # video_gen model (text-to-video)
    "video_gen_api_base": "VIDEO_GEN_API_BASE",
    "video_gen_api_key": "VIDEO_GEN_API_KEY",
    "video_gen_model": "VIDEO_GEN_MODEL_NAME",
    "video_gen_provider": "VIDEO_GEN_PROVIDER",
    # 其他
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
    # agents
    "skills": "SKILLS",
    "max_iterations": "MAX_ITERATIONS",
    "completion_timeout": "COMPLETION_TIMEOUT",
    # team
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
# 配置项键名列表，用于日志等说明
CONFIG_KEYS = tuple(_CONFIG_SET_ENV_MAP.keys())

# 来自 config.yaml 的配置项（前端 param 名 -> config.yaml 路径）
_CONFIG_YAML_KEYS = frozenset({
    "context_engine_enabled",
    "kv_cache_release_enabled",
    "kv_cache_affinity_enabled",
    "permissions_enabled",
    "memory_forbidden_enabled",
    "memory_forbidden_description",
    "a2ui_enabled",
    "proactive_recommendation_enabled",
    "proactive_recommendation_max_recommend_per_day",
    "proactive_recommendation_max_rounds_per_tick",
    "swarmflow_enabled",
    "external_cli_agent_claude_enabled",
    "external_cli_agent_claude_use_builtin",
    "external_cli_agent_claude_cli_path",
    "external_cli_agent_codex_enabled",
    "external_cli_agent_codex_use_builtin",
    "external_cli_agent_codex_cli_path",
    "setup_guide_enabled",
    "skill_evolution",
    "enable_free_models",
})
_EXTERNAL_CLI_AGENT_CONFIG_KEYS = frozenset({
    "external_cli_agent_claude_enabled",
    "external_cli_agent_claude_use_builtin",
    "external_cli_agent_claude_cli_path",
    "external_cli_agent_codex_enabled",
    "external_cli_agent_codex_use_builtin",
    "external_cli_agent_codex_cli_path",
})
_EXTERNAL_CLI_AGENT_KINDS = ("claude", "codex")
_DEFAULT_EXTERNAL_CLI_PUBLISH_HOST = "127.0.0.1"
_DEFAULT_EXTERNAL_CLI_PUBLISH_PORT = "19000"
_EXTERNAL_CLI_PUBLISH_PATH = "/ws"
_UNSUPPORTED_WINDOWS_CLI_SUFFIXES = {".bat", ".cmd", ".ps1"}

# 微信通道数值参数的取值范围：(下限, 上限, 是否必须为整数)。均为秒，必须为有限正数。
# 用于 channel.wechat.set_conf 写盘前校验，拒绝负数 / 0 / 极大值 / 浮点越界 / 非数字，
# 避免非法值落盘后导致后台轮询忙循环轰炸接口或退避过久使通道僵死。
_WECHAT_NUMERIC_BOUNDS: dict[str, tuple[float, float, bool]] = {
    "qrcode_poll_interval_sec": (0.1, 3600.0, False),
    "long_poll_timeout_sec": (1, 600, True),
    "backoff_base_sec": (0.1, 3600.0, False),
    "backoff_max_sec": (0.1, 3600.0, False),
}


def _validate_wechat_numeric_params(params: dict) -> str | None:
    """校验微信通道四个数值参数。合法返回 None，非法返回中文错误描述。

    规则：出现在 params 中的字段必须为有限正数并落在各自范围内；
    ``long_poll_timeout_sec`` 必须为整数；且 ``backoff_max_sec`` 不得小于
    ``backoff_base_sec``（两者同时出现时才校验此跨字段约束）。
    仅校验存在的键，缺省字段交由默认值处理。
    """
    def _as_number(value: Any) -> float | None:
        # bool 是 int 的子类，需显式排除，避免 True/False 被当作 1/0 通过。
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        val = float(value)
        return val if math.isfinite(val) else None

    for key, (lo, hi, is_int) in _WECHAT_NUMERIC_BOUNDS.items():
        if key not in params:
            continue
        val = _as_number(params[key])
        if val is None:
            return f"{key} 需为有限数值"
        if is_int and val != int(val):
            return f"{key} 需为整数"
        if not (lo <= val <= hi):
            return f"{key} 需在 {lo}–{hi} 之间"

    base = _as_number(params.get("backoff_base_sec"))
    mx = _as_number(params.get("backoff_max_sec"))
    if base is not None and mx is not None and mx < base:
        return "backoff_max_sec 不得小于 backoff_base_sec"
    return None


_SYMPHONY_CONFIG_SPECS: dict[str, tuple[tuple[str, ...], str, Any]] = {
    "symphony_enabled": (("enabled",), "bool", False),
}
_SYMPHONY_CONFIG_KEYS = tuple(_SYMPHONY_CONFIG_SPECS.keys())
_SKILL_RETRIEVAL_CONFIG_SPECS: dict[str, tuple[tuple[str, ...], str, Any]] = {
    "skill_retrieval_enabled": (("enabled",), "bool", False),
    "skill_retrieval_index_enabled": (("index", "enabled"), "bool", False),
    "skill_retrieval_max_results": (("discovery", "max_results"), "int", 10),
    "skill_retrieval_max_output_chars": (
        ("discovery", "max_output_chars"),
        "output_chars",
        12000,
    ),
    "skill_retrieval_max_list_entries": (
        ("discovery", "max_list_entries"),
        "int",
        40,
    ),
    "skill_retrieval_incremental_notice_max_chars": (
        ("discovery", "incremental_notice_max_chars"),
        "int",
        4000,
    ),
}
_SKILL_RETRIEVAL_CONFIG_KEYS = tuple(_SKILL_RETRIEVAL_CONFIG_SPECS.keys())


def _coerce_config_panel_value(value: Any, value_type: str, default: Any) -> Any:
    if value_type == "bool":
        return str(value).strip().lower() in ("true", "1", "yes", "on", "enabled")
    if value_type == "int":
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return default
    if value_type == "non_negative_int":
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return default
    if value_type == "raw_int":
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if value_type == "float":
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return default
    if value_type == "ratio":
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return parsed if 0.0 < parsed <= 1.0 else default
    if value_type == "output_chars":
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_output_chars must be an integer from 512 to 48000") from exc
        if not 512 <= parsed <= 48_000:
            raise ValueError("max_output_chars must be from 512 to 48000")
        return parsed
    return str(value if value is not None else default)


def _set_nested_config_value(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = target
    for segment in path[:-1]:
        child = current.get(segment)
        if not isinstance(child, dict):
            child = {}
            current[segment] = child
        current = child
    current[path[-1]] = value


def _get_nested_config_value(source: dict[str, Any], path: tuple[str, ...], default: Any) -> Any:
    current: Any = source
    for segment in path:
        if not isinstance(current, dict) or segment not in current:
            return default
        current = current.get(segment)
    return default if current is None else current


def _flatten_symphony_for_config_panel(raw: dict[str, Any]) -> dict[str, str]:
    symphony = raw.get("symphony") if isinstance(raw.get("symphony"), dict) else {}
    flat: dict[str, str] = {}
    for key, (path, value_type, default) in _SYMPHONY_CONFIG_SPECS.items():
        value = _get_nested_config_value(symphony, path, default)
        if value_type == "bool":
            flat[key] = "true" if bool(value) else "false"
        else:
            flat[key] = str(value)
    flat.update(_flatten_skill_retrieval_for_config_panel(raw))
    return flat


def _flatten_skill_retrieval_for_config_panel(raw: dict[str, Any]) -> dict[str, str]:
    symphony = raw.get("symphony") if isinstance(raw.get("symphony"), dict) else {}
    section = symphony.get("skill_retrieval") if isinstance(symphony.get("skill_retrieval"), dict) else {}
    flat: dict[str, str] = {}
    for key, (path, value_type, default) in _SKILL_RETRIEVAL_CONFIG_SPECS.items():
        value = _get_nested_config_value(section, path, default)
        if value_type == "bool":
            flat[key] = "true" if bool(value) else "false"
        else:
            flat[key] = str(value)
    return flat


def _flatten_swarmflow_for_config_panel(raw: dict[str, Any]) -> dict[str, str]:
    enabled = _get_nested_config_value(
        raw,
        SWARMFLOW_ENABLED_CONFIG_PATH,
        DEFAULT_SWARMFLOW_ENABLED,
    )
    return {"swarmflow_enabled": "true" if enabled else "false"}


def _flatten_external_cli_agents_for_config_panel(raw: dict[str, Any]) -> dict[str, str]:
    agents = _get_nested_config_value(raw, EXTERNAL_CLI_AGENTS_CONFIG_PATH, [])
    configured: dict[str, dict[str, str]] = {}
    if isinstance(agents, list):
        for item in agents:
            if isinstance(item, str):
                cli_agent = item.strip()
                cli_path = ""
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


def _parse_config_switch_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_version_tuple(version: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _external_cli_reference_version(cli_agent: str) -> str:
    if cli_agent == "claude":
        try:
            from claude_agent_sdk._cli_version import __cli_version__

            return str(__cli_version__)
        except Exception:
            return ""
    if cli_agent == "codex":
        try:
            return importlib.metadata.version("openai-codex")
        except importlib.metadata.PackageNotFoundError:
            return ""
    return ""


def _resolve_external_cli_path(cli_agent: str, cli_path: str = "") -> tuple[str, str]:
    requested = cli_path.strip()
    if requested:
        resolved = shutil.which(requested)
        if resolved:
            return resolved, ""
        candidate = Path(requested).expanduser()
        if candidate.is_file():
            return str(candidate), ""
        return "", f"{requested} not found"

    resolved = shutil.which(cli_agent)
    if resolved:
        return resolved, ""
    return "", f"{cli_agent} not found in PATH"


def _is_windows_platform() -> bool:
    return os.name == "nt"


def _run_external_cli_version_command(cli_agent: str, resolved_path: str) -> tuple[str, str]:
    commands = [["-v"]] if cli_agent == "claude" else [["--version"], ["-V"]]
    errors: list[str] = []
    for args in commands:
        try:
            process = subprocess.run(
                [resolved_path, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                shell=False,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            continue
        output = "\n".join(part for part in (process.stdout, process.stderr) if part).strip()
        if process.returncode == 0:
            return output, ""
        errors.append(output or f"exit code {process.returncode}")
    return "", "; ".join(errors)


def _detect_external_cli_agent(cli_agent: str, cli_path: str = "") -> dict[str, Any]:
    normalized_agent = cli_agent.strip().lower()
    if normalized_agent not in _EXTERNAL_CLI_AGENT_KINDS:
        return {
            "cli_agent": normalized_agent,
            "status": "unavailable",
            "path": "",
            "version": "",
            "reference_version": "",
            "message": f"unsupported cli_agent: {cli_agent}",
        }

    resolved_path, path_error = _resolve_external_cli_path(normalized_agent, cli_path)
    reference_version = _external_cli_reference_version(normalized_agent)
    if not resolved_path:
        return {
            "cli_agent": normalized_agent,
            "status": "missing",
            "path": "",
            "version": "",
            "reference_version": reference_version,
            "message": path_error,
        }

    suffix = Path(resolved_path).suffix.lower()
    if _is_windows_platform() and suffix in _UNSUPPORTED_WINDOWS_CLI_SUFFIXES:
        return {
            "cli_agent": normalized_agent,
            "status": "unsupported",
            "path": resolved_path,
            "version": "",
            "reference_version": reference_version,
            "reason": "windows_script",
            "suffix": suffix,
            "message": "windows_script",
        }

    version_output, version_error = _run_external_cli_version_command(normalized_agent, resolved_path)
    version_match = re.search(r"(\d+\.\d+\.\d+)", version_output)
    version = version_match.group(1) if version_match else ""
    if not version_output:
        return {
            "cli_agent": normalized_agent,
            "status": "unavailable",
            "path": resolved_path,
            "version": "",
            "reference_version": reference_version,
            "message": version_error or "version command failed",
        }

    status = "ok"
    message = ""
    version_tuple = _parse_version_tuple(version)
    reference_tuple = _parse_version_tuple(reference_version)
    has_detected_version = bool(version)
    has_reference_version = bool(reference_version)
    has_parsed_versions = version_tuple is not None and reference_tuple is not None
    has_comparable_versions = has_detected_version and has_reference_version and has_parsed_versions
    if has_comparable_versions:
        if version_tuple < reference_tuple:
            status = "warning"
            message = f"version {version} may be incompatible with expected version {reference_version}"
    elif not version:
        status = "warning"
        message = "version output could not be parsed"

    return {
        "cli_agent": normalized_agent,
        "status": status,
        "path": resolved_path,
        "version": version,
        "reference_version": reference_version,
        "message": message,
    }


def _build_symphony_config_update(params: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    for key, (path, value_type, default) in _SYMPHONY_CONFIG_SPECS.items():
        if key not in params:
            continue
        value = _coerce_config_panel_value(params[key], value_type, default)
        _set_nested_config_value(updates, path, value)
    return updates


def _build_skill_retrieval_config_update(params: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    for key, (path, value_type, default) in _SKILL_RETRIEVAL_CONFIG_SPECS.items():
        if key not in params:
            continue
        value = _coerce_config_panel_value(params[key], value_type, default)
        _set_nested_config_value(updates, path, value)
    return updates


def _flatten_modes_team_for_config_panel(raw: dict[str, Any]) -> dict[str, str]:
    """Return the legacy flat fields consumed by the web config panel."""
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
        if not agent_key:
            return ""
        if isinstance(spec, dict) and agent_key not in agent_specs:
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
        team_prefix = f"team_{team_idx}_"
        flat[f"{team_prefix}name"] = str(team_spec.get("team_name") or team_name or "")
        flat[f"{team_prefix}lifecycle"] = str(team_spec.get("lifecycle") or "")
        flat[f"{team_prefix}teammate_mode"] = str(team_spec.get("teammate_mode") or "")
        flat[f"{team_prefix}spawn_mode"] = str(team_spec.get("spawn_mode") or "")
        flat[f"{team_prefix}enable_permissions"] = (
            "true" if bool(team_spec.get("enable_permissions", False)) else "false"
        )
        external_cli_agents = team_spec.get("external_cli_agents")
        flat[f"{team_prefix}external_cli_agents"] = (
            json.dumps(external_cli_agents, ensure_ascii=False)
            if isinstance(external_cli_agents, list)
            else ""
        )

        agents = team_spec.get("agents")
        if not isinstance(agents, dict):
            agents = {}

        leader = team_spec.get("leader")
        if isinstance(leader, dict):
            for key in ("member_name", "display_name", "persona"):
                flat[f"{team_prefix}leader_{key}"] = str(leader.get(key) or "")
        leader_key = str(leader.get("agent_key") or "") if isinstance(leader, dict) else ""
        if not leader_key:
            leader_key = f"{team_name}_leader"
        flat[f"{team_prefix}leader_agent_key"] = add_agent(leader_key, agents.get("leader"))

        teammate_spec = agents.get("teammate")
        if isinstance(teammate_spec, dict):
            teammate = team_spec.get("teammate")
            teammate_key = str(teammate.get("agent_key") or "") if isinstance(teammate, dict) else ""
            if not teammate_key:
                teammate_key = f"{team_name}_teammate"
            flat[f"{team_prefix}teammate_agent_key"] = add_agent(teammate_key, teammate_spec)
        else:
            flat[f"{team_prefix}teammate_agent_key"] = ""

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
                members_out.append({
                    "member_name": member_name,
                    "display_name": str(member.get("display_name") or ""),
                    "persona": str(member.get("persona") or ""),
                    "prompt_hint": str(member.get("prompt_hint") or ""),
                    "agent_key": agent_key,
                })
        flat[f"{team_prefix}predefined_members"] = json.dumps(members_out, ensure_ascii=False)

    for agent_idx, (agent_key, spec) in enumerate(agent_specs.items()):
        if agent_idx >= 10:
            break
        flat[f"agent_name_{agent_idx}"] = agent_key
        flat[f"agent_model_{agent_idx}"] = model_name_from_spec(spec)
        skills = spec.get("skills")
        flat[f"agent_skills_{agent_idx}"] = ",".join(str(item) for item in skills) if isinstance(skills, list) else ""
        flat[f"agent_max_iterations_{agent_idx}"] = str(spec.get("max_iterations") or 200)
        flat[f"agent_completion_timeout_{agent_idx}"] = str(spec.get("completion_timeout") or 600)

    return flat


def _team_payload_requests_codex(params: dict[str, Any]) -> bool:
    teams_raw = params.get("team")
    if not isinstance(teams_raw, list):
        return False
    for team_item in teams_raw:
        if not isinstance(team_item, dict):
            continue
        external_cli_agents = team_item.get("external_cli_agents")
        if not isinstance(external_cli_agents, list):
            continue
        for item in external_cli_agents:
            if item == "codex":
                return True
            if isinstance(item, dict) and item.get("cli_agent") == "codex":
                return True
    return False


def _team_item_requests_codex(team_item: dict[str, Any]) -> bool:
    external_cli_agents = team_item.get("external_cli_agents")
    if not isinstance(external_cli_agents, list):
        return False
    for item in external_cli_agents:
        if item == "codex":
            return True
        if isinstance(item, dict) and item.get("cli_agent") == "codex":
            return True
    return False


def _inject_external_cli_publish_url(params: dict[str, Any]) -> dict[str, Any]:
    teams_raw = params.get("team")
    if not isinstance(teams_raw, list):
        return params

    publish_url = _build_external_cli_publish_url()
    teams: list[Any] = []
    changed = False
    for team_item in teams_raw:
        if isinstance(team_item, dict) and _team_item_requests_codex(team_item):
            item = dict(team_item)
            item["external_cli_publish_url"] = publish_url
            teams.append(item)
            changed = True
        else:
            teams.append(team_item)

    if not changed:
        return params
    return {**params, "team": teams}


def _external_cli_agent_key(cli_agent: str, suffix: str) -> str:
    return f"external_cli_agent_{cli_agent}_{suffix}"


def _external_cli_agent_effective_state(
    cli_agent: str,
    current: dict[str, str],
    params: dict[str, Any],
) -> tuple[bool, bool, str]:
    enabled_key = _external_cli_agent_key(cli_agent, "enabled")
    use_builtin_key = _external_cli_agent_key(cli_agent, "use_builtin")
    cli_path_key = _external_cli_agent_key(cli_agent, "cli_path")
    enabled = _parse_config_switch_bool(params.get(enabled_key, current[enabled_key]))
    use_builtin = _parse_config_switch_bool(params.get(use_builtin_key, current[use_builtin_key]))
    if use_builtin:
        return enabled, True, ""
    return enabled, False, str(params.get(cli_path_key, current[cli_path_key]) or "").strip()


def _external_cli_agents_from_switches(raw: dict[str, Any], params: dict[str, Any]) -> list[dict[str, str]]:
    current = _flatten_external_cli_agents_for_config_panel(raw)
    agents: list[dict[str, str]] = []
    for cli_agent in _EXTERNAL_CLI_AGENT_KINDS:
        enabled, use_builtin, requested_path = _external_cli_agent_effective_state(cli_agent, current, params)
        if not enabled:
            continue
        entry = {"cli_agent": cli_agent}
        if not use_builtin:
            detection = _detect_external_cli_agent(cli_agent, requested_path)
            if detection["status"] not in {"ok", "warning"}:
                raise ValueError(
                    f"{cli_agent} cli_path is not available: {detection.get('message') or detection.get('status')}"
                )
            entry["cli_path"] = str(detection.get("path") or "").strip()
        agents.append(entry)
    return agents


def _build_external_cli_publish_url() -> str:
    host = str(os.getenv("WEB_HOST") or _DEFAULT_EXTERNAL_CLI_PUBLISH_HOST).strip()
    if host in {"", "0.0.0.0", "::", "[::]"}:
        host = _DEFAULT_EXTERNAL_CLI_PUBLISH_HOST
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        host = f"[{host}]"

    port = str(os.getenv("WEB_PORT") or _DEFAULT_EXTERNAL_CLI_PUBLISH_PORT).strip()
    return f"ws://{host}:{port}{_EXTERNAL_CLI_PUBLISH_PATH}"


def _snapshot_external_cli_dependency_install_status(cli_agent: str) -> dict[str, Any]:
    lock = _EXTERNAL_CLI_DEPENDENCY_INSTALL_LOCKS[cli_agent]
    status = _EXTERNAL_CLI_DEPENDENCY_INSTALL_STATUSES[cli_agent]
    with lock:
        result = dict(status)
        result["cli_agent"] = cli_agent
        result["log_tail"] = list(result.get("log_tail") or [])
        return result


def _update_external_cli_dependency_install_status(cli_agent: str, updates: dict[str, Any]) -> None:
    lock = _EXTERNAL_CLI_DEPENDENCY_INSTALL_LOCKS[cli_agent]
    status = _EXTERNAL_CLI_DEPENDENCY_INSTALL_STATUSES[cli_agent]
    with lock:
        status.update(updates)
        status["updated_at"] = time.time()


def _append_external_cli_dependency_install_log(cli_agent: str, line: str) -> None:
    stripped = line.strip()
    if not stripped:
        return
    lock = _EXTERNAL_CLI_DEPENDENCY_INSTALL_LOCKS[cli_agent]
    status = _EXTERNAL_CLI_DEPENDENCY_INSTALL_STATUSES[cli_agent]
    with lock:
        log_tail = list(status.get("log_tail") or [])
        log_tail.append(stripped)
        status.update({
            "last_log": stripped,
            "log_tail": log_tail[-_CODEX_DEPENDENCY_INSTALL_LOG_TAIL_LIMIT:],
            "updated_at": time.time(),
        })


def _snapshot_claude_dependency_install_status() -> dict[str, Any]:
    return _snapshot_external_cli_dependency_install_status("claude")


def _ensure_claude_dependency_available_or_start_install() -> dict[str, Any] | None:
    if importlib.util.find_spec("claude_agent_sdk") is not None:
        with _CLAUDE_DEPENDENCY_INSTALL_LOCK:
            _CLAUDE_DEPENDENCY_INSTALL_STATUS.update(
                {
                    "status": "succeeded",
                    "phase": "succeeded",
                    "error": "",
                    "finished_at": time.time(),
                    "updated_at": time.time(),
                }
            )
        return None
    if _is_frozen_runtime():
        return _ensure_managed_external_cli_runtime_or_start_install("claude")
    with _CLAUDE_DEPENDENCY_INSTALL_LOCK:
        if _CLAUDE_DEPENDENCY_INSTALL_STATUS.get("status") == "running":
            return _snapshot_claude_dependency_install_status()
        _CLAUDE_DEPENDENCY_INSTALL_STATUS.update(
            {
                "status": "running",
                "phase": "installing",
                "error": "",
                "last_log": "",
                "log_tail": [],
                "started_at": time.time(),
                "finished_at": 0.0,
                "updated_at": time.time(),
                "downloaded_bytes": 0,
                "total_bytes": 0,
                "bytes_per_second": 0.0,
                "eta_seconds": 0.0,
                "artifact_index": 0,
                "artifact_count": 0,
                "current_package": "",
                "current_version": "",
                "download_attempt": 0,
                "download_max_attempts": 0,
                "switching_source": False,
            }
        )
    threading.Thread(
        target=_install_claude_dependency_background, name="claude-dependency-install", daemon=True
    ).start()
    return _snapshot_claude_dependency_install_status()


def _install_claude_dependency_background() -> None:
    try:
        package = _resolve_openjiuwen_extra_package("claude")
        _install_optional_dependency("claude", package, "claude_agent_sdk")
        updates = {"status": "succeeded", "phase": "succeeded", "error": "", "finished_at": time.time()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[config.set] Claude dependency installation failed: %s", exc)
        _append_external_cli_dependency_install_log("claude", str(exc))
        updates = {
            "status": "failed",
            "phase": "failed",
            "error": str(exc),
            "finished_at": time.time(),
        }
    _update_external_cli_dependency_install_status("claude", updates)


def _is_frozen_runtime() -> bool:
    return bool(getattr(sys, "frozen", False))


def _snapshot_external_cli_dependency_install_status_unlocked(cli_agent: str) -> dict[str, Any]:
    status = _EXTERNAL_CLI_DEPENDENCY_INSTALL_STATUSES[cli_agent]
    result = dict(status)
    result["cli_agent"] = cli_agent
    result["log_tail"] = list(result.get("log_tail") or [])
    return result


def _ensure_managed_external_cli_runtime_or_start_install(cli_agent: str) -> dict[str, Any]:
    lock = _EXTERNAL_CLI_DEPENDENCY_INSTALL_LOCKS[cli_agent]
    status = _EXTERNAL_CLI_DEPENDENCY_INSTALL_STATUSES[cli_agent]
    with lock:
        if status.get("status") == "running":
            return _snapshot_external_cli_dependency_install_status_unlocked(cli_agent)
        status.update({
            "status": "running",
            "phase": "preparing",
            "error": "",
            "last_log": "",
            "log_tail": [],
            "started_at": time.time(),
            "finished_at": 0.0,
            "updated_at": time.time(),
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "bytes_per_second": 0.0,
            "eta_seconds": 0.0,
            "artifact_index": 0,
            "artifact_count": 0,
            "current_package": "",
            "current_version": "",
            "download_attempt": 0,
            "download_max_attempts": 0,
            "switching_source": False,
        })
    threading.Thread(
        target=_run_managed_external_cli_runtime_install,
        args=(cli_agent,),
        name=f"{cli_agent}-managed-runtime-install",
        daemon=True,
    ).start()
    return _snapshot_external_cli_dependency_install_status(cli_agent)


def _run_managed_external_cli_runtime_install(cli_agent: str) -> None:
    try:
        from jiuwenswarm.common.external_cli_runtime import (
            activate_external_cli_runtime_paths,
            install_external_cli_runtime,
        )

        install_external_cli_runtime(
            cli_agent,
            log_callback=lambda line: _append_external_cli_dependency_install_log(cli_agent, line),
            progress_callback=lambda progress: _update_external_cli_dependency_install_status(cli_agent, progress),
        )
        activate_external_cli_runtime_paths()
        importlib.invalidate_caches()
        required_module = "claude_agent_sdk" if cli_agent == "claude" else "openai_codex"
        if importlib.util.find_spec(required_module) is None:
            raise RuntimeError(f"{required_module} is still unavailable after installation")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[config.set] %s managed runtime installation failed: %s", cli_agent, exc)
        _append_external_cli_dependency_install_log(cli_agent, str(exc))
        _update_external_cli_dependency_install_status(
            cli_agent,
            {
                "status": "failed",
                "phase": "failed",
                "error": str(exc),
                "finished_at": time.time(),
                "bytes_per_second": 0.0,
                "eta_seconds": 0.0,
            },
        )
        return

    _update_external_cli_dependency_install_status(
        cli_agent,
        {
            "status": "succeeded",
            "phase": "succeeded",
            "error": "",
            "finished_at": time.time(),
            "bytes_per_second": 0.0,
            "eta_seconds": 0.0,
        },
    )


def _snapshot_codex_dependency_install_status() -> dict[str, Any]:
    return _snapshot_external_cli_dependency_install_status("codex")


def _update_codex_dependency_install_status(updates: dict[str, Any]) -> None:
    _update_external_cli_dependency_install_status("codex", updates)


def _append_codex_dependency_install_log(line: str) -> None:
    _append_external_cli_dependency_install_log("codex", line)


def _ensure_codex_dependency_available_or_start_install() -> dict[str, Any] | None:
    if importlib.util.find_spec("openai_codex") is not None:
        _update_codex_dependency_install_status({
            "status": "succeeded",
            "phase": "succeeded",
            "error": "",
            "finished_at": time.time(),
        })
        return None

    if _is_frozen_runtime():
        return _ensure_managed_external_cli_runtime_or_start_install("codex")

    with _CODEX_DEPENDENCY_INSTALL_LOCK:
        if _CODEX_DEPENDENCY_INSTALL_STATUS.get("status") == "running":
            already_running = True
        else:
            already_running = False
            _CODEX_DEPENDENCY_INSTALL_STATUS.update({
                "status": "running",
                "phase": "preparing",
                "error": "",
                "last_log": "",
                "log_tail": [],
                "started_at": time.time(),
                "finished_at": 0.0,
                "updated_at": time.time(),
                "downloaded_bytes": 0,
                "total_bytes": 0,
                "bytes_per_second": 0.0,
                "eta_seconds": 0.0,
                "artifact_index": 0,
                "artifact_count": 0,
                "current_package": "",
                "current_version": "",
                "download_attempt": 0,
                "download_max_attempts": 0,
                "switching_source": False,
            })
    if already_running:
        return _snapshot_codex_dependency_install_status()

    thread = threading.Thread(
        target=_run_codex_dependency_install_background,
        name="codex-dependency-install",
        daemon=True,
    )
    thread.start()
    return _snapshot_codex_dependency_install_status()


def _run_codex_dependency_install_background() -> None:
    try:
        _install_codex_dependency()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[config.set] Codex dependency installation failed: %s", exc)
        _append_codex_dependency_install_log(str(exc))
        _update_codex_dependency_install_status({
            "status": "failed",
            "phase": "failed",
            "error": str(exc),
            "finished_at": time.time(),
        })
        return

    _update_codex_dependency_install_status({
        "status": "succeeded",
        "phase": "succeeded",
        "error": "",
        "finished_at": time.time(),
    })


def _install_codex_dependency() -> None:
    if _is_frozen_runtime():
        raise RuntimeError("frozen applications must use the managed Codex runtime installer")
    package = _resolve_openjiuwen_codex_package()
    _install_optional_dependency("codex", package, "openai_codex")


def _install_optional_dependency(
    cli_agent: str,
    package: str,
    required_module: str,
) -> None:
    if _is_frozen_runtime():
        raise RuntimeError(f"frozen applications must use the managed {cli_agent} runtime installer")
    args = _build_optional_dependency_install_args(package)
    output_lines: list[str] = []
    _update_external_cli_dependency_install_status(
        cli_agent,
        {
            "phase": "installing",
        },
    )
    try:
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"failed to install {cli_agent} dependency: {exc}") from exc

    output_queue: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        if process.stdout is None:
            output_queue.put(None)
            return
        try:
            for line in process.stdout:
                output_queue.put(line)
        finally:
            output_queue.put(None)

    reader = threading.Thread(target=read_output, name=f"{cli_agent}-dependency-install-output", daemon=True)
    reader.start()
    deadline = time.monotonic() + _OPTIONAL_DEPENDENCY_INSTALL_TIMEOUT_SECONDS
    reader_done = False
    while True:
        try:
            item = output_queue.get(timeout=0.2)
        except queue.Empty:
            item = None
        else:
            if item is None:
                reader_done = True
            else:
                line = item.rstrip()
                if line:
                    output_lines.append(line)
                    _append_external_cli_dependency_install_log(cli_agent, line)

        if process.poll() is not None and reader_done:
            break
        if time.monotonic() >= deadline:
            process.kill()
            process.wait()
            reader.join(timeout=1)
            raise RuntimeError(f"failed to install {cli_agent} dependency: timed out")

    reader.join(timeout=1)
    returncode = process.wait()
    if returncode != 0:
        output = "\n".join(output_lines[-20:])
        raise RuntimeError(f"failed to install {cli_agent} dependency: {output}")
    _update_external_cli_dependency_install_status(
        cli_agent,
        {
            "phase": "verifying",
        },
    )
    importlib.invalidate_caches()
    if importlib.util.find_spec(required_module) is None:
        raise RuntimeError(f"failed to install {cli_agent} dependency: {required_module} is still unavailable")


def _resolve_openjiuwen_codex_package() -> str:
    return _resolve_openjiuwen_extra_package("codex")


def _resolve_openjiuwen_extra_package(extra: str) -> str:
    spec = importlib.util.find_spec("openjiuwen")
    if spec is None:
        return f"openjiuwen[{extra}]"

    candidate_paths: list[Path] = []
    if spec.origin:
        candidate_paths.append(Path(spec.origin))
    if spec.submodule_search_locations:
        candidate_paths.extend(Path(location) for location in spec.submodule_search_locations)

    for candidate_path in candidate_paths:
        package_root = candidate_path if candidate_path.is_dir() else candidate_path.parent
        project_root = package_root.parent if package_root.name == "openjiuwen" else package_root
        if (project_root / "pyproject.toml").is_file() and (project_root / "openjiuwen").is_dir():
            return f"{project_root}[{extra}]"

    return f"openjiuwen[{extra}]"


def _build_optional_dependency_install_args(package: str) -> list[str]:
    uv_cmd = shutil.which("uv")
    if uv_cmd and sys.prefix != sys.base_prefix:
        return [uv_cmd, "pip", "install", package]
    return [sys.executable, "-m", "pip", "install", package]


async def _clear_agent_config_cache(agent_client=None) -> None:
    """写回 config.yaml 后清除 agent 侧配置缓存，使下次读取时得到最新文件内容。"""
    try:
        if agent_client is not None:
            from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
            from jiuwenswarm.common.schema.message import ReqMethod

            env = e2a_from_agent_fields(
                request_id=f"cfg-reload-{uuid.uuid4().hex[:8]}",
                channel_id="",
                req_method=ReqMethod.AGENT_RELOAD_CONFIG,
            )
            await agent_client.send_request(env)
        else:
            get_config()
    except Exception:  # noqa: BLE001
        pass


async def _restart_agent_browser_runtime(
    agent_client=None,
    *,
    previous_chrome_path: str = "",
    previous_headless: bool = True,
) -> None:
    """Stop active agent-side browser runtimes so the next task uses new config."""
    if agent_client is None:
        return

    from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
    from jiuwenswarm.common.schema.message import ReqMethod

    env = e2a_from_agent_fields(
        request_id=f"browser-restart-{uuid.uuid4().hex[:8]}",
        channel_id="",
        req_method=ReqMethod.BROWSER_RUNTIME_RESTART,
        params={
            "browser_key": "",
            "profile_name": (
                os.getenv("BROWSER_PROFILE_NAME") or "jiuwenclaw"
            ).strip(),
            "display_mode": "headless" if previous_headless else "headed",
            "browser_binary": str(previous_chrome_path or "").strip(),
        },
    )
    response = await agent_client.send_request(env)
    if not response.ok:
        error = response.payload.get("error", "browser runtime restart failed")
        raise RuntimeError(str(error))


def _make_session_id() -> str:
    # 与前端 generateSessionId 保持一致：毫秒时间戳(16进制) + 6位随机16进制
    ts = format(int(time.time() * 1000), "x")
    suffix = secrets.token_hex(3)
    return f"sess_{ts}_{suffix}"


# ---------------------------------------------------------------------------
# 飞书 Feishu / 小艺 Xiaoyi 多应用配置 — 默认值 & 归一化函数
# ---------------------------------------------------------------------------

_FEISHU_APP_DEFAULTS: dict[str, Any] = {
    "name": "默认应用",
    "is_default": False,
    "enabled": True,
    "app_id": "",
    "app_secret": "",
    "encrypt_key": "",
    "verification_token": "",
    "allow_from": ["0.0.0.0/0"],
    "enable_streaming": True,
    "group_digital_avatar": False,
    "my_user_id": "",
    "bot_name": "",
    "enable_memory": False,
}


def _merge_apps_by_id(
    new_apps: list[dict],
    existing_apps: list[dict],
) -> list[dict]:
    """将新 apps 与已有 apps 按 app_id 合并，保留前端未显式发送的字段。

    各 channel 的 ``_normalize_*_conf`` 会用对应的 ``_*_APP_DEFAULTS`` 空值填充
    前端未发的字段。合并以已有值为基座、新值覆盖，避免已配置的敏感字段（如
    app_secret / sk 等）被默认空值覆盖丢失。

    Parameters
    ----------
    new_apps : list[dict]
        前端提交并经归一化的新 apps 列表。
    existing_apps : list[dict]
        从 cm.get_conf (或 config.yaml) 读出的已有 apps 列表。

    Returns
    -------
    list[dict]
        合并后的 apps 列表。
    """
    if not isinstance(existing_apps, list) or not existing_apps:
        return new_apps

    existing_by_app_id = {
        a["app_id"]: a
        for a in existing_apps
        if isinstance(a, dict) and a.get("app_id")
    }
    if not existing_by_app_id:
        return new_apps

    return [
        {**existing_by_app_id[app["app_id"]], **app}
        if isinstance(app, dict) and app.get("app_id") in existing_by_app_id
        else app
        for app in new_apps
    ]


def _normalize_feishu_conf(raw: dict) -> dict:
    """将 channels.feishu 统一为 apps 格式，并为每个 app 补充缺省字段。

    输入可以是旧平铺格式（``{"app_id": "xxx", "app_secret": "yyy"}``）
    或新多应用格式（``{"apps": [...]}``）。返回结果始终包含 ``apps`` 列表。
    若输入为空或非 dict，返回 ``{"apps": []}``。
    """
    if not isinstance(raw, dict):
        logger.debug("[normalize_feishu] 输入非 dict (%s), 返回空 apps", type(raw).__name__)
        return {"apps": []}
    if "apps" in raw:
        apps_raw = raw["apps"]
        app_names = [a.get("name", "?") for a in apps_raw] if isinstance(apps_raw, list) else []
        logger.debug(
            "[normalize_feishu] 多应用格式, apps=%d, names=%s",
            len(apps_raw) if isinstance(apps_raw, list) else -1,
            app_names,
        )
        apps = [
            {**_FEISHU_APP_DEFAULTS, **app}
            for app in apps_raw
        ]
        return {**raw, "apps": apps}
    # 旧平铺格式 → 转为 apps
    keys_present = [k for k in ("app_id", "app_secret", "encrypt_key", "verification_token") if k in raw]
    logger.debug("[normalize_feishu] 旧平铺格式, keys=%s, 转为单 app", keys_present)
    return {
        **raw,
        "apps": [_normalize_single_feishu_to_app(raw)],
    }


def _normalize_single_feishu_to_app(raw: dict) -> dict:
    """将单个平铺飞书配置转为 apps 列表项。"""
    return {
        **_FEISHU_APP_DEFAULTS,
        "is_default": True,
        "name": raw.get("name", "默认应用"),
        "enabled": bool(raw.get("enabled", True)),
        "app_id": raw.get("app_id", ""),
        "app_secret": raw.get("app_secret", ""),
        "encrypt_key": raw.get("encrypt_key", ""),
        "verification_token": raw.get("verification_token", ""),
        "allow_from": raw.get("allow_from") or ["0.0.0.0/0"],
        "enable_streaming": bool(raw.get("enable_streaming", True)),
        "group_digital_avatar": bool(raw.get("group_digital_avatar", False)),
        "my_user_id": raw.get("my_user_id", ""),
        "bot_name": raw.get("bot_name", ""),
        "enable_memory": bool(raw.get("enable_memory", False)),
        **raw,
    }


_XIAOYI_APP_DEFAULTS: dict[str, Any] = {
    "name": "默认应用",
    "is_default": False,
    "enabled": True,
    "ak": "",
    "sk": "",
    "app_id": "",
    "api_id": "",
    "agent_id": "",
    "enable_streaming": True,
    "mode": "xiaoyi_channel",
    "push_id": "",
    "ws_url1": "wss://hag.cloud.huawei.com/openclaw/v1/ws/link",
    "ws_url2": "wss://116.63.174.231/openclaw/v1/ws/link",
    "phone_tools_enabled": False,
    "uid": "",
    "api_key": "",
    "push_url": "",
    "file_upload_url": "",
}


def _normalize_xiaoyi_conf(raw: dict) -> dict:
    """将 channels.xiaoyi 统一为 apps 格式，并为每个 app 补充缺省字段。

    输入可以是旧平铺格式或新多应用格式。返回结果始终包含 ``apps`` 列表。
    若输入为空或非 dict，返回 ``{"apps": []}``。
    """
    if not isinstance(raw, dict):
        logger.debug("[normalize_xiaoyi] 输入非 dict (%s), 返回空 apps", type(raw).__name__)
        return {"apps": []}
    if "apps" in raw:
        apps_raw = raw["apps"]
        app_names = [a.get("name", "?") for a in apps_raw] if isinstance(apps_raw, list) else []
        logger.debug(
            "[normalize_xiaoyi] 多应用格式, apps=%d, names=%s",
            len(apps_raw) if isinstance(apps_raw, list) else -1,
            app_names,
        )
        apps = [
            {**_XIAOYI_APP_DEFAULTS, **app}
            for app in apps_raw
        ]
        return {**raw, "apps": apps}
    # 旧平铺格式 → 转为 apps
    keys_present = [k for k in ("ak", "sk", "agent_id") if k in raw]
    logger.debug("[normalize_xiaoyi] 旧平铺格式, keys=%s, 转为单 app", keys_present)
    return {
        **raw,
        "apps": [_normalize_single_xiaoyi_to_app(raw)],
    }


def _normalize_single_xiaoyi_to_app(raw: dict) -> dict:
    """将单个平铺小艺配置转为 apps 列表项。"""
    return {
        **_XIAOYI_APP_DEFAULTS,
        "is_default": True,
        "name": raw.get("name", "默认应用"),
        "enabled": bool(raw.get("enabled", True)),
        "ak": raw.get("ak", ""),
        "sk": raw.get("sk", ""),
        "app_id": raw.get("app_id", ""),
        "api_id": str(raw.get("api_id") or ""),
        "agent_id": raw.get("agent_id", ""),
        "enable_streaming": bool(raw.get("enable_streaming", True)),
        "mode": raw.get("mode", "xiaoyi_channel"),
        "push_id": raw.get("push_id", ""),
        "ws_url1": raw.get("ws_url1", "wss://hag.cloud.huawei.com/openclaw/v1/ws/link"),
        "ws_url2": raw.get("ws_url2", "wss://116.63.174.231/openclaw/v1/ws/link"),
        "phone_tools_enabled": bool(raw.get("phone_tools_enabled", False)),
        "uid": raw.get("uid", ""),
        "api_key": raw.get("api_key", ""),
        "push_url": raw.get("push_url", ""),
        "file_upload_url": raw.get("file_upload_url", ""),
        **raw,
    }


@dataclass
class WebHandlersBindParams:
    """Named bundle for :func:`_register_web_handlers` (avoids long positional / keyword lists)."""

    channel: Any
    agent_client: Any = None
    message_handler: Any = None
    channel_manager: Any = None
    on_config_saved: Any = None
    heartbeat_service: Any = None
    cron_controller: Any = None
    heartbeat_controller: Any = None
    updater_service: UpdaterService | None = None


_CONTAINER_FILE_API_METHODS = (
    "upload_container_file",
    "download_container_file",
    "list_container_files",
    "mkdir_container_dir",
)


def _supports_container_file_api(client: Any) -> bool:
    """True when *client* can back WebChannel ``/file-api`` (no extensions import)."""
    return all(callable(getattr(client, name, None)) for name in _CONTAINER_FILE_API_METHODS)


def _attribute_session_project(
    meta: dict[str, Any],
    visible_by_id: set[str],
) -> str:
    """返回会话归属的 project_id(或按 work_mode 分桶的默认项目 ID)。

    仅按 ``session.project_id`` 匹配可见项目;不命中(含无 project_id 的存量会话)
    按会话自身的 ``work_mode`` 归入对应默认项目:
      - ``work_mode == "code"`` → ``"default_code"``
      - 其他(含 ``"work"`` / 空 / 非法) → ``"default"``

    存量会话的 project_dir → project_id 解析由启动迁移完成。

    Args:
        meta: 会话元数据
        visible_by_id: 可见(非隐藏)项目的 ``project_id`` 集合
    """
    sp_id = str(meta.get("project_id") or "")
    if sp_id and sp_id in visible_by_id:
        return sp_id
    # 按会话 work_mode 分桶默认项目,使 code 模式孤立会话归 default_code,
    # work 模式孤立会话归 default,与 project.list 默认项目拆分一致
    s_work_mode = str(meta.get("work_mode") or "")
    if s_work_mode == "code":
        return DEFAULT_PROJECT_ID_CODE
    return DEFAULT_PROJECT_ID_WORK


def _project_info_payload(
    proj: Any | None,
    *,
    default_id: str | None = None,
    stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Serialize a project item consistently for project.list/info/create."""
    st = stats or {"session_count": 0, "last_message_at": None, "last_user_message_at": None}
    git = getattr(proj, "git", {}) if proj is not None else {}
    git_defaults = {
        "enabled": False,
        "repo_root": "",
        "initialized_by_jiuwenswarm": False,
        "detected_at": 0,
        "status": "disabled",
        "branch": "",
        "error": "",
        "error_code": "",
        "hint": "",
        "is_dirty": False,
    }
    git_payload = {**git_defaults, **dict(git)} if isinstance(git, dict) and git else git_defaults
    if default_id is not None:
        work_mode = DEFAULT_TUI_WORK_MODE if default_id == DEFAULT_PROJECT_ID_CODE else DEFAULT_WEB_WORK_MODE
        return {
            "project_id": default_id,
            "name": "默认项目",
            "project_dir": "",
            "pinned": False,
            "pin_order": 0,
            "is_default": True,
            "hidden": False,
            "work_mode": work_mode,
            "git": git_payload,
            "session_count": st["session_count"],
            "last_message_at": st["last_message_at"],
            "last_user_message_at": st["last_user_message_at"],
            "created_at": 0,
            "updated_at": 0,
        }
    work_mode = getattr(proj, "work_mode", "") or DEFAULT_WEB_WORK_MODE
    return {
        "project_id": proj.project_id,
        "name": proj.name,
        "project_dir": proj.project_dir,
        "pinned": proj.pinned,
        "pin_order": proj.pin_order,
        "is_default": False,
        "hidden": proj.hidden,
        "work_mode": work_mode,
        "git": git_payload,
        "session_count": st["session_count"],
        "last_message_at": st["last_message_at"],
        "last_user_message_at": st["last_user_message_at"],
        "created_at": proj.created_at,
        "updated_at": getattr(proj, "updated_at", 0),
    }


def _normalize_provider_value(value: str) -> str:
    """把任意大小写的 provider 值归一化为 ``ProviderType`` 规范大小写。

    与 TUI 侧 ``gateway/channel_manager/tui/tui_connect.py:_normalize_provider_value``
    保持一致：TUI 在写入/校验 model_provider 时会做大小写归一化，Web 侧此前没有做，
    导致同一份配置（例如历史数据里 model_provider 大小写不规范）在 TUI 能正常识别，
    但 Web 的"测试"/"保存"因大小写敏感的精确匹配而被误判为非法值。
    """
    normalized = value.strip()
    if not normalized:
        return normalized

    available_model_providers = [provider.value for provider in ProviderType]
    lookup = {provider.lower(): provider for provider in available_model_providers}
    return lookup.get(normalized.lower(), normalized)


def _resolve_model_config_obj_for_validate(model_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """从热更新后的配置中查找对应模型的 ``model_config_obj``。

    按 ``model_name`` 或 ``alias`` 匹配 ``models.defaults`` 中的条目，
    返回该条目的 ``model_config_obj``；找不到或读取失败时 fallback 到
    空字典（让模型使用自身默认参数，避免 ``temperature=0`` 对部分
    模型不兼容）。前端传入的 ``reasoning_level`` 可覆盖配置值。
    """
    model_config_obj: dict[str, Any] = {}
    try:
        _cfg = get_config()
        _models = get_default_models(_cfg)
        for entry in _models:
            if not isinstance(entry, dict):
                continue
            mcc = entry.get("model_client_config") or {}
            entry_model_name = str(mcc.get("model_name", "")).strip()
            entry_alias = str(entry.get("alias", "")).strip()
            if entry_model_name == model_name or entry_alias == model_name:
                obj = entry.get("model_config_obj")
                if isinstance(obj, dict):
                    model_config_obj = dict(obj)
                # context_window（模型支持的上下文总长度）可配在任意模型条目的
                # model_config_obj 里（defaults / agentos / video / audio / vision /
                # image_gen 均可），供 core 从 ModelRequestConfig 取值。是否在出口
                # 清掉取决于 core 是否已把 context_window 加为 ModelRequestConfig
                # 正式字段（见 reasoning_injector.core_has_context_window_field）：
                # - core 未加字段（过渡期）：context_window 进 extra 会被
                #   base_model_client 经 model_dump 透传给厂商 SDK 报 unexpected
                #   keyword argument -> 需清。
                # - core 已加字段：context_window 作正式字段，core 自行 exclude
                #   不发厂商、可读 -> 不清（否则切掉 core 想读的值）。
                # 不再守 _source=="agentos"：所有条目一视同仁，defaults 配了
                # context_window 同样需要过渡期清防发厂商。_source 标记本身由
                # reasoning_injector._build_model_request_kwargs 统一 pop；此处与
                # 公共出口同口径，覆盖绕过 build_model_from_entry 的 validate 路径。
                if not core_has_context_window_field():
                    model_config_obj.pop("context_window", None)
                logger.info(
                    "[config.validate_model] loaded model_config_obj for '%s' "
                    "(matched_by=%s): %s",
                    model_name,
                    "model_name" if entry_model_name == model_name else "alias",
                    model_config_obj,
                )
                break
        else:
            logger.info(
                "[config.validate_model] no model_config_obj found for '%s', using empty default",
                model_name,
            )
    except Exception as exc:
        logger.warning(
            "[config.validate_model] failed to read model_config_obj from config, using default. %s",
            exc,
        )
    if "reasoning_level" in params:
        model_config_obj["reasoning_level"] = params.get("reasoning_level")
    return model_config_obj


def _persist_media_locally(
    data: bytes, safe_session_id: str, filename: str
) -> tuple[bool, dict[str, Any]]:
    """Write large media bytes directly to the shared user directory.

    Used in legacy single-user mode where the AgentServer has no HTTP upload
    listener.  The path matches ``media_attachments._store_image_item`` so the
    AgentServer-side ``_persisted`` passthrough recognizes the file.
    """
    from jiuwenswarm.common.utils import get_agent_sessions_dir
    from jiuwenswarm.server.runtime.attachments.upload_storage import atomic_write_unique

    try:
        upload_dir = get_agent_sessions_dir() / safe_session_id / "uploads"
        path = atomic_write_unique(upload_dir / filename, data)
        return True, {"path": str(path)}
    except Exception as exc:  # noqa: BLE001
        return False, {"error": str(exc), "code": "UPLOAD_FAILED"}


async def _upload_media_item_via_http(
    item: dict[str, Any],
    data: bytes,
    *,
    session_id: str | None,
    index: int,
    agent_client: Any,
    user_id: str | None,
) -> dict[str, Any] | None:
    """把单张浏览器图片经 HTTP bridge 上传到 AgentServer 注入目录，返回落盘记录。

    落盘路径与 AgentServer 侧 ``_store_image_item`` 一致
    （``agent/sessions/<safe_session_id>/uploads/<safe_filename>``，同名去重）。
    上传失败返回 ``None``（调用方保留原 base64 项，由下游链路处理）。
    """
    from jiuwenswarm.gateway.routing.agent_http_bridge import upload_file_bytes_via_e2a
    from jiuwenswarm.gateway.routing.e2a_proxy import is_agentos_routing_client
    from jiuwenswarm.server.runtime.attachments import media_attachments as _ma
    from jiuwenswarm.server.runtime.attachments.upload_storage import (
        safe_session_dirname,
        safe_upload_filename,
    )

    mime_type = str(item.get("mimeType") or item.get("mime_type") or "").lower().strip()
    suffix = _ma.image_suffix_for_mime(mime_type)
    if suffix is None:
        return None
    filename = safe_upload_filename(
        str(item.get("filename") or f"image-{index + 1}{suffix}"),
        fallback=f"image-{index + 1}{suffix}",
    )
    if Path(filename).suffix.lower() not in _ma.supported_image_suffixes():
        filename = f"{filename}{suffix}"
    safe_session_id = safe_session_dirname(session_id)
    rel_path = f"agent/sessions/{safe_session_id}/uploads/{filename}"
    if is_agentos_routing_client(agent_client):
        ok, payload = await upload_file_bytes_via_e2a(
            data,
            rel_path,
            agent_client=agent_client,
            user_id=user_id,
            channel_id="web",
            session_id=session_id,
        )
    else:
        # Legacy single-user mode: the AgentServer has no HTTP upload
        # listener (its upload endpoint would be on ws_port+1, which is
        # never started).  Write directly to the shared user directory
        # using the same path the AgentServer ``_store_image_item`` would
        # use, avoiding a doomed HTTP attempt that always fails with
        # ConnectionRefused.
        ok, payload = _persist_media_locally(data, safe_session_id, filename)
    if not ok:
        logger.warning("[media.persist] 大图上传失败: %s", payload.get("error"))
        return None
    return {
        "type": "image",
        "filename": filename,
        "mime_type": mime_type,
        "path": str(payload.get("path") or ""),
        "size_bytes": len(data),
        "_persisted": True,
    }


async def _pre_persist_large_media(
    params: dict[str, Any], *, session_id: str | None, agent_client: Any, user_id: str | None
) -> dict[str, Any]:
    """转发 media.persist 前，把超预算的 base64 图片改为 HTTP bridge 上传。

    小图保留 base64 走 E2A（AgentServer 注入目录落盘）；大图在 Gateway 侧
    解码后经 HTTP 上传并标记 ``_persisted``，AgentServer 侧直接透传落盘记录，
    不重复解码。返回处理后的 params（原对象就地修改 media_items）。
    """
    items = params.get("media_items")
    if not isinstance(items, list):
        return params
    import json as _json

    from jiuwenswarm.gateway.routing.agent_http_bridge import E2A_PAYLOAD_MAX_BYTES

    try:
        base_payload = {k: v for k, v in params.items() if k != "media_items"}
        overhead = len(_json.dumps(base_payload, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001
        overhead = 4096
    remaining = E2A_PAYLOAD_MAX_BYTES - overhead
    new_items: list[Any] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or item.get("type") != "image":
            new_items.append(item)
            continue
        raw = item.get("base64Data") or item.get("base64_data")
        if not isinstance(raw, str) or not raw.strip():
            new_items.append(item)
            continue
        try:
            data = base64.b64decode(raw, validate=True)
        except Exception:  # noqa: BLE001
            new_items.append(item)
            continue
        if not data:
            new_items.append(item)
            continue
        if len(data) > remaining:
            persisted = await _upload_media_item_via_http(
                item,
                data,
                session_id=session_id,
                index=index,
                agent_client=agent_client,
                user_id=user_id,
            )
            if persisted is not None:
                new_items.append(persisted)
                continue
            # 上传失败：保留原 base64（若仍超帧限制，由下游链路返回可重试错误）
            new_items.append(item)
            continue
        remaining -= len(data)
        new_items.append(item)
    params["media_items"] = new_items
    return params


def _register_web_handlers(bind: WebHandlersBindParams) -> None:
    """注册 Web 前端需要的 method 与 on_connect。
    on_config_saved: 可选，config.set 写回后调用的回调；
        updated_env_keys 为本次改动的键名集合，
        env_updates 为本次变更的环境变量增量（仅包含更新项），
        config_payload 为当前最新配置快照；
        返回 True 表示已热更新未重启，False 表示已安排进程重启。
    heartbeat_service: 可选，GatewayHealthCheckService 实例，用于处理 health_check.get_conf / health_check.set_conf。
    """
    channel = bind.channel
    agent_client = bind.agent_client
    message_handler = bind.message_handler
    channel_manager = bind.channel_manager
    on_config_saved = bind.on_config_saved
    heartbeat_service = bind.heartbeat_service
    cron_controller = bind.cron_controller
    heartbeat_controller = bind.heartbeat_controller
    updater_service = bind.updater_service

    from jiuwenswarm.common.schema.message import Message, EventType

    def _resolve(ref, key="value"):
        """若为 ref 字典则取 key（无则返回 None），否则返回自身。"""
        if isinstance(ref, dict):
            return ref.get(key)
        return ref

    def _schedule_clear_agent_config_cache(name: str) -> None:
        asyncio.create_task(
            _clear_agent_config_cache(_resolve(agent_client)),
            name=f"{name}.clear_agent_config_cache",
        )

    def _schedule_agent_prewarm_sync(name: str) -> None:
        """Reconcile project-derived warm keys without delaying the Web RPC."""

        async def _sync() -> None:
            try:
                from jiuwenswarm.common.e2a.gateway_normalize import (
                    e2a_from_agent_fields,
                )
                from jiuwenswarm.common.schema.message import ReqMethod

                real_client = _resolve(agent_client)
                if real_client is None:
                    return
                cm = _resolve(channel_manager)
                enabled_channels = [
                    channel
                    for channel in list(getattr(cm, "enabled_channels", []) or [])
                    if str(channel).lower() not in {"acp", "a2a"}
                ]
                env = e2a_from_agent_fields(
                    request_id=f"agent-prewarm-project-{uuid.uuid4().hex[:8]}",
                    channel_id="web",
                    req_method=ReqMethod.AGENT_PREWARM_SYNC,
                    params={"enabled_channels": sorted(set(enabled_channels))},
                    is_stream=False,
                    timestamp=time.time(),
                )
                response = await real_client.send_request(env)
                if not getattr(response, "ok", False):
                    logger.warning(
                        "[Project] agent.prewarm.sync rejected: %s",
                        getattr(response, "payload", None),
                    )
            except Exception as exc:  # noqa: BLE001 - maintenance is non-blocking
                logger.warning("[Project] agent.prewarm.sync failed: %s", exc)

        asyncio.create_task(_sync(), name=f"{name}.agent_prewarm_sync")

    def _resolve_env_vars(value: Any) -> Any:
        """Recursively resolve environment variables in config values."""
        if isinstance(value, str):
            pattern = r'\$\{([^:}]+)(?::-([^}]*))?\}'

            def replace_env(match):
                var_name = match.group(1)
                default = match.group(2) if match.group(2) is not None else ""
                return os.getenv(var_name, default)

            return re.sub(pattern, replace_env, value)
        elif isinstance(value, dict):
            return {k: _resolve_env_vars(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [_resolve_env_vars(item) for item in value]
        else:
            return value

    async def _on_connect(ws):
        ac = _resolve(agent_client)
        if ac is None or not getattr(ac, "server_ready", False):
            logger.debug("[_on_connect] Agent 未就绪，跳过 connection.ack")
            return
        # V2: 复用 ws 握手时注册的占位 session_id，而不是另 make 一个新 sid。
        # 原实现凭空生成 sid_B 与 ws 在 _clients_by_key 中注册的 sid_A 不一致，
        # 导致 send() 按 session_id 反查落空、ACK 被丢弃，前端收不到 connection.ack。
        # 复用 sid_A 后，ACK 走标准 send 流程即可命中本 ws，无需特殊路由兜底。
        sid = getattr(ws, "_jiuwen_initial_sid", None) or _make_session_id()

        ack_msg = Message(
            id=f"ack-{sid}",
            type="event",
            channel_id=channel.channel_id,
            session_id=sid,
            params={},
            timestamp=time.time(),
            ok=True,
            event_type=EventType.CONNECTION_ACK,
            payload={
                "session_id": sid,
                "mode": "BUILD",
                "tools": [],
                "protocol_version": "1.0",
            },
        )
        mh = _resolve(message_handler)
        if mh:
            await mh.publish_robot_messages(ack_msg)
        else:
            await channel.send(ack_msg)

    channel.on_connect(_on_connect)

    async def _on_disconnect(ws, _session_ids):
        mh = _resolve(message_handler)
        cleanup = getattr(mh, "unregister_ws_subscriptions", None)
        ws_id = str(getattr(ws, "_jiuwen_ws_id", "") or "").strip()
        if callable(cleanup) and ws_id:
            await cleanup(channel.channel_id, ws_id)

    register_disconnect = getattr(channel, "on_disconnect", None)
    if callable(register_disconnect):
        register_disconnect(_on_disconnect)

    async def _config_get(ws, req_id, params, session_id):
        # 返回 _CONFIG_SET_ENV_MAP 里所有键对应的环境变量当前值
        payload = {
            param_key: (os.getenv(env_key) or "")
            for param_key, env_key in _CONFIG_SET_ENV_MAP.items()
        }
        payload["app_version"] = __version__
        runtime_platform = (os.getenv("JIUWENSWARM_RUNTIME_PLATFORM") or "").strip().lower() or "default"
        payload["runtime_platform"] = runtime_platform
        payload["external_cli_agents_supported"] = "false" if runtime_platform == "harmony" else "true"
        # 合并 config.yaml 中的配置项
        try:
            raw = get_config_raw()
            setup_guide_cfg = raw.get("setup_guide") or {}
            payload["setup_guide_enabled"] = (
                "true" if setup_guide_cfg.get("enabled", True) else "false"
            )
            for key, val in payload.items():
                from jiuwenswarm.extensions.registry import ExtensionRegistry
                if (("api_key" in key.lower() or "token" in key.lower())
                        and ExtensionRegistry.get_instance().get_crypto_provider()):
                    payload[key] = ExtensionRegistry.get_instance().get_crypto_provider().decrypt(val)
            react_cfg = raw.get("react") or {}
            ctx_cfg = react_cfg.get("context_engine_config") or {}
            kv_cfg = react_cfg.get("kv_cache_affinity_config") or {}
            payload["context_engine_enabled"] = "true" if ctx_cfg.get("enabled", False) else "false"
            payload["kv_cache_release_enabled"] = (
                "true" if kv_cfg.get("enable_kv_cache_release", False) else "false"
            )
            payload["kv_cache_affinity_enabled"] = (
                "true" if kv_cfg.get("enable_kv_cache_affinity", False) else "false"
            )
            perm_cfg = raw.get("permissions") or {}
            payload["permissions_enabled"] = "true" if perm_cfg.get("enabled", False) else "false"
            # Skill evolution is controlled solely by the canonical nested YAML key.
            evolution_cfg = (raw.get("react") or {}).get("evolution") or {}
            payload["skill_evolution"] = "true" if evolution_cfg.get("skill_evolution", False) else "false"
            memory_cfg = (raw.get("memory") or {}).get("forbidden_memory_definition") or {}
            payload["memory_forbidden_enabled"] = "true" if memory_cfg.get("enabled", False) else "false"
            memory_desc = memory_cfg.get("description") or {}
            payload["memory_forbidden_description"] = memory_desc
            payload.update(get_a2ui_config_payload(raw))
            payload.update(_flatten_swarmflow_for_config_panel(raw))
            payload.update(_flatten_external_cli_agents_for_config_panel(raw))
            payload.update(_flatten_symphony_for_config_panel(raw))
            if not payload.get("free_search_ddg_enabled"):
                payload["free_search_ddg_enabled"] = "false"
            if not payload.get("free_search_bing_enabled"):
                payload["free_search_bing_enabled"] = "false"
            payload.update(_flatten_modes_team_for_config_panel(raw))
            # Proactive recommendation — use resolved config (env vars expanded)
            resolved = get_config()
            proactive_cfg = resolved.get("proactive_recommendation") or {}
            payload["proactive_recommendation_enabled"] = "true" if proactive_cfg.get("enabled", False) else "false"
            payload["proactive_recommendation_max_recommend_per_day"] = str(
                proactive_cfg.get("max_recommend_per_day", 10))
            payload["proactive_recommendation_max_rounds_per_tick"] = str(
                proactive_cfg.get("max_rounds_per_tick", 20))
            models_cfg = resolved.get("models") or {}
            payload["enable_free_models"] = "true" if models_cfg.get("enable_free_models", True) else "false"
        except Exception:  # noqa: BLE001
            payload.setdefault("context_engine_enabled", "false")
            payload.setdefault("kv_cache_release_enabled", "false")
            payload.setdefault("kv_cache_affinity_enabled", "false")
            payload.setdefault("permissions_enabled", "false")
            payload.setdefault("setup_guide_enabled", "true")
            payload.setdefault("skill_evolution", "false")
            payload.setdefault("memory_forbidden_enabled", "false")
            payload.setdefault("memory_forbidden_description", "")
            payload.setdefault("swarmflow_enabled", "true" if DEFAULT_SWARMFLOW_ENABLED else "false")
            for key, value in get_default_a2ui_config_payload().items():
                payload.setdefault(key, value)
            for key, (_, value_type, default) in {
                **_SYMPHONY_CONFIG_SPECS,
                **_SKILL_RETRIEVAL_CONFIG_SPECS,
            }.items():
                if value_type == "bool":
                    default_text = "true" if default else "false"
                else:
                    default_text = str(default)
                payload.setdefault(key, default_text)
            payload.setdefault("free_search_ddg_enabled", "false")
            payload.setdefault("free_search_bing_enabled", "false")
            payload.setdefault("proactive_recommendation_enabled", "false")
            payload.setdefault("proactive_recommendation_max_recommend_per_day", "10")
            payload.setdefault("proactive_recommendation_max_rounds_per_tick", "20")
            payload.setdefault("enable_free_models", "true")
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _external_cli_detect(ws, req_id, params, session_id):
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        cli_agent = str(params.get("cli_agent") or "").strip().lower()
        cli_path = str(params.get("cli_path") or "").strip()
        result = _detect_external_cli_agent(cli_agent, cli_path)
        await channel.send_response(ws, req_id, ok=True, payload=result)

    async def _external_cli_codex_install_status(ws, req_id, params, session_id):
        await channel.send_response(ws, req_id, ok=True, payload=_snapshot_codex_dependency_install_status())

    async def _external_cli_install_status(ws, req_id, params, session_id):
        cli_agent = str((params or {}).get("cli_agent") or "").strip().lower()
        if cli_agent == "claude":
            payload = _snapshot_claude_dependency_install_status()
        elif cli_agent == "codex":
            payload = _snapshot_codex_dependency_install_status()
            payload["cli_agent"] = "codex"
        else:
            await channel.send_response(
                ws, req_id, ok=False, error="unsupported external cli agent", code="BAD_REQUEST"
            )
            return
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    def _persist_env_updates(updates: dict[str, str]) -> None:
        """把已更新的环境变量写回 .env（仅覆盖或追加对应 KEY=value 行）。"""
        env_path = _ENV_FILE
        if not updates:
            return
        try:
            lines: list[str] = []
            if env_path.is_file():
                with open(env_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            new_lines: list[str] = []
            for line in lines:
                stripped = line.strip()
                found = False
                for env_key, value in updates.items():
                    if stripped.startswith(env_key + "="):
                        new_lines.append(f'{env_key}="{value}"\n' if value else f"{env_key}=\n")
                        found = True
                        break
                if not found:
                    new_lines.append(line)
            for env_key, value in updates.items():
                if not any(s.strip().startswith(env_key + "=") for s in new_lines):
                    new_lines.append(f'{env_key}="{value}"\n' if value else f"{env_key}=\n")
            env_path.parent.mkdir(parents=True, exist_ok=True)
            with open(env_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
        except OSError as e:
            logger.warning("[config.set] 写回 .env 失败: %s", e)

    class _ConfigBadRequest(ValueError):
        pass

    class _ConfigInternalError(RuntimeError):
        pass

    def _validate_proactive_int(
        val: Any, *, name: str, lo: int = 1, hi: int = 50,
    ) -> int:
        """校验 proactive 数值配置项：必须是 [lo, hi] 的正整数字符串。

        挡住负数、零、浮点数(3.5)、字符串(abc)、科学计数(1e5)、空值。
        校验失败抛 _ConfigBadRequest（携带中文提示），由外层返回前端。
        """
        raw = str(val if val is not None else "").strip()
        if not raw:
            raise _ConfigBadRequest(f"{name} 不能为空，需为 {lo}-{hi} 的正整数")
        # 正则一次挡住浮点、负数、科学计数、非数字
        if not re.fullmatch(r"[0-9]+", raw):
            raise _ConfigBadRequest(
                f"{name} 必须是正整数（{lo}-{hi}），当前值无效：{raw!r}"
            )
        n = int(raw)
        if n < lo or n > hi:
            raise _ConfigBadRequest(f"{name} 需为 {lo}-{hi} 的正整数，当前：{n}")
        return n

    def _parse_config_bool(value: Any) -> bool:
        return parse_kvc_bool(value)

    def _encrypt_config_params(params: dict[str, Any]) -> dict[str, Any]:
        encrypted = dict(params)
        for key, val in list(encrypted.items()):
            from jiuwenswarm.extensions.registry import ExtensionRegistry
            if (("api_key" in key.lower() or "token" in key.lower())
                    and ExtensionRegistry.get_instance().get_crypto_provider()):
                encrypted[key] = ExtensionRegistry.get_instance().get_crypto_provider().encrypt(val)
        return encrypted

    def _front_team_template_ids(params: dict[str, Any]) -> set[str] | None:
        teams_raw = params.get("team")
        if teams_raw is None:
            return None
        if not isinstance(teams_raw, list):
            return set()
        template_ids: set[str] = set()
        for team_raw in teams_raw:
            if not isinstance(team_raw, dict):
                continue
            template_id = str(team_raw.get("team_name") or "").strip()
            if template_id:
                template_ids.add(template_id)
        return template_ids

    def _preserve_deleted_team_entities(params: dict[str, Any]) -> None:
        next_template_ids = _front_team_template_ids(params)
        if next_template_ids is None:
            return

        from jiuwenswarm.agents.harness.team import list_team_template_summaries
        from jiuwenswarm.server.runtime.team_binding_store import get_team_binding_store
        from jiuwenswarm.server.runtime.team_entity_store import ensure_team_entity_for_binding

        config_base = get_config()
        current_template_ids = {
            str(item.get("template_id") or "").strip()
            for item in list_team_template_summaries(config_base)
            if str(item.get("source") or "").startswith("modes.team.")
        }
        deleted_template_ids = current_template_ids - next_template_ids
        if not deleted_template_ids:
            return

        failed_team_names: list[str] = []
        for binding in get_team_binding_store().list():
            if binding.template_id not in deleted_template_ids:
                continue
            if ensure_team_entity_for_binding(binding, config_base=config_base) is None:
                failed_team_names.append(binding.team_name)
        if failed_team_names:
            raise _ConfigInternalError(
                "failed to preserve team entity config: " + ", ".join(sorted(failed_team_names))
            )

    def _apply_config_payload(params: dict[str, Any]) -> _ConfigApplyResult:
        """Apply config.set-style payload to .env/config.yaml without triggering reload."""
        params = _encrypt_config_params(params)
        env_updates: dict[str, str] = {}
        yaml_updated: list[str] = []
        codex_dependency_install: dict[str, Any] | None = None
        external_cli_dependency_installs: dict[str, dict[str, Any]] = {}
        available_model_providers = [provider.value for provider in ProviderType]
        raw = get_config_raw()
        preferred_lang = raw.get("preferred_language", "zh")

        try:
            normalize_affinity_request(params)
        except ValueError as exc:
            raise _ConfigBadRequest(str(exc)) from exc

        for param_key, env_key in _CONFIG_SET_ENV_MAP.items():
            if param_key not in params:
                continue
            val = params[param_key]
            if param_key.endswith("_provider") and val and val not in available_model_providers:
                raise _ConfigBadRequest(f"Model provider must in: {available_model_providers} ")
            if val is None:
                env_updates[env_key] = ""
            else:
                env_updates[env_key] = str(val).strip()

        raw = get_config_raw()
        preferred_lang = raw.get("preferred_language", "zh")

        if "agents" in params or "team" in params:
            try:
                skip_team_update = False
                if _team_payload_requests_codex(params):
                    codex_dependency_install = _ensure_codex_dependency_available_or_start_install()
                    if codex_dependency_install is not None:
                        skip_team_update = True
                if "team" in params and not skip_team_update:
                    _preserve_deleted_team_entities(params)
                if not skip_team_update:
                    replace_teams_in_config(_inject_external_cli_publish_url(params))
                    yaml_updated.append("modes.team")
            except ValueError as exc:
                raise _ConfigBadRequest(str(exc)) from exc
            except _ConfigInternalError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[config.set] 写回 modes.team 失败: %s", exc)
                raise _ConfigInternalError("failed to update modes.team") from exc

        external_cli_agents_updated = False
        for param_key in _CONFIG_YAML_KEYS:
            if param_key not in params:
                continue
            val = params[param_key]
            parsed = _parse_config_bool(val)
            try:
                if param_key == "context_engine_enabled":
                    update_context_engine_enabled_in_config(parsed)
                elif param_key == "kv_cache_release_enabled":
                    update_kv_cache_release_enabled_in_config(parsed)
                elif param_key == "kv_cache_affinity_enabled":
                    update_kv_cache_affinity_enabled_in_config(parsed)
                elif param_key == "permissions_enabled":
                    update_permissions_enabled_in_config(parsed)
                elif param_key == "setup_guide_enabled":
                    update_setup_guide_enabled_in_config(parsed)
                elif param_key == "enable_free_models":
                    update_enable_free_models_in_config(parsed)
                elif param_key == "memory_forbidden_enabled":
                    update_memory_forbidden_enabled_in_config(parsed)
                elif param_key == "memory_forbidden_description":
                    desc_val = str(val).strip()
                    update_memory_forbidden_description_in_config({preferred_lang: desc_val})
                elif param_key == "swarmflow_enabled":
                    update_swarmflow_enabled_in_config(parsed)
                elif param_key in _EXTERNAL_CLI_AGENT_CONFIG_KEYS:
                    if not external_cli_agents_updated:
                        try:
                            external_cli_agents = _external_cli_agents_from_switches(raw, params)
                        except ValueError as exc:
                            raise _ConfigBadRequest(str(exc)) from exc
                        if any(item.get("cli_agent") == "codex" for item in external_cli_agents):
                            codex_dependency_install = _ensure_codex_dependency_available_or_start_install()
                            if codex_dependency_install is not None:
                                external_cli_dependency_installs["codex"] = codex_dependency_install
                                external_cli_agents = [
                                    item for item in external_cli_agents if item.get("cli_agent") != "codex"
                                ]
                        if any(item.get("cli_agent") == "claude" for item in external_cli_agents):
                            claude_install = _ensure_claude_dependency_available_or_start_install()
                            if claude_install is not None:
                                external_cli_dependency_installs["claude"] = claude_install
                                external_cli_agents = [
                                    item for item in external_cli_agents if item.get("cli_agent") != "claude"
                                ]
                        update_external_cli_agents_in_config(
                            external_cli_agents,
                            _build_external_cli_publish_url(),
                        )
                        external_cli_agents_updated = True
                elif param_key == "skill_evolution":
                    update_skill_evolution_enabled_in_config(parsed)
                elif param_key.startswith("a2ui_"):
                    ok, update, error = validate_a2ui_config_update(param_key, val)
                    if not ok:
                        raise _ConfigBadRequest(error or "invalid A2UI config")
                    update_a2ui_in_config(update)
                elif param_key == "proactive_recommendation_enabled":
                    update_proactive_recommendation_in_config({"enabled": parsed})
                elif param_key == "proactive_recommendation_max_recommend_per_day":
                    n = _validate_proactive_int(val, name="每日推荐上限(max_recommend_per_day)")
                    update_proactive_recommendation_in_config({"max_recommend_per_day": n})
                elif param_key == "proactive_recommendation_max_rounds_per_tick":
                    n = _validate_proactive_int(val, name="每次检查对话轮数(max_rounds_per_tick)")
                    update_proactive_recommendation_in_config({"max_rounds_per_tick": n})
                yaml_updated.append(param_key)
            except _ConfigBadRequest:
                # proactive 数值校验等：直接返回前端，不被外层吞成 warning
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("[config.set] 写回 config.yaml 失败 %s: %s", param_key, e)
                if param_key == "swarmflow_enabled":
                    raise _ConfigInternalError("failed to update enable_swarmflow") from e
                if param_key in _EXTERNAL_CLI_AGENT_CONFIG_KEYS:
                    raise _ConfigInternalError("failed to update external_cli_agents") from e

        if params.get("model_provider") == ASCEND_AFFINITY_PROVIDER:
            try:
                if update_default_model_provider_in_config(
                    ASCEND_AFFINITY_PROVIDER
                ):
                    yaml_updated.append("models.default_provider")
            except Exception as e:  # noqa: BLE001
                logger.warning("[config.set] 写回默认模型 provider 失败: %s", e)

        symphony_updates = _build_symphony_config_update(params)
        if symphony_updates:
            try:
                update_symphony_in_config(symphony_updates)
                yaml_updated.extend(k for k in _SYMPHONY_CONFIG_KEYS if k in params)
            except Exception as e:
                logger.warning("[config.set] 写回 symphony 失败: %s", e)

        try:
            skill_retrieval_updates = _build_skill_retrieval_config_update(params)
        except ValueError as exc:
            raise _ConfigBadRequest(str(exc)) from exc
        if skill_retrieval_updates:
            try:
                update_skill_retrieval_in_config(skill_retrieval_updates)
                yaml_updated.extend(k for k in _SKILL_RETRIEVAL_CONFIG_KEYS if k in params)
            except Exception as e:
                logger.warning("[config.set] 写回 skill_retrieval 失败: %s", e)

        for env_key, value in env_updates.items():
            os.environ[env_key] = value
        if env_updates:
            _persist_env_updates(env_updates)
            logger.info("[config.set] 已更新 .env: %s", list(env_updates.keys()))
        if yaml_updated:
            logger.info("[config.set] 已更新 config.yaml: %s", yaml_updated)

        kvc_config_changed = any(key in params for key in KVC_CONFIG_KEYS)
        if kvc_config_changed:
            valid, failures = validate_persisted_kv_cache_affinity()
            if not valid:
                # Do not leave a persisted half-success state active. This is a
                # narrow fail-closed correction, not a cross-file transaction.
                update_kv_cache_affinity_enabled_in_config(False)
                raise _ConfigInternalError(
                    "KV cache affinity saved but not applied: " + "; ".join(failures)
                )

        return _ConfigApplyResult(
            env_updates, yaml_updated, codex_dependency_install,
            external_cli_dependency_installs or None,
        )

    async def _apply_config_change_set(change_set: _ConfigChangeSet) -> bool:
        """Synchronously apply only the runtime scope affected by a saved config change."""
        if not change_set.changed:
            return True
        if on_config_saved:
            config_payload = get_config()
            callback_result = on_config_saved(
                change_set.updated_keys,
                env_updates=dict(change_set.env_updates),
                config_payload=config_payload,
                reload_options=change_set.reload_options,
            )
            if inspect.isawaitable(callback_result):
                return bool(await callback_result)
            return bool(callback_result)
        await _clear_agent_config_cache(_resolve(agent_client))
        return True

    def _build_models_defaults_from_frontend(raw_models: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_models, list) or not raw_models:
            raise _ConfigBadRequest("models must be a non-empty list")

        available_model_providers = [p.value for p in ProviderType]
        parsed: list[dict] = []
        aliases_seen: dict[str, int] = {}
        for idx, item in enumerate(raw_models):
            if not isinstance(item, dict):
                raise _ConfigBadRequest(f"models[{idx}] must be object")
            # agentos 备份模型条目不参与 defaults 替换：前端置灰只读展示 agentos，
            # 提交的列表里不应含 agentos；此防御性过滤确保即便误传也不会把
            # agentos 当 defaults 写回 models.defaults（污染 config 结构、丢 _source 标记）
            if item.get("is_agentos") is True:
                continue
            model_name = str(item.get("model_name") or "").strip()
            if not model_name:
                raise _ConfigBadRequest(f"models[{idx}].model_name is required")
            origin_index_raw = item.get("origin_index")
            if origin_index_raw is None:
                origin_index = None
            else:
                try:
                    origin_index = int(origin_index_raw)
                except (TypeError, ValueError):
                    origin_index = None
            api_key = str(item.get("api_key") or "").strip()
            api_base = str(item.get("api_base") or "").strip()
            model_provider = _normalize_provider_value(str(item.get("model_provider") or ""))
            # OpenAIAccount uses the token store managed by core OAuth, so it does not
            # carry a user-entered api_key in config.
            if not api_key and origin_index is None and not is_openai_account_provider(model_provider):
                raise _ConfigBadRequest(f"models[{idx}].api_key is required")
            if model_provider and model_provider not in available_model_providers:
                raise _ConfigBadRequest(f"models[{idx}].model_provider must be one of: {available_model_providers}")
            try:
                temperature = float(item.get("temperature", 0.95))
            except (ValueError, TypeError):
                temperature = 0.95
            try:
                timeout = int(item.get("timeout", 1800))
            except (ValueError, TypeError):
                timeout = 1800
            verify_ssl = bool(item.get("verify_ssl", False))
            is_default = bool(item.get("is_default", False))
            alias = str(item.get("alias") or "").strip()
            # 原样透传给共享校验函数：不要用 `or ""` 压平，否则布尔 False
            # （legacy YAML 裸 off / 非前端客户端传的 JSON false）会被当成清空。
            raw_reasoning_level = item.get("reasoning_level")
            vendor_key = str(item.get("vendor_key") or "").strip() or None
            plan = str(item.get("plan") or "").strip() or None
            if plan:
                from jiuwenswarm.common.model_vendor_registry import PlanKind

                try:
                    plan = PlanKind(plan).value
                except ValueError as exc:
                    raise _ConfigBadRequest(
                        f"models[{idx}].plan must be one of: token_plan, coding_plan, custom_api"
                    ) from exc
                if not vendor_key:
                    raise _ConfigBadRequest(f"models[{idx}].vendor_key is required when plan is set")

            try:
                reasoning_level = validate_reasoning_level_for_model(
                    raw_level=raw_reasoning_level,
                    model_name=model_name,
                    model_provider=model_provider,
                    api_base=api_base,
                    endpoint_profile=item.get("endpoint_profile"),
                )
            except ValueError as reasoning_err:
                raise _ConfigBadRequest(f"models[{idx}].{reasoning_err}") from reasoning_err

            if alias:
                if alias in aliases_seen:
                    prev_idx = aliases_seen[alias]
                    raise _ConfigBadRequest(f"Alias '{alias}' is used by both models[{prev_idx}] and models[{idx}]")
                aliases_seen[alias] = idx

            parsed.append({
                "model_name": model_name,
                "api_base": api_base,
                "api_key": api_key,
                "model_provider": model_provider,
                "temperature": temperature,
                "is_default": is_default,
                "timeout": timeout,
                "verify_ssl": verify_ssl,
                "alias": alias,
                "reasoning_level": reasoning_level or "",
                "origin_index": origin_index,
                # vendor_key is an opaque hint
                # selector; not validated (the selector only ever emits keys
                # present in jiuwenswarm.common.model_vendor_registry). It is
                # persisted so the UI can match a configured entry back to its
                # preset for icon display / re-selection. Not required.
                "vendor_key": vendor_key,
                # plan is the other half of the provider-selection identity.
                # Older entries may have vendor_key only; do not infer a plan.
                "plan": plan,
                # endpoint_profile: OpenAI 协议端点方言(deepseek/openrouter/dashscope/...);
                # opaque passthrough, not validated. Anthropic 协议时 core 忽略此字段。
                # 前端未传时按 api_base host 推断已知自建网关方言(如 vllm)并落库,
                # 否则该类端点的思考开关只会发官方 thinking.type 而被网关忽略。
                "endpoint_profile": effective_endpoint_profile(api_base, item.get("endpoint_profile")),
            })

        # alias 与其他条目的 model_name 冲突校验
        for i, p in enumerate(parsed):
            a = p["alias"]
            if not a:
                continue
            for j, q in enumerate(parsed):
                if i == j:
                    continue
                if q["model_name"] == a:
                    raise _ConfigBadRequest(f"Alias '{a}' on models[{i}] conflicts with model_name on models[{j}]")

        from jiuwenswarm.extensions.registry import ExtensionRegistry
        crypto = ExtensionRegistry.get_instance().get_crypto_provider()

        raw_cfg = get_config_raw()
        raw_defaults = raw_cfg.get("models", {}).get("defaults") if isinstance(raw_cfg, dict) else None
        if not isinstance(raw_defaults, list):
            raw_defaults = []
        resolved_defaults = get_default_models()

        new_models = _merge_models_for_replace_all(parsed, raw_defaults, resolved_defaults, crypto)
        from jiuwenswarm.common.config import _infer_is_default
        return _infer_is_default(new_models)

    async def _config_set(ws, req_id, params, session_id):
        """根据前端消息内容更新配置（支持 .env 与 config.yaml 中的键），并写回对应文件。"""
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        try:
            apply_result = _apply_config_payload(params)
        except _ConfigBadRequest as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST")
            return
        except _ConfigInternalError as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")
            return
        env_updates = apply_result.env_updates
        yaml_updated = apply_result.yaml_updated
        change_set = _ConfigChangeSet(env_updates, yaml_updated)
        try:
            applied_without_restart = await _apply_config_change_set(change_set)
        except Exception as exc:
            logger.warning("[config.set] on_config_saved failed: %s", exc)
            applied_without_restart = False

        if "enable_free_models" in apply_result.yaml_updated:
            try:
                from jiuwenswarm.server.runtime.opencode_zen import warm_zen_free_models
                await warm_zen_free_models(reason="config-toggle")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[config.set] warm_zen_free_models failed: %s", exc)

        updated_param_keys = [k for k, e in _CONFIG_SET_ENV_MAP.items() if e in env_updates] + yaml_updated
        payload = {"updated": updated_param_keys, "applied_without_restart": applied_without_restart}
        if apply_result.codex_dependency_install is not None:
            payload["codex_dependency_install"] = apply_result.codex_dependency_install
        if apply_result.external_cli_dependency_installs is not None:
            payload["external_cli_dependency_installs"] = apply_result.external_cli_dependency_installs
        await channel.send_response(
            ws, req_id, ok=True,
            payload=payload,
        )

    async def _config_validate_model(ws, req_id, params, session_id, max_tokens_bounds=None):
        """Send a minimal chat completion (user message \"Hi\") using draft default-model fields.

        Tries ``max_tokens=infimum_max_tokens`` first to limit cost; if the API rejects it (e.g. minimum output length),
        retries with ``max_tokens=supremum_max_tokens``.
        """
        if max_tokens_bounds is None:
            max_tokens_bounds = {
                "infimum_max_tokens": 3,
                "supremum_max_tokens": 16,
            }

        if isinstance(max_tokens_bounds, dict):
            infimum_max_tokens = max_tokens_bounds.get("infimum_max_tokens")
            supremum_max_tokens = max_tokens_bounds.get("supremum_max_tokens")
        else:
            infimum_max_tokens = 3
            supremum_max_tokens = 16
        infimum_max_tokens = max(infimum_max_tokens, 3)

        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        api_base = str(params.get("api_base") or "").strip()
        api_key = str(params.get("api_key") or "").strip()
        model = str(params.get("model") or "").strip()
        model_provider = _normalize_provider_value(str(params.get("model_provider") or ""))
        needs_api_key = not is_openai_account_provider(model_provider)
        if not all([api_base, model, model_provider]) or (needs_api_key and not api_key):
            await channel.send_response(
                ws, req_id, ok=False,
                error="api_base, model, model_provider, and api_key for non-OAuth providers are required",
                code="BAD_REQUEST",
            )
            return
        available_model_providers = [provider.value for provider in ProviderType]
        if model_provider not in available_model_providers:
            await channel.send_response(
                ws, req_id, ok=False,
                error=f"Model provider must be one of: {available_model_providers}",
                code="BAD_REQUEST",
            )
            return
        api_base = api_base.rstrip("/")

        verify_ssl = bool(params.get("verify_ssl", False))
        # 未显式传方言时按 api_base host 推断已知自建网关(如 vllm)，
        # 保证“测试连接”与保存后的真实运行走同一条 core 路由。
        endpoint_profile = effective_endpoint_profile(api_base, params.get("endpoint_profile"))

        model_config_obj = _resolve_model_config_obj_for_validate(model, params)

        reasoning_mcc = {
            "client_provider": model_provider,
            "endpoint_profile": endpoint_profile,
            "api_base": api_base,
        }
        model_request_config = ModelRequestConfig(
            **build_reasoning_model_request_kwargs(
                model_client_config=reasoning_mcc,
                model_config_obj=model_config_obj,
                model_name=model,
            )
        )
        logger.info(
            "[config.validate_model] final model_request_config for '%s': %s",
            model,
            model_request_config.model_dump(),
        )
        model_client_config = ModelClientConfig(
            client_id="config-validate",
            client_provider=model_provider,
            endpoint_profile=endpoint_profile,
            api_key=api_key,
            api_base=api_base,
            timeout=25.0,
            max_retries=0,
            verify_ssl=verify_ssl,
        )
        # Anthropic-compatible endpoints that send thinking.budget_tokens
        # require max_tokens > budget. Use the actual budget core would emit
        # (effort-mapped or explicit), not a stale 1024 default: many wires
        # (qwen38_anthropic, dashscope_budget) no longer pin 1024, while
        # anthropic_manual maps high → 16384.
        try:
            from openjiuwen.core.foundation.llm.reasoning import resolve_reasoning_plan

            _plan = resolve_reasoning_plan(
                model_client_config,
                model_request_config,
                request_model=model,
            )
            _thinking = (_plan.sdk_params or {}).get("thinking")
            if isinstance(_thinking, dict):
                _budget = _thinking.get("budget_tokens")
                if isinstance(_budget, int) and _budget > 0:
                    supremum_max_tokens = max(supremum_max_tokens, _budget + 16)
        except Exception:  # noqa: BLE001
            logger.debug(
                "[config.validate_model] skip budget floor from reasoning plan",
                exc_info=True,
            )
        llm = Model(model_config=model_request_config, model_client_config=model_client_config)

        async def test_invoke(max_tokens: int):
            return await llm.invoke(
                [{"role": "user", "content": "Hi"}],
                max_tokens=max_tokens,
            )

        try:
            try:
                resp = await test_invoke(infimum_max_tokens)
            except Exception as first_exc:  # noqa: BLE001
                logger.info(
                    "[config.validate_model] max_tokens=%d failed, retrying with %d: %s",
                    infimum_max_tokens,
                    supremum_max_tokens,
                    first_exc,
                )
                try:
                    resp = await test_invoke(supremum_max_tokens)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[config.validate_model] Testing LLM failed: %s", exc)
                    await channel.send_response(
                        ws, req_id, ok=False,
                        error=str(exc).strip() or "LLM request failed",
                        code="LLM_ERROR",
                    )
                    return
        except Exception as exc:  # noqa: BLE001
            logger.warning("[config.validate_model] LLM probe failed: %s", exc)
            await channel.send_response(
                ws, req_id, ok=False,
                error=str(exc).strip() or "LLM request failed",
                code="LLM_ERROR",
            )
            return

        if hasattr(resp, "content"):
            content = resp.content
        elif isinstance(resp, dict):
            content = resp.get("content", "")
        else:
            content = str(resp)
        # For reasoning models (e.g. deepseek-v4-flash), the model may put all
        # tokens into reasoning_content while leaving content empty.  Treat a
        # non-empty reasoning_content as a valid response as well.
        reasoning_content = getattr(resp, "reasoning_content", None) if hasattr(resp, "reasoning_content") else None
        # Some backends report thinking in a field the client does not map at
        # all (e.g. Ollama's "reasoning"), leaving both content and
        # reasoning_content empty.  Generated-token usage still proves the
        # endpoint, credentials, and model name are all valid.
        usage = getattr(resp, "usage_metadata", None)
        output_tokens = usage.get("output_tokens") if isinstance(usage, dict) else getattr(usage, "output_tokens", None)
        has_valid_response = (
            (isinstance(content, str) and content)
            or (isinstance(reasoning_content, str) and reasoning_content)
            or (isinstance(output_tokens, (int, float)) and output_tokens > 0)
        )
        if not has_valid_response:
            await channel.send_response(
                ws, req_id, ok=False,
                error="Empty response from model",
                code="LLM_ERROR",
            )
            return

        await channel.send_response(
            ws, req_id, ok=True,
            payload={"ok": True, "model_provider": model_provider},
        )

    # ── models.* handlers ────────────────────────────────────────

    async def _models_list(ws, req_id, params, session_id):
        """返回已配置的所有默认模型列表（与 config.get 一致，返回解密后的完整值）。

        每条带 ``origin_index`` 指向 ``models.defaults`` 中的位置，配合 replace_all
        在保存时识别"未编辑字段"并保留原 YAML 占位符（如 ``${API_KEY}``）。
        """
        try:
            config = get_config()
            models = get_default_models(config)
            result = []
            active_model = ""
            for idx, entry in enumerate(models):
                mcc = entry.get("model_client_config", {})
                mco = entry.get("model_config_obj", {})
                is_default = entry.get("is_default", False)
                model_name = mcc.get("model_name", "")
                try:
                    context_window_tokens = resolve_context_window_tokens(
                        model_name=model_name,
                        context_engine_config=(config.get("react", {}) or {}),
                        model_config_obj=mco,
                    )
                except Exception:
                    context_window_tokens = 0
                    logger.debug(
                        "Failed to resolve context_window_tokens for model %s",
                        model_name,
                        exc_info=True,
                    )
                result.append({
                    "model_name": model_name,
                    "api_base": mcc.get("api_base", ""),
                    "api_key": mcc.get("api_key", ""),
                    "model_provider": mcc.get("client_provider", ""),
                    "temperature": mco.get("temperature", 0.95),
                    "reasoning_level": _reasoning_level_display(mco.get("reasoning_level")),
                    "is_default": is_default,
                    # agentos 备份模型标记：由 get_default_models 经 _source=="agentos"
                    # 注入。前端据此区分 defaults / agentos，置灰只读展示 agentos、
                    # 并让 agentos 进 ModelSelector 下拉（is_default!==false || is_agentos）
                    "is_agentos": bool(mco.get("_source") == "agentos"),
                    "alias": entry.get("alias", ""),
                    "origin_index": idx,
                    "context_window_tokens": context_window_tokens,
                    "vendor_key": mcc.get("vendor_key") or entry.get("vendor_key") or "",
                    "plan": mcc.get("plan") or entry.get("plan") or "",
                    "endpoint_profile": mcc.get("endpoint_profile") or "",
                })
            # Zen 免费模型仅存在于进程内缓存，不能写回 models.defaults；但需要
            # 与普通模型一同出现在会话选择器中。is_default 保持 None（而不是
            # False），使前端把它视为可选模型，同时不会改变首个配置模型作为
            # active_model 的既有语义。
            try:
                from jiuwenswarm.server.runtime.opencode_zen import (
                    get_zen_free_model_entries,
                )

                existing_names = {str(item.get("model_name") or "") for item in result}
                for entry in get_zen_free_model_entries():
                    mcc = entry.get("model_client_config") or {}
                    mco = entry.get("model_config_obj") or {}
                    model_name = str(mcc.get("model_name") or "").strip()
                    if not model_name or model_name in existing_names:
                        continue
                    result.append({
                        "model_name": model_name,
                        "api_base": mcc.get("api_base", ""),
                        "api_key": mcc.get("api_key", ""),
                        "model_provider": mcc.get("client_provider", ""),
                        "temperature": mco.get("temperature", 0.95),
                        "reasoning_level": _reasoning_level_display(mco.get("reasoning_level")),
                        "is_default": entry.get("is_default"),
                        "is_agentos": False,
                        "is_free": True,
                        "alias": entry.get("alias", ""),
                        "context_window_tokens": entry.get("context_window_tokens", 0),
                    })
                    existing_names.add(model_name)
            except Exception:
                logger.warning("[models.list] append Zen free models failed", exc_info=True)

            # active_model 为列表首位的模型（主对话默认）
            active_model = result[0]["model_name"] if result else ""
            await channel.send_response(ws, req_id, ok=True, payload={
                "models": result,
                "active_model": active_model,
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("[models.list] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _models_replace_all(ws, req_id, params, session_id):
        """原子地用提交的列表整体替换 models.defaults。

        前端在保存配置时一次性提交完整的最终列表，避免按 model_name/index 分多步
        save+remove 在同 model_name 多条目场景下出现的位置覆写、漏删等问题。

        每条 entry 可携带 ``origin_index`` 指向 ``models.defaults`` 中的原始位置；
        命中后 raw YAML 中的占位符（如 ``${API_KEY}``）以及 custom_headers 等未在
        前端暴露的字段会被保留，仅当字段值与前端最初看到的解析值不一致时才覆写。
        """
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        try:
            new_models = _build_models_defaults_from_frontend(params.get("models"))
            default_provider = default_model_provider_from_entries(new_models)
            if (
                is_affinity_enabled(get_config_raw())
                and default_provider != ASCEND_AFFINITY_PROVIDER
            ):
                update_kv_cache_affinity_enabled_in_config(False)
            update_default_models_in_config(new_models)

            applied_without_restart = await _apply_config_change_set(
                _ConfigChangeSet({}, ["models.defaults"], force=True)
            )

            await channel.send_response(ws, req_id, ok=True, payload={
                "count": len(new_models),
                "applied_without_restart": applied_without_restart,
            })
        except _ConfigBadRequest as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[models.replace_all] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _config_save_all(ws, req_id, params, session_id):
        """Batch-save config panel changes and trigger a single hot reload.

        Accepted payload keys:
        - config: config.set-style key/value updates
        - models: complete models.defaults draft list
        - agents/team: team editor payload
        """
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return

        env_updates: dict[str, str] = {}
        yaml_updated: list[str] = []
        models_count: int | None = None

        try:
            new_models: list[dict[str, Any]] | None = None
            if "models" in params:
                new_models = _build_models_defaults_from_frontend(params.get("models"))

            config_params: dict[str, Any] = {}
            raw_config_params = params.get("config")
            if raw_config_params is not None:
                if not isinstance(raw_config_params, dict):
                    raise _ConfigBadRequest("config must be object")
                config_params.update(raw_config_params)

            if "agents" in params:
                config_params["agents"] = params.get("agents")
            if "team" in params:
                config_params["team"] = params.get("team")

            affinity_requested = _parse_config_bool(config_params.get("kv_cache_affinity_enabled"))
            if affinity_requested and new_models is not None:
                if set_default_model_provider_in_entries(
                    new_models,
                    ASCEND_AFFINITY_PROVIDER,
                ):
                    yaml_updated.append("models.default_provider")

            if config_params:
                apply_result = _apply_config_payload(config_params)
                applied_env = apply_result.env_updates
                applied_yaml = apply_result.yaml_updated
                env_updates.update(applied_env)
                yaml_updated.extend(applied_yaml)
            else:
                apply_result = _ConfigApplyResult({}, [])

            if new_models is not None:
                default_provider = default_model_provider_from_entries(new_models)
                if (
                    is_affinity_enabled(get_config_raw())
                    and default_provider != ASCEND_AFFINITY_PROVIDER
                ):
                    update_kv_cache_affinity_enabled_in_config(False)
                    yaml_updated.append("kv_cache_affinity_enabled")
                update_default_models_in_config(new_models)
                yaml_updated.append("models.defaults")
                models_count = len(new_models)

            kvc_config_changed = new_models is not None or any(
                key in config_params for key in KVC_CONFIG_KEYS
            )
            if kvc_config_changed:
                valid, failures = validate_persisted_kv_cache_affinity()
                if not valid:
                    update_kv_cache_affinity_enabled_in_config(False)
                    raise _ConfigInternalError(
                        "KV cache affinity saved but not applied: " + "; ".join(failures)
                    )

            change_set = _ConfigChangeSet(
                env_updates,
                yaml_updated,
                force=bool(env_updates or yaml_updated),
            )
            applied_without_restart = await _apply_config_change_set(change_set)

            if "enable_free_models" in yaml_updated:
                try:
                    from jiuwenswarm.server.runtime.opencode_zen import warm_zen_free_models
                    await warm_zen_free_models(reason="config-toggle")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[config.save_all] warm_zen_free_models failed: %s", exc)

            payload = {
                "updated": [k for k, e in _CONFIG_SET_ENV_MAP.items() if e in env_updates] + yaml_updated,
                "applied_without_restart": applied_without_restart,
                "models_count": models_count,
            }
            if apply_result.codex_dependency_install is not None:
                payload["codex_dependency_install"] = apply_result.codex_dependency_install
            if apply_result.external_cli_dependency_installs is not None:
                payload["external_cli_dependency_installs"] = apply_result.external_cli_dependency_installs

            await channel.send_response(
                ws,
                req_id,
                ok=True,
                payload=payload,
            )
        except _ConfigBadRequest as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST")
        except _ConfigInternalError as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[config.save_all] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _models_validate(ws, req_id, params, session_id):
        """测试指定模型配置是否可用（复用 config.validate_model 逻辑）。"""
        await _config_validate_model(ws, req_id, params, session_id)

    async def _channel_get(ws, req_id, params, session_id):
        """返回已注册的 channel 列表."""
        cm = _resolve(channel_manager)
        if cm is not None:
            channels = [{"channel_id": cid} for cid in cm.enabled_channels]
        else:
            channels = []
        await channel.send_response(ws, req_id, ok=True, payload={"channels": channels})

    # ── vendors.* handlers ──────────────

    async def _vendors_list(ws, req_id, params, session_id):
        """返回按 plan 分组的厂商预设列表(供前端 Tab+厂商卡片渲染)。

        纯数据,读 ``jiuwenswarm.common.model_vendor_registry``,无副作用。
        """
        del params, session_id
        try:
            from jiuwenswarm.common.model_vendor_registry import to_frontend_payload
            await channel.send_response(
                ws, req_id, ok=True,
                payload={"vendors": to_frontend_payload()},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[vendors.list] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _vendors_fetch_models(ws, req_id, params, session_id):
        """按厂商预设拉取远端可用模型列表(供前端"拉取最新"按钮)。

        params: {vendor_key, plan, api_key?}。按预设的 ``models_needs_key``
        决定是否带 Authorization;失败/无端点优雅回退预设列表,永不报错。
        """
        del session_id
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        vendor_key = str(params.get("vendor_key") or "").strip()
        plan_raw = str(params.get("plan") or "").strip()
        api_key = str(params.get("api_key") or "").strip()
        try:
            from jiuwenswarm.common.model_vendor_registry import (
                PlanKind,
                get_preset,
            )

            def _fetch_remote_sync(
                endpoint: str, hdrs: dict[str, str]
            ) -> tuple[list[str], str]:
                """同步拉取并解析 /models(在线程池里跑,不阻塞事件循环)。

                成功返回 ``(模型 ID 列表, "")``;任何失败(网络/状态码非 200/
                解析失败/空)返回 ``([], 原因)``。原因透传给前端,写进回退响应的
                ``reason`` 字段,便于联调时区分鉴权失败/端点问题/限流/网络异常,
                而非笼统的"拉取失败"。永不抛异常。
                """
                import httpx  # noqa: PLC0415
                from openjiuwen.extensions.external_provider.openai_auth.openai_account_models import (
                    parse_openai_account_model_ids,
                )
                try:
                    resp = httpx.get(endpoint, headers=hdrs, timeout=12.0)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[vendors.fetch_models] http error: %s", exc)
                    return [], f"remote fetch error: {type(exc).__name__}"
                if resp.status_code != 200:
                    # 401/403 = key 错或鉴权方式不符(如 maas 需华为签名);
                    # 429 = 限流;5xx = 上游故障。把状态码透传给前端。
                    return [], f"remote returned HTTP {resp.status_code}"
                try:
                    payload_json = resp.json()
                except ValueError:
                    return [], "remote returned non-JSON body"
                if not isinstance(payload_json, dict):
                    return [], "remote returned unexpected body shape"
                try:
                    ids = parse_openai_account_model_ids(payload_json)
                except Exception:  # noqa: BLE001
                    return [], "remote payload parse failed"
                if not ids:
                    return [], "remote returned empty model list"
                return ids, ""

            try:
                plan = PlanKind(plan_raw)
            except ValueError:
                await channel.send_response(
                    ws, req_id, ok=False,
                    error=f"invalid plan: {plan_raw}",
                    code="BAD_REQUEST",
                )
                return

            preset = get_preset(vendor_key, plan)
            if preset is None:
                await channel.send_response(
                    ws, req_id, ok=False,
                    error=f"unknown vendor/plan: {vendor_key}/{plan_raw}",
                    code="BAD_REQUEST",
                )
                return

            # 无远端端点(Maas 等) -> 直接回退预设
            if not preset.models_endpoint:
                await channel.send_response(
                    ws, req_id, ok=True,
                    payload={
                        "models": list(preset.model_options),
                        "source": "preset",
                        "reason": "no remote models endpoint",
                    },
                )
                return

            headers: dict[str, str] = {"Accept": "application/json"}
            if preset.models_needs_key:
                if not api_key:
                    # 需 key 但未提供 -> 回退预设
                    await channel.send_response(
                        ws, req_id, ok=True,
                        payload={
                            "models": list(preset.model_options),
                            "source": "preset",
                            "reason": "api_key required for fetch",
                        },
                    )
                    return
                headers["Authorization"] = f"Bearer {api_key}"

            # 同步 httpx.get 通过 asyncio.to_thread 卸载到线程池,避免阻塞事件循环
            # (与同文件 _openai_account_*_payload / _updater_* 的 to_thread 模式一致)。
            remote_ids, remote_reason = await asyncio.to_thread(
                _fetch_remote_sync, preset.models_endpoint, headers,
            )

            if remote_ids:
                await channel.send_response(
                    ws, req_id, ok=True,
                    payload={"models": remote_ids, "source": "remote"},
                )
                return

            # 远端失败/空 -> 回退预设。把远端状态码/原因透传给前端,联调时能
            # 分辨是鉴权失败(key 错 / 需厂商专属签名)、限流、还是端点不可达,
            # 而非笼统的"拉取报错"。注意:仍回 source="preset" + ok=True,保证
            # 拉取失败不阻塞前端选模型,只是可见地说明为何回退。
            await channel.send_response(
                ws, req_id, ok=True,
                payload={
                    "models": list(preset.model_options),
                    "source": "preset",
                    "reason": remote_reason or "remote fetch failed or empty",
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[vendors.fetch_models] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _openai_account_auth_status(ws, req_id, params, session_id):
        del params, session_id
        try:
            payload = await asyncio.to_thread(_openai_account_auth_status_payload)
            await channel.send_response(ws, req_id, ok=True, payload=payload)
        except OpenAIAccountAuthError as exc:
            logger.warning("[openai_account.auth.status] %s", exc)
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc),
                code=exc.code,
                payload=_openai_account_auth_error_payload(exc),
            )
        except _OPENAI_ACCOUNT_LOCAL_ERRORS as exc:
            logger.warning("[openai_account.auth.status] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _openai_account_auth_start_login(ws, req_id, params, session_id):
        del params, session_id
        try:
            payload = await asyncio.to_thread(_openai_account_start_login_payload)
            await channel.send_response(
                ws,
                req_id,
                ok=True,
                payload=payload,
            )
        except OpenAIAccountAuthError as exc:
            logger.warning("[openai_account.auth.start_login] %s", exc)
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc),
                code=exc.code,
                payload=_openai_account_auth_error_payload(exc),
            )
        except _OPENAI_ACCOUNT_LOCAL_ERRORS as exc:
            logger.warning("[openai_account.auth.start_login] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _openai_account_auth_pending_login(ws, req_id, params, session_id):
        del params, session_id
        try:
            payload = await asyncio.to_thread(_openai_account_pending_login_payload)
            await channel.send_response(ws, req_id, ok=True, payload=payload)
        except OpenAIAccountAuthError as exc:
            logger.warning("[openai_account.auth.pending_login] %s", exc)
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc),
                code=exc.code,
                payload=_openai_account_auth_error_payload(exc),
            )
        except _OPENAI_ACCOUNT_LOCAL_ERRORS as exc:
            logger.warning("[openai_account.auth.pending_login] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _openai_account_auth_poll_login(ws, req_id, params, session_id):
        del session_id
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        login_id = str(params.get("login_id") or "").strip()
        if not login_id:
            await channel.send_response(ws, req_id, ok=False, error="login_id is required", code="BAD_REQUEST")
            return
        try:
            payload = await asyncio.to_thread(_openai_account_poll_login_payload, login_id)
            await channel.send_response(
                ws,
                req_id,
                ok=True,
                payload=payload,
            )
        except OpenAIAccountAuthError as exc:
            logger.warning("[openai_account.auth.poll_login] %s", exc)
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc),
                code=exc.code,
                payload=_openai_account_auth_error_payload(exc),
            )
        except _OPENAI_ACCOUNT_LOCAL_ERRORS as exc:
            logger.warning("[openai_account.auth.poll_login] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _openai_account_auth_logout(ws, req_id, params, session_id):
        del params, session_id
        try:
            payload = await asyncio.to_thread(_openai_account_logout_payload)
            await channel.send_response(
                ws,
                req_id,
                ok=True,
                payload=payload,
            )
        except OpenAIAccountAuthError as exc:
            logger.warning("[openai_account.auth.logout] %s", exc)
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc),
                code=exc.code,
                payload=_openai_account_auth_error_payload(exc),
            )
        except _OPENAI_ACCOUNT_LOCAL_ERRORS as exc:
            logger.warning("[openai_account.auth.logout] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _openai_account_models_list(ws, req_id, params, session_id):
        del params, session_id
        try:
            payload = await asyncio.to_thread(_openai_account_models_payload)
            await channel.send_response(
                ws,
                req_id,
                ok=True,
                payload=payload,
            )
        except OpenAIAccountAuthError as exc:
            logger.warning("[openai_account.models.list] %s", exc)
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc),
                code=exc.code,
                payload=_openai_account_auth_error_payload(exc),
            )
        except OpenAIAccountModelListError as exc:
            logger.warning("[openai_account.models.list] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="MODEL_LIST_ERROR")
        except _OPENAI_ACCOUNT_LOCAL_ERRORS as exc:
            logger.warning("[openai_account.models.list] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _updater_get_status(ws, req_id, params, session_id):
        service = updater_service or UpdaterService()
        await channel.send_response(ws, req_id, ok=True, payload=service.get_status())

    async def _updater_check(ws, req_id, params, session_id):
        service = updater_service or UpdaterService()
        manual = bool((params or {}).get("manual", False)) if isinstance(params, dict) else False
        payload = await asyncio.to_thread(service.check, manual)
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _updater_download(ws, req_id, params, session_id):
        service = updater_service or UpdaterService()
        payload = service.start_download()
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _updater_upgrade(ws, req_id, params, session_id):
        service = updater_service or UpdaterService()
        payload = await asyncio.to_thread(service.start_upgrade)
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _updater_get_conf(ws, req_id, params, session_id):
        service = updater_service or UpdaterService()
        await channel.send_response(ws, req_id, ok=True, payload=service.get_runtime_config())

    async def _updater_reset_source(ws, req_id, params, session_id):
        try:
            update_updater_in_config(dict(DEFAULT_SOURCE_CONFIG))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[updater.reset_source] 写回 config.yaml 失败: %s", exc)
            await channel.send_response(ws, req_id, ok=False,
                                        error=str(exc), code="INTERNAL_ERROR")
            return

        service = updater_service or UpdaterService()
        await channel.send_response(ws, req_id, ok=True, payload=service.get_runtime_config())

    async def _updater_set_conf(ws, req_id, params, session_id):
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return

        updates: dict[str, Any] = {}
        if "enabled" in params:
            updates["enabled"] = bool(params.get("enabled"))
        for key in ("repo_owner", "repo_name", "release_api_url", "asset_name_pattern",
                "release_api_type", "pypi_mirror"):
            if key in params:
                updates[key] = str(params.get(key) or "").strip()
        for plat in ("windows", "macos", "linux"):
            key = f"asset_name_pattern_{plat}"
            if key in params:
                updates[key] = str(params.get(key) or "").strip()
        if "timeout_seconds" in params:
            try:
                updates["timeout_seconds"] = max(5, int(params.get("timeout_seconds")))
            except (TypeError, ValueError):
                await channel.send_response(ws, req_id, ok=False,
                                            error="timeout_seconds must be integer", code="BAD_REQUEST")
                return

        try:
            update_updater_in_config(updates)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[updater.set_conf] 写回 config.yaml 失败: %s", exc)
            await channel.send_response(ws, req_id, ok=False,
                                        error=str(exc), code="INTERNAL_ERROR")
            return

        service = updater_service or UpdaterService()
        await channel.send_response(ws, req_id, ok=True, payload=service.get_runtime_config())

    async def _session_list(ws, req_id, params, session_id, user_id=None):
        """返回会话列表，包含完整的会话管理信息。

        经统一薄代理 E2A 转发目标 AgentServer（SessionAdapter 处理），
        AgentServer 输出与迁移前 Web fallback 投影（to_session_info）完全一致。
        """
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import (
            is_legacy_shared_directory_client,
            proxy_unary_request,
        )

        real_client = _resolve(agent_client)
        # Only the default local WebSocket client shares Gateway's directory.
        # Other extension clients may be remote; never substitute deployment
        # state for their user state when they are unavailable.
        if (
            is_legacy_shared_directory_client(real_client)
            and not getattr(real_client, "server_ready", True)
        ):
            from jiuwenswarm.server.runtime.gateway_adapter.base import parse_int_param
            from jiuwenswarm.server.runtime.session.session_info import to_session_info
            from jiuwenswarm.server.runtime.session.session_metadata import get_all_sessions_metadata

            raw_params = params if isinstance(params, dict) else {}
            limit = parse_int_param(raw_params, "limit", 20, minimum=1, maximum=200)
            offset = parse_int_param(raw_params, "offset", 0, minimum=0, maximum=10**9)
            sessions, total = get_all_sessions_metadata(limit=limit, offset=offset)
            await channel.send_response(
                ws,
                req_id,
                ok=True,
                payload={
                    "sessions": [to_session_info(item) for item in sessions],
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                },
            )
            return

        await proxy_unary_request(
            channel=channel,
            agent_client=real_client,
            ws=ws,
            req_id=req_id,
            params=params,
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.SESSION_LIST,
            label="session.list",
        )

    async def _session_get_metadata(ws, req_id, params, session_id, user_id=None):
        """返回单个会话的元数据（mode / model / project_dir / last_user_message_at 等）。

        经统一薄代理 E2A 转发目标 AgentServer（SessionAdapter
        SESSION_GET_METADATA），由注入目录读取（O(1)，不扫描目录）。
        """
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve(agent_client),
            ws=ws,
            req_id=req_id,
            params=params,
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.SESSION_GET_METADATA,
            label="session.get_metadata",
        )

    async def _session_create(ws, req_id, params, session_id, user_id=None):
        """创建一个新 session（在 agent/sessions 下创建一个新目录）。

        project_id / project_dir / work_mode 绑定规则:
          - work_mode 归一化(详见 resolve_session_work_mode_params):未传时按通道
            推断(Web→work,TUI→code);显式传非法值返回 BAD_REQUEST;
          - project_id / project_dir 绑定规则(详见 project_store.resolve_session_project_binding):
            两者皆空(或 project_id 为 "default"/"default_code" 且 path 为空)→ 默认项目;
            仅传 project_id → 按项目记录自动补齐 project_dir;
            同时传 project_id + project_dir → 校验与项目绑定路径一致,不一致报错;
            仅传 project_dir 而无有效 project_id → 拒绝(BAD_REQUEST)。
          - 真实 project_id 命中后,最终 work_mode 以 Project 记录为准;若请求显式
            传了 work_mode 且与 Project 记录不一致,返回 BAD_REQUEST。
        绑定后 project_id / project_dir / work_mode 不可变(首次锁定)。
        """
        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST",
            )
            return
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        # 统一薄代理 E2A 转发（envelope.user_id 承载认证身份，
        # drop session_id / 缺省注入 create_token 保持与迁移前一致）。
        create_params = dict(params)
        create_params.setdefault("create_token", secrets.token_hex(16))
        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve(agent_client),
            ws=ws,
            req_id=req_id,
            params=create_params,
            session_id=None,
            user_id=user_id,
            req_method=ReqMethod.SESSION_CREATE,
            label="session.create",
            drop_params=("session_id",),
            default_error_code="SESSION_CREATE_FAILED",
        )

    async def _session_rename(ws, req_id, params, session_id, user_id=None):
        """重命名会话标题(查询/设置/清除三种语义)。

        经统一薄代理 E2A 转发目标 AgentServer（SESSION_RENAME，
        AgentServer 注入目录 session_metadata），不再 Gateway 本地直写——AgentOS
        下重命名必须落在用户 AgentServer 的会话目录（方案 §10.3 契约）。
        title 不传→查询、空串/纯空白→清除、非空→设置(截断 200 字符)。
        """
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import is_legacy_shared_directory_client
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        real_client = _resolve(agent_client)
        # Only the default local WebSocket client shares the Gateway directory.
        if (
            is_legacy_shared_directory_client(real_client)
            and not getattr(real_client, "server_ready", True)
        ):
            from jiuwenswarm.server.runtime.session.session_rename import apply_session_rename

            ok, payload, error, code = apply_session_rename(
                params if isinstance(params, dict) else {},
                session_id,
                init_channel_id=channel.channel_id,
            )
            await channel.send_response(
                ws, req_id, ok=ok, payload=payload or {},
                error=None if ok else error or "session.rename failed",
                code=None if ok else code,
            )
            return

        await proxy_unary_request(
            channel=channel,
            agent_client=real_client,
            ws=ws,
            req_id=req_id,
            params=params,
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.SESSION_RENAME,
            label="session.rename",
        )

    async def _session_pin(ws, req_id, params, session_id, user_id=None):
        """置顶/取消置顶会话,操作后对所有置顶会话紧凑重编号为 1..N。幂等。

        经统一薄代理 E2A 转发目标 AgentServer（SessionAdapter
        SESSION_PIN，AgentServer 注入目录 session_metadata）。
        """
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import (
            is_legacy_shared_directory_client,
            proxy_unary_request,
        )

        real_client = _resolve(agent_client)
        if (
            is_legacy_shared_directory_client(real_client)
            and not getattr(real_client, "server_ready", True)
        ):
            raw_params = params if isinstance(params, dict) else {}
            sid = raw_params.get("session_id")
            pinned = raw_params.get("pinned")
            if not isinstance(sid, str) or not sid.strip():
                await channel.send_response(ws, req_id, ok=False, error="session_id is required", code="BAD_REQUEST")
                return
            if not isinstance(pinned, bool):
                await channel.send_response(ws, req_id, ok=False, error="pinned must be boolean", code="BAD_REQUEST")
                return
            from jiuwenswarm.server.runtime.session.session_metadata import set_session_pinned

            result = set_session_pinned(sid.strip(), pinned)
            if result is None:
                await channel.send_response(ws, req_id, ok=False, error="session not found", code="NOT_FOUND")
                return
            new_pinned, new_order = result
            await channel.send_response(ws, req_id, ok=True, payload={"pinned": new_pinned, "pin_order": new_order})
            return

        await proxy_unary_request(
            channel=channel,
            agent_client=real_client,
            ws=ws,
            req_id=req_id,
            params=params,
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.SESSION_PIN,
            label="session.pin",
        )

    async def _session_delete(ws, req_id, params, session_id, user_id=None):
        """删除一个 session（统一薄代理 E2A 转发 + 单用户共享目录适配器 fallback）。

        手写 E2A 与本地 ``_delete_from_shared_dir`` 收敛到
        ``proxy_unary_request``——单用户 WebSocket 客户端在 AgentServer 不可达时
        由薄代理跑 SessionAdapter 的文件级删除（共享目录等价）；AgentOS 与
        client 未构造（ac=None）时返回可重试 SERVICE_UNAVAILABLE（决策 D8）。
        """
        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST",
            )
            return
        session_id_to_delete = params.get("session_id")
        if not isinstance(session_id_to_delete, str) or not session_id_to_delete.strip():
            await channel.send_response(
                ws, req_id, ok=False, error="session_id is required", code="BAD_REQUEST",
            )
            return

        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve(agent_client),
            ws=ws,
            req_id=req_id,
            params=params,
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.SESSION_DELETE,
            label="session.delete",
        )

    async def _project_list(ws, req_id, params, session_id, user_id=None):
        """获取项目列表(含统计),已排序,包含默认项目。

        filter: ``"all"``(默认) / ``"pinned"`` / ``"unpinned"``
        include_hidden: 是否包含已软删除(``hidden:true``)项目,默认 ``false``。
            仅 ``"all"`` / ``"unpinned"`` 生效;``"pinned"`` 模式自动排除隐藏项目。
        work_mode: 可选,按工作模式过滤(``"code"`` / ``"work"``),不传则返回全部模式。
            默认项目按 work_mode 拆分:``default``(work)+ ``default_code``(code)。

        统计口径: ``session_count`` / ``last_message_at`` / ``last_user_message_at``
        仅统计该项目的非置顶**普通**会话(``cron_id`` 为空)。置顶会话与 cron 会话
        不计入任何项目统计。隐藏项目统计恒为 0/null(其非置顶会话已临时归属默认项目)。
        """
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve(agent_client),
            ws=ws,
            req_id=req_id,
            params=params if isinstance(params, dict) else {},
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.PROJECT_LIST,
            label="project.list",
        )

    async def _project_get_sessions(ws, req_id, params, session_id, user_id=None):
        """获取项目下的非置顶普通会话列表（目标 AgentServer 执行）。"""
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve(agent_client),
            ws=ws,
            req_id=req_id,
            params=params if isinstance(params, dict) else {},
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.PROJECT_GET_SESSIONS,
            label="project.get_sessions",
        )

    async def _project_get_cron_sessions(ws, req_id, params, session_id, user_id=None):
        """获取项目 cron 会话（目标 AgentServer 执行）。"""
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve(agent_client),
            ws=ws,
            req_id=req_id,
            params=params if isinstance(params, dict) else {},
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.PROJECT_GET_CRON_SESSIONS,
            label="project.get_cron_sessions",
        )

    async def _project_create(ws, req_id, params, session_id, user_id=None):
        """Forward project creation to the target AgentServer.

        The AgentServer validates paths and owns project/Git state in its
        injected user directory. Gateway only schedules deployment-side warm
        key reconciliation after a successful mutation.
        """
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve(agent_client),
            ws=ws,
            req_id=req_id,
            params=params if isinstance(params, dict) else {},
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.PROJECT_CREATE,
            label="project.create",
            on_done=lambda ok, _payload: (
                _schedule_agent_prewarm_sync("project.create") if ok else None
            ),
        )

    async def _project_rename(ws, req_id, params, session_id, user_id=None):
        """重命名项目,仅修改展示名,不改动工作目录路径。

        默认项目禁止重命名(``FORBIDDEN``)。``name`` 与已有项目(含隐藏)重复时返回 ``CONFLICT``。
        ``name`` 含文件系统非法字符 / 为保留设备名时返回 ``BAD_REQUEST``。
        """
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve(agent_client),
            ws=ws,
            req_id=req_id,
            params=params if isinstance(params, dict) else {},
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.PROJECT_RENAME,
            label="project.rename",
        )

    async def _project_pin(ws, req_id, params, session_id, user_id=None):
        """置顶/取消置顶项目,操作后对所有置顶项目紧凑重编号为 1..N。幂等。

        默认项目禁止置顶(``FORBIDDEN``)。新置顶项目 ``pin_order`` 默认 0,
        重编号后置于置顶区顶部(与 ``session.pin`` 行为一致)。
        """
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve(agent_client),
            ws=ws,
            req_id=req_id,
            params=params if isinstance(params, dict) else {},
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.PROJECT_PIN,
            label="project.pin",
        )

    async def _project_remove(ws, req_id, params, session_id, user_id=None):
        """Forward project soft-deletion; clean Gateway Git watchers on success."""
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        project_id = str((params or {}).get("project_id") or "").strip()

        def _after_remove(ok: bool, _payload: object) -> None:
            if not ok:
                return
            registry = getattr(channel, "git_watcher_registry", None)
            if registry is not None and project_id:
                registry.cleanup_project(project_id)
            _schedule_agent_prewarm_sync("project.remove")

        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve(agent_client),
            ws=ws,
            req_id=req_id,
            params=params if isinstance(params, dict) else {},
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.PROJECT_REMOVE,
            label="project.remove",
            on_done=_after_remove,
        )

    async def _project_restore(ws, req_id, params, session_id, user_id=None):
        """Forward project restoration to the target AgentServer."""
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve(agent_client),
            ws=ws,
            req_id=req_id,
            params=params if isinstance(params, dict) else {},
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.PROJECT_RESTORE,
            label="project.restore",
            on_done=lambda ok, _payload: (
                _schedule_agent_prewarm_sync("project.restore") if ok else None
            ),
        )

    async def _project_info(ws, req_id, params, session_id, user_id=None):
        """获取单个项目详情(含统计),支持虚拟默认项目。

        ``project_id`` 为 ``"default"`` / ``"default_code"`` 时返回对应虚拟默认项目;
        为真实 project_id 时返回该项目的详情。字段与 ``project.list`` 条目一致。

        统计口径同 ``project.list``:仅统计该项目的非置顶普通会话(``cron_id`` 为空)。
        """
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve(agent_client),
            ws=ws,
            req_id=req_id,
            params=params if isinstance(params, dict) else {},
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.PROJECT_INFO,
            label="project.info",
        )

    async def _project_pinned_sessions(ws, req_id, params, session_id, user_id=None):
        """获取全部置顶会话,按 ``pin_order`` 升序排列。

        置顶会话已从项目分组中剥离,通过本接口独立获取。``project_dir`` 仍指向
        原归属项目。不接受任何参数。
        """
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve(agent_client),
            ws=ws,
            req_id=req_id,
            params=params if isinstance(params, dict) else {},
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.PROJECT_PINNED_SESSIONS,
            label="project.pinned_sessions",
        )

    # ── Git RPC thin proxies ────────────────────────────────────────────────
    # Worktree, project-store, and diff-history access executes only in the
    # routed AgentServer. Gateway retains the watcher notification below,
    # because the /ws/git connection lifecycle remains deployment-side.

    def _mark_git_watcher_dirty(project_id: str) -> None:
        """写操作成功后唤醒 /ws/git watcher(阶段 10 注入后生效)。

        阶段 7 时 ``git_watcher_registry`` 属性可能尚未注入,此处防御性调用;
        阶段 10 在 WebChannel 构造后注入 ``git_watcher_registry`` 即自动启用。
        """
        registry = getattr(channel, "git_watcher_registry", None)
        if registry is None:
            return
        try:
            registry.mark_dirty(project_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[ProjectGit] mark_dirty failed (project=%s): %s",
                project_id, exc,
            )

    from jiuwenswarm.common.schema.message import ReqMethod

    def _make_git_proxy_handler(
        req_method, label, *, doc, mark_dirty=False, mark_dirty_on_failure=False
    ):
        """构造一个 Git RPC 薄代理 handler。
        Git 工作区/项目表/diff 历史访问只发生在目标 AgentServer（E2A 转发）；
        Gateway 仅保留 /ws/git watcher 唤醒（mark_dirty）作为部署侧连接维护。
        统一 ``user_id=None``（AgentOS 下由路由层按连接上下文注入）与
        ``preserve_error_payload=True``（Git 已定义结构化错误明细）。
        """

        async def _handler(ws, req_id, params, session_id, user_id=None):
            from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

            callbacks = {}
            if mark_dirty or mark_dirty_on_failure:
                project_id = str((params or {}).get("project_id") or "").strip()

                def _on_done(ok, _payload):
                    # 成功与"已发生局部变更的失败"都唤醒 watcher（commit 失败但 index
                    # 已改变时同样需要刷新）；是否行动由调用方在回调内判定。
                    if ok or mark_dirty_on_failure:
                        return _mark_git_watcher_dirty(project_id)
                    return None

                callbacks["on_done"] = _on_done
            await proxy_unary_request(
                channel=channel,
                agent_client=_resolve(agent_client),
                ws=ws,
                req_id=req_id,
                params=params if isinstance(params, dict) else {},
                session_id=session_id,
                user_id=user_id,
                req_method=req_method,
                label=label,
                preserve_error_payload=True,
                **callbacks,
            )

        _handler.__doc__ = doc
        return _handler

    _project_git_status = _make_git_proxy_handler(
        ReqMethod.PROJECT_GIT_STATUS,
        "project.git.status",
        doc="Forward Git status to the AgentServer that owns the worktree.",
    )
    _project_git_probe = _make_git_proxy_handler(
        ReqMethod.PROJECT_GIT_PROBE,
        "project.git.probe",
        doc="Probe Git in AgentServer and refresh the Gateway watcher afterwards.",
        mark_dirty=True,
    )
    _project_git_init = _make_git_proxy_handler(
        ReqMethod.PROJECT_GIT_INIT,
        "project.git.init",
        doc="Initialize Git in the target AgentServer's project worktree.",
        mark_dirty=True,
    )
    _project_git_switch_branch = _make_git_proxy_handler(
        ReqMethod.PROJECT_GIT_SWITCH_BRANCH,
        "project.git.switch_branch",
        doc="Switch a branch in the target AgentServer's project worktree.",
        mark_dirty=True,
    )
    _project_git_create_branch = _make_git_proxy_handler(
        ReqMethod.PROJECT_GIT_CREATE_BRANCH,
        "project.git.create_branch",
        doc="Create a branch in the target AgentServer's project worktree.",
        mark_dirty=True,
    )
    _project_git_commit = _make_git_proxy_handler(
        ReqMethod.PROJECT_GIT_COMMIT,
        "project.git.commit",
        doc=(
            "提交当前工作区改动到当前分支(设计文档 §4.9 ``project.git.commit``)。"
            "``stage_all=true`` 先 ``git add -A`` 暂存全部;``paths`` 显式指定暂存路径"
            "(与 ``stage_all`` 互斥)。``amend=true`` 覆盖最近一次提交。成功/失败后调"
            "``mark_dirty`` 触发 /ws/git 立即重算(HEAD/is_dirty/stats 变化)。"
        ),
        mark_dirty=True,
        mark_dirty_on_failure=True,
    )
    _project_git_push = _make_git_proxy_handler(
        ReqMethod.PROJECT_GIT_PUSH,
        "project.git.push",
        doc=(
            "推送本地分支到远程(设计文档 §4.10 ``project.git.push``)。"
            "``remote`` 默认 ``origin``;``branch`` 不传时用当前分支(detached HEAD"
            "须显式传 ``branch``)。``force=true`` 使用 ``--force-with-lease``(更安全)。"
            "``delete=true`` 删除远程分支(与 ``set_upstream``/``force`` 互斥)。push"
            "涉及网络,超时独立配置(``GIT_PUSH_TIMEOUT_SEC``,默认 60s)。成功后调"
            "``mark_dirty`` 刷新 /ws/git(主要更新 upstream 字段)。"
        ),
        mark_dirty=True,
    )
    _project_git_diff_status = _make_git_proxy_handler(
        ReqMethod.PROJECT_GIT_DIFF_STATUS,
        "project.git.diff_status",
        doc=(
            "拉取当前分支 diff 和上一轮对话 diff 的快照(设计文档 §4.1.16)。"
            "``include_files=true`` 返回文件列表;``include_hunks=true`` 隐含"
            "``include_files=true`` 并返回 hunk。transient 状态下 ``current``"
            "为 ``null``,仍成功返回 ``repo.transient=true``。"
        ),
    )
    _project_git_turn_diff_list = _make_git_proxy_handler(
        ReqMethod.PROJECT_GIT_TURN_DIFF_LIST,
        "project.git.turn_diff_list",
        doc="查询历史 Diff 摘要列表。",
    )
    _project_git_turn_diff = _make_git_proxy_handler(
        ReqMethod.PROJECT_GIT_TURN_DIFF,
        "project.git.turn_diff",
        doc="查询指定轮次 Diff 详情。",
    )

    async def _path_get(ws, req_id, params, session_id, user_id=None):
        """读 browser.chrome_path / browser_type 并返回给前端（会解析环境变量）。"""
        from jiuwenswarm.gateway.routing.e2a_proxy import is_legacy_shared_directory_client, proxy_unary_request
        from jiuwenswarm.common.schema.message import ReqMethod

        resolved_client = _resolve(agent_client)
        if resolved_client is not None and not is_legacy_shared_directory_client(resolved_client):
            await proxy_unary_request(
                channel=channel, agent_client=resolved_client, ws=ws, req_id=req_id,
                params={}, session_id=session_id, user_id=user_id,
                req_method=ReqMethod.PATH_GET, label="path.get",
            )
            return
        try:
            config_base = get_config()
        except FileNotFoundError:
            await channel.send_response(
                ws,
                req_id,
                ok=True,
                payload={"chrome_path": "", "browser_type": "auto", "headless": True},
            )
            return

        if not isinstance(config_base, dict):
            config_base = {}

        config = _resolve_env_vars(config_base)
        browser_cfg = config.get("browser", {}) if isinstance(config, dict) else {}
        chrome_path = ""
        browser_type = "auto"
        headless = True
        if isinstance(browser_cfg, dict):
            value = browser_cfg.get("chrome_path", "")
            if isinstance(value, str):
                chrome_path = value
            raw_type = browser_cfg.get("browser_type", "auto")
            if isinstance(raw_type, str) and raw_type.strip():
                normalized = raw_type.strip().lower()
                if normalized in {"chrome", "google-chrome", "google_chrome"}:
                    browser_type = "chrome"
                elif normalized in {"msedge", "edge", "microsoft-edge", "microsoft_edge"}:
                    browser_type = "msedge"
                else:
                    browser_type = "auto"
            raw_headless = browser_cfg.get("headless", True)
            headless = bool(raw_headless) if isinstance(raw_headless, bool) else True

        await channel.send_response(
            ws, req_id, ok=True,
            payload={"chrome_path": chrome_path, "browser_type": browser_type, "headless": headless},
        )

    async def _path_set(ws, req_id, params, session_id, user_id=None):
        """更新 browser.chrome_path / browser_type / headless 并写回 config。"""
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return

        chrome_path = params.get("chrome_path")
        if not isinstance(chrome_path, str):
            await channel.send_response(ws, req_id, ok=False, error="chrome_path must be string", code="BAD_REQUEST")
            return
        chrome_path = chrome_path.strip()

        raw_browser_type = params.get("browser_type", "auto")
        if not isinstance(raw_browser_type, str):
            await channel.send_response(ws, req_id, ok=False, error="browser_type must be string", code="BAD_REQUEST")
            return
        normalized_type = raw_browser_type.strip().lower()
        if normalized_type in {"chrome", "google-chrome", "google_chrome"}:
            browser_type = "chrome"
        elif normalized_type in {"msedge", "edge", "microsoft-edge", "microsoft_edge"}:
            browser_type = "msedge"
        elif normalized_type in {"", "auto"}:
            browser_type = "auto"
        else:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="browser_type must be one of: auto, chrome, msedge",
                code="BAD_REQUEST",
            )
            return

        raw_headless = params.get("headless", True)
        headless = bool(raw_headless) if isinstance(raw_headless, bool) else True

        from jiuwenswarm.gateway.routing.e2a_proxy import (
            fetch_agent_unary,
            is_legacy_shared_directory_client,
            proxy_unary_request,
        )
        from jiuwenswarm.common.schema.message import ReqMethod

        resolved_client = _resolve(agent_client)
        if resolved_client is not None and not is_legacy_shared_directory_client(resolved_client):
            # Fetch current browser config so the restart callback can match the
            # old runtime identity (custom binary / headed mode).
            previous_chrome_path = ""
            previous_headless = True
            try:
                prev_ok, prev_payload = await fetch_agent_unary(
                    agent_client=resolved_client,
                    req_method=ReqMethod.PATH_GET,
                    params=None,
                    session_id=session_id,
                    user_id=user_id,
                    channel_id=channel.channel_id,
                    label="path.get (prev)",
                )
                if prev_ok and isinstance(prev_payload, dict):
                    prev_chrome = prev_payload.get("chrome_path")
                    if isinstance(prev_chrome, str):
                        previous_chrome_path = prev_chrome
                    prev_headless_val = prev_payload.get("headless", True)
                    previous_headless = (
                        bool(prev_headless_val) if isinstance(prev_headless_val, bool) else True
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning("[path.set] failed to fetch previous browser config: %s", e)

            async def _on_path_set_done(ok: bool, _payload: dict) -> None:
                if not ok:
                    return
                try:
                    await _clear_agent_config_cache(resolved_client)
                except Exception as e:  # noqa: BLE001
                    logger.warning("[path.set] AgentServer config cache clear failed: %s", e)
                try:
                    await _restart_agent_browser_runtime(
                        resolved_client,
                        previous_chrome_path=previous_chrome_path,
                        previous_headless=previous_headless,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("[path.set] browser runtime restart failed: %s", e)

            await proxy_unary_request(
                channel=channel, agent_client=resolved_client, ws=ws, req_id=req_id,
                params={"chrome_path": chrome_path, "browser_type": browser_type, "headless": headless},
                session_id=session_id, user_id=user_id,
                req_method=ReqMethod.PATH_SET, label="path.set",
                on_done=_on_path_set_done,
            )
            return

        current_browser_cfg = get_config().get("browser", {})
        if not isinstance(current_browser_cfg, dict):
            current_browser_cfg = {}
        previous_chrome_path = current_browser_cfg.get("chrome_path", "")
        if not isinstance(previous_chrome_path, str):
            previous_chrome_path = ""
        raw_previous_headless = current_browser_cfg.get("headless", True)
        previous_headless = (
            bool(raw_previous_headless)
            if isinstance(raw_previous_headless, bool)
            else True
        )

        try:
            update_browser_in_config({"chrome_path": chrome_path, "browser_type": browser_type, "headless": headless})
            resolved_agent_client = _resolve(agent_client)
            await _clear_agent_config_cache(resolved_agent_client)
        except Exception as e:  # noqa: BLE001
            logger.warning("[path.set] 写回 config.yaml 失败: %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")
            return

        try:
            await _restart_agent_browser_runtime(
                resolved_agent_client,
                previous_chrome_path=previous_chrome_path,
                previous_headless=previous_headless,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[path.set] browser config saved but active runtime reset failed: %s",
                e,
            )

        await channel.send_response(
            ws, req_id, ok=True,
            payload={"chrome_path": chrome_path, "browser_type": browser_type, "headless": headless},
        )

    async def _path_select_directory(ws, req_id, params, session_id, user_id=None):
        """在 AgentServer 注入目录内选择项目目录（决策 D3：返回 AgentServer 侧绝对路径）。

        经统一薄代理 E2A 转发目标 AgentServer（WorkspaceFileAdapter
        PATH_SELECT_DIRECTORY，注入 workspace 内解析/校验，越界返回 BAD_REQUEST）。
        """
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import (
            is_legacy_shared_directory_client,
            proxy_unary_request,
        )

        # A local single-user deployment historically opened a native picker in
        # the Gateway process.  Keep that UI contract there; AgentOS cannot use
        # a deployment-side picker because it would expose the wrong machine and
        # user directory.
        resolved_client = _resolve(agent_client)
        if is_legacy_shared_directory_client(resolved_client):
            if not isinstance(params, dict):
                params = {}
            initial_dir = params.get("initial_dir")
            if initial_dir is not None and not isinstance(initial_dir, str):
                await channel.send_response(
                    ws, req_id, ok=False, error="initial_dir must be string", code="BAD_REQUEST",
                )
                return
            from jiuwenswarm.channels.web.directory_picker import select_directory_native
            try:
                selected = await asyncio.to_thread(
                    select_directory_native,
                    initial_dir=initial_dir.strip() if isinstance(initial_dir, str) else None,
                )
            except Exception as exc:  # noqa: BLE001
                await channel.send_response(ws, req_id, ok=False, error=str(exc), code="UNSUPPORTED")
                return
            await channel.send_response(
                ws, req_id, ok=True,
                payload={"path": selected, "cancelled": not bool(selected)},
            )
            return

        await proxy_unary_request(
            channel=channel,
            agent_client=resolved_client,
            ws=ws,
            req_id=req_id,
            params=params,
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.PATH_SELECT_DIRECTORY,
            label="path.select_directory",
        )

    async def _path_select_files(ws, req_id, params, session_id, user_id=None):
        """枚举 AgentServer 注入目录下的可上传文件（决策 D3：返回 AgentServer 侧绝对路径）。

        经统一薄代理 E2A 转发目标 AgentServer（WorkspaceFileAdapter
        PATH_SELECT_FILES，注入 workspace 内枚举，返回与桌面端同形元数据）。
        """
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import (
            is_legacy_shared_directory_client,
            proxy_unary_request,
        )

        resolved_client = _resolve(agent_client)
        if is_legacy_shared_directory_client(resolved_client):
            if not isinstance(params, dict):
                params = {}
            initial_dir = params.get("initial_dir")
            allow_multiple = params.get("allow_multiple", True)
            if initial_dir is not None and not isinstance(initial_dir, str):
                await channel.send_response(
                    ws, req_id, ok=False, error="initial_dir must be string", code="BAD_REQUEST",
                )
                return
            if not isinstance(allow_multiple, bool):
                await channel.send_response(
                    ws, req_id, ok=False, error="allow_multiple must be boolean", code="BAD_REQUEST",
                )
                return
            from jiuwenswarm.channels.web.file_picker import select_and_describe_files
            try:
                selected = await asyncio.to_thread(
                    select_and_describe_files,
                    allow_multiple=allow_multiple,
                    initial_dir=initial_dir.strip() if isinstance(initial_dir, str) else None,
                )
            except Exception as exc:  # noqa: BLE001
                await channel.send_response(ws, req_id, ok=False, error=str(exc), code="UNSUPPORTED")
                return
            await channel.send_response(
                ws, req_id, ok=True,
                payload={"files": selected or [], "cancelled": selected is None},
            )
            return

        await proxy_unary_request(
            channel=channel,
            agent_client=resolved_client,
            ws=ws,
            req_id=req_id,
            params=params,
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.PATH_SELECT_FILES,
            label="path.select_files",
        )

    async def _path_select_file(ws, req_id, params, _session_id):
        if not isinstance(params, dict):
            params = {}

        initial_dir = params.get("initial_dir")
        initial_path = params.get("initial_path")
        title = params.get("title")
        if initial_dir is not None and not isinstance(initial_dir, str):
            await channel.send_response(
                ws, req_id, ok=False, error="initial_dir must be string", code="BAD_REQUEST",
            )
            return
        if initial_path is not None and not isinstance(initial_path, str):
            await channel.send_response(
                ws, req_id, ok=False, error="initial_path must be string", code="BAD_REQUEST",
            )
            return
        if title is not None and not isinstance(title, str):
            await channel.send_response(
                ws, req_id, ok=False, error="title must be string", code="BAD_REQUEST",
            )
            return

        resolved_initial_dir = initial_dir.strip() if isinstance(initial_dir, str) else None
        if isinstance(initial_path, str) and initial_path.strip():
            resolved_initial_dir = str(Path(initial_path.strip()).expanduser().parent)

        from jiuwenswarm.channels.web.directory_picker import select_file_native

        try:
            selected = await asyncio.to_thread(
                select_file_native,
                initial_dir=resolved_initial_dir,
                title=title.strip() if isinstance(title, str) else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[path.select_file] picker unavailable: %s", exc)
            await channel.send_response(
                ws, req_id, ok=False, error=str(exc), code="UNSUPPORTED",
            )
            return

        if not selected:
            await channel.send_response(ws, req_id, ok=True, payload={"path": None, "cancelled": True})
            return
        await channel.send_response(ws, req_id, ok=True, payload={"path": selected, "cancelled": False})

    async def _memory_compute(ws, req_id, params, session_id):
        if _HAS_PSUTIL:
            process = _psutil.Process()  # type: ignore[union-attribute]
            rss_mb = process.memory_info().rss / (1024 * 1024)

            mem = _psutil.virtual_memory()  # type: ignore[union-attribute]
            total_mb = mem.total / (1024 * 1024)
            available_mb = mem.available / (1024 * 1024)
        else:
            rss_mb, total_mb, available_mb = _read_proc_meminfo()

        await channel.send_response(ws, req_id, ok=True,
                                    payload={"rss_mb": rss_mb, "total_mb": total_mb,
                                             "available_mb": available_mb})

    async def _commands_list(ws, req_id, params, session_id):
        """下发快捷面板命令清单（静态元数据，本地直接返回，不转发 AgentServer）。"""
        try:
            from jiuwenswarm.gateway.message_handler.command_parser.slash_command import (
                list_builtin_commands,
            )
            payload = list_builtin_commands(params if isinstance(params, dict) else {})
            await channel.send_response(ws, req_id, ok=True, payload=payload)
        except Exception as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _chat_send(ws, req_id, params, session_id):
        await channel.send_response(
            ws,
            req_id,
            ok=True,
            payload={"accepted": True, "session_id": session_id},
        )

    async def _media_persist(ws, req_id, params, session_id, user_id=None):
        """浏览器上传媒体附件落盘（E2A 转发 AgentServer 注入目录 uploads）。

        经统一薄代理 E2A 转发目标 AgentServer（WorkspaceFileAdapter
        MEDIA_PERSIST，base64 小文件由注入目录落盘；大文件在 Gateway 侧解码后
        走受认证 HTTP bridge 上传，避免超内部 WS 帧限制）。
        """
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        real_client = _resolve(agent_client)
        if isinstance(params, dict):
            params = await _pre_persist_large_media(
                params,
                session_id=session_id,
                agent_client=real_client,
                user_id=user_id,
            )

        await proxy_unary_request(
            channel=channel,
            agent_client=real_client,
            ws=ws,
            req_id=req_id,
            params=params,
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.MEDIA_PERSIST,
            label="media.persist",
        )

    async def _document_persist(ws, req_id, params, session_id, user_id=None):
        """文档附件路径黑名单校验（E2A 转发，路径判定由 AgentServer 注入目录执行）。"""
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve(agent_client),
            ws=ws,
            req_id=req_id,
            params=params,
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.DOCUMENT_PERSIST,
            label="document.persist",
        )

    async def _document_formats(ws, req_id, params, session_id, user_id=None):
        """返回文档上传黑名单格式（E2A 转发 AgentServer）。"""
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve(agent_client),
            ws=ws,
            req_id=req_id,
            params=params,
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.DOCUMENT_FORMATS,
            label="document.formats",
        )

    async def _chat_resume(ws, req_id, params, session_id):
        await channel.send_response(
            ws,
            req_id,
            ok=True,
            payload={"accepted": True, "session_id": session_id},
        )

    async def _chat_interrupt(ws, req_id, params, session_id):
        intent = params.get("intent") if isinstance(params, dict) else None
        payload = {"accepted": True, "session_id": session_id}
        if isinstance(intent, str) and intent:
            payload["intent"] = intent
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _chat_user_answer(ws, req_id, params, session_id):
        payload = {"accepted": True, "session_id": session_id}
        request_id = params.get("request_id") if isinstance(params, dict) else None
        if isinstance(request_id, str) and request_id:
            payload["request_id"] = request_id
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _history_get(ws, req_id, params, session_id):
        payload = {"accepted": True, "session_id": session_id}
        if isinstance(params, dict):
            if "session_id" in params:
                payload["session_id"] = params.get("session_id")
            if "page_idx" in params:
                payload["page_idx"] = params.get("page_idx")
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _locale_get_conf(ws, req_id, params, session_id, user_id=None):
        """Return the selected AgentServer user's preferred language."""
        from jiuwenswarm.gateway.routing.e2a_proxy import (
            is_legacy_shared_directory_client,
            proxy_unary_request,
        )
        from jiuwenswarm.common.schema.message import ReqMethod

        resolved_client = _resolve(agent_client)
        if resolved_client is None or is_legacy_shared_directory_client(resolved_client):
            try:
                cfg = get_config()
                lang = str(cfg.get("preferred_language") or "zh").strip().lower()
                if lang not in ("zh", "en"):
                    lang = "zh"
                await channel.send_response(
                    ws, req_id, ok=True, payload={"preferred_language": lang}
                )
            except Exception as e:
                logger.exception("[locale.get_conf] %s", e)
                await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")
            return

        await proxy_unary_request(
            channel=channel, agent_client=_resolve(agent_client), ws=ws,
            req_id=req_id, params={}, session_id=session_id, user_id=user_id,
            req_method=ReqMethod.LOCALE_GET_CONF, label="locale.get_conf",
        )

    async def _locale_set_conf(ws, req_id, params, session_id, user_id=None):
        """Update preferred language in the selected AgentServer directory."""
        from jiuwenswarm.gateway.routing.e2a_proxy import (
            is_legacy_shared_directory_client,
            proxy_unary_request,
        )
        from jiuwenswarm.common.schema.message import ReqMethod

        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        lang_raw = params.get("preferred_language")
        if not isinstance(lang_raw, str):
            await channel.send_response(
                ws, req_id, ok=False, error="preferred_language must be string", code="BAD_REQUEST"
            )
            return
        lang = lang_raw.strip().lower()
        if lang not in ("zh", "en"):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="preferred_language must be zh or en",
                code="BAD_REQUEST"
            )
            return
        resolved_client = _resolve(agent_client)
        if resolved_client is None or is_legacy_shared_directory_client(resolved_client):
            try:
                update_preferred_language_in_config(lang)
                await channel.send_response(ws, req_id, ok=True, payload={"preferred_language": lang})
            except Exception as e:
                logger.warning("[locale.set_conf] 写回 config.yaml 失败: %s", e)
                await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")
            return
        await proxy_unary_request(
            channel=channel, agent_client=_resolve(agent_client), ws=ws,
            req_id=req_id, params={"preferred_language": lang}, session_id=session_id,
            user_id=user_id, req_method=ReqMethod.LOCALE_SET_CONF,
            label="locale.set_conf",
        )

    async def _health_check_get_conf(ws, req_id, params, session_id):
        """返回当前探活配置（every / target / active_hours）。"""
        hb = _resolve(heartbeat_service)
        if hb is None:
            await channel.send_response(ws, req_id, ok=False, error="health_check service not available",
                                        code="SERVICE_UNAVAILABLE")
            return
        try:
            payload = dict(hb.get_health_check_conf())
            await channel.send_response(ws, req_id, ok=True, payload=payload)
        except Exception as e:
            logger.exception("[health_check.get_conf] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _health_check_set_conf(ws, req_id, params, session_id):
        """更新探活配置并重启探活服务；params 可含 every、target、active_hours。"""
        hb = _resolve(heartbeat_service)
        if hb is None:
            await channel.send_response(ws, req_id, ok=False, error="heartbeat service not available",
                                        code="SERVICE_UNAVAILABLE")
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        try:
            every = params.get("every")
            target = params.get("target")
            active_hours = params.get("active_hours")
            if every is not None:
                every = float(every)
            if target is not None:
                target = str(target)
            if active_hours is not None:
                if not isinstance(active_hours, dict):
                    active_hours = None
                elif active_hours and ("start" not in active_hours or "end" not in active_hours):
                    # 必须同时包含 start/end，否则视为清除时间段（始终生效）
                    active_hours = None

            # 先检查：如果目标渠道是飞书，检测是否有可用的推送目标
            if target == "feishu":
                try:
                    raw = get_config_raw() or {}
                    ch_cfg = (raw.get("channels") or {}).get("feishu") or {}
                    # V2 多应用：心跳 relay 会 fan-out 到同 channel_id 的全部 app，每个 app 各走
                    # 自己的 last_chat_id/chat_id 投递；故要求「每个 app 都有目标」才算可用，
                    # 否则缺失目标的 app 每次 tick 都会静默投递失败。
                    apps = ch_cfg.get("apps") or []
                    if isinstance(apps, list) and apps:
                        has_target = all(
                            isinstance(app, dict)
                            and (
                                bool(str(app.get("last_chat_id") or "").strip())
                                or bool(str(app.get("chat_id") or "").strip())
                            )
                            for app in apps
                        )
                    else:
                        # 旧平铺格式（单应用）：兜底看顶层 last_chat_id/chat_id。
                        has_target = bool(
                            str(ch_cfg.get("last_chat_id") or "").strip()
                            or str(ch_cfg.get("chat_id") or "").strip()
                        )
                    if not has_target:
                        await channel.send_response(
                            ws, req_id, ok=False,
                            error="feishuNoTarget",
                            code="feishuNoTarget",
                        )
                        return
                except Exception as e:
                    logger.debug("[health_check.set_conf] 飞书目标检测异常: %s", e)
                    await channel.send_response(
                        ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR",
                    )
                    return

            # 检查通过后再保存配置
            await hb.set_health_check_conf(every=every, target=target, active_hours=active_hours)
            payload = dict(hb.get_health_check_conf())
            should_clear_agent_config_cache = False
            try:
                update_health_check_in_config(payload)
                should_clear_agent_config_cache = True
            except Exception as e:  # noqa: BLE001
                logger.warning("[health_check.set_conf] 写回 config.yaml 失败: %s", e)
            try:
                await channel.send_response(ws, req_id, ok=True, payload=payload)
            finally:
                if should_clear_agent_config_cache:
                    _schedule_clear_agent_config_cache("health_check.set_conf")
        except ValueError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="BAD_REQUEST")
        except Exception as e:
            logger.exception("[health_check.set_conf] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    def _mask_sensitive(params: dict | list, sensitive_keys: frozenset[str]) -> dict | list:
        """递归脱敏，替换敏感字段值为 ``****``。"""
        if isinstance(params, dict):
            return {
                k: (_mask_sensitive(v, sensitive_keys) if isinstance(v, (dict, list))
                    else "****" if k in sensitive_keys else v)
                for k, v in params.items()
            }
        if isinstance(params, list):
            return [_mask_sensitive(item, sensitive_keys) if isinstance(item, (dict, list)) else item
                    for item in params]
        return params

    _feishu_sensitive_keys: frozenset[str] = frozenset({"app_secret", "encrypt_key", "verification_token"})
    _xiaoyi_sensitive_keys: frozenset[str] = frozenset({"sk", "api_key"})

    async def _channel_feishu_get_conf(ws, req_id, params, session_id):
        """返回 FeishuChannel 的当前配置（由 ChannelManager 管理）。"""
        cm = _resolve(channel_manager)
        if cm is None:
            logger.warning("[channel.feishu.get_conf] channel_manager not available, req_id=%s", req_id)
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="channel manager not available",
                code="SERVICE_UNAVAILABLE",
            )
            return
        try:
            raw = cm.get_conf("feishu")
            conf = _normalize_feishu_conf(raw)
            apps = conf.get("apps", [])
            app_names = [a.get("name", "?") for a in apps]
            logger.debug(
                "[channel.feishu.get_conf] ok, req_id=%s, apps=%d, names=%s",
                req_id, len(apps), app_names,
            )
            await channel.send_response(ws, req_id, ok=True, payload={"config": conf})
        except Exception as e:  # noqa: BLE001
            logger.exception("[channel.feishu.get_conf] 异常, req_id=%s: %s", req_id, e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _channel_feishu_set_conf(ws, req_id, params, session_id):
        """更新 FeishuChannel 的配置，并按新配置重新实例化通道。

        ``params`` 必须含 ``apps`` 键，保存到 channels.feishu.apps。
        """
        cm = _resolve(channel_manager)
        if cm is None:
            logger.warning("[channel.feishu.set_conf] channel_manager not available, req_id=%s", req_id)
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="channel manager not available",
                code="SERVICE_UNAVAILABLE",
            )
            return
        if not isinstance(params, dict):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="params must be object",
                code="BAD_REQUEST",
            )
            return
        try:
            # 多应用模式：params 必须含 apps 键
            apps = params["apps"]
            app_names = [a.get("name", "?") for a in apps]
            logger.debug(
                "[channel.feishu.set_conf] req_id=%s, apps=%d, names=%s",
                req_id, len(apps), app_names,
            )
            # 先归一化（用 _FEISHU_APP_DEFAULTS 补充前端未发送的字段），再持久化
            normalized_apps = _normalize_feishu_conf({"apps": apps})["apps"]
            # 从 cm 读取已有 apps，按 app_id 合并保留未发送的敏感字段
            existing_feishu = cm.get_conf("feishu")
            existing_apps = existing_feishu.get("apps", []) if isinstance(existing_feishu, dict) else []
            merged_apps = _merge_apps_by_id(normalized_apps, existing_apps)
            await cm.set_conf("feishu", {"apps": merged_apps})
            should_clear_agent_config_cache = False
            try:
                replace_channel_subsection_with_cleanup("feishu", "apps", merged_apps, {"apps", "send_file_allowed"})
                should_clear_agent_config_cache = True
            except Exception as e:  # noqa: BLE001
                logger.warning("[channel.feishu.set_conf] 写回 config.yaml apps 失败: %s", e)
            try:
                await channel.send_response(ws, req_id, ok=True, payload={"config": {"apps": merged_apps}})
            finally:
                if should_clear_agent_config_cache:
                    _schedule_clear_agent_config_cache("channel.feishu.set_conf")
        except Exception as e:  # noqa: BLE001
            logger.exception("[channel.feishu.set_conf] 异常, req_id=%s: %s", req_id, e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _channel_xiaoyi_get_conf(ws, req_id, params, session_id):
        """返回 XiaoyiChannel 的当前配置（由 ChannelManager 管理）。"""
        cm = _resolve(channel_manager)
        if cm is None:
            logger.warning("[channel.xiaoyi.get_conf] channel_manager not available, req_id=%s", req_id)
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="channel manager not available",
                code="SERVICE_UNAVAILABLE",
            )
            return
        try:
            raw = cm.get_conf("xiaoyi")
            conf = _normalize_xiaoyi_conf(raw)
            apps = conf.get("apps", [])
            app_names = [a.get("name", "?") for a in apps]
            logger.debug(
                "[channel.xiaoyi.get_conf] ok, req_id=%s, apps=%d, names=%s",
                req_id, len(apps), app_names,
            )
            await channel.send_response(ws, req_id, ok=True, payload={"config": conf})
        except Exception as e:  # noqa: BLE001
            logger.exception("[channel.xiaoyi.get_conf] 异常, req_id=%s: %s", req_id, e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _channel_xiaoyi_set_conf(ws, req_id, params, session_id):
        """更新 XiaoyiChannel 的配置，并按新配置重新实例化通道。

        ``params`` 必须含 ``apps`` 键，保存到 channels.xiaoyi.apps。
        """
        cm = _resolve(channel_manager)
        if cm is None:
            logger.warning("[channel.xiaoyi.set_conf] channel_manager not available, req_id=%s", req_id)
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="channel manager not available",
                code="SERVICE_UNAVAILABLE",
            )
            return
        if not isinstance(params, dict):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="params must be object",
                code="BAD_REQUEST",
            )
            return
        try:
            # 多应用模式：params 必须含 apps 键
            apps = params["apps"]
            app_names = [a.get("name", "?") for a in apps]
            logger.debug(
                "[channel.xiaoyi.set_conf] req_id=%s, apps=%d, names=%s",
                req_id, len(apps), app_names,
            )
            # 先归一化（用 _XIAOYI_APP_DEFAULTS 补充前端未发送的字段），再持久化
            normalized_apps = _normalize_xiaoyi_conf({"apps": apps})["apps"]
            # 从 cm 读取已有 apps，按 app_id 合并保留未发送的敏感字段
            existing_xiaoyi = cm.get_conf("xiaoyi")
            existing_apps = existing_xiaoyi.get("apps", []) if isinstance(existing_xiaoyi, dict) else []
            merged_apps = _merge_apps_by_id(normalized_apps, existing_apps)
            await cm.set_conf("xiaoyi", {"apps": merged_apps})
            try:
                replace_channel_subsection_with_cleanup("xiaoyi", "apps", merged_apps, {"apps", "send_file_allowed"})
                await _clear_agent_config_cache(_resolve(agent_client))
            except Exception as e:  # noqa: BLE001
                logger.warning("[channel.xiaoyi.set_conf] 写回 config.yaml apps 失败: %s", e)
            await channel.send_response(ws, req_id, ok=True, payload={"config": {"apps": merged_apps}})
        except Exception as e:  # noqa: BLE001
            logger.exception("[channel.xiaoyi.set_conf] 异常, req_id=%s: %s", req_id, e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _channel_telegram_get_conf(ws, req_id, params, session_id):
        """返回 TelegramChannel 的当前配置（由 ChannelManager 管理）。"""
        cm = _resolve(channel_manager)
        if cm is None:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="channel manager not available",
                code="SERVICE_UNAVAILABLE",
            )
            return
        try:
            conf = cm.get_conf("telegram")
            await channel.send_response(ws, req_id, ok=True, payload={"config": conf})
        except Exception as e:  # noqa: BLE001
            logger.exception("[channel.telegram.get_conf] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _channel_telegram_set_conf(ws, req_id, params, session_id):
        """更新 TelegramChannel 的配置，并按新配置重新实例化通道。"""
        cm = _resolve(channel_manager)
        if cm is None:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="channel manager not available",
                code="SERVICE_UNAVAILABLE",
            )
            return
        if not isinstance(params, dict):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="params must be object",
                code="BAD_REQUEST",
            )
            return
        try:
            await cm.set_conf("telegram", params)
            conf = cm.get_conf("telegram")
            try:
                update_channel_in_config("telegram", conf)
                await _clear_agent_config_cache(_resolve(agent_client))
            except Exception as e:  # noqa: BLE001
                logger.warning("[channel.telegram.set_conf] 写回 config.yaml 失败: %s", e)
            await channel.send_response(ws, req_id, ok=True, payload={"config": conf})
        except Exception as e:  # noqa: BLE001
            logger.exception("[channel.telegram.set_conf] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _channel_dingtalk_get_conf(ws, req_id, params, session_id):
        cm = _resolve(channel_manager)
        if cm is None:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="channel manager not available",
                code="SERVICE_UNAVAILABLE",
            )
            return
        try:
            conf = cm.get_conf("dingtalk")
            await channel.send_response(ws, req_id, ok=True, payload={"config": conf})
        except Exception as e:  # noqa: BLE001
            logger.exception("[channel.dingtalk.get_conf] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _channel_dingtalk_set_conf(ws, req_id, params, session_id):
        cm = _resolve(channel_manager)
        if cm is None:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="channel manager not available",
                code="SERVICE_UNAVAILABLE",
            )
            return
        if not isinstance(params, dict):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="params must be object",
                code="BAD_REQUEST",
            )
            return
        try:
            await cm.set_conf("dingtalk", params)
            conf = cm.get_conf("dingtalk")
            should_clear_agent_config_cache = False
            try:
                update_channel_in_config("dingtalk", conf)
                should_clear_agent_config_cache = True
            except Exception as e:  # noqa: BLE001
                logger.warning("[channel.dingtalk.set_conf] 写回 config.yaml 失败: %s", e)
            try:
                await channel.send_response(ws, req_id, ok=True, payload={"config": conf})
            finally:
                if should_clear_agent_config_cache:
                    _schedule_clear_agent_config_cache("channel.dingtalk.set_conf")
        except Exception as e:  # noqa: BLE001
            logger.exception("[channel.dingtalk.set_conf] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _channel_whatsapp_get_conf(ws, req_id, params, session_id):
        cm = _resolve(channel_manager)
        if cm is None:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="channel manager not available",
                code="SERVICE_UNAVAILABLE",
            )
            return
        try:
            conf = cm.get_conf("whatsapp")
            await channel.send_response(ws, req_id, ok=True, payload={"config": conf})
        except Exception as e:  # noqa: BLE001
            logger.exception("[channel.whatsapp.get_conf] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _channel_whatsapp_set_conf(ws, req_id, params, session_id):
        cm = _resolve(channel_manager)
        if cm is None:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="channel manager not available",
                code="SERVICE_UNAVAILABLE",
            )
            return
        if not isinstance(params, dict):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="params must be object",
                code="BAD_REQUEST",
            )
            return
        try:
            await cm.set_conf("whatsapp", params)
            conf = cm.get_conf("whatsapp")
            try:
                update_channel_in_config("whatsapp", conf)
                await _clear_agent_config_cache(_resolve(agent_client))
            except Exception as e:  # noqa: BLE001
                logger.warning("[channel.whatsapp.set_conf] 写回 config.yaml 失败: %s", e)
            await channel.send_response(ws, req_id, ok=True, payload={"config": conf})
        except Exception as e:  # noqa: BLE001
            logger.exception("[channel.whatsapp.set_conf] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _channel_discord_get_conf(ws, req_id, params, session_id):
        cm = _resolve(channel_manager)
        if cm is None:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="channel manager not available",
                code="SERVICE_UNAVAILABLE",
            )
            return
        try:
            conf = cm.get_conf("discord")
            await channel.send_response(ws, req_id, ok=True, payload={"config": conf})
        except Exception as e:  # noqa: BLE001
            logger.exception("[channel.discord.get_conf] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _channel_discord_set_conf(ws, req_id, params, session_id):
        cm = _resolve(channel_manager)
        if cm is None:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="channel manager not available",
                code="SERVICE_UNAVAILABLE",
            )
            return
        if not isinstance(params, dict):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="params must be object",
                code="BAD_REQUEST",
            )
            return
        try:
            await cm.set_conf("discord", params)
            conf = cm.get_conf("discord")
            try:
                update_channel_in_config("discord", conf)
                await _clear_agent_config_cache(_resolve(agent_client))
            except Exception as e:  # noqa: BLE001
                logger.warning("[channel.discord.set_conf] 写回 config.yaml 失败: %s", e)
            await channel.send_response(ws, req_id, ok=True, payload={"config": conf})
        except Exception as e:  # noqa: BLE001
            logger.exception("[channel.discord.set_conf] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _channel_slack_get_conf(ws, req_id, params, session_id):
        cm = _resolve(channel_manager)
        if cm is None:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="channel manager not available",
                code="SERVICE_UNAVAILABLE",
            )
            return
        try:
            conf = cm.get_conf("slack")
            await channel.send_response(ws, req_id, ok=True, payload={"config": conf})
        except Exception as e:  # noqa: BLE001
            logger.exception("[channel.slack.get_conf] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _channel_slack_set_conf(ws, req_id, params, session_id):
        cm = _resolve(channel_manager)
        if cm is None:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="channel manager not available",
                code="SERVICE_UNAVAILABLE",
            )
            return
        if not isinstance(params, dict):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="params must be object",
                code="BAD_REQUEST",
            )
            return
        try:
            await cm.set_conf("slack", params)
            conf = cm.get_conf("slack")
            try:
                update_channel_in_config("slack", conf)
                await _clear_agent_config_cache(_resolve(agent_client))
            except Exception as e:  # noqa: BLE001
                logger.warning("[channel.slack.set_conf] failed to persist config.yaml: %s", e)
            await channel.send_response(ws, req_id, ok=True, payload={"config": conf})
        except Exception as e:  # noqa: BLE001
            logger.exception("[channel.slack.set_conf] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _channel_wecom_get_conf(ws, req_id, params, session_id):
        cm = _resolve(channel_manager)
        if cm is None:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="channel manager not available",
                code="SERVICE_UNAVAILABLE",
            )
            return
        try:
            conf = cm.get_conf("wecom")
            await channel.send_response(ws, req_id, ok=True, payload={"config": conf})
        except Exception as e:  # noqa: BLE001
            logger.exception("[channel.wecom.get_conf] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _channel_wecom_set_conf(ws, req_id, params, session_id):
        cm = _resolve(channel_manager)
        if cm is None:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="channel manager not available",
                code="SERVICE_UNAVAILABLE",
            )
            return
        if not isinstance(params, dict):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="params must be object",
                code="BAD_REQUEST",
            )
            return
        try:
            await cm.set_conf("wecom", params)
            conf = cm.get_conf("wecom")
            try:
                update_channel_in_config("wecom", conf)
                await _clear_agent_config_cache(_resolve(agent_client))
            except Exception as e:  # noqa: BLE001
                logger.warning("[channel.wecom.set_conf] 写回 config.yaml 失败: %s", e)
            await channel.send_response(ws, req_id, ok=True, payload={"config": conf})
        except Exception as e:  # noqa: BLE001
            logger.exception("[channel.wecom.set_conf] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _channel_wechat_get_conf(ws, req_id, params, session_id):
        cm = _resolve(channel_manager)
        if cm is None:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="channel manager not available",
                code="SERVICE_UNAVAILABLE",
            )
            return
        try:
            conf = cm.get_conf("wechat")
            await channel.send_response(ws, req_id, ok=True, payload={"config": conf})
        except Exception as e:  # noqa: BLE001
            logger.exception("[channel.wechat.get_conf] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _channel_wechat_set_conf(ws, req_id, params, session_id):
        cm = _resolve(channel_manager)
        if cm is None:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="channel manager not available",
                code="SERVICE_UNAVAILABLE",
            )
            return
        if not isinstance(params, dict):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="params must be object",
                code="BAD_REQUEST",
            )
            return
        # 数值参数写盘前校验：拒绝负数 / 0 / 极大值 / 浮点越界 / 非数字，早于 set_conf 中断。
        numeric_error = _validate_wechat_numeric_params(params)
        if numeric_error is not None:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=numeric_error,
                code="BAD_REQUEST",
            )
            return
        try:
            await cm.set_conf("wechat", params)
            conf = cm.get_conf("wechat")
            try:
                update_channel_in_config("wechat", conf)
                await _clear_agent_config_cache(_resolve(agent_client))
            except Exception as e:  # noqa: BLE001
                logger.warning("[channel.wechat.set_conf] 写回 config.yaml 失败: %s", e)
            await channel.send_response(ws, req_id, ok=True, payload={"config": conf})
        except Exception as e:  # noqa: BLE001
            logger.exception("[channel.wechat.set_conf] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _channel_wechat_get_login_ui(ws, req_id, params, session_id):
        from jiuwenswarm.gateway.channel_manager.im_platforms.wechat.wechat_connect import (
            snapshot_wechat_login_ui_state,
        )

        try:
            ui = await snapshot_wechat_login_ui_state()
            if "updated_at" in ui and isinstance(ui["updated_at"], (int, float)):
                ui["updated_at"] = int(ui["updated_at"])
            await channel.send_response(ws, req_id, ok=True, payload=ui)
        except Exception as e:  # noqa: BLE001
            logger.exception("[channel.wechat.get_login_ui] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _channel_wechat_unbind(ws, req_id, params, session_id):
        cm = _resolve(channel_manager)
        if cm is None:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="channel manager not available",
                code="SERVICE_UNAVAILABLE",
            )
            return
        try:
            from jiuwenswarm.gateway.channel_manager.im_platforms.wechat.wechat_connect import \
                clear_wechat_bound_session, reset_wechat_login_ui_state

            conf = cm.get_conf("wechat")
            new_conf = clear_wechat_bound_session(conf)
            await reset_wechat_login_ui_state()
            # 若 YAML 里 bot_token 本就为空，仅删凭据文件时 dict 与上次相同，
            # _should_restart_channel 不会重启，扫码 UI 会一直停在 idle
            cm.mark_channel_restart_pending("wechat")
            await cm.set_conf("wechat", new_conf)
            final = cm.get_conf("wechat")
            try:
                update_channel_in_config("wechat", final)
                await _clear_agent_config_cache(_resolve(agent_client))
            except Exception as e:  # noqa: BLE001
                logger.warning("[channel.wechat.unbind] 写回 config.yaml 失败: %s", e)
            await channel.send_response(ws, req_id, ok=True, payload={"config": final})
        except Exception as e:  # noqa: BLE001
            logger.exception("[channel.wechat.unbind] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    # ----- cron jobs -----

    def _get_cron():
        return _resolve(cron_controller)

    async def _get_owned_cron_job(cc, job_id: str, user_id: str | None):
        """Return a job only when it belongs to the authenticated Web user.

        Empty ``user_id`` preserves the legacy unauthenticated single-user
        behavior.  Authenticated connections must never access a different
        user's job, including historical jobs whose owner is empty.

        ``cc.get_job`` returns a plain dict (``CronJob.to_dict()``), so the
        owner field must be read with ``dict.get``; ``getattr`` on a dict
        always yields the empty default and would reject every authenticated
        access with "job not found".  Keep the object fallback for tests and
        callers that return ``CronJob``-like objects.
        """
        job = await cc.get_job(job_id)
        owner = str(user_id or "").strip()
        if job is None:
            return None
        owner_field = (
            job.get("user_id", "") if isinstance(job, dict) else getattr(job, "user_id", "")
        )
        if owner and str(owner_field or "").strip() != owner:
            return None
        return job

    async def _cron_job_list(ws, req_id, params, session_id, user_id=None):
        cc = _get_cron()
        if cc is None:
            await channel.send_response(ws, req_id, ok=False, error="cron not available", code="INTERNAL_ERROR")
            return
        jobs = await cc.list_jobs()
        # 按 web 连接 user_id 过滤:只返回该用户创建的定时任务(隔离)。
        # user_id 为空(连接未带)时维持原行为(返回全部);agent 内部创建的 cron
        # (user_id 空串)在带 user_id 的连接下被过滤掉(对人类用户不可见)。
        uid_str = str(user_id or "").strip()
        if uid_str:
            jobs = [j for j in jobs if str(j.get("user_id") or "").strip() == uid_str]
        # 可选按 project_id 过滤(支持 default/default_code 虚拟项目)
        if isinstance(params, dict):
            raw_pid = params.get("project_id")
            if isinstance(raw_pid, str) and raw_pid.strip():
                filter_pid = raw_pid.strip()
                if is_default_project_id(filter_pid):
                    # 默认项目:按 filter_pid 精确匹配,用 work_mode 消歧空 project_id。
                    # 避免 default 过滤返回 default_code 的 job（反之亦然）。
                    # 兼容未迁移的老 job(work_mode 为空或非法):按 channel_id 推断
                    # 兜底 work_mode,避免迁移失败场景下 default_code 过滤漏掉老 job。
                    target_wm = DEFAULT_TUI_WORK_MODE if filter_pid == DEFAULT_PROJECT_ID_CODE \
                        else DEFAULT_WEB_WORK_MODE
                    filtered = []
                    for j in jobs:
                        j_pid = j.get("project_id")
                        if j_pid == filter_pid:
                            filtered.append(j)
                            continue
                        if not j_pid:
                            j_wm = j.get("work_mode")
                            if isinstance(j_wm, str) and j_wm.strip() in SUPPORTED_WORK_MODES:
                                if j_wm == target_wm:
                                    filtered.append(j)
                            else:
                                # work_mode 缺失/非法(未迁移的老 job):
                                # 按 target_wm 匹配 default(default→work)或不匹配
                                # default_code(default_code→code,老 job 兜底 work 不匹配)
                                if target_wm == DEFAULT_WEB_WORK_MODE:
                                    filtered.append(j)
                    jobs = filtered
                else:
                    filtered = []
                    for j in jobs:
                        if j.get("project_id") == filter_pid:
                            filtered.append(j)
                    jobs = filtered
        await channel.send_response(ws, req_id, ok=True, payload={"jobs": jobs})

    async def _cron_job_meta(ws, req_id, params, session_id):
        cc = _get_cron()
        if cc is None:
            await channel.send_response(ws, req_id, ok=False, error="cron not available", code="INTERNAL_ERROR")
            return
        await channel.send_response(ws, req_id, ok=True, payload=cc.job_metadata())

    async def _cron_job_get(ws, req_id, params, session_id, user_id=None):
        cc = _get_cron()
        if cc is None:
            await channel.send_response(ws, req_id, ok=False, error="cron not available", code="INTERNAL_ERROR")
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        job = await _get_owned_cron_job(cc, job_id, user_id)
        if job is None:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
            return
        await channel.send_response(ws, req_id, ok=True, payload={"job": job})

    async def _cron_job_create(ws, req_id, params, session_id, user_id=None):
        cc = _get_cron()
        if cc is None:
            await channel.send_response(ws, req_id, ok=False, error="cron not available", code="INTERNAL_ERROR")
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        try:
            from jiuwenswarm.gateway.routing.e2a_proxy import (
                is_agentos_routing_client,
                is_legacy_shared_directory_client,
            )

            if session_id:
                params["session_id"] = session_id
            # 注入创建者 user_id（web 连接携带），执行时透传给 faas 的 X-Session-Context，
            # 否则 CreateSandbox 拉不起 → 60s 超时（见 plan-cron-user-id）。
            if user_id:
                params["user_id"] = str(user_id).strip()
            is_agentos = is_agentos_routing_client(_resolve(agent_client))
            # 仅共享目录单用户可由 Gateway 从 session metadata 补 project_dir。
            # AgentOS 下 metadata 在目标 AgentServer 用户目录，Gateway 读部署目录会
            # 误把任务归到默认项目；缺省时让目标 AgentServer / 显式 project_id 决定。
            # 注意：显式传空串 "" 等价于归默认项目，不可覆盖——用 key presence 区分。
            if (
                is_legacy_shared_directory_client(_resolve(agent_client))
                and "project_dir" not in params
                and session_id
            ):
                try:
                    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata
                    meta = get_session_metadata(session_id, cache_bust=True)
                    if isinstance(meta, dict):
                        pd = meta.get("project_dir")
                        if isinstance(pd, str) and pd.strip():
                            params["project_dir"] = pd.strip()
                except Exception:  # noqa: BLE001
                    pass
            if is_agentos:
                from jiuwenswarm.gateway.routing.e2a_proxy import (
                    resolve_agent_cron_project_binding,
                )

                bound, binding = await resolve_agent_cron_project_binding(
                    agent_client=_resolve(agent_client), params=params,
                    user_id=user_id, channel_id="web", session_id=session_id,
                )
                if not bound:
                    await channel.send_response(
                        ws, req_id, ok=False,
                        error=str(binding.get("error") or "cron project binding failed"),
                        code=str(binding.get("code") or "SERVICE_UNAVAILABLE"),
                    )
                    return
                resolved_project_id = binding.get("project_id")
                resolved_work_mode = binding.get("work_mode")
                if (
                    not isinstance(resolved_project_id, str)
                    or not isinstance(resolved_work_mode, str)
                    or not resolved_work_mode.strip()
                ):
                    await channel.send_response(
                        ws, req_id, ok=False,
                        error="invalid cron project binding",
                        code="BAD_REQUEST",
                    )
                    return
                params.update({
                    "project_id": resolved_project_id,
                    "work_mode": resolved_work_mode,
                })
                params.pop("project_dir", None)
                params["_agentos_project_binding_verified"] = True
            job = await cc.create_job(params)
            await channel.send_response(ws, req_id, ok=True, payload={"job": job})
        except Exception as e:  # noqa: BLE001
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="BAD_REQUEST")

    async def _cron_job_update(ws, req_id, params, session_id, user_id=None):
        cc = _get_cron()
        if cc is None:
            await channel.send_response(ws, req_id, ok=False, error="cron not available", code="INTERNAL_ERROR")
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        patch = params.get("patch") or {}
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        if not isinstance(patch, dict):
            await channel.send_response(ws, req_id, ok=False, error="patch must be object", code="BAD_REQUEST")
            return
        try:
            from jiuwenswarm.gateway.routing.e2a_proxy import is_agentos_routing_client

            if await _get_owned_cron_job(cc, job_id, user_id) is None:
                await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
                return
            patch = dict(patch)
            # 仅当 patch 涉及 project 字段时才解析项目绑定（避免非项目字段的
            # update 因用户侧项目解析失败而被整体拒绝；单用户不 resolve）。
            has_project_fields = "project_id" in patch or "project_dir" in patch
            if (
                is_agentos_routing_client(_resolve(agent_client))
                and has_project_fields
            ):
                from jiuwenswarm.gateway.routing.e2a_proxy import (
                    resolve_agent_cron_project_binding,
                )

                existing = await _get_owned_cron_job(cc, job_id, user_id)
                # 与 _get_owned_cron_job 相同的 dict/object 双态读取：
                # 真实 CronController.get_job 返回 to_dict() 的 dict。
                existing_work_mode = str(
                    existing.get("work_mode", "")
                    if isinstance(existing, dict)
                    else getattr(existing, "work_mode", "")
                )
                existing_session_id = (
                    existing.get("session_id")
                    if isinstance(existing, dict)
                    else getattr(existing, "session_id", None)
                )
                binding_params = dict(patch)
                binding_params.setdefault("work_mode", existing_work_mode)
                bound, binding = await resolve_agent_cron_project_binding(
                    agent_client=_resolve(agent_client), params=binding_params,
                    user_id=user_id, channel_id="web", session_id=existing_session_id,
                )
                if not bound:
                    await channel.send_response(
                        ws, req_id, ok=False,
                        error=str(binding.get("error") or "cron project binding failed"),
                        code=str(binding.get("code") or "SERVICE_UNAVAILABLE"),
                    )
                    return
                resolved_project_id = binding.get("project_id")
                resolved_work_mode = binding.get("work_mode")
                if (
                    not isinstance(resolved_project_id, str)
                    or not isinstance(resolved_work_mode, str)
                    or not resolved_work_mode.strip()
                ):
                    await channel.send_response(
                        ws, req_id, ok=False,
                        error="invalid cron project binding",
                        code="BAD_REQUEST",
                    )
                    return
                patch.update({
                    "project_id": resolved_project_id,
                    "work_mode": resolved_work_mode,
                })
                patch.pop("project_dir", None)
                patch["_agentos_project_binding_verified"] = True
            job = await cc.update_job(job_id, patch)
            await channel.send_response(ws, req_id, ok=True, payload={"job": job})
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
        except Exception as e:  # noqa: BLE001
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="BAD_REQUEST")

    async def _cron_job_delete(ws, req_id, params, session_id, user_id=None):
        cc = _get_cron()
        if cc is None:
            await channel.send_response(ws, req_id, ok=False, error="cron not available", code="INTERNAL_ERROR")
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        # proactive.tick job 由主动推荐开关自动创建/删除，禁止面板删除。
        existing = await _get_owned_cron_job(cc, job_id, user_id)
        if existing is None:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
            return
        if existing is not None and str(getattr(existing, "mode", "") or "").strip().lower() == "proactive.tick":
            await channel.send_response(
                ws, req_id, ok=False,
                error="主动推荐定时任务由设置→主动推荐开关控制，不能在面板删除；请到设置关闭开关。",
                code="BAD_REQUEST",
            )
            return
        deleted = await cc.delete_job(job_id)
        if not deleted:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
            return
        await channel.send_response(ws, req_id, ok=True, payload={"deleted": True})

    async def _cron_job_toggle(ws, req_id, params, session_id, user_id=None):
        cc = _get_cron()
        if cc is None:
            await channel.send_response(ws, req_id, ok=False, error="cron not available", code="INTERNAL_ERROR")
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        enabled = params.get("enabled", None)
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        if enabled is None:
            await channel.send_response(ws, req_id, ok=False, error="enabled is required", code="BAD_REQUEST")
            return
        # proactive.tick job 的 enabled 由 config 开关驱动，禁止面板手动切换。
        existing = await _get_owned_cron_job(cc, job_id, user_id)
        if existing is None:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
            return
        if existing is not None and str(getattr(existing, "mode", "") or "").strip().lower() == "proactive.tick":
            await channel.send_response(
                ws, req_id, ok=False,
                error="主动推荐定时任务由设置→主动推荐开关控制，不能在面板启停。",
                code="BAD_REQUEST",
            )
            return
        try:
            job = await cc.toggle_job(job_id, bool(enabled))
            await channel.send_response(ws, req_id, ok=True, payload={"job": job})
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")

    async def _cron_job_preview(ws, req_id, params, session_id, user_id=None):
        cc = _get_cron()
        if cc is None:
            await channel.send_response(ws, req_id, ok=False, error="cron not available", code="INTERNAL_ERROR")
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        count = params.get("count", 5)
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        try:
            if await _get_owned_cron_job(cc, job_id, user_id) is None:
                await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
                return
            next_runs = await cc.preview_job(job_id, int(count) if count is not None else 5)
            await channel.send_response(ws, req_id, ok=True, payload={"next": next_runs})
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
        except Exception as e:  # noqa: BLE001
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="BAD_REQUEST")

    async def _cron_job_run_now(ws, req_id, params, session_id, user_id=None):
        cc = _get_cron()
        if cc is None:
            await channel.send_response(ws, req_id, ok=False, error="cron not available", code="INTERNAL_ERROR")
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        try:
            # 先取 job 拿 last_session_id（回退值），再触发 run_now 取 run_id
            # 对齐 chat.send 的 {accepted, session_id} 语义；首次执行 last_session_id
            # 为 None → session_id 空串（会话尚未就绪，前端轮询 cron.job.get 获取）
            job = await _get_owned_cron_job(cc, job_id, user_id)
            if job is None:
                await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
                return
            run_info = await cc.run_now_info(job_id)
            await channel.send_response(
                ws, req_id, ok=True,
                payload={
                    "accepted": True,
                    "run_id": run_info.get("run_id", ""),
                    "session_id": run_info.get("session_id", ""),
                },
            )
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
        except Exception as e:  # noqa: BLE001
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    channel.register_method("external_cli.detect", _external_cli_detect)
    channel.register_method("external_cli.codex_install_status", _external_cli_codex_install_status)
    channel.register_method("external_cli.install_status", _external_cli_install_status)

    async def _proxy_config_request(ws, req_id, params, session_id, user_id=None, *, req_method):
        """经 E2A 在目标 AgentServer 注入目录执行配置请求（仅 AgentOS 多用户调用）。

        单用户共享目录由 ``_register_config_proxy`` 走本地 handler（保留
        on_config_saved 热更新与既有写回语义）；本函数只服务 AgentOS 路径。
        """
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve(agent_client),
            ws=ws,
            req_id=req_id,
            params=params if isinstance(params, dict) else {},
            session_id=session_id,
            user_id=user_id,
            req_method=req_method,
            label="config",
        )

    def _register_config_proxy(method_name, req_method, local_handler):
        async def _handler(ws, req_id, params, session_id, user_id=None):
            # 单用户共享目录：本地执行（保留 on_config_saved 热更新与原有写回语义）。
            # AgentOS 多用户：经 E2A 在目标 AgentServer 注入目录执行（与 command.model 同模式）。
            from jiuwenswarm.gateway.routing.e2a_proxy import is_legacy_shared_directory_client

            resolved_client = _resolve(agent_client)
            if resolved_client is None or is_legacy_shared_directory_client(resolved_client):
                await local_handler(ws, req_id, params, session_id)
                return
            await _proxy_config_request(
                ws, req_id, params, session_id, user_id, req_method=req_method
            )
        channel.register_method(method_name, _handler)

    from jiuwenswarm.common.schema.message import ReqMethod as _ConfigReq
    _register_config_proxy("config.get", _ConfigReq.CONFIG_GET, _config_get)
    _register_config_proxy("config.set", _ConfigReq.CONFIG_SET, _config_set)
    _register_config_proxy("config.save_all", _ConfigReq.CONFIG_SAVE_ALL, _config_save_all)
    _register_config_proxy("config.validate_model", _ConfigReq.CONFIG_VALIDATE_MODEL, _config_validate_model)
    _register_config_proxy("models.list", _ConfigReq.MODELS_LIST, _models_list)
    _register_config_proxy("models.replace_all", _ConfigReq.MODELS_REPLACE_ALL, _models_replace_all)
    _register_config_proxy("models.validate", _ConfigReq.MODELS_VALIDATE, _models_validate)

    # vendors.* 为本分支新增的厂商选择接口，不经 config proxy（无 AgentOS 多用户注入目录语义），
    # 直接走本地 handler。
    channel.register_method("vendors.list", _vendors_list)
    channel.register_method("vendors.fetch_models", _vendors_fetch_models)
    channel.register_method("channel.get", _channel_get)
    channel.register_method("openai_account.auth.status", _openai_account_auth_status)
    channel.register_method("openai_account.auth.start_login", _openai_account_auth_start_login)
    channel.register_method("openai_account.auth.pending_login", _openai_account_auth_pending_login)
    channel.register_method("openai_account.auth.poll_login", _openai_account_auth_poll_login)
    channel.register_method("openai_account.auth.logout", _openai_account_auth_logout)
    channel.register_method("openai_account.models.list", _openai_account_models_list)

    channel.register_method("session.list", _session_list)
    channel.register_method("session.create", _session_create)
    channel.register_method("session.delete", _session_delete)
    channel.register_method("session.get_metadata", _session_get_metadata)
    channel.register_method("session.rename", _session_rename)
    channel.register_method("session.pin", _session_pin)

    channel.register_method("project.list", _project_list)
    channel.register_method("project.info", _project_info)
    channel.register_method("project.get_sessions", _project_get_sessions)
    channel.register_method("project.get_cron_sessions", _project_get_cron_sessions)
    channel.register_method("project.create", _project_create)
    channel.register_method("project.rename", _project_rename)
    channel.register_method("project.pin", _project_pin)
    channel.register_method("project.remove", _project_remove)
    channel.register_method("project.restore", _project_restore)
    channel.register_method("project.pinned_sessions", _project_pinned_sessions)

    # Git RPC handlers (设计文档 §4.1.11-§4.1.15)
    channel.register_method("project.git.status", _project_git_status)
    channel.register_method("project.git.probe", _project_git_probe)
    channel.register_method("project.git.init", _project_git_init)
    channel.register_method("project.git.switch_branch", _project_git_switch_branch)
    channel.register_method("project.git.create_branch", _project_git_create_branch)
    channel.register_method("project.git.commit", _project_git_commit)
    channel.register_method("project.git.push", _project_git_push)
    channel.register_method("project.git.diff_status", _project_git_diff_status)
    channel.register_method("project.git.turn_diff_list", _project_git_turn_diff_list)
    channel.register_method("project.git.turn_diff", _project_git_turn_diff)

    channel.register_method("path.get", _path_get)
    channel.register_method("path.set", _path_set)
    channel.register_method("path.select_directory", _path_select_directory)
    channel.register_method("path.select_files", _path_select_files)
    channel.register_method("path.select_file", _path_select_file)

    async def _hooks_list(ws, req_id, params, session_id, user_id=None):
        # 单用户共享目录：本地读取 config.yaml（hooks 属用户态配置，共享目录等价；
        # AgentServer 未启动/断连时保持可用）。AgentOS：经 E2A 在目标注入目录执行。
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import (
            is_legacy_shared_directory_client,
            proxy_unary_request,
        )

        resolved_client = _resolve(agent_client)
        if resolved_client is None or is_legacy_shared_directory_client(resolved_client):
            from jiuwenswarm.common.hooks_config import load_hooks_config

            try:
                hooks_config = load_hooks_config(get_config())
                summary = hooks_config.get_event_summary()
                await channel.send_response(
                    ws, req_id, ok=True,
                    payload={
                        "events": summary,
                        "disable_all_hooks": hooks_config.disable_all_hooks,
                        "source": "config.yaml",
                    },
                )
            except Exception as e:  # noqa: BLE001
                await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")
            return

        await proxy_unary_request(
            channel=channel, agent_client=_resolve(agent_client), ws=ws,
            req_id=req_id, params={}, session_id=session_id, user_id=user_id,
            req_method=ReqMethod.HOOKS_LIST, label="hooks.list",
        )

    channel.register_method("memory.compute", _memory_compute)
    channel.register_method("hooks.list", _hooks_list)
    # 注意：commands.list 是本地 handler，刻意不加入 _FORWARD_REQ_METHODS（否则会被
    # 转发到 AgentServer 与本地 handler 冲突）。静态元数据本地返回最快、依赖最少。
    channel.register_method("commands.list", _commands_list)

    channel.register_method("chat.send", _chat_send)
    channel.register_method("media.persist", _media_persist)
    channel.register_method("document.persist", _document_persist)
    channel.register_method("document.formats", _document_formats)
    channel.register_method("chat.resume", _chat_resume)
    channel.register_method("chat.interrupt", _chat_interrupt)
    channel.register_method("chat.user_answer", _chat_user_answer)
    channel.register_method("history.get", _history_get)
    channel.register_method("locale.get_conf", _locale_get_conf)
    channel.register_method("locale.set_conf", _locale_set_conf)
    channel.register_method("updater.get_status", _updater_get_status)
    channel.register_method("updater.check", _updater_check)
    channel.register_method("updater.download", _updater_download)
    channel.register_method("updater.upgrade", _updater_upgrade)
    channel.register_method("updater.get_conf", _updater_get_conf)
    channel.register_method("updater.reset_source", _updater_reset_source)
    channel.register_method("updater.set_conf", _updater_set_conf)
    channel.register_method("health_check.get_conf", _health_check_get_conf)
    channel.register_method("health_check.set_conf", _health_check_set_conf)
    # Deprecated aliases for clients upgrading from the pre-split probe API.
    channel.register_method("heartbeat.get_conf", _health_check_get_conf)
    channel.register_method("heartbeat.set_conf", _health_check_set_conf)
    channel.register_method("channel.feishu.get_conf", _channel_feishu_get_conf)
    channel.register_method("channel.feishu.set_conf", _channel_feishu_set_conf)
    channel.register_method("channel.xiaoyi.get_conf", _channel_xiaoyi_get_conf)
    channel.register_method("channel.xiaoyi.set_conf", _channel_xiaoyi_set_conf)
    channel.register_method("channel.telegram.get_conf", _channel_telegram_get_conf)
    channel.register_method("channel.telegram.set_conf", _channel_telegram_set_conf)
    channel.register_method("channel.dingtalk.get_conf", _channel_dingtalk_get_conf)
    channel.register_method("channel.dingtalk.set_conf", _channel_dingtalk_set_conf)
    channel.register_method("channel.whatsapp.get_conf", _channel_whatsapp_get_conf)
    channel.register_method("channel.whatsapp.set_conf", _channel_whatsapp_set_conf)
    channel.register_method("channel.discord.get_conf", _channel_discord_get_conf)
    channel.register_method("channel.discord.set_conf", _channel_discord_set_conf)
    channel.register_method("channel.slack.get_conf", _channel_slack_get_conf)
    channel.register_method("channel.slack.set_conf", _channel_slack_set_conf)
    channel.register_method("channel.wecom.get_conf", _channel_wecom_get_conf)
    channel.register_method("channel.wecom.set_conf", _channel_wecom_set_conf)
    channel.register_method("channel.wechat.get_conf", _channel_wechat_get_conf)
    channel.register_method("channel.wechat.set_conf", _channel_wechat_set_conf)
    channel.register_method("channel.wechat.get_login_ui", _channel_wechat_get_login_ui)
    channel.register_method("channel.wechat.unbind", _channel_wechat_unbind)
    channel.register_method("cron.job.list", _cron_job_list)
    channel.register_method("cron.job.meta", _cron_job_meta)
    channel.register_method("cron.job.get", _cron_job_get)
    channel.register_method("cron.job.create", _cron_job_create)
    channel.register_method("cron.job.update", _cron_job_update)
    channel.register_method("cron.job.delete", _cron_job_delete)
    channel.register_method("cron.job.toggle", _cron_job_toggle)
    channel.register_method("cron.job.preview", _cron_job_preview)
    channel.register_method("cron.job.run_now", _cron_job_run_now)

    # ---- 新 Heartbeat 任务(线程续跑) Web/RPC handlers ----
    # 与旧探活 health_check.* 严格区分;heartbeat.job.* 只服务新心跳任务。
    # 响应壳 {ok, payload/error, code} 对齐 cron.job.*。
    from jiuwenswarm.gateway.heartbeat import HeartbeatServiceUnavailableError

    def _get_heartbeat():
        return _resolve(heartbeat_controller)

    async def _heartbeat_unavailable(ws, req_id, error="heartbeat not available"):
        await channel.send_response(
            ws, req_id, ok=False, error=str(error), code="SERVICE_UNAVAILABLE"
        )

    async def _hb_job_list(ws, req_id, params, session_id, user_id=None):
        hc = _get_heartbeat()
        if hc is None:
            await _heartbeat_unavailable(ws, req_id)
            return
        try:
            result = await hc.list_jobs(
                params if isinstance(params, dict) else {},
                access_session_id=session_id,
                user_id=str(user_id or ""),
            )
        except PermissionError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="FORBIDDEN")
            return
        except ValueError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="BAD_REQUEST")
            return
        except HeartbeatServiceUnavailableError as e:
            await _heartbeat_unavailable(ws, req_id, e)
            return
        await channel.send_response(ws, req_id, ok=True, payload={"jobs": result.get("jobs", [])})

    async def _hb_job_meta(ws, req_id, params, session_id, user_id=None):
        hc = _get_heartbeat()
        if hc is None:
            await _heartbeat_unavailable(ws, req_id)
            return
        try:
            result = await hc.get_meta(user_id=str(user_id or ""))
        except HeartbeatServiceUnavailableError as e:
            await _heartbeat_unavailable(ws, req_id, e)
            return
        await channel.send_response(ws, req_id, ok=True, payload=result)

    async def _hb_job_get(ws, req_id, params, session_id, user_id=None):
        hc = _get_heartbeat()
        if hc is None:
            await _heartbeat_unavailable(ws, req_id)
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        try:
            job = await hc.get_job(
                job_id,
                access_session_id=session_id,
                user_id=str(user_id or ""),
            )
        except PermissionError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="FORBIDDEN")
            return
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
            return
        except HeartbeatServiceUnavailableError as e:
            await _heartbeat_unavailable(ws, req_id, e)
            return
        if job is None:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
            return
        await channel.send_response(ws, req_id, ok=True, payload={"job": job})

    async def _hb_job_create(ws, req_id, params, session_id, user_id=None):
        hc = _get_heartbeat()
        if hc is None:
            await _heartbeat_unavailable(ws, req_id)
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        # 普通 Web RPC 只能绑定当前 session；跨 session 迁移需独立管理权限入口。
        params = {**params, "channel_id": "web", "session_id": session_id, "source": "web_rpc"}
        try:
            job = await hc.create_job(params, user_id=str(user_id or ""))
        except ValueError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="BAD_REQUEST")
            return
        except HeartbeatServiceUnavailableError as e:
            await _heartbeat_unavailable(ws, req_id, e)
            return
        except Exception as e:  # noqa: BLE001
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")
            return
        await channel.send_response(ws, req_id, ok=True, payload={"job": job})

    async def _hb_job_update(ws, req_id, params, session_id, user_id=None):
        hc = _get_heartbeat()
        if hc is None:
            await _heartbeat_unavailable(ws, req_id)
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        patch = params.get("patch")
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        if not isinstance(patch, dict):
            await channel.send_response(ws, req_id, ok=False, error="patch must be object", code="BAD_REQUEST")
            return
        try:
            job = await hc.update_job(
                job_id,
                patch,
                access_session_id=session_id,
                user_id=str(user_id or ""),
            )
        except PermissionError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="FORBIDDEN")
            return
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
            return
        except ValueError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="BAD_REQUEST")
            return
        except HeartbeatServiceUnavailableError as e:
            await _heartbeat_unavailable(ws, req_id, e)
            return
        await channel.send_response(ws, req_id, ok=True, payload={"job": job})

    async def _hb_job_delete(ws, req_id, params, session_id, user_id=None):
        hc = _get_heartbeat()
        if hc is None:
            await _heartbeat_unavailable(ws, req_id)
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        try:
            result = await hc.delete_job(
                job_id,
                access_session_id=session_id,
                user_id=str(user_id or ""),
            )
        except PermissionError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="FORBIDDEN")
            return
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
            return
        except RuntimeError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="CONFLICT")
            return
        except HeartbeatServiceUnavailableError as e:
            await _heartbeat_unavailable(ws, req_id, e)
            return
        await channel.send_response(ws, req_id, ok=True, payload=result)

    async def _hb_job_toggle(ws, req_id, params, session_id, user_id=None):
        hc = _get_heartbeat()
        if hc is None:
            await _heartbeat_unavailable(ws, req_id)
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        enabled = params.get("enabled")
        if not isinstance(enabled, bool):
            await channel.send_response(ws, req_id, ok=False, error="enabled must be boolean", code="BAD_REQUEST")
            return
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        try:
            job = await hc.toggle_job(
                job_id,
                enabled,
                access_session_id=session_id,
                user_id=str(user_id or ""),
            )
        except PermissionError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="FORBIDDEN")
            return
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
            return
        except ValueError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="BAD_REQUEST")
            return
        except HeartbeatServiceUnavailableError as e:
            await _heartbeat_unavailable(ws, req_id, e)
            return
        await channel.send_response(ws, req_id, ok=True, payload={"job": job})

    async def _hb_job_preview(ws, req_id, params, session_id, user_id=None):
        hc = _get_heartbeat()
        if hc is None:
            await _heartbeat_unavailable(ws, req_id)
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        count = 5
        raw_count = params.get("count")
        if isinstance(raw_count, int) and raw_count > 0:
            count = raw_count
        try:
            result = await hc.preview_job(
                job_id,
                count=count,
                access_session_id=session_id,
                user_id=str(user_id or ""),
            )
        except PermissionError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="FORBIDDEN")
            return
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
            return
        except ValueError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="BAD_REQUEST")
            return
        except HeartbeatServiceUnavailableError as e:
            await _heartbeat_unavailable(ws, req_id, e)
            return
        await channel.send_response(ws, req_id, ok=True, payload=result)

    async def _hb_job_run_now(ws, req_id, params, session_id, user_id=None):
        hc = _get_heartbeat()
        if hc is None:
            await _heartbeat_unavailable(ws, req_id)
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        reschedule = params.get("reschedule", False)
        if not isinstance(reschedule, bool):
            await channel.send_response(ws, req_id, ok=False, error="reschedule must be boolean", code="BAD_REQUEST")
            return
        try:
            result = await hc.run_now(
                job_id,
                reschedule=reschedule,
                access_session_id=session_id,
                user_id=str(user_id or ""),
            )
        except PermissionError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="FORBIDDEN")
            return
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
            return
        except ValueError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="BAD_REQUEST")
            return
        except HeartbeatServiceUnavailableError as e:
            await _heartbeat_unavailable(ws, req_id, e)
            return
        await channel.send_response(ws, req_id, ok=True, payload=result)

    async def _hb_job_cancel(ws, req_id, params, session_id, user_id=None):
        hc = _get_heartbeat()
        if hc is None:
            await _heartbeat_unavailable(ws, req_id)
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        pause_schedule = params.get("pause_schedule", False)
        if not isinstance(pause_schedule, bool):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="pause_schedule must be boolean",
                code="BAD_REQUEST",
            )
            return
        try:
            result = await hc.cancel_run(
                job_id,
                pause_schedule=pause_schedule,
                access_session_id=session_id,
                user_id=str(user_id or ""),
            )
        except PermissionError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="FORBIDDEN")
            return
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
            return
        except HeartbeatServiceUnavailableError as e:
            await _heartbeat_unavailable(ws, req_id, e)
            return
        await channel.send_response(ws, req_id, ok=True, payload=result)

    channel.register_method("heartbeat.job.list", _hb_job_list)
    channel.register_method("heartbeat.job.meta", _hb_job_meta)
    channel.register_method("heartbeat.job.get", _hb_job_get)
    channel.register_method("heartbeat.job.create", _hb_job_create)
    channel.register_method("heartbeat.job.update", _hb_job_update)
    channel.register_method("heartbeat.job.delete", _hb_job_delete)
    channel.register_method("heartbeat.job.toggle", _hb_job_toggle)
    channel.register_method("heartbeat.job.preview", _hb_job_preview)
    channel.register_method("heartbeat.job.run_now", _hb_job_run_now)
    channel.register_method("heartbeat.job.cancel", _hb_job_cancel)

    # 数字分身 — permissions.owner_scopes：仅 Web 网关直连 config（不经 E2A / config_rpc）。
    # 其余 permissions.*（tools / rules / approval_overrides）走 _forward_permissions_to_agent。

    async def _permissions_owner_scopes_get(ws, req_id, params, session_id, user_id=None):
        from jiuwenswarm.gateway.routing.e2a_proxy import is_legacy_shared_directory_client, proxy_unary_request
        from jiuwenswarm.common.schema.message import ReqMethod

        resolved_client = _resolve(agent_client)
        if resolved_client is not None and not is_legacy_shared_directory_client(resolved_client):
            await proxy_unary_request(channel=channel, agent_client=resolved_client, ws=ws, req_id=req_id,
                                      params={}, session_id=session_id, user_id=user_id,
                                      req_method=ReqMethod.PERMISSIONS_OWNER_SCOPES_GET,
                                      label="permissions.owner_scopes.get")
            return
        from jiuwenswarm.common.config import get_permissions_owner_scopes

        try:
            payload = get_permissions_owner_scopes()
            await channel.send_response(ws, req_id, ok=True, payload=payload)
        except Exception as e:
            logger.exception("[permissions.owner_scopes.get] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _permissions_owner_scopes_set(ws, req_id, params, session_id, user_id=None):
        from jiuwenswarm.common.config import update_permissions_owner_scopes_in_config

        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        from jiuwenswarm.gateway.routing.e2a_proxy import is_legacy_shared_directory_client, proxy_unary_request
        from jiuwenswarm.common.schema.message import ReqMethod

        resolved_client = _resolve(agent_client)
        if resolved_client is not None and not is_legacy_shared_directory_client(resolved_client):
            await proxy_unary_request(channel=channel, agent_client=resolved_client, ws=ws, req_id=req_id,
                                      params=params, session_id=session_id, user_id=user_id,
                                      req_method=ReqMethod.PERMISSIONS_OWNER_SCOPES_SET,
                                      label="permissions.owner_scopes.set")
            return
        try:
            owner_scopes = params.get("owner_scopes", {})
            deny_guidance = params.get("deny_guidance_message")
            update_permissions_owner_scopes_in_config(owner_scopes, deny_guidance)
            applied_without_restart = await _apply_config_change_set(
                _ConfigChangeSet({}, ["permissions"], force=True)
            )
            await channel.send_response(
                ws,
                req_id,
                ok=True,
                payload={"ok": True, "applied_without_restart": applied_without_restart},
            )
        except Exception as e:
            logger.exception("[permissions.owner_scopes.set] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    channel.register_method("permissions.owner_scopes.get", _permissions_owner_scopes_get)
    channel.register_method("permissions.owner_scopes.set", _permissions_owner_scopes_set)

    async def _forward_permissions_to_agent(ws, req_id, params, session_id, user_id=None, *, req_method):
        """Forward permissions requests; Gateway never falls back to local state."""
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        if not isinstance(req_method, ReqMethod):
            await channel.send_response(ws, req_id, ok=False, error="invalid req_method", code="INTERNAL_ERROR")
            return
        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve(agent_client),
            ws=ws,
            req_id=req_id,
            params=params if isinstance(params, dict) else {},
            session_id=session_id,
            user_id=user_id,
            req_method=req_method,
            label="permissions",
        )

    from jiuwenswarm.common.schema.message import ReqMethod as _PermReq

    def _register_perm(method_name: str, rm: Any) -> None:
        async def _handler(ws, req_id, params, session_id, user_id=None):
            await _forward_permissions_to_agent(ws, req_id, params, session_id, user_id, req_method=rm)

        channel.register_method(method_name, _handler)

    _register_perm("permissions.tools.get", _PermReq.PERMISSIONS_TOOLS_GET)
    _register_perm("permissions.tools.set", _PermReq.PERMISSIONS_TOOLS_SET)
    _register_perm("permissions.tools.update", _PermReq.PERMISSIONS_TOOLS_UPDATE)
    _register_perm("permissions.tools.delete", _PermReq.PERMISSIONS_TOOLS_DELETE)
    _register_perm("permissions.rules.get", _PermReq.PERMISSIONS_RULES_GET)
    _register_perm("permissions.rules.create", _PermReq.PERMISSIONS_RULES_CREATE)
    _register_perm("permissions.rules.update", _PermReq.PERMISSIONS_RULES_UPDATE)
    _register_perm("permissions.rules.delete", _PermReq.PERMISSIONS_RULES_DELETE)
    _register_perm("permissions.approval_overrides.get", _PermReq.PERMISSIONS_APPROVAL_OVERRIDES_GET)
    _register_perm("permissions.approval_overrides.delete", _PermReq.PERMISSIONS_APPROVAL_OVERRIDES_DELETE)

    async def _memory_forbidden_get(ws, req_id, params, session_id, user_id=None):
        from jiuwenswarm.gateway.routing.e2a_proxy import is_legacy_shared_directory_client, proxy_unary_request
        from jiuwenswarm.common.schema.message import ReqMethod

        resolved_client = _resolve(agent_client)
        if resolved_client is not None and not is_legacy_shared_directory_client(resolved_client):
            await proxy_unary_request(channel=channel, agent_client=resolved_client, ws=ws, req_id=req_id,
                                      params={}, session_id=session_id, user_id=user_id,
                                      req_method=ReqMethod.MEMORY_FORBIDDEN_GET, label="memory.forbidden.get")
            return
        try:
            cfg = get_config() or {}
            payload = cfg.get("memory", {}).get("forbidden_memory_definition", {})
            await channel.send_response(ws, req_id, ok=True, payload=payload)
        except Exception as e:
            logger.exception("[memory.forbidden.get] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _memory_forbidden_set(ws, req_id, params, session_id, user_id=None):
        from jiuwenswarm.common.config import update_memory_forbidden_in_config
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        from jiuwenswarm.gateway.routing.e2a_proxy import is_legacy_shared_directory_client, proxy_unary_request
        from jiuwenswarm.common.schema.message import ReqMethod

        resolved_client = _resolve(agent_client)
        if resolved_client is not None and not is_legacy_shared_directory_client(resolved_client):
            await proxy_unary_request(channel=channel, agent_client=resolved_client, ws=ws, req_id=req_id,
                                      params=params, session_id=session_id, user_id=user_id,
                                      req_method=ReqMethod.MEMORY_FORBIDDEN_SET, label="memory.forbidden.set")
            return
        try:
            update_memory_forbidden_in_config(params)
            await channel.send_response(ws, req_id, ok=True, payload={"ok": True})
        except Exception as e:
            logger.exception("[memory.forbidden.set] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    channel.register_method("memory.forbidden.get", _memory_forbidden_get)
    channel.register_method("memory.forbidden.set", _memory_forbidden_set)

    async def _forward_harness_to_agent(ws, req_id, params, session_id, user_id=None, *, req_method):
        """harness.*：经 E2A 转发到 AgentServer（harness 包目录在 AgentServer 注入目录）。

        AgentServer 不可达时：默认本地 WebSocket client（单用户共享目录）由
        ``e2a_proxy`` 的 legacy fallback 在 Gateway 进程内执行同一业务；远程/
        AgentOS client 返回可重试错误，不读部署侧目录。
        """
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        if not isinstance(req_method, ReqMethod):
            await channel.send_response(ws, req_id, ok=False, error="invalid req_method", code="INTERNAL_ERROR")
            return

        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve(agent_client),
            ws=ws,
            req_id=req_id,
            params=params if isinstance(params, dict) else {},
            session_id=session_id,
            user_id=user_id,
            req_method=req_method,
            label="harness",
        )

    from jiuwenswarm.common.schema.message import ReqMethod as _HarnessReq

    def _register_harness(method_name: str, rm: Any) -> None:
        async def _handler(ws, req_id, params, session_id, user_id=None):
            await _forward_harness_to_agent(ws, req_id, params, session_id, user_id, req_method=rm)

        channel.register_method(method_name, _handler)

    _register_harness("harness.packages", _HarnessReq.HARNESS_PACKAGES_GET)
    _register_harness("harness.packages.scan", _HarnessReq.HARNESS_PACKAGES_SCAN)
    _register_harness("harness.activate", _HarnessReq.HARNESS_PACKAGES_ACTIVATE)
    _register_harness("harness.deactivate", _HarnessReq.HARNESS_PACKAGES_DEACTIVATE)
    _register_harness("harness.delete", _HarnessReq.HARNESS_PACKAGES_DELETE)

    async def _harness_import_handler(ws, req_id, params, session_id, user_id=None):
        """Import harness archives without exceeding the internal WS frame limit."""
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.agent_http_bridge import (
            E2A_PAYLOAD_MAX_BYTES,
            upload_file_bytes_via_e2a,
        )
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        request_params = params if isinstance(params, dict) else {}
        resolved_client = _resolve(agent_client)
        raw_content = request_params.get("file_content")
        # The browser link accepts substantially larger payloads than the
        # Gateway--AgentServer WebSocket.  Move larger archives in bounded E2A
        # chunks first; the final import request then contains only a path in
        # the target AgentServer's injected directory.
        if (
            isinstance(raw_content, str)
            and getattr(resolved_client, "server_ready", False)
        ):
            try:
                archive = base64.b64decode(raw_content, validate=True)
            except Exception:
                archive = None
            if archive is not None and len(archive) > E2A_PAYLOAD_MAX_BYTES:
                ok, upload_payload = await upload_file_bytes_via_e2a(
                    archive,
                    f"auto-harness/temp/uploads/harness_{uuid.uuid4().hex}.zip",
                    agent_client=resolved_client,
                    user_id=user_id,
                    channel_id=channel.channel_id,
                    session_id=session_id,
                )
                if not ok:
                    await channel.send_response(
                        ws, req_id, ok=False,
                        error=str(upload_payload.get("error") or "archive upload failed"),
                        code=str(upload_payload.get("code") or "SERVICE_UNAVAILABLE"),
                    )
                    return
                request_params = {
                    **request_params,
                    "uploaded_path": str(upload_payload.get("path") or ""),
                }
                request_params.pop("file_content", None)
        await proxy_unary_request(channel=channel, agent_client=_resolve(agent_client), ws=ws,
                                  req_id=req_id, params=request_params,
                                  session_id=session_id, user_id=user_id,
                                  req_method=ReqMethod.HARNESS_PACKAGES_IMPORT,
                                  label="harness.import", preserve_error_payload=True)

    channel.register_method("harness.import", _harness_import_handler)

    async def _harness_export_handler(ws, req_id, params, session_id, user_id=None):
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request
        await proxy_unary_request(channel=channel, agent_client=_resolve(agent_client), ws=ws,
                                  req_id=req_id, params=params if isinstance(params, dict) else {},
                                  session_id=session_id, user_id=user_id,
                                  req_method=ReqMethod.HARNESS_PACKAGES_EXPORT,
                                  label="harness.export", preserve_error_payload=True)

    channel.register_method("harness.export", _harness_export_handler)

    real_agent_client = _resolve(agent_client)
    # Container file transfer is HTTP on the WebChannel port (dual_protocol),
    # not WS JSON-RPC. Bind any client that already exposes the container-file
    # methods so build_web_channel_app can mount /file-api/* at channel.start().
    # Do not import AgentOSRouterClient here: this module must not depend on extensions.
    if _supports_container_file_api(real_agent_client):
        channel.container_file_client = real_agent_client
    else:
        channel.container_file_client = None
