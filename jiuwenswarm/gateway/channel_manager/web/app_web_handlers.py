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
    update_heartbeat_in_config,
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
from jiuwenswarm.common.reasoning_injector import build_reasoning_model_request_kwargs
from jiuwenswarm.common.updater import DEFAULT_SOURCE_CONFIG, UpdaterService
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.utils import (
    get_agent_sessions_dir,
    get_env_file,
    get_root_dir,
    get_user_workspace_dir
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
from jiuwenswarm.agents.harness.common.auto_harness import AutoHarnessService
from jiuwenswarm.agents.harness.common.tools.web_file_download import build_file_download_info
from jiuwenswarm.common.version import __version__
from jiuwenswarm.gateway.media_attachments import normalize_chat_media_attachments
from jiuwenswarm.gateway.document_attachments import (
    forbidden_formats,
    persist_and_parse_documents,
)
from jiuwenswarm.server.runtime.session import project_store
from jiuwenswarm.symphony.skill_retrieval.taxonomy_config import (
    coerce_root_categories_value,
    root_categories_to_text,
)

for _jiuwen_log in LogManager.get_all_loggers().values():
    _jiuwen_log.set_level(logging.INFO)

logger = logging.getLogger(__name__)


_WEB_CONFIG_RELOAD_CHANNEL_ID = "web"
_MODEL_RELOAD_ENV_KEYS = {
    "MODEL_PROVIDER",
    "MODEL_NAME",
    "API_BASE",
    "API_KEY",
    "VIDEO_PROVIDER",
    "VIDEO_MODEL_NAME",
    "VIDEO_API_BASE",
    "VIDEO_API_KEY",
    "AUDIO_PROVIDER",
    "AUDIO_MODEL_NAME",
    "AUDIO_API_BASE",
    "AUDIO_API_KEY",
    "VISION_PROVIDER",
    "VISION_MODEL_NAME",
    "VISION_API_BASE",
    "VISION_API_KEY",
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
    external_cli_dependency_installs: dict[str, dict[str, Any]] | None = None


def _external_cli_dependency_install_status_template() -> dict[str, Any]:
    return {
        "status": "idle",
        "phase": "idle",
        "error": "",
        "last_log": "",
        "log_tail": [],
        "started_at": 0.0,
        "finished_at": 0.0,
        "updated_at": 0.0,
    }


_EXTERNAL_CLI_DEPENDENCY_MODULES = {
    "claude": "claude_agent_sdk",
    "codex": "openai_codex",
}
_EXTERNAL_CLI_DEPENDENCY_EXTRAS = {
    "claude": "claude",
    "codex": "codex",
}
_EXTERNAL_CLI_DEPENDENCY_DISPLAY_NAMES = {
    "claude": "Claude",
    "codex": "Codex",
}
_EXTERNAL_CLI_DEPENDENCY_INSTALL_LOCKS = {
    cli_agent: threading.Lock()
    for cli_agent in _EXTERNAL_CLI_DEPENDENCY_MODULES
}
_EXTERNAL_CLI_DEPENDENCY_INSTALL_STATUS: dict[str, dict[str, Any]] = {
    cli_agent: _external_cli_dependency_install_status_template()
    for cli_agent in _EXTERNAL_CLI_DEPENDENCY_MODULES
}
_EXTERNAL_CLI_DEPENDENCY_INSTALL_LOG_TAIL_LIMIT = 8


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
            reasoning_level = item.get("reasoning_level", "")
            if not _values_match(reasoning_level, resolved_mco.get("reasoning_level")):
                if reasoning_level:
                    new_mco["reasoning_level"] = _serialize_reasoning_level(reasoning_level)
                else:
                    new_mco.pop("reasoning_level", None)
            if not _values_match(item["timeout"], resolved_mcc.get("timeout")):
                new_mcc["timeout"] = item["timeout"]
            if not _values_match(item["alias"], (resolved_entry or {}).get("alias")):
                new_entry["alias"] = item["alias"]
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
    "session.create",
    "session.switch",
    "acp.tool_response",
    "team.delete",
    "command.goal",
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
    "skills.toggle",
    "skills.install",
    "skills.import_local",
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
    "skills.teamskillshub.install",
    "skills.teamskillshub.publish",
    "skills.teamskillshub.delete",
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
    "plugins.list",
    "plugins.install",
    "plugins.uninstall",
    "plugins.enable",
    "plugins.disable",
    "plugins.reload",
    "agent_templates.list",
    "agent_templates.show",
    "agent_templates.file.list",
    "agent_templates.file.read",
    "agent_templates.create",
    "agent_templates.install",
    "agent_templates.uninstall",
    "plugin_packages.list",
    "plugin_packages.show",
    "plugin_packages.create",
    "plugin_packages.install",
    "plugin_packages.uninstall",
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
    "session.create",
    "session.switch",
    "acp.tool_response",
    "team.templates.list",
    "team.bindings.list",
    "team.binding.create",
    "team.binding.generate",
    "team.session.bind",
    "team.delete",
    "command.goal",
    "team.snapshot",
    "team.history.get",
    "team.mq.publish",
    "skills.marketplace.list",
    "skills.list",
    "skills.installed",
    "skills.get",
    "skills.toggle",
    "skills.install",
    "skills.import_local",
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
    "skills.teamskillshub.install",
    "skills.teamskillshub.publish",
    "skills.teamskillshub.delete",
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
    "plugins.list",
    "plugins.install",
    "plugins.uninstall",
    "plugins.enable",
    "plugins.disable",
    "plugins.reload",
    "agent_templates.list",
    "agent_templates.show",
    "agent_templates.file.list",
    "agent_templates.file.read",
    "agent_templates.create",
    "agent_templates.install",
    "agent_templates.uninstall",
    "plugin_packages.list",
    "plugin_packages.show",
    "plugin_packages.create",
    "plugin_packages.install",
    "plugin_packages.uninstall",
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
    "external_cli.install_status",
})

# 配置信息：config.get 返回、config.set 可修改的键（前端 param 名 -> 环境变量名）
# default 模型 + video/audio/vision 多模型
_CONFIG_SET_ENV_MAP = {
    # default 模型（主对话）
    "model_provider": "MODEL_PROVIDER",
    "model": "MODEL_NAME",
    "api_base": "API_BASE",
    "api_key": "API_KEY",
    # video 模型
    "video_api_base": "VIDEO_API_BASE",
    "video_api_key": "VIDEO_API_KEY",
    "video_model": "VIDEO_MODEL_NAME",
    "video_provider": "VIDEO_PROVIDER",
    # audio 模型
    "audio_api_base": "AUDIO_API_BASE",
    "audio_api_key": "AUDIO_API_KEY",
    "audio_model": "AUDIO_MODEL_NAME",
    "audio_provider": "AUDIO_PROVIDER",
    # vision 模型
    "vision_api_base": "VISION_API_BASE",
    "vision_api_key": "VISION_API_KEY",
    "vision_model": "VISION_MODEL_NAME",
    "vision_provider": "VISION_PROVIDER",
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
    "skill_retrieval_build_branching_factor": (("build", "branching_factor"), "int", 128),
    "skill_retrieval_build_max_depth": (("build", "max_depth"), "int", 6),
    "skill_retrieval_build_root_categories": (("build", "root_categories"), "root_categories", ""),
    "skill_retrieval_build_max_workers": (("build", "max_workers"), "int", 2),
    "skill_retrieval_build_max_retries": (("build", "max_retries"), "non_negative_int", 2),
    "skill_retrieval_build_request_timeout_seconds": (("build", "request_timeout_seconds"), "float", 420.0),
    "skill_retrieval_build_total_timeout_seconds": (("build", "total_timeout_seconds"), "float", 0.0),
    "skill_retrieval_build_classification_batch_limit": (("build", "classification_batch_limit"), "int", 32),
    "skill_retrieval_build_discovery_seed": (("build", "discovery_seed"), "raw_int", 42),
    "skill_retrieval_build_postprocess_enabled": (("build", "postprocess_enabled"), "bool", True),
    "skill_retrieval_build_postprocess_max_passes": (("build", "postprocess_max_passes"), "non_negative_int", 1),
    "skill_retrieval_build_postprocess_min_skills": (("build", "postprocess_min_skills"), "int", 6),
    "skill_retrieval_build_equivalence_enabled": (("build", "equivalence_enabled"), "bool", True),
    "skill_retrieval_retrieve_compact_codes_enabled": (("retrieve", "compact_codes_enabled"), "bool", False),
    "skill_retrieval_retrieve_flatten_tree": (("retrieve", "flatten_tree"), "bool", False),
    "skill_retrieval_retrieve_max_exposure_depth": (("retrieve", "max_exposure_depth"), "int", 1),
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
    if value_type == "root_categories":
        return coerce_root_categories_value(value, allow_path=False) or ""
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
        elif value_type == "root_categories":
            flat[key] = root_categories_to_text(value)
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
        elif value_type == "root_categories":
            flat[key] = root_categories_to_text(value)
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


def _team_payload_requested_external_cli_agents(params: dict[str, Any]) -> set[str]:
    teams_raw = params.get("team")
    requested: set[str] = set()
    if not isinstance(teams_raw, list):
        return requested
    for team_item in teams_raw:
        if not isinstance(team_item, dict):
            continue
        external_cli_agents = team_item.get("external_cli_agents")
        if not isinstance(external_cli_agents, list):
            continue
        for item in external_cli_agents:
            cli_agent = item if isinstance(item, str) else item.get("cli_agent") if isinstance(item, dict) else ""
            if cli_agent in _EXTERNAL_CLI_DEPENDENCY_MODULES:
                requested.add(cli_agent)
    return requested


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


def _remove_external_cli_agents_from_team_payload(params: dict[str, Any], cli_agents: set[str]) -> dict[str, Any]:
    teams_raw = params.get("team")
    if not cli_agents or not isinstance(teams_raw, list):
        return params

    teams: list[Any] = []
    changed = False
    for team_item in teams_raw:
        if not isinstance(team_item, dict):
            teams.append(team_item)
            continue
        external_cli_agents = team_item.get("external_cli_agents")
        if not isinstance(external_cli_agents, list):
            teams.append(team_item)
            continue
        filtered_agents: list[Any] = []
        item_changed = False
        for item in external_cli_agents:
            cli_agent = item if isinstance(item, str) else item.get("cli_agent") if isinstance(item, dict) else ""
            if cli_agent in cli_agents:
                item_changed = True
                changed = True
                continue
            filtered_agents.append(item)
        if item_changed:
            item = dict(team_item)
            item["external_cli_agents"] = filtered_agents
            teams.append(item)
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


def _external_cli_dependency_module(cli_agent: str) -> str:
    module = _EXTERNAL_CLI_DEPENDENCY_MODULES.get(cli_agent)
    if module is None:
        raise ValueError(f"unsupported external cli agent: {cli_agent}")
    return module


def _external_cli_dependency_display_name(cli_agent: str) -> str:
    return _EXTERNAL_CLI_DEPENDENCY_DISPLAY_NAMES.get(cli_agent, cli_agent)


def _external_cli_desktop_missing_dependency_error(cli_agent: str) -> str:
    display_name = _external_cli_dependency_display_name(cli_agent)
    extra = _EXTERNAL_CLI_DEPENDENCY_EXTRAS[cli_agent]
    return (
        f"this desktop package does not include {display_name} support; "
        f"rebuild it after running `uv sync --extra {extra}`"
    )


def _is_frozen_runtime() -> bool:
    return bool(getattr(sys, "frozen", False))


def _snapshot_external_cli_dependency_install_status(cli_agent: str) -> dict[str, Any]:
    with _EXTERNAL_CLI_DEPENDENCY_INSTALL_LOCKS[cli_agent]:
        status = _EXTERNAL_CLI_DEPENDENCY_INSTALL_STATUS[cli_agent]
        snapshot = dict(status)
        snapshot["cli_agent"] = cli_agent
        snapshot["log_tail"] = list(status.get("log_tail") or [])
        return snapshot


def _update_external_cli_dependency_install_status(cli_agent: str, updates: dict[str, Any]) -> None:
    with _EXTERNAL_CLI_DEPENDENCY_INSTALL_LOCKS[cli_agent]:
        _EXTERNAL_CLI_DEPENDENCY_INSTALL_STATUS[cli_agent].update(updates)
        _EXTERNAL_CLI_DEPENDENCY_INSTALL_STATUS[cli_agent]["updated_at"] = time.time()


def _append_external_cli_dependency_install_log(cli_agent: str, line: str) -> None:
    stripped = line.strip()
    if not stripped:
        return
    with _EXTERNAL_CLI_DEPENDENCY_INSTALL_LOCKS[cli_agent]:
        status = _EXTERNAL_CLI_DEPENDENCY_INSTALL_STATUS[cli_agent]
        log_tail = list(status.get("log_tail") or [])
        log_tail.append(stripped)
        status.update({
            "last_log": stripped,
            "log_tail": log_tail[-_EXTERNAL_CLI_DEPENDENCY_INSTALL_LOG_TAIL_LIMIT:],
            "updated_at": time.time(),
        })


def _ensure_external_cli_dependency_available_or_start_install(cli_agent: str) -> dict[str, Any] | None:
    module = _external_cli_dependency_module(cli_agent)
    if importlib.util.find_spec(module) is not None:
        _update_external_cli_dependency_install_status(cli_agent, {
            "status": "succeeded",
            "phase": "succeeded",
            "error": "",
            "finished_at": time.time(),
        })
        return None

    if _is_frozen_runtime():
        _update_external_cli_dependency_install_status(cli_agent, {
            "status": "failed",
            "phase": "failed",
            "error": _external_cli_desktop_missing_dependency_error(cli_agent),
            "last_log": "",
            "log_tail": [],
            "started_at": 0.0,
            "finished_at": time.time(),
        })
        return _snapshot_external_cli_dependency_install_status(cli_agent)

    with _EXTERNAL_CLI_DEPENDENCY_INSTALL_LOCKS[cli_agent]:
        if _EXTERNAL_CLI_DEPENDENCY_INSTALL_STATUS[cli_agent].get("status") == "running":
            already_running = True
        else:
            already_running = False
            _EXTERNAL_CLI_DEPENDENCY_INSTALL_STATUS[cli_agent].update({
                "status": "running",
                "phase": "preparing",
                "error": "",
                "last_log": "",
                "log_tail": [],
                "started_at": time.time(),
                "finished_at": 0.0,
                "updated_at": time.time(),
            })
    if already_running:
        return _snapshot_external_cli_dependency_install_status(cli_agent)

    thread = threading.Thread(
        target=_run_external_cli_dependency_install_background,
        args=(cli_agent,),
        name=f"{cli_agent}-dependency-install",
        daemon=True,
    )
    thread.start()
    return _snapshot_external_cli_dependency_install_status(cli_agent)


def _ensure_external_cli_dependencies_available_or_start_install(
    cli_agents: set[str],
) -> dict[str, dict[str, Any]]:
    installs: dict[str, dict[str, Any]] = {}
    for cli_agent in sorted(cli_agents):
        snapshot = _ensure_external_cli_dependency_available_or_start_install(cli_agent)
        if snapshot is not None:
            installs[cli_agent] = snapshot
    return installs


def _run_external_cli_dependency_install_background(cli_agent: str) -> None:
    try:
        _install_external_cli_dependency(cli_agent)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[config.set] %s dependency installation failed: %s",
            _external_cli_dependency_display_name(cli_agent),
            exc,
        )
        _append_external_cli_dependency_install_log(cli_agent, str(exc))
        _update_external_cli_dependency_install_status(cli_agent, {
            "status": "failed",
            "phase": "failed",
            "error": str(exc),
            "finished_at": time.time(),
        })
        return

    _update_external_cli_dependency_install_status(cli_agent, {
        "status": "succeeded",
        "phase": "succeeded",
        "error": "",
        "finished_at": time.time(),
    })


def _install_external_cli_dependency(cli_agent: str) -> None:
    if _is_frozen_runtime():
        raise RuntimeError(_external_cli_desktop_missing_dependency_error(cli_agent))
    module = _external_cli_dependency_module(cli_agent)
    package = _resolve_openjiuwen_extra_package(_EXTERNAL_CLI_DEPENDENCY_EXTRAS[cli_agent])
    args = _build_optional_dependency_install_args(package)
    output_lines: list[str] = []
    _update_external_cli_dependency_install_status(cli_agent, {
        "phase": "installing",
    })
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
    deadline = time.monotonic() + 600
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
        if time.monotonic() > deadline:
            process.kill()
            raise RuntimeError(f"failed to install {cli_agent} dependency: timed out")

    reader.join(timeout=1)
    returncode = process.wait()
    if returncode != 0:
        output = "\n".join(output_lines[-20:])
        raise RuntimeError(f"failed to install {cli_agent} dependency: {output}")
    _update_external_cli_dependency_install_status(cli_agent, {
        "phase": "verifying",
    })
    if importlib.util.find_spec(module) is None:
        raise RuntimeError(f"failed to install {cli_agent} dependency: {module} is still unavailable")


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
    updater_service: UpdaterService | None = None


_CONTAINER_FILE_API_METHODS = (
    "upload_container_file",
    "download_container_file",
    "list_container_files",
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
                # AgentOS 备份模型的 mco 含 _source=="agentos" 标记（由
                # get_default_models 注入）。其 max_tokens 是输入侧上下文窗口
                # 别名（-> ContextEngineConfig.context_window_tokens，压缩阈值，
                # 不发厂商），不得进入输出侧的 ModelRequestConfig.max_tokens
                # （否则会被当输出上限发给厂商）。_source 标记本身由
                # reasoning_injector._build_model_request_kwargs 统一 pop，
                # 这里只需清 max_tokens。
                if model_config_obj.get("_source") == "agentos":
                    model_config_obj.pop("max_tokens", None)
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


def _register_web_handlers(bind: WebHandlersBindParams) -> None:
    """注册 Web 前端需要的 method 与 on_connect。
    on_config_saved: 可选，config.set 写回后调用的回调；
        updated_env_keys 为本次改动的键名集合，
        env_updates 为本次变更的环境变量增量（仅包含更新项），
        config_payload 为当前最新配置快照；
        返回 True 表示已热更新未重启，False 表示已安排进程重启。
    heartbeat_service: 可选，GatewayHeartbeatService 实例，用于处理 heartbeat.get_conf / heartbeat.set_conf。
    """
    channel = bind.channel
    agent_client = bind.agent_client
    message_handler = bind.message_handler
    channel_manager = bind.channel_manager
    on_config_saved = bind.on_config_saved
    heartbeat_service = bind.heartbeat_service
    cron_controller = bind.cron_controller
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
            # Opencode Zen 免费模型开关（models.enable_free_models，默认 true）
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
                elif value_type == "root_categories":
                    default_text = root_categories_to_text(default)
                else:
                    default_text = str(default)
                payload.setdefault(key, default_text)
            payload.setdefault("free_search_ddg_enabled", "false")
            payload.setdefault("free_search_bing_enabled", "false")
            payload.setdefault("proactive_recommendation_enabled", "false")
            payload.setdefault("enable_free_models", "true")
            payload.setdefault("proactive_recommendation_max_recommend_per_day", "10")
            payload.setdefault("proactive_recommendation_max_rounds_per_tick", "20")
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _external_cli_detect(ws, req_id, params, session_id):
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        cli_agent = str(params.get("cli_agent") or "").strip().lower()
        cli_path = str(params.get("cli_path") or "").strip()
        result = _detect_external_cli_agent(cli_agent, cli_path)
        await channel.send_response(ws, req_id, ok=True, payload=result)

    async def _external_cli_install_status(ws, req_id, params, session_id):
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        cli_agent = str(params.get("cli_agent") or "").strip().lower()
        if cli_agent not in _EXTERNAL_CLI_DEPENDENCY_MODULES:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=f"unsupported external cli agent: {cli_agent}",
                code="BAD_REQUEST",
            )
            return
        await channel.send_response(
            ws,
            req_id,
            ok=True,
            payload=_snapshot_external_cli_dependency_install_status(cli_agent),
        )

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
                requested_cli_agents = _team_payload_requested_external_cli_agents(params)
                external_cli_dependency_installs.update(
                    _ensure_external_cli_dependencies_available_or_start_install(requested_cli_agents)
                )
                params_to_save = _remove_external_cli_agents_from_team_payload(
                    params,
                    set(external_cli_dependency_installs),
                )
                if "team" in params:
                    _preserve_deleted_team_entities(params)
                replace_teams_in_config(_inject_external_cli_publish_url(params_to_save))
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
                        requested_cli_agents = {
                            item.get("cli_agent", "")
                            for item in external_cli_agents
                            if item.get("cli_agent") in _EXTERNAL_CLI_DEPENDENCY_MODULES
                        }
                        external_cli_dependency_installs.update(
                            _ensure_external_cli_dependencies_available_or_start_install(requested_cli_agents)
                        )
                        if external_cli_dependency_installs:
                            external_cli_agents = [
                                item
                                for item in external_cli_agents
                                if item.get("cli_agent") not in external_cli_dependency_installs
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

        return _ConfigApplyResult(env_updates, yaml_updated, external_cli_dependency_installs or None)

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
            reasoning_level = str(item.get("reasoning_level") or "").strip()

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
                "reasoning_level": reasoning_level,
                "origin_index": origin_index,
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

        updated_param_keys = [k for k, e in _CONFIG_SET_ENV_MAP.items() if e in env_updates] + yaml_updated
        payload = {"updated": updated_param_keys, "applied_without_restart": applied_without_restart}
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

        model_config_obj = _resolve_model_config_obj_for_validate(model, params)

        reasoning_mcc = {
            "client_provider": model_provider,
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
            api_key=api_key,
            api_base=api_base,
            timeout=25.0,
            max_retries=0,
            verify_ssl=verify_ssl,
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
            # 显式配置的上下文窗口上限（react.context_engine_config.context_window_tokens）
            # 优先级高于按模型名解析，与 AgentServer 侧 ContextEngine 行为保持一致
            cec = (config.get("react", {}) or {}).get("context_engine_config", {}) or {}
            cw_override = cec.get("context_window_tokens")
            if not (isinstance(cw_override, int) and cw_override > 0):
                cw_override = None
            for idx, entry in enumerate(models):
                mcc = entry.get("model_client_config", {})
                mco = entry.get("model_config_obj", {})
                is_default = entry.get("is_default", False)
                model_name = mcc.get("model_name", "")
                context_window_tokens = 0
                try:
                    from openjiuwen.core.context_engine.context.context_utils import ContextUtils
                    context_window_tokens = ContextUtils.resolve_context_max(
                        model_name=model_name,
                        fallback_context_window_tokens=cw_override,
                    )
                except Exception:
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
                    "reasoning_level": "off" if mco.get("reasoning_level") is False else mco.get("reasoning_level", ""),
                    "is_default": is_default,
                    "alias": entry.get("alias", ""),
                    "origin_index": idx,
                    "context_window_tokens": context_window_tokens,
                })
                # active_model 为列表首位的模型（主对话默认）；首次启动时该条目
                # 仍为 .env 占位符，则回退到 Zen 免费模型（如 DeepSeek V4 Flash）。
            default_name = result[0]["model_name"] if result else ""
            active_model = default_name
            try:
                from jiuwenswarm.common.model_config_validation import (
                    is_placeholder_model_entry,
                )
                from jiuwenswarm.server.runtime.opencode_zen import (
                    get_zen_default_free_model_entry,
                )
                if not default_name or is_placeholder_model_entry(
                    {
                        "api_base": result[0].get("api_base", ""),
                        "api_key": result[0].get("api_key", ""),
                        "model_name": result[0].get("model_name", ""),
                    }
                ):
                    zen_default = get_zen_default_free_model_entry()
                    if zen_default is not None:
                        zen_name = (
                            (zen_default.get("model_client_config") or {})
                            .get("model_name", "")
                        )
                        if zen_name:
                            active_model = zen_name
            except Exception:
                logger.debug("[models.list] resolve default model failed", exc_info=True)

            # 追加 Opencode Zen 免费模型（内存态，不入 config.yaml）。
            # 仅在此处合并展示，不影响 active_model（已取首位用户自配模型，
            # 占位时已在上方回退到 Zen 免费模型）。
            try:
                from jiuwenswarm.server.runtime.opencode_zen import (
                    get_zen_free_model_entries,
                    get_zen_free_context_window,
                )
                zen_entries = get_zen_free_model_entries()
                zen_cw = get_zen_free_context_window()
                existing_names = {r["model_name"] for r in result}
                for zent in zen_entries:
                    zmcc = zent.get("model_client_config") or {}
                    zname = zmcc.get("model_name", "")
                    if not zname or zname in existing_names:
                        continue
                    existing_names.add(zname)
                    result.append({
                        "model_name": zname,
                        "api_base": zmcc.get("api_base", ""),
                        "api_key": zmcc.get("api_key", ""),
                        "model_provider": zmcc.get("client_provider", ""),
                        "temperature": (zent.get("model_config_obj") or {}).get("temperature", 0.95),
                        "reasoning_level": "",
                        "is_default": True,  # 出现在前端下拉框（is_default !== false 过滤）
                        "alias": zent.get("alias", ""),
                        "origin_index": -1,  # 非 config 条目，不参与 replace_all 保留逻辑
                        "context_window_tokens": zen_cw,
                        "is_free": bool(zent.get("is_free", False)),  # 免费模型分组标识（前端据此归入"免费模型"组）
                    })
            except Exception:
                logger.debug("[models.list] append zen free models failed", exc_info=True)

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

            payload = {
                "updated": [k for k, e in _CONFIG_SET_ENV_MAP.items() if e in env_updates] + yaml_updated,
                "applied_without_restart": applied_without_restart,
                "models_count": models_count,
            }
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

    async def _session_list(ws, req_id, params, session_id):
        """返回会话列表,包含完整的会话管理信息。"""
        limit = 20
        offset = 0
        if isinstance(params, dict):
            raw_limit = params.get("limit")
            if isinstance(raw_limit, int) and not isinstance(raw_limit, bool):
                limit = raw_limit
            elif isinstance(raw_limit, float) and raw_limit.is_integer():
                # JSON 2.0 会被解析为 float,归一为 int;非整数浮点(2.5)落穿到默认
                limit = int(raw_limit)
            elif isinstance(raw_limit, str) and raw_limit.strip().isdigit():
                limit = int(raw_limit.strip())

            raw_offset = params.get("offset")
            if isinstance(raw_offset, int) and not isinstance(raw_offset, bool):
                offset = raw_offset
            elif isinstance(raw_offset, float) and raw_offset.is_integer():
                offset = int(raw_offset)
            elif isinstance(raw_offset, str) and raw_offset.strip().isdigit():
                offset = int(raw_offset.strip())

        limit = max(1, min(limit, 200))
        offset = max(0, offset)

        from jiuwenswarm.server.runtime.session.session_metadata import get_all_sessions_metadata

        sessions, total = get_all_sessions_metadata(limit=limit, offset=offset)

        # 通过 _to_session_info 投影,确保 work_mode 等字段有兜底值(与 session.get_metadata 一致)
        session_infos = [_to_session_info(s) for s in sessions]

        await channel.send_response(ws, req_id, ok=True, payload={
            "sessions": session_infos,
            "total": total,
            "limit": limit,
            "offset": offset,
        })

    async def _session_get_metadata(ws, req_id, params, session_id):
        """返回单个会话的元数据（mode / model / project_dir / last_user_message_at 等）。

        按单个 session_id 读取，O(1) 不扫描目录，会话再多也不卡；相互隔离。
        供前端恢复会话时还原模型/模式/项目路径选择器。
        """
        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST",
            )
            return
        sid = params.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            await channel.send_response(
                ws, req_id, ok=False, error="session_id is required", code="BAD_REQUEST",
            )
            return
        sid = sid.strip()

        from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata
        # cache_bust=True 强制读盘，跨进程（Gateway 读 / AgentServer 写）拿最新
        meta = get_session_metadata(sid, cache_bust=True)
        if not meta:
            await channel.send_response(
                ws, req_id, ok=False, error="session not found", code="NOT_FOUND",
            )
            return
        await channel.send_response(ws, req_id, ok=True, payload=meta)

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
        resolved_agent_client = _resolve(agent_client)
        if resolved_agent_client is None:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="AgentServer is unavailable",
                code="SERVICE_UNAVAILABLE",
            )
            return
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields

        create_params = dict(params)
        create_params.pop("session_id", None)
        authenticated_user_id = str(user_id or "").strip()
        if authenticated_user_id:
            create_params["user_id"] = authenticated_user_id
        else:
            create_params.pop("user_id", None)
        create_params.setdefault("create_token", secrets.token_hex(16))
        env = e2a_from_agent_fields(
            request_id=req_id,
            channel_id=channel.channel_id,
            req_method=ReqMethod.SESSION_CREATE,
            params=create_params,
            is_stream=False,
            timestamp=time.time(),
            user_id=authenticated_user_id or None,
        )
        try:
            response = await resolved_agent_client.send_request(env)
        except Exception as exc:  # noqa: BLE001
            await channel.send_response(
                ws, req_id, ok=False, error=str(exc), code="SERVICE_UNAVAILABLE"
            )
            return
        payload = dict(response.payload or {}) if isinstance(response.payload, dict) else {}
        await channel.send_response(
            ws,
            req_id,
            ok=bool(response.ok),
            payload=payload if response.ok else None,
            error=None if response.ok else str(payload.get("error") or "session.create failed"),
            code=None if response.ok else str(payload.get("code") or "SESSION_CREATE_FAILED"),
        )

    async def _session_rename(ws, req_id, params, session_id):
        """重命名会话标题(查询/设置/清除三种语义),复用 apply_session_rename。

        与 list/create/delete 同走本地路径,不转发 AgentServer。
        title 不传→查询、空串/纯空白→清除、非空→设置(截断 200 字符)。
        """
        from jiuwenswarm.server.runtime.session.session_rename import apply_session_rename

        ok, payload, err, code = apply_session_rename(
            params if isinstance(params, dict) else {},
            session_id,
            init_channel_id=channel.channel_id,
        )
        if ok:
            await channel.send_response(ws, req_id, ok=True, payload=payload or {})
        else:
            await channel.send_response(
                ws, req_id, ok=False, error=err or "session.rename failed", code=code,
            )

    async def _session_pin(ws, req_id, params, session_id):
        """置顶/取消置顶会话,操作后对所有置顶会话紧凑重编号为 1..N。幂等。

        置顶时会话从项目分组剥离,进入全局置顶区;取消置顶时回归原项目。
        """
        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST",
            )
            return
        sid = params.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            await channel.send_response(
                ws, req_id, ok=False, error="session_id is required", code="BAD_REQUEST",
            )
            return
        sid = sid.strip()
        raw_pinned = params.get("pinned")
        if not isinstance(raw_pinned, bool):
            await channel.send_response(
                ws, req_id, ok=False, error="pinned must be boolean", code="BAD_REQUEST",
            )
            return

        from jiuwenswarm.server.runtime.session.session_metadata import set_session_pinned

        result = set_session_pinned(sid, raw_pinned)
        if result is None:
            await channel.send_response(
                ws, req_id, ok=False, error="session not found", code="NOT_FOUND",
            )
            return
        new_pinned, new_order = result
        await channel.send_response(
            ws, req_id, ok=True, payload={"pinned": new_pinned, "pin_order": new_order},
        )

    async def _session_delete(ws, req_id, params, session_id, user_id=None):
        """删除一个 session（在 agent/sessions 下删除一个目录）。"""
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
        session_id_to_delete = session_id_to_delete.strip()

        from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields

        ac = _resolve(agent_client)
        if ac is not None and getattr(ac, "server_ready", False):
            try:
                env = e2a_from_agent_fields(
                    request_id=str(req_id) if req_id else "",
                    channel_id="",
                    session_id=session_id,
                    req_method=ReqMethod.SESSION_DELETE,
                    params=params,
                    user_id=user_id,
                )
                resp = await ac.send_request(env)
                if resp.ok:
                    pl = resp.payload if isinstance(resp.payload, dict) else {}
                    await channel.send_response(ws, req_id, ok=True, payload=pl)
                    return
                pl = resp.payload if isinstance(resp.payload, dict) else {}
                await channel.send_response(
                    ws,
                    req_id,
                    ok=False,
                    error=str(pl.get("error", "session.delete failed")),
                    code=pl.get("code"),
                )
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("[session.delete] forward to agent failed, fallback local: %s", e)

        metadata = get_session_metadata(session_id_to_delete)
        if str(metadata.get("mode") or "").strip().lower() == "team":
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="team session delete requires agent server",
                code="AGENT_UNAVAILABLE",
            )
            return

        from jiuwenswarm.server.runtime.session.session_history import resolve_session_dir

        session_dir, invalid_reason = resolve_session_dir(
            session_id_to_delete, sessions_root=get_agent_sessions_dir()
        )
        if session_dir is None:
            await channel.send_response(
                ws, req_id, ok=False, error=invalid_reason or "invalid session_id", code="BAD_REQUEST",
            )
            return
        if not session_dir.exists():
            await channel.send_response(
                ws, req_id, ok=False, error="session not found", code="NOT_FOUND",
            )
            return
        if not session_dir.is_dir():
            await channel.send_response(
                ws, req_id, ok=False, error="session is not a directory", code="BAD_REQUEST",
            )
            return
        from jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle import (
            evict_session_kv_cache,
        )

        try:
            await evict_session_kv_cache(
                session_id=session_id_to_delete,
                parent_session_id=session_id_to_delete,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[session.delete] KV cache evict hook failed during local fallback; "
                "continuing: session_id=%s error=%s",
                session_id_to_delete,
                exc,
            )
        shutil.rmtree(session_dir)
        await channel.send_response(ws, req_id, ok=True, payload={"session_id": session_id_to_delete})

    async def _project_list(ws, req_id, params, session_id):
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
        if not isinstance(params, dict):
            params = {}
        filter_val = str(params.get("filter") or "all").strip() or "all"
        if filter_val not in ("all", "pinned", "unpinned"):
            filter_val = "all"
        include_hidden = bool(params.get("include_hidden", False))
        # work_mode 过滤: "code" / "work" / 不传(全部)
        raw_work_mode = params.get("work_mode")
        work_mode_filter: str | None = None
        if isinstance(raw_work_mode, str) and raw_work_mode.strip():
            wmf = raw_work_mode.strip().lower()
            if wmf in SUPPORTED_WORK_MODES:
                work_mode_filter = wmf
            else:
                await channel.send_response(
                    ws, req_id, ok=False,
                    error=f"invalid work_mode: {wmf!r}, must be 'code' or 'work'",
                    code="BAD_REQUEST",
                )
                return

        from jiuwenswarm.server.runtime.session.session_metadata import collect_all_sessions_metadata

        # 加载全部项目(含隐藏,用于会话归属判断);cache_bust 跨进程拿最新
        all_projects_full = project_store.list_projects(include_hidden=True, cache_bust=True)
        # 按 work_mode 过滤项目(不影响默认项目输出)
        if work_mode_filter:
            all_projects = [p for p in all_projects_full if (p.work_mode or DEFAULT_WEB_WORK_MODE) == work_mode_filter]
        else:
            all_projects = all_projects_full
        # 可见(非隐藏)项目的 project_id 集合(会话仅按 project_id 归属)
        # 注意: 会话归属判断用全部项目(含跨模式),不按 work_mode_filter 截断,
        # 否则跨模式的孤立会话会漏统计。_attribute_session_project 按 session 自身的
        # work_mode 分桶到 default / default_code。
        visible_by_id_full = {p.project_id for p in all_projects_full if not p.hidden}

        sessions = collect_all_sessions_metadata()
        stats: dict[str, dict[str, Any]] = {}

        def _ensure_stats(key: str) -> dict[str, Any]:
            st = stats.get(key)
            if st is None:
                st = {"session_count": 0, "last_message_at": None, "last_user_message_at": None}
                stats[key] = st
            return st

        for s in sessions:
            # 仅统计 web 渠道会话
            if s.get("channel_id") != "web":
                continue
            # 置顶会话已从项目分组剥离,不计入任何项目统计
            if s.get("pinned"):
                continue
            # cron 会话不计入项目统计(由 project.get_cron_sessions 独立获取)
            if s.get("cron_id"):
                continue
            # 归属: 仅按 project_id 匹配,不命中归默认项目(按 session work_mode 分桶)
            key = _attribute_session_project(s, visible_by_id_full)
            st = _ensure_stats(key)
            st["session_count"] += 1
            lm = s.get("last_message_at")
            if isinstance(lm, (int, float)) and not isinstance(lm, bool):
                if st["last_message_at"] is None or lm > st["last_message_at"]:
                    st["last_message_at"] = lm
            lum = s.get("last_user_message_at")
            if isinstance(lum, (int, float)) and not isinstance(lum, bool):
                if st["last_user_message_at"] is None or lum > st["last_user_message_at"]:
                    st["last_user_message_at"] = lum

        def _zero_stats() -> dict[str, Any]:
            return {"session_count": 0, "last_message_at": None, "last_user_message_at": None}

        def _build_project_info(proj: Any, default_id: str | None = None) -> dict[str, Any]:
            if default_id is not None:
                # 默认项目(default / default_code)
                st = stats.get(default_id, _zero_stats())
                return _project_info_payload(None, default_id=default_id, stats=st)
            # 隐藏项目统计恒为 0/null(其非置顶会话已归属默认项目)
            st = _zero_stats() if proj.hidden else stats.get(proj.project_id, _zero_stats())
            return _project_info_payload(proj, stats=st)

        def _lum_sort_key(info: dict[str, Any]) -> float:
            v = info["last_user_message_at"]
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0

        # 根据 work_mode 过滤决定输出哪些默认项目条目
        default_ids: list[str] = []
        if not work_mode_filter or work_mode_filter == DEFAULT_WEB_WORK_MODE:
            default_ids.append(DEFAULT_PROJECT_ID_WORK)
        if not work_mode_filter or work_mode_filter == DEFAULT_TUI_WORK_MODE:
            default_ids.append(DEFAULT_PROJECT_ID_CODE)
        default_items = [_build_project_info(None, default_id=did) for did in default_ids]

        if filter_val == "pinned":
            # 仅置顶项目,按 pin_order 升序;隐藏项目已在 remove 时取消置顶,无需额外过滤
            items = [_build_project_info(p) for p in all_projects if p.pinned]
            items.sort(key=lambda x: x["pin_order"])
        elif filter_val == "unpinned":
            # 非置顶项目(按 include_hidden 决定是否含隐藏),按 last_user_message_at 倒序,末位默认项目
            items = [_build_project_info(p) for p in all_projects
                     if not p.pinned and (include_hidden or not p.hidden)]
            items.sort(key=_lum_sort_key, reverse=True)
            items.extend(default_items)
        else:  # "all"
            # 置顶项目在前(按 pin_order 升序) → 非置顶项目(按 last_user_message_at 倒序) → 末位默认项目
            pinned_items = [_build_project_info(p) for p in all_projects if p.pinned]
            pinned_items.sort(key=lambda x: x["pin_order"])
            unpinned_items = [_build_project_info(p) for p in all_projects
                              if not p.pinned and (include_hidden or not p.hidden)]
            unpinned_items.sort(key=_lum_sort_key, reverse=True)
            items = pinned_items + unpinned_items + default_items

        await channel.send_response(ws, req_id, ok=True, payload={"projects": items})

    def _to_session_info(meta: dict[str, Any]) -> dict[str, Any]:
        """将会话元数据投影为 SessionInfo(排除 delivery_context/channel_metadata 等内部字段)。"""
        lum = meta.get("last_user_message_at")
        return {
            "session_id": str(meta.get("session_id", "")),
            "title": str(meta.get("title", "")),
            "created_at": meta.get("created_at", 0),
            "last_message_at": meta.get("last_message_at", 0),
            "message_count": int(meta.get("message_count", 0)),
            "mode": str(meta.get("mode", "unknown")),
            "team_name": str(meta.get("team_name", "")),
            "pinned": bool(meta.get("pinned", False)),
            "pin_order": int(meta.get("pin_order", 0)),
            "project_dir": str(meta.get("project_dir", "")),
            "project_id": str(meta.get("project_id", "")),
            "cron_id": str(meta.get("cron_id", "")),
            "last_user_message_at": lum if isinstance(lum, (int, float)) and not isinstance(lum, bool) else None,
            "model": str(meta.get("model", "")),
            "work_mode": str(meta.get("work_mode") or DEFAULT_WEB_WORK_MODE),
        }

    async def _project_get_sessions(ws, req_id, params, session_id, user_id=None):
        """获取项目下的非置顶普通会话列表,按 last_user_message_at 倒序。

        会话仅按 ``project_id`` 匹配可见项目。``project_id`` 传 ``"default"`` 时,
        返回不属于任何可见项目的非置顶普通会话(含命中已隐藏项目的会话、孤立会话),
        这些会话临时归属默认项目,与 ``project.list`` 统计口径一致。
        置顶会话不出现(由 ``project.pinned_sessions`` 获取)。
        定时任务会话(``cron_id`` 非空)不出现(由 ``project.get_cron_sessions`` 获取)。
        """
        if not isinstance(params, dict):
            params = {}
        project_id = str(params.get("project_id") or "").strip()
        if not project_id:
            await channel.send_response(
                ws, req_id, ok=False, error="project_id is required", code="BAD_REQUEST",
            )
            return

        # limit 不传则不限;offset 默认 0
        raw_limit = params.get("limit")
        limit: int | None = None
        if isinstance(raw_limit, int) and not isinstance(raw_limit, bool):
            limit = raw_limit
        elif isinstance(raw_limit, float) and raw_limit.is_integer():
            # JSON 2.0 会被解析为 float,归一为 int;非整数浮点(2.5)落穿到默认(不限)
            limit = int(raw_limit)
        elif isinstance(raw_limit, str) and raw_limit.strip().isdigit():
            limit = int(raw_limit.strip())
        raw_offset = params.get("offset")
        offset = 0
        if isinstance(raw_offset, int) and not isinstance(raw_offset, bool):
            offset = raw_offset
        elif isinstance(raw_offset, float) and raw_offset.is_integer():
            offset = int(raw_offset)
        elif isinstance(raw_offset, str) and raw_offset.strip().isdigit():
            offset = int(raw_offset.strip())
        offset = max(0, offset)
        if limit is not None:
            limit = max(1, limit)

        from jiuwenswarm.server.runtime.session.session_metadata import collect_all_sessions_metadata

        # 可见(非隐藏)项目的 project_id 集合(与 project.list 统计口径一致)
        all_projects = project_store.list_projects(include_hidden=True, cache_bust=True)
        visible_by_id = {p.project_id for p in all_projects if not p.hidden}

        if not is_default_project_id(project_id):
            # 校验目标项目存在且可见
            proj = project_store.get_project_by_id(project_id, cache_bust=True)
            if proj is None or proj.hidden:
                await channel.send_response(
                    ws, req_id, ok=False, error="project not found", code="NOT_FOUND",
                )
                return

        # 归属判断: 仅按 project_id 匹配,不命中归默认
        def _belongs(meta: dict[str, Any]) -> bool:
            return _attribute_session_project(meta, visible_by_id) == project_id

        # 按 web 连接 user_id 读对应用户家目录的会话(隔离前提:user_id == OS 用户名)。
        # user_id 为空时扫 gateway 默认目录(向后兼容)。
        sessions = collect_all_sessions_metadata(user_id=user_id)
        # 仅非置顶普通会话(cron_id 为空) + 归属匹配 + web 渠道
        # cron 会话由 get_cron_sessions 返回
        # user_id 非空时再按 metadata.user_id 收敛(隔离:只看自己的普通会话历史);
        # 旧会话缺 user_id 字段不被过滤(避免历史会话凭空消失)。
        uid_str = str(user_id or "").strip()

        def _is_user_owned(s: dict[str, Any]) -> bool:
            # user_id 非空时按 metadata.user_id 收敛;旧会话缺 user_id 不过滤
            if not uid_str:
                return True
            s_uid = str(s.get("user_id") or "").strip()
            return not s_uid or s_uid == uid_str

        def _is_plain_web_session(s: dict[str, Any]) -> bool:
            return (
                not s.get("pinned")
                and _belongs(s)
                and not s.get("cron_id")
                and s.get("channel_id") == "web"
                and _is_user_owned(s)
            )

        matched = [s for s in sessions if _is_plain_web_session(s)]

        def _lum(s: dict[str, Any]) -> float:
            v = s.get("last_user_message_at")
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0

        matched.sort(key=_lum, reverse=True)

        total = len(matched)
        page = matched[offset: offset + limit] if limit is not None else matched[offset:]

        await channel.send_response(ws, req_id, ok=True, payload={
            "sessions": [_to_session_info(s) for s in page],
            "total": total,
        })

    async def _project_get_cron_sessions(ws, req_id, params, session_id, user_id=None):
        """获取项目下的定时任务会话列表(cron_id 非空的非置顶会话),按 last_user_message_at 倒序。

        与 ``project.get_sessions`` 互斥分工:本接口仅返回 cron 会话,
        ``project.get_sessions`` 仅返回普通会话。支持按 ``cron_id`` 过滤某任务的历史执行会话。
        归属校验同 ``project.get_sessions``:非默认项目(``default`` / ``default_code``
        视为默认)时校验项目存在且可见。
        """
        if not isinstance(params, dict):
            params = {}
        project_id = str(params.get("project_id") or "").strip()
        if not project_id:
            await channel.send_response(
                ws, req_id, ok=False, error="project_id is required", code="BAD_REQUEST",
            )
            return
        cron_id_filter = str(params.get("cron_id") or "").strip()

        # limit 不传则不限;offset 默认 0
        raw_limit = params.get("limit")
        limit: int | None = None
        if isinstance(raw_limit, int) and not isinstance(raw_limit, bool):
            limit = raw_limit
        elif isinstance(raw_limit, float) and raw_limit.is_integer():
            limit = int(raw_limit)
        elif isinstance(raw_limit, str) and raw_limit.strip().isdigit():
            limit = int(raw_limit.strip())
        raw_offset = params.get("offset")
        offset = 0
        if isinstance(raw_offset, int) and not isinstance(raw_offset, bool):
            offset = raw_offset
        elif isinstance(raw_offset, float) and raw_offset.is_integer():
            offset = int(raw_offset)
        elif isinstance(raw_offset, str) and raw_offset.strip().isdigit():
            offset = int(raw_offset.strip())
        offset = max(0, offset)
        if limit is not None:
            limit = max(1, limit)

        from jiuwenswarm.server.runtime.session.session_metadata import collect_all_sessions_metadata

        all_projects = project_store.list_projects(include_hidden=True, cache_bust=True)
        visible_by_id = {p.project_id for p in all_projects if not p.hidden}

        if not is_default_project_id(project_id):
            proj = project_store.get_project_by_id(project_id, cache_bust=True)
            if proj is None or proj.hidden:
                await channel.send_response(
                    ws, req_id, ok=False, error="project not found", code="NOT_FOUND",
                )
                return

        def _belongs(meta: dict[str, Any]) -> bool:
            return _attribute_session_project(meta, visible_by_id) == project_id

        # 按 web 连接 user_id 读对应用户家目录的会话(faas 沙箱按 OS 用户写入;
        # 前提:业务 user_id == OS 用户名)。user_id 为空时扫 gateway 默认目录。
        sessions = collect_all_sessions_metadata(user_id=user_id)
        # 仅非置顶 cron 会话(cron_id 非空) + 归属匹配 + 可选按 cron_id 过滤
        # 注意: cron 会话的 channel_id 通常为 "__cron__"(默认模式)或 job.targets(team 模式),
        # 不固定为 "web",因此不过滤 channel_id,否则 cron 面板会变空。
        # user_id 非空时再按 metadata.user_id 收敛(隔离:只看自己的定时任务历史)。
        uid_str = str(user_id or "").strip()
        matched = []
        for s in sessions:
            if s.get("pinned"):
                continue
            if not _belongs(s):
                continue
            if not s.get("cron_id"):
                continue
            if cron_id_filter and s.get("cron_id") != cron_id_filter:
                continue
            if uid_str and str(s.get("user_id") or "").strip() and str(s.get("user_id") or "").strip() != uid_str:
                continue
            matched.append(s)

        def _lum(s: dict[str, Any]) -> float:
            v = s.get("last_user_message_at")
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0

        matched.sort(key=_lum, reverse=True)

        total = len(matched)
        page = matched[offset: offset + limit] if limit is not None else matched[offset:]

        await channel.send_response(ws, req_id, ok=True, payload={
            "sessions": [_to_session_info(s) for s in page],
            "total": total,
        })

    async def _project_create(ws, req_id, params, session_id):
        """创建项目,指定工作目录。

        ``project_dir`` 为可选:传则指定工作目录绝对路径;不传或空串则在默认工作区
        (``~/.jiuwenswarm/agent/workspace/{work|code}``)下按项目名自动新建文件夹作为工作目录。
        ``work_mode`` 为可选:``"code"`` / ``"work"``,默认按通道推断(Web→work,TUI→code)。
        项目名含文件系统非法字符(``<>:"/\\|?*`` 等)时返回 ``BAD_REQUEST``。
        自动恢复: 若 ``project_dir`` 命中已隐藏(``hidden:true``)**且同 work_mode**的项目,置
        ``hidden:false`` 并按传入 ``name`` 更新展示名,其下会话因 ``project_dir``
        仍匹配自动重新归属。响应 ``restored`` 标识恢复/新建。``project_dir`` 与已有
        **同 work_mode**可见项目重复,或 ``name`` 与已有**同 work_mode**项目(含隐藏)重复时返回 ``CONFLICT``。
        """
        if not isinstance(params, dict):
            params = {}
        name = str(params.get("name") or "").strip()
        if not name:
            await channel.send_response(
                ws, req_id, ok=False, error="name is required", code="BAD_REQUEST",
            )
            return
        project_dir = str(params.get("project_dir") or "").strip()
        # project_dir 非空时必须为绝对路径
        if project_dir and not os.path.isabs(project_dir):
            await channel.send_response(
                ws, req_id,
                ok=False,
                error="project_dir must be an absolute path",
                code="BAD_REQUEST",
            )
            return
        if project_dir and not os.path.isdir(project_dir):
            await channel.send_response(
                ws, req_id,
                ok=False,
                error="project directory does not exist",
                code="PROJECT_DIR_MISSING",
            )
            return
        # 解析 work_mode(严格校验:非法值返回 BAD_REQUEST,不静默回落)
        from jiuwenswarm.server.runtime.session.work_mode import resolve_request_work_mode
        work_mode, mode_error = resolve_request_work_mode(params, channel.channel_id)
        if mode_error is not None:
            await channel.send_response(
                ws, req_id, ok=False,
                error=f"invalid work_mode: {params.get('work_mode')!r}",
                code=mode_error,
            )
            return

        from jiuwenswarm.server.runtime.session.project_store import (
            ProjectDirConflict, ProjectNameConflict,
        )

        # 未传 project_dir 时,在默认工作区下按项目名 + work_mode 自动生成工作目录
        if not project_dir:
            try:
                project_dir = project_store.resolve_default_project_dir(name, work_mode)
            except ValueError as exc:
                await channel.send_response(
                    ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST",
                )
                return
            # 创建文件夹(已存在则复用)
            try:
                os.makedirs(project_dir, exist_ok=True)
            except OSError as exc:
                await channel.send_response(
                    ws, req_id,
                    ok=False,
                    error=f"failed to create project directory: {exc}",
                    code="INTERNAL_ERROR",
                )
                return

        # 原子完成查重/恢复/新建(锁内,无 TOCTOU 窗口):
        # 命中同 work_mode 的隐藏项目 → 恢复;命中同 work_mode 的可见项目 → CONFLICT;
        # 同 work_mode 的 name 重复 → CONFLICT;无匹配 → 新建
        try:
            proj, restored = project_store.create_or_restore_project(name, project_dir, work_mode)
        except ProjectDirConflict:
            await channel.send_response(
                ws, req_id, ok=False, error="project_dir already exists", code="CONFLICT",
            )
            return
        except ProjectNameConflict:
            await channel.send_response(
                ws, req_id, ok=False, error="project name already exists", code="CONFLICT",
            )
            return
        except ValueError as exc:
            await channel.send_response(
                ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST",
            )
            return
        # code 模式新建项目时触发 Git 探测/初始化(设计文档 §6):
        # - work 模式不探测,git.enabled=false / status="disabled"
        # - code 模式:ensure_on_project_create 探测目录,空目录主动 git init,
        #   非空非 Git 目录返回 not_git,已存在 Git 仓库返回 ready
        # - restored 项目(已存在的隐藏项目恢复)不重新探测,保留原 git 快照
        #
        # 设计文档建议"先完成 Git 操作再写入 projects.json",但当前实现采用
        # "先创建项目再探测 Git"的顺序,原因:
        # 1. ensure_on_project_create 需要已持久化的 Project 对象才能写回 git 快照
        # 2. Git 探测失败不阻断项目创建(下方 try/except 兜底),用户可后续安装
        #    Git 后调 project.git.probe 重新探测
        # 3. 不会产生 half-write:Git 异常被捕获,project 记录始终完整(git={} 或 git={...})
        if not restored and proj.work_mode == "code":
            try:
                from jiuwenswarm.server.runtime.session.project_git import (
                    get_project_git_service,
                )
                git_service = get_project_git_service()
                git_service.ensure_on_project_create(proj)
                # ensure_on_project_create 内部已通过 _persist_git_snapshot
                # 写回 Project.git 快照;重新读取 proj 以获取最新 git 字段
                from jiuwenswarm.server.runtime.session.project_store import get_project_by_id
                refreshed = get_project_by_id(proj.project_id, cache_bust=True)
                if refreshed is not None:
                    proj = refreshed
            except Exception as exc:  # noqa: BLE001
                # Git 探测失败不阻断项目创建,仅记日志
                logger.warning(
                    "[Project] git probe failed on project create (id=%s dir=%s): %s",
                    proj.project_id, proj.project_dir, exc,
                )
        project_payload = _project_info_payload(proj)
        _schedule_agent_prewarm_sync("project.create")
        await channel.send_response(ws, req_id, ok=True, payload={
            "project_id": proj.project_id,
            "project_dir": proj.project_dir,
            "restored": restored,
            "work_mode": proj.work_mode or DEFAULT_WEB_WORK_MODE,
            "git": project_payload["git"],
            "project": project_payload,
        })

    async def _project_rename(ws, req_id, params, session_id):
        """重命名项目,仅修改展示名,不改动工作目录路径。

        默认项目禁止重命名(``FORBIDDEN``)。``name`` 与已有项目(含隐藏)重复时返回 ``CONFLICT``。
        ``name`` 含文件系统非法字符 / 为保留设备名时返回 ``BAD_REQUEST``。
        """
        if not isinstance(params, dict):
            params = {}
        project_id = str(params.get("project_id") or "").strip()
        if not project_id:
            await channel.send_response(
                ws, req_id, ok=False, error="project_id is required", code="BAD_REQUEST",
            )
            return
        name = str(params.get("name") or "").strip()
        if not name:
            await channel.send_response(
                ws, req_id, ok=False, error="name is required", code="BAD_REQUEST",
            )
            return

        from jiuwenswarm.server.runtime.session.project_store import ProjectNameConflict

        if is_default_project_id(project_id):
            await channel.send_response(
                ws, req_id, ok=False, error="default project cannot be renamed", code="FORBIDDEN",
            )
            return
        # 原子完成名称冲突检测与写入(锁内,无 TOCTOU 窗口)
        try:
            updated = project_store.rename_project(project_id, name)
        except ProjectNameConflict:
            await channel.send_response(
                ws, req_id, ok=False, error="project name already exists", code="CONFLICT",
            )
            return
        except ValueError as exc:
            await channel.send_response(
                ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST",
            )
            return
        if updated is None:
            await channel.send_response(
                ws, req_id, ok=False, error="project not found", code="NOT_FOUND",
            )
            return
        await channel.send_response(ws, req_id, ok=True, payload={
            "project_id": updated.project_id,
            "name": updated.name,
            "work_mode": updated.work_mode or DEFAULT_WEB_WORK_MODE,
        })

    async def _project_pin(ws, req_id, params, session_id):
        """置顶/取消置顶项目,操作后对所有置顶项目紧凑重编号为 1..N。幂等。

        默认项目禁止置顶(``FORBIDDEN``)。新置顶项目 ``pin_order`` 默认 0,
        重编号后置于置顶区顶部(与 ``session.pin`` 行为一致)。
        """
        if not isinstance(params, dict):
            params = {}
        project_id = str(params.get("project_id") or "").strip()
        if not project_id:
            await channel.send_response(
                ws, req_id, ok=False, error="project_id is required", code="BAD_REQUEST",
            )
            return
        raw_pinned = params.get("pinned")
        if not isinstance(raw_pinned, bool):
            await channel.send_response(
                ws, req_id, ok=False, error="pinned must be boolean", code="BAD_REQUEST",
            )
            return


        if is_default_project_id(project_id):
            await channel.send_response(
                ws, req_id, ok=False, error="default project cannot be pinned", code="FORBIDDEN",
            )
            return
        proj = project_store.get_project_by_id(project_id, cache_bust=True)
        if proj is None or proj.hidden:
            await channel.send_response(
                ws, req_id, ok=False, error="project not found", code="NOT_FOUND",
            )
            return

        # 幂等: 已处于目标状态也视为成功,仍走重编号保证 pin_order 紧凑
        proj.pinned = raw_pinned
        if not raw_pinned:
            proj.pin_order = 0
        project_store.save_project(proj)
        # 紧凑重编号所有置顶项目为 1..N(消除间隙)
        project_store.reindex_project_pin_orders()
        # 重读拿操作后的 pin_order
        updated = project_store.get_project_by_id(project_id, cache_bust=True)
        new_order = updated.pin_order if updated is not None else 0
        await channel.send_response(ws, req_id, ok=True, payload={
            "pinned": raw_pinned,
            "pin_order": new_order,
        })

    async def _project_remove(ws, req_id, params, session_id):
        """移除项目(软删除:``hidden=true``)。其下非置顶会话临时归入默认项目;
        置顶会话不受影响。幂等:已隐藏再移除返回 ``affected_sessions: 0``。

        默认项目禁止移除(``FORBIDDEN``)。
        """
        if not isinstance(params, dict):
            params = {}
        project_id = str(params.get("project_id") or "").strip()
        if not project_id:
            await channel.send_response(
                ws, req_id, ok=False, error="project_id is required", code="BAD_REQUEST",
            )
            return

        from jiuwenswarm.server.runtime.session.session_metadata import collect_all_sessions_metadata

        if is_default_project_id(project_id):
            await channel.send_response(
                ws, req_id, ok=False, error="default project cannot be removed", code="FORBIDDEN",
            )
            return
        proj = project_store.get_project_by_id(project_id, cache_bust=True)
        if proj is None:
            await channel.send_response(
                ws, req_id, ok=False, error="project not found", code="NOT_FOUND",
            )
            return

        # 幂等: 已隐藏再移除视为成功,无会话受影响
        if proj.hidden:
            registry = getattr(channel, "git_watcher_registry", None)
            if registry is not None:
                registry.cleanup_project(project_id)
            await channel.send_response(
                ws, req_id, ok=True, payload={
                    "project_id": project_id,
                    "hidden": True,
                    "affected_sessions": 0,
                },
            )
            return

        # 统计将临时归入默认项目的非置顶会话数(当前归属本项目的非置顶会话;
        # 置顶会话不受影响)。归属口径与 project.list 一致: 仅按 project_id 匹配。
        all_projects = project_store.list_projects(include_hidden=True, cache_bust=True)
        visible_by_id = {p.project_id for p in all_projects if not p.hidden}
        sessions = collect_all_sessions_metadata()
        affected = 0
        for s in sessions:
            if s.get("channel_id") != "web":
                continue
            if not s.get("pinned") and _attribute_session_project(s, visible_by_id) == project_id:
                affected += 1

        # 原子隐藏(锁内完成 hidden 翻转与置顶取消,无 TOCTOU 窗口)
        hidden = project_store.hide_project(project_id)
        if hidden is None:
            registry = getattr(channel, "git_watcher_registry", None)
            if registry is not None:
                registry.cleanup_project(project_id)
            # 竞态: 项目已被其他进程隐藏或删除,视为幂等成功
            await channel.send_response(
                ws, req_id, ok=True, payload={
                    "project_id": project_id,
                    "hidden": True,
                    "affected_sessions": 0,
                },
            )
            return
        # 紧凑重编号(若原为置顶项目,取消后需消除间隙)
        registry = getattr(channel, "git_watcher_registry", None)
        if registry is not None:
            registry.cleanup_project(project_id)
        project_store.reindex_project_pin_orders()
        _schedule_agent_prewarm_sync("project.remove")
        await channel.send_response(
            ws, req_id, ok=True, payload={
                "project_id": project_id,
                "hidden": True,
                "affected_sessions": affected,
            },
        )

    async def _project_restore(ws, req_id, params, session_id):
        """恢复已软删除(``hidden:true``)的项目为可见。其下会话因 ``project_id``
        仍匹配自动重新归属到该项目。

        已是可见的项目返回 ``CONFLICT``(无可恢复内容);恢复后 ``name`` 与已有
        项目(含隐藏)重复时返回 ``CONFLICT``;默认项目禁止恢复(``FORBIDDEN``)。
        """
        if not isinstance(params, dict):
            params = {}
        project_id = str(params.get("project_id") or "").strip()
        if not project_id:
            await channel.send_response(
                ws, req_id, ok=False, error="project_id is required", code="BAD_REQUEST",
            )
            return

        from jiuwenswarm.server.runtime.session.project_store import ProjectNameConflict
        from jiuwenswarm.server.runtime.session.session_metadata import collect_all_sessions_metadata

        if is_default_project_id(project_id):
            await channel.send_response(
                ws, req_id, ok=False, error="default project cannot be restored", code="FORBIDDEN",
            )
            return
        proj = project_store.get_project_by_id(project_id, cache_bust=True)
        if proj is None:
            await channel.send_response(
                ws, req_id, ok=False, error="project not found", code="NOT_FOUND",
            )
            return

        # 已是可见 → 无可恢复内容
        if not proj.hidden:
            await channel.send_response(
                ws, req_id, ok=False, error="project is not hidden", code="CONFLICT",
            )
            return

        # 统计将重新归属到该项目的非置顶会话数(恢复后该项目的会话数)。
        # 把待恢复项目视为可见来计数(恢复后即可见)。与 project.list 口径一致。
        all_projects = project_store.list_projects(include_hidden=True, cache_bust=True)
        # 可见集合: 非隐藏项目 + 待恢复项目自身(恢复后即可见)
        visible_by_id = {
            p.project_id for p in all_projects
            if not p.hidden or p.project_id == project_id
        }
        sessions = collect_all_sessions_metadata()
        affected = 0
        for s in sessions:
            if s.get("channel_id") != "web":
                continue
            if not s.get("pinned") and _attribute_session_project(s, visible_by_id) == project_id:
                affected += 1

        # 原子恢复(锁内完成名称冲突检测与 hidden 翻转,无 TOCTOU 窗口)
        try:
            restored = project_store.restore_project(project_id)
        except ProjectNameConflict:
            await channel.send_response(
                ws, req_id, ok=False, error="project name already exists", code="CONFLICT",
            )
            return
        if restored is None:
            # 竞态: 项目已被其他进程恢复或删除,视为无可恢复内容
            await channel.send_response(
                ws, req_id, ok=False, error="project is not hidden", code="CONFLICT",
            )
            return
        _schedule_agent_prewarm_sync("project.restore")
        await channel.send_response(
            ws, req_id, ok=True, payload={
                "project_id": restored.project_id,
                "restored": True,
                "work_mode": restored.work_mode or DEFAULT_WEB_WORK_MODE,
                "affected_sessions": affected,
            },
        )

    async def _project_info(ws, req_id, params, session_id):
        """获取单个项目详情(含统计),支持虚拟默认项目。

        ``project_id`` 为 ``"default"`` / ``"default_code"`` 时返回对应虚拟默认项目;
        为真实 project_id 时返回该项目的详情。字段与 ``project.list`` 条目一致。

        统计口径同 ``project.list``:仅统计该项目的非置顶普通会话(``cron_id`` 为空)。
        """
        if not isinstance(params, dict):
            params = {}
        project_id = str(params.get("project_id") or "").strip()
        if not project_id:
            await channel.send_response(
                ws, req_id, ok=False, error="project_id is required", code="BAD_REQUEST",
            )
            return

        from jiuwenswarm.server.runtime.session.session_metadata import collect_all_sessions_metadata

        # 可见项目集合(用于会话归属判断)
        all_projects = project_store.list_projects(include_hidden=True, cache_bust=True)
        visible_by_id = {p.project_id for p in all_projects if not p.hidden}

        # 统计非置顶普通会话(同 project.list 口径)
        sessions = collect_all_sessions_metadata()
        session_count = 0
        last_message_at = None
        last_user_message_at = None
        for s in sessions:
            if s.get("channel_id") != "web":
                continue
            if s.get("pinned") or s.get("cron_id"):
                continue
            if _attribute_session_project(s, visible_by_id) == project_id:
                session_count += 1
                lm = s.get("last_message_at")
                if isinstance(lm, (int, float)) and not isinstance(lm, bool):
                    if last_message_at is None or lm > last_message_at:
                        last_message_at = lm
                lum = s.get("last_user_message_at")
                if isinstance(lum, (int, float)) and not isinstance(lum, bool):
                    if last_user_message_at is None or lum > last_user_message_at:
                        last_user_message_at = lum

        if is_default_project_id(project_id):
            # 虚拟默认项目
            info = _project_info_payload(None, default_id=project_id, stats={
                "session_count": session_count,
                "last_message_at": last_message_at,
                "last_user_message_at": last_user_message_at,
            })
            await channel.send_response(ws, req_id, ok=True, payload={"project": info, **info})
            return

        # 真实项目
        include_hidden = bool(params.get("include_hidden"))
        proj = project_store.get_project_by_id(project_id, cache_bust=True)
        if proj is None or (proj.hidden and not include_hidden):
            await channel.send_response(
                ws, req_id, ok=False, error="project not found", code="NOT_FOUND",
            )
            return
        info = _project_info_payload(proj, stats={
            "session_count": session_count,
            "last_message_at": last_message_at,
            "last_user_message_at": last_user_message_at,
        })
        await channel.send_response(ws, req_id, ok=True, payload={"project": info, **info})

    async def _project_pinned_sessions(ws, req_id, params, session_id):
        """获取全部置顶会话,按 ``pin_order`` 升序排列。

        置顶会话已从项目分组中剥离,通过本接口独立获取。``project_dir`` 仍指向
        原归属项目。不接受任何参数。
        """
        from jiuwenswarm.server.runtime.session.session_metadata import collect_all_sessions_metadata

        sessions = collect_all_sessions_metadata()
        pinned = [s for s in sessions if s.get("pinned") and s.get("channel_id") == "web"]
        pinned.sort(key=lambda s: int(s.get("pin_order", 0) or 0))

        await channel.send_response(ws, req_id, ok=True, payload={
            "sessions": [_to_session_info(s) for s in pinned],
        })

    # ── Git RPC handlers (设计文档 §4.1.11-§4.1.15) ──────────────────────────
    #
    # 以下 5 个 handler 共享 ``_resolve_git_project`` 项目校验、
    # ``_build_git_status_payload`` payload 构造与 ``_send_git_error_response``
    # 结构化错误响应(设计文档 §1.4)。``project.git.diff_status`` 由阶段 9 注入。
    #
    # Git 错误响应约定:
    #   - 非业务错误(NOT_FOUND/FORBIDDEN/BAD_REQUEST)payload 保持 ``{}``,
    #     仅顶层 ``error`` + ``code``
    #   - Git 领域错误(GIT_NOT_FOUND/NOT_GIT_REPOSITORY/BRANCH_* 等)在
    #     ``payload.detail`` 写结构化对象,顶层 ``error``/``code`` 与
    #     ``detail.message``/``detail.code`` 保持一致(§5.2.8)
    #   - merge/rebase 中间状态在 ``status``/``probe`` 中不报错(返回
    #     ``repo.transient=true``),仅 ``switch_branch``/``create_branch``
    #     写操作返回 ``GIT_TRANSIENT_STATE``

    def _resolve_git_project(project_id: str, *, cache_bust: bool = False):
        """校验并加载可用于 Git 操作的 code 项目。

        委托给共享 helper ``project_git.resolve_git_project``,
        与 ``git_ws_handler.py`` 的 /ws/git handler 共用同一校验逻辑。

        ``cache_bust=False`` 用于只读操作(status/diff_status),避免每次绕过
        缓存重读磁盘;写操作(probe/init/switch/create)传 ``True`` 确保持有最新
        项目快照。

        Returns:
            ``(project, error_message, error_code)``: 成功时后两项为 None;
            失败时 project 为 None,调用方应直接 send_response。
        """
        from jiuwenswarm.server.runtime.session.project_git import resolve_git_project
        return resolve_git_project(project_id, cache_bust=cache_bust)

    def _build_git_status_payload(proj: Any, repo_status: Any) -> dict[str, Any]:
        """按设计文档 §4.1.11 构造 Git 状态 payload。

        被 ``project.git.status``/``probe``/``init``/``switch_branch``/
        ``create_branch`` 复用,确保字段集合一致。
        """
        return {
            "project_id": proj.project_id,
            "project_name": proj.name,
            "project_dir": proj.project_dir,
            "work_mode": proj.work_mode,
            "repo": {
                "is_git": repo_status.is_git,
                "repo_root": repo_status.repo_root,
                "branch": repo_status.branch,
                "head": repo_status.head,
                "detached": repo_status.detached,
                "transient": repo_status.transient,
                "upstream": repo_status.upstream,
            },
            "working_tree": {
                "is_dirty": repo_status.is_dirty,
                "staged": repo_status.staged,
                "unstaged": repo_status.unstaged,
                "untracked": repo_status.untracked,
                "conflicted": repo_status.conflicted,
            },
            "branches": {
                "current": repo_status.branch,
                "locals": list(repo_status.local_branches),
                "remotes": list(repo_status.remote_branches),
            },
            "generated_at": time.time(),
        }

    async def _send_git_error_response(
        ws: Any, req_id: str, error: Any,
    ) -> None:
        """发送 Git 结构化错误响应(设计文档 §1.4)。

        委托给共享 helper ``project_git.send_git_error_response``。
        ``error`` 可以是 ``GitOperationError`` 异常、``GitError`` 对象或其他异常。
        """
        from jiuwenswarm.server.runtime.session.project_git import send_git_error_response
        await send_git_error_response(channel, ws, req_id, error)

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

    async def _project_git_status(ws, req_id, params, session_id):
        """查询项目 Git 状态(设计文档 §4.1.11)。

        用于状态栏与分支选择器初始化。``merge``/``rebase``/``cherry-pick``
        等中间状态不报错,返回 ``repo.transient=true``,前端据此禁用写操作。
        """
        if not isinstance(params, dict):
            params = {}
        project_id = str(params.get("project_id") or "").strip()
        proj, err, code = _resolve_git_project(project_id)
        if proj is None:
            if code == "NOT_FOUND":
                code = "PROJECT_NOT_FOUND"
            await channel.send_response(
                ws, req_id, ok=False, error=err, code=code, payload={},
            )
            return
        from jiuwenswarm.server.runtime.session.project_git import (
            get_project_git_service,
        )
        service = get_project_git_service()
        try:
            repo_status = await asyncio.to_thread(service.status, proj)
        except Exception as exc:  # noqa: BLE001
            git_error = getattr(exc, "git_error", None)
            if git_error is not None:
                await _send_git_error_response(ws, req_id, git_error)
                return
            logger.warning(
                "[ProjectGit] status failed (project=%s): %s",
                proj.project_id, exc,
            )
            await channel.send_response(
                ws, req_id, ok=False,
                error=f"handler error: {exc}",
                code="INTERNAL_ERROR",
            )
            return
        if repo_status.error is not None:
            await _send_git_error_response(ws, req_id, repo_status.error)
            return
        await channel.send_response(
            ws, req_id, ok=True,
            payload=_build_git_status_payload(proj, repo_status),
        )

    async def _project_git_probe(ws, req_id, params, session_id):
        """重新探测 Git 状态并刷新 ``Project.git`` 快照(设计文档 §4.1.12)。

        不执行 ``git init``;用于外部安装 Git 后刷新、用户手动 init 后刷新、
        用户删除 ``.git`` 后重新探测。探测后调 ``mark_dirty`` 唤醒 /ws/git。
        """
        if not isinstance(params, dict):
            params = {}
        project_id = str(params.get("project_id") or "").strip()
        proj, err, code = _resolve_git_project(project_id, cache_bust=True)
        if proj is None:
            await channel.send_response(
                ws, req_id, ok=False, error=err, code=code, payload={},
            )
            return
        from jiuwenswarm.server.runtime.session.project_git import (
            get_project_git_service,
        )
        service = get_project_git_service()
        try:
            repo_status = await asyncio.to_thread(service.probe, proj)
        except Exception as exc:  # noqa: BLE001
            git_error = getattr(exc, "git_error", None)
            if git_error is not None:
                await _send_git_error_response(ws, req_id, git_error)
                return
            logger.warning(
                "[ProjectGit] probe failed (project=%s): %s",
                proj.project_id, exc,
            )
            await channel.send_response(
                ws, req_id, ok=False,
                error=f"handler error: {exc}",
                code="INTERNAL_ERROR",
            )
            return
        if repo_status.error is not None:
            await _send_git_error_response(ws, req_id, repo_status.error)
            return
        # 探测写回 Project.git 后唤醒 watcher 重算
        _mark_git_watcher_dirty(proj.project_id)
        await channel.send_response(
            ws, req_id, ok=True,
            payload=_build_git_status_payload(proj, repo_status),
        )

    async def _project_git_init(ws, req_id, params, session_id):
        """初始化 Git 仓库(设计文档 §4.1.13)。

        用于非空目录探测后用户确认初始化,或创建时失败后的重试。``initial_branch``
        默认 ``"main"``;成功后调 ``mark_dirty`` 唤醒 /ws/git。
        """
        if not isinstance(params, dict):
            params = {}
        project_id = str(params.get("project_id") or "").strip()
        proj, err, code = _resolve_git_project(project_id, cache_bust=True)
        if proj is None:
            await channel.send_response(
                ws, req_id, ok=False, error=err, code=code, payload={},
            )
            return
        initial_branch = str(params.get("initial_branch") or "main").strip() or "main"
        from jiuwenswarm.server.runtime.session.project_git import (
            get_project_git_service,
        )
        service = get_project_git_service()
        try:
            repo_status = await asyncio.to_thread(
                service.init, proj, initial_branch=initial_branch,
            )
        except Exception as exc:  # noqa: BLE001
            git_error = getattr(exc, "git_error", None)
            if git_error is not None:
                await _send_git_error_response(ws, req_id, git_error)
                return
            logger.warning(
                "[ProjectGit] init failed (project=%s): %s",
                proj.project_id, exc,
            )
            await channel.send_response(
                ws, req_id, ok=False,
                error=f"handler error: {exc}",
                code="INTERNAL_ERROR",
            )
            return
        if repo_status.error is not None:
            await _send_git_error_response(ws, req_id, repo_status.error)
            return
        # git init 让项目从 not_git/disabled 变为可计算 diff 状态,必须唤醒 watcher
        _mark_git_watcher_dirty(proj.project_id)
        await channel.send_response(
            ws, req_id, ok=True,
            payload=_build_git_status_payload(proj, repo_status),
        )

    async def _project_git_switch_branch(ws, req_id, params, session_id):
        """切换 Git 分支(设计文档 §4.1.14)。

        ``require_clean=true`` 时工作区不干净返回 ``WORKTREE_DIRTY``。成功后
        调 ``mark_dirty`` 触发 /ws/git 立即重算。中间状态返回
        ``GIT_TRANSIENT_STATE``。
        """
        if not isinstance(params, dict):
            params = {}
        project_id = str(params.get("project_id") or "").strip()
        proj, err, code = _resolve_git_project(project_id, cache_bust=True)
        if proj is None:
            await channel.send_response(
                ws, req_id, ok=False, error=err, code=code, payload={},
            )
            return
        branch = str(params.get("branch") or "").strip()
        if not branch:
            await channel.send_response(
                ws, req_id, ok=False,
                error="branch is required", code="BAD_REQUEST",
            )
            return
        require_clean = bool(params.get("require_clean") or False)
        from jiuwenswarm.server.runtime.session.project_git import (
            get_project_git_service,
        )
        service = get_project_git_service()
        try:
            op_result = await asyncio.to_thread(
                service.switch_branch, proj, branch, require_clean=require_clean,
            )
        except Exception as exc:  # noqa: BLE001
            git_error = getattr(exc, "git_error", None)
            if git_error is not None:
                await _send_git_error_response(ws, req_id, git_error)
                return
            logger.warning(
                "[ProjectGit] switch_branch failed (project=%s): %s",
                proj.project_id, exc,
            )
            await channel.send_response(
                ws, req_id, ok=False,
                error=f"handler error: {exc}",
                code="INTERNAL_ERROR",
            )
            return
        if not op_result.success:
            await _send_git_error_response(ws, req_id, op_result.error)
            return
        # 写后即时刷新 /ws/git summary
        _mark_git_watcher_dirty(proj.project_id)
        status_payload = _build_git_status_payload(proj, op_result.repo_status)
        await channel.send_response(ws, req_id, ok=True, payload={
            "switched": True,
            "previous_branch": op_result.previous_branch,
            "current_branch": op_result.repo_status.branch,
            "status": status_payload,
        })

    async def _project_git_create_branch(ws, req_id, params, session_id):
        """新建 Git 分支,可选同时切换(设计文档 §4.1.15)。

        ``checkout`` 默认 true;``start_point`` 默认当前 HEAD。成功后调
        ``mark_dirty`` 触发 /ws/git 立即重算。
        """
        if not isinstance(params, dict):
            params = {}
        project_id = str(params.get("project_id") or "").strip()
        proj, err, code = _resolve_git_project(project_id, cache_bust=True)
        if proj is None:
            await channel.send_response(
                ws, req_id, ok=False, error=err, code=code, payload={},
            )
            return
        branch = str(params.get("branch") or "").strip()
        if not branch:
            await channel.send_response(
                ws, req_id, ok=False,
                error="branch is required", code="BAD_REQUEST",
            )
            return
        checkout = bool(params.get("checkout") if "checkout" in params else True)
        start_point = params.get("start_point")
        if start_point is not None:
            start_point = str(start_point).strip() or None
        from jiuwenswarm.server.runtime.session.project_git import (
            get_project_git_service,
        )
        service = get_project_git_service()
        try:
            op_result = await asyncio.to_thread(
                service.create_branch,
                proj, branch, checkout=checkout, start_point=start_point,
            )
        except Exception as exc:  # noqa: BLE001
            git_error = getattr(exc, "git_error", None)
            if git_error is not None:
                await _send_git_error_response(ws, req_id, git_error)
                return
            logger.warning(
                "[ProjectGit] create_branch failed (project=%s): %s",
                proj.project_id, exc,
            )
            await channel.send_response(
                ws, req_id, ok=False,
                error=f"handler error: {exc}",
                code="INTERNAL_ERROR",
            )
            return
        if not op_result.success:
            await _send_git_error_response(ws, req_id, op_result.error)
            return
        _mark_git_watcher_dirty(proj.project_id)
        status_payload = _build_git_status_payload(proj, op_result.repo_status)
        await channel.send_response(ws, req_id, ok=True, payload={
            "created": True,
            "checked_out": bool(checkout),
            "branch": branch,
            "status": status_payload,
        })

    def _strict_bool_param(
        params: dict, key: str, *, default: bool,
    ) -> tuple[bool, str | None]:
        """严格解析布尔参数:只接受 JSON ``true``/``false``。

        字符串 ``"false"``、``"true"``、整数 ``0``/``1`` 等均被拒绝,
        避免前端误传字符串导致 ``bool("false") == True`` 的陷阱。
        对 ``force``/``delete``/``no_verify`` 等高影响参数尤为重要。

        Returns:
            ``(value, error_or_none)``: 参数缺失时返回 ``(default, None)``;
            类型非法时返回 ``(default, error_message)``。
        """
        if key not in params:
            return default, None
        val = params[key]
        # 注意:bool 是 int 的子类,必须先检查 bool 再检查 int
        if isinstance(val, bool):
            return val, None
        return default, (
            f"{key} must be a JSON boolean (true/false), "
            f"got {type(val).__name__}: {val!r}"
        )

    async def _project_git_commit(ws, req_id, params, session_id):
        """提交当前工作区改动到当前分支(设计文档 §4.9 ``project.git.commit``)。

        ``stage_all=true`` 先 ``git add -A`` 暂存全部;``paths`` 显式指定暂存路径
        (与 ``stage_all`` 互斥)。``amend=true`` 覆盖最近一次提交。成功后调
        ``mark_dirty`` 触发 /ws/git 立即重算(HEAD/is_dirty/stats 变化)。
        """
        if not isinstance(params, dict):
            params = {}
        project_id = str(params.get("project_id") or "").strip()
        proj, err, code = _resolve_git_project(project_id, cache_bust=True)
        if proj is None:
            await channel.send_response(
                ws, req_id, ok=False, error=err, code=code, payload={},
            )
            return
        message = params.get("message")
        if message is None:
            await channel.send_response(
                ws, req_id, ok=False,
                error="message is required", code="BAD_REQUEST",
            )
            return
        if not isinstance(message, str):
            await channel.send_response(
                ws, req_id, ok=False,
                error=(
                    f"message must be a string, "
                    f"got {type(message).__name__}: {message!r}"
                ),
                code="BAD_REQUEST",
            )
            return
        if not message.strip():
            await channel.send_response(
                ws, req_id, ok=False,
                error="message must not be empty", code="BAD_REQUEST",
            )
            return
        paths_param = params.get("paths")
        paths: list[str] | None = None
        if paths_param is not None:
            if not isinstance(paths_param, list):
                await channel.send_response(
                    ws, req_id, ok=False,
                    error="paths must be an array of strings", code="BAD_REQUEST",
                )
                return
            # 严格校验:任一元素非字符串或空白即拒绝,而非静默过滤。
            # 否则 ["a.py", 123] 会只提交 a.py,调用方以为两个路径都处理了。
            paths = []
            for idx, p in enumerate(paths_param):
                if not isinstance(p, str):
                    await channel.send_response(
                        ws, req_id, ok=False,
                        error=(
                            f"paths[{idx}] must be a string, "
                            f"got {type(p).__name__}: {p!r}"
                        ),
                        code="BAD_REQUEST",
                    )
                    return
                if not p.strip():
                    await channel.send_response(
                        ws, req_id, ok=False,
                        error=f"paths[{idx}] must not be empty or whitespace",
                        code="BAD_REQUEST",
                    )
                    return
                paths.append(p)
            # 显式传了空数组:调用方意图是只提交指定路径,但没有路径——拒绝。
            if not paths:
                await channel.send_response(
                    ws, req_id, ok=False,
                    error="paths is empty",
                    code="BAD_REQUEST",
                )
                return
        # amend 需在 stage_all 之前解析:amend=true 时 stage_all 默认 False,
        # 避免"只改 commit message 的 amend"意外把所有未暂存改动塞进上一条提交。
        amend, sb_err = _strict_bool_param(params, "amend", default=False)
        if sb_err:
            await channel.send_response(
                ws, req_id, ok=False, error=sb_err, code="BAD_REQUEST",
            )
            return
        # stage_all 默认值推导:
        #   - 未传 paths 且非 amend → True(常规提交,暂存全部)
        #   - 传了 paths → False(只暂存指定路径)
        #   - amend=true → False(除非显式传 stage_all=true,否则不自动暂存)
        stage_all_default = (paths_param is None) and not amend
        stage_all, sb_err = _strict_bool_param(
            params, "stage_all", default=stage_all_default,
        )
        if sb_err:
            await channel.send_response(
                ws, req_id, ok=False, error=sb_err, code="BAD_REQUEST",
            )
            return
        if stage_all and paths:
            await channel.send_response(
                ws, req_id, ok=False,
                error="stage_all and paths are mutually exclusive",
                code="BAD_REQUEST",
            )
            return
        no_verify, sb_err = _strict_bool_param(params, "no_verify", default=False)
        if sb_err:
            await channel.send_response(
                ws, req_id, ok=False, error=sb_err, code="BAD_REQUEST",
            )
            return
        from jiuwenswarm.server.runtime.session.project_git import (
            get_project_git_service,
        )
        service = get_project_git_service()
        try:
            op_result = await asyncio.to_thread(
                service.commit,
                proj, message,
                stage_all=stage_all,
                paths=paths,
                amend=amend,
                no_verify=no_verify,
            )
        except Exception as exc:  # noqa: BLE001
            git_error = getattr(exc, "git_error", None)
            if git_error is not None:
                await _send_git_error_response(ws, req_id, git_error)
                return
            logger.warning(
                "[ProjectGit] commit failed (project=%s): %s",
                proj.project_id, exc,
            )
            await channel.send_response(
                ws, req_id, ok=False,
                error=f"handler error: {exc}",
                code="INTERNAL_ERROR",
            )
            return
        if not op_result.success:
            # 失败路径也唤醒 watcher:staging 可能已改变 index(如 git add 成功
            # 但 commit 失败),watcher 重算后通过 /ws/git 推送最新 staged/dirty
            # 状态,避免前端 UI 短时间失真。service 层返回的 repo_status 已反映
            # 暂存后的真实状态(非 pre_status)。
            _mark_git_watcher_dirty(proj.project_id)
            await _send_git_error_response(ws, req_id, op_result.error)
            return
        # 写后即时刷新 /ws/git summary(HEAD/is_dirty/stats 变化)
        _mark_git_watcher_dirty(proj.project_id)
        status_payload = _build_git_status_payload(proj, op_result.repo_status)
        await channel.send_response(ws, req_id, ok=True, payload={
            "committed": True,
            "commit_hash": op_result.commit_hash,
            "amended": bool(amend),
            "status": status_payload,
        })

    async def _project_git_push(ws, req_id, params, session_id):
        """推送本地分支到远程(设计文档 §4.10 ``project.git.push``)。

        ``remote`` 默认 ``origin``;``branch`` 不传时用当前分支(detached HEAD
        须显式传 ``branch``)。``force=true`` 使用 ``--force-with-lease``(更安全)。
        ``delete=true`` 删除远程分支(与 ``set_upstream``/``force`` 互斥)。push
        涉及网络,超时独立配置(``GIT_PUSH_TIMEOUT_SEC``,默认 60s)。成功后调
        ``mark_dirty`` 刷新 /ws/git(主要更新 upstream 字段)。
        """
        if not isinstance(params, dict):
            params = {}
        project_id = str(params.get("project_id") or "").strip()
        proj, err, code = _resolve_git_project(project_id, cache_bust=True)
        if proj is None:
            await channel.send_response(
                ws, req_id, ok=False, error=err, code=code, payload={},
            )
            return
        remote = str(params.get("remote") or "origin").strip() or "origin"
        branch_param = params.get("branch")
        branch: str | None = None
        if branch_param is not None:
            if not isinstance(branch_param, str):
                await channel.send_response(
                    ws, req_id, ok=False,
                    error=(
                        f"branch must be a string, "
                        f"got {type(branch_param).__name__}: {branch_param!r}"
                    ),
                    code="BAD_REQUEST",
                )
                return
            branch = branch_param.strip() or None
        set_upstream, sb_err = _strict_bool_param(params, "set_upstream", default=False)
        if sb_err:
            await channel.send_response(
                ws, req_id, ok=False, error=sb_err, code="BAD_REQUEST",
            )
            return
        force, sb_err = _strict_bool_param(params, "force", default=False)
        if sb_err:
            await channel.send_response(
                ws, req_id, ok=False, error=sb_err, code="BAD_REQUEST",
            )
            return
        delete, sb_err = _strict_bool_param(params, "delete", default=False)
        if sb_err:
            await channel.send_response(
                ws, req_id, ok=False, error=sb_err, code="BAD_REQUEST",
            )
            return
        if delete and (set_upstream or force):
            await channel.send_response(
                ws, req_id, ok=False,
                error="delete is mutually exclusive with set_upstream and force",
                code="BAD_REQUEST",
            )
            return
        from jiuwenswarm.server.runtime.session.project_git import (
            get_project_git_service,
        )
        service = get_project_git_service()
        try:
            op_result = await asyncio.to_thread(
                service.push,
                proj,
                remote=remote,
                branch=branch,
                set_upstream=set_upstream,
                force=force,
                delete=delete,
            )
        except Exception as exc:  # noqa: BLE001
            git_error = getattr(exc, "git_error", None)
            if git_error is not None:
                await _send_git_error_response(ws, req_id, git_error)
                return
            logger.warning(
                "[ProjectGit] push failed (project=%s): %s",
                proj.project_id, exc,
            )
            await channel.send_response(
                ws, req_id, ok=False,
                error=f"handler error: {exc}",
                code="INTERNAL_ERROR",
            )
            return
        if not op_result.success:
            await _send_git_error_response(ws, req_id, op_result.error)
            return
        # push 主要影响 upstream 字段;调 mark_dirty 让 watcher 重算并推送
        _mark_git_watcher_dirty(proj.project_id)
        status_payload = _build_git_status_payload(proj, op_result.repo_status)
        await channel.send_response(ws, req_id, ok=True, payload={
            "pushed": True,
            "remote": op_result.pushed_remote or remote,
            "branch": branch or op_result.repo_status.branch,
            "deleted": bool(delete),
            "upstream_set": bool(set_upstream),
            "status": status_payload,
        })

    async def _project_git_diff_status(ws, req_id, params, session_id):
        """拉取当前分支 diff 和上一轮对话 diff 的快照(设计文档 §4.1.16)。

        用于首次加载、手动刷新、断线重连。实时监控不依赖此接口轮询,
        而是通过 /ws/git 的 ``diff_watch`` 订阅。

        ``include_files=true`` 返回文件列表;``include_hunks=true`` 隐含
        ``include_files=true`` 并返回 hunk。transient 状态下 ``current``
        为 ``null``,仍成功返回 ``repo.transient=true``。
        """
        if not isinstance(params, dict):
            params = {}
        project_id = str(params.get("project_id") or "").strip()
        proj, err, code = _resolve_git_project(project_id)
        if proj is None:
            if code == "NOT_FOUND":
                code = "PROJECT_NOT_FOUND"
            await channel.send_response(
                ws, req_id, ok=False, error=err, code=code, payload={},
            )
            return
        session_id_param = params.get("session_id")
        if session_id_param is not None:
            session_id_param = str(session_id_param).strip() or None
        include_files = bool(params.get("include_files") or False) or bool(params.get("include_hunks") or False)
        include_hunks = bool(params.get("include_hunks") or False)
        from jiuwenswarm.server.runtime.session.git_diff_status import (
            get_diff_status_service,
        )
        service = get_diff_status_service()
        try:
            status = await asyncio.to_thread(
                service.get_project_diff_status,
                project=proj,
                session_id=session_id_param,
                include_files=include_files,
                include_hunks=include_hunks,
            )
        except Exception as exc:  # noqa: BLE001
            git_error = getattr(exc, "git_error", None)
            if git_error is not None:
                await _send_git_error_response(ws, req_id, git_error)
                return
            logger.warning(
                "[ProjectGit] diff_status failed (project=%s): %s",
                proj.project_id, exc,
            )
            await channel.send_response(
                ws, req_id, ok=False,
                error=f"handler error: {exc}",
                code="INTERNAL_ERROR",
            )
            return
        await channel.send_response(
            ws, req_id, ok=True,
            payload=status.to_dict(include_hunks=include_hunks),
        )

    async def _validate_session_project_binding(
        ws: Any, req_id: str, project_id: str, session_id: str,
    ) -> tuple[bool, str | None, str | None]:
        """校验 session 与 project 的绑定关系。"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )
        try:
            session_meta = await asyncio.to_thread(
                get_session_metadata, session_id,
                cache_bust=True, enable_writeback=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[ProjectGit] failed to read session metadata (session=%s): %s",
                session_id, exc,
            )
            return False, f"failed to read session metadata: {exc}", "INTERNAL_ERROR"
        if not session_meta:
            return False, f"session not found: {session_id}", "SESSION_NOT_FOUND"
        session_project_id = str(session_meta.get("project_id") or "").strip()
        if not session_project_id:
            return False, (
                "session has no project_id binding; cannot verify project ownership"
            ), "SESSION_NOT_BOUND"
        if session_project_id != project_id:
            return False, (
                f"session_id does not belong to project_id: "
                f"expected {project_id}, got {session_project_id}"
            ), "PROJECT_SESSION_MISMATCH"
        return True, None, None

    async def _project_git_turn_diff_list(ws, req_id, params, session_id):
        """查询历史 Diff 摘要列表。"""
        if not isinstance(params, dict):
            params = {}
        project_id = str(params.get("project_id") or "").strip()
        proj, err, code = _resolve_git_project(project_id)
        if proj is None:
            await channel.send_response(
                ws, req_id, ok=False, error=err, code=code, payload={},
            )
            return
        session_id_param = str(params.get("session_id") or "").strip()
        if not session_id_param:
            await channel.send_response(
                ws, req_id, ok=False,
                error="session_id is required", code="BAD_REQUEST",
            )
            return
        ok_val, v_err, v_code = await _validate_session_project_binding(
            ws, req_id, project_id, session_id_param,
        )
        if not ok_val:
            await channel.send_response(
                ws, req_id, ok=False, error=v_err, code=v_code, payload={},
            )
            return
        limit_raw = params.get("limit")
        if limit_raw is None:
            limit = 50
        else:
            try:
                limit = int(limit_raw)
            except (ValueError, TypeError):
                limit = 50
            if limit < 0:
                limit = 50
        cursor_raw = params.get("cursor", 0)
        try:
            cursor = int(cursor_raw)
        except (ValueError, TypeError):
            cursor = 0
        if cursor < 0:
            cursor = 0
        from jiuwenswarm.server.runtime.session.git_diff_status import (
            get_diff_status_service,
        )
        service = get_diff_status_service()
        try:
            result = await asyncio.to_thread(
                service.get_turn_diff_list,
                project=proj,
                session_id=session_id_param,
                limit=limit,
                cursor=cursor,
            )
        except Exception as exc:  # noqa: BLE001
            git_error = getattr(exc, "git_error", None)
            if git_error is not None:
                await _send_git_error_response(ws, req_id, git_error)
                return
            logger.warning(
                "[ProjectGit] turn_diff_list failed (project=%s): %s",
                proj.project_id, exc,
            )
            await channel.send_response(
                ws, req_id, ok=False,
                error=f"handler error: {exc}", code="INTERNAL_ERROR",
            )
            return
        await channel.send_response(ws, req_id, ok=True, payload=result)

    async def _project_git_turn_diff(ws, req_id, params, session_id):
        """查询指定轮次 Diff 详情。"""
        if not isinstance(params, dict):
            params = {}
        project_id = str(params.get("project_id") or "").strip()
        proj, err, code = _resolve_git_project(project_id)
        if proj is None:
            await channel.send_response(
                ws, req_id, ok=False, error=err, code=code, payload={},
            )
            return
        session_id_param = str(params.get("session_id") or "").strip()
        if not session_id_param:
            await channel.send_response(
                ws, req_id, ok=False,
                error="session_id is required", code="BAD_REQUEST",
            )
            return
        change_set_id = str(params.get("change_set_id") or "").strip() or None
        turn_index: int | None = None
        if change_set_id is None:
            turn_index_raw = params.get("turn_index")
            try:
                turn_index = int(turn_index_raw)
            except (ValueError, TypeError):
                await channel.send_response(
                    ws, req_id, ok=False,
                    error="either change_set_id or turn_index (integer) is required",
                    code="BAD_REQUEST",
                )
                return

        def _bool_param(name: str, default: bool) -> bool:
            raw = params.get(name, default)
            if isinstance(raw, bool):
                return raw
            if raw is None:
                return default
            if isinstance(raw, (int, float)):
                return bool(raw)
            raw_str = str(raw).strip().lower()
            if raw_str in {"1", "true", "yes", "on"}:
                return True
            if raw_str in {"0", "false", "no", "off"}:
                return False
            return default

        include_hunks = _bool_param("include_hunks", True)
        include_files = _bool_param("include_files", True) or include_hunks
        ok_val, v_err, v_code = await _validate_session_project_binding(
            ws, req_id, project_id, session_id_param,
        )
        if not ok_val:
            await channel.send_response(
                ws, req_id, ok=False, error=v_err, code=v_code, payload={},
            )
            return
        from jiuwenswarm.server.runtime.session.git_diff_status import (
            get_diff_status_service,
        )
        from jiuwenswarm.server.utils.diff_service import DiffHistoryExpiredError
        service = get_diff_status_service()
        try:
            result = await asyncio.to_thread(
                service.get_turn_diff_detail,
                project=proj,
                session_id=session_id_param,
                turn_index=turn_index,
                change_set_id=change_set_id,
                include_files=include_files,
                include_hunks=include_hunks,
            )
        except DiffHistoryExpiredError as exc:
            await channel.send_response(
                ws, req_id, ok=False,
                error=str(exc) or "diff history expired",
                code="DIFF_HISTORY_EXPIRED",
            )
            return
        except Exception as exc:  # noqa: BLE001
            git_error = getattr(exc, "git_error", None)
            if git_error is not None:
                await _send_git_error_response(ws, req_id, git_error)
                return
            logger.warning(
                "[ProjectGit] turn_diff failed (project=%s): %s",
                proj.project_id, exc,
            )
            await channel.send_response(
                ws, req_id, ok=False,
                error=f"handler error: {exc}", code="INTERNAL_ERROR",
            )
            return
        if result is None:
            identifier = change_set_id or f"turn_index={turn_index}"
            not_found_code = "CHANGE_SET_NOT_FOUND" if change_set_id else "TURN_DIFF_NOT_FOUND"
            await channel.send_response(
                ws, req_id, ok=False,
                error=f"turn diff not found for {identifier}",
                code=not_found_code,
            )
            return
        await channel.send_response(ws, req_id, ok=True, payload=result)

    async def _path_get(ws, req_id, params, session_id):
        """读 browser.chrome_path / browser_type 并返回给前端（会解析环境变量）。"""
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
            ws,
            req_id,
            ok=True,
            payload={
                "chrome_path": chrome_path,
                "browser_type": browser_type,
                "headless": headless,
            },
        )

    async def _path_set(ws, req_id, params, session_id):
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
            update_browser_in_config(
                {
                    "chrome_path": chrome_path,
                    "browser_type": browser_type,
                    "headless": headless,
                }
            )
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
            ws,
            req_id,
            ok=True,
            payload={
                "chrome_path": chrome_path,
                "browser_type": browser_type,
                "headless": headless,
            },
        )

    async def _path_select_directory(ws, req_id, params, session_id):
        """在服务端本机弹出系统文件夹对话框（浏览器 / whl 包回退方案）。

        桌面端 pywebview 已提供 ``select_project_directory``；本方法覆盖无该桥接时的
        纯浏览器访问场景。

        - Windows：tkinter
        - macOS：osascript；
        - Linux：zenity/kdialog/yad/tkinter
        - 不可用时返回 ``UNSUPPORTED``，前端回落到手填绝对路径
        """
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
            logger.warning("[path.select_directory] picker unavailable: %s", exc)
            await channel.send_response(
                ws, req_id, ok=False, error=str(exc), code="UNSUPPORTED",
            )
            return

        if not selected:
            await channel.send_response(ws, req_id, ok=True, payload={"path": None, "cancelled": True})
            return
        await channel.send_response(ws, req_id, ok=True, payload={"path": selected, "cancelled": False})

    async def _path_select_files(ws, req_id, params, session_id):
        """在服务端本机弹出系统文件对话框（浏览器 / whl 包回退方案）。

        桌面端 pywebview 已提供 ``select_local_files``；本方法覆盖无该桥接时的
        纯浏览器访问场景，返回带绝对路径的文件元数据（与桌面端同形）。
        """
        if not isinstance(params, dict):
            params = {}
        initial_dir = params.get("initial_dir")
        if initial_dir is not None and not isinstance(initial_dir, str):
            await channel.send_response(
                ws, req_id, ok=False, error="initial_dir must be string", code="BAD_REQUEST",
            )
            return
        allow_multiple = params.get("allow_multiple", True)
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
            logger.warning("[path.select_files] picker unavailable: %s", exc)
            await channel.send_response(
                ws, req_id, ok=False, error=str(exc), code="UNSUPPORTED",
            )
            return

        if selected is None:
            await channel.send_response(
                ws, req_id, ok=True, payload={"files": [], "cancelled": True},
            )
            return
        await channel.send_response(
            ws, req_id, ok=True, payload={"files": selected, "cancelled": False},
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

    async def _chat_send(ws, req_id, params, session_id):
        await channel.send_response(
            ws,
            req_id,
            ok=True,
            payload={"accepted": True, "session_id": session_id},
        )

    async def _media_persist(ws, req_id, params, session_id):
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        normalized = dict(params)
        try:
            normalize_chat_media_attachments(normalized, session_id)
        except Exception as exc:
            logger.exception("[media.persist] failed: %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")
            return
        # G.EXP.04: 多 key + 条件过滤不用推导式，改为显式循环。
        payload = {}
        for key in ("content", "query", "media_items", "files"):
            if key in normalized:
                payload[key] = normalized[key]
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _document_persist(ws, req_id, params, session_id):
        """Validate local document paths (blacklist); do not persist or parse content."""
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        normalized = dict(params)
        try:
            await persist_and_parse_documents(normalized)
        except Exception as exc:
            logger.exception("[document.persist] failed: %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")
            return
        # G.EXP.04: 多 key + 条件过滤不用推导式，改为显式循环。
        payload = {}
        for key in (
            "content",
            "query",
            "media_items",
            "files",
            "documents",
            "document_errors",
            "forbidden_formats",
        ):
            if key in normalized:
                payload[key] = normalized[key]
        if "forbidden_formats" not in payload:
            payload["forbidden_formats"] = forbidden_formats()
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _document_formats(ws, req_id, params, session_id):
        await channel.send_response(
            ws,
            req_id,
            ok=True,
            payload={"forbidden_formats": forbidden_formats()},
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

    async def _locale_get_conf(ws, req_id, params, session_id):
        """返回当前 preferred_language 配置（zh / en）。"""
        try:
            cfg = get_config()
            lang = str(cfg.get("preferred_language") or "zh").strip().lower()
            if lang not in ("zh", "en"):
                lang = "zh"
            await channel.send_response(
                ws,
                req_id,
                ok=True,
                payload={"preferred_language": lang}
            )
        except Exception as e:
            logger.exception("[locale.get_conf] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _locale_set_conf(ws, req_id, params, session_id):
        """更新 preferred_language 并写回 config.yaml。"""
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
        try:
            update_preferred_language_in_config(lang)
            await channel.send_response(ws, req_id, ok=True, payload={"preferred_language": lang})
        except Exception as e:
            logger.warning("[locale.set_conf] 写回 config.yaml 失败: %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _heartbeat_get_conf(ws, req_id, params, session_id):
        """返回当前心跳配置（every / target / active_hours）。"""
        hb = _resolve(heartbeat_service)
        if hb is None:
            await channel.send_response(ws, req_id, ok=False, error="heartbeat service not available",
                                        code="SERVICE_UNAVAILABLE")
            return
        try:
            payload = dict(hb.get_heartbeat_conf())
            await channel.send_response(ws, req_id, ok=True, payload=payload)
        except Exception as e:
            logger.exception("[heartbeat.get_conf] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _heartbeat_set_conf(ws, req_id, params, session_id):
        """更新心跳配置并重启心跳服务；params 可含 every、target、active_hours。"""
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
                    logger.debug("[heartbeat.set_conf] 飞书目标检测异常: %s", e)
                    await channel.send_response(
                        ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR",
                    )
                    return

            # 检查通过后再保存配置
            await hb.set_heartbeat_conf(every=every, target=target, active_hours=active_hours)
            payload = dict(hb.get_heartbeat_conf())
            should_clear_agent_config_cache = False
            try:
                update_heartbeat_in_config(payload)
                should_clear_agent_config_cache = True
            except Exception as e:  # noqa: BLE001
                logger.warning("[heartbeat.set_conf] 写回 config.yaml 失败: %s", e)
            try:
                await channel.send_response(ws, req_id, ok=True, payload=payload)
            finally:
                if should_clear_agent_config_cache:
                    _schedule_clear_agent_config_cache("heartbeat.set_conf")
        except ValueError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="BAD_REQUEST")
        except Exception as e:
            logger.exception("[heartbeat.set_conf] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _heartbeat_get_path(ws, req_id, params, session_id):
        """返回 HEARTBEAT.md 文件路径。"""
        from jiuwenswarm.common.utils import get_deepagent_heartbeat_path, get_agent_root_dir

        try:
            heartbeat_path = get_deepagent_heartbeat_path()
            # 返回相对于 agent 根目录的路径，与 file-api 格式一致
            agent_root = get_agent_root_dir()
            relative_path = heartbeat_path.relative_to(agent_root.parent)
            await channel.send_response(
                ws, req_id, ok=True,
                payload={"path": str(relative_path)}
            )
        except Exception as e:
            logger.exception("[heartbeat.get_path] %s", e)
            await channel.send_response(
                ws, req_id, ok=False,
                error=str(e), code="INTERNAL_ERROR"
            )

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
            # 若 YAML 里 bot_token 本就为空，仅删凭据文件时 dict 与上次相同，_should_restart_channel 不会重启，扫码 UI 会一直停在 idle
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

    async def _cron_job_get(ws, req_id, params, session_id):
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
        job = await cc.get_job(job_id)
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
            if session_id:
                params["session_id"] = session_id
            # 注入创建者 user_id（web 连接携带），执行时透传给 faas 的 X-Session-Context，
            # 否则 CreateSandbox 拉不起 → 60s 超时（见 plan-cron-user-id）。
            if user_id:
                params["user_id"] = str(user_id).strip()
            # project_dir 默认值：仅当前端「未传」时从当前 WebSocket 会话 metadata 读取
            # （cache_bust=True 强制读盘，跨进程拿最新值；见设计文档 §5.1）
            # 注意：显式传空串 "" 等价于归默认项目，不可覆盖——用 key presence 区分
            if "project_dir" not in params and session_id:
                try:
                    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata
                    meta = get_session_metadata(session_id, cache_bust=True)
                    if isinstance(meta, dict):
                        pd = meta.get("project_dir")
                        if isinstance(pd, str) and pd.strip():
                            params["project_dir"] = pd.strip()
                except Exception:  # noqa: BLE001
                    pass
            job = await cc.create_job(params)
            await channel.send_response(ws, req_id, ok=True, payload={"job": job})
        except Exception as e:  # noqa: BLE001
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="BAD_REQUEST")

    async def _cron_job_update(ws, req_id, params, session_id):
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
            job = await cc.update_job(job_id, patch)
            await channel.send_response(ws, req_id, ok=True, payload={"job": job})
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
        except Exception as e:  # noqa: BLE001
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="BAD_REQUEST")

    async def _cron_job_delete(ws, req_id, params, session_id):
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
        existing = await cc.get_job(job_id)
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

    async def _cron_job_toggle(ws, req_id, params, session_id):
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
        existing = await cc.get_job(job_id)
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

    async def _cron_job_preview(ws, req_id, params, session_id):
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
            next_runs = await cc.preview_job(job_id, int(count) if count is not None else 5)
            await channel.send_response(ws, req_id, ok=True, payload={"next": next_runs})
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
        except Exception as e:  # noqa: BLE001
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="BAD_REQUEST")

    async def _cron_job_run_now(ws, req_id, params, session_id):
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
            job = await cc.get_job(job_id)
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

    channel.register_method("config.get", _config_get)
    channel.register_method("config.set", _config_set)
    channel.register_method("config.save_all", _config_save_all)
    channel.register_method("config.validate_model", _config_validate_model)
    channel.register_method("external_cli.detect", _external_cli_detect)
    channel.register_method("external_cli.install_status", _external_cli_install_status)
    channel.register_method("models.list", _models_list)
    channel.register_method("models.replace_all", _models_replace_all)
    channel.register_method("models.validate", _models_validate)
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

    async def _hooks_list(ws, req_id, params, session_id):
        from jiuwenswarm.common.hooks_config import load_hooks_config
        try:
            hooks_config = load_hooks_config(get_config())
            summary = hooks_config.get_event_summary()
            await channel.send_response(ws, req_id, ok=True,
                                        payload={
                                            "events": summary,
                                            "disable_all_hooks": hooks_config.disable_all_hooks,
                                            "source": "config.yaml",
                                        })
        except Exception as e:
            await channel.send_response(ws, req_id, ok=False,
                                        error=str(e), code="INTERNAL_ERROR")

    channel.register_method("memory.compute", _memory_compute)
    channel.register_method("hooks.list", _hooks_list)

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
    channel.register_method("heartbeat.get_conf", _heartbeat_get_conf)
    channel.register_method("heartbeat.set_conf", _heartbeat_set_conf)
    channel.register_method("heartbeat.get_path", _heartbeat_get_path)
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

    # 数字分身 — permissions.owner_scopes：仅 Web 网关直连 config（不经 E2A / config_rpc）。
    # 其余 permissions.*（tools / rules / approval_overrides）走 _forward_permissions_to_agent。

    async def _permissions_owner_scopes_get(ws, req_id, params, session_id):
        from jiuwenswarm.common.config import get_permissions_owner_scopes

        try:
            payload = get_permissions_owner_scopes()
            await channel.send_response(ws, req_id, ok=True, payload=payload)
        except Exception as e:
            logger.exception("[permissions.owner_scopes.get] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _permissions_owner_scopes_set(ws, req_id, params, session_id):
        from jiuwenswarm.common.config import update_permissions_owner_scopes_in_config

        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
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

    async def _forward_permissions_to_agent(ws, req_id, params, session_id, *, req_method):
        """permissions.*：优先经 E2A 转发到 AgentServer；Agent 未就绪时本地执行（与 config_rpc 同源）。"""
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenswarm.common.schema.agent import AgentRequest

        if not isinstance(req_method, ReqMethod):
            await channel.send_response(ws, req_id, ok=False, error="invalid req_method", code="INTERNAL_ERROR")
            return

        synthetic = AgentRequest(
            request_id=str(req_id) if req_id else "",
            channel_id="",
            session_id=session_id,
            req_method=req_method,
            params=dict(params) if isinstance(params, dict) else {},
        )

        ac = _resolve(agent_client)
        if ac is None or not getattr(ac, "server_ready", False):
            from jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc import \
                dispatch_permissions_config_request

            resp = dispatch_permissions_config_request(synthetic)
            if not resp.ok:
                pl = resp.payload if isinstance(resp.payload, dict) else {}
                await channel.send_response(
                    ws,
                    req_id,
                    ok=False,
                    error=str(pl.get("error") or "request failed"),
                    code=str(pl.get("code") or "BAD_REQUEST"),
                )
                return
            out = resp.payload if isinstance(resp.payload, dict) else {}
            should_schedule_reload = req_method not in (
                ReqMethod.PERMISSIONS_TOOLS_GET,
                ReqMethod.PERMISSIONS_RULES_GET,
                ReqMethod.PERMISSIONS_APPROVAL_OVERRIDES_GET,
            )
            if should_schedule_reload:
                out = {
                    **out,
                    "applied_without_restart": await _apply_config_change_set(
                        _ConfigChangeSet({}, ["permissions"], force=True)
                    ),
                }
            await channel.send_response(ws, req_id, ok=True, payload=out)
            return

        env = e2a_from_agent_fields(
            request_id=str(req_id) if req_id else "",
            channel_id="",
            session_id=session_id,
            req_method=req_method,
            params=dict(params) if isinstance(params, dict) else {},
        )
        try:
            resp = await ac.send_request(env)
        except Exception as e:
            logger.exception("[permissions] forward to agent failed: %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")
            return
        if not resp.ok:
            pl = resp.payload if isinstance(resp.payload, dict) else {}
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(pl.get("error") or "request failed"),
                code=str(pl.get("code") or "BAD_REQUEST"),
            )
            return
        out = resp.payload if isinstance(resp.payload, dict) else {}
        await channel.send_response(ws, req_id, ok=True, payload=out)

    async def _forward_mcp_to_agent(ws, req_id, params, session_id, *, req_method):
        """mcp.* : 经 E2A 转发到 AgentServer。"""
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields

        if not isinstance(req_method, ReqMethod):
            await channel.send_response(ws, req_id, ok=False, error="invalid req_method", code="INTERNAL_ERROR")
            return

        ac = _resolve(agent_client)
        if ac is None or not getattr(ac, "server_ready", False):
            await channel.send_response(ws, req_id, ok=False, error="AgentServer not ready", code="AGENT_NOT_READY")
            return

        env = e2a_from_agent_fields(
            request_id=str(req_id) if req_id else "",
            channel_id="",
            session_id=session_id,
            req_method=req_method,
            params=dict(params) if isinstance(params, dict) else {},
        )
        try:
            resp = await ac.send_request(env)
        except Exception as e:
            logger.exception("[mcp] forward to agent failed: %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")
            return
        if not resp.ok:
            pl = resp.payload if isinstance(resp.payload, dict) else {}
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(pl.get("error") or "request failed"),
                code=str(pl.get("code") or "BAD_REQUEST"),
            )
            return
        out = resp.payload if isinstance(resp.payload, dict) else {}
        await channel.send_response(ws, req_id, ok=True, payload=out)

    async def _mcp_list_local(ws, req_id, params, session_id):
        # 本地处理(不转发 AgentServer)，避免被长连接 connect 阻塞。直接读 marketplace。
        # filter: builtin(预置目录) | local(已连接预置 + 全部自定义)。兜底 builtin。
        try:
            from jiuwenswarm.server.runtime.mcp.registry import list_marketplace_mcps
            filter_val = str((params or {}).get("filter") or "builtin").strip().lower() or "builtin"
            if filter_val not in ("builtin", "local"):
                filter_val = "builtin"
            items = await asyncio.to_thread(list_marketplace_mcps, filter_val)
            await channel.send_response(ws, req_id, ok=True, payload={"type": "list", "items": items})
        except Exception as exc:
            logger.exception("[mcp] mcp.list failed: %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="MCP_INTERNAL")

    def _register_mcp_agent(method_name: str, rm: Any) -> None:
        async def _handler(ws, req_id, params, session_id):
            await _forward_mcp_to_agent(ws, req_id, params, session_id, req_method=rm)
        channel.register_method(method_name, _handler)

    channel.register_method("mcp.list", _mcp_list_local)
    _register_mcp_agent("mcp.show", ReqMethod.MCP_SHOW)
    _register_mcp_agent("mcp.connect", ReqMethod.MCP_CONNECT)
    _register_mcp_agent("mcp.wait_auth", ReqMethod.MCP_WAIT_AUTH)
    _register_mcp_agent("mcp.disconnect", ReqMethod.MCP_DISCONNECT)
    _register_mcp_agent("mcp.register_custom", ReqMethod.MCP_REGISTER_CUSTOM)
    _register_mcp_agent("mcp.delete_custom", ReqMethod.MCP_DELETE_CUSTOM)
    _register_mcp_agent("mcp.save_credentials", ReqMethod.MCP_SAVE_CREDENTIALS)
    from jiuwenswarm.common.schema.message import ReqMethod as _PermReq

    def _register_perm(method_name: str, rm: Any) -> None:
        async def _handler(ws, req_id, params, session_id):
            await _forward_permissions_to_agent(ws, req_id, params, session_id, req_method=rm)

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

    async def _memory_forbidden_get(ws, req_id, params, session_id):
        try:
            cfg = get_config() or {}
            payload = cfg.get("memory", {}).get("forbidden_memory_definition", {})
            await channel.send_response(ws, req_id, ok=True, payload=payload)
        except Exception as e:
            logger.exception("[memory.forbidden.get] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _memory_forbidden_set(ws, req_id, params, session_id):
        from jiuwenswarm.common.config import update_memory_forbidden_in_config
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        try:
            update_memory_forbidden_in_config(params)
            await channel.send_response(ws, req_id, ok=True, payload={"ok": True})
        except Exception as e:
            logger.exception("[memory.forbidden.set] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    channel.register_method("memory.forbidden.get", _memory_forbidden_get)
    channel.register_method("memory.forbidden.set", _memory_forbidden_set)

    async def _forward_harness_to_agent(ws, req_id, params, session_id, *, req_method):
        """harness.*：优先经 E2A 转发到 AgentServer；Agent 未就绪时本地执行（无 agent 实例）。"""
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields

        if not isinstance(req_method, ReqMethod):
            await channel.send_response(ws, req_id, ok=False, error="invalid req_method", code="INTERNAL_ERROR")
            return

        ac = _resolve(agent_client)
        if ac is None or not getattr(ac, "server_ready", False):
            # Agent 未就绪时本地处理（无 agent 实例可用）
            from jiuwenswarm.agents.harness.common.auto_harness import (
                _HARNESS_PACKAGES_FILE,
                AutoHarnessService,
            )

            try:
                if req_method == ReqMethod.HARNESS_PACKAGES_GET:
                    packages_file = Path(_HARNESS_PACKAGES_FILE)
                    if await asyncio.to_thread(packages_file.exists):
                        raw_text = await asyncio.to_thread(packages_file.read_text, encoding="utf-8")
                        data = await asyncio.to_thread(json.loads, raw_text)
                    else:
                        service = AutoHarnessService(rail=None, agent=None)
                        data = await asyncio.to_thread(service.scan_runtime_extensions)
                        await asyncio.to_thread(service.save_packages, data)
                    await channel.send_response(ws, req_id, ok=True, payload=data)
                    return
                elif req_method == ReqMethod.HARNESS_PACKAGES_SCAN:
                    service = AutoHarnessService(rail=None, agent=None)
                    data = await asyncio.to_thread(service.scan_runtime_extensions)
                    await asyncio.to_thread(service.save_packages, data)
                    await channel.send_response(ws, req_id, ok=True, payload=data)
                    return
                elif req_method == ReqMethod.HARNESS_PACKAGES_DELETE:
                    package_id = params.get("package_id")
                    if package_id == "native":
                        await channel.send_response(
                            ws, req_id, ok=False, error="Cannot delete native agent version", code="BAD_REQUEST")
                        return
                    service = AutoHarnessService(rail=None, agent=None)
                    payload = await service.delete_package(package_id)
                    await channel.send_response(ws, req_id, ok=True, payload=payload)
                    return
                else:
                    await channel.send_response(
                        ws, req_id, ok=False,
                        error="Agent not ready for this operation",
                        code="SERVICE_UNAVAILABLE"
                    )
                    return
            except ValueError as exc:
                await channel.send_response(ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST")
                return
            except Exception as exc:
                logger.exception("[harness] local fallback failed: %s", exc)
                await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")
                return

        env = e2a_from_agent_fields(
            request_id=str(req_id) if req_id else "",
            channel_id="",
            session_id=session_id,
            req_method=req_method,
            params=dict(params) if isinstance(params, dict) else {},
        )
        try:
            resp = await ac.send_request(env)
        except Exception as e:
            logger.exception("[harness] forward to agent failed: %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")
            return
        if not resp.ok:
            pl = resp.payload if isinstance(resp.payload, dict) else {}
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(pl.get("error") or "request failed"),
                code=str(pl.get("code") or "BAD_REQUEST"),
            )
            return
        out = resp.payload if isinstance(resp.payload, dict) else {}
        await channel.send_response(ws, req_id, ok=True, payload=out)

    from jiuwenswarm.common.schema.message import ReqMethod as _HarnessReq

    def _register_harness(method_name: str, rm: Any) -> None:
        async def _handler(ws, req_id, params, session_id):
            await _forward_harness_to_agent(ws, req_id, params, session_id, req_method=rm)

        channel.register_method(method_name, _handler)

    _register_harness("harness.packages", _HarnessReq.HARNESS_PACKAGES_GET)
    _register_harness("harness.packages.scan", _HarnessReq.HARNESS_PACKAGES_SCAN)
    _register_harness("harness.activate", _HarnessReq.HARNESS_PACKAGES_ACTIVATE)
    _register_harness("harness.deactivate", _HarnessReq.HARNESS_PACKAGES_DEACTIVATE)
    _register_harness("harness.delete", _HarnessReq.HARNESS_PACKAGES_DELETE)

    async def _harness_import_handler(ws, req_id, params, session_id):
        """Import a harness package via WebSocket (base64 encoded zip content)."""
        # Get base64 encoded file content
        file_content_b64 = params.get("file_content")
        if not file_content_b64:
            await channel.send_response(ws, req_id, ok=False, error="Missing file_content", code="BAD_REQUEST")
            return

        # Decode base64 content
        try:
            file_content = base64.b64decode(file_content_b64)
        except Exception as e:
            await channel.send_response(ws, req_id, ok=False, error=f"Invalid base64 content: {e}", code="BAD_REQUEST")
            return

        # Check file size (100MB limit)
        max_size = 50 * 1024 * 1024
        if len(file_content) > max_size:
            await channel.send_response(ws, req_id, ok=False, error="File exceeds 100MB limit", code="BAD_REQUEST")
            return

        # Save to temp directory
        temp_dir = get_user_workspace_dir() / "auto-harness" / "temp" / "uploads"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_zip_path = temp_dir / f"upload_{uuid.uuid4().hex[:8]}.zip"

        try:
            temp_zip_path.write_bytes(file_content)
            service = AutoHarnessService(rail=None, agent=None)
            package_info = service.import_package(temp_zip_path)
            await channel.send_response(ws, req_id, ok=True, payload={
                "ok": True,
                "package": package_info,
                "message": "Package imported successfully",
            })
        except ValueError as exc:
            msg = str(exc)
            if "already exists" in msg.lower():
                await channel.send_response(ws, req_id, ok=False, error=msg, code="CONFLICT")
            elif "invalid" in msg.lower() or "must contain" in msg.lower():
                await channel.send_response(ws, req_id, ok=False, error=msg, code="BAD_REQUEST")
            else:
                await channel.send_response(ws, req_id, ok=False, error=msg, code="BAD_REQUEST")
        except Exception as exc:
            logger.exception("[harness.import] failed: %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=f"Import failed: {exc}", code="INTERNAL_ERROR")
        finally:
            # Cleanup temp file
            try:
                temp_zip_path.unlink(missing_ok=True)
            except Exception:
                pass

    channel.register_method("harness.import", _harness_import_handler)

    async def _harness_export_handler(ws, req_id, params, session_id):
        """Export a harness package - returns download URL instead of base64 content.

        Uses HTTP download endpoint to avoid WebSocket message size limits.
        The temporary zip file will be cleaned up after download or token expiry.
        """
        package_id = params.get("package_id")
        if not package_id:
            await channel.send_response(ws, req_id, ok=False, error="Missing package_id", code="BAD_REQUEST")
            return

        try:
            service = AutoHarnessService(rail=None, agent=None)
            zip_path = service.export_package(package_id)

            download_info = build_file_download_info(
                str(zip_path),
                zip_path.name,
                session_id,
                expires_in=600,  # 10 minutes
            )

            await channel.send_response(ws, req_id, ok=True, payload={
                "ok": True,
                "download_url": download_info["download_url"],
                "download_token": download_info["download_token"],
                "filename": download_info["name"],
                "file_size": download_info["size"],
                "message": "Package exported successfully",
            })
            # No cleanup here - file will be served via HTTP download endpoint
            # and cleaned up after download or when token expires
        except ValueError as exc:
            msg = str(exc)
            if "not found" in msg.lower():
                await channel.send_response(ws, req_id, ok=False, error=msg, code="NOT_FOUND")
            elif "native" in msg.lower():
                await channel.send_response(ws, req_id, ok=False, error=msg, code="BAD_REQUEST")
            else:
                await channel.send_response(ws, req_id, ok=False, error=msg, code="BAD_REQUEST")
        except Exception as exc:
            logger.exception("[harness.export] failed: %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=f"Export failed: {exc}", code="INTERNAL_ERROR")

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
