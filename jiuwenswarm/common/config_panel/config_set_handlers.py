# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Web 侧 config.set / config.save_all 域 handler 下沉实现（自 gateway app_web_handlers 迁出）。

gateway 拆分：ConfigAdapter（AgentServer 进程）复用这些成熟实现时不再 import
``gateway.channel_manager.web.app_web_handlers``。gateway 侧 ``_register_web_handlers``
通过本模块间接消费同一实现，保持单一实现源。

对外入口：
- ``register_config_set_handlers(channel, *, on_config_saved=None, agent_client=None,
  ensure_codex_dependency=None, ensure_claude_dependency=None)``：在 channel 上注册
  ``config.set`` / ``config.save_all`` 本地 handler（AgentOS 多用户 E2A 代理分叉由
  gateway 侧 ``_register_config_proxy`` 包装，本模块仅提供本地 handler）。
- ``apply_config_payload(params, ...)``：纯写盘管线（.env + config.yaml），供
  ConfigAdapter 等非 channel 场景复用。
- ``ensure_codex_dependency`` / ``ensure_claude_dependency``：外部 CLI 依赖安装器由
  调用方注入（gateway 进程持有安装状态机；AgentServer/ConfigAdapter 上下文不注入时
  视为依赖已可用，跳过安装流程）。
"""

from __future__ import annotations

import importlib.metadata
import inspect
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.llm import ProviderType

from jiuwenswarm.agents.harness.common.rails.permissions.auto_config import (
    is_auto_permission_mode,
)
from jiuwenswarm.common.config import (
    DEFAULT_SWARMFLOW_ENABLED,
    EXTERNAL_CLI_AGENTS_CONFIG_PATH,
    SWARMFLOW_BUDGET_CONFIG_PATH,
    SWARMFLOW_ENABLED_CONFIG_PATH,
    get_config,
    get_config_raw,
    replace_teams_in_config,
    update_a2ui_in_config,
    update_context_engine_enabled_in_config,
    update_default_model_provider_in_config,
    update_enable_free_models_in_config,
    update_external_cli_agents_in_config,
    update_kv_cache_affinity_enabled_in_config,
    update_memory_forbidden_description_in_config,
    update_memory_forbidden_enabled_in_config,
    update_permissions_profile_in_config,
    update_proactive_recommendation_in_config,
    update_rsi_enabled_in_config,
    update_setup_guide_enabled_in_config,
    update_skill_evolution_enabled_in_config,
    update_skill_retrieval_in_config,
    update_swarmflow_budget_in_config,
    update_swarmflow_enabled_in_config,
    update_symphony_in_config,
    update_task_full_duplex_in_config,
    update_task_asr_in_config,
    update_trajectory_ui_in_config,
    update_default_models_in_config,
    update_login_model_settings_in_config,
    validate_persisted_kv_cache_affinity,
)
from jiuwenswarm.common.config_panel.models_handlers import (
    ConfigPanelBadRequest,
    build_models_defaults_from_frontend,
)
from jiuwenswarm.common.context_window import parse_positive_int
from jiuwenswarm.common.kv_cache_affinity_config import (
    ASCEND_AFFINITY_PROVIDER,
    KVC_CONFIG_KEYS,
    default_model_client_config_from_entries,
    has_kv_cache_affinity_capability,
    is_affinity_enabled,
    normalize_affinity_request,
    parse_bool as parse_kvc_bool,
    set_default_model_provider_in_entries,
)
from jiuwenswarm.common.utils import get_env_file
from jiuwenswarm.common.version import __version__
from jiuwenswarm.server.runtime.a2ui.integration import (
    get_a2ui_config_payload,
    get_default_a2ui_config_payload,
    validate_a2ui_config_update,
)

logger = logging.getLogger(__name__)


class ConfigPanelInternalError(RuntimeError):
    """config 域写盘/内部失败（HTTP 语义 500）。"""


# --------------------------------------------------------------------------- #
# 热更新 scope 常量与变更集（自 app_web_handlers 迁出）
# --------------------------------------------------------------------------- #
WEB_CONFIG_RELOAD_CHANNEL_ID = "web"
SEARCH_RELOAD_ENV_KEYS = {
    "BOCHA_API_KEY", "PERPLEXITY_API_KEY", "SERPER_API_KEY", "JINA_API_KEY",
}
MODEL_RELOAD_ENV_KEYS = {
    "MODEL_PROVIDER",
    "MODEL_NAME",
    "API_BASE",
    "API_KEY",
}
MULTIMODAL_RELOAD_ENV_KEYS = {
    "VIDEO_PROVIDER",
    "VIDEO_MODEL_NAME",
    "VIDEO_API_BASE",
    "VIDEO_API_KEY",
    "VIDEO_ENDPOINT_PROFILE",
    "VIDEO_CONTEXT_WINDOW_TOKENS",
    "AUDIO_PROVIDER",
    "AUDIO_MODEL_NAME",
    "AUDIO_API_BASE",
    "AUDIO_API_KEY",
    "AUDIO_ENDPOINT_PROFILE",
    "AUDIO_CONTEXT_WINDOW_TOKENS",
    "VISION_PROVIDER",
    "VISION_MODEL_NAME",
    "VISION_API_BASE",
    "VISION_API_KEY",
    "VISION_ENDPOINT_PROFILE",
    "VISION_CONTEXT_WINDOW_TOKENS",
    "VISION_ENABLED",
    "AUDIO_ENABLED",
    "VIDEO_ENABLED",
    "VIDEO_GEN_ENABLED",
    "VIDEO_GEN_API_BASE",
    "VIDEO_GEN_API_KEY",
    "VIDEO_GEN_MODEL_NAME",
    "VIDEO_GEN_PROVIDER",
    "VIDEO_GEN_PROTOCOL",
    "VIDEO_GEN_CONTEXT_WINDOW_TOKENS",
    "VISUAL_GEN_ENABLED",
    "VISUAL_GEN_API_BASE",
    "VISUAL_GEN_API_KEY",
    "VISUAL_GEN_MODEL_NAME",
    "VISUAL_GEN_PROVIDER",
    "VISUAL_GEN_PROTOCOL",
    "VISUAL_GEN_CONTEXT_WINDOW_TOKENS",
}
ASR_ENV_KEYS = {
    "ASR_API_BASE",
    "ASR_API_KEY",
    "ASR_MODEL_NAME",
}


@dataclass(frozen=True)
class ConfigChangeSet:
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
        if MODEL_RELOAD_ENV_KEYS & set(self.env_updates):
            scopes.add("model")
        if MULTIMODAL_RELOAD_ENV_KEYS & set(self.env_updates):
            scopes.add("multimodal")
        if ASR_ENV_KEYS & set(self.env_updates):
            scopes.add("web_ui")
        if SEARCH_RELOAD_ENV_KEYS & set(self.env_updates):
            scopes.add("search")
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
            elif key_text == "trajectory_ui_enabled":
                scopes.update({"agent_runtime", "web_ui"})
            elif key_text == "task_full_duplex_enabled":
                scopes.add("web_ui")
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
            "target_channel_id": WEB_CONFIG_RELOAD_CHANNEL_ID,
            "reload_scopes": sorted(self.reload_scopes),
        }


@dataclass(frozen=True)
class ConfigApplyResult:
    env_updates: dict[str, str]
    yaml_updated: list[str]
    codex_dependency_install: dict[str, Any] | None = None
    external_cli_dependency_installs: dict[str, dict[str, Any]] | None = None
    canonical_config: dict[str, str] | None = None
    pending_permission_profile: str | None = None
    pending_permission_key: str | None = None


# .env 路径（调用方可 monkeypatch 本模块属性以隔离测试）
ENV_FILE = get_env_file()


# --------------------------------------------------------------------------- #
# 配置键映射（前端 param 名 -> 环境变量名 / config.yaml 路径）
# --------------------------------------------------------------------------- #
# 配置信息：config.get 返回、config.set 可修改的键（前端 param 名 -> 环境变量名）
# default 模型 + video/audio/vision 多模型
CONFIG_SET_ENV_MAP = {
    # default 模型（主对话）
    "model_provider": "MODEL_PROVIDER",
    "model": "MODEL_NAME",
    "api_base": "API_BASE",
    "api_key": "API_KEY",
    "endpoint_profile": "ENDPOINT_PROFILE",
    # video 模型
    "video_api_base": "VIDEO_API_BASE",
    "video_api_key": "VIDEO_API_KEY",
    "video_model": "VIDEO_MODEL_NAME",
    "video_provider": "VIDEO_PROVIDER",
    "video_endpoint_profile": "VIDEO_ENDPOINT_PROFILE",
    "video_vendor_key": "VIDEO_VENDOR_KEY",
    "video_plan": "VIDEO_PLAN",
    "video_context_window_tokens": "VIDEO_CONTEXT_WINDOW_TOKENS",
    "video_enabled": "VIDEO_ENABLED",
    # video processing (generation) - dedicated slot, separate from the
    # video-understanding fields above.
    "video_gen_api_base": "VIDEO_GEN_API_BASE",
    "video_gen_api_key": "VIDEO_GEN_API_KEY",
    "video_gen_model": "VIDEO_GEN_MODEL_NAME",
    "video_gen_provider": "VIDEO_GEN_PROVIDER",
    "video_gen_protocol": "VIDEO_GEN_PROTOCOL",
    "video_gen_context_window_tokens": "VIDEO_GEN_CONTEXT_WINDOW_TOKENS",
    "video_gen_enabled": "VIDEO_GEN_ENABLED",
    # visual processing (image generation) - dedicated slot, independent of
    # both visual_question_answering's VISION_* slot and image_tools.py's
    # DashScope-only generate_image (IMAGE_GEN_* slot).
    "visual_gen_api_base": "VISUAL_GEN_API_BASE",
    "visual_gen_api_key": "VISUAL_GEN_API_KEY",
    "visual_gen_model": "VISUAL_GEN_MODEL_NAME",
    "visual_gen_provider": "VISUAL_GEN_PROVIDER",
    "visual_gen_protocol": "VISUAL_GEN_PROTOCOL",
    "visual_gen_context_window_tokens": "VISUAL_GEN_CONTEXT_WINDOW_TOKENS",
    "visual_gen_enabled": "VISUAL_GEN_ENABLED",
    # audio 模型
    "audio_api_base": "AUDIO_API_BASE",
    "audio_api_key": "AUDIO_API_KEY",
    "audio_model": "AUDIO_MODEL_NAME",
    "audio_provider": "AUDIO_PROVIDER",
    "audio_endpoint_profile": "AUDIO_ENDPOINT_PROFILE",
    "audio_vendor_key": "AUDIO_VENDOR_KEY",
    "audio_plan": "AUDIO_PLAN",
    "audio_context_window_tokens": "AUDIO_CONTEXT_WINDOW_TOKENS",
    "audio_enabled": "AUDIO_ENABLED",
    # vision 模型
    "vision_api_base": "VISION_API_BASE",
    "vision_api_key": "VISION_API_KEY",
    "vision_model": "VISION_MODEL_NAME",
    "vision_provider": "VISION_PROVIDER",
    "vision_endpoint_profile": "VISION_ENDPOINT_PROFILE",
    "vision_vendor_key": "VISION_VENDOR_KEY",
    "vision_plan": "VISION_PLAN",
    "vision_context_window_tokens": "VISION_CONTEXT_WINDOW_TOKENS",
    "vision_enabled": "VISION_ENABLED",
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
    # General ASR used by regular task chat. JoyAI keeps its VOICE_ASR_* settings.
    "asr_api_base": "ASR_API_BASE",
    "asr_api_key": "ASR_API_KEY",
    "asr_model": "ASR_MODEL_NAME",
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
CONFIG_KEYS = tuple(CONFIG_SET_ENV_MAP.keys())

# 来自 config.yaml 的配置项（前端 param 名 -> config.yaml 路径）
CONFIG_YAML_KEYS = frozenset({
    "context_engine_enabled",
    "kv_cache_affinity_enabled",
    "permissions_enabled",
    "memory_forbidden_enabled",
    "memory_forbidden_description",
    "a2ui_enabled",
    "rsi_enabled",
    "trajectory_ui_enabled",
    "task_full_duplex_enabled",
    "task_asr_enabled",
    "proactive_recommendation_enabled",
    "proactive_recommendation_max_recommend_per_day",
    "proactive_recommendation_max_rounds_per_tick",
    "swarmflow_enabled",
    "swarmflow_budget",
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
EXTERNAL_CLI_AGENT_CONFIG_KEYS = frozenset({
    "external_cli_agent_claude_enabled",
    "external_cli_agent_claude_use_builtin",
    "external_cli_agent_claude_cli_path",
    "external_cli_agent_codex_enabled",
    "external_cli_agent_codex_use_builtin",
    "external_cli_agent_codex_cli_path",
})
EXTERNAL_CLI_AGENT_KINDS = ("claude", "codex")
DEFAULT_EXTERNAL_CLI_PUBLISH_HOST = "127.0.0.1"
DEFAULT_EXTERNAL_CLI_PUBLISH_PORT = "19000"
EXTERNAL_CLI_PUBLISH_PATH = "/ws"
UNSUPPORTED_WINDOWS_CLI_SUFFIXES = {".bat", ".cmd", ".ps1"}
PERMISSIONS_PROFILES = frozenset({"default", "full_access"})


def permission_profile(permission_config: object) -> str:
    if not isinstance(permission_config, dict) or permission_config.get("enabled") is not True:
        return "full_access"
    return "automatic" if is_auto_permission_mode(permission_config) else "default"


def canonical_permission_facade(profile: str) -> dict[str, str]:
    return {
        "permissions_profile": profile,
        "permissions_enabled": "false" if profile == "full_access" else "true",
    }


SYMPHONY_CONFIG_SPECS: dict[str, tuple[tuple[str, ...], str, Any]] = {
    "symphony_enabled": (("enabled",), "bool", False),
    "symphony_evolution_enabled": (("evolution", "enabled"), "bool", False),
}
SYMPHONY_CONFIG_KEYS = tuple(SYMPHONY_CONFIG_SPECS.keys())
SKILL_RETRIEVAL_CONFIG_SPECS: dict[str, tuple[tuple[str, ...], str, Any]] = {
    "skill_retrieval_enabled": (("enabled",), "bool", False),
    # Kept for compatibility with existing config-panel clients and older
    # config.yaml files.  Newer runtimes may ignore this legacy switch.
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
SKILL_RETRIEVAL_CONFIG_KEYS = tuple(SKILL_RETRIEVAL_CONFIG_SPECS.keys())


# --------------------------------------------------------------------------- #
# config panel 取值/扁平化工具（config.get 与 config.set 共用）
# --------------------------------------------------------------------------- #
def coerce_config_panel_value(value: Any, value_type: str, default: Any) -> Any:
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


def set_nested_config_value(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = target
    for segment in path[:-1]:
        child = current.get(segment)
        if not isinstance(child, dict):
            child = {}
            current[segment] = child
        current = child
    current[path[-1]] = value


def get_nested_config_value(source: dict[str, Any], path: tuple[str, ...], default: Any) -> Any:
    current: Any = source
    for segment in path:
        if not isinstance(current, dict) or segment not in current:
            return default
        current = current.get(segment)
    return default if current is None else current


def flatten_symphony_for_config_panel(raw: dict[str, Any]) -> dict[str, str]:
    symphony = raw.get("symphony") if isinstance(raw.get("symphony"), dict) else {}
    flat: dict[str, str] = {}
    for key, (path, value_type, default) in SYMPHONY_CONFIG_SPECS.items():
        value = get_nested_config_value(symphony, path, default)
        if value_type == "bool":
            flat[key] = "true" if bool(value) else "false"
        else:
            flat[key] = str(value)
    flat.update(flatten_skill_retrieval_for_config_panel(raw))
    return flat


def flatten_skill_retrieval_for_config_panel(raw: dict[str, Any]) -> dict[str, str]:
    symphony = raw.get("symphony") if isinstance(raw.get("symphony"), dict) else {}
    section = symphony.get("skill_retrieval") if isinstance(symphony.get("skill_retrieval"), dict) else {}
    flat: dict[str, str] = {}
    for key, (path, value_type, default) in SKILL_RETRIEVAL_CONFIG_SPECS.items():
        value = get_nested_config_value(section, path, default)
        if value_type == "bool":
            flat[key] = "true" if bool(value) else "false"
        else:
            flat[key] = str(value)
    return flat


def flatten_swarmflow_for_config_panel(raw: dict[str, Any]) -> dict[str, str]:
    enabled = get_nested_config_value(
        raw,
        SWARMFLOW_ENABLED_CONFIG_PATH,
        DEFAULT_SWARMFLOW_ENABLED,
    )
    budget = get_nested_config_value(raw, SWARMFLOW_BUDGET_CONFIG_PATH, None)
    flat = {"swarmflow_enabled": "true" if enabled else "false"}
    if budget is not None:
        flat["swarmflow_budget"] = str(budget)
    return flat


def flatten_external_cli_agents_for_config_panel(raw: dict[str, Any]) -> dict[str, str]:
    agents = get_nested_config_value(raw, EXTERNAL_CLI_AGENTS_CONFIG_PATH, [])
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


def parse_config_switch_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def flatten_modes_team_for_config_panel(raw: dict[str, Any]) -> dict[str, str]:
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


# --------------------------------------------------------------------------- #
# 外部 CLI 探测（纯函数，subprocess 版本探测）
# --------------------------------------------------------------------------- #
def parse_version_tuple(version: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def external_cli_reference_version(cli_agent: str) -> str:
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


def resolve_external_cli_path(cli_agent: str, cli_path: str = "") -> tuple[str, str, str]:
    requested = cli_path.strip()
    if requested:
        resolved = shutil.which(requested)
        if resolved:
            return resolved, "", ""
        candidate = Path(requested).expanduser()
        if candidate.is_file():
            return str(candidate), "", ""
        if candidate.is_dir():
            return "", f"{requested} is a directory", "directory"
        return "", f"{requested} not found", "not_found"

    resolved = shutil.which(cli_agent)
    if resolved:
        return resolved, "", ""
    return "", f"{cli_agent} not found in PATH", "not_found"


def is_windows_platform() -> bool:
    return os.name == "nt"


def run_external_cli_version_command(cli_agent: str, resolved_path: str) -> tuple[str, str]:
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
    # Both flag variants usually fail with the same message (e.g. WinError 193
    # for a non-executable path); show it once instead of repeating it.
    unique_errors = list(dict.fromkeys(errors))
    return "", "; ".join(unique_errors)


def detect_external_cli_agent(cli_agent: str, cli_path: str = "") -> dict[str, Any]:
    normalized_agent = cli_agent.strip().lower()
    if normalized_agent not in EXTERNAL_CLI_AGENT_KINDS:
        return {
            "cli_agent": normalized_agent,
            "status": "unavailable",
            "path": "",
            "version": "",
            "reference_version": "",
            "message": f"unsupported cli_agent: {cli_agent}",
        }

    resolved_path, path_error, path_reason = resolve_external_cli_path(normalized_agent, cli_path)
    reference_version = external_cli_reference_version(normalized_agent)
    if not resolved_path:
        return {
            "cli_agent": normalized_agent,
            "status": "unsupported" if path_reason == "directory" else "missing",
            "path": "",
            "version": "",
            "reference_version": reference_version,
            "reason": path_reason,
            "message": path_error,
        }

    suffix = Path(resolved_path).suffix.lower()
    if is_windows_platform() and suffix in UNSUPPORTED_WINDOWS_CLI_SUFFIXES:
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

    version_output, version_error = run_external_cli_version_command(normalized_agent, resolved_path)
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
    version_tuple = parse_version_tuple(version)
    reference_tuple = parse_version_tuple(reference_version)
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


# --------------------------------------------------------------------------- #
# config.set 写盘管线
# --------------------------------------------------------------------------- #
def build_symphony_config_update(params: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    for key, (path, value_type, default) in SYMPHONY_CONFIG_SPECS.items():
        if key not in params:
            continue
        value = coerce_config_panel_value(params[key], value_type, default)
        set_nested_config_value(updates, path, value)
    return updates


def build_skill_retrieval_config_update(params: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    for key, (path, value_type, default) in SKILL_RETRIEVAL_CONFIG_SPECS.items():
        if key not in params:
            continue
        value = coerce_config_panel_value(params[key], value_type, default)
        set_nested_config_value(updates, path, value)
    return updates


def team_payload_requests_codex(params: dict[str, Any]) -> bool:
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


def team_item_requests_codex(team_item: dict[str, Any]) -> bool:
    external_cli_agents = team_item.get("external_cli_agents")
    if not isinstance(external_cli_agents, list):
        return False
    for item in external_cli_agents:
        if item == "codex":
            return True
        if isinstance(item, dict) and item.get("cli_agent") == "codex":
            return True
    return False


def inject_external_cli_publish_url(params: dict[str, Any]) -> dict[str, Any]:
    teams_raw = params.get("team")
    if not isinstance(teams_raw, list):
        return params

    publish_url = build_external_cli_publish_url()
    teams: list[Any] = []
    changed = False
    for team_item in teams_raw:
        if isinstance(team_item, dict) and team_item_requests_codex(team_item):
            item = dict(team_item)
            item["external_cli_publish_url"] = publish_url
            teams.append(item)
            changed = True
        else:
            teams.append(team_item)

    if not changed:
        return params
    return {**params, "team": teams}


def external_cli_agent_key(cli_agent: str, suffix: str) -> str:
    return f"external_cli_agent_{cli_agent}_{suffix}"


def external_cli_agent_effective_state(
    cli_agent: str,
    current: dict[str, str],
    params: dict[str, Any],
) -> tuple[bool, bool, str]:
    enabled_key = external_cli_agent_key(cli_agent, "enabled")
    use_builtin_key = external_cli_agent_key(cli_agent, "use_builtin")
    cli_path_key = external_cli_agent_key(cli_agent, "cli_path")
    enabled = parse_config_switch_bool(params.get(enabled_key, current[enabled_key]))
    use_builtin = parse_config_switch_bool(params.get(use_builtin_key, current[use_builtin_key]))
    if use_builtin:
        return enabled, True, ""
    return enabled, False, str(params.get(cli_path_key, current[cli_path_key]) or "").strip()


def external_cli_agents_from_switches(raw: dict[str, Any], params: dict[str, Any]) -> list[dict[str, str]]:
    current = flatten_external_cli_agents_for_config_panel(raw)
    agents: list[dict[str, str]] = []
    for cli_agent in EXTERNAL_CLI_AGENT_KINDS:
        enabled, use_builtin, requested_path = external_cli_agent_effective_state(cli_agent, current, params)
        if not enabled:
            continue
        entry = {"cli_agent": cli_agent}
        if not use_builtin:
            detection = detect_external_cli_agent(cli_agent, requested_path)
            if detection["status"] not in {"ok", "warning"}:
                raise ValueError(
                    f"{cli_agent} cli_path is not available: {detection.get('message') or detection.get('status')}"
                )
            entry["cli_path"] = str(detection.get("path") or "").strip()
        agents.append(entry)
    return agents


def build_external_cli_publish_url() -> str:
    host = str(os.getenv("WEB_HOST") or DEFAULT_EXTERNAL_CLI_PUBLISH_HOST).strip()
    if host in {"", "0.0.0.0", "::", "[::]"}:
        host = DEFAULT_EXTERNAL_CLI_PUBLISH_HOST
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        host = f"[{host}]"

    port = str(os.getenv("WEB_PORT") or DEFAULT_EXTERNAL_CLI_PUBLISH_PORT).strip()
    return f"ws://{host}:{port}{EXTERNAL_CLI_PUBLISH_PATH}"


def persist_env_updates(updates: dict[str, str]) -> None:
    """把已更新的环境变量写回 .env（仅覆盖或追加对应 KEY=value 行）。"""
    env_path = ENV_FILE
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


def validate_proactive_int(
    val: Any, *, name: str, lo: int = 1, hi: int = 50,
) -> int:
    """校验 proactive 数值配置项：必须是 [lo, hi] 的正整数字符串。

    挡住负数、零、浮点数(3.5)、字符串(abc)、科学计数(1e5)、空值。
    校验失败抛 ConfigPanelBadRequest（携带中文提示），由外层返回前端。
    """
    raw = str(val if val is not None else "").strip()
    if not raw:
        raise ConfigPanelBadRequest(f"{name} 不能为空，需为 {lo}-{hi} 的正整数")
    # 正则一次挡住浮点、负数、科学计数、非数字
    if not re.fullmatch(r"[0-9]+", raw):
        raise ConfigPanelBadRequest(
            f"{name} 必须是正整数（{lo}-{hi}），当前值无效：{raw!r}"
        )
    n = int(raw)
    if n < lo or n > hi:
        raise ConfigPanelBadRequest(f"{name} 需为 {lo}-{hi} 的正整数，当前：{n}")
    return n


def parse_config_bool(value: Any) -> bool:
    return parse_kvc_bool(value)


def _get_crypto_provider() -> Any:
    """经 common 钩子取 crypto provider（未注册/未就绪返回 None）。

    gateway 进程由 ``extensions.registry`` 在 create_instance 时安装桥接；
    AgentServer 进程无 crypto 扩展时 api_key 以明文落库（与原实现一致）。
    """
    try:
        from jiuwenswarm.common.security.base_crypto import get_crypto_provider

        return get_crypto_provider()
    except Exception:  # noqa: BLE001
        return None


def encrypt_config_params(params: dict[str, Any]) -> dict[str, Any]:
    crypto = _get_crypto_provider()
    if crypto is None:
        return dict(params)
    encrypted = dict(params)
    for key, val in list(encrypted.items()):
        if key.endswith("_context_window_tokens"):
            continue
        if "api_key" in key.lower() or "token" in key.lower():
            encrypted[key] = crypto.encrypt(val)
    return encrypted


def front_team_template_ids(params: dict[str, Any]) -> set[str] | None:
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


def preserve_deleted_team_entities(params: dict[str, Any]) -> None:
    next_template_ids = front_team_template_ids(params)
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
        raise ConfigPanelInternalError(
            "failed to preserve team entity config: " + ", ".join(sorted(failed_team_names))
        )


def apply_config_payload(
    params: dict[str, Any],
    *,
    ensure_codex_dependency: Any = None,
    ensure_claude_dependency: Any = None,
) -> ConfigApplyResult:
    """Apply config.set-style payload to .env/config.yaml without triggering reload.

    ``ensure_codex_dependency`` / ``ensure_claude_dependency``：外部 CLI 依赖安装器
    （gateway 进程注入）。返回非 None 表示安装已启动、需跳过对应写盘；未注入
    （AgentServer/ConfigAdapter 上下文）时视为依赖已可用。
    """
    if "permissions_mode" in params:
        raise ConfigPanelBadRequest("permissions_mode is not writable")
    if "permissions_profile" in params and "permissions_enabled" in params:
        raise ConfigPanelBadRequest(
            "permissions_profile and permissions_enabled are mutually exclusive"
        )
    params = encrypt_config_params(params)
    env_updates: dict[str, str] = {}
    yaml_updated: list[str] = []
    codex_dependency_install: dict[str, Any] | None = None
    external_cli_dependency_installs: dict[str, dict[str, Any]] = {}
    canonical_config: dict[str, str] | None = None
    available_model_providers = [provider.value for provider in ProviderType]
    raw = get_config_raw()
    preferred_lang = raw.get("preferred_language", "zh")

    pending_permission_profile: str | None = None
    if "permissions_profile" in params:
        pending_permission_profile = str(params["permissions_profile"]).strip()
        if pending_permission_profile not in PERMISSIONS_PROFILES:
            raise ConfigPanelBadRequest("invalid permissions_profile")
        canonical_config = canonical_permission_facade(pending_permission_profile)
    elif "permissions_enabled" in params:
        pending_permission_profile = (
            "default" if parse_config_bool(params["permissions_enabled"]) else "full_access"
        )
        canonical_config = canonical_permission_facade(pending_permission_profile)

    try:
        normalize_affinity_request(params)
    except ValueError as exc:
        raise ConfigPanelBadRequest(str(exc)) from exc

    for param_key, env_key in CONFIG_SET_ENV_MAP.items():
        if param_key not in params:
            continue
        val = params[param_key]
        if param_key.endswith("_context_window_tokens") and val not in (None, ""):
            parsed_context_window = parse_positive_int(val)
            if parsed_context_window is None:
                raise ConfigPanelBadRequest(
                    f"{param_key} must be a positive integer or a value such as 256K or 1M"
                )
            val = str(parsed_context_window)
        if param_key.endswith("_provider") and val and val not in available_model_providers:
            raise ConfigPanelBadRequest(f"Model provider must in: {available_model_providers} ")
        if val is None:
            env_updates[env_key] = ""
        else:
            env_updates[env_key] = str(val).strip()

    raw = get_config_raw()
    preferred_lang = raw.get("preferred_language", "zh")

    if "agents" in params or "team" in params:
        try:
            skip_team_update = False
            if team_payload_requests_codex(params):
                codex_dependency_install = (
                    ensure_codex_dependency() if ensure_codex_dependency else None
                )
                if codex_dependency_install is not None:
                    skip_team_update = True
            if "team" in params and not skip_team_update:
                preserve_deleted_team_entities(params)
            if not skip_team_update:
                replace_teams_in_config(inject_external_cli_publish_url(params))
                yaml_updated.append("modes.team")
        except ValueError as exc:
            raise ConfigPanelBadRequest(str(exc)) from exc
        except ConfigPanelInternalError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[config.set] 写回 modes.team 失败: %s", exc)
            raise ConfigPanelInternalError("failed to update modes.team") from exc

    external_cli_agents_updated = False
    for param_key in CONFIG_YAML_KEYS:
        if param_key not in params:
            continue
        val = params[param_key]
        parsed = parse_config_bool(val)
        try:
            if param_key == "context_engine_enabled":
                update_context_engine_enabled_in_config(parsed)
            elif param_key == "kv_cache_affinity_enabled":
                update_kv_cache_affinity_enabled_in_config(parsed)
            elif param_key == "permissions_enabled":
                continue
            elif param_key == "setup_guide_enabled":
                update_setup_guide_enabled_in_config(parsed)
            elif param_key == "rsi_enabled":
                update_rsi_enabled_in_config(parsed)
            elif param_key == "enable_free_models":
                update_enable_free_models_in_config(parsed)
            elif param_key == "memory_forbidden_enabled":
                update_memory_forbidden_enabled_in_config(parsed)
            elif param_key == "memory_forbidden_description":
                desc_val = str(val).strip()
                update_memory_forbidden_description_in_config({preferred_lang: desc_val})
            elif param_key == "swarmflow_enabled":
                update_swarmflow_enabled_in_config(parsed)
            elif param_key == "swarmflow_budget":
                update_swarmflow_budget_in_config(str(val).strip())
            elif param_key in EXTERNAL_CLI_AGENT_CONFIG_KEYS:
                if not external_cli_agents_updated:
                    try:
                        external_cli_agents = external_cli_agents_from_switches(raw, params)
                    except ValueError as exc:
                        raise ConfigPanelBadRequest(str(exc)) from exc
                    if any(item.get("cli_agent") == "codex" for item in external_cli_agents):
                        codex_dependency_install = (
                            ensure_codex_dependency() if ensure_codex_dependency else None
                        )
                        if codex_dependency_install is not None:
                            external_cli_dependency_installs["codex"] = codex_dependency_install
                            external_cli_agents = [
                                item for item in external_cli_agents if item.get("cli_agent") != "codex"
                            ]
                    if any(item.get("cli_agent") == "claude" for item in external_cli_agents):
                        claude_install = (
                            ensure_claude_dependency() if ensure_claude_dependency else None
                        )
                        if claude_install is not None:
                            external_cli_dependency_installs["claude"] = claude_install
                            external_cli_agents = [
                                item for item in external_cli_agents if item.get("cli_agent") != "claude"
                            ]
                    update_external_cli_agents_in_config(
                        external_cli_agents,
                        build_external_cli_publish_url(),
                    )
                    external_cli_agents_updated = True
            elif param_key == "skill_evolution":
                update_skill_evolution_enabled_in_config(parsed)
            elif param_key.startswith("a2ui_"):
                ok, update, error = validate_a2ui_config_update(param_key, val)
                if not ok:
                    raise ConfigPanelBadRequest(error or "invalid A2UI config")
                update_a2ui_in_config(update)
            elif param_key == "trajectory_ui_enabled":
                update_trajectory_ui_in_config(parsed)
            elif param_key == "task_full_duplex_enabled":
                update_task_full_duplex_in_config(parsed)
            elif param_key == "task_asr_enabled":
                update_task_asr_in_config(parsed)
            elif param_key == "proactive_recommendation_enabled":
                update_proactive_recommendation_in_config({"enabled": parsed})
            elif param_key == "proactive_recommendation_max_recommend_per_day":
                n = validate_proactive_int(val, name="每日推荐上限(max_recommend_per_day)")
                update_proactive_recommendation_in_config({"max_recommend_per_day": n})
            elif param_key == "proactive_recommendation_max_rounds_per_tick":
                n = validate_proactive_int(val, name="每次检查对话轮数(max_rounds_per_tick)")
                update_proactive_recommendation_in_config({"max_rounds_per_tick": n})
            yaml_updated.append(param_key)
        except ConfigPanelBadRequest:
            # proactive 数值校验等：直接返回前端，不被外层吞成 warning
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("[config.set] 写回 config.yaml 失败 %s: %s", param_key, e)
            if param_key == "swarmflow_enabled":
                raise ConfigPanelInternalError("failed to update enable_swarmflow") from e
            if param_key in EXTERNAL_CLI_AGENT_CONFIG_KEYS:
                raise ConfigPanelInternalError("failed to update external_cli_agents") from e

    if params.get("model_provider") == ASCEND_AFFINITY_PROVIDER:
        try:
            if update_default_model_provider_in_config(
                ASCEND_AFFINITY_PROVIDER
            ):
                yaml_updated.append("models.default_provider")
        except Exception as e:  # noqa: BLE001
            logger.warning("[config.set] 写回默认模型 provider 失败: %s", e)

    symphony_updates = build_symphony_config_update(params)
    if symphony_updates:
        try:
            update_symphony_in_config(symphony_updates)
            yaml_updated.extend(k for k in SYMPHONY_CONFIG_KEYS if k in params)
        except Exception as e:
            logger.warning("[config.set] 写回 symphony 失败: %s", e)

    try:
        skill_retrieval_updates = build_skill_retrieval_config_update(params)
    except ValueError as exc:
        raise ConfigPanelBadRequest(str(exc)) from exc
    if skill_retrieval_updates:
        try:
            update_skill_retrieval_in_config(skill_retrieval_updates)
            yaml_updated.extend(k for k in SKILL_RETRIEVAL_CONFIG_KEYS if k in params)
        except Exception as e:
            logger.warning("[config.set] 写回 skill_retrieval 失败: %s", e)

    for env_key, value in env_updates.items():
        os.environ[env_key] = value
    if env_updates:
        persist_env_updates(env_updates)
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
            raise ConfigPanelInternalError(
                "KV cache affinity saved but not applied: " + "; ".join(failures)
            )

    return ConfigApplyResult(
        env_updates=env_updates,
        yaml_updated=yaml_updated,
        codex_dependency_install=codex_dependency_install,
        external_cli_dependency_installs=external_cli_dependency_installs or None,
        canonical_config=canonical_config,
        pending_permission_profile=pending_permission_profile,
        pending_permission_key=(
            "permissions_profile"
            if "permissions_profile" in params
            else "permissions_enabled"
            if "permissions_enabled" in params
            else None
        ),
    )


def commit_pending_permission_profile(apply_result: ConfigApplyResult) -> None:
    profile = apply_result.pending_permission_profile
    if profile is None:
        return
    try:
        update_permissions_profile_in_config(profile)
    except (OSError, ValueError) as exc:
        raise ConfigPanelInternalError("failed to update permissions profile") from exc
    if apply_result.pending_permission_key is not None:
        apply_result.yaml_updated.append(apply_result.pending_permission_key)


async def _clear_agent_config_cache(agent_client=None) -> None:
    """写回 config.yaml 后清除 agent 侧配置缓存，使下次读取时得到最新文件内容。"""
    import uuid as _uuid

    try:
        if agent_client is not None:
            from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
            from jiuwenswarm.common.schema.message import ReqMethod

            env = e2a_from_agent_fields(
                request_id=f"cfg-reload-{_uuid.uuid4().hex[:8]}",
                channel_id="",
                req_method=ReqMethod.AGENT_RELOAD_CONFIG,
            )
            await agent_client.send_request(env)
        else:
            get_config()
    except Exception:  # noqa: BLE001
        pass


async def apply_config_change_set(
    change_set: ConfigChangeSet,
    *,
    on_config_saved: Any = None,
    agent_client: Any = None,
) -> bool:
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
    await _clear_agent_config_cache(agent_client)
    return True


# --------------------------------------------------------------------------- #
# handler 实现（channel 语义与 app_web_handlers 原实现一致）
# --------------------------------------------------------------------------- #
async def config_get_handler(
    channel: Any,
    ws: Any,
    req_id: Any,
    params: Any,
    session_id: Any,
) -> None:
    """返回 CONFIG_SET_ENV_MAP 里所有键对应的环境变量当前值（Web 契约）。"""
    payload = {
        param_key: (os.getenv(env_key) or "")
        for param_key, env_key in CONFIG_SET_ENV_MAP.items()
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
        rsi_cfg = raw.get("rsi") or {}
        payload["rsi_enabled"] = "true" if rsi_cfg.get("enabled", True) else "false"
        crypto_provider = _get_crypto_provider()
        if crypto_provider is not None:
            for key, val in list(payload.items()):
                if "api_key" in key.lower() or "token" in key.lower():
                    payload[key] = crypto_provider.decrypt(val)
        react_cfg = raw.get("react") or {}
        ctx_cfg = react_cfg.get("context_engine_config") or {}
        payload["context_engine_enabled"] = "true" if ctx_cfg.get("enabled", False) else "false"
        payload["kv_cache_affinity_enabled"] = (
            "true" if is_affinity_enabled(raw) else "false"
        )
        perm_cfg = raw.get("permissions") or {}
        payload.update(canonical_permission_facade(permission_profile(perm_cfg)))
        # Skill evolution is controlled solely by the canonical nested YAML key.
        evolution_cfg = (raw.get("react") or {}).get("evolution") or {}
        payload["skill_evolution"] = "true" if evolution_cfg.get("skill_evolution", False) else "false"
        memory_cfg = (raw.get("memory") or {}).get("forbidden_memory_definition") or {}
        payload["memory_forbidden_enabled"] = "true" if memory_cfg.get("enabled", False) else "false"
        memory_desc = memory_cfg.get("description") or {}
        payload["memory_forbidden_description"] = memory_desc
        payload.update(get_a2ui_config_payload(raw))
        trajectory_cfg = raw.get("trajectory_ui") or {}
        payload["trajectory_ui_enabled"] = (
            "true" if trajectory_cfg.get("enabled", False) else "false"
        )
        experimental_cfg = raw.get("experimental") or {}
        payload["task_full_duplex_enabled"] = (
            "true" if experimental_cfg.get("task_full_duplex_enabled", False) else "false"
        )
        payload["task_asr_enabled"] = (
            "true" if experimental_cfg.get("task_asr_enabled", False) else "false"
        )
        payload.update(flatten_swarmflow_for_config_panel(raw))
        payload.update(flatten_external_cli_agents_for_config_panel(raw))
        payload.update(flatten_symphony_for_config_panel(raw))
        if not payload.get("free_search_ddg_enabled"):
            payload["free_search_ddg_enabled"] = "false"
        if not payload.get("free_search_bing_enabled"):
            payload["free_search_bing_enabled"] = "false"
        payload.update(flatten_modes_team_for_config_panel(raw))
        # Proactive recommendation — use resolved config (env vars expanded)
        resolved = get_config()
        proactive_cfg = resolved.get("proactive_recommendation") or {}
        payload["proactive_recommendation_enabled"] = "true" if proactive_cfg.get("enabled", False) else "false"
        payload["proactive_recommendation_max_recommend_per_day"] = str(
            proactive_cfg.get("max_recommend_per_day", 10))
        payload["proactive_recommendation_max_rounds_per_tick"] = str(
            proactive_cfg.get("max_rounds_per_tick", 20))
        models_cfg = resolved.get("models") or {}
        payload["enable_free_models"] = "true" if models_cfg.get("enable_free_models", False) else "false"
    except Exception:  # noqa: BLE001
        payload.setdefault("context_engine_enabled", "false")
        payload.setdefault("kv_cache_affinity_enabled", "false")
        payload.setdefault("permissions_enabled", "false")
        payload.setdefault("rsi_enabled", "true")
        payload.setdefault("permissions_profile", "full_access")
        payload.setdefault("setup_guide_enabled", "true")
        payload.setdefault("skill_evolution", "false")
        payload.setdefault("memory_forbidden_enabled", "false")
        payload.setdefault("memory_forbidden_description", "")
        payload.setdefault("swarmflow_enabled", "true" if DEFAULT_SWARMFLOW_ENABLED else "false")
        for key, value in get_default_a2ui_config_payload().items():
            payload.setdefault(key, value)
        payload.setdefault("trajectory_ui_enabled", "false")
        payload.setdefault("task_full_duplex_enabled", "false")
        payload.setdefault("task_asr_enabled", "false")
        for key, (_, value_type, default) in {
            **SYMPHONY_CONFIG_SPECS,
            **SKILL_RETRIEVAL_CONFIG_SPECS,
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
        payload.setdefault("enable_free_models", "false")
    await channel.send_response(ws, req_id, ok=True, payload=payload)


async def config_set_handler(
    channel: Any,
    ws: Any,
    req_id: Any,
    params: Any,
    session_id: Any,
    *,
    on_config_saved: Any = None,
    agent_client: Any = None,
    ensure_codex_dependency: Any = None,
    ensure_claude_dependency: Any = None,
) -> None:
    """根据前端消息内容更新配置（支持 .env 与 config.yaml 中的键），并写回对应文件。"""
    if not isinstance(params, dict):
        await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
        return
    try:
        apply_result = apply_config_payload(
            params,
            ensure_codex_dependency=ensure_codex_dependency,
            ensure_claude_dependency=ensure_claude_dependency,
        )
        commit_pending_permission_profile(apply_result)
    except ConfigPanelBadRequest as exc:
        await channel.send_response(ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST")
        return
    except ConfigPanelInternalError as exc:
        await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")
        return
    env_updates = apply_result.env_updates
    yaml_updated = apply_result.yaml_updated
    change_set = ConfigChangeSet(env_updates, yaml_updated)
    try:
        applied_without_restart = await apply_config_change_set(
            change_set,
            on_config_saved=on_config_saved,
            agent_client=agent_client,
        )
    except Exception as exc:
        logger.warning("[config.set] on_config_saved failed: %s", exc)
        applied_without_restart = False

    if "enable_free_models" in apply_result.yaml_updated:
        try:
            from jiuwenswarm.server.runtime.opencode_zen import warm_zen_free_models
            await warm_zen_free_models(reason="config-toggle")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[config.set] warm_zen_free_models failed: %s", exc)

    updated_param_keys = [k for k, e in CONFIG_SET_ENV_MAP.items() if e in env_updates] + yaml_updated
    payload = {"updated": updated_param_keys, "applied_without_restart": applied_without_restart}
    if apply_result.codex_dependency_install is not None:
        payload["codex_dependency_install"] = apply_result.codex_dependency_install
    if apply_result.external_cli_dependency_installs is not None:
        payload["external_cli_dependency_installs"] = apply_result.external_cli_dependency_installs
    if apply_result.canonical_config is not None:
        payload["canonical_config"] = apply_result.canonical_config
    await channel.send_response(
        ws, req_id, ok=True,
        payload=payload,
    )


def parse_login_model_settings(raw: Any) -> dict[str, int | None]:
    if not isinstance(raw, dict):
        raise ConfigPanelBadRequest("login_model_settings must be object")
    parsed: dict[str, int | None] = {}
    for name, item in raw.items():
        model_name = str(name or "").strip()
        if not model_name or not isinstance(item, dict) or "context_window" not in item:
            raise ConfigPanelBadRequest(f"login_model_settings[{name!r}].context_window is required")
        value = item["context_window"]
        if value is None:
            parsed[model_name] = None
            continue
        context_window = parse_positive_int(value)
        if context_window is None:
            raise ConfigPanelBadRequest(f"login_model_settings[{name!r}].context_window must be positive")
        parsed[model_name] = context_window
    return parsed


async def config_save_all_handler(
    channel: Any,
    ws: Any,
    req_id: Any,
    params: Any,
    session_id: Any,
    *,
    on_config_saved: Any = None,
    agent_client: Any = None,
    ensure_codex_dependency: Any = None,
    ensure_claude_dependency: Any = None,
) -> None:
    """Batch-save config panel changes and trigger a single hot reload.

    Accepted payload keys:
    - config: config.set-style key/value updates
    - models: complete models.defaults draft list
    - login_model_settings: per-login-model user settings (context window)
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
            new_models = build_models_defaults_from_frontend(params.get("models"))
        login_model_settings: dict[str, int | None] | None = None
        if "login_model_settings" in params:
            login_model_settings = parse_login_model_settings(params.get("login_model_settings"))

        config_params: dict[str, Any] = {}
        raw_config_params = params.get("config")
        if raw_config_params is not None:
            if not isinstance(raw_config_params, dict):
                raise ConfigPanelBadRequest("config must be object")
            config_params.update(raw_config_params)

        if "agents" in params:
            config_params["agents"] = params.get("agents")
        if "team" in params:
            config_params["team"] = params.get("team")

        affinity_requested = parse_config_bool(config_params.get("kv_cache_affinity_enabled"))
        if affinity_requested and new_models is not None:
            if set_default_model_provider_in_entries(
                new_models,
                ASCEND_AFFINITY_PROVIDER,
            ):
                yaml_updated.append("models.default_provider")

        if config_params:
            apply_result = apply_config_payload(
                config_params,
                ensure_codex_dependency=ensure_codex_dependency,
                ensure_claude_dependency=ensure_claude_dependency,
            )
            applied_env = apply_result.env_updates
            env_updates.update(applied_env)
        else:
            apply_result = ConfigApplyResult({}, [])

        if new_models is not None:
            if (
                is_affinity_enabled(get_config_raw())
                and not has_kv_cache_affinity_capability(
                    default_model_client_config_from_entries(new_models)
                )
            ):
                update_kv_cache_affinity_enabled_in_config(False)
                yaml_updated.append("kv_cache_affinity_enabled")
            update_default_models_in_config(new_models)
            yaml_updated.append("models.defaults")
            models_count = len(new_models)

        if login_model_settings:
            update_login_model_settings_in_config(login_model_settings)
            yaml_updated.append("models.login_model_settings")

        kvc_config_changed = new_models is not None or any(
            key in config_params for key in KVC_CONFIG_KEYS
        )
        if kvc_config_changed:
            valid, failures = validate_persisted_kv_cache_affinity()
            if not valid:
                update_kv_cache_affinity_enabled_in_config(False)
                raise ConfigPanelInternalError(
                    "KV cache affinity saved but not applied: " + "; ".join(failures)
                )

        commit_pending_permission_profile(apply_result)
        yaml_updated.extend(apply_result.yaml_updated)

        change_set = ConfigChangeSet(
            env_updates,
            yaml_updated,
            force=bool(env_updates or yaml_updated),
        )
        applied_without_restart = await apply_config_change_set(
            change_set,
            on_config_saved=on_config_saved,
            agent_client=agent_client,
        )

        if "enable_free_models" in yaml_updated:
            try:
                from jiuwenswarm.server.runtime.opencode_zen import warm_zen_free_models
                await warm_zen_free_models(reason="config-toggle")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[config.save_all] warm_zen_free_models failed: %s", exc)

        payload = {
            "updated": [k for k, e in CONFIG_SET_ENV_MAP.items() if e in env_updates] + yaml_updated,
            "applied_without_restart": applied_without_restart,
            "models_count": models_count,
        }
        if apply_result.codex_dependency_install is not None:
            payload["codex_dependency_install"] = apply_result.codex_dependency_install
        if apply_result.external_cli_dependency_installs is not None:
            payload["external_cli_dependency_installs"] = apply_result.external_cli_dependency_installs
        if apply_result.canonical_config is not None:
            payload["canonical_config"] = apply_result.canonical_config

        await channel.send_response(
            ws,
            req_id,
            ok=True,
            payload=payload,
        )
    except ConfigPanelBadRequest as exc:
        await channel.send_response(ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST")
    except ConfigPanelInternalError as exc:
        await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[config.save_all] %s", exc)
        await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")


def register_config_set_handlers(
    channel: Any,
    *,
    on_config_saved: Any = None,
    agent_client: Any = None,
    ensure_codex_dependency: Any = None,
    ensure_claude_dependency: Any = None,
) -> None:
    """在 channel 上注册 Web 侧 config.set / config.save_all 本地 handler。

    注意：AgentOS 多用户的 E2A 代理分叉由 gateway 侧 ``_register_config_proxy``
    包装；ConfigAdapter 直接本地执行。两侧注册入口各自调用本函数。
    """

    async def _config_get(ws, req_id, params, session_id):
        await config_get_handler(channel, ws, req_id, params, session_id)

    async def _config_set(ws, req_id, params, session_id):
        await config_set_handler(
            channel, ws, req_id, params, session_id,
            on_config_saved=on_config_saved,
            agent_client=agent_client,
            ensure_codex_dependency=ensure_codex_dependency,
            ensure_claude_dependency=ensure_claude_dependency,
        )

    async def _config_save_all(ws, req_id, params, session_id):
        await config_save_all_handler(
            channel, ws, req_id, params, session_id,
            on_config_saved=on_config_saved,
            agent_client=agent_client,
            ensure_codex_dependency=ensure_codex_dependency,
            ensure_claude_dependency=ensure_claude_dependency,
        )

    channel.register_method("config.get", _config_get)
    channel.register_method("config.set", _config_set)
    channel.register_method("config.save_all", _config_save_all)
