# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import secrets
import shutil
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

import yaml
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from openjiuwen.core.foundation.llm import Model, ProviderType
from openjiuwen.core.foundation.llm.schema.config import (
    ModelClientConfig,
    ModelRequestConfig,
)
from openjiuwen.rsi.auto_harness.schema import load_auto_harness_config

from jiuwenswarm.common.config import (
    get_config,
    get_config_raw,
    get_default_models,
    resolve_env_vars,
    update_auto_recap_enabled_in_config,
    update_context_engine_enabled_in_config,
    update_memory_forbidden_enabled_in_config,
    update_permissions_enabled_in_config,
    get_model_names,
    update_preferred_language_in_config,
    update_swarmflow_enabled_in_config,
    update_config,
)
from jiuwenswarm.common.reasoning_injector import build_reasoning_model_request_kwargs
from jiuwenswarm.gateway.routing.route_binding import GatewayRouteBinding
from jiuwenswarm.common.version import __version__
from jiuwenswarm.common.utils import get_user_workspace_dir
from jiuwenswarm.gateway.routing.agent_request_timeout import (
    AGENT_SERVER_TIMEOUT_CODE,
    AGENT_SERVER_TIMEOUT_ERROR,
    AgentRequestTimeoutError,
    resolve_agent_request_timeout_seconds,
    send_agent_request_with_timeout,
)

logger = logging.getLogger(__name__)


class _ModelOpError(Exception):
    """模型操作校验失败：在 update_config 事务内抛出，事务外转成 RPC 错误响应。"""

# Auto-Harness config file path
_DEFAULT_REPO_URL = "https://gitcode.com/openJiuwen/agent-core.git"
_AUTO_HARNESS_CONFIG_DIR = get_user_workspace_dir() / "auto-harness"
_AUTO_HARNESS_CONFIG_FILE = _AUTO_HARNESS_CONFIG_DIR / "config.yaml"
_AUTO_HARNESS_LOCAL_REPO = _AUTO_HARNESS_CONFIG_DIR / "repo" / "openJiuwen--agent-core"

# Default values for ci_gate config
_DEFAULT_CI_GATE_PYTHON_EXECUTABLE = sys.executable
_DEFAULT_CI_GATE_INSTALL_COMMAND = "uv sync --active --group dev --extra cli"


def _resolve_agent_client(agent_client: Any) -> Any:
    if isinstance(agent_client, dict):
        return agent_client.get("value")
    return agent_client


async def _send_tui_agent_request(real_client: Any, env: Any, *, label: str) -> Any:
    timeout_seconds = resolve_agent_request_timeout_seconds(
        channel_id="tui",
        method=getattr(env, "method", None),
        is_stream=bool(getattr(env, "is_stream", False)),
    )
    return await send_agent_request_with_timeout(
        real_client,
        env,
        label=f"tui {label}",
        timeout_seconds=timeout_seconds,
    )


def _get_auto_harness_config() -> dict[str, Any]:
    """Load auto-harness config.yaml with auto-fill for ci_gate defaults."""
    config: dict[str, Any] = {}

    if not _AUTO_HARNESS_CONFIG_FILE.exists():
        load_auto_harness_config(str(_AUTO_HARNESS_CONFIG_FILE))

    try:
        config = yaml.safe_load(_AUTO_HARNESS_CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception as e:
        logger.warning("[auto-harness config] Failed to load: %s", e)
        config = {}

    # Auto-fill ci_gate defaults if missing
    ci_gate = config.get("ci_gate") or {}
    git_config = config.get("git") or {}
    needs_save = False

    git_remote = git_config.get("remote")
    if not git_remote:
        git_config["remote"] = "autoharness"

    # Ensure local_repo is a string (not Path object which causes YAML serialization issues)
    local_repo = config.get("local_repo")
    repo_url = config.get("repo_url")
    if not local_repo:
        config["local_repo"] = str(_AUTO_HARNESS_LOCAL_REPO)
        needs_save = True

    if not repo_url:
        config["repo_url"] = str(_DEFAULT_REPO_URL)
        needs_save = True

    elif hasattr(local_repo, "__fspath__"):  # Path-like object
        config["local_repo"] = str(local_repo)
        needs_save = True

    if not ci_gate.get("python_executable"):
        ci_gate["python_executable"] = str(_DEFAULT_CI_GATE_PYTHON_EXECUTABLE)
        needs_save = True

    if not ci_gate.get("install_command"):
        ci_gate["install_command"] = _DEFAULT_CI_GATE_INSTALL_COMMAND
        needs_save = True

    budget = config.get("budget", {})
    max_tasks_per_session = budget.get("max_tasks_per_session", 5)
    if max_tasks_per_session > 5:
        budget["max_tasks_per_session"] = 5
        needs_save = True

    if needs_save:
        config["ci_gate"] = ci_gate
        _save_auto_harness_config(config)
        logger.info("[auto-harness config] Auto-filled ci_gate defaults: python_executable=%s, install_command=%s",
                    ci_gate.get("python_executable"), ci_gate.get("install_command"))

    return config


def _save_auto_harness_config(config: dict[str, Any]) -> None:
    """Save auto-harness config.yaml."""
    _AUTO_HARNESS_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _AUTO_HARNESS_CONFIG_FILE.write_text(
        yaml.dump(config, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8"
    )


def _update_auto_harness_git_user_name(value: str) -> None:
    """Update git.user_name, fork_owner, and gitcode.username in auto-harness config.
    - git.user_name: 用于 git commit
    - git.fork_owner: 用于创建 PR
    - gitcode.username: GitCode 登录用户名
    """
    config = _get_auto_harness_config()
    if "git" not in config:
        config["git"] = {}
    config["git"]["user_name"] = value
    config["git"]["fork_owner"] = value  # 合并：用户名同时作为 fork_owner
    if "gitcode" not in config:
        config["gitcode"] = {}
    config["gitcode"]["username"] = value  # 合并：用户名同时作为 gitcode.username
    _save_auto_harness_config(config)


def _update_auto_harness_git_user_email(value: str) -> None:
    """Update git.user_email in auto-harness config."""
    config = _get_auto_harness_config()
    if "git" not in config:
        config["git"] = {}
    config["git"]["user_email"] = value
    _save_auto_harness_config(config)


def _update_auto_harness_gitcode_access_token(value: str) -> None:
    """Update gitcode.access_token in auto-harness config."""
    config = _get_auto_harness_config()
    if "gitcode" not in config:
        config["gitcode"] = {}
    config["gitcode"]["access_token"] = value
    _save_auto_harness_config(config)

# ── 需要转发到 Agent 的方法集合 ──────────────────────────────

CLI_FORWARD_REQ_METHODS = frozenset(
    {
        "command.add_dir",
        "command.btw",
        "command.chrome",
        "command.compact",
        "command.compact_partial",
        "command.context",
        "command.recap",
        "command.diff",
        "command.simplify",
        "command.mcp",
        "command.resume",
        "command.sandbox",
        "command.session",
        "command.workflows",
        "command.status",
        "command.goal",
        "chat.send",
        "chat.interrupt",
        "chat.resume",
        "chat.user_answer",
        "chat.swarmflow_reply",
        "history.get",
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
        "skills.evolution.status",
        "skills.evolution.get",
        "skills.evolution.save",
        "symphony.build_score",
        "symphony.pause_build",
        "symphony.score_status",
        "symphony.graph",
        "symphony.plan",
        "plugins.list",
        "plugins.install",
        "plugins.uninstall",
        "plugins.enable",
        "plugins.disable",
        "plugins.reload",
        "permissions.tools.get",
        "permissions.tools.update",
        "permissions.tools.delete",
        "permissions.rules.get",
        "permissions.rules.create",
        "permissions.rules.update",
        "permissions.rules.delete",
        "extensions.list",
        "extensions.import",
        "extensions.delete",
        "extensions.toggle",
        "session.switch",
        "team.templates.list",
        "team.bindings.list",
        "team.binding.create",
        "team.binding.generate",
        "team.session.bind",
        "team.mq.publish",
        "session.fork",
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
    }
)

CLI_FORWARD_NO_LOCAL_HANDLER_METHODS = frozenset(
    {
        "command.add_dir",
        "command.btw",
        "command.chrome",
        "command.compact",
        "command.compact_partial",
        "command.context",
        "command.recap",
        "command.diff",
        "command.simplify",
        "command.mcp",
        "command.resume",
        "command.sandbox",
        "command.session",
        "command.workflows",
        "command.status",
        "command.goal",
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
        "skills.evolution.status",
        "skills.evolution.get",
        "skills.evolution.save",
        "symphony.build_score",
        "symphony.pause_build",
        "symphony.score_status",
        "symphony.graph",
        "symphony.plan",
        "plugins.list",
        "plugins.install",
        "plugins.uninstall",
        "plugins.enable",
        "plugins.disable",
        "plugins.reload",
        "permissions.tools.get",
        "permissions.tools.update",
        "permissions.tools.delete",
        "permissions.rules.get",
        "permissions.rules.create",
        "permissions.rules.update",
        "permissions.rules.delete",
        "extensions.list",
        "extensions.import",
        "extensions.delete",
        "extensions.toggle",
        "session.switch",
        "team.templates.list",
        "team.bindings.list",
        "team.binding.create",
        "team.binding.generate",
        "team.session.bind",
        "team.mq.publish",
        "session.fork",
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
    }
)


@dataclass
class CliHandlersBindParams:
    channel: Any  # GatewayServer instance
    agent_client: Any = None
    message_handler: Any = None
    third_agent: Any = None
    on_config_saved: Any = None
    path: str = "/tui"
    cron_controller: Any = None


@dataclass
class CliRouteBindParams:
    agent_client: Any = None
    message_handler: Any = None
    third_agent: Any = None
    on_config_saved: Any = None
    path: str = "/tui"
    channel_id: str = "tui"
    cron_controller: Any = None
    # V2: 委托 ws 注册的 TuiChannel 实例。GatewayServer 仍作 /tui ws 宿主 + 入站帧解析，
    # 但把 ws + RoutingKey 委托注册进 TuiChannel 的五维索引，出站由 ChannelManager
    # 派发到 TuiChannel.send（按 delivery.ws_id 物理寻址）。
    ws_channel: Any = None


@dataclass
class ForwardRewindE2AParams:
    """Parameters for forwarding rewind request to AgentServer via E2A."""

    ws: Any
    req_id: str
    target_sid: str
    turn_index: int
    req_method: Any
    error_label: str
    user_id: str | None = None


_CLI_CONFIG_SET_ENV_MAP = {
    "model_provider": "MODEL_PROVIDER",
    "model": "MODEL_NAME",
    "api_base": "API_BASE",
    "api_key": "API_KEY",
    "video_api_base": "VIDEO_API_BASE",
    "video_api_key": "VIDEO_API_KEY",
    "video_model": "VIDEO_MODEL_NAME",
    "video_provider": "VIDEO_PROVIDER",
    "audio_api_base": "AUDIO_API_BASE",
    "audio_api_key": "AUDIO_API_KEY",
    "audio_model": "AUDIO_MODEL_NAME",
    "audio_provider": "AUDIO_PROVIDER",
    "vision_api_base": "VISION_API_BASE",
    "vision_api_key": "VISION_API_KEY",
    "vision_model": "VISION_MODEL_NAME",
    "vision_provider": "VISION_PROVIDER",
    "email_address": "EMAIL_ADDRESS",
    "email_token": "EMAIL_TOKEN",
    "embed_api_key": "EMBED_API_KEY",
    "embed_api_base": "EMBED_API_BASE",
    "embed_model": "EMBED_MODEL",
    "jina_api_key": "JINA_API_KEY",
    "serper_api_key": "SERPER_API_KEY",
    "perplexity_api_key": "PERPLEXITY_API_KEY",
    "github_token": "GITHUB_TOKEN",
    "evolution_auto_scan": "EVOLUTION_AUTO_SCAN",
    "teamskills_market_url": "TEAM_SKILLS_HUB_BASE_URL",
    "teamskills_user_token": "TEAM_SKILLS_HUB_USER_TOKEN",
    "teamskills_system_token": "TEAM_SKILLS_HUB_SYSTEM_TOKEN",
    "teamskills_allowed_download_hosts": "TEAM_SKILLS_HUB_ALLOWED_DOWNLOAD_HOSTS",
}

_CLI_CONFIG_YAML_SETTERS: dict[str, Any] = {
    "auto_recap_enabled": update_auto_recap_enabled_in_config,
    "context_engine_enabled": update_context_engine_enabled_in_config,
    "permissions_enabled": update_permissions_enabled_in_config,
    "memory_forbidden_enabled": update_memory_forbidden_enabled_in_config,
    "preferred_language": update_preferred_language_in_config,
    "enable_swarmflow": update_swarmflow_enabled_in_config,
    # Auto-Harness config items (stored in ~/.jiuwenswarm/auto-harness/config.yaml)
    # 用户名同时设置 git.user_name, fork_owner, gitcode.username（三者合一）
    "auto_harness_git_user_name": _update_auto_harness_git_user_name,
    "auto_harness_git_user_email": _update_auto_harness_git_user_email,
    "auto_harness_gitcode_access_token": _update_auto_harness_gitcode_access_token,
}

_CLI_CONFIG_YAML_KEYS = frozenset(_CLI_CONFIG_YAML_SETTERS.keys())


_PREFERRED_LANGUAGE_OPTIONS = ("zh", "en")


def _build_config_schema() -> list[dict]:
    """构建配置项 Schema，供前端渲染交互界面。与 config.yaml 结构对齐。"""
    available_providers = [p.value for p in ProviderType]
    # 显式使用 ProviderType.OpenAI 作为默认供应商，避免依赖枚举声明顺序
    default_provider = (
        ProviderType.OpenAI.value
        if hasattr(ProviderType, "OpenAI")
        else (available_providers[0] if available_providers else "")
    )
    empty = ""
    return [
        # Model
        {"key": "model", "label": "默认模型", "group": "Model", "type": "string",
         "source": "env", "default": empty},
        {"key": "model_provider", "label": "模型供应商", "group": "Model", "type": "select",
         "options": available_providers, "source": "env", "default": default_provider},
        {"key": "api_base", "label": "API 地址", "group": "Model", "type": "string",
         "source": "env", "default": empty},
        {"key": "api_key", "label": "API Key", "group": "Model", "type": "password",
         "sensitive": True, "source": "env", "default": empty},
        # Vision
        {"key": "vision_model", "label": "视觉模型", "group": "Vision", "type": "string",
         "source": "env", "default": empty},
        {"key": "vision_provider", "label": "视觉供应商", "group": "Vision", "type": "select",
         "options": available_providers, "source": "env", "default": default_provider},
        {"key": "vision_api_base", "label": "视觉API地址", "group": "Vision", "type": "string",
         "source": "env", "default": empty},
        {"key": "vision_api_key", "label": "视觉API Key", "group": "Vision", "type": "password",
         "sensitive": True, "source": "env", "default": empty},
        # Video
        {"key": "video_model", "label": "视频模型", "group": "Video", "type": "string",
         "source": "env", "default": empty},
        {"key": "video_provider", "label": "视频供应商", "group": "Video", "type": "select",
         "options": available_providers, "source": "env", "default": default_provider},
        {"key": "video_api_base", "label": "视频API地址", "group": "Video", "type": "string",
         "source": "env", "default": empty},
        {"key": "video_api_key", "label": "视频API Key", "group": "Video", "type": "password",
         "sensitive": True, "source": "env", "default": empty},
        # Audio
        {"key": "audio_model", "label": "音频模型", "group": "Audio", "type": "string",
         "source": "env", "default": empty},
        {"key": "audio_provider", "label": "音频供应商", "group": "Audio", "type": "select",
         "options": available_providers, "source": "env", "default": default_provider},
        {"key": "audio_api_base", "label": "音频API地址", "group": "Audio", "type": "string",
         "source": "env", "default": empty},
        {"key": "audio_api_key", "label": "音频API Key", "group": "Audio", "type": "password",
         "sensitive": True, "source": "env", "default": empty},
        # Embedding
        {"key": "embed_api_key", "label": "嵌入API Key", "group": "Embedding", "type": "password",
         "sensitive": True, "source": "env", "default": empty},
        {"key": "embed_api_base", "label": "嵌入API地址", "group": "Embedding", "type": "string",
         "source": "env", "default": empty},
        {"key": "embed_model", "label": "嵌入模型", "group": "Embedding", "type": "string",
         "source": "env", "default": empty},
        # Search & External
        {"key": "jina_api_key", "label": "Jina API Key", "group": "Search & External", "type": "password",
         "sensitive": True, "source": "env", "default": empty},
        {"key": "serper_api_key", "label": "Serper API Key", "group": "Search & External", "type": "password",
         "sensitive": True, "source": "env", "default": empty},
        {"key": "perplexity_api_key", "label": "Perplexity API Key", "group": "Search & External", "type": "password",
         "sensitive": True, "source": "env", "default": empty},
        {"key": "github_token", "label": "GitHub Token", "group": "Search & External", "type": "password",
         "sensitive": True, "source": "env", "default": empty},
        # TeamSkills
        {"key": "teamskills_market_url", "label": "TeamSkills Hub 地址", "group": "TeamSkills", "type": "string",
         "source": "env", "default": empty},
        {"key": "teamskills_user_token", "label": "TeamSkills 用户Token", "group": "TeamSkills", "type": "password",
         "sensitive": True, "source": "env", "default": empty},
        {"key": "teamskills_system_token", "label": "TeamSkills 系统Token", "group": "TeamSkills", "type": "password",
         "sensitive": True, "source": "env", "default": empty},
        {
         "key": "teamskills_allowed_download_hosts",
         "label": "TeamSkills 下载白名单Hosts(逗号分隔)",
         "group": "TeamSkills",
         "type": "string",
         "source": "env", "default": empty},
        # Email
        {"key": "email_address", "label": "邮箱地址", "group": "Email", "type": "string",
         "source": "env", "default": empty},
        {"key": "email_token", "label": "邮箱Token", "group": "Email", "type": "password",
         "sensitive": True, "source": "env", "default": empty},
        # Features
        {"key": "context_engine_enabled", "label": "上下文压缩", "group": "Features",
         "type": "toggle", "source": "yaml", "default": "false"},
        {"key": "permissions_enabled", "label": "权限管控", "group": "Features",
         "type": "toggle", "source": "yaml", "default": "false"},
        {"key": "memory_forbidden_enabled", "label": "敏感信息过滤", "group": "Features",
         "type": "toggle", "source": "yaml", "default": "false"},
        {"key": "preferred_language", "label": "显示语言", "group": "Features", "type": "select",
         "options": ["zh", "en"], "source": "yaml", "default": "zh"},
        {"key": "auto_recap_enabled", "label": "自动回顾", "group": "Features",
         "type": "toggle", "source": "yaml", "default": "true"},
        {"key": "evolution_auto_scan", "label": "自动扫描技能", "group": "Features",
         "type": "toggle", "source": "env", "default": "false"},
        # Auto-Harness (定时任务配置) - 合并为三项
        {"key": "auto_harness_git_user_name", "label": "用户名", "group": "Auto-Harness",
         "type": "string", "source": "yaml", "default": empty,
         "description": "GitCode用户名，用于 git commit、创建 PR"},
        {"key": "auto_harness_git_user_email", "label": "邮箱", "group": "Auto-Harness",
         "type": "string", "source": "yaml", "default": empty,
         "description": "GitCode用户邮箱，用于 git commit"},
        {"key": "auto_harness_gitcode_access_token", "label": "GitCode Access Token", "group": "Auto-Harness",
         "type": "password", "sensitive": True, "source": "yaml", "default": empty,
         "description": "GitCode Access token，也可通过环境变量 GITCODE_ACCESS_TOKEN 配置"},
    ]


def _normalize_provider_value(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return normalized

    available_model_providers = [provider.value for provider in ProviderType]
    lookup = {provider.lower(): provider for provider in available_model_providers}
    return lookup.get(normalized.lower(), normalized)



async def _clear_agent_config_cache(agent_client=None, user_id=None) -> None:
    try:
        if agent_client is not None:
            from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
            from jiuwenswarm.common.schema.message import ReqMethod
            import uuid

            env = e2a_from_agent_fields(
                request_id=f"cfg-reload-{uuid.uuid4().hex[:8]}",
                channel_id="",
                req_method=ReqMethod.AGENT_RELOAD_CONFIG,
                user_id=user_id,
            )
            await _send_tui_agent_request(
                _resolve_agent_client(agent_client),
                env,
                label="config.cache_clear",
            )
        else:
            get_config()
    except Exception as e:  # noqa: BLE001
        logger.debug("[cli config.set] clear agent config cache skipped: %s", e)


def _persist_env_updates(updates: dict[str, str]) -> None:
    from jiuwenswarm.common.utils import get_env_file

    env_path = get_env_file()
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
                    new_lines.append(
                        f'{env_key}="{value}"\n' if value else f"{env_key}=\n"
                    )
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
        logger.warning("[cli config.set] 写回 .env 失败: %s", e)


def _load_env_from_file() -> dict[str, str]:
    """从 .env 文件读取环境变量值（不从当前 os.environ 读取）。"""
    from jiuwenswarm.common.utils import get_env_file

    env_path = get_env_file()
    result = {}
    if not env_path.is_file():
        return result
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if "=" in stripped:
                    key, _, val = stripped.partition("=")
                    val = val.strip('"').strip("'")
                    result[key] = val
    except OSError:
        pass
    return result


def resolve_tui_session_project_path(session: dict[str, Any] | None) -> str:
    """解析 TUI session 的项目路径，供 /resume current-dir 过滤与展示。

    优先 ``channel_metadata.project_dir`` / ``cwd``（与历史 chat 落盘一致），
    回退顶层 ``project_dir``（``session.create`` / ``/clear`` 写入）。
    修复 Issue #2503：创建后尚未发聊时 channel_metadata 为空导致 current-dir 漏列。
    """
    if not isinstance(session, dict):
        return ""
    ch_meta = session.get("channel_metadata")
    if isinstance(ch_meta, dict):
        for key in ("project_dir", "cwd"):
            raw = ch_meta.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    top = session.get("project_dir")
    if isinstance(top, str) and top.strip():
        return top.strip()
    return ""


def tui_session_matches_project_dir(
    session: dict[str, Any] | None,
    project_dir: str,
    *,
    show_all_projects: bool = False,
) -> bool:
    """判断 session 是否属于 ``project_dir``（current-dir resume 过滤）。"""
    if show_all_projects:
        return True
    project_dir = str(project_dir or "").strip()
    if not project_dir:
        return True
    session_project = resolve_tui_session_project_path(session)
    if not session_project:
        return False
    try:
        project_dir = os.path.realpath(project_dir)
    except OSError:
        pass
    try:
        session_project = os.path.realpath(session_project)
    except OSError:
        pass
    session_norm = os.path.normcase(os.path.normpath(session_project))
    project_norm = os.path.normcase(os.path.normpath(project_dir))
    if session_norm == project_norm:
        return True
    return session_norm.startswith(project_norm + os.sep)


def build_tui_session_create_channel_metadata(
    params: dict[str, Any] | None,
    resolved_project_dir: str = "",
) -> dict[str, Any] | None:
    """为 TUI ``session.create`` 构造应同步落盘的 ``channel_metadata``。

    路径优先用项目预解析结果，否则回退请求中的 ``project_dir`` / ``cwd``。
    """
    seed = str(resolved_project_dir or "").strip()
    if not seed and isinstance(params, dict):
        for key in ("project_dir", "cwd"):
            raw = params.get(key)
            if isinstance(raw, str) and raw.strip():
                seed = raw.strip()
                break
    if not seed:
        return None
    meta: dict[str, Any] = {"project_dir": seed, "cwd": seed}
    try:
        from jiuwenswarm.common.utils import resolve_git_branch

        meta["git_branch"] = resolve_git_branch(seed)
    except Exception:  # noqa: BLE001
        logger.debug(
            "[TUI] session.create resolve_git_branch failed for %s", seed, exc_info=True
        )
    return meta


def resolve_3rdagent_switch_session_id(params: dict | None) -> str:
    """Explicit ``params.session_id`` for 3rdagent.switch (never gateway req_id fallback)."""
    if not isinstance(params, dict):
        return ""
    return str(params.get("session_id") or "").strip()


def register_cli_handlers(bind: CliHandlersBindParams) -> None:
    channel = bind.channel
    agent_client = bind.agent_client
    on_config_saved = bind.on_config_saved
    path = bind.path
    cron_controller_ref = bind.cron_controller
    from jiuwenswarm.gateway.routing.third_agent import get_unsupported_third_agent

    third_agent = bind.third_agent if bind.third_agent is not None else get_unsupported_third_agent()

    async def _config_get(ws, req_id, params, session_id):
        payload = {
            param_key: (os.getenv(env_key) or "")
            for param_key, env_key in _CLI_CONFIG_SET_ENV_MAP.items()
        }
        payload["app_version"] = __version__
        try:
            raw = get_config_raw()
            for key, val in payload.items():
                from jiuwenswarm.extensions import ExtensionRegistry

                crypto_provider = ExtensionRegistry.get_instance().get_crypto_provider()
                if (
                    "api_key" in key.lower() or "token" in key.lower()
                ) and crypto_provider:
                    payload[key] = crypto_provider.decrypt(val)
            ctx_cfg = (raw.get("react") or {}).get("context_engine_config") or {}
            payload["context_engine_enabled"] = (
                "true" if ctx_cfg.get("enabled", False) else "false"
            )
            perm_cfg = raw.get("permissions") or {}
            payload["permissions_enabled"] = (
                "true" if perm_cfg.get("enabled", False) else "false"
            )
            mem_cfg = (raw.get("memory") or {}).get("forbidden_memory_definition") or {}
            payload["memory_forbidden_enabled"] = (
                "true" if mem_cfg.get("enabled", False) else "false"
            )
            payload["preferred_language"] = raw.get("preferred_language") or "zh"
            auto_recap_cfg = raw.get("auto_recap") or {}
            payload["auto_recap_enabled"] = (
                "true" if auto_recap_cfg.get("enabled", True) else "false"
            )
            # swarmflow toggle lives at modes.team.jiuwen_team.enable_swarmflow
            _team_cfg = (raw.get("modes") or {}).get("team") or {}
            _jiuwen_team_cfg = _team_cfg.get("jiuwen_team") or {}
            _swarmflow_enabled = bool(_jiuwen_team_cfg.get("enable_swarmflow", False))
            payload["enable_swarmflow"] = "true" if _swarmflow_enabled else "false"

            # Resolve model-related fields from config.yaml.
            # When models.defaults list is in use, it is the canonical source
            # for the current model. Environment variables may be stale if the
            # model was switched via /model or Web UI without restarting gateway.
            try:
                _default_models = get_default_models()
                if _default_models:
                    _current = _default_models[0]
                    _mcc = _current.get("model_client_config") or {}
                    _model_overrides = {
                        "model": _mcc.get("model_name"),
                        "model_provider": _mcc.get("client_provider"),
                        "api_base": _mcc.get("api_base"),
                        "api_key": _mcc.get("api_key"),
                    }
                    for _k, _v in _model_overrides.items():
                        if _v:
                            payload[_k] = str(_v)
            except Exception as e:
                logger.warning("[config.get] Failed to resolve default model config: %s", e)

            # Resolve multimodal model configs (vision, video, audio)
            _multimodal_sections = {
                "vision": {
                    "vision_model": "model_name",
                    "vision_provider": "client_provider",
                    "vision_api_base": "api_base",
                    "vision_api_key": "api_key",
                },
                "video": {
                    "video_model": "model_name",
                    "video_provider": "client_provider",
                    "video_api_base": "api_base",
                    "video_api_key": "api_key",
                },
                "audio": {
                    "audio_model": "model_name",
                    "audio_provider": "client_provider",
                    "audio_api_base": "api_base",
                    "audio_api_key": "api_key",
                },
            }
            for _section_name, _key_map in _multimodal_sections.items():
                try:
                    _section = (raw.get("models") or {}).get(_section_name)
                    if isinstance(_section, dict):
                        _smcc = _section.get("model_client_config") or {}
                        for _pk, _yk in _key_map.items():
                            if not payload.get(_pk):
                                _resolved = resolve_env_vars(str(_smcc.get(_yk, ""))) if _smcc.get(_yk) else ""
                                if _resolved:
                                    payload[_pk] = _resolved
                except Exception as e:
                    logger.warning("[config.get] Failed to resolve %s model config: %s", _section_name, e)
        except Exception:
            payload.setdefault("auto_recap_enabled", "true")
            payload.setdefault("context_engine_enabled", "false")
            payload.setdefault("permissions_enabled", "false")
            payload.setdefault("memory_forbidden_enabled", "false")
            payload.setdefault("preferred_language", "zh")
        
        # Auto-Harness config values (from ~/.jiuwenswarm/auto-harness/config.yaml)
        # 合并显示：用户名、邮箱、Access Token 三项
        try:
            ah_config = _get_auto_harness_config()
            git_cfg = ah_config.get("git") or {}
            gitcode_cfg = ah_config.get("gitcode") or {}
            payload["auto_harness_git_user_name"] = git_cfg.get("user_name") or ""
            payload["auto_harness_git_user_email"] = git_cfg.get("user_email") or ""
            # Check env var first for access_token
            ah_token = os.getenv("GITCODE_ACCESS_TOKEN") or gitcode_cfg.get("access_token") or ""
            payload["auto_harness_gitcode_access_token"] = ah_token
        except Exception:
            payload.setdefault("auto_harness_git_user_name", "")
            payload.setdefault("auto_harness_git_user_email", "")
            payload.setdefault("auto_harness_gitcode_access_token", "")

        payload["schema"] = _build_config_schema()
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _config_set(ws, req_id, params, session_id):
        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST"
            )
            return
        for key, val in params.items():
            from jiuwenswarm.extensions import ExtensionRegistry

            crypto_provider = ExtensionRegistry.get_instance().get_crypto_provider()
            if ("api_key" in key.lower() or "token" in key.lower()) and crypto_provider:
                params[key] = crypto_provider.encrypt(val)

        env_updates: dict[str, str] = {}
        yaml_updated: list[str] = []
        available_model_providers = [provider.value for provider in ProviderType]

        for param_key, env_key in _CLI_CONFIG_SET_ENV_MAP.items():
            if param_key not in params:
                continue
            val = params[param_key]
            if param_key.endswith("_provider") and val:
                val = _normalize_provider_value(str(val))
                params[param_key] = val
            if (
                param_key.endswith("_provider")
                and val
                and val not in available_model_providers
            ):
                await channel.send_response(
                    ws,
                    req_id,
                    ok=False,
                    error=f"Model provider must in: {available_model_providers} ",
                    code="BAD_REQUEST",
                )
                return
            env_updates[env_key] = "" if val is None else str(val).strip()

        for param_key, setter in _CLI_CONFIG_YAML_SETTERS.items():
            if param_key not in params:
                continue
            raw_value = str(params[param_key]).strip()
            if param_key == "preferred_language":
                normalized_lang = raw_value.lower()
                if normalized_lang not in _PREFERRED_LANGUAGE_OPTIONS:
                    await channel.send_response(
                        ws,
                        req_id,
                        ok=False,
                        error=(
                            f"preferred_language must be one of "
                            f"{list(_PREFERRED_LANGUAGE_OPTIONS)}"
                        ),
                        code="BAD_REQUEST",
                    )
                    return
            try:
                if param_key == "preferred_language":
                    setter(raw_value)
                elif param_key.startswith("auto_harness_"):
                    # Auto-harness config items are strings, not toggles
                    setter(raw_value)
                else:
                    parsed = raw_value.lower() in ("true", "1", "yes")
                    setter(parsed)
                yaml_updated.append(param_key)
            except Exception as e:
                logger.warning(
                    "[cli config.set] 写回 config.yaml 失败 %s: %s", param_key, e
                )

        for env_key, value in env_updates.items():
            os.environ[env_key] = value
        # env 变量直接写 os.environ 立即生效；YAML 改动需要 agent 重启/热重载才生效
        applied_without_restart = not yaml_updated

        # ── 同步 env-only 模型/多模态/嵌入配置到 config.yaml ──
        # config.set 对 source:"env" 的配置项只更新 os.environ 和 .env，
        # 不更新 config.yaml 本体。但 command.status / command.model 等读取配置时
        # 优先从 config.yaml 对应 section 的 model_client_config 获取值。
        # 若值是硬编码（非 ${MODEL_NAME} 语法），env 变量更新无法传播。
        # 因此需将修改后的值同步写入 config.yaml 的对应 section。
        #
        # 映射关系：param_key → (yaml_path, mcc_key)
        #   models.defaults[0].model_client_config → 主模型 (model/model_provider/api_base/api_key)
        #   models.vision.model_client_config → 视觉 (vision_*)
        #   models.video.model_client_config → 视频 (video_*)
        #   models.audio.model_client_config → 音频 (audio_*)
        #   embed → 嵌入 (embed_*)

        _mcc_param_key_map = {
            "model_name": "model",
            "client_provider": "model_provider",
            "api_base": "api_base",
            "api_key": "api_key",
        }
        _multimodal_mcc_prefix_map = {
            "vision": "vision_",
            "video": "video_",
            "audio": "audio_",
        }
        _embed_param_key_map = {
            "embed_api_key": "embed_api_key",
            "embed_api_base": "embed_api_base",
            "embed_model": "embed_model",
        }

        _yaml_sections_updated: list[str] = []

        # ── 收集本次要改的 models / embed 字段 ──
        _changed_main_params = {
            pk: params[pk] for mk, pk in _mcc_param_key_map.items()
            if pk in params
        }
        _changed_mm_by_section: dict[str, dict[str, str]] = {}
        for _section_name, _prefix in _multimodal_mcc_prefix_map.items():
            _mm = {}
            for _mcc_key, _base_pk in _mcc_param_key_map.items():
                _mm_pk = _prefix + _base_pk
                if _mm_pk in params:
                    _mm[_mcc_key] = params[_mm_pk]
            if _mm:
                _changed_mm_by_section[_section_name] = _mm
        _changed_embed_params = {
            pk: params[pk] for pk, _ in _embed_param_key_map.items()
            if pk in params
        }

        # ── 单事务改 models.defaults[0] / 多模态 / embed，避免并发丢失更新 ──
        if _changed_main_params or _changed_mm_by_section or _changed_embed_params:
            def _sync_models_embed(data):
                if _changed_main_params:
                    _models = data.get("models")
                    if not isinstance(_models, dict):
                        _models = {}
                        data["models"] = _models
                    _defs = _models.get("defaults")
                    if not (isinstance(_defs, list) and _defs):
                        _defs = [{
                            "model_client_config": {
                                "api_base": "${API_BASE}",
                                "api_key": "${API_KEY}",
                                "model_name": "${MODEL_NAME}",
                                "client_provider": "${MODEL_PROVIDER}",
                            },
                            "model_config_obj": {"temperature": 0.95},
                            "is_default": True,
                        }]
                        _models["defaults"] = _defs
                        # 旧格式迁移：建 defaults 后清理冗余的 models.default 单对象键
                        _models.pop("default", None)
                    _first = _defs[0]
                    if isinstance(_first, dict):
                        _mcc = _first.get("model_client_config")
                        if not isinstance(_mcc, dict):
                            _mcc = {}
                            _first["model_client_config"] = _mcc
                        for _mcc_key, _param_key in _mcc_param_key_map.items():
                            if _param_key in _changed_main_params:
                                _val = str(_changed_main_params[_param_key]).strip()
                                if _param_key == "model_provider":
                                    _val = _normalize_provider_value(_val)
                                _mcc[_mcc_key] = _val
                        logger.info(
                            "[cli config.set] synced models.defaults[0].model_client_config: %s",
                            list(_changed_main_params.keys()),
                        )
                for _section_name, _mm in _changed_mm_by_section.items():
                    _models = data.get("models")
                    if not isinstance(_models, dict):
                        _models = {}
                        data["models"] = _models
                    _section = _models.get(_section_name)
                    if not isinstance(_section, dict):
                        _section = {}
                        _models[_section_name] = _section
                    _mcc = _section.get("model_client_config")
                    if not isinstance(_mcc, dict):
                        _mcc = {}
                        _section["model_client_config"] = _mcc
                    for _mcc_key, _val in _mm.items():
                        _val = str(_val).strip()
                        if _mcc_key == "client_provider":
                            _val = _normalize_provider_value(_val)
                        _mcc[_mcc_key] = _val
                    logger.info(
                        "[cli config.set] synced models.%s.model_client_config: %s",
                        _section_name, list(_mm.keys()),
                    )
                if _changed_embed_params:
                    _embed = data.get("embed")
                    if not isinstance(_embed, dict):
                        _embed = {}
                        data["embed"] = _embed
                    for _pk, _yaml_key in _embed_param_key_map.items():
                        if _pk in _changed_embed_params:
                            _embed[_yaml_key] = str(_changed_embed_params[_pk]).strip()
                    logger.info(
                        "[cli config.set] synced embed section: %s",
                        list(_changed_embed_params.keys()),
                    )
                return data
            try:
                update_config(_sync_models_embed)
                # 仅在写盘成功后登记改动段，避免失败时误报"需要重启"与误清缓存
                if _changed_main_params:
                    _yaml_sections_updated.append("models.defaults[0]")
                for _section_name in _changed_mm_by_section:
                    _yaml_sections_updated.append(f"models.{_section_name}")
                if _changed_embed_params:
                    _yaml_sections_updated.append("embed")
            except Exception as e:
                logger.warning("[cli config.set] failed to sync models/embed: %s", e)
                # env 变更先落盘，避免随 models/embed 失败一起丢失
                if env_updates:
                    _persist_env_updates(env_updates)
                await channel.send_response(
                    ws,
                    req_id,
                    ok=False,
                    error=f"Failed to sync models/embed to config.yaml: {e}",
                    code="CONFIG_SYNC_FAILED",
                )
                return

        if _yaml_sections_updated:
            applied_without_restart = False  # YAML 改动需要热重载才生效

        if env_updates:
            _persist_env_updates(env_updates)

        # 当 models / embed / yaml 配置改动时，后台通知 AgentServer 清缓存并热重载。
        # 必须在 send_response 之前 fire-and-forget：reload 在 AgentServer 端要重建
        # 全部 agent + session adapter（单次可达 25~44s），若同步 await 会阻塞当前
        # WebSocket 消息循环（同连接 `async for raw in ws` 串行），导致后续 config.get
        # 等本地帧排队等满 reload 超时（25s），前端 30s 超时报 request timeout: config.get。
        # 写盘（上面 setter + _persist_env_updates）已同步完成，config.get 直接读
        # config.yaml 即可立即验证；reload 仅用于 AgentServer 内存热更新，本就尽力而为，
        # 故丢后台不阻塞回包。与 _command_model._model_switch_background 对齐。
        if yaml_updated or _yaml_sections_updated:
            real_client = (
                agent_client.get("value")
                if isinstance(agent_client, dict)
                else agent_client
            )

            async def _config_set_reload_background() -> None:
                try:
                    await _clear_agent_config_cache(
                        real_client,
                        user_id=getattr(ws, "_gateway_user_id", None),
                    )
                except Exception as _e_reload:
                    logger.warning(
                        "[cli config.set] AGENT_RELOAD_CONFIG failed: %s", _e_reload
                    )

            asyncio.create_task(_config_set_reload_background())

        updated_param_keys = [
            k for k, e in _CLI_CONFIG_SET_ENV_MAP.items() if e in env_updates
        ] + yaml_updated

        # 先回包再执行 on_config_saved（含 Agent 热重载），
        # 避免 WebSocket 长时间无响应、CLI 误以为无反馈。
        await channel.send_response(
            ws,
            req_id,
            ok=True,
            payload={
                "updated": updated_param_keys,
                "applied_without_restart": applied_without_restart,
            },
        )

        if env_updates or yaml_updated:
            if on_config_saved:
                # on_config_saved 内部会 await agent.reload_config（app_gateway._on_config_saved），
                # reload 在 AgentServer 端要重建全部 agent + session adapter（全量并发下可达 25~34s）。
                # 若同步 await 会阻塞当前 WebSocket 连接的 `async for raw in ws` 串行循环，
                # 导致后续 config.get 等本地帧排队等满，前端 30s 超时报 request timeout: config.get。
                # 故丢后台 fire-and-forget，与上面 _config_set_reload_background 对齐。
                # 写盘已完成且已回包，reload 仅用于 AgentServer 内存热更新，本就尽力而为。
                async def _config_set_on_saved_background() -> None:
                    try:
                        config_payload = get_config()
                        callback_result = on_config_saved(
                            set(env_updates.keys()) | set(yaml_updated),
                            env_updates=dict(env_updates),
                            config_payload=config_payload,
                        )
                        if inspect.isawaitable(callback_result):
                            await callback_result
                    except Exception as e:  # noqa: BLE001
                        logger.warning("[cli config.set] on_config_saved failed: %s", e)

                asyncio.create_task(_config_set_on_saved_background())

    async def _config_validate_model(ws, req_id, params, session_id):
        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST"
            )
            return

        api_base = str(params.get("api_base") or "").strip()
        api_key = str(params.get("api_key") or "").strip()
        model = str(params.get("model") or "").strip()
        model_provider = _normalize_provider_value(str(params.get("model_provider") or ""))
        verify_ssl = bool(params.get("verify_ssl", False))

        if not all([api_base, api_key, model, model_provider]):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="api_base, api_key, model, and model_provider are required",
                code="BAD_REQUEST",
            )
            return

        available_model_providers = [provider.value for provider in ProviderType]
        if model_provider not in available_model_providers:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=f"Model provider must be one of: {available_model_providers}",
                code="BAD_REQUEST",
            )
            return

        if api_base.endswith("/chat/completions"):
            api_base = api_base.rsplit("/chat/completions", 1)[0]
        api_base = api_base.rstrip("/")

        model_config_obj = {"temperature": 0}
        if "reasoning_level" in params:
            model_config_obj["reasoning_level"] = params.get("reasoning_level")
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
        model_client_config = ModelClientConfig(
            client_id="config-validate",
            client_provider=model_provider,
            api_key=api_key,
            api_base=api_base,
            timeout=25.0,
            max_retries=0,
            verify_ssl=verify_ssl,
        )
        llm = Model(
            model_config=model_request_config,
            model_client_config=model_client_config,
        )

        async def _probe(max_tokens: int):
            return await llm.invoke(
                [{"role": "user", "content": "Hi"}],
                max_tokens=max_tokens,
                temperature=0,
            )

        try:
            try:
                response = await _probe(3)
            except Exception as first_exc:  # noqa: BLE001
                logger.info(
                    "[cli config.validate_model] max_tokens=3 failed, retrying with 16: %s",
                    first_exc,
                )
                response = await _probe(16)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[cli config.validate_model] LLM probe failed: %s", exc)
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc).strip() or "LLM request failed",
                code="LLM_ERROR",
            )
            return

        if hasattr(response, "content"):
            content = response.content
        elif isinstance(response, dict):
            content = response.get("content", "")
        else:
            content = str(response)
        reasoning_content = getattr(response, "reasoning_content", None) if hasattr(response,
                                                                                    "reasoning_content") else None
        has_valid_response = (isinstance(content, str) and content) or (
                isinstance(reasoning_content, str) and reasoning_content
        )
        if not has_valid_response:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="Empty response from model",
                code="LLM_ERROR",
            )
            return

        await channel.send_response(
            ws,
            req_id,
            ok=True,
            payload={
                "provider": model_provider,
                "model": model,
                "response": content.strip(),
            },
        )

    async def _session_list(ws, req_id, params, session_id, user_id=None):
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenswarm.common.schema.message import ReqMethod

        limit = 20
        if isinstance(params, dict):
            raw_limit = params.get("limit")
            if isinstance(raw_limit, int):
                limit = raw_limit
            elif isinstance(raw_limit, str) and raw_limit.strip().isdigit():
                limit = int(raw_limit.strip())
        limit = max(1, min(limit, 200))

        real_client = _resolve_agent_client(agent_client)
        if real_client is None:
            await channel.send_response(
                ws, req_id, ok=True, payload={"sessions": []}
            )
            return
        env = e2a_from_agent_fields(
            request_id=req_id,
            channel_id="tui",
            session_id=session_id,
            req_method=ReqMethod.SESSION_LIST,
            params=params or {},
            is_stream=False,
            timestamp=time.time(),
            user_id=user_id,
        )
        try:
            resp = await _send_tui_agent_request(
                real_client, env, label="session.list",
            )
        except AgentRequestTimeoutError:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=AGENT_SERVER_TIMEOUT_ERROR,
                code=AGENT_SERVER_TIMEOUT_CODE,
            )
            return
        if not resp.ok:
            await channel.send_response(ws, req_id, ok=False, error="session.list failed")
            return
        all_sessions = (
            resp.payload.get("sessions", [])
            if isinstance(resp.payload, dict)
            else []
        )
        # 过滤掉 None/非 dict/无效 session_id，防止前端 SelectList.render() 崩溃
        normalized_sessions = []
        for s in all_sessions:
            if not s or not isinstance(s, dict):
                continue
            raw_sid = s.get("session_id")
            if isinstance(raw_sid, str):
                session_id = raw_sid.strip()
            elif raw_sid is not None:
                session_id = str(raw_sid).strip()
            else:
                session_id = ""
            if not session_id:
                continue
            s["session_id"] = session_id
            normalized_sessions.append(s)
        all_sessions = normalized_sessions
        # 按项目目录过滤 + 排除当前会话（对齐 /resume 行为）
        # all_projects=True 时跳过项目过滤，列出所有项目的会话（Ctrl+A）
        show_all_projects = (
            bool(params.get("all_projects"))
            if isinstance(params, dict) else False
        )
        project_dir = (
            str(params.get("project_dir", "")).strip()
            if isinstance(params, dict) else ""
        )
        # 规范化路径以处理 macOS 符号链接（如 /tmp → /private/tmp）
        if project_dir:
            try:
                project_dir = os.path.realpath(project_dir)
            except OSError:
                pass
        current_sid = str(session_id or "").strip()

        cli_sessions = []
        for s in all_sessions:
            if s.get("channel_id", "") != "tui":
                continue
            if not tui_session_matches_project_dir(
                s, project_dir, show_all_projects=show_all_projects
            ):
                continue
            if s.get("session_id", "") == current_sid:
                continue
            cli_sessions.append(s)
        # 按 last_message_at 降序排序（最近活跃优先）
        cli_sessions.sort(
            key=lambda s: s.get("last_message_at", 0) or 0, reverse=True
        )
        cli_sessions = cli_sessions[:limit]

        # 附带每个会话的 project_dir / git_branch 供前端判断跨项目恢复 + 按分支过滤
        for s in cli_sessions:
            ch_meta = s.get("channel_metadata") if isinstance(s.get("channel_metadata"), dict) else {}
            sp = resolve_tui_session_project_path(s)
            if sp:
                try:
                    sp = os.path.realpath(sp)
                except OSError:
                    pass
            s["project_dir"] = sp
            # 会话首条消息时记录的分支；存量会话无该字段时回填空串（前端按"兜底显示"处理）
            s["git_branch"] = str(ch_meta.get("git_branch") or "").strip()

        # 标记已在其他 TUI 窗口中打开的会话，供前端拦截冲突的 /resume
        try:
            active_session_ids = channel.get_active_session_ids("tui", exclude_ws=ws)
        except Exception:
            logger.warning(
                "[tui] session.list: get_active_session_ids failed, active_in_window degraded",
                exc_info=True,
            )
            active_session_ids = set()
        for s in cli_sessions:
            if s.get("session_id") in active_session_ids:
                s["active_in_window"] = True

        # 当前项目的 git 分支，供前端 Ctrl+B 过滤对比（非 git/失败为哨兵 "HEAD"）
        from jiuwenswarm.common.utils import resolve_git_branch

        current_branch = resolve_git_branch(project_dir or None)

        await channel.send_response(
            ws,
            req_id,
            ok=True,
            payload={"sessions": cli_sessions, "current_branch": current_branch},
        )

    async def _session_create(ws, req_id, params, session_id, user_id=None):
        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST"
            )
            return
        real_client = _resolve_agent_client(agent_client)
        if real_client is None:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="AgentServer is unavailable",
                code="SERVICE_UNAVAILABLE",
            )
            return
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenswarm.common.schema.message import ReqMethod

        create_params = dict(params)
        requested_session_id = str(create_params.get("session_id") or "").strip()
        if requested_session_id:
            # TUI --session compatibility: preserve the supplied ID and let
            # AgentServer resolve/persist its authoritative project binding.
            create_params["session_id"] = requested_session_id
        else:
            create_params.pop("session_id", None)
            from jiuwenswarm.server.runtime.session.project_store import (
                find_or_create_code_project_for_tui_params,
            )
            project = find_or_create_code_project_for_tui_params(create_params)
            if project is not None:
                create_params["project_id"] = project.project_id
                create_params["project_dir"] = project.project_dir
                create_params["work_mode"] = project.work_mode
            create_params.setdefault("create_token", secrets.token_hex(16))
        env = e2a_from_agent_fields(
            request_id=req_id,
            channel_id="tui",
            req_method=ReqMethod.SESSION_CREATE,
            params=create_params,
            is_stream=False,
            timestamp=time.time(),
            user_id=user_id or getattr(ws, "_gateway_user_id", None),
        )
        try:
            response = await _send_tui_agent_request(
                real_client, env, label="session.create"
            )
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

    async def _session_delete(ws, req_id, params, session_id, user_id=None):
        from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenswarm.common.schema.message import ReqMethod

        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST"
            )
            return
        target = str(params.get("session_id") or "").strip()
        if not target:
            await channel.send_response(
                ws, req_id, ok=False, error="session_id is required", code="BAD_REQUEST"
            )
            return
        real_client = (
            agent_client.get("value")
            if isinstance(agent_client, dict)
            else agent_client
        )
        if real_client is not None:
            try:
                env = e2a_from_agent_fields(
                    request_id=req_id,
                    channel_id="tui",
                    session_id=session_id,
                    req_method=ReqMethod.SESSION_DELETE,
                    params=params,
                    is_stream=False,
                    timestamp=time.time(),
                    user_id=user_id,
                )
                resp = await _send_tui_agent_request(
                    real_client, env, label="session.delete",
                )
                if resp.ok:
                    pl = resp.payload if isinstance(resp.payload, dict) else {}
                    await channel.send_response(ws, req_id, ok=True, payload=pl)
                    return
                pl = resp.payload if isinstance(resp.payload, dict) else {}
                err = pl.get("error", "session.delete failed")
                code = pl.get("code") or None
                if isinstance(code, str) and not code.strip():
                    code = None
                await channel.send_response(
                    ws,
                    req_id,
                    ok=False,
                    error=str(err),
                    code=code,
                )
                return
            except Exception as e:
                logger.warning("[cli session.delete] forward to agent failed, fallback local: %s", e)

        metadata = get_session_metadata(target)
        if str(metadata.get("mode") or "").strip().lower() == "team":
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="team session delete requires agent server",
                code="AGENT_UNAVAILABLE",
            )
            return
        from jiuwenswarm.common.utils import get_agent_sessions_dir
        from jiuwenswarm.server.runtime.session.session_history import resolve_session_dir

        session_dir, invalid_reason = resolve_session_dir(
            target, sessions_root=get_agent_sessions_dir()
        )
        if session_dir is None:
            await channel.send_response(
                ws, req_id, ok=False, error=invalid_reason or "invalid session_id", code="BAD_REQUEST"
            )
            return
        if not session_dir.exists():
            await channel.send_response(
                ws, req_id, ok=False, error="session not found", code="NOT_FOUND"
            )
            return
        if not session_dir.is_dir():
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="session is not a directory",
                code="BAD_REQUEST",
            )
            return

        from jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle import (
            evict_session_kv_cache,
        )

        try:
            await evict_session_kv_cache(
                session_id=target,
                parent_session_id=target,
            )
        except Exception as exc:
            logger.warning(
                "[cli session.delete] KV cache evict hook failed during local fallback; "
                "continuing: session_id=%s error=%s",
                target,
                exc,
            )
        shutil.rmtree(session_dir)
        await channel.send_response(ws, req_id, ok=True, payload={"session_id": target})

    async def _forward_rewind_e2a(params: ForwardRewindE2AParams) -> bool:
        """Try to forward a rewind request to AgentServer via E2A.

        Returns True if the request was successfully handled by AgentServer.
        Returns False if E2A is unavailable or AgentServer returned an error,
        so the caller should fall back to local-only processing.
        """
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields

        real_client = _resolve_agent_client(agent_client)
        if real_client is None:
            return False

        try:
            env = e2a_from_agent_fields(
                request_id=params.req_id,
                channel_id="tui",
                session_id=params.target_sid,
                req_method=params.req_method,
                params={"session_id": params.target_sid, "turn_index": params.turn_index},
                is_stream=False,
                timestamp=time.time(),
                user_id=params.user_id,
            )
            resp = await _send_tui_agent_request(
                real_client, env, label=params.error_label,
            )
            if resp.ok:
                pl = resp.payload if isinstance(resp.payload, dict) else {}
                await channel.send_response(params.ws, params.req_id, ok=True, payload=pl)
                return True
            pl = resp.payload if isinstance(resp.payload, dict) else {}
            err = pl.get("error", params.error_label)
            logger.warning("[cli %s] AgentServer returned error, fallback local: %s", params.error_label, err)
            return False
        except Exception as e:
            logger.warning("[cli %s] forward to agent failed, fallback local: %s", params.error_label, e)
            return False

    async def _compact_partial_via_e2a(
        target_sid: str,
        turn_index: int,
        direction: str,
        user_id: str | None = None,
    ) -> tuple[Optional[str], int]:
        """通过 E2A 转发 LLM 摘要请求到 AgentServer。返回 (summary, summarized_count)。"""
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenswarm.common.schema.message import ReqMethod

        real_client = _resolve_agent_client(agent_client)
        if real_client is None:
            return None, 0

        try:
            env = e2a_from_agent_fields(
                request_id=str(time.time()),
                channel_id="tui",
                session_id=target_sid,
                req_method=ReqMethod.COMMAND_COMPACT_PARTIAL,
                params={
                    "session_id": target_sid,
                    "turn_index": turn_index,
                    "direction": direction,
                },
                is_stream=False,
                timestamp=time.time(),
                user_id=user_id,
            )
            resp = await _send_tui_agent_request(
                real_client, env, label="command.compact_partial",
            )
            if resp.ok:
                pl = resp.payload if isinstance(resp.payload, dict) else {}
                summary = pl.get("summary") if pl.get("status") == "ok" else None
                summarized_count = pl.get("summarized_count", 0)
                return summary, summarized_count
            logger.warning("[compact_partial_via_e2a] E2A failed: %s", resp.payload)
        except Exception as e:
            logger.warning("[compact_partial_via_e2a] E2A call failed: %s", e)

        return None, 0

    async def _session_rewind(ws, req_id, params, session_id, user_id=None):
        """session.rewind: E2A → AgentServer（权威写入者），fallback 本地."""
        from jiuwenswarm.agents.harness.common.session_ops_service import rewind_session
        from jiuwenswarm.common.schema.message import ReqMethod

        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST"
            )
            return
        target_sid = str(params.get("session_id") or session_id or "").strip()
        turn_index = params.get("turn_index")
        if not target_sid:
            await channel.send_response(
                ws, req_id, ok=False, error="session_id is required", code="BAD_REQUEST"
            )
            return
        if turn_index is None:
            await channel.send_response(
                ws, req_id, ok=False, error="turn_index is required", code="BAD_REQUEST"
            )
            return
        try:
            turn_index = int(turn_index)
        except (ValueError, TypeError):
            await channel.send_response(
                ws, req_id, ok=False, error="turn_index must be an integer", code="BAD_REQUEST"
            )
            return

        if await _forward_rewind_e2a(
            ForwardRewindE2AParams(
                ws=ws,
                req_id=req_id,
                target_sid=target_sid,
                turn_index=turn_index,
                req_method=ReqMethod.SESSION_REWIND,
                error_label="session.rewind failed",
                user_id=user_id,
            )
        ):
            return

        try:
            result = rewind_session(session_id=target_sid, turn_index=turn_index)
            await channel.send_response(ws, req_id, ok=True, payload=result)
        except ValueError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="BAD_REQUEST")
        except Exception as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _history_list_turns(ws, req_id, params, session_id):
        from jiuwenswarm.agents.harness.common.session_ops_service import list_session_turns

        if not isinstance(params, dict):
            params = {}
        target_sid = str(params.get("session_id") or session_id or "").strip()
        if not target_sid:
            await channel.send_response(
                ws, req_id, ok=False, error="session_id is required", code="BAD_REQUEST"
            )
            return
        try:
            result = list_session_turns(session_id=target_sid)
            await channel.send_response(ws, req_id, ok=True, payload=result)
        except Exception as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _session_rewind_and_restore(ws, req_id, params, session_id, user_id=None):
        """session.rewind_and_restore: E2A → AgentServer（权威写入者），fallback 本地."""
        from jiuwenswarm.agents.harness.common.session_ops_service import (
            restore_session_files,
            rewind_session,
        )
        from jiuwenswarm.common.schema.message import ReqMethod

        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST"
            )
            return
        target_sid = str(params.get("session_id") or session_id or "").strip()
        turn_index = params.get("turn_index")
        if not target_sid:
            await channel.send_response(
                ws, req_id, ok=False, error="session_id is required", code="BAD_REQUEST"
            )
            return
        if turn_index is None:
            await channel.send_response(
                ws, req_id, ok=False, error="turn_index is required", code="BAD_REQUEST"
            )
            return
        try:
            turn_index = int(turn_index)
        except (ValueError, TypeError):
            await channel.send_response(
                ws, req_id, ok=False, error="turn_index must be an integer", code="BAD_REQUEST"
            )
            return

        if await _forward_rewind_e2a(
            ForwardRewindE2AParams(
                ws=ws,
                req_id=req_id,
                target_sid=target_sid,
                turn_index=turn_index,
                req_method=ReqMethod.SESSION_REWIND_AND_RESTORE,
                error_label="session.rewind_and_restore failed",
                user_id=user_id,
            )
        ):
            return

        try:
            restore_result = restore_session_files(session_id=target_sid, turn_index=turn_index)
            rewind_result = rewind_session(session_id=target_sid, turn_index=turn_index)
            combined = {
                **rewind_result,
                "restored_files": restore_result.get("restored_files", []),
                "deleted_files": restore_result.get("deleted_files", []),
                "restore_errors": restore_result.get("errors", []),
            }
            await channel.send_response(ws, req_id, ok=True, payload=combined)
        except ValueError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="BAD_REQUEST")
        except Exception as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _session_restore_files(ws, req_id, params, session_id):
        """session.restore_files: 仅恢复文件，不截断对话."""
        from jiuwenswarm.agents.harness.common.session_ops_service import restore_session_files

        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST"
            )
            return
        target_sid = str(params.get("session_id") or session_id or "").strip()
        turn_index = params.get("turn_index")
        if not target_sid:
            await channel.send_response(
                ws, req_id, ok=False, error="session_id is required", code="BAD_REQUEST"
            )
            return
        if turn_index is None:
            await channel.send_response(
                ws, req_id, ok=False, error="turn_index is required", code="BAD_REQUEST"
            )
            return
        try:
            turn_index = int(turn_index)
        except (ValueError, TypeError):
            await channel.send_response(
                ws, req_id, ok=False, error="turn_index must be an integer", code="BAD_REQUEST"
            )
            return
        try:
            result = restore_session_files(session_id=target_sid, turn_index=turn_index)
            await channel.send_response(ws, req_id, ok=True, payload=result)
        except ValueError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="BAD_REQUEST")
        except Exception as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _command_rewind_compact(ws, req_id, params, session_id, user_id=None):
        """command.rewind_compact: LLM 摘要(E2A→AgentServer) + 截断 + 记录写入(AgentServer E2A)。"""
        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST"
            )
            return
        target_sid = str(params.get("session_id") or session_id or "").strip()
        turn_index = params.get("turn_index")
        direction = str(params.get("direction") or "from").strip()
        if not target_sid:
            await channel.send_response(
                ws, req_id, ok=False, error="session_id is required", code="BAD_REQUEST"
            )
            return
        if turn_index is None:
            await channel.send_response(
                ws, req_id, ok=False, error="turn_index is required", code="BAD_REQUEST"
            )
            return
        try:
            turn_index = int(turn_index)
        except (ValueError, TypeError):
            await channel.send_response(
                ws, req_id, ok=False, error="turn_index must be an integer", code="BAD_REQUEST"
            )
            return
        if direction not in ("from", "up_to"):
            await channel.send_response(
                ws, req_id, ok=False, error="direction must be 'from' or 'up_to'", code="BAD_REQUEST"
            )
            return

        try:
            llm_summary, summarized_count = await _compact_partial_via_e2a(
                target_sid, turn_index, direction, user_id=user_id
            )
        except Exception as e:
            logger.warning("[cli command.rewind_compact] LLM summary failed: %s", e)
            llm_summary = None
            summarized_count = 0

        # Step 2: Send rewind to AgentServer (truncation + agent-internal record writing)
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenswarm.common.schema.message import ReqMethod

        real_client = (
            agent_client.get("value")
            if isinstance(agent_client, dict)
            else agent_client
        )
        if real_client is not None:
            try:
                env = e2a_from_agent_fields(
                    request_id=req_id,
                    channel_id="tui",
                    session_id=target_sid,
                    req_method=ReqMethod.SESSION_REWIND_COMPACT,
                    params={
                        "session_id": target_sid,
                        "turn_index": turn_index,
                        "direction": direction,
                        "compact_summary": llm_summary,
                        "summarized_count": summarized_count,
                    },
                    is_stream=False,
                    timestamp=time.time(),
                    user_id=user_id,
                )
                resp = await _send_tui_agent_request(
                    real_client, env, label="command.rewind_compact",
                )
                if resp.ok:
                    pl = resp.payload if isinstance(resp.payload, dict) else {}
                    pl["summary"] = llm_summary
                    pl["summarized_messages"] = summarized_count
                    await channel.send_response(ws, req_id, ok=True, payload=pl)
                    return
                logger.warning("[cli command.rewind_compact] E2A failed: %s", resp.payload)
            except Exception as e:
                logger.warning("[cli command.rewind_compact] E2A failed, fallback local: %s", e)

        # Fallback: local truncation + record writing
        try:
            from jiuwenswarm.agents.harness.common.session_ops_service import compact_partial_session
            result = compact_partial_session(
                session_id=target_sid,
                turn_index=turn_index,
                direction=direction,
                llm_summary=llm_summary,
            )
            result["summary"] = llm_summary
            result["summarized_messages"] = summarized_count
            await channel.send_response(ws, req_id, ok=True, payload=result)
        except ValueError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="BAD_REQUEST")
        except Exception as e:
            logger.exception("[cli command.rewind_compact] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _session_rename(ws, req_id, params, session_id, user_id=None):
        """优先经 E2A 转发至 AgentWebSocketServer._handle_session_rename；无 agent 或转发失败时本地回退。"""
        from jiuwenswarm.server.runtime.session.session_rename import apply_session_rename
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenswarm.common.schema.message import ReqMethod

        real_client = _resolve_agent_client(agent_client)
        if real_client is not None:
            try:
                env = e2a_from_agent_fields(
                    request_id=req_id,
                    channel_id="tui",
                    session_id=session_id,
                    req_method=ReqMethod.SESSION_RENAME,
                    params=params if isinstance(params, dict) else {},
                    is_stream=False,
                    timestamp=time.time(),
                    user_id=user_id,
                )
                resp = await _send_tui_agent_request(
                    real_client, env, label="session.rename",
                )
                if resp.ok:
                    pl = resp.payload if isinstance(resp.payload, dict) else {}
                    await channel.send_response(ws, req_id, ok=True, payload=pl)
                    return
                pl = resp.payload if isinstance(resp.payload, dict) else {}
                err = pl.get("error", "session.rename failed")
                code = pl.get("code") or None
                if isinstance(code, str) and not code.strip():
                    code = None
                await channel.send_response(
                    ws, req_id, ok=False, error=str(err), code=code
                )
                return
            except Exception as e:
                logger.warning(
                    "[cli session.rename] forward to agent failed, fallback local: %s",
                    e,
                )

        ok, payload, err, code = apply_session_rename(
            params,
            session_id,
            init_channel_id="tui",
        )
        if ok:
            await channel.send_response(ws, req_id, ok=True, payload=payload or {})
        else:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=err or "session.rename failed",
                code=code,
            )

    async def _session_color_set(ws, req_id, params, session_id):
        """设置 session 的 accent_color。"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
            _write_metadata_sync,
            _read_metadata,
        )

        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST"
            )
            return
        target = str(params.get("session_id") or session_id).strip()
        if not target:
            await channel.send_response(
                ws, req_id, ok=False, error="session_id is required", code="BAD_REQUEST"
            )
            return

        color = params.get("color")
        valid_colors = ["default", "blue", "green", "pink", "purple", "red", "yellow"]

        if color is None:
            # 查询模式
            metadata = get_session_metadata(target)
            accent_color = metadata.get("accent_color", "default") if metadata else "default"
            await channel.send_response(
                ws, req_id, ok=True,
                payload={"session_id": target, "accent_color": accent_color}
            )
            return

        if str(color) not in valid_colors:
            await channel.send_response(
                ws, req_id, ok=False, error=f"invalid color: {color}", code="BAD_REQUEST"
            )
            return

        # 设置模式 - 同步写入确保跨进程可见
        metadata = _read_metadata(target)
        metadata["accent_color"] = str(color)
        _write_metadata_sync(target, metadata)
        await channel.send_response(
            ws, req_id, ok=True,
            payload={"session_id": target, "accent_color": str(color)}
        )

    async def _session_preview(ws, req_id, params, session_id):
        """获取 session 预览信息，包括最新几条完整对话内容。"""
        from jiuwenswarm.server.runtime.session.session_history import load_history_records

        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST"
            )
            return
        target = str(params.get("session_id") or session_id).strip()
        if not target:
            await channel.send_response(
                ws, req_id, ok=False, error="session_id is required", code="BAD_REQUEST"
            )
            return

        # 预览消息数量，默认 30 条
        preview_count = 30
        raw_count = params.get("count")
        if isinstance(raw_count, int):
            preview_count = max(1, min(raw_count, 100))
        elif isinstance(raw_count, str) and raw_count.strip().isdigit():
            preview_count = max(1, min(int(raw_count.strip()), 100))

        # 读取历史记录（自动兼容 history.json 和 history.jsonl）
        preview_messages = []
        try:
            raw = load_history_records(target)
            if isinstance(raw, list):
                # history.json 的写入很宽松：interface.py 中所有 event_type 以 "chat." 开头
                # 的记录（外加 team.message）都会以 role="assistant" 落盘，因此历史里混有大量
                # 非对话内容。预览只需展示真正代表"对话"的两类，用白名单显式放行：
                #   - chat.final ：assistant 的完整最终回复（单人模式与团队成员回复都用它）
                #   - team.message：团队消息
                # 刻意排除（均非完整对话文本，纳入会造成碎片化/重复/噪声）：
                #   - chat.delta（流式增量片段，内容已包含在 chat.final 中）
                #   - chat.reasoning（思考过程）/ chat.tool_call / chat.tool_update / chat.tool_result
                #   - chat.error / chat.processing_status / chat.usage_* / chat.media / chat.file 等
                # 注：用白名单而非黑名单，是为了让将来新增的 chat.* 状态类型不会意外混入预览。
                _chat_event_types = frozenset({
                    "chat.final",  # assistant 最终回复
                    "team.message",  # team 消息
                })

                def _is_previewable(item):
                    if not isinstance(item, dict):
                        return False
                    role = item.get("role")
                    content = item.get("content")
                    has_content = isinstance(content, str) and bool(content.strip())
                    if role == "user":
                        return has_content
                    # 非 user 记录只放行白名单内的对话类型（不依赖 role，
                    # 以兼容团队成员回复可能带 teammate 等非 assistant role 的情况）
                    event_type = item.get("event_type")
                    if event_type in _chat_event_types:
                        return has_content
                    return False

                previewable = [item for item in raw if _is_previewable(item)]
                # 取时间顺序下最新的 N 条（保持原顺序：旧的在上、新的在下）
                recent = previewable[-preview_count:]
                for msg in recent:
                    role = msg.get("role", "unknown")
                    content = msg.get("content", "")
                    event_type = msg.get("event_type", "")
                    preview_messages.append({
                        "role": role,
                        "content": content if isinstance(content, str) else "",
                        "event_type": event_type,
                    })
        except Exception as exc:
            logger.warning("[session.preview] read history failed: %s", exc)

        await channel.send_response(
            ws, req_id, ok=True,
            payload={"session_id": target, "preview_messages": preview_messages}
        )

    async def _chat_send(ws, req_id, params, session_id):
        await channel.send_response(
            ws, req_id, ok=True, payload={"accepted": True, "session_id": session_id}
        )

    async def _chat_resume(ws, req_id, params, session_id):
        await channel.send_response(
            ws, req_id, ok=True, payload={"accepted": True, "session_id": session_id}
        )

    async def _chat_interrupt(ws, req_id, params, session_id):
        intent = params.get("intent") if isinstance(params, dict) else None
        payload = {"accepted": True, "session_id": session_id}
        if isinstance(intent, str) and intent:
            payload["intent"] = intent
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _tui_disconnect_request(ws, req_id, params, session_id):
        payload = {"accepted": True, "session_id": session_id}
        try:
            await channel.send_response(ws, req_id, ok=True, payload=payload)
        except Exception:
            logger.debug("[tui.disconnect] response skipped on closed ws", exc_info=True)

        mh = bind.message_handler
        sid = (session_id or "").strip()
        owns_session = True
        is_bound_to_client = getattr(channel, "is_session_bound_to_client", None)
        if callable(is_bound_to_client):
            owns_session = bool(is_bound_to_client("tui", sid, ws))
        if mh is not None and sid and owns_session:
            cleaned = await mh.cancel_agent_sessions_on_disconnect(
                [("tui", sid)],
                user_id=getattr(ws, "_gateway_user_id", None),
            )
            if not cleaned:
                logger.warning(
                    "[tui.disconnect] immediate cleanup failed; "
                    "transport-close fallback remains enabled: session_id=%s",
                    sid,
                )
                return
            # Only suppress the transport-close fallback after the immediate
            # cleanup has completed.  The TUI process can disappear after the
            # acknowledgement and cancel this handler; marking the websocket
            # earlier would make _tui_disconnect skip the only remaining
            # cleanup path and leak the session runtime.
            try:
                setattr(ws, "_jiuwenswarm_tui_user_exit", True)
            except Exception:
                logger.debug(
                    "[tui.disconnect] mark completed user exit failed",
                    exc_info=True,
                )

    async def _chat_user_answer(ws, req_id, params, session_id):
        payload = {"accepted": True, "session_id": session_id}
        request_id = params.get("request_id") if isinstance(params, dict) else None
        if isinstance(request_id, str) and request_id:
            payload["request_id"] = request_id
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _chat_swarmflow_reply(ws, req_id, params, session_id):
        # Empty-ack shell — standard 3-layer routing forwards the reply to the
        # agent adapter, which builds HumanAgentMessage and calls team_manager.
        await channel.send_response(
            ws, req_id, ok=True, payload={"accepted": True, "session_id": session_id}
        )

    async def _history_get(ws, req_id, params, session_id):
        payload = {"accepted": True, "session_id": session_id}
        if isinstance(params, dict):
            if "session_id" in params:
                payload["session_id"] = params.get("session_id")
            if "page_idx" in params:
                payload["page_idx"] = params.get("page_idx")
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _command_model(ws, req_id, params, session_id, user_id=None):
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenswarm.common.schema.message import ReqMethod

        if not isinstance(params, dict):
            params = {}
        action = params.get("action")
        model_name = params.get("model")
        model_index = params.get("index")

        real_client = (
            agent_client.get("value")
            if isinstance(agent_client, dict)
            else agent_client
        )
        if real_client is None:
            await channel.send_response(
                ws, req_id, ok=False, error="agent client not available"
            )
            return

        async def _reload_model_config_background(config_payload: dict[str, Any], label: str) -> None:
            _reload_env = e2a_from_agent_fields(
                request_id=req_id,
                channel_id="cli",
                session_id=session_id,
                req_method=ReqMethod.AGENT_RELOAD_CONFIG,
                params={"config": config_payload, "env": {}},
                is_stream=False,
                timestamp=time.time(),
                user_id=user_id,
            )
            try:
                await _send_tui_agent_request(
                    real_client, _reload_env, label=f"command.model.{label}",
                )
            except Exception as _e_reload:
                logger.warning("[cli command.model] %s AGENT_RELOAD_CONFIG failed: %s", label, _e_reload)
            if on_config_saved:
                try:
                    _cb = on_config_saved(set(), env_updates={}, config_payload=config_payload)
                    if inspect.isawaitable(_cb):
                        await _cb
                except Exception as _e_saved:
                    logger.warning("[cli command.model] %s on_config_saved failed: %s", label, _e_saved)

        if action == "add_model":
            target = str(params.get("target", "")).strip()
            configs = params.get("config", {})
            if not target:
                await channel.send_response(
                    ws, req_id, ok=False, error="Target model name (target) is required"
                )
                return
            client_cfg = {}
            model_config_obj = configs.get("model_config_obj", {})
            if not isinstance(model_config_obj, dict):
                model_config_obj = {}
            key_map = {
                "model": "model_name",
                "model_name": "model_name",
                "provider": "client_provider",
                "model_provider": "client_provider",
                "client_provider": "client_provider",
                "reasoning_level": "reasoning_level",
                "api_key": "api_key",
                "key": "api_key",
                "api_base": "api_base",
                "url": "api_base",
                "base_url": "api_base",
                "timeout": "timeout",
                "verify_ssl": "verify_ssl",
                "ssl_cert": "ssl_cert",
                "alias": "alias",
            }
            # target 可能是 "model=gpt-5" 形式（前端把第一个 key=value 当作 name 参数解析）
            if "=" in target:
                _eq = target.index("=")
                _k, _v = target[:_eq].strip().lower(), target[_eq + 1:].strip()
                _mapped_target_key = key_map.get(_k, _k)
                if _mapped_target_key == "reasoning_level":
                    if _v:
                        model_config_obj["reasoning_level"] = _v
                else:
                    client_cfg[_mapped_target_key] = _v
                if _k in ("model", "model_name"):
                    target = _v
            for k, v in configs.items():
                mapped_k = key_map.get(str(k).lower(), str(k))
                if mapped_k == "model_config_obj":
                    continue
                if mapped_k == "reasoning_level":
                    if str(v).strip():
                        model_config_obj["reasoning_level"] = str(v).strip()
                    else:
                        model_config_obj.pop("reasoning_level", None)
                    continue
                client_cfg[mapped_k] = v
            if "verify_ssl" not in client_cfg:
                client_cfg["verify_ssl"] = False
            if "timeout" not in client_cfg:
                client_cfg["timeout"] = 1800
            if "temperature" not in model_config_obj:
                model_config_obj["temperature"] = 0.95
            _reasoning_level = str(model_config_obj.get("reasoning_level", "")).strip()
            if _reasoning_level and _reasoning_level not in {"off", "low", "medium", "high"}:
                await channel.send_response(
                    ws,
                    req_id,
                    ok=False,
                    error="reasoning_level must be one of: off, low, medium, high",
                )
                return
            # target 作为 model_name 的回退：若未通过 model= 参数指定，则以 target 为准
            if not client_cfg.get("model_name"):
                client_cfg["model_name"] = target
            effective_name = client_cfg["model_name"]

            # alias 为顶层字段，从 client_cfg 提取；提前算最终值，
            # 确保唯一性校验基于实际存储值
            entry_alias = client_cfg.pop("alias", None)
            effective_alias = str(entry_alias).strip() if entry_alias else ""

            new_entry = {
                "model_client_config": client_cfg,
                "model_config_obj": model_config_obj,
            }
            # alias 带双引号写出，避免 yes/no/on/off 被 YAML 1.1 解析为布尔
            new_entry["alias"] = DoubleQuotedScalarString(effective_alias) if effective_alias else ""
            try:
                # 单事务读-校验-改：避免 ensure+update 两步间的 TOCTOU 窗口
                def _add_mutate(data):
                    models = data.get("models")
                    if not isinstance(models, dict):
                        models = {}
                        data["models"] = models
                    _raw_defs = models.get("defaults")
                    if not (isinstance(_raw_defs, list) and _raw_defs):
                        _raw_defs = [{
                            "model_client_config": {
                                "api_base": "${API_BASE}",
                                "api_key": "${API_KEY}",
                                "model_name": "${MODEL_NAME}",
                                "client_provider": "${MODEL_PROVIDER}",
                            },
                            "model_config_obj": {"temperature": 0.95},
                            "is_default": True,
                        }]
                        models["defaults"] = _raw_defs
                        models.pop("default", None)
                    _effective_api_base = resolve_env_vars(str(client_cfg.get("api_base", "")))
                    _effective_api_key = resolve_env_vars(str(client_cfg.get("api_key", "")))
                    for _e in _raw_defs:
                        if not isinstance(_e, dict):
                            continue
                        _emn = resolve_env_vars(str((_e.get("model_client_config") or {}).get("model_name", "")))
                        _eab = resolve_env_vars(str((_e.get("model_client_config") or {}).get("api_base", "")))
                        _eak = resolve_env_vars(str((_e.get("model_client_config") or {}).get("api_key", "")))
                        if _emn == effective_name and _eab == _effective_api_base and _eak == _effective_api_key:
                            raise _ModelOpError(
                                f"Model '{effective_name}' with the same api_base and api_key already exists"
                            )
                    _missing = []
                    for field, display in {
                        "api_key": "api_key",
                        "api_base": "api_base",
                        "model_name": "model_name",
                        "client_provider": "model_provider",
                    }.items():
                        if not resolve_env_vars(str(client_cfg.get(field, ""))):
                            _missing.append(display)
                    if _missing:
                        raise _ModelOpError(
                            f"Failed to add model '{effective_name}'. "
                            f"Required fields missing: {', '.join(_missing)}. "
                            f"Usage: /model add <name> "
                            f"api_base=xxx api_key=xxx "
                            f"model=<name> model_provider=<provider>"
                        )
                    if effective_alias:
                        for _e in _raw_defs:
                            if not isinstance(_e, dict):
                                continue
                            _emn = resolve_env_vars(str((_e.get("model_client_config") or {}).get("model_name", "")))
                            _ea = resolve_env_vars(str(_e.get("alias", "")))
                            if _ea == effective_alias:
                                raise _ModelOpError(f"Alias '{effective_alias}' is already used by model '{_emn}'")
                            if _emn == effective_alias:
                                raise _ModelOpError(f"Alias '{effective_alias}' conflicts with model name '{_emn}'")
                    _raw_defs.append(new_entry)
                    return data
                update_config(_add_mutate)
                logger.info(
                    "[cli command.model] 新增模型: name=%s, "
                    "client_cfg=%s, model_config_obj=%s",
                    effective_name, client_cfg, model_config_obj,
                )
            except _ModelOpError as _op_err:
                await channel.send_response(ws, req_id, ok=False, error=str(_op_err))
                return
            except Exception as e:
                await channel.send_response(ws, req_id, ok=False, error=str(e))
                return
            _config_payload = get_config()
            await _reload_model_config_background(_config_payload, "model.add")
            await channel.send_response(
                ws, req_id, ok=True,
                payload={"type": "model_added", "name": target},
            )
            return

        if action == "update_model":
            configs = params.get("config", {})
            if not isinstance(configs, dict):
                await channel.send_response(ws, req_id, ok=False, error="config must be object")
                return
            try:
                _idx = int(model_index)
            except (ValueError, TypeError):
                await channel.send_response(ws, req_id, ok=False, error="index is required")
                return
            try:
                _update_result: dict = {}

                def _update_mutate(data):
                    models = data.get("models")
                    if not isinstance(models, dict):
                        models = {}
                        data["models"] = models
                    _raw_defs = models.get("defaults")
                    if not (isinstance(_raw_defs, list) and _raw_defs):
                        raise _ModelOpError("model index not found")
                    if _idx < 0 or _idx >= len(_raw_defs) or not isinstance(_raw_defs[_idx], dict):
                        raise _ModelOpError("model index not found")
                    _entry = _raw_defs[_idx]
                    _client_cfg = _entry.get("model_client_config")
                    if not isinstance(_client_cfg, dict):
                        _client_cfg = {}
                        _entry["model_client_config"] = _client_cfg
                    key_map = {
                        "model": "model_name", "model_name": "model_name",
                        "provider": "client_provider", "model_provider": "client_provider",
                        "client_provider": "client_provider", "reasoning_level": "reasoning_level",
                        "api_key": "api_key", "key": "api_key", "api_base": "api_base",
                        "url": "api_base", "base_url": "api_base", "timeout": "timeout",
                        "verify_ssl": "verify_ssl", "ssl_cert": "ssl_cert", "alias": "alias",
                    }
                    _model_cfg_obj = _entry.get("model_config_obj")
                    if not isinstance(_model_cfg_obj, dict):
                        _model_cfg_obj = {}
                        _entry["model_config_obj"] = _model_cfg_obj
                    for k, v in configs.items():
                        mapped_k = key_map.get(str(k).lower(), str(k))
                        if mapped_k == "alias":
                            _alias_val = str(v).strip()
                            _entry["alias"] = (
                                DoubleQuotedScalarString(_alias_val) if _alias_val else ""
                            )
                        elif mapped_k == "reasoning_level":
                            _rl = str(v).strip()
                            if _rl:
                                _model_cfg_obj["reasoning_level"] = _rl
                            else:
                                _model_cfg_obj.pop("reasoning_level", None)
                        elif mapped_k == "model_config_obj":
                            continue
                        else:
                            _client_cfg[mapped_k] = v
                    _reasoning_level = str(_model_cfg_obj.get("reasoning_level", "")).strip()
                    if _reasoning_level and _reasoning_level not in {"off", "low", "medium", "high"}:
                        raise _ModelOpError("reasoning_level must be one of: off, low, medium, high")
                    if "verify_ssl" not in _client_cfg:
                        _client_cfg["verify_ssl"] = False
                    if "timeout" not in _client_cfg:
                        _client_cfg["timeout"] = 1800
                    _missing_fields = []
                    for _req_field, _display in [
                        ("api_key", "api_key"), ("api_base", "api_base"),
                        ("model_name", "model_name"), ("client_provider", "model_provider"),
                    ]:
                        if not resolve_env_vars(str(_client_cfg.get(_req_field, ""))):
                            _missing_fields.append(_display)
                    if _missing_fields:
                        raise _ModelOpError(f"Model missing required config: {', '.join(_missing_fields)}")
                    _effective_alias = resolve_env_vars(str(_entry.get("alias", ""))) if _entry.get("alias") else ""
                    if _effective_alias:
                        for _other_idx, _other in enumerate(_raw_defs):
                            if _other_idx == _idx or not isinstance(_other, dict):
                                continue
                            _other_mcc = _other.get("model_client_config") or {}
                            _other_mn = resolve_env_vars(str(_other_mcc.get("model_name", "")))
                            _other_alias = resolve_env_vars(str(_other.get("alias", ""))) if _other.get("alias") else ""
                            if _other_alias == _effective_alias:
                                raise _ModelOpError(
                                    f"Alias '{_effective_alias}' is already used by model '{_other_mn}'"
                                )
                            if _other_mn == _effective_alias:
                                raise _ModelOpError(
                                    f"Alias '{_effective_alias}' conflicts with model name '{_other_mn}'"
                                )
                    # 展示字段从锁内 data 直接取，避免事务后再开锁读取
                    _upd_mcc = _entry.get("model_client_config") or {}
                    _update_result["updated_name"] = resolve_env_vars(str(_upd_mcc.get("model_name", "")))
                    _cur_mcc = (_raw_defs[0].get("model_client_config") or {}) if _raw_defs else {}
                    _update_result["current_name"] = resolve_env_vars(str(_cur_mcc.get("model_name", "")))
                    return data
                update_config(_update_mutate)
            except _ModelOpError as _op_err:
                await channel.send_response(ws, req_id, ok=False, error=str(_op_err))
                return
            except Exception as e:
                await channel.send_response(ws, req_id, ok=False, error=str(e))
                return
            _updated_name = _update_result.get("updated_name", "")
            _current_name = _update_result.get("current_name", "")
            _config_payload = get_config()
            await _reload_model_config_background(_config_payload, "model.update")
            await channel.send_response(ws, req_id, ok=True, payload={
                "type": "model_updated",
                "name": _updated_name,
                "index": _idx,
                "current": _current_name,
            })
            return

        if action == "delete_model":
            try:
                _idx = int(model_index)
            except (ValueError, TypeError):
                await channel.send_response(ws, req_id, ok=False, error="index is required")
                return
            _removed_holder: dict = {}
            try:
                def _delete_mutate(data):
                    models = data.get("models")
                    if not isinstance(models, dict):
                        models = {}
                        data["models"] = models
                    _raw_defs = models.get("defaults")
                    if not (isinstance(_raw_defs, list) and _raw_defs):
                        raise _ModelOpError("model index not found")
                    if len(_raw_defs) <= 1:
                        raise _ModelOpError("Cannot delete the last model")
                    if _idx < 0 or _idx >= len(_raw_defs) or not isinstance(_raw_defs[_idx], dict):
                        raise _ModelOpError("model index not found")
                    _removed_holder["entry"] = _raw_defs.pop(_idx)
                    # 展示字段从锁内 data 直接取，避免事务后再开锁读取
                    _cur_mcc = (_raw_defs[0].get("model_client_config") or {}) if _raw_defs else {}
                    _removed_holder["current_name"] = resolve_env_vars(str(_cur_mcc.get("model_name", "")))
                    return data
                update_config(_delete_mutate)
            except _ModelOpError as _op_err:
                await channel.send_response(ws, req_id, ok=False, error=str(_op_err))
                return
            except Exception as e:
                await channel.send_response(ws, req_id, ok=False, error=str(e))
                return
            _removed = _removed_holder.get("entry") or {}
            _removed_name = resolve_env_vars(str((_removed.get("model_client_config") or {}).get("model_name", "")))
            _current_name = _removed_holder.get("current_name", "")
            _config_payload = get_config()
            await _reload_model_config_background(_config_payload, "model.delete")
            await channel.send_response(ws, req_id, ok=True, payload={
                "type": "model_deleted",
                "name": _removed_name,
                "current": _current_name,
            })
            return

        if not model_name or not str(model_name).strip():
            names = get_model_names()
            logger.info(
                "[cli command.model] 列出模型: names=%s, current=%s",
                names,
                os.getenv("MODEL_NAME", "unknown"),
            )
            # 列出模型全部数据均可从本地 config.yaml 获取，
            # 无需等待 AgentServer 响应（其返回的 current/available 会被本地值覆盖）。
            # 若 await send_request() 阻塞 >30s，会导致 TUI WS 超时且后续请求排队，
            # 故直接以本地数据构建 payload 立即回包。
            payload: dict = {}
            payload["available_models"] = names
            _raw = get_config_raw()
            _defs = (_raw.get("models") or {}).get("defaults")
            if isinstance(_defs, list) and _defs:
                _first_name = resolve_env_vars(str((_defs[0].get("model_client_config") or {}).get("model_name", "")))
                _first_alias = resolve_env_vars(str(_defs[0].get("alias", ""))) if _defs[0].get("alias") else ""
                payload["current"] = _first_alias or _first_name or os.getenv("MODEL_NAME", "unknown")
                payload["current_model_name"] = _first_name or os.getenv("MODEL_NAME", "unknown")

                def _model_meta(i: int, e: dict) -> dict:
                    mcc = e.get("model_client_config") or {}
                    mco = e.get("model_config_obj") or {}
                    _alias = e.get("alias", "")
                    _resolved_alias = resolve_env_vars(str(_alias)) if _alias else ""
                    _model_name = resolve_env_vars(str(mcc.get("model_name", "")))
                    _api_key = resolve_env_vars(str(mcc.get("api_key", "")))
                    return {
                        "index": i,
                        "name": _resolved_alias or _model_name,
                        "alias": _resolved_alias,
                        "model_name": _model_name,
                        "model_provider": resolve_env_vars(str(mcc.get("client_provider", ""))),
                        "api_base": resolve_env_vars(str(mcc.get("api_base", ""))),
                        "reasoning_level": resolve_env_vars(str(mco.get("reasoning_level", ""))),
                        # 同名模型冲突时用于区分：仅展示末4位，避免泄露过多 key 信息
                        "api_key_suffix": _api_key[-4:] if _api_key else "",
                        "is_current": i == 0,
                    }

                payload["models"] = [
                    _model_meta(i, e)
                    for i, e in enumerate(_defs) if isinstance(e, dict)
                ]
            else:
                payload["current"] = os.getenv("MODEL_NAME", "unknown")
            await channel.send_response(ws, req_id, ok=True, payload=payload)
            return

        target = str(model_name).strip()
        logger.info(
            "[cli command.model] 切换模型: target=%s, model_index=%s, params=%s",
            target, model_index, params,
        )
        _switch_result: dict = {}
        try:
            def _switch_mutate(data):
                models = data.get("models")
                if not isinstance(models, dict):
                    models = {}
                    data["models"] = models
                _raw_defaults = models.get("defaults")
                if not (isinstance(_raw_defaults, list) and _raw_defaults):
                    _raw_defaults = [{
                        "model_client_config": {
                            "api_base": "${API_BASE}", "api_key": "${API_KEY}",
                            "model_name": "${MODEL_NAME}", "client_provider": "${MODEL_PROVIDER}",
                        },
                        "model_config_obj": {"temperature": 0.95}, "is_default": True,
                    }]
                    models["defaults"] = _raw_defaults
                    models.pop("default", None)
                _valid_names: set[str] = set()
                _avail_parts: list[str] = []
                for _e in _raw_defaults:
                    if not isinstance(_e, dict):
                        continue
                    _mn = resolve_env_vars(str((_e.get("model_client_config") or {}).get("model_name", "")))
                    _al = resolve_env_vars(str(_e.get("alias", ""))) if _e.get("alias") else ""
                    if _mn:
                        _valid_names.add(_mn)
                    if _al:
                        _valid_names.add(_al)
                    if _al and _mn and _al != _mn:
                        _avail_parts.append(f"{_al} ({_mn})")
                    elif _mn:
                        _avail_parts.append(_mn)
                _skip_name_check = model_index is not None
                if not _skip_name_check and target not in _valid_names:
                    logger.warning(
                        "[cli command.model] 模型不存在: %s, 可用: %s",
                        target, _avail_parts,
                    )
                    raise _ModelOpError(
                        f"Model '{target}' not found. "
                        f"Available: {', '.join(_avail_parts) or ''}"
                    )
                _target_entry = None
                _target_idx = None
                if model_index is not None:
                    try:
                        _idx = int(model_index)
                        if 0 <= _idx < len(_raw_defaults) and isinstance(_raw_defaults[_idx], dict):
                            _target_entry = _raw_defaults[_idx]
                            _target_idx = _idx
                    except (ValueError, TypeError):
                        pass
                if _target_entry is None:
                    for _i, _e in enumerate(_raw_defaults):
                        if not isinstance(_e, dict):
                            continue
                        _ename = resolve_env_vars(str((_e.get("model_client_config") or {}).get("model_name", "")))
                        _ealias = resolve_env_vars(str(_e.get("alias", ""))) if _e.get("alias") else ""
                        if _ename == target or _ealias == target:
                            _target_entry = _e
                            _target_idx = _i
                            break
                if _target_entry is None:
                    raise _ModelOpError(f"Model '{target}' config not found")
                _target_mcc = _target_entry.get("model_client_config") or {}
                _missing_fields = []
                for _req_field, _display in [
                    ("api_key", "api_key"), ("api_base", "api_base"),
                    ("model_name", "model_name"), ("client_provider", "client_provider"),
                ]:
                    if not resolve_env_vars(str(_target_mcc.get(_req_field, ""))):
                        _missing_fields.append(_display)
                if _missing_fields:
                    raise _ModelOpError(f"Model '{target}' missing required config: {', '.join(_missing_fields)}")
                _target_model_name_resolved = resolve_env_vars(str(_target_mcc.get("model_name", "")))
                _target_entry["is_default"] = True
                for _i, _e in enumerate(_raw_defaults):
                    if _i == _target_idx or not isinstance(_e, dict):
                        continue
                    _other_mcc = _e.get("model_client_config") or {}
                    _other_name = resolve_env_vars(str(_other_mcc.get("model_name", "")))
                    if _other_name == _target_model_name_resolved and _e.get("is_default") is True:
                        _e["is_default"] = False
                _others = [_e for _i, _e in enumerate(_raw_defaults) if _i != _target_idx]
                models["defaults"] = [_target_entry] + _others
                _switch_result["name"] = resolve_env_vars(
                    str((_target_entry.get("model_client_config") or {}).get("model_name", target)))
                return data
            update_config(_switch_mutate)
        except _ModelOpError as _op_err:
            await channel.send_response(ws, req_id, ok=False, error=str(_op_err))
            return
        except Exception as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e))
            return
        logger.info("[cli command.model] 切换，已更新 models.defaults 首位: %s", target)
        _target_model_name = _switch_result.get("name", target)

        # 先回包再执行 Agent 热重载（与 config.set 保持一致），
        # 避免 WebSocket 长时间无响应、CLI 误以为无反馈 / 超时。
        await channel.send_response(ws, req_id, ok=True, payload={
            "current": _target_model_name,
            "requested": target,
            "type": "switched",
            "applied": True,
        })

        # 后台触发 AgentServer reload + on_config_saved（不阻塞 WS 消息循环）
        _config_payload = get_config()

        async def _model_switch_background():
            _reload_env = e2a_from_agent_fields(
                request_id=req_id,
                channel_id="cli",
                session_id=session_id,
                req_method=ReqMethod.AGENT_RELOAD_CONFIG,
                params={
                    "config": _config_payload,
                    "env": {},
                    "target_channel_id": "tui",
                    "target_session_id": session_id,
                    "reason": "model_switch",
                },
                is_stream=False,
                timestamp=time.time(),
                user_id=user_id,
            )
            try:
                await _send_tui_agent_request(
                    real_client, _reload_env, label="command.model.switch",
                )
            except Exception as _e_reload:
                logger.warning("[cli model.switch] AGENT_RELOAD_CONFIG failed: %s", _e_reload)
            if on_config_saved:
                try:
                    _cb = on_config_saved(set(), env_updates={}, config_payload=_config_payload)
                    if inspect.isawaitable(_cb):
                        await _cb
                except Exception as _e2:
                    logger.warning("[cli model.switch] on_config_saved failed: %s", _e2)
            logger.info("[cli command.model] 切换完成: current=%s", _target_model_name)

        asyncio.create_task(_model_switch_background())
        return

    async def _models_list(ws, req_id, params, session_id):
        try:
            config = get_config()
            models = get_default_models(config)
            result = []
            # 显式配置的上下文窗口上限（react.context_engine_config.context_window_tokens）
            # 优先级高于按模型名解析，与 AgentServer 侧 ContextEngine 行为保持一致
            cec = (config.get("react", {}) or {}).get("context_engine_config", {}) or {}
            cw_override = cec.get("context_window_tokens")
            if not (isinstance(cw_override, int) and cw_override > 0):
                cw_override = None
            for entry in models:
                mcc = entry.get("model_client_config", {})
                mco = entry.get("model_config_obj", {})
                model_name = mcc.get("model_name", "")
                # 解析模型的上下文窗口大小
                context_window_tokens = 0
                try:
                    from openjiuwen.core.context_engine.context.context_utils import ContextUtils
                    context_window_tokens = ContextUtils.resolve_context_max(
                        model_name=model_name,
                        fallback_context_window_tokens=cw_override,
                    )
                except Exception:
                    logger.debug("Failed to resolve context_window_tokens for model %s", model_name, exc_info=True)
                result.append({
                    "model_name": model_name,
                    "api_base": mcc.get("api_base", ""),
                    "api_key": mcc.get("api_key", ""),
                    "model_provider": mcc.get("client_provider", ""),
                    "temperature": mco.get("temperature", 0.95),
                    "reasoning_level": "off" if mco.get("reasoning_level") is False else mco.get("reasoning_level", ""),
                    "alias": entry.get("alias", ""),
                    "context_window_tokens": context_window_tokens,
                })
            active_model = result[0]["model_name"] if result else ""
            await channel.send_response(ws, req_id, ok=True, payload={
                "models": result,
                "active_model": active_model,
            })
        except Exception as exc:
            logger.warning("[models.list] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _3rdagent_list(ws, req_id, params, session_id, user_id=None):
        params = params if isinstance(params, dict) else {}
        current = str(
            getattr(ws, "_gateway_agent_type", None)
            or params.get("agent_type")
            or "jiuwenswarm"
        ).strip() or "jiuwenswarm"
        uid = str(user_id or getattr(ws, "_gateway_user_id", None) or "").strip()
        try:
            result = await third_agent.thirdagent_list(
                user_id=uid,
                current_agent_type=current,
            )
        except Exception as exc:
            logger.warning("[3rdagent.list] %s", exc)
            await channel.send_response(
                ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR"
            )
            return
        if not result.get("ok"):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(result.get("error") or "3rdagent.list unsupported"),
                code=str(result.get("code") or "UNSUPPORTED"),
            )
            return
        await channel.send_response(
            ws, req_id, ok=True, payload=dict(result.get("payload") or {})
        )

    async def _3rdagent_switch(ws, req_id, params, session_id, user_id=None):
        del session_id  # do not use gateway req_id fallback; require explicit params.session_id
        params = params if isinstance(params, dict) else {}
        uid = str(user_id or getattr(ws, "_gateway_user_id", None) or "").strip()
        explicit_session_id = resolve_3rdagent_switch_session_id(params)
        if not explicit_session_id:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="session_id is required for 3rdagent.switch",
                code="BAD_REQUEST",
            )
            return
        try:
            result = await third_agent.thirdagent_switch(
                user_id=uid,
                agent_type=str(params.get("agent_type") or ""),
                session_id=explicit_session_id,
                params=params,
            )
        except Exception as exc:
            logger.warning("[3rdagent.switch] %s", exc)
            await channel.send_response(
                ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR"
            )
            return
        if not result.get("ok"):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(result.get("error") or "3rdagent.switch unsupported"),
                code=str(result.get("code") or "UNSUPPORTED"),
            )
            return
        payload = dict(result.get("payload") or {})
        switched = str(payload.get("agent_type") or "").strip()
        if switched:
            setattr(ws, "_gateway_agent_type", switched)
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    channel.register_local_handler(path, "config.get", _config_get)
    channel.register_local_handler(path, "config.set", _config_set)
    channel.register_local_handler(path, "config.validate_model", _config_validate_model)
    channel.register_local_handler(path, "models.list", _models_list)
    channel.register_local_handler(path, "3rdagent.list", _3rdagent_list)
    channel.register_local_handler(path, "3rdagent.switch", _3rdagent_switch)
    channel.register_local_handler(path, "session.list", _session_list)
    channel.register_local_handler(path, "session.create", _session_create)
    channel.register_local_handler(path, "session.delete", _session_delete)
    channel.register_local_handler(path, "session.rename", _session_rename)
    channel.register_local_handler(path, "session.color_set", _session_color_set)
    channel.register_local_handler(path, "session.preview", _session_preview)
    channel.register_local_handler(path, "session.rewind", _session_rewind)
    channel.register_local_handler(path, "session.rewind_and_restore", _session_rewind_and_restore)
    channel.register_local_handler(path, "session.restore_files", _session_restore_files)
    channel.register_local_handler(path, "command.rewind_compact", _command_rewind_compact)
    channel.register_local_handler(path, "history.list_turns", _history_list_turns)
    channel.register_local_handler(path, "chat.send", _chat_send)
    channel.register_local_handler(path, "chat.resume", _chat_resume)
    channel.register_local_handler(path, "chat.interrupt", _chat_interrupt)
    channel.register_local_handler(path, "tui.disconnect", _tui_disconnect_request)
    channel.register_local_handler(path, "chat.user_answer", _chat_user_answer)
    channel.register_local_handler(path, "chat.swarmflow_reply", _chat_swarmflow_reply)
    channel.register_local_handler(path, "history.get", _history_get)
    channel.register_local_handler(path, "command.model", _command_model)

    # ── Hooks RPC handlers ─────────────────────────────────────────────
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
        except Exception as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    channel.register_local_handler(path, "hooks.list", _hooks_list)

    # ── Memory RPC handlers ────────────────────────────────────────────
    from jiuwenswarm.agents.harness.common.memory_rpc import (
        handle_memory_list,
        handle_memory_edit,
        handle_memory_status,
        handle_memory_toggle,
        handle_memory_open,
    )
    from jiuwenswarm.common.utils import get_agent_workspace_dir

    def _resolve_project_dir(params):
        project_dir = params.get("project_dir")
        if isinstance(project_dir, str) and project_dir:
            return project_dir
        trusted_dirs = params.get("trusted_dirs")
        if isinstance(trusted_dirs, list) and trusted_dirs:
            return str(trusted_dirs[0])
        cwd = params.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
        return None

    async def _memory_list(ws, req_id, params, session_id):
        workspace = str(get_agent_workspace_dir())
        mode = params.get("mode", "plan")
        project_dir = _resolve_project_dir(params)
        if project_dir:
            params = {**params, "project_dir": project_dir}
        try:
            result = await handle_memory_list(workspace, mode, params)
            await channel.send_response(ws, req_id, ok=True, payload=result)
        except Exception as exc:
            logger.warning("[memory.list] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _memory_edit(ws, req_id, params, session_id):
        workspace = str(get_agent_workspace_dir())
        project_dir = _resolve_project_dir(params)
        if project_dir:
            params = {**params, "project_dir": project_dir}
        try:
            result = await handle_memory_edit(workspace, params)
            await channel.send_response(ws, req_id, ok=True, payload=result)
        except Exception as exc:
            logger.warning("[memory.edit] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _memory_status(ws, req_id, params, session_id):
        workspace = str(get_agent_workspace_dir())
        mode = params.get("mode", "plan")
        project_dir = _resolve_project_dir(params)
        if project_dir:
            params = {**params, "project_dir": project_dir}
        try:
            result = await handle_memory_status(workspace, mode, params)
            await channel.send_response(ws, req_id, ok=True, payload=result)
        except Exception as exc:
            logger.warning("[memory.status] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _memory_toggle(ws, req_id, params, session_id):
        workspace = str(get_agent_workspace_dir())
        mode = params.get("mode", "plan")
        try:
            result = await handle_memory_toggle(workspace, mode, params)
            await channel.send_response(ws, req_id, ok=True, payload=result)
        except Exception as exc:
            logger.warning("[memory.toggle] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _memory_open(ws, req_id, params, session_id):
        workspace = str(get_agent_workspace_dir())
        project_dir = _resolve_project_dir(params)
        if project_dir:
            params = {**params, "project_dir": project_dir}
        try:
            result = await handle_memory_open(workspace, params)
            await channel.send_response(ws, req_id, ok=True, payload=result)
        except Exception as exc:
            logger.warning("[memory.open] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    channel.register_local_handler(path, "memory.list", _memory_list)
    channel.register_local_handler(path, "memory.edit", _memory_edit)
    channel.register_local_handler(path, "memory.status", _memory_status)
    channel.register_local_handler(path, "memory.toggle", _memory_toggle)
    channel.register_local_handler(path, "memory.open", _memory_open)

    # ── Cron RPC handlers ────────────────────────────────────────────

    def _get_cron():
        """Resolve cron_controller from ref dict or direct instance."""
        if isinstance(cron_controller_ref, dict):
            return cron_controller_ref.get("value")
        return cron_controller_ref

    async def _cron_job_list(ws, req_id, params, session_id):
        cc = _get_cron()
        if cc is None:
            await channel.send_response(ws, req_id, ok=False, error="cron not available", code="INTERNAL_ERROR")
            return
        try:
            jobs = await cc.list_jobs()
            await channel.send_response(ws, req_id, ok=True, payload={"jobs": jobs})
        except Exception as exc:
            logger.warning("[cron.job.list] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _cron_job_meta(ws, req_id, params, session_id):
        cc = _get_cron()
        if cc is None:
            await channel.send_response(ws, req_id, ok=False, error="cron not available", code="INTERNAL_ERROR")
            return
        try:
            await channel.send_response(ws, req_id, ok=True, payload=cc.job_metadata())
        except Exception as exc:
            logger.warning("[cron.job.meta] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

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
        try:
            job = await cc.get_job(job_id)
            if job is None:
                await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
                return
            await channel.send_response(ws, req_id, ok=True, payload={"job": job})
        except Exception as exc:
            logger.warning("[cron.job.get] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _cron_job_create(ws, req_id, params, session_id):
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
            # project_dir 默认值：TUI 前端已自动注入；仅当「未传」时从当前会话 metadata 兜底
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
        except Exception as exc:
            logger.warning("[cron.job.create] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST")

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
        except KeyError as exc:
            # ZoneInfoNotFoundError is a subclass of KeyError; only treat
            # bare "job not found" KeyError as NOT_FOUND, otherwise surface
            # the real error message.
            if "job not found" in str(exc):
                await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
            else:
                logger.warning("[cron.job.update] %s", exc)
                await channel.send_response(ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST")
        except Exception as exc:
            logger.warning("[cron.job.update] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST")

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
        try:
            deleted = await cc.delete_job(job_id)
            if not deleted:
                await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
                return
            await channel.send_response(ws, req_id, ok=True, payload={"deleted": True})
        except Exception as exc:
            logger.warning("[cron.job.delete] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

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
        try:
            job = await cc.toggle_job(job_id, bool(enabled))
            await channel.send_response(ws, req_id, ok=True, payload={"job": job})
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
        except Exception as exc:
            logger.warning("[cron.job.toggle] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

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
        except Exception as exc:
            logger.warning("[cron.job.preview] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST")

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
            # 对齐 chat.send 的 {accepted, session_id} 语义
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
        except Exception as exc:
            logger.warning("[cron.job.run_now] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    channel.register_local_handler(path, "cron.job.list", _cron_job_list)
    channel.register_local_handler(path, "cron.job.meta", _cron_job_meta)
    channel.register_local_handler(path, "cron.job.get", _cron_job_get)
    channel.register_local_handler(path, "cron.job.create", _cron_job_create)
    channel.register_local_handler(path, "cron.job.update", _cron_job_update)
    channel.register_local_handler(path, "cron.job.delete", _cron_job_delete)
    channel.register_local_handler(path, "cron.job.toggle", _cron_job_toggle)
    channel.register_local_handler(path, "cron.job.preview", _cron_job_preview)
    channel.register_local_handler(path, "cron.job.run_now", _cron_job_run_now)


def build_cli_route_binding(bind: CliRouteBindParams) -> GatewayRouteBinding:
    def _install(channel: Any) -> None:
        register_cli_handlers(
            CliHandlersBindParams(
                channel=channel,
                agent_client=bind.agent_client,
                message_handler=bind.message_handler,
                third_agent=bind.third_agent,
                on_config_saved=bind.on_config_saved,
                path=bind.path,
                cron_controller=bind.cron_controller,
            )
        )

    async def _tui_disconnect(
        _ws: Any,
        stale_session_keys: list[tuple[str, str]],
        stale_request_keys: list[tuple[str, str]] | None = None,
    ) -> None:
        if bool(getattr(_ws, "_jiuwenswarm_tui_user_exit", False)):
            return
        mh = bind.message_handler
        if mh is None:
            return
        # NOTE: do not early-return on empty stale_session_keys; in-flight streams
        # may still be tracked under stale_request_keys even when _session_to_client
        # was overwritten by a later reconnect on the same session_id.
        request_keys = stale_request_keys or []
        if not stale_session_keys and not request_keys:
            return
        _ws_user_id = getattr(_ws, "_gateway_user_id", None)
        if hasattr(mh, "schedule_cancel_agent_sessions_on_disconnect"):
            await mh.schedule_cancel_agent_sessions_on_disconnect(
                stale_session_keys,
                stale_request_keys=request_keys,
                user_id=_ws_user_id,
            )
            return
        await mh.cancel_agent_sessions_on_disconnect(
            stale_session_keys,
            stale_request_keys=request_keys,
            user_id=_ws_user_id,
        )

    def _tui_session_bound(channel_id: str, session_id: str) -> None:
        mh = bind.message_handler
        if mh is None or not hasattr(mh, "cancel_scheduled_disconnect_cancel"):
            return
        mh.cancel_scheduled_disconnect_cancel(channel_id, session_id)

    return GatewayRouteBinding(
        path=bind.path,
        channel_id=bind.channel_id,
        forward_methods=CLI_FORWARD_REQ_METHODS,
        forward_no_local_handler_methods=CLI_FORWARD_NO_LOCAL_HANDLER_METHODS,
        install=_install,
        disconnect_handler=_tui_disconnect,
        session_bind_handler=_tui_session_bound,
        ws_channel=bind.ws_channel,
    )
