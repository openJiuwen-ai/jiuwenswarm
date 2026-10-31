# Gateway 侧副本，源文件 jiuwenswarm/common/config_panel/config_set_handlers.py；改动请同步两侧。
# 本副本仅保留 gateway 实际引用的符号；同步母仓时请勿整体覆盖。
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
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


from gateway.common.config import (
    get_config,
)
from gateway.common.config_panel.external_cli_read import (
    EXTERNAL_CLI_AGENT_KINDS,
    _uploaded_cli_path,
    external_cli_agent_kinds,
)
from gateway.common.paths import get_env_file

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 热更新 scope 常量与变更集（自 app_web_handlers 迁出）
# --------------------------------------------------------------------------- #


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
    "jev_openrouter_api_key": "OPENROUTER_API_KEY",
    "jev_typesafe_api_key": "TYPESAFE_API_KEY",
    "jev_custom_api_key": "JEV_CUSTOM_API_KEY",
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

# 来自 config.yaml 的配置项（前端 param 名 -> config.yaml 路径）
# 调度模式开关（modes.team.jiuwen_team.dispatch_mode / enable_task_verification /
# enable_member_messaging），改动后需重载 team。
# Settings toggles that only change YAML; AgentServer reads the file next turn.
# Scopes that must reach AgentServer before config.save_all returns.
AGENT_RELOAD_SCOPES = frozenset({
    "model",
    "multimodal",
    "search",
    "team",
    "permissions",
    "agent_runtime",
    "proactive",
})


def needs_agent_reload(scopes: set[str] | None) -> bool:
    """True when this save must wait for AgentServer to apply it."""

    if not scopes:
        return True
    return bool(scopes & AGENT_RELOAD_SCOPES)


DEFAULT_EXTERNAL_CLI_PUBLISH_HOST = "127.0.0.1"
DEFAULT_EXTERNAL_CLI_PUBLISH_PORT = "19000"
EXTERNAL_CLI_PUBLISH_PATH = "/ws"
UNSUPPORTED_WINDOWS_CLI_SUFFIXES = {".bat", ".cmd", ".ps1"}


# --------------------------------------------------------------------------- #
# config panel 取值/扁平化工具（config.get 与 config.set 共用）
# --------------------------------------------------------------------------- #


def get_nested_config_value(source: dict[str, Any], path: tuple[str, ...], default: Any) -> Any:
    current: Any = source
    for segment in path:
        if not isinstance(current, dict) or segment not in current:
            return default
        current = current.get(segment)
    return default if current is None else current


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


def external_cli_managed_cli_path(cli_agent: str) -> str:
    """返回内置（托管安装）CLI 可执行文件路径；不可用时返回空串。

    与 external_cli_runtime._validate_staged_runtime 的布局保持一致：
    claude -> claude_agent_sdk/_bundled/claude(.exe)
    codex  -> codex_cli_bin/bin/codex(.exe)
    通过 importlib 定位 site-packages（冻结与非冻结环境均适用）。
    """
    relative = {
        "claude": (
            "claude_agent_sdk",
            "_bundled",
            "claude.exe" if os.name == "nt" else "claude",
        ),
        "codex": (
            "codex_cli_bin",
            "bin",
            "codex.exe" if os.name == "nt" else "codex",
        ),
    }.get(cli_agent)
    if relative is None:
        return ""
    try:
        spec = importlib.util.find_spec(relative[0])
    except Exception:  # noqa: BLE001
        return ""
    if spec is None or not spec.origin:
        return ""
    package_dir = Path(spec.origin).resolve().parent
    candidate = package_dir.joinpath(*relative[1:])
    return str(candidate) if candidate.is_file() else ""


def _resolve_windows_cli_executable_from_path(cli_agent: str) -> str:
    """PATH lookup restricted to real executables.

    npm-style global installs drop `claude`, `claude.cmd` and `claude.ps1`
    side by side; the PATHEXT-driven `shutil.which` resolves the script shim,
    which the detector rejects as unsupported and the path dropdown would
    then offer as a dead entry (#5678). Only an `.exe` is launchable through
    CreateProcess, so scan PATH for that instead. Windows-only: other
    platforms have no `.exe` and their `shutil.which` never resolves the
    script shims.
    """
    if not is_windows_platform():
        return ""
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry.strip():
            continue
        candidate = Path(entry) / f"{cli_agent}.exe"
        if candidate.is_file():
            return str(candidate)
    return ""


def resolve_external_cli_path(cli_agent: str, cli_path: str = "") -> tuple[str, str, str]:
    from gateway.common.windows_cli_paths import resolve_preserved_cli_path

    requested = resolve_preserved_cli_path(cli_path.strip())
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
    if resolved and is_windows_platform() and Path(resolved).suffix.lower() in UNSUPPORTED_WINDOWS_CLI_SUFFIXES:
        # The PATHEXT lookup hit a script shim (.cmd/.bat/.ps1) that this
        # detector cannot run; prefer a real executable from the same PATH so
        # the UI does not offer the shim as a dead path option (#5678).
        resolved = _resolve_windows_cli_executable_from_path(cli_agent)
        if not resolved:
            # npm-style installs often ship only the shim without a native
            # .exe; report the known windows-script situation instead of a
            # misleading "not found" so the UI can show the accurate
            # unsupported-shim message (#5678).
            return "", "windows_script", "windows_script"
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


def detect_external_cli_agent(
    cli_agent: str, cli_path: str = "", use_builtin: bool = False
) -> dict[str, Any]:
    normalized_agent = cli_agent.strip().lower()
    if normalized_agent not in EXTERNAL_CLI_AGENT_KINDS and normalized_agent in external_cli_agent_kinds():
        return _detect_custom_agent_cli(normalized_agent, cli_path)
    if normalized_agent not in EXTERNAL_CLI_AGENT_KINDS:
        return {
            "cli_agent": normalized_agent,
            "status": "unavailable",
            "path": "",
            "version": "",
            "reference_version": "",
            "message": f"unsupported cli_agent: {cli_agent}",
        }

    return _detect_cli_at(normalized_agent, cli_path, use_builtin)


def _detect_custom_agent_cli(cli_agent: str, cli_path: str) -> dict[str, Any]:
    """Detect the CLI a custom agent will run.

    An explicit path is checked like Claude's and Codex's. Without one the
    agent runs its uploaded CLI when there is one; otherwise the provider
    package picks its CLI itself, which cannot be checked from here.
    """
    requested = cli_path.strip() or _uploaded_cli_path(cli_agent)
    if requested:
        return _detect_cli_at(cli_agent, requested, False)
    return {
        "cli_agent": cli_agent,
        "status": "ok",
        "path": "",
        "version": "",
        "reference_version": "",
        "reason": "provider_managed",
        "message": "",
    }


def _detect_cli_at(normalized_agent: str, cli_path: str, use_builtin: bool = False) -> dict[str, Any]:
    # use_builtin 且未显式给路径时，优先解析内置托管 CLI；解析失败再回退 PATH 搜索。
    effective_cli_path = cli_path.strip()
    if not effective_cli_path and use_builtin:
        effective_cli_path = external_cli_managed_cli_path(normalized_agent)
    resolved_path, path_error, path_reason = resolve_external_cli_path(
        normalized_agent, effective_cli_path
    )
    reference_version = external_cli_reference_version(normalized_agent)
    if not resolved_path:
        return {
            "cli_agent": normalized_agent,
            "status": "unsupported" if path_reason in ("directory", "windows_script") else "missing",
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


def build_external_cli_publish_url() -> str:
    host = str(os.getenv("WEB_HOST") or DEFAULT_EXTERNAL_CLI_PUBLISH_HOST).strip()
    if host in {"", "0.0.0.0", "::", "[::]"}:
        host = DEFAULT_EXTERNAL_CLI_PUBLISH_HOST
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        host = f"[{host}]"

    port = str(os.getenv("WEB_PORT") or DEFAULT_EXTERNAL_CLI_PUBLISH_PORT).strip()
    return f"ws://{host}:{port}{EXTERNAL_CLI_PUBLISH_PATH}"


def _get_crypto_provider() -> Any:
    """经 common 钩子取 crypto provider（未注册/未就绪返回 None）。

    gateway 进程由 ``extensions.registry`` 在 create_instance 时安装桥接；
    AgentServer 进程无 crypto 扩展时 api_key 以明文落库（与原实现一致）。
    """
    try:
        from gateway.extensions.base_crypto import get_crypto_provider

        return get_crypto_provider()
    except Exception:  # noqa: BLE001
        return None


async def _clear_agent_config_cache(agent_client=None) -> None:
    """写回 config.yaml 后清除 agent 侧配置缓存，使下次读取时得到最新文件内容。"""
    import uuid as _uuid

    try:
        if agent_client is not None:
            from openjiuwen_protocol.e2a.gateway_normalize import e2a_from_agent_fields
            from openjiuwen_protocol.message import ReqMethod

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


# --------------------------------------------------------------------------- #
# handler 实现（channel 语义与 app_web_handlers 原实现一致）
# --------------------------------------------------------------------------- #
